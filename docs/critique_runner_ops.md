# Critique runner ops (2026-05-12)

> Three production bugs in the critique-chat runner surfaced when the
> user clicked "Start critique" from the dashboard. All three are now
> fixed and pinned by tests; this doc captures what to do when any of
> them recurs and how to keep the runner healthy.

The critique runner is the laptop-side daemon that picks up `critiques/<id>`
docs from Firestore, drives a Claude/Copilot agent through a fix-add-test
loop against the local repo, runs hard gates, and pushes to `main`. See
[`docs/critique_chat.md`](./critique_chat.md) for the architecture; this
doc is the operations counterpart.

## 1. Auto-start on login (was: forgotten after every reboot)

**Symptom:** dashboard shows "runner not picking up — Your message landed
in Firestore but the laptop hasn't claimed it yet. On your laptop, run:
`cd /Users/rohit/ytFactory && make critique-runner`."

**Root cause:** the runner ran as a manually-launched process. After
laptop reboot OR `make critique-runner` Ctrl-C'd OR a hung Python crash,
the runner was just gone, with no auto-restart. The user had to remember
to start it. They didn't.

**Fix (shipped commit `2fb4b1f`, 2026-05-12):**
`control/com.ytfactory.critique-runner.plist` is a launchd LaunchAgent
modelled on the launchd LaunchAgent pattern (KeepAlive + ThrottleInterval):

- `RunAtLoad=true` → starts the moment the user logs in.
- `KeepAlive=true` → restarts on crash (within `ThrottleInterval=30s`).
- `StandardOutPath`/`StandardErrorPath` → `/Users/rohit/.config/ytfactory/critique-runner.log`.
- Env vars: `PATH` includes `/opt/homebrew/bin` (git, claude),
  `CLOUDSDK_CONFIG=/Users/rohit/.config/gcloud` (Firestore auth),
  `GOOGLE_CLOUD_PROJECT=ytfactory-prod-v2`.

Install once:

```bash
cp control/com.ytfactory.critique-runner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ytfactory.critique-runner.plist
```

Verify it's alive:

```bash
launchctl list | grep ytfactory.critique-runner
# → 62190    0    com.ytfactory.critique-runner
#   (PID, last exit code, label — exit code 0 = healthy)
pgrep -f scripts/critique_runner.py
# → should print one PID (the bash wrapper) + one child (the python)
```

Tail the log:

```bash
tail -f /Users/rohit/.config/ytfactory/critique-runner.log
```

Restart after pulling new runner code (e.g. after this PR ships):
```bash
launchctl unload ~/Library/LaunchAgents/com.ytfactory.critique-runner.plist
launchctl load   ~/Library/LaunchAgents/com.ytfactory.critique-runner.plist
```

On modern macOS (Sonoma+, 2023+) the legacy `unload`/`load` pair is
sometimes a no-op when the agent was previously `bootout`'d AND the
persistent `Disabled` flag is still set. The reliable revival triple
that always brings the agent back up:

```bash
U=$(id -u)
launchctl enable    gui/$U/com.ytfactory.critique-runner
launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.ytfactory.critique-runner.plist
launchctl kickstart -k gui/$U/com.ytfactory.critique-runner
launchctl print     gui/$U/com.ytfactory.critique-runner | grep -E 'state|pid ='
# expect: state = running, pid = N
```

Diagnosis when the agent is silently dead:

```bash
launchctl print gui/$(id -u)/com.ytfactory.critique-runner 2>&1 | head -3
```

* `state = running` → agent is up; investigate elsewhere.
* `Could not find service` / `=> disabled` → run the enable+bootstrap+
  kickstart triple above. The **same lifecycle pattern** (silent stuck-
  in-disabled state) bit `com.ytfactory.laptop-agent` for ~24h on
  2026-05-13 before that whole subsystem was retired —
  see [`docs/finally_cleanup_overwrite_guard.md`](./finally_cleanup_overwrite_guard.md)
  for an analogous "diagnostic message clobbered" pattern that made
  the launchd-disabled state hard to spot.

**Do NOT restart while a critique is `in_progress`** — the in-flight
agent turn loses context. Wait for the current critique to reach
`done`, `failed`, or `abandoned` first (poll
`critiques/<id>` in Firestore).

## 2. Agent-turn watchdog (was: 24-hour silent hang)

**Symptom:** a critique sits at status `in_progress` indefinitely.
`pgrep -f "claude -p"` shows the subprocess is alive but produced
no output for hours. The runner's `agent turn: claude (timeout=1800s, …)`
log entry is days old.

**Root cause:** `pipeline/critique/agent.py::run_agent_turn` checked
the per-turn timeout INSIDE the `proc.stdout.readline()` loop:

```python
# pre-fix
while True:
    line = proc.stdout.readline()      # blocks if claude is silent
    ...
    if (time.time() - t0) > timeout_s:
        proc.kill()                    # never reached
```

When claude opens an interactive prompt and waits for stdin (a real
failure mode of `claude -p ... --dangerously-skip-permissions`),
`readline()` blocks on the read syscall and the timeout check never
runs. **One real incident:** critique `63651d8d` survived 24 hours
past the 30-min timeout on 2026-05-11.

**Fix (shipped commit `2fb4b1f`, 2026-05-12):**
`threading.Timer` watchdog that fires unconditionally after `timeout_s`
seconds, kills the subprocess (closing stdout, which lets readline()
return EOF), and stamps `stderr_tail` with `[runner-watchdog] agent turn killed after Ns`
so the chat panel knows what happened. Cancelled in the `finally`
block so fast-exiting agents don't get killed retroactively.

