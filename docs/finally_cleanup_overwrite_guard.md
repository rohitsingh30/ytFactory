# Cleanup `finally` blocks must guard on the failure flag

> **Cross-channel rule.** Established 2026-05-13 from a burner-channel
> dashboard regression that was so masked by this bug that the
> originating system was retired before anyone realised the auth path
> itself was sometimes correct.

## The pattern

When a function calls `_save_state(state)` (or any helper that mirrors
state to disk / GCS / a UI poll surface) **inside** the `finally`
block of an outer `try` — and that bump is intended as a benign
informational message ("Chrome left running", "container shut down
cleanly", "lock released") — guard it with `if state.phase != "failed":`.

Without the guard, the cleanup line **clobbers** the actual diagnostic
written in the `try` body just before a failed `return 1`, and the
operator sees the cleanup line as `last_action_msg` even though the
real cause was something completely different.

```python
# BUG — cleanup unconditionally overwrites the failure message
try:
    if not switch_to_burner_brand(...):
        state.phase = "failed"
        _bump_action(state, "brand-account switch failed — refusing to engage")
        return 1                                  # exits via the finally below
    ...
finally:
    if chrome_proc is not None:
        chrome_proc.terminate()
    else:
        # ↓ overwrites "brand-account switch failed" — operator never sees it
        _bump_action(state, "Chrome left running (we attached to existing) — "
                            "next worker can attach to it directly")
```

```python
# FIX — gate the cleanup bump on the failure flag
finally:
    if chrome_proc is not None:
        chrome_proc.terminate()
    else:
        if state.phase != "failed":              # preserve the diagnostic
            _bump_action(state, "Chrome left running …")
```

## Why this matters operationally

Burner-channel runs surfaced this on 2026-05-13. The dashboard showed
`phase=failed, last_action_msg="Chrome left running …"` for 20 of 31
failed runs. Every operator (and every previous
`/critique-video` / `/update-docs` audit) read the message at face
value and concluded "Chrome cleanup is doing something weird" — when
the actual cause was either `brand-switch failed`, `BrowserType.connect_over_cdp:
Timeout 180000ms`, or `all tabs died` from earlier in the same
function. The 24 hours spent chasing the wrong message materially
contributed to the decision to retire the entire system instead of
fixing it (commit `0508903`).

## How to apply

For every `finally` block (or `__exit__`, or `atexit` callback) in
`pipeline/`, `control/`, or `cloud/`:

1. List every state-mirroring call inside the block —
   `_save_state`, `_bump_action`, `track`, `obs.timed`, `_push_state_gcs`,
   any function that ends up writing into a UI-poll surface.
2. For each, decide: **is the message a benign info line or a
   diagnostic?** Info lines need the guard; diagnostics
   (the actual reason for the failure) don't.
3. The guard pattern is:
   ```python
   if not _is_failed(state):       # or state.phase != "failed"
       _bump_action(state, "<info line>")
   ```

Sweep recipe:

```bash
grep -rnA 3 "finally:" pipeline/ control/ --include='*.py' \
  | grep -E "_bump_action|_save_state|track\(|_push_state_gcs"
```

Every match is a candidate — check whether the message would
overwrite a diagnostic written earlier in the same function.

## Related doc

This pattern surfaced specifically in the now-retired
`pipeline/cross_engage/burner_engage.py::run` (commit `0508903`
deleted the file). The principle is general — apply to any new
long-running worker that uses the `state.phase = "failed";
return 1; finally cleanup` shape.

## Memory

`feedback_finally_cleanup_overwrite_guard.md` (cross-link).
