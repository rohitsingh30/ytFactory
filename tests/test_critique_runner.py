"""Tests for the critique runner stack (2026-05-11).

Covers:

- ``pipeline/critique/messages.py`` — the typed Firestore subcollection
  helpers. Pure-ish (mocked Firestore client) so they run instantly.
- ``pipeline/critique/agent.py`` — prompt builder + per-agent argv
  builder + summary parser + the subprocess turn driver.
- ``pipeline/critique/runner.py`` — the daemon loop, claim
  transaction, per-message turn driver, and full happy-path
  end-to-end with a fake agent + fake firestore.

The full agent.run_agent_turn is exercised against a tiny shell
script that emulates `claude -p ...` so we don't need network or
the real CLI installed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pipeline.critique import agent as agent_mod
from pipeline.critique import messages as msg_mod
from pipeline.critique import runner as runner_mod


# ===========================================================================
# Fake Firestore in-process — close enough to real for runner-level tests.
# ===========================================================================


class _FakeFirestoreField:
    """Sentinel for SERVER_TIMESTAMP equivalent."""
    pass


_SERVER_TIMESTAMP_SENTINEL = _FakeFirestoreField()


class _FakeSnap:
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDocRef:
    def __init__(self, parent, doc_id: str):
        self._parent = parent
        self._doc_id = doc_id
        self.id = doc_id  # mirror real DocumentReference.id

    def get(self, transaction=None):
        return _FakeSnap(self._doc_id, self._parent.docs.get(self._doc_id))

    def set(self, data, merge=False):
        cur = self._parent.docs.get(self._doc_id)
        if cur and merge:
            cur.update(data)
            self._parent.docs[self._doc_id] = cur
        else:
            self._parent.docs[self._doc_id] = dict(data)

    def update(self, data):
        cur = self._parent.docs.setdefault(self._doc_id, {})
        cur.update(data)

    def collection(self, name: str):
        # Subcollection — keyed under (parent_collection, parent_doc_id, sub_collection)
        return self._parent._sub_collection(self._doc_id, name)


class _FakeQuery:
    def __init__(self, results):
        self._results = results

    def where(self, *args, **kwargs):
        return self  # assume callers pass compatible filters; tests stub directly

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def get(self):
        return self._results

    def stream(self):
        return iter(self._results)


class _FakeCollection:
    def __init__(self, parent, name: str):
        self._parent = parent
        self._name = name
        self.docs: dict[str, dict] = {}
        self._sub: dict[tuple[str, str], "_FakeCollection"] = {}
        self._auto_id = 0

    def document(self, doc_id: str | None = None) -> _FakeDocRef:
        if doc_id is None:
            self._auto_id += 1
            doc_id = f"auto-{self._name}-{self._auto_id}"
        return _FakeDocRef(self, doc_id)

    def where(self, *args, **kwargs):
        # For fetch_messages tests we don't need where; return all docs as snaps.
        return _FakeQuery([_FakeSnap(k, v) for k, v in self.docs.items()])

    def order_by(self, *args, **kwargs):
        # Stable order = insertion order = our dict iteration order on py3.7+.
        return _FakeQuery([_FakeSnap(k, v) for k, v in self.docs.items()])

    def stream(self):
        return iter(_FakeSnap(k, v) for k, v in self.docs.items())

    def _sub_collection(self, parent_doc_id: str, name: str) -> "_FakeCollection":
        key = (parent_doc_id, name)
        if key not in self._sub:
            self._sub[key] = _FakeCollection(self._parent, name)
        return self._sub[key]


class _FakeFirestoreClient:
    """In-process Firestore double sufficient for runner tests.

    Intentionally minimal — full transactional semantics (atomic
    read-modify-write) are emulated by serialising on a Lock so the
    claim_critique txn can be exercised under contention.
    """
    def __init__(self):
        self._collections: dict[str, _FakeCollection] = {}
        self._txn_lock = threading.Lock()

    def collection(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            self._collections[name] = _FakeCollection(self, name)
        return self._collections[name]

    def transaction(self):
        return _FakeTransaction(self)


class _FakeTransaction:
    """Exactly enough surface for runner.claim_critique. The real
    Firestore SDK's @transactional decorator hides the txn arg by
    binding it as a closure; we mimic that by exposing get/update."""
    def __init__(self, client):
        self._client = client

    def update(self, ref: _FakeDocRef, data: dict) -> None:
        ref.update(data)

    # context-manager-style helpers used by the @transactional adapter
    def __call__(self, fn):
        return fn(self)


# ---------------------------------------------------------------------------
# Tests for messages.py
# ---------------------------------------------------------------------------


class MessagesTests(unittest.TestCase):
    def setUp(self):
        # Patch SERVER_TIMESTAMP so we don't need the real firestore module
        # at import time. messages.add_message imports lazily, so we patch
        # the firestore module reference inside the function.
        self.client = _FakeFirestoreClient()

    def _patch_firestore(self):
        fake_module = mock.MagicMock()
        fake_module.SERVER_TIMESTAMP = _SERVER_TIMESTAMP_SENTINEL
        return mock.patch.dict("sys.modules", {"google.cloud.firestore": fake_module}), \
               mock.patch.dict("sys.modules", {"google.cloud": mock.MagicMock(firestore=fake_module)})

    def test_add_message_user(self):
        p1, _ = self._patch_firestore()
        with p1:
            mid = msg_mod.add_message(self.client, "c1", role="user", text="hi")
        # Doc landed in the messages subcollection.
        sub = self.client.collection("critiques").document("c1").collection("messages")
        self.assertEqual(len(sub.docs), 1)
        only_doc = next(iter(sub.docs.values()))
        self.assertEqual(only_doc["role"], "user")
        self.assertEqual(only_doc["text"], "hi")
        self.assertNotIn("action", only_doc)
        self.assertTrue(mid.startswith("auto-messages-"))

    def test_add_message_with_action(self):
        p1, _ = self._patch_firestore()
        with p1:
            msg_mod.add_action_message(
                self.client, "c1",
                action=msg_mod.ACTION_GATE_PASSED,
                text="pytest_full: 247 passed",
                action_data={"name": "pytest_full"},
            )
        only_doc = next(iter(self.client.collection("critiques").document("c1").collection("messages").docs.values()))
        self.assertEqual(only_doc["action"], "gate_passed")
        self.assertEqual(only_doc["action_data"], {"name": "pytest_full"})

    def test_add_message_rejects_bad_role(self):
        with self.assertRaises(ValueError) as ctx:
            msg_mod.add_message(self.client, "c1", role="hax", text="x")
        self.assertIn("role must be one of", str(ctx.exception))

    def test_add_message_rejects_bad_action(self):
        with self.assertRaises(ValueError):
            msg_mod.add_message(self.client, "c1", role="agent", text="x", action="not_a_real_action")

    def test_add_message_rejects_empty_text_and_action(self):
        # Empty text + no action → silent ghost message; refuse.
        with self.assertRaises(ValueError):
            msg_mod.add_message(self.client, "c1", role="user", text="")

    def test_fetch_messages_round_trip(self):
        p1, _ = self._patch_firestore()
        with p1:
            msg_mod.add_user_message(self.client, "c1", "hello")
            msg_mod.add_agent_message(self.client, "c1", "hi back",
                                      action=msg_mod.ACTION_AGENT_FINISHED)
        msgs = msg_mod.fetch_messages(self.client, "c1")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].role, "user")
        self.assertEqual(msgs[0].text, "hello")
        self.assertEqual(msgs[1].role, "agent")
        self.assertEqual(msgs[1].action, msg_mod.ACTION_AGENT_FINISHED)

    def test_messages_to_prompt_history_render(self):
        m1 = msg_mod.ChatMessage(message_id="a", role="user", text="hello", ts=None)
        m2 = msg_mod.ChatMessage(message_id="b", role="agent", text="hi", ts=None)
        m3 = msg_mod.ChatMessage(
            message_id="c", role="agent", text="", ts=None,
            action=msg_mod.ACTION_GATE_RUNNING,
        )
        out = msg_mod.messages_to_prompt_history([m1, m2, m3])
        self.assertIn("USER: hello", out)
        self.assertIn("AGENT: hi", out)
        self.assertIn("[gate_running]", out)


# ---------------------------------------------------------------------------
# Tests for agent.py: prompt + argv + parser + run_agent_turn
# ---------------------------------------------------------------------------


class BuildPromptTests(unittest.TestCase):
    def test_includes_repo_root_and_job_context(self):
        p = agent_mod.build_prompt(
            repo_root=Path("/repo"),
            job_context={"channel": "mystoriesanimated", "mp4_local": "/tmp/x.mp4"},
            conversation_history="USER: bug found\nAGENT: ok",
            latest_user_message="please fix the closer",
        )
        self.assertIn("/repo", p)
        self.assertIn("channel: mystoriesanimated", p)
        self.assertIn("mp4_local: /tmp/x.mp4", p)
        self.assertIn("USER: bug found", p)
        self.assertIn("please fix the closer", p)
        # Hard rules embedded so the agent always sees them, even on
        # turn 1 with empty history.
        self.assertIn('Hard rules:', p)
        self.assertIn('"type":"agent_summary"', p)


class ArgvBuilderTests(unittest.TestCase):
    def test_claude_argv_carries_dangerously_skip(self):
        argv = agent_mod._argv_for_agent(
            agent_mod.AGENT_CLAUDE, "PROMPT",
            repo_root=Path("/repo"),
        )
        self.assertEqual(argv[0], "claude")
        self.assertIn("-p", argv)
        # "PROMPT" must appear as the value AFTER -p (otherwise the agent
        # treats it as a positional-prompt arg, which IS valid for claude
        # but not consistent with the contract).
        self.assertEqual(argv[argv.index("-p") + 1], "PROMPT")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--add-dir", argv)

    def test_copilot_argv_carries_allow_all(self):
        argv = agent_mod._argv_for_agent(
            agent_mod.AGENT_COPILOT, "PROMPT",
            repo_root=Path("/repo"),
        )
        self.assertEqual(argv[0], "copilot")
        self.assertIn("--allow-all", argv)
        self.assertIn("-p", argv)

    def test_unknown_agent_rejected(self):
        with self.assertRaises(ValueError):
            agent_mod._argv_for_agent("rm-rf", "x", repo_root=Path("/"))


class ParseAgentSummaryTests(unittest.TestCase):
    GOOD = (
        'I read the file and added a test.\n'
        '{"type":"agent_summary","action":"done","files_changed":["pipeline/x.py"],'
        '"tests_added":["tests/test_x.py"],"rationale":"fixed it","follow_up_questions":[]}'
    )

    def test_parses_well_formed_summary(self):
        d = agent_mod.parse_agent_summary(self.GOOD)
        self.assertIsNotNone(d)
        self.assertEqual(d["action"], "done")
        self.assertEqual(d["files_changed"], ["pipeline/x.py"])

    def test_handles_fenced_code_block(self):
        wrapped = "```json\n" + self.GOOD.split("\n", 1)[1] + "\n```"
        d = agent_mod.parse_agent_summary(wrapped)
        self.assertIsNotNone(d)
        self.assertEqual(d["action"], "done")

    def test_returns_none_on_missing(self):
        self.assertIsNone(agent_mod.parse_agent_summary("just plain text"))

    def test_returns_none_on_malformed_json(self):
        # The block looks like a summary but the JSON is busted.
        bad = '{"type":"agent_summary","action":"done",, broken}'
        self.assertIsNone(agent_mod.parse_agent_summary(bad))


class RunAgentTurnTests(unittest.TestCase):
    """Drive the subprocess pipeline through a tiny fake agent.

    We replace the argv builder with one that runs a python -c
    snippet emitting the same shape of output as the real CLI. This
    keeps the test fast, hermetic, and entirely covers the
    streaming + parsing + timeout paths.
    """

    def _fake_agent_argv(self, behaviour: str) -> list[str]:
        snippets = {
            "happy": (
                "import sys, json, time\n"
                "print('reading files')\n"
                "sys.stdout.flush()\n"
                "print('done!')\n"
                "print(json.dumps({"
                "'type':'agent_summary','action':'done',"
                "'files_changed':['x.py'],'tests_added':['t.py'],"
                "'rationale':'ok','follow_up_questions':[]}))\n"
            ),
            "need_more_info": (
                "import sys, json\n"
                "print('hmm not sure')\n"
                "print(json.dumps({"
                "'type':'agent_summary','action':'need_more_info',"
                "'files_changed':[],'tests_added':[],"
                "'rationale':'unclear','follow_up_questions':['which channel?']}))\n"
            ),
            "no_summary": (
                "print('done with no summary block')\n"
            ),
            "non_zero_exit": (
                "import sys; print('boom on stderr', file=sys.stderr); sys.exit(7)\n"
            ),
        }
        return [sys.executable, "-c", snippets[behaviour]]

    def _patched_run_turn(self, agent_kind: str, behaviour: str, **kwargs):
        argv = self._fake_agent_argv(behaviour)
        with mock.patch.object(agent_mod, "_argv_for_agent", return_value=argv):
            return agent_mod.run_agent_turn(
                agent_kind, "PROMPT", repo_root=Path.cwd(), **kwargs,
            )

    def test_happy_path_returns_done(self):
        seen: list[str] = []
        result = self._patched_run_turn(
            agent_mod.AGENT_CLAUDE, "happy",
            on_stdout_line=seen.append,
        )
        self.assertEqual(result.action, "done")
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.summary["files_changed"], ["x.py"])
        self.assertEqual(result.exit_code, 0)
        # Streaming callback fired for at least the two non-summary lines.
        self.assertTrue(any("reading files" in l for l in seen))
        self.assertTrue(any("done!" in l for l in seen))

    def test_need_more_info_path(self):
        result = self._patched_run_turn(agent_mod.AGENT_COPILOT, "need_more_info")
        self.assertEqual(result.action, "need_more_info")
        self.assertEqual(result.summary["follow_up_questions"], ["which channel?"])

    def test_missing_summary_is_failure(self):
        result = self._patched_run_turn(agent_mod.AGENT_CLAUDE, "no_summary")
        self.assertEqual(result.action, "failed")
        self.assertIsNone(result.summary)

    def test_non_zero_exit_is_failure(self):
        result = self._patched_run_turn(agent_mod.AGENT_CLAUDE, "non_zero_exit")
        self.assertEqual(result.action, "failed")
        self.assertEqual(result.exit_code, 7)
        self.assertIn("boom on stderr", result.stderr_tail)

    def test_watchdog_kills_silently_blocked_agent(self):
        """Regression — pre-fix the timeout was checked INSIDE the
        readline() loop, which only runs when readline() returns. A
        claude subprocess that opens an interactive prompt and waits
        for input forever (no stdout output) blocked the runner for
        24 hours past the 30-min timeout (real incident, 2026-05-12,
        critique 63651d8d). The watchdog Timer must fire even when
        stdout never produces a line."""
        # Snippet sleeps far longer than our test timeout, prints
        # nothing. A pre-fix runner would hang here forever; the
        # watchdog must terminate it within ~timeout_s.
        argv = [
            sys.executable, "-c",
            "import time; time.sleep(120)",
        ]
        t0 = time.time()
        with mock.patch.object(agent_mod, "_argv_for_agent", return_value=argv):
            result = agent_mod.run_agent_turn(
                agent_mod.AGENT_CLAUDE, "PROMPT",
                repo_root=Path.cwd(), timeout_s=2,
            )
        elapsed = time.time() - t0
        # Should finish within timeout_s + a small grace — definitely
        # not 120 s of sleep. Allow up to 10 s for CI noise + the
        # 5 s wait+kill+reap cycle in the finally block.
        self.assertLess(
            elapsed, 12,
            f"watchdog failed to fire — turn ran for {elapsed:.1f}s "
            "instead of being killed at 2s",
        )
        self.assertEqual(result.action, "failed")
        # Cause is logged into stderr_tail so the runner has something
        # actionable for the chat panel + does not silently retry.
        self.assertIn("[runner-watchdog]", result.stderr_tail,
                      "watchdog kill must annotate stderr_tail with cause")
        self.assertIn("killed after 2s", result.stderr_tail)


# ---------------------------------------------------------------------------
# Tests for runner.py — claim, gate failure loop, happy push
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t.io", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def _make_clean_repo(*, with_remote: bool = True) -> tuple[Path, Path | None]:
    """Build an ephemeral git repo + a bare 'remote' so push tests
    don't talk to the network."""
    work = Path(tempfile.mkdtemp(prefix="critique-runner-"))
    _git(work, "init", "-q", "-b", "main")
    (work / "mod.py").write_text("def f():\n    return 1\n")
    (work / "tests").mkdir()
    (work / "tests" / "test_mod.py").write_text(
        "import mod\ndef test_f(): assert mod.f() == 1\n"
    )
    (work / ".coveragerc").write_text("[run]\nsource = .\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")

    if not with_remote:
        return work, None

    remote = Path(tempfile.mkdtemp(prefix="critique-remote-"))
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    return work, remote


