# Driving Playwright with your real signed-in Chrome profile

How to make Playwright (Python or via MCP) attach to a Chrome window that is already signed into your real Google accounts — useful for any automation that needs the logged-in session of one of your YouTube channels, Gmail, Drive, etc.

This is the technique that actually works on this laptop in **2026** with **Chrome v136+** (the version that hardcoded the security policy that breaks the obvious approach). Validated 2026-05-08 with the first ScrollPulse upload.

---

## TL;DR

```
[your real Chrome, Profile N]   ──cookies──▶   [Chrome-Debug, Profile N]
                                                      │
                                                      │ --user-data-dir=…/Chrome-Debug
                                                      │ --profile-directory="Profile N"
                                                      │ --remote-debugging-port=0
                                                      ▼
                                              [Chrome process; CDP port written to stderr]
                                                      │
                                                      │ http://127.0.0.1:<port>
                                                      ▼
                                              [Playwright connect_over_cdp]
                                                      │
                                                      ▼
                                              [drive the page — already signed in]
```

Three reusable building blocks: **Chrome-Debug as a separate user-data-dir**, **cookie bridge from your real Chrome**, **Playwright via CDP**.

---

## Why the obvious approach fails

You'd think you could just point Chrome at your real profile and turn on remote debugging:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome" \
  --profile-directory="Profile 3"
```

Chrome **silently drops** the remote-debugging flag and writes to stderr:

```
DevTools remote debugging requires a non-default data directory.
Specify this using --user-data-dir.
```

This is a Chromium **security policy** added in v136. It refuses remote debugging on the default user-data-dir even when you pass the flag explicitly — the check is path-based. Sources:

- [Chromium issue: default-data-dir remote-debug rejection](https://github.com/openclaw/openclaw/issues/46483)
- [DEV: Running Playwright Codegen with existing Chromium Profiles](https://dev.to/mxschmitt/running-playwright-codegen-with-existing-chromium-profiles-5g7k)
- [Playwright BrowserType docs (`launchPersistentContext`)](https://playwright.dev/docs/api/class-browsertype)

There is **no flag bypass**. Every workaround uses a non-default user-data-dir.

---

## The pattern: Chrome-Debug as a parallel user-data-dir

`~/Library/Application Support/Google/Chrome-Debug/` is just a folder. Chrome doesn't care that it's not the "real" location — it's not the default, so remote debugging works there. It can hold its own set of profiles, each one able to be signed into different Google accounts.

This is exactly what the **shofferAi** project does (`~/shofferAi/apps/playwright/scripts/playwright-mcp-with-chrome.sh`) — it's the canonical pattern on this laptop.

Set it up once:

1. Launch Chrome pointed at Chrome-Debug:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --user-data-dir="$HOME/Library/Application Support/Google/Chrome-Debug" \
     --profile-directory="Profile 3"
   ```
2. Sign into the Google account you want to automate (e.g. `rsinghtomar3011@gmail.com`).
3. Bookmarks, sync, extensions don't matter — only the auth session matters.
4. Quit Chrome.

Now `Chrome-Debug/Profile 3` has a persistent signed-in session for that account. Future Playwright runs attach here. Each automation account gets its own Profile inside Chrome-Debug.

Inspect the mapping:

```python
import json, os
ls = json.load(open(os.path.expanduser(
    '~/Library/Application Support/Google/Chrome-Debug/Local State')))
for k, v in ls['profile']['info_cache'].items():
    if v.get('user_name'):
        print(k, '->', v['user_name'])
# Profile 1  ->  rsinghtomar54@gmail.com
# Profile 3  ->  rsinghtomar3011@gmail.com
# Profile 4  ->  rohit30.iitkgp@gmail.com
```

---

## When Chrome-Debug's session goes stale: the cookie bridge

Google sessions in Chrome-Debug last ~30 days, but if you only automate occasionally they may expire. When they do, instead of re-signing in, you can **bridge cookies from your real Chrome** (which is signed in because you use it daily).

Same macOS user → same Keychain → same `os_crypt` decryption key → cookies decrypt correctly even though they originated in a different user-data-dir.