Pin: `tests/test_critique_runner.py::AgentTurnIntegrationTests::test_watchdog_kills_silently_blocked_agent`
runs an agent snippet that sleeps 120 s with no stdout — pre-fix this
hung the test runner; with the watchdog it completes in ~2 s.

**Recovery for a stuck pre-fix runner (manual steps):**

```bash
# 1. Find the stuck claude PID (parent will be the runner).
pgrep -f "claude -p"

# 2. Kill it. The runner's `proc.wait(timeout=5)` finally block reaps it.
kill <pid>

# 3. If the runner itself is wedged (rare), kill it too — launchd will
#    restart automatically.
pgrep -f scripts/critique_runner.py | xargs kill

# 4. Reset the stranded Firestore claim so a fresh runner picks it up.
.venv/bin/python -c "
from google.cloud import firestore
fs = firestore.Client(project='ytfactory-prod-v2')
fs.collection('critiques').document('<critique_id>').update({
    'status': 'abandoned',
    'claimed_by': None,
    'claimed_at': None,
    'error': 'runner stuck; reset by ops',
})"
```

## 3. Streaming progress chips (was: "running claude…" for 12 minutes)

**Symptom:** chat panel shows a single "running claude…" message that
never updates. The agent IS productive (you can verify by reading
`/Users/rohit/.claude/projects/-Users-rohit-ytFactory/<sessionId>.jsonl`)
but the user has zero visibility into what tool calls are happening.

**Root cause:** `pipeline/critique/runner.py::process_user_message`
passed `on_stdout_line=_on_line` to `run_agent_turn`, but `_on_line`
was a no-op (`return None`). Real claude turns stream 30-100 tool-use
lines (`● Read(file)`, `● Edit(file)`, `● Bash(cmd)`, …); the runner
saw them all but dropped them on the floor.

**Fix (shipped commit `fdb5b14`, 2026-05-12):**

- `_TOOL_USE_RE` regex matches the bullet-prefixed lines.
- `_TOOL_TO_CHIP` maps tool name → chat-panel action chip
  (`Read`/`Glob`/`Grep` → `file_read`, `Edit`/`MultiEdit`/`Write` →
  `file_edited`, `Bash` → `agent_thinking`).
- `_parse_tool_use_chip(line)` returns `{action, text}` or `None`.
- `_on_line` is now a real implementation that writes a throttled
  action chip to the messages subcollection — 4 s minimum between
  writes (`_MIN_STREAM_EMIT_INTERVAL_S`) + same-text dedupe.
- Every chip emit is wrapped in `try/except` — Firestore outage
  during a turn never propagates into the agent process; full stdout
  is still preserved in `AgentTurnResult.stdout_tail` for debug.

Pins:
- `ParseToolUseChipTests` (8 cases) — regex against real claude CLI
  v2.1.139 output.
- `ProcessUserMessageTests::test_streams_tool_use_lines_to_chat_as_chips` —
  proves chips ACTUALLY land in Firestore.
- `ProcessUserMessageTests::test_streaming_callback_swallows_firestore_errors` —
  defence-in-depth.
- `ProcessUserMessageTests::test_streaming_throttle_drops_rapid_duplicates` —
  pins the throttle + dedupe.

## 4. Diagnostic git failure messages (was: "git status failed: .")

**Symptom:** chat shows "Runner couldn't isolate pre-existing dirt:
git status failed: . Recover manually with `git stash list` + `git stash pop`."
The user has nothing to act on — the `.` after "failed:" is just the
trailing period from the f-string.

**Root cause:** `f"git status failed: {proc.stderr.strip()}"` collapses
to literally that when git exits non-zero with empty stderr — most
commonly a `.git/index.lock` race against a concurrent `git commit`
(e.g. when the user is doing their own work simultaneously).

**Fix (shipped commit `fdb5b14`, 2026-05-12):**
`_git_failure_diag(cmd_label, proc, cwd)` composes a useful message
in EVERY case: returncode + stderr (or stdout fallback) + cwd + an
explicit `.git/index.lock contention` hint when both streams are empty.

Example before / after:

```text
# pre-fix:
git status failed: .

# post-fix (typical error):
git status failed (rc=128); stderr: fatal: not a git repository; cwd: /tmp/x

# post-fix (the real index-lock case):
git status failed (rc=128); no git output (likely .git/index.lock contention
with another git process — try again in a moment); cwd: /Users/rohit/ytFactory
```

Pin: `tests/test_critique_runner.py::GitFailureDiagTests` (4 cases) +
`IsolatePreExistingDirtTests::test_isolate_raises_diagnostic_when_git_status_fails`.

## Health check (one-liner the user can run any time)

```bash
launchctl list | grep ytfactory.critique-runner && \
  pgrep -f scripts/critique_runner.py >/dev/null && \
  echo "✓ runner alive" || \
  echo "✗ runner DOWN — launchctl load ~/Library/LaunchAgents/com.ytfactory.critique-runner.plist"
```

## Cross-references

- Architecture: `docs/critique_chat.md`
- Source: `pipeline/critique/runner.py`, `pipeline/critique/agent.py`,
  `scripts/critique_runner.py`
- Plist: `control/com.ytfactory.critique-runner.plist`
- Tests: `tests/test_critique_runner.py` (47 cases covering all four
  bug classes above)
- Memory: `feedback_critique_runner_ops.md`