class ClaimCritiqueTests(unittest.TestCase):
    def setUp(self):
        self.client = _FakeFirestoreClient()

    def _seed_doc(self, status="queued"):
        # Patch firestore.SERVER_TIMESTAMP for the helpers that call it.
        fake_module = mock.MagicMock()
        fake_module.SERVER_TIMESTAMP = _SERVER_TIMESTAMP_SENTINEL
        fake_module.transactional = lambda fn: fn  # decorator no-op
        self._fs_module_patch = mock.patch.dict(
            "sys.modules",
            {"google.cloud": mock.MagicMock(firestore=fake_module)},
        )
        self._fs_module_patch.start()
        self.addCleanup(self._fs_module_patch.stop)

        self.client.collection("critiques").document("cid").set({
            "status": status, "job_id": "j1", "created_at": 1,
            "agent": "claude",
        })

    def test_claims_when_queued(self):
        self._seed_doc("queued")
        ok = runner_mod.claim_critique(self.client, "cid", hostname="my-laptop")
        self.assertTrue(ok)
        cur = self.client.collection("critiques").docs["cid"]
        self.assertEqual(cur["status"], "in_progress")
        self.assertEqual(cur["claimed_by"], "my-laptop")

    def test_does_not_claim_already_in_progress(self):
        self._seed_doc("in_progress")
        ok = runner_mod.claim_critique(self.client, "cid", hostname="my-laptop")
        self.assertFalse(ok)
        # Status untouched.
        self.assertEqual(self.client.collection("critiques").docs["cid"]["status"], "in_progress")

    def test_does_not_claim_missing_doc(self):
        # Don't seed at all.
        fake_module = mock.MagicMock()
        fake_module.SERVER_TIMESTAMP = _SERVER_TIMESTAMP_SENTINEL
        fake_module.transactional = lambda fn: fn
        with mock.patch.dict("sys.modules", {"google.cloud": mock.MagicMock(firestore=fake_module)}):
            ok = runner_mod.claim_critique(self.client, "missing", hostname="laptop")
        self.assertFalse(ok)