```bash
# Quit Chrome first — cookie SQLite DB is locked while Chrome runs.
pkill -f "Google Chrome.app/Contents/MacOS/Google Chrome" && sleep 2

SRC="$HOME/Library/Application Support/Google/Chrome"
DST="$HOME/Library/Application Support/Google/Chrome-Debug"
SRC_PROFILE="$SRC/Profile 3"
DST_PROFILE="$DST/Profile 3"

mkdir -p "$DST_PROFILE/Network"

# Top-level Local State has os_crypt.encrypted_key
cp "$SRC/Local State" "$DST/Local State"

# Profile-level auth state
for f in "Cookies" "Cookies-journal" "Login Data" "Login Data-journal" \
         "Web Data" "Web Data-journal" "Preferences"; do
  [ -f "$SRC_PROFILE/$f" ] && cp "$SRC_PROFILE/$f" "$DST_PROFILE/$f"
done

# Newer Chrome (v123+) stores cookies under Profile/Network/
for f in "Cookies" "Cookies-journal"; do
  [ -f "$SRC_PROFILE/Network/$f" ] && cp "$SRC_PROFILE/Network/$f" "$DST_PROFILE/Network/$f"
done
```

After the bridge, Chrome-Debug/Profile 3's signed-in state matches your real Chrome's Profile 3. Zero clicks from you.

---

## Launching Chrome with CDP + connecting Playwright

```python
import subprocess, time, re, pathlib
from playwright.sync_api import sync_playwright

USER_DATA_DIR = "/Users/rohit/Library/Application Support/Google/Chrome-Debug"
PROFILE = "Profile 3"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP = pathlib.Path("/tmp/pw-chrome"); TMP.mkdir(exist_ok=True)
STDERR = TMP / "chrome.stderr"

# Clear any leftover singleton locks from a prior unclean exit
for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
    p = pathlib.Path(USER_DATA_DIR) / f
    if p.exists(): p.unlink()

# Launch Chrome with port=0 so it picks a free one and writes it to stderr
proc = subprocess.Popen(
    [
        CHROME_BIN,
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={USER_DATA_DIR}",
        f"--profile-directory={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=AutomationControlled",
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        "about:blank",
    ],
    stdout=subprocess.DEVNULL,
    stderr=open(STDERR, "w"),
)

# Scrape the chosen port from stderr
cdp_port = None
for _ in range(40):
    time.sleep(0.3)
    if STDERR.exists():
        m = re.search(r"ws://127\.0\.0\.1:(\d+)", STDERR.read_text())
        if m:
            cdp_port = m.group(1); break
assert cdp_port, f"no CDP port — stderr:\n{STDERR.read_text()}"

# Attach Playwright
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    ctx = browser.contexts[0]   # IMPORTANT: use existing context (signed-in)
    page = ctx.new_page()
    page.goto("https://studio.youtube.com/")
    # ... drive whatever you need; you're signed in.

proc.terminate()
```

### Key points

- **`--remote-debugging-port=0`** — let Chrome pick a free port. Reading the chosen port from stderr (`ws://127.0.0.1:<port>`) is more reliable than guessing 9222.
- **`browser.contexts[0]`** — DO NOT call `browser.new_context()`. The existing context is the one with cookies + signed-in state. A fresh context starts logged out.
- **`--disable-blink-features=AutomationControlled`** — hides the `navigator.webdriver` flag that some sites use to detect automation. Works against most consumer sites; doesn't fool full bot detection.
- **Singleton locks** — kill any leftover `SingletonLock` files in the user-data-dir before launching, otherwise Chrome refuses to start.

---

## Pre-flight: Chrome must be closed

Both your real Chrome and Chrome-Debug share the same `Chrome.app` binary. Cookie DBs are locked while any Chrome is running. Always:

```python
import subprocess
out = subprocess.run(
    ["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
    capture_output=True, text=True,
)
if out.stdout.strip():
    raise SystemExit(
        "Chrome still running. Cmd-Q every window then re-run."
    )
```

If your local cron jobs auto-launch Chrome (e.g. `pipeline.research.cross_engage` view spawns), pause them before running. Multiple Chromes against the same user-data-dir will cause SingletonLock conflicts.

---

## Engagement actions (like, subscribe) on the watch page

Validated 2026-05-08 against `l_7wQXOeVPc` from 4 sibling profiles via
`pipeline.cross_engage_via_playwright`. The watch page (`youtube.com/watch?v=`)
exposes both like and subscribe in stable selectors — prefer it over
`shorts/<id>` for engagement automation.

