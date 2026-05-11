"""Agent CLI dispatcher for the critique runner.

Spawns either ``claude`` or ``copilot`` (selected at runtime by the
Firestore doc's ``agent`` field) to do one turn of a critique-fix
conversation. The runner stays agent-agnostic; everything that
differs between Claude and Copilot lives here.

Per-turn flow (orchestrated by :func:`run_agent_turn`):

  1. Build the system prompt from the static template +
     repo-context block (cwd, paths the agent may write).
  2. Append the user-rendered conversation history so the agent
     sees what's already been said + what files it's already
     touched (including any prior gate-failure messages we
     synthesised as USER turns).
  3. Spawn the chosen CLI with ``-p <prompt>`` and the appropriate
     "let it edit files" flags. Stream stdout line-by-line so the
     runner can surface "agent thinking" chips into the chat in
     near real-time.
  4. Parse the trailing ``{"type":"agent_summary",...}`` JSON block
     for structured next-step routing (action: ``done`` /
     ``need_more_info``, files_changed, follow-up questions).

Both binaries are invoked through subprocess.Popen; the runner
NEVER reads from the agent's auth keychain or shell-init configs.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


# Allowed values for the `agent` Firestore field. Mirror
# critique_routes._ALLOWED_AGENTS so changes touch one place.
AGENT_CLAUDE = "claude"
AGENT_COPILOT = "copilot"
_ALLOWED_AGENTS = (AGENT_CLAUDE, AGENT_COPILOT)


# Default per-turn timeout. Long enough that a real fix + tests +
# coverage can finish; short enough that a stuck agent doesn't park
# the whole runner.
DEFAULT_AGENT_TIMEOUT_S = 1800  # 30 min


# Marker the agent prints to delimit the structured-summary tail.
# Tightly-formatted JSON block we look for at the END of stdout to
# extract the agent's self-reported decision.
_SUMMARY_RE = re.compile(
    r'\{"type"\s*:\s*"agent_summary"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class AgentTurnResult:
    """Outcome of one agent turn.

    The runner uses ``action`` to decide what to do next:
      - ``"done"`` → run gates, push if green
      - ``"need_more_info"`` → just emit the agent text + wait for
        the next user message
      - ``"failed"`` → CLI exited non-zero or no parseable summary;
        post the raw stderr tail as an agent message and stop.
    """
    action: str  # "done" | "need_more_info" | "failed"
    text: str  # last assistant turn (full)
    summary: dict | None
    stdout_tail: str
    stderr_tail: str
    exit_code: int
    duration_s: float


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT_TEMPLATE = """\
You are the ytFactory critique-fix agent. The user gave a critique \
about a finished YouTube Short. Your job is to find the \
class-of-bug fix in the pipeline (NOT a one-off content tweak), add \
tests, verify the fix, and stop.

Repo root: {repo_root}

Job context:
{job_context_block}

Conversation so far:
{conversation_history}

Latest user message:
{latest_user_message}

Hard rules:
  1. Make the fix in pipeline/ or <channel>/learnings/ or other \
shared modules — NOT in the per-render artifacts (those are outputs).
  2. Add at least one test (under tests/ or <module>/tests/) that \
would have caught the bug.
  3. Run `pytest -x -q` and confirm green BEFORE saying \"done\". The \
runner will independently re-verify; lying gets you put back into \
\"need_more_info\".
  4. Run `coverage run -m pytest && coverage json -o /tmp/cov.json` \
to confirm 100% coverage on the lines you added.
  5. Do NOT commit yourself — the runner does that on green gates.
  6. End each turn with EXACTLY this JSON block (last line of your \
response):

```json
{{"type":"agent_summary","action":"done","files_changed":["..."],"tests_added":["..."],"rationale":"...","follow_up_questions":[]}}
```

   Action values:
     - "done"           — fix is complete, runner should run gates + push.
     - "need_more_info" — you need clarification; populate \
follow_up_questions[] with what you need.
     - Any other value will fail the turn.