class IsolatePreExistingDirtTests(unittest.TestCase):
    """The runner stashes pre-existing dirty changes before claiming
    a critique so the agent's diff is isolated from whatever the
    user was hand-editing. The stash is restored in a try/finally so
    the user's work always comes back regardless of how the critique
    ends.
    """

    def test_clean_repo_returns_false(self):
        repo, _ = _make_clean_repo(with_remote=False)
        try:
            stashed = runner_mod._isolate_pre_existing_dirt(
                repo, stash_label="t/clean",
            )
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)
        self.assertFalse(stashed)

    def test_dirty_repo_stashed_then_restored(self):
        # End-to-end: dirty tree → isolate → tree is clean → restore →
        # tree dirt is back exactly as it was.
        repo, _ = _make_clean_repo(with_remote=False)
        try:
            (repo / "stray.py").write_text("oops\n")
            (repo / "mod.py").write_text("def f():\n    return 999\n")  # modify tracked
            before = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=str(repo), text=True,
            )
            self.assertTrue(before.strip(),
                            "test setup expected dirt before isolate")

            stashed = runner_mod._isolate_pre_existing_dirt(
                repo, stash_label="t/dirt-roundtrip",
            )
            self.assertTrue(stashed)

            mid = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=str(repo), text=True,
            )
            self.assertEqual(mid.strip(), "",
                             "tree must be clean after isolate")

            runner_mod._restore_pre_existing_dirt(
                repo, stash_label="t/dirt-roundtrip",
            )

            after = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=str(repo), text=True,
            )
            self.assertEqual(before.strip(), after.strip(),
                             "tree state must be byte-identical after restore")
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)

    def test_restore_handles_missing_stash_gracefully(self):
        """If something else popped the stash, the restore is a no-op,
        not a crash. The runner already isn't in a position to fix a
        crashed laptop env, so we log + move on."""
        repo, _ = _make_clean_repo(with_remote=False)
        try:
            # Don't stash anything — restore should just log "no stash".
            runner_mod._restore_pre_existing_dirt(
                repo, stash_label="t/nonexistent",
            )  # must not raise
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)

    def test_isolate_raises_diagnostic_when_git_status_fails(self):
        """`_isolate_pre_existing_dirt` must surface the diagnostic
        message from `_git_failure_diag` (not just bare stderr) so the
        chat panel error is actionable. Pre-fix, git status failing on
        an `.git/index.lock` race produced "git status failed: ." —
        the user couldn't tell what to do. The new error must include
        the returncode + cwd + index-lock hint."""
        not_a_repo = Path(tempfile.mkdtemp(prefix="not-a-repo-"))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                runner_mod._isolate_pre_existing_dirt(
                    not_a_repo, stash_label="t/no-repo",
                )
            err = str(ctx.exception)
            # Must include returncode + cwd at minimum.
            self.assertIn("rc=", err)
            self.assertIn(str(not_a_repo), err)
        finally:
            subprocess.run(["rm", "-rf", str(not_a_repo)], check=False)