### Selectors (priority-ordered tuples, first hit wins)

```python
LIKE_SELECTORS = (
    "button[aria-label*='like this video' i][aria-pressed]",  # primary
    "button[aria-label*='Like this' i]",
    "ytd-toggle-button-renderer #like-button button",
    "like-button-view-model button",
)
SUBSCRIBE_SELECTORS = (
    "ytd-subscribe-button-renderer button",                    # primary
    "yt-subscribe-button-view-model button",
    "button[aria-label*='Subscribe to' i]",
    "ytd-watch-metadata #subscribe-button-shape button",
)
```

### State probes

- **Like:** `btn.get_attribute("aria-pressed")` → `"true"` = liked, `"false"` = unliked.
- **Subscribe:** check `btn.text_content()` + `btn.get_attribute("aria-label")` lower-cased — `"subscribed"` or `"unsubscribe"` substring → already subscribed; `"subscribe"` substring (without prefix) → unsubscribed.

### THE rule for clicking — use `dispatch_event`, never `click()`

YouTube renders **multiple like buttons** in parallel DOM trees (theater
mode + standard layout); only one is visible at a time. Playwright's
visibility-aware actionability checks consistently match the hidden one
and either time out or refuse the click:

```python
btn.click(timeout=8000)                             # ❌ TimeoutError "element not visible"
btn.scroll_into_view_if_needed(); btn.click(force=True)  # ❌ scroll fails first, "not visible"
```

The fix:

```python
btn.dispatch_event("click", timeout=8000)            # ✅ delivers DOM click regardless
```

`dispatch_event` bypasses Playwright's actionability checks and fires the
click event directly through the DOM. YouTube's listener picks it up the
same as a real user click.

This is THE rule for ALL YouTube-UI automation, not just like buttons.
Subscribe doesn't have the hidden-sibling problem (`click()` works), but
use `dispatch_event` everywhere for consistency. Validated on 4 profiles.

### Idempotency note

After a click, **wait 4–6 s** (YouTube debounces UI updates). Then re-probe
state — that's the source of truth. If your `click` reported a TimeoutError
but the post-click state probe shows the change happened, **the click
landed**; treat it as success. The TimeoutError is on Playwright's
verification of click delivery, not on the click itself.

Re-bridging cookies (real Chrome → Chrome-Debug) **wipes Chrome-Debug's
local engagement UI state**, but YouTube's server-side state persists. So
re-running an engagement cycle on the same video is safe — the post-load
state probe correctly reads `already_liked` / `already_subscribed` from
server data. **Idempotent at YouTube's end.** Don't worry about
deduplicating.

### Reference implementation

[`pipeline/cross_engage_via_playwright.py`](../pipeline/cross_engage_via_playwright.py)
— `fanout(video_url, source_profile="Profile N")` cycles every other profile
and runs the like + subscribe pattern. ~30 s per profile sequential
(Chrome-Debug locks the user-data-dir per process — no parallelism).

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `DevTools remote debugging requires a non-default data directory.` | Pointed `--user-data-dir` at the real `~/Library/Application Support/Google/Chrome` | Use Chrome-Debug instead |
| Page redirects to `accounts.google.com/v3/signin/confirmidentifier` | Chrome-Debug/Profile N session expired | Run the cookie bridge from the real Chrome's matching Profile N |
| `Target page, context or browser has been closed` mid-script | User Cmd-W'd the browser window | Don't touch the Chrome window during automation |
| Chrome refuses to start, `SingletonLock` exists | Prior unclean exit | Delete `SingletonLock`, `SingletonSocket`, `SingletonCookie` before launch |
| Cookies fail to decrypt after copy | You bridged Cookies but forgot `Local State` | Always copy `Local State` (top-level) — it has `os_crypt.encrypted_key` |
| Site detects automation despite all flags | Bot-detection heuristics beyond `navigator.webdriver` | Some sites can't be fully evaded; consider headed-with-human-pause |
| Chrome immediately exits with no stderr | `Chrome.app/Contents/MacOS/Google Chrome` was killed by macOS Gatekeeper | Quarantine: `xattr -cr "/Applications/Google Chrome.app"` |

