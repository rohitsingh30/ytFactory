# Playwright + signed-in Chrome — technique pointer

The canonical writeup lives inline in the
[`upload-via-playwright` skill](../.claude/skills/upload-via-playwright/SKILL.md)
(sections "Stage 2 — cookie bridge", "Stage 3 — Chrome launch + Playwright
attach", and the "Important rules" list). That skill exists primarily for
the YouTube Studio upload fallback, but the same pattern is reused by any
laptop-side automation that needs to drive a *real, signed-in* Chrome via
Playwright over CDP (e.g. the E2E render smoke at
`tests/e2e_smoke.py`).

## TL;DR

1. **Launcher** — `scripts/playwright_mcp_signed_in.sh` boots a separate
   Chrome instance on a non-default user-data-dir
   (`~/Library/Application Support/Google/Chrome-Debug`) with
   `--remote-debugging-port=9222`, then `exec`s `@playwright/mcp` in
   CDP-attach mode. The Chrome process is launched with `nohup` +
   `disown` so it survives MCP restarts.
2. **Cookie bridge** — on first run the launcher copies SQLite cookie
   files from the user's real Chrome profile into the Chrome-Debug
   profile so Google sessions decrypt against the same macOS keychain.
   Skipped (with a log line) if real Chrome is currently running — that
   safety guard exists because cron jobs (cross_engage, x_scrape) own
   their own Chrome instances and shouldn't be killed.
3. **Profile mapping** — `Chrome-Debug` profile *N* must match the
   real Chrome profile *N* whose `user_name` is the desired Google
   account. Map known accounts ↔ profiles via:
   ```
   cat ~/Library/Application\ Support/Google/Chrome/Local\ State \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); \
       [print(k,"→",v.get("user_name","?")) \
        for k,v in d["profile"]["info_cache"].items()]'
   ```
4. **Playwright attach** — `pw.chromium.connect_over_cdp("http://127.0.0.1:9222")`,
   then `browser.contexts[0]` (the existing signed-in context — *do not*
   call `new_context()`, which is logged out).

## Why this file exists

`upload-via-playwright/SKILL.md` previously linked to
`docs/playwright_with_signed_in_chrome.md`. That file got swept during
the engineering-charter cleanup and the link broke. This pointer
restores the link target without re-stating the full technique (single
source of truth lives in the skill).

## Reference

- `scripts/playwright_mcp_signed_in.sh` — the launcher
- `.claude/skills/upload-via-playwright/SKILL.md` — full technique
- `tests/e2e_smoke.py` — the wizard-driving smoke that reuses the same
  CDP-attach pattern (non-upload use case)