class GitFailureDiagTests(unittest.TestCase):
    """`_git_failure_diag` composes a useful error message even when
    git's stderr is empty. Pre-fix the runner reported
    ``"git status failed: ."`` (an unactionable empty stderr collapsed
    against the f-string's trailing period) for `.git/index.lock`
    races against a concurrent commit. Established 2026-05-12 after
    the d7abfdd7 critique surfaced the empty-stderr case."""

    def _make_proc(
        self, *, returncode: int = 1, stdout: str = "", stderr: str = "",
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def test_includes_returncode_and_cwd(self):
        proc = self._make_proc(returncode=128, stderr="fatal: not a git repo")
        msg = runner_mod._git_failure_diag("git status", proc, Path("/tmp/x"))
        self.assertIn("rc=128", msg)
        self.assertIn("/tmp/x", msg)
        self.assertIn("fatal: not a git repo", msg)

    def test_empty_stderr_surfaces_index_lock_hint(self):
        # The actual class of bug from 2026-05-12 — git exits non-zero
        # with both streams empty. Pre-fix this produced "git status
        # failed: ." which told the user nothing.
        proc = self._make_proc(returncode=128, stdout="", stderr="")
        msg = runner_mod._git_failure_diag("git status", proc, Path("/tmp/x"))
        self.assertIn("rc=128", msg)
        self.assertIn(".git/index.lock", msg,
                      "empty-stderr case must hint at the most "
                      "common cause (concurrent git operation)")

    def test_stdout_surfaces_when_stderr_empty(self):
        proc = self._make_proc(returncode=1, stdout="some output", stderr="")
        msg = runner_mod._git_failure_diag("git status", proc, Path("/tmp/x"))
        self.assertIn("stdout: some output", msg)

    def test_does_not_double_print_when_both_present(self):
        proc = self._make_proc(returncode=1, stdout="out", stderr="err")
        msg = runner_mod._git_failure_diag("git status", proc, Path("/tmp/x"))
        self.assertIn("stderr: err", msg)
        self.assertNotIn("stdout: out", msg,
                         "stdout suppressed when stderr already speaks")


class ParseToolUseChipTests(unittest.TestCase):
    """`_parse_tool_use_chip` extracts an action chip from claude's
    streaming stdout so the chat panel reflects per-tool progress
    instead of sitting on a static "running claude…" for 5-15
    minutes. Established 2026-05-12 after the user pinged
    "running claude… just showing this?" while a real, productive
    turn was 4 minutes deep into pipeline/compose.py exploration."""

    def test_recognizes_read(self):
        chip = runner_mod._parse_tool_use_chip(
            "● Read(pipeline/compose.py)"
        )
        self.assertEqual(chip["action"], "file_read")
        self.assertEqual(chip["text"], "reading pipeline/compose.py")

    def test_recognizes_read_with_offset(self):
        chip = runner_mod._parse_tool_use_chip(
            "● Read(pipeline/compose.py:870)"
        )
        self.assertEqual(chip["action"], "file_read")
        self.assertIn("pipeline/compose.py", chip["text"])

    def test_recognizes_edit(self):
        chip = runner_mod._parse_tool_use_chip(
            "● Edit(pipeline/render/long_form.py)"
        )
        self.assertEqual(chip["action"], "file_edited")
        self.assertEqual(chip["text"], "editing pipeline/render/long_form.py")

    def test_recognizes_write(self):
        chip = runner_mod._parse_tool_use_chip(
            "● Write(tests/test_foo.py)"
        )
        self.assertEqual(chip["action"], "file_edited")
        self.assertIn("writing tests/test_foo.py", chip["text"])

    def test_recognizes_bash(self):
        chip = runner_mod._parse_tool_use_chip(
            "● Bash(pytest -x -q)"
        )
        self.assertEqual(chip["action"], "agent_thinking")
        self.assertEqual(chip["text"], "running pytest -x -q")

    def test_recognizes_grep_glob(self):
        for line, expect in [
            ("● Grep(\"loudnorm\" in pipeline/)", "searching"),
            ("● Glob(**/*.py)", "searching"),
        ]:
            chip = runner_mod._parse_tool_use_chip(line)
            self.assertEqual(chip["action"], "file_read")
            self.assertTrue(chip["text"].startswith(expect))

    def test_ignores_plain_thinking_text(self):
        # Pre-fix, EVERY line was treated as a chip candidate; that's
        # what created the Firestore-write spam. Whitelist only.
        for line in [
            "Now I need to look at the long_form module…",
            "Looking for the loudnorm filter chain.",
            "● TodoWrite(...)",  # not in whitelist
            "",
            "  ",
        ]:
            self.assertIsNone(
                runner_mod._parse_tool_use_chip(line),
                f"non-tool-use line incorrectly produced a chip: {line!r}",
            )

    def test_truncates_overly_long_args(self):
        long_arg = "echo " + ("x" * 500)
        chip = runner_mod._parse_tool_use_chip(f"● Bash({long_arg})")
        self.assertIsNotNone(chip)
        self.assertLessEqual(len(chip["text"]), 240,
                             "chip text must fit Firestore doc cap headroom")


class ProcessUserMessageTests(unittest.TestCase):
    """End-to-end happy + sad path with a fake agent."""

    def _patch_for_runner(self):
        fake_module = mock.MagicMock()
        fake_module.SERVER_TIMESTAMP = _SERVER_TIMESTAMP_SENTINEL
        fake_module.transactional = lambda fn: fn
        return mock.patch.dict("sys.modules", {"google.cloud": mock.MagicMock(firestore=fake_module)})

    def test_happy_path_commits_and_pushes(self):
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo,
            push_remote="origin",
            push_branch="main",
            agent_timeout_s=30,
            gate_timeout_s=30,
            poll_interval_s=0.05,
        )
        try:
            with self._patch_for_runner():
                # Fake agent: edit mod.py + add a test, then emit a "done" summary.
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    (repo / "mod.py").write_text(
                        "def f():\n    return 1\n\n"
                        "def g():\n    return 2\n"
                    )
                    (repo / "tests" / "test_mod.py").write_text(
                        "import mod\n"
                        "def test_f(): assert mod.f() == 1\n"
                        "def test_g(): assert mod.g() == 2\n"
                    )
                    return agent_mod.AgentTurnResult(
                        action="done",
                        text="agent did the fix",
                        summary={
                            "type": "agent_summary",
                            "action": "done",
                            "files_changed": ["mod.py"],
                            "tests_added": ["tests/test_mod.py"],
                            "rationale": "added g() with test",
                            "follow_up_questions": [],
                        },
                        stdout_tail="",
                        stderr_tail="",
                        exit_code=0,
                        duration_s=0.1,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn", side_effect=fake_run_agent_turn):
                    next_status, extra, fc = runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "please add g()",
                        cfg,
                    )

            self.assertEqual(next_status, "done")
            self.assertIn("commit_sha", extra)
            self.assertEqual(fc, 0)
            # The new commit landed on the bare remote too.
            log = subprocess.check_output(
                ["git", "-c", "safe.bareRepository=all",
                 "log", "--oneline", "main"],
                cwd=str(remote), text=True,
            )
            self.assertEqual(len(log.strip().splitlines()), 2,
                             f"expected 2 commits on remote, got:\n{log}")

            # Chat got the chips.
            messages = msg_mod.fetch_messages(client, "cid")
            actions = [m.action for m in messages if m.action]
            self.assertIn(msg_mod.ACTION_AGENT_THINKING, actions)
            self.assertIn(msg_mod.ACTION_GATE_RUNNING, actions)
            self.assertIn(msg_mod.ACTION_GATE_PASSED, actions)
            self.assertIn(msg_mod.ACTION_COMMIT_CREATED, actions)
            self.assertIn(msg_mod.ACTION_PUSHED, actions)
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)

    def test_gate_failure_loops_with_followup(self):
        """Agent claims `done` but adds NO test → tests_added gate
        fails → runner posts a synthetic USER follow-up so the
        agent's next turn knows what to fix."""
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo,
            agent_timeout_s=30,
            gate_timeout_s=30,
            poll_interval_s=0.05,
            max_failed_turns=5,
        )
        try:
            with self._patch_for_runner():
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    # Edit source, NO test file added.
                    (repo / "mod.py").write_text("def f():\n    return 999\n")
                    return agent_mod.AgentTurnResult(
                        action="done", text="claimed done",
                        summary={
                            "type": "agent_summary",
                            "action": "done",
                            "files_changed": ["mod.py"],
                            "tests_added": [],
                            "rationale": "tweaked f",
                            "follow_up_questions": [],
                        },
                        stdout_tail="", stderr_tail="", exit_code=0, duration_s=0.1,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn", side_effect=fake_run_agent_turn):
                    next_status, extra, fc = runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "fix f",
                        cfg,
                    )
            self.assertEqual(next_status, "in_progress")
            self.assertEqual(fc, 1)
            # No commit was created.
            log = subprocess.check_output(
                ["git", "log", "--oneline", "main"],
                cwd=str(repo), text=True,
            )
            self.assertEqual(len(log.strip().splitlines()), 1,
                             "no commit should be created on gate failure")
            # A synthetic USER follow-up message was queued for the next turn.
            messages = msg_mod.fetch_messages(client, "cid")
            user_msgs = [m for m in messages if m.role == msg_mod.ROLE_USER]
            self.assertEqual(len(user_msgs), 1)
            self.assertIn("tests_added", user_msgs[0].text)
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)

    def test_max_failed_turns_terminates_and_resets_repo(self):
        """After max_failed_turns gate failures the runner gives up,
        marks the critique failed, and resets the working tree so the
        user's repo isn't littered with the agent's half-baked diff."""
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo,
            agent_timeout_s=30,
            gate_timeout_s=30,
            poll_interval_s=0.05,
            max_failed_turns=1,  # tight cap
        )
        try:
            with self._patch_for_runner():
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    (repo / "mod.py").write_text("def f():\n    return 999\n")
                    return agent_mod.AgentTurnResult(
                        action="done", text="…",
                        summary={"type": "agent_summary", "action": "done",
                                 "files_changed": [], "tests_added": [], "rationale": "x", "follow_up_questions": []},
                        stdout_tail="", stderr_tail="", exit_code=0, duration_s=0.1,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn", side_effect=fake_run_agent_turn):
                    next_status, extra, fc = runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "fix",
                        cfg,
                    )
            self.assertEqual(next_status, "failed")
            self.assertIn("max_failed_turns", extra["error"])
            # Repo is clean again.
            status = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(repo), text=True,
            )
            self.assertEqual(status.strip(), "",
                             f"working tree should be clean after reset; got:\n{status}")
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)

    def test_agent_failed_short_circuits_to_failed_status(self):
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo,
            agent_timeout_s=30,
            gate_timeout_s=30,
            poll_interval_s=0.05,
        )
        try:
            with self._patch_for_runner():
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    return agent_mod.AgentTurnResult(
                        action="failed", text="",
                        summary=None,
                        stdout_tail="", stderr_tail="OOM", exit_code=137, duration_s=0.1,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn", side_effect=fake_run_agent_turn):
                    next_status, extra, fc = runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "x",
                        cfg,
                    )
            self.assertEqual(next_status, "failed")
            self.assertIn("agent exit=137", extra["error"])
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)

    def test_streams_tool_use_lines_to_chat_as_chips(self):
        """Regression — pre-fix the runner's `_on_line` callback was a
        no-op (`return None`). The chat panel sat on a single
        "running claude…" message for 5-15 minutes regardless of how
        much progress claude made. Established 2026-05-12 after the
        user pinged "running claude… just showing this?" while
        claude was 4 minutes deep into productive exploration of
        pipeline/compose.py.

        The fix wires `_on_line` to write throttled action chips to
        Firestore for every recognized tool-use line. This test
        proves chips ACTUALLY land in the message subcollection."""
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo,
            agent_timeout_s=30,
            gate_timeout_s=30,
            poll_interval_s=0.05,
        )
        try:
            with self._patch_for_runner():
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    # Drive the streaming callback with realistic claude
                    # tool-use output. We need 2 distinct chip TEXTS
                    # spaced > _MIN_EMIT_INTERVAL_S apart so both pass
                    # the throttle.
                    cb = kwargs.get("on_stdout_line")
                    self.assertIsNotNone(
                        cb,
                        "process_user_message must pass on_stdout_line "
                        "to run_agent_turn",
                    )
                    cb("● Read(pipeline/compose.py)")
                    # Force the throttle clock past the floor.
                    time.sleep(runner_mod._MIN_STREAM_EMIT_INTERVAL_S + 0.1)
                    cb("● Edit(pipeline/render/long_form.py)")
                    # Plain prose must NOT produce a chip.
                    cb("Now I need to verify the loudnorm filter chain…")
                    return agent_mod.AgentTurnResult(
                        action="need_more_info",
                        text="needs clarification",
                        summary={
                            "type": "agent_summary",
                            "action": "need_more_info",
                            "files_changed": [],
                            "tests_added": [],
                            "rationale": "test",
                            "follow_up_questions": ["which channel?"],
                        },
                        stdout_tail="", stderr_tail="",
                        exit_code=0, duration_s=0.1,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn",
                                       side_effect=fake_run_agent_turn):
                    runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "x", cfg,
                    )

            messages = msg_mod.fetch_messages(client, "cid")
            actions = [m.action for m in messages]
            chip_texts = [m.text for m in messages if m.action]
            # Original "running claude…" + the two streamed chips.
            self.assertGreaterEqual(
                len([a for a in actions
                     if a in (msg_mod.ACTION_FILE_READ,
                              msg_mod.ACTION_FILE_EDITED)]),
                2,
                f"streaming must produce ≥2 file_read/edited chips; got: {actions}",
            )
            self.assertTrue(
                any("reading pipeline/compose.py" in t for t in chip_texts),
                f"expected 'reading pipeline/compose.py' chip; got: {chip_texts}",
            )
            self.assertTrue(
                any("editing pipeline/render/long_form.py" in t for t in chip_texts),
                f"expected 'editing pipeline/render/long_form.py' chip; got: {chip_texts}",
            )
            self.assertFalse(
                any("Now I need to verify" in t for t in chip_texts),
                "plain prose must NOT produce a chip",
            )
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)

    def test_streaming_callback_swallows_firestore_errors(self):
        """If Firestore writes start failing mid-turn, the streaming
        callback must NOT propagate — that would crash the agent
        process and lose all context. The full stdout is still
        captured in stdout_tail for debug. Established 2026-05-12
        when we wired streaming."""
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo, agent_timeout_s=30, gate_timeout_s=30,
            poll_interval_s=0.05,
        )
        # Make add_action_message blow up only on chip writes (text
        # contains "reading"/"editing"/"running"), so the initial
        # "running claude…" prelude still goes through.
        original = msg_mod.add_action_message
        def selective_break(client_, cid, action, *, text="", **kwargs):
            if any(t in text for t in ("reading ", "editing ", "writing ", "running ")):
                if "claude" in text:
                    return original(client_, cid, action, text=text, **kwargs)
                raise RuntimeError("firestore down")
            return original(client_, cid, action, text=text, **kwargs)

        try:
            with self._patch_for_runner():
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    cb = kwargs.get("on_stdout_line")
                    # Multiple lines — none should propagate.
                    cb("● Read(pipeline/compose.py)")
                    time.sleep(runner_mod._MIN_STREAM_EMIT_INTERVAL_S + 0.1)
                    cb("● Edit(pipeline/render/long_form.py)")
                    return agent_mod.AgentTurnResult(
                        action="need_more_info", text="x", summary=None,
                        stdout_tail="", stderr_tail="",
                        exit_code=0, duration_s=0.01,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn",
                                       side_effect=fake_run_agent_turn), \
                     mock.patch.object(msg_mod, "add_action_message",
                                       side_effect=selective_break):
                    # Must not raise even though every chip-write call fails.
                    runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "x", cfg,
                    )
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)


    def test_streaming_throttle_drops_rapid_duplicates(self):
        """Two chip-eligible lines emitted within _MIN_STREAM_EMIT_INTERVAL_S
        must produce only ONE chip — Firestore writes are expensive.
        Two identical chip texts (different lines, same parsed
        result) emitted past the throttle window must still dedupe."""
        repo, remote = _make_clean_repo(with_remote=True)
        client = _FakeFirestoreClient()
        cfg = runner_mod.RunnerConfig(
            repo_root=repo, agent_timeout_s=30, gate_timeout_s=30,
            poll_interval_s=0.05,
        )
        try:
            with self._patch_for_runner():
                def fake_run_agent_turn(agent_kind, prompt, **kwargs):
                    cb = kwargs.get("on_stdout_line")
                    # Two RAPID lines (no sleep) → second hits throttle.
                    cb("● Read(pipeline/a.py)")
                    cb("● Read(pipeline/b.py)")  # throttled out
                    # Sleep past throttle, then send the SAME text →
                    # second one hits the dedupe early-return.
                    time.sleep(runner_mod._MIN_STREAM_EMIT_INTERVAL_S + 0.1)
                    cb("● Read(pipeline/a.py)")  # deduped (same text as 1st)
                    return agent_mod.AgentTurnResult(
                        action="need_more_info", text="x", summary=None,
                        stdout_tail="", stderr_tail="",
                        exit_code=0, duration_s=0.01,
                    )
                with mock.patch.object(agent_mod, "run_agent_turn",
                                       side_effect=fake_run_agent_turn):
                    runner_mod.process_user_message(
                        client, "cid",
                        {"agent": "claude", "channel": "test"},
                        "x", cfg,
                    )

            messages = msg_mod.fetch_messages(client, "cid")
            read_chips = [m for m in messages
                          if m.action == msg_mod.ACTION_FILE_READ]
            self.assertEqual(
                len(read_chips), 1,
                f"throttle+dedupe must collapse 3 calls to 1 chip; "
                f"got {len(read_chips)}: {[m.text for m in read_chips]}",
            )
        finally:
            subprocess.run(["rm", "-rf", str(repo), str(remote) if remote else ""],
                           check=False)


class CommitMessageTests(unittest.TestCase):
    def test_includes_critique_id_and_rationale(self):
        msg = runner_mod._build_commit_message("cid-123", {
            "rationale": "fixed the closer prosody",
            "files_changed": ["pipeline/tts/cloudrun.py"],
            "tests_added": ["tests/test_tts_closer.py"],
        })
        self.assertIn("critique-fix:", msg)
        self.assertIn("fixed the closer prosody", msg)
        self.assertIn("critiques/cid-123", msg)
        self.assertIn("pipeline/tts/cloudrun.py", msg)
        self.assertIn("tests/test_tts_closer.py", msg)
        self.assertIn("Co-authored-by: Copilot", msg)

    def test_handles_missing_summary(self):
        # Defensive — agent might emit a malformed summary.
        msg = runner_mod._build_commit_message("cid-456", None)
        self.assertIn("critique-fix:", msg)
        self.assertIn("(no rationale provided)", msg)
        self.assertIn("critiques/cid-456", msg)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