---

## Using this with Playwright MCP

The MCP server `@playwright/mcp` accepts a `--cdp-endpoint` flag that lets it connect to an already-running Chrome instead of launching its own. Combined with the launch sequence above:

```bash
"$CHROME_BIN" --remote-debugging-port=0 \
  --user-data-dir=...Chrome-Debug \
  --profile-directory="Profile 3" \
  about:blank 2>"$STDERR" &
sleep 2
CDP_PORT=$(grep -oE 'ws://127\.0\.0\.1:[0-9]+' "$STDERR" | head -1 | grep -oE '[0-9]+$')

cat > "$CONFIG" <<JSON
{
  "browser": {
    "browserName": "chromium",
    "cdpEndpoint": "http://127.0.0.1:${CDP_PORT}"
  }
}
JSON

playwright-mcp --config "$CONFIG"
```

Then point your `mcp.json` at this wrapper script (e.g. `playwright-mcp-with-chrome.sh`). Tool calls like `mcp__playwright__browser_navigate` route through the persistent signed-in Chrome.

The shofferAi project's full version of this is at `/Users/rohit/shofferAi/apps/playwright/scripts/playwright-mcp-with-chrome.sh` — it adds per-PID instance dirs, selective rsync to /tmp for parallelism, and crash-cleanup. Start there if you need parallel sessions.

---

## When to use which approach

| Need | Use |
|---|---|
| One-off scripted browser automation as a signed-in account | The Python launch sequence above + `connect_over_cdp` |
| Long-lived MCP session (Claude Code drives the browser conversationally) | `playwright-mcp-with-chrome.sh` style wrapper, configured in `mcp.json` |
| Parallel automations (multiple accounts at once) | shofferAi's per-PID rsync pattern |
| Anonymous browsing (no sign-in needed) | Vanilla Playwright `chromium.launch()` — no Chrome-Debug needed |
| API-only operations (no UI) | Skip Playwright entirely; use the relevant SDK (e.g. `pipeline.upload.youtube_upload` via OAuth token) |

---

## File checklist (cross-reference)

- `/Users/rohit/shofferAi/apps/playwright/scripts/playwright-mcp-with-chrome.sh` — canonical reference implementation
- `/Users/rohit/shofferAi/apps/playwright/scripts/stealth-init.js` — companion init script for bot-detection evasion
- `~/.claude.json` — your Claude Code MCP server config (look at the `mcpServers.playwright` block)
- `~/Library/Application Support/Google/Chrome-Debug/` — separate user-data-dir for automation
- `~/Library/Application Support/Google/Chrome/` — your real Chrome (do NOT use directly with `--remote-debugging-port`)

---

## Coexistence — attach to a running Chrome-Debug instead of relaunching (2026-05-09)

Earlier callers (`upload_via_playwright`, `cross_engage_via_playwright`)
hard-asserted `assert_chrome_closed()` and refused to run if any Chrome
process was alive. That made them brittle:

- A leftover Chrome-Debug from a prior `--keep-open` run (or from
  `pipeline.create_burner_channel`) blocked the next invocation
  entirely — until the user manually `kill <PID>`.
- Re-launching also re-ran the **destructive cookie bridge** which
  overwrites Chrome-Debug's cookies with real Chrome's. Any live
  YouTube session you signed into INSIDE Chrome-Debug got wiped.

The pattern that fixes both, validated 2026-05-09 in
`pipeline/create_burner_channel.py::find_running_chrome_debug`:

```python
import subprocess, urllib.request, pathlib, re

CHROME_DEBUG_DIR = "/Users/rohit/Library/Application Support/Google/Chrome-Debug"

def find_running_chrome_debug(profile: str) -> tuple[int, int] | None:
    # macOS pgrep -af doesn't print argv (BSD vs GNU); use ps -axww instead.
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid,command"], capture_output=True, text=True,
    )
    for line in out.stdout.splitlines():
        if "Google Chrome.app/Contents/MacOS/Google Chrome" not in line:
            continue
        if " --type=" in line:           # skip helpers (renderer/gpu/etc.)
            continue
        if (f"--user-data-dir={CHROME_DEBUG_DIR}" not in line
                or f"--profile-directory={profile}" not in line):
            continue
        pid = int(line.strip().split()[0])
        # Recover the ephemeral CDP port from sibling chrome.stderr files
        for f in pathlib.Path("/tmp").glob("pw-*/**/chrome.stderr"):
            try:
                text = f.read_text(errors="replace")
            except OSError: continue
            m = re.search(r"ws://127\.0\.0\.1:(\d+)", text)
            if not m: continue
            port = int(m.group(1))
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2
                ) as r:
                    if r.status == 200:
                        return pid, port
            except Exception:
                continue
    return None
```

