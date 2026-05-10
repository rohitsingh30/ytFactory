# Test isolation — never silently attach to live dev infrastructure

**Established 2026-05-09** after `tests/test_e2e_happy_path.py` polluted the
developer's live in-memory dashboard with 20 `"E2E test render"` rows in
~35 minutes.

## The rule

Tests MUST NOT default to attaching to whatever happens to be listening
on a well-known dev port (8765, 8766, etc.). Default behavior must be
**always boot an ephemeral instance on a random free port**.

If the operator wants to point a test at a live or remote target (smoke
test against staging, manual debugging against the local dev server),
that is opt-in only — via an explicit env var like `YTFACTORY_E2E_BASE`.

## What went wrong (2026-05-09)

`tests/test_e2e_happy_path.py::HappyPathE2ETest.setUpClass` did this:

```python
host = "127.0.0.1"
port = 8766                      # the canonical live-control-plane port
if _is_listening(host, port):
    cls._spawned = False         # → reuse whatever is there
    return
# else: boot our own
```

Combined with the **auto-test rule** (`docs/auto_test_rule.md` /
`CLAUDE.md` § "Auto-test rule") that fires the suite after every code
edit to `pipeline/`, `web/`, `tests/`, etc., the result was:

1. Developer had `uvicorn control.server_dev:app --port 8766` running
   (the canonical website-first production substrate per CLAUDE.md
   § "Website-first production").
2. Every code edit kicked the suite.
3. Each suite run found 8766 listening, attached, and `POST /api/render`
   with `topic="E2E test render"` `channel="historyrecapped"`.
4. After ~20 edits in ~35 min, the dashboard showed 20 pollution rows
   the developer had to ask "what is this bullshit filled with?".

The in-memory backend made this hard to clean up — no DELETE endpoint,
restart wiped all jobs (including 1 legit AITA render the dev wanted).

## The fix

`tests/test_e2e_happy_path.py` now:

- `BASE = os.environ.get("YTFACTORY_E2E_BASE", "")` — empty by default.
- `setUpClass` always allocates a free port via `_free_port()` (binds
  to `127.0.0.1:0`, returns OS-assigned port), boots its own uvicorn,
  sets `BASE` to that URL.
- Calls `control.jobs.reset_jobs()` before booting to force a fresh
  in-memory backend.
- Only skips the boot if `YTFACTORY_E2E_BASE` is explicitly set in env.

## Audit checklist for any new test that talks HTTP

When adding a test that hits any HTTP endpoint, verify ALL of:

- [ ] Default boots its own server on a random free port (not a fixed port).
- [ ] Never probes well-known dev ports (8765 web, 8766 control plane,
      anything in `.env` `*_URL` vars).
- [ ] Resets module-global singletons (jobs backend, queue backend,
      cache) before/after to avoid cross-test bleed.
- [ ] Opt-in env var for remote/live targeting, **not** auto-detection.
- [ ] Test data is recognizable — `topic="<test_name> render"` with the
      test name in it, not a generic string — so accidental pollution
      is identifiable in 2 seconds, not 5 minutes.

## Why this rule is permanent

The auto-test rule (`rule_auto_test_after_edit.md`) is good and stays
— but it amplifies any "attach if available" pattern in tests by an
order of magnitude (10-30 runs/hour during active dev). Any test that
auto-attaches to live infra becomes a destructive write multiplier,
not a single-incident bug. Fix the multiplier (test isolation), not
the trigger (auto-test rule).

## See also

- `tests/test_e2e_happy_path.py` — fixed implementation (the docstring
  links back here).
- `CLAUDE.md` § "Auto-test rule" — the trigger.
- `CLAUDE.md` § "Website-first production" — establishes 8765 (web)
  and 8766 (control plane) as canonical dev ports tests must avoid.