"""


def build_prompt(
    *,
    repo_root: Path,
    job_context: dict,
    conversation_history: str,
    latest_user_message: str,
) -> str:
    """Render the per-turn prompt the CLI receives via ``-p``.

    ``job_context`` should carry the keys the prompt template
    references — channel, mp4_uri, script_uri, and any other artifact
    paths the runner has materialised locally.
    """
    job_lines = []
    for key in ("critique_id", "job_id", "channel", "mp4_local",
                "script_local", "cast_local", "shotlist_local",
                "channel_yaml", "channel_learnings_dir"):
        v = job_context.get(key)
        if v:
            job_lines.append(f"  - {key}: {v}")
    job_block = "\n".join(job_lines) if job_lines else "  (no artifacts materialised yet)"

    return _SYSTEM_PROMPT_TEMPLATE.format(
        repo_root=str(repo_root),
        job_context_block=job_block,
        conversation_history=conversation_history or "  (empty)",
        latest_user_message=latest_user_message,
    )


# ---------------------------------------------------------------------------
# Per-agent argv builders
# ---------------------------------------------------------------------------


def _build_claude_argv(
    prompt: str,
    *,
    repo_root: Path,
    extra_dirs: Iterable[Path] | None = None,
) -> list[str]:
    """Claude argv — non-interactive, full repo edit access.

    ``--dangerously-skip-permissions`` is the equivalent of
    ``--allow-all`` in copilot; the alternative is for the agent to
    pause every Edit/Bash invocation waiting for stdin (which the
    runner can't supply since it's headless).
    """
    argv = [
        "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--add-dir", str(repo_root),
    ]
    for d in (extra_dirs or []):
        argv += ["--add-dir", str(d)]
    return argv


def _build_copilot_argv(
    prompt: str,
    *,
    repo_root: Path,
    extra_dirs: Iterable[Path] | None = None,
) -> list[str]:
    """Copilot argv — non-interactive, allow-all so it doesn't pause
    on file/bash/url permission prompts.

    Sticking to ``--allow-all`` (not narrower flags) intentionally:
    the agent may need ``Bash(*)`` for pytest, ``Edit`` for fixes,
    and read access to the mp4 sample dir. Narrowing each is more
    LoC + more brittle than trusting the runner-driven gate sweep
    to catch everything.
    """
    argv = [
        "copilot",
        "-p", prompt,
        "--allow-all",
        "--add-dir", str(repo_root),
    ]
    for d in (extra_dirs or []):
        argv += ["--add-dir", str(d)]
    return argv


def _argv_for_agent(
    agent: str,
    prompt: str,
    *,
    repo_root: Path,
    extra_dirs: Iterable[Path] | None = None,
) -> list[str]:
    if agent == AGENT_CLAUDE:
        return _build_claude_argv(prompt, repo_root=repo_root, extra_dirs=extra_dirs)
    if agent == AGENT_COPILOT:
        return _build_copilot_argv(prompt, repo_root=repo_root, extra_dirs=extra_dirs)
    raise ValueError(f"unknown agent {agent!r}; allowed: {_ALLOWED_AGENTS}")


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def parse_agent_summary(stdout: str) -> dict | None:
    """Find the trailing ``{"type":"agent_summary",...}`` JSON block
    in the agent's stdout. Returns None if missing / malformed —
    callers treat that as a failed turn.

    We tolerate the block being inside a fenced code block, so the
    regex hunts for the literal JSON after stripping ``` markers.
    """
    if not stdout:
        return None
    cleaned = stdout.replace("```json", "").replace("```", "")
    matches = list(_SUMMARY_RE.finditer(cleaned))
    if not matches:
        return None
    raw = matches[-1].group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("agent_summary JSON parse failed: %r", raw[:200])
        return None
    if data.get("type") != "agent_summary":
        return None
    return data


# ---------------------------------------------------------------------------
# Turn runner
# ---------------------------------------------------------------------------


def run_agent_turn(
    agent: str,
    prompt: str,
    *,
    repo_root: Path,
    extra_dirs: Iterable[Path] | None = None,
    timeout_s: int = DEFAULT_AGENT_TIMEOUT_S,
    on_stdout_line: Callable[[str], None] | None = None,
    env_overrides: dict | None = None,
) -> AgentTurnResult:
    """Execute one agent turn end-to-end.

    ``on_stdout_line`` is invoked for every line the CLI prints to
    stdout, in real time, so the runner can surface "agent thinking"
    chips into the Firestore chat as the turn progresses (instead of
    making the user wait the full 30 min for the result).
    """
    argv = _argv_for_agent(agent, prompt, repo_root=repo_root, extra_dirs=extra_dirs)
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    # Force unbuffered output so the line-stream is real-time, not
    # batched in 8 KB blocks. Both CLIs respect the standard
    # PYTHONUNBUFFERED / NODE_NO_OUTPUT_BUFFERING contracts but
    # belt-and-braces won't hurt.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("FORCE_COLOR", "0")  # ANSI colour codes pollute the chat

    logger.info("agent turn: %s (timeout=%ds, repo=%s)", agent, timeout_s, repo_root)
    logger.debug("argv: %s", " ".join(shlex.quote(a) for a in argv))

    t0 = time.time()
    proc = subprocess.Popen(
        argv,
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    # Drain stdout line-by-line so on_stdout_line fires in real time.
    # stderr we read at the end (lower-priority for chat display).
    assert proc.stdout is not None
    try:
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if not line:
                # rare: poll returned None but readline empty; backoff
                time.sleep(0.05)
                continue
            stdout_lines.append(line)
            if on_stdout_line is not None:
                try:
                    on_stdout_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001
                    logger.warning("on_stdout_line callback raised", exc_info=True)
            if (time.time() - t0) > timeout_s:
                proc.kill()
                logger.warning("agent turn timed out after %ds, killing", timeout_s)
                break
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stderr is not None:
            stderr_lines.extend(proc.stderr.readlines())

    elapsed = time.time() - t0
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)

    summary = parse_agent_summary(stdout)
    exit_code = proc.returncode if proc.returncode is not None else -1

    # Decide the action. Prefer the agent's self-reported summary;
    # fall back to "failed" on non-zero exit OR missing summary.
    if exit_code != 0:
        action = "failed"
    elif summary is None:
        action = "failed"
    else:
        action = str(summary.get("action") or "failed")
        if action not in ("done", "need_more_info"):
            action = "failed"

    # Tail for chat display — too much stdout would balloon Firestore
    # docs (1 MiB hard limit). 4 KB tail covers the agent's last
    # rationale + the JSON summary block in every realistic case.
    return AgentTurnResult(
        action=action,
        text=stdout[-4000:] if stdout else "",
        summary=summary,
        stdout_tail=stdout[-4000:],
        stderr_tail=stderr[-2000:],
        exit_code=exit_code,
        duration_s=elapsed,
    )