Caller pattern:
```python
existing = find_running_chrome_debug(profile)
if existing is not None:
    attach_pid, port = existing
    # SKIP bridge_cookies + launch_chrome_for entirely
else:
    bridge_cookies(profile)
    proc, port = launch_chrome_for(profile, work_dir=…)
# ... attach Playwright via CDP either way:
browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
ctx = browser.contexts[0]
```

### Why `start_new_session=True` matters for `--keep-open`

`launch_chrome_for` previously launched Chrome as a regular subprocess
of the Python parent. When the Python script exited (cleanly or
otherwise), Chrome was reaped along with it — even if the script set
`proc = None` to "detach" before its `finally` block. The fix is
`subprocess.Popen(..., start_new_session=True)` which puts Chrome in
its own POSIX session, so it survives the Python exit. With this in
place, `--keep-open` actually keeps Chrome up for the next run to
attach to. Validated 2026-05-09 with the burner-create flow leaving
Chrome PID 46010 alive across three back-to-back runs that all
attached to it.

### Stash the CDP port for follow-up runs

`launch_chrome_for` callers should also write the CDP port to a
sidecar file (`<work_dir>/cdp_port.txt`) on `--keep-open` exit so the
attach-detector has a fast path even if `chrome.stderr` got rotated
out. The find-running probe checks both.

### Don't bridge cookies on attach

Re-bridging would overwrite the live session in Chrome-Debug. Skip
the bridge step entirely when attaching. If the user signed into a
new account in Chrome-Debug between runs, that session is preserved.

### Coexists with the user's regular Chrome

The user's daily Chrome lives in
`~/Library/Application Support/Google/Chrome/` — completely separate
`user-data-dir`. SingletonLock conflicts only happen between
processes sharing the same `user-data-dir`. Real Chrome and any
number of Chrome-Debugs coexist trivially; the only conflict is
**two Chrome-Debug processes on the same Profile** — which the
attach-mode pattern eliminates.

---

## Cookie harvest via CDP (avoids the locked-SQLite problem) — 2026-05-09

`bridge_cookies` (file-level copy) needs the source Chrome **stopped**
because Chrome holds an exclusive SQLite lock on `Cookies` while it
runs. That makes it useless for "carry the live signed-in session of
running Chrome A → fresh Chrome B" workflows — and the workaround of
terminating Chrome A defeats the user's parallel work.

The CDP `Storage.getCookies` API serves cookies straight from Chrome's
in-memory jar — no DB access, no lock contention. Then `add_cookies`
on the destination Chrome's CDP injects them. Whole round-trip is sub-
second for ~600 cookies. Validated 2026-05-09 in
`pipeline/cross_engage_burner_attached.py::harvest_cookies_via_cdp` to
clone PID 46010's signed-in YouTube session into a sibling Chrome
process without touching disk.

```python
def harvest_cookies_via_cdp(src_port: int) -> list[dict]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        src = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{src_port}")
        try:
            cookies = src.contexts[0].cookies()
        finally:
            try: src.close()  # disconnect only — does NOT terminate source Chrome
            except Exception: pass
    return list(cookies)

# Usage:
harvested = harvest_cookies_via_cdp(canonical_port)   # read-only; doesn't disturb user
proc, port = launch_chrome_for(profile, ..., user_data_dir=engage_udd)
with sync_playwright() as pw:
    dst = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    dst.contexts[0].add_cookies(harvested)            # session inherited
```

For workflows that want full isolation (separate process to avoid focus
stealing while user works in the source), this combines naturally with
a `cp -R Chrome-Debug/Profile\ X/ Chrome-Debug-engage/Profile\ X/`
clone for non-cookie profile metadata (avatar, channel registry,
settings) which Chrome doesn't lock.
