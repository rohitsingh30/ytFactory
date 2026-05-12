"""Critique-runner daemon: laptop-side worker that consumes critique
docs from Firestore, drives the agent loop, runs hard gates, and
pushes successful fixes direct to main.

Lifecycle of one critique doc:

  ┌──────────────────────────────────────────────────────────────┐
  │ 1. Daemon's on_snapshot fires for queued critiques.          │
  │ 2. Run claim_transaction: queued → in_progress + claimed_by. │
  │ 3. Materialise the job's mp4 + script locally.               │
  │ 4. Subscribe to messages subcollection.                      │
  │ 5. For every new role=user message:                          │
  │      a. Build prompt from history + new message.             │
  │      b. Spawn agent (run_agent_turn).                        │
  │      c. Stream stdout chips into chat.                       │
  │      d. On agent action="done": stage + run gates.           │
  │      e. On all gates green: commit + push direct to main.    │
  │      f. On any gate red: post failure as USER msg → loop.    │
  │ 6. After max_failed_turns or status flip: release the doc.   │
  └──────────────────────────────────────────────────────────────┘

Single-laptop design (Phase 1). Multi-runner safety would need:
  - Watchdog reaper that resets in_progress + claimed_at older than
    30 min back to queued.
  - claim_transaction is already correct under contention (Firestore
    transactions are linearisable on the doc).

Public entry point is :func:`run_forever`; all helpers underneath it
are exposed so tests can exercise each layer in isolation.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pipeline.critique import agent as agent_mod
from pipeline.critique import gates as gates_mod
from pipeline.critique import messages as msg_mod

logger = logging.getLogger(__name__)


# Hard caps so a runaway critique can't park the runner forever.
DEFAULT_MAX_FAILED_TURNS = 3
DEFAULT_AGENT_TURN_TIMEOUT_S = 1800  # passed through to agent_mod
DEFAULT_GATE_TIMEOUT_S = 600


# Status values mirror docs/critique_chat.md.
STATUS_QUEUED = "queued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class RunnerConfig:
    """Per-runner settings. Most callers will accept the defaults; the
    test suite injects a smaller ``poll_interval_s`` and a fake git/agent
    so a critique can be driven end-to-end in milliseconds.
    """
    repo_root: Path
    push_remote: str = "origin"
    push_branch: str = "main"
    max_failed_turns: int = DEFAULT_MAX_FAILED_TURNS
    agent_timeout_s: int = DEFAULT_AGENT_TURN_TIMEOUT_S
    gate_timeout_s: int = DEFAULT_GATE_TIMEOUT_S
    poll_interval_s: float = 2.0
    hostname: str = socket.gethostname()


# ---------------------------------------------------------------------------
# Firestore claim
# ---------------------------------------------------------------------------


def claim_critique(client, critique_id: str, *, hostname: str) -> bool:
    """Atomically transition ``status`` from ``queued`` → ``in_progress``
    and stamp ``claimed_by`` + ``claimed_at`` so no other runner picks
    it up.

    Returns True if THIS process won the claim, False if the doc was
    no longer queued by the time we ran the txn (another runner won,
    user cancelled, doc deleted, …).

    Implementation note: Firestore transactions are linearisable on
    the doc, so the read-then-conditional-write is race-free.
    """
    from google.cloud import firestore  # noqa: PLC0415
    transaction = client.transaction()

    @firestore.transactional
    def _txn(txn):
        ref = client.collection("critiques").document(critique_id)
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return False
        data = snap.to_dict() or {}
        if data.get("status") != STATUS_QUEUED:
            return False
        txn.update(ref, {
            "status": STATUS_IN_PROGRESS,
            "claimed_by": hostname,
            "claimed_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        return True

    return bool(_txn(transaction))


def _set_critique_status(
    client,
    critique_id: str,
    status: str,
    *,
    extra: dict | None = None,
) -> None:
    """Patch the parent doc's status + optional fields (commit_sha,
    summary, gate_results, error). Uses merge semantics so we never
    accidentally clobber fields the browser is reading."""
    from google.cloud import firestore  # noqa: PLC0415
    payload = {"status": status, "updated_at": firestore.SERVER_TIMESTAMP}
    if extra:
        payload.update(extra)
    client.collection("critiques").document(critique_id).set(payload, merge=True)


# ---------------------------------------------------------------------------
# Per-message turn driver
# ---------------------------------------------------------------------------


# Tool-use lines that claude (`claude -p`) prints to stdout while it
# runs. Format observed 2026-05-12 against claude CLI v2.1.139:
#
#     ● Read(pipeline/compose.py)
#     ● Read(pipeline/compose.py:870)        ← optional :line
#     ● Edit(pipeline/render/long_form.py)
#     ● Write(tests/test_foo.py)
#     ● Bash(pytest -x -q)
#     ● Glob(**/*.py)
#     ● Grep("loudnorm" in pipeline/)
#
# We only care about a small whitelist that maps cleanly to the chat
# panel's existing action-chip palette (see
# web-next/components/app/critique-chat-panel.tsx). Anything else
# (TodoWrite, internal SDK chatter, plain-prose thinking) gets
# dropped from chat — full stdout is still preserved in
# AgentTurnResult.stdout_tail for debugging.
_TOOL_USE_RE = re.compile(
    r"^[\s●\*]+(?P<tool>Read|Edit|Write|Bash|Glob|Grep|MultiEdit)\((?P<arg>[^)]+)\)"
)

# Throttle floor for the streaming chip writer. Firestore charges per
# write + has hard QPS limits per doc; 4 s gives the user feedback
# every few seconds without saturating writes during a hot tool-use
# burst (claude can emit 5-10 tool-use lines per second when reading
# many small files). Module-level so tests can pin it.
_MIN_STREAM_EMIT_INTERVAL_S = 4.0

# Map claude tool name → chat-panel action chip + human-friendly verb.
_TOOL_TO_CHIP: dict[str, tuple[str, str]] = {
    "Read":      (msg_mod.ACTION_FILE_READ,    "reading"),
    "Glob":      (msg_mod.ACTION_FILE_READ,    "searching"),
    "Grep":      (msg_mod.ACTION_FILE_READ,    "searching"),
    "Edit":      (msg_mod.ACTION_FILE_EDITED,  "editing"),
    "MultiEdit": (msg_mod.ACTION_FILE_EDITED,  "editing"),
    "Write":     (msg_mod.ACTION_FILE_EDITED,  "writing"),
    "Bash":      (msg_mod.ACTION_AGENT_THINKING, "running"),
}


def _parse_tool_use_chip(line: str) -> dict | None:
    """Return ``{action, text}`` for a recognised tool-use line, else None.

    The chat panel renders these as labelled chips so the user sees
    "reading pipeline/compose.py" / "editing pipeline/llm/cli.py" /
    "running pytest -x -q" while claude works, instead of the
    pre-fix "running claude…" sitting static for 5-15 minutes.

    Established 2026-05-12 — see process_user_message::_on_line.
    """
    m = _TOOL_USE_RE.match(line)
    if not m:
        return None
    tool = m.group("tool")
    arg = m.group("arg").strip()
    chip = _TOOL_TO_CHIP.get(tool)
    if chip is None:
        return None  # coverage: defence-in-depth — regex limits tools to whitelist keys
    action, verb = chip
    # Trim long Bash commands so the chat doesn't overflow Firestore's
    # 1 MiB doc cap (already throttled, but defence in depth).
    text = f"{verb} {arg}"[:240]
    return {"action": action, "text": text}


def _git_failure_diag(
    cmd_label: str, proc: subprocess.CompletedProcess[str], cwd: Path,
) -> str:
    """Compose a diagnostic message for a failed git invocation.

    Pre-fix the runner reported `f"git status failed: {proc.stderr.strip()}"`,
    which collapses to ``"git status failed: "`` (or ``"git status failed: ."``
    when the f-string's trailing period sticks) when git exits non-zero
    with empty stderr — a real class of failures including
    ``.git/index.lock`` races against a concurrent ``git commit`` from
    another process. The result is a chat message the user CAN'T act
    on. Bundle returncode + stdout + stderr + cwd so the next failure
    has at least one usable signal.

    Established 2026-05-12 after the d7abfdd7 critique surfaced the
    empty-stderr case to a confused user — see
    ``docs/critique_runner_ops.md`` § "Diagnosing stash_failed".
    """
    parts = [f"{cmd_label} failed (rc={proc.returncode})"]
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    if stderr:
        parts.append(f"stderr: {stderr[:400]}")
    if stdout and not stderr:
        # Mention stdout only when stderr is empty so we don't
        # double-print on the common case.
        parts.append(f"stdout: {stdout[:400]}")
    if not stderr and not stdout:
        # Most likely cause: git's index.lock race against a
        # concurrent git operation. Surface the hypothesis so the
        # user knows what to check.
        parts.append(
            "no git output (likely .git/index.lock contention with "
            "another git process — try again in a moment)"
        )
    parts.append(f"cwd: {cwd}")
    return "; ".join(parts)


def _isolate_pre_existing_dirt(repo_root: Path, *, stash_label: str) -> bool:
    """Stash any pre-existing dirty changes so the agent's diff is
    isolated from whatever the user was hand-editing. Returns True if
    a stash was created (caller must call :func:`_restore_pre_existing_dirt`
    in a try/finally), False if the tree was already clean.

    Pre-2026-05-11-evening the runner had `_ensure_clean_repo` which
    *refused to start* on a dirty tree. That was too strict — the
    repo regularly carries hundreds of small uncommitted edits across
    `.claude/skills/`, `docs/`, channel learnings, etc. The agent's
    fix only needs to be isolated FOR THE DURATION of one critique;
    we can restore the user's dirt the moment we're done.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(_git_failure_diag("git status", proc, repo_root))
    if not proc.stdout.strip():
        return False  # tree was already clean

    # --include-untracked stashes new files too (e.g. firebase-debug.log,
    # local docs the user is drafting). Without it, untracked files
    # would survive the stash and pollute the agent's diff.
    stash = subprocess.run(
        ["git", "stash", "push", "--include-untracked", "-m", stash_label],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if stash.returncode != 0:
        raise RuntimeError(
            f"{_git_failure_diag('git stash push', stash, repo_root)}"
            "; refusing to start"
        )
    logger.info("[runner] stashed pre-existing dirt as %r", stash_label)
    return True


def _restore_pre_existing_dirt(repo_root: Path, *, stash_label: str) -> None:
    """Counterpart to :func:`_isolate_pre_existing_dirt`. Best-effort —
    if the pop conflicts (because the agent's commit touched a file
    that was also dirty in the stash), we leave the stash on the
    stack and log loudly so the user can recover manually with
    ``git stash list`` + ``git stash pop``.
    """
    # First find the stash entry by label — `git stash pop` with no
    # ref would pop the TOP of the stack, but if the user did their
    # own stash since claim, popping the top would restore THEIR
    # work, not ours. Look up the right entry instead.
    list_proc = subprocess.run(
        ["git", "stash", "list"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    target = None
    for line in (list_proc.stdout or "").splitlines():
        if stash_label in line:
            target = line.split(":", 1)[0]  # e.g. "stash@{0}"
            break
    if target is None:
        logger.warning(
            "[runner] no stash entry matching %r; nothing to restore", stash_label
        )
        return
    pop = subprocess.run(
        ["git", "stash", "pop", target],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if pop.returncode != 0:
        logger.error(
            "[runner] git stash pop %s FAILED — pre-existing dirt left on the stack. "
            "Recover manually: `git stash list` + `git stash pop`. stderr: %s",
            target, pop.stderr.strip(),
        )
        return
    logger.info("[runner] restored pre-existing dirt from %s", target)


def _stage_and_commit(repo_root: Path, message: str) -> str:
    """Stage everything + commit. Returns the new HEAD SHA.

    Called only AFTER all gates pass — never trust the agent's
    self-reported success.
    """
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    # ``-c commit.gpgsign=false`` so a missing GPG key on the laptop
    # doesn't block the commit. The user can opt back in via env if
    # they want signing.
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git commit failed: {proc.stderr.strip()}")
    sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return sha_proc.stdout.strip()


def _push(repo_root: Path, *, remote: str, branch: str) -> None:
    """Push the just-committed change to ``<remote>/<branch>``."""
    subprocess.run(
        ["git", "push", remote, branch],
        cwd=str(repo_root),
        check=True,
    )


def _build_commit_message(critique_id: str, summary: dict | None) -> str:
    """Render a commit message that embeds the critique id + agent's
    rationale so future archaeologists can trace any line of code
    back to the exact critique that produced it."""
    rationale = (summary or {}).get("rationale") or "(no rationale provided)"
    files = (summary or {}).get("files_changed") or []
    tests = (summary or {}).get("tests_added") or []

    lines = [
        f"critique-fix: {rationale[:72]}",
        "",
        f"Critique: critiques/{critique_id}",
        f"Files changed: {', '.join(files) if files else '(via git diff)'}",
        f"Tests added: {', '.join(tests) if tests else '(via git diff)'}",
        "",
        "Pushed direct-to-main by the laptop critique runner after",
        "gates green: diff_exists, tests_added, pytest_full, diff_coverage.",
        "",
        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
    ]
    return "\n".join(lines)


def _emit_gate_chip(
    client,
    critique_id: str,
    result: gates_mod.GateResult,
) -> None:
    """Surface a gate's outcome as a chat chip."""
    action = (
        msg_mod.ACTION_GATE_PASSED
        if result.passed
        else msg_mod.ACTION_GATE_FAILED
    )
    msg_mod.add_action_message(
        client, critique_id,
        action=action,
        text=f"{result.name}: {result.msg}",
        action_data={
            "name": result.name,
            "passed": result.passed,
            "msg": result.msg,
            "duration_s": result.duration_s,
            "detail": result.detail,
        },
    )


def _gate_failure_followup(report: gates_mod.GateRunReport) -> str:
    """Render a synthetic USER message that re-injects the gate failure
    into the chat so the agent's NEXT turn sees a concrete to-do
    list. Without this, the agent has no idea why the runner
    rejected its previous "done" claim.
    """
    failure = report.first_failure
    assert failure is not None
    body = [
        f"Hard gate `{failure.name}` failed: {failure.msg}",
        "",
        failure.detail or "",
        "",
        "Please address the failure above and run the gate locally before "
        "claiming `done` again. If the bug is unfixable from your side, "
        "respond with `action=need_more_info` and explain what you need.",
    ]
    return "\n".join(body)


def process_user_message(
    client,
    critique_id: str,
    job_context: dict,
    user_text: str,
    config: RunnerConfig,
    *,
    failed_turns_so_far: int = 0,
) -> tuple[str, dict | None, int]:
    """Drive ONE round of: agent turn → gates → commit → push.

    Returns ``(next_status, extra_fields, new_failed_count)``:
      - ``next_status`` ∈ ("in_progress", "done", "failed")
      - ``extra_fields`` carries commit_sha + gate_results on success,
        ``error`` on failure.
      - ``new_failed_count`` increments only on gate failure where
        the agent claimed "done" but failed verification.
    """
    history = msg_mod.fetch_messages(client, critique_id)
    conversation = msg_mod.messages_to_prompt_history(history)
    prompt = agent_mod.build_prompt(
        repo_root=config.repo_root,
        job_context=job_context,
        conversation_history=conversation,
        latest_user_message=user_text,
    )

    msg_mod.add_action_message(
        client, critique_id,
        action=msg_mod.ACTION_AGENT_THINKING,
        text=f"running {job_context.get('agent', 'agent')}…",
    )

    # Streaming callback — surface claude's per-line stdout to the
    # chat panel as throttled action chips so the user sees progress
    # within seconds instead of a single "running claude…" sitting
    # static for 5-15 minutes.
    #
    # Pre-fix this was a no-op (`return None`). Real claude turns
    # interleave 30-100 tool-use lines (Read / Edit / Bash / Write)
    # before producing the final summary; the user has zero feedback
    # in the chat panel during that window. Established 2026-05-12
    # after the user pinged "running claude… just showing this?"
    # while a real, productive turn was 4 minutes deep into
    # exploring pipeline/compose.py.
    #
    # Throttling — Firestore charges per write + has hard QPS limits
    # per doc. We:
    #   1. Parse only lines that match a tool-use signal (claude
    #      prefixes them with the bullet ``●``); plain "thinking"
    #      text is dropped from the chat (still captured in
    #      stdout_tail for debug).
    #   2. Coalesce duplicate signals (5 consecutive Read lines on
    #      the same file → one chip).
    #   3. Hard floor of 4 s between writes to the same critique
    #      doc, regardless of signal count.
    _stream_state = {
        "last_emit_ts": 0.0,
        "last_emit_text": "",
    }

    def _on_line(line: str) -> None:
        try:
            chip = _parse_tool_use_chip(line)
            if chip is None:
                return
            now = time.time()
            if (now - _stream_state["last_emit_ts"]) < _MIN_STREAM_EMIT_INTERVAL_S:
                return
            if chip["text"] == _stream_state["last_emit_text"]:
                return
            _stream_state["last_emit_ts"] = now
            _stream_state["last_emit_text"] = chip["text"]
            msg_mod.add_action_message(
                client, critique_id,
                action=chip["action"],
                text=chip["text"],
            )
        except Exception:  # noqa: BLE001
            # NEVER let a chat-streaming error break the agent turn.
            # The full stdout is still captured in stdout_tail.
            logger.warning("on_stdout_line emit failed", exc_info=True)

    turn = agent_mod.run_agent_turn(
        job_context.get("agent", agent_mod.AGENT_CLAUDE),
        prompt,
        repo_root=config.repo_root,
        timeout_s=config.agent_timeout_s,
        on_stdout_line=_on_line,
    )

    # Always post the agent's final text (or a failure summary) into chat
    # so the user sees what happened.
    if turn.action == "failed":
        msg_mod.add_agent_message(
            client, critique_id,
            text=(
                "Agent turn failed."
                + (f" exit={turn.exit_code}." if turn.exit_code else "")
                + (f"\n\nstderr tail:\n{turn.stderr_tail}" if turn.stderr_tail else "")
            ),
            action=msg_mod.ACTION_AGENT_FINISHED,
            action_data={"exit_code": turn.exit_code, "duration_s": turn.duration_s},
        )
        return STATUS_FAILED, {"error": f"agent exit={turn.exit_code}"}, failed_turns_so_far

    if turn.action == "need_more_info":
        msg_mod.add_agent_message(
            client, critique_id,
            text=turn.text,
            action=msg_mod.ACTION_AGENT_FINISHED,
            action_data={
                "summary": turn.summary,
                "duration_s": turn.duration_s,
            },
        )
        return STATUS_IN_PROGRESS, None, failed_turns_so_far

    # turn.action == "done" → run gates.
    msg_mod.add_action_message(
        client, critique_id,
        action=msg_mod.ACTION_GATE_RUNNING,
        text="running hard gates…",
    )

    # Stage everything the agent did so the gates see new files too.
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(config.repo_root),
        check=True,
    )

    report = gates_mod.run_all_gates(
        config.repo_root,
        pytest_timeout_s=config.gate_timeout_s,
        staged=True,
    )
    for r in report.results:
        _emit_gate_chip(client, critique_id, r)

    if not report.all_passed:
        # Re-inject the failure as a synthetic USER message so the
        # next agent turn has the failing-gate detail to act on.
        followup = _gate_failure_followup(report)
        msg_mod.add_user_message(client, critique_id, followup)

        new_failed = failed_turns_so_far + 1
        if new_failed >= config.max_failed_turns:
            msg_mod.add_agent_message(
                client, critique_id,
                text=(
                    f"Reached {new_failed} consecutive gate failure(s); "
                    "giving up. The repo working tree was reset; no commit "
                    "was created. Re-open the chat after manual triage."
                ),
                action=msg_mod.ACTION_AGENT_FINISHED,
            )
            # Reset working tree so the user's repo isn't littered.
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=str(config.repo_root),
                check=False,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=str(config.repo_root),
                check=False,
            )
            return STATUS_FAILED, {
                "error": f"max_failed_turns={config.max_failed_turns} reached",
                "gate_results": [
                    {"name": r.name, "passed": r.passed, "msg": r.msg}
                    for r in report.results
                ],
            }, new_failed
        return STATUS_IN_PROGRESS, None, new_failed

    # All gates green → commit + push.
    commit_msg = _build_commit_message(critique_id, turn.summary)
    sha = _stage_and_commit(config.repo_root, commit_msg)
    msg_mod.add_action_message(
        client, critique_id,
        action=msg_mod.ACTION_COMMIT_CREATED,
        text=f"committed {sha[:8]} on {config.push_branch}",
        action_data={"sha": sha},
    )
    msg_mod.add_action_message(
        client, critique_id,
        action=msg_mod.ACTION_PUSH_PENDING,
        text=f"pushing to {config.push_remote}/{config.push_branch}…",
    )
    _push(config.repo_root, remote=config.push_remote, branch=config.push_branch)
    msg_mod.add_action_message(
        client, critique_id,
        action=msg_mod.ACTION_PUSHED,
        text=f"pushed {sha[:8]} → {config.push_remote}/{config.push_branch}",
        action_data={"sha": sha, "remote": config.push_remote, "branch": config.push_branch},
    )

    return STATUS_DONE, {
        "commit_sha": sha,
        "summary": (turn.summary or {}).get("rationale"),
        "gate_results": [
            {"name": r.name, "passed": r.passed, "msg": r.msg, "duration_s": r.duration_s}
            for r in report.results
        ],
    }, failed_turns_so_far


# ---------------------------------------------------------------------------
# Daemon loop (run_forever)
# ---------------------------------------------------------------------------


def _critique_doc_to_context(doc_id: str, doc: dict) -> dict:
    """Flatten the critique doc + the related job's artifacts into the
    ``job_context`` shape :func:`agent_mod.build_prompt` expects."""
    return {
        "critique_id": doc_id,
        "job_id": doc.get("job_id"),
        "channel": doc.get("channel"),
        "agent": doc.get("agent", agent_mod.AGENT_CLAUDE),
        "mp4_uri": doc.get("mp4_uri"),
        "script_uri": doc.get("script_uri"),
    }


def process_one_critique(
    client,
    critique_id: str,
    config: RunnerConfig,
    *,
    on_message_seen: Callable[[str], None] | None = None,
) -> None:
    """Drive a single critique from claim → terminal status.

    Subscribes to the messages subcollection and processes each
    ``role=user`` message in arrival order until the parent doc
    leaves an active status.

    Auto-stashes any pre-existing dirty changes BEFORE the agent
    runs so the diff the gates evaluate is purely the agent's work.
    The stash is restored in a try/finally so the user's working
    tree comes back intact regardless of whether the critique
    succeeded, failed, or crashed.
    """
    if not claim_critique(client, critique_id, hostname=config.hostname):
        logger.info("critique %s already claimed by another runner", critique_id)
        return

    stash_label = f"critique-runner/{critique_id}"
    stashed = False
    try:
        stashed = _isolate_pre_existing_dirt(config.repo_root, stash_label=stash_label)
    except RuntimeError as exc:
        msg_mod.add_agent_message(
            client, critique_id,
            text=(
                f"Runner couldn't isolate pre-existing dirt: {exc}. "
                "Recover manually with `git stash list` + `git stash pop`."
            ),
            action=msg_mod.ACTION_AGENT_FINISHED,
        )
        _set_critique_status(
            client, critique_id, STATUS_FAILED,
            extra={"error": "stash_failed"},
        )
        return

    try:
        parent_ref = client.collection("critiques").document(critique_id)
        parent_snap = parent_ref.get()
        if not parent_snap.exists:
            return
        job_context = _critique_doc_to_context(critique_id, parent_snap.to_dict() or {})

        failed_count = 0
        seen_message_ids: set[str] = set()
        # Pre-seed seen with any messages already on the doc so we don't
        # re-process the user's bootstrapping message multiple times if
        # the runner restarts mid-conversation.
        existing = msg_mod.fetch_messages(client, critique_id)
        for m in existing:
            seen_message_ids.add(m.message_id)
            # If the existing seed includes the user's first message and
            # we have NO agent reply yet, treat it as a freshly-arrived
            # user message so the agent gets to act on it.
        has_user_seed = any(m.role == msg_mod.ROLE_USER for m in existing)
        has_agent_reply = any(m.role == msg_mod.ROLE_AGENT for m in existing)
        if has_user_seed and not has_agent_reply:
            # Replay the LAST user message so the agent processes it.
            last_user = [m for m in existing if m.role == msg_mod.ROLE_USER][-1]
            seen_message_ids.discard(last_user.message_id)

        while True:
            # Re-read parent so a status flip from the browser (cancel)
            # bails us out promptly.
            parent_snap = parent_ref.get()
            if not parent_snap.exists:
                return
            cur_status = (parent_snap.to_dict() or {}).get("status")
            if cur_status not in (STATUS_QUEUED, STATUS_IN_PROGRESS):
                return

            # Find the OLDEST unseen role=user message.
            msgs = msg_mod.fetch_messages(client, critique_id)
            unseen_user = [
                m for m in msgs
                if m.role == msg_mod.ROLE_USER and m.message_id not in seen_message_ids
            ]
            if not unseen_user:
                time.sleep(config.poll_interval_s)
                continue
            next_msg = unseen_user[0]
            if on_message_seen is not None:
                on_message_seen(next_msg.message_id)

            next_status, extra, failed_count = process_user_message(
                client, critique_id, job_context, next_msg.text,
                config, failed_turns_so_far=failed_count,
            )
            seen_message_ids.add(next_msg.message_id)

            if next_status in (STATUS_DONE, STATUS_FAILED):
                _set_critique_status(client, critique_id, next_status, extra=extra)
                return

            # in_progress with extra=None → just keep listening for the
            # next user follow-up (need_more_info path) or another
            # iteration after a gate failure (already enqueued above).
    finally:
        if stashed:
            _restore_pre_existing_dirt(config.repo_root, stash_label=stash_label)


def run_forever(
    client,
    config: RunnerConfig,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Long-poll Firestore for queued critiques and process them.

    Single-claim semantics: at most one critique at a time. Once a
    claim is held, the runner blocks on the per-critique loop until
    the doc reaches a terminal status, then resumes scanning.

    ``stop_event`` (optional) lets a host process request a clean
    shutdown without SIGKILL — the loop checks before each poll
    cycle so it bails between critiques, not mid-turn.
    """
    stop_event = stop_event or threading.Event()
    logger.info(
        "critique-runner starting on %s (repo=%s, branch=%s)",
        config.hostname, config.repo_root, config.push_branch,
    )

    while not stop_event.is_set():
        try:
            queued = (
                client.collection("critiques")
                .where("status", "==", STATUS_QUEUED)
                .order_by("created_at")
                .limit(1)
                .get()
            )
        except Exception:  # noqa: BLE001
            logger.warning("Firestore poll failed", exc_info=True)
            stop_event.wait(timeout=config.poll_interval_s)
            continue
        if not queued:
            stop_event.wait(timeout=config.poll_interval_s)
            continue
        snap = queued[0]
        try:
            process_one_critique(client, snap.id, config)
        except Exception:  # noqa: BLE001
            logger.exception("critique %s blew up — marking failed", snap.id)
            try:
                _set_critique_status(
                    client, snap.id, STATUS_FAILED,
                    extra={"error": "runner crashed; see laptop logs"},
                )
            except Exception:  # noqa: BLE001
                logger.warning("could not mark failed", exc_info=True)
