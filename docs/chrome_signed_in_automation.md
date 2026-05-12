# Driving a signed-in Chrome profile reliably with Playwright

Project-agnostic engineering reference. Write-once, copy into any project that
needs to script user-flows in a browser that is **already logged into**
real user accounts — Google, GitHub, internal SSO, social platforms — without
ever asking the user to sign in again.

The patterns here are validated on **macOS Sequoia + Chrome v147** (Apr 2026)
and **Linux + Chrome v147** (server-side); they cover both the laptop
single-developer case and the production case of a long-running automation
agent that survives reboots and coexists with the user's interactive Chrome.

> **Status (2026-05-13):** captured from a 6-month run of a YouTube
> cross-engagement system that drove ~50 signed-in Google profiles
> reliably. The system itself was retired (anti-spam considerations,
> see [Anti-automation observations](#anti-automation-observations));
> the techniques are kept here because they apply to any signed-in
> automation, not just YouTube.

---

## Table of contents

1. [TL;DR / when to use this](#tldr--when-to-use-this)
2. [Why the obvious approach fails](#why-the-obvious-approach-fails)
3. [Architecture](#architecture)
4. [Building block 1 — a dedicated user-data-dir](#building-block-1--a-dedicated-user-data-dir-uddebug)
5. [Building block 2 — bridging cookies from real Chrome](#building-block-2--bridging-cookies-from-real-chrome)
6. [Building block 3 — launching Chrome with CDP enabled](#building-block-3--launching-chrome-with-cdp-enabled)
7. [Building block 4 — Playwright connecting over CDP](#building-block-4--playwright-connecting-over-cdp)
8. [Coexistence with the user's real Chrome](#coexistence-with-the-users-real-chrome)
9. [Parallel runs — per-profile sibling user-data-dirs](#parallel-runs--per-profile-sibling-user-data-dirs)
10. [Headless mode considerations](#headless-mode-considerations)
11. [Long-running operation — macOS LaunchAgent pattern](#long-running-operation--macos-launchagent-pattern)
12. [Cloud / laptop split — running browser work from a serverless control plane](#cloud--laptop-split--running-browser-work-from-a-serverless-control-plane)
13. [Multi-identity flows — switching brand accounts inside one Google login](#multi-identity-flows--switching-brand-accounts-inside-one-google-login)
14. [Probing UI state robustly — `aria-pressed` over text](#probing-ui-state-robustly--aria-pressed-over-text)
15. [Anti-automation observations](#anti-automation-observations)
16. [Failure-mode table — symptoms → causes → fixes](#failure-mode-table--symptoms--causes--fixes)
17. [Reusable Python snippets](#reusable-python-snippets)
18. [Comparison with alternatives](#comparison-with-alternatives)
19. [References](#references)

---

## TL;DR / when to use this

You want to write a script that drives a Chrome window that is **already
logged in** as the user (or as a service account whose human signed in once)
and you want all of the following:

- No re-typing of passwords / no 2FA prompts on every run.
- No "is this you signing in?" emails to the user every time the script runs.
- The same Chrome session that loads `chrome://settings/people` and shows the
  user's avatar is the one your script drives.
- Compatible with the modern Chromium security policy that **forbids remote
  debugging against the default user-data-dir** (Chrome v136+).
- Survives the user double-clicking the Chrome dock icon while your script is
  running. Your automation does not steal their windows.
- Can run unattended on a launch-agent / cron schedule.

The core technique:

```
[user's real Chrome, Profile N]    ──cookies + Local State──▶    [a sibling Chrome
 (signed in, encrypted on-disk)                                    user-data-dir
                                                                   you control]
                                                                          │
                                                                          │ launch with
                                                                          │   --remote-debugging-port=0
                                                                          ▼
                                                              [Chrome process whose
                                                               actual CDP port is
                                                               written to its stderr]
                                                                          │
                                                                          │ http://127.0.0.1:<port>
                                                                          ▼
                                                              [Playwright
                                                               connect_over_cdp]
                                                                          │
                                                                          ▼
                                                              [your script drives
                                                               the page — fully
                                                               signed in]
```

Three reusable building blocks:

1. A **dedicated user-data-dir** that is NOT the default Chrome one (Chromium
   refuses CDP otherwise; see next section).
2. A **cookie / Local State bridge** from the user's real Chrome into the
   dedicated dir, idempotent, runs whenever cookies look stale.
3. A **CDP attach** from Playwright — `connect_over_cdp` against the port the
   freshly-launched Chrome printed to stderr.

If you need only one piece — the user is OK with a one-off `playwright open
https://accounts.google.com` to sign in once into the dedicated dir, and you
never need their real Chrome's session — you can skip the bridge entirely.
The bridge is the **harder** half; everything else is mechanical.

---

## Why the obvious approach fails

You'd think you could just point Chrome at the user's real profile and turn on
remote debugging:

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

This is a Chromium **security policy** added in v136 (PR
[`crrev/c/5719976`](https://chromium-review.googlesource.com/c/chromium/src/+/5719976)).
It refuses remote debugging on the default user-data-dir even when you pass
the flag explicitly — the check is path-based. There is **no flag bypass**;
every workaround uses a non-default user-data-dir.

The naive workaround — `chromium.launch_persistent_context(user_data_dir=…)`
on a fresh Playwright Chromium — fails for a different reason: **macOS cookie
encryption**. Chrome encrypts the cookie values stored in
`~/Library/Application Support/Google/Chrome/Profile N/Cookies` with a key
derived from a `Google Chrome Safe Storage` keychain entry whose ACL allows
ONLY the binary at `/Applications/Google Chrome.app/Contents/MacOS/Google
Chrome` to read it. A fresh Playwright-bundled Chromium has a different
binary fingerprint and so the keychain refuses the read; the cookies decrypt
to garbage and YouTube/Google bounce you to the sign-in page.

The combined fix: **launch the user's real Chrome binary** against a
**non-default user-data-dir** with `--remote-debugging-port=0`, then connect
Playwright over CDP. The real binary preserves the keychain ACL match; the
non-default UDD satisfies the v136 security policy.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ User's machine                                                         │
│                                                                        │
│  ┌──────────────────────────────┐    ┌────────────────────────────┐    │
│  │ Real Chrome                  │    │ Chrome-Debug               │    │
│  │ ── /Applications/Google      │    │ ── same binary             │    │
│  │    Chrome.app/…              │    │ ── --user-data-dir=        │    │
│  │ ── ~/Library/Application     │    │       ~/.../Chrome-Debug   │    │
│  │    Support/Google/Chrome     │    │ ── --remote-debugging-     │    │
│  │ ── interactive user          │    │       port=0               │    │
│  │    windows                   │    │ ── prints                  │    │
│  │ ── cookies encrypted with    │    │       ws://127.0.0.1:<N>   │    │
│  │    Keychain entry whose ACL  │    │       to stderr            │    │
│  │    allows ONLY this binary   │    │                            │    │
│  └──────────────┬───────────────┘    └────────────┬───────────────┘    │
│                 │                                 ▲                    │
│                 │  cookie bridge                  │  Playwright        │
│                 │  (file copy + Local State)      │  connect_over_cdp  │
│                 ▼                                 │                    │
│         ┌─────────────────┐              ┌────────┴─────────┐          │
│         │ bridge_cookies()│              │ Your automation  │          │
│         │ — selective     │              │ script           │          │
│         │   copy of:      │              │ — pip install    │          │
│         │   Cookies       │              │     playwright   │          │
│         │   Cookies-jrnl  │              │ — pulls page,    │          │
│         │   Login Data    │              │     clicks etc.  │          │
│         │   Network/Cookies│             └──────────────────┘          │
│         │   Local State   │                                            │
│         └─────────────────┘                                            │
└────────────────────────────────────────────────────────────────────────┘
```

The arrows are the only data flow. **Real Chrome and Chrome-Debug never share
a user-data-dir**, so they can run simultaneously without fighting over the
SingletonLock. The cookie bridge is one-directional (real → debug); your
script never writes back into the real Chrome's UDD.

---

## Building block 1 — a dedicated user-data-dir (UDD-Debug)

Pick a sibling path to the user's real Chrome user-data-dir. On macOS:

| OS              | Real Chrome UDD                                              | Suggested debug UDD                                              |
| --------------- | ------------------------------------------------------------ | ---------------------------------------------------------------- |
| macOS           | `~/Library/Application Support/Google/Chrome`                | `~/Library/Application Support/Google/Chrome-Debug`              |
| Linux (Wayland) | `~/.config/google-chrome`                                    | `~/.config/google-chrome-debug`                                  |
| Linux (X11)     | `~/.config/google-chrome`                                    | `~/.config/google-chrome-debug`                                  |
| Windows         | `%LOCALAPPDATA%\Google\Chrome\User Data`                     | `%LOCALAPPDATA%\Google\Chrome-Debug`                             |

Inside both UDDs, individual signed-in identities live as subdirectories named
`Profile 1`, `Profile 2`, … (or `Default` for the very first one). When you
launch Chrome you pass **both**:

- `--user-data-dir=<UDD-Debug>` — satisfies the v136 security policy.
- `--profile-directory="Profile 3"` — picks which signed-in identity inside
  the UDD to load.

The `Profile N` numbering is **per UDD**, not global. After you bridge
cookies for `Profile 3` from real Chrome, the debug UDD has its own
`Profile 3` folder containing the same cookie set; the numbers happen to
align because we copied them across, but Chrome doesn't care.

**Don't share the UDD-Debug with anything else** — not the user's other
automations, not VS Code's CDP debugger, nothing. Sharing creates lock
contention and obscure session corruption. One UDD = one running Chrome at a
time. (For parallelism see [§9](#parallel-runs--per-profile-sibling-user-data-dirs).)

---

## Building block 2 — bridging cookies from real Chrome

The bridge is selective — copy only the files Chrome needs to recognise the
session, not the whole UDD (which is gigabytes of cache, history, extension
state, etc.). The minimal set on macOS / Linux:

```python
COOKIE_FILES = (
    "Cookies", "Cookies-journal",
    "Login Data", "Login Data-journal",
    "Web Data", "Web Data-journal",
    "Preferences",
)
NETWORK_COOKIE_FILES = ("Cookies", "Cookies-journal")
```

Plus the **top-level `Local State` JSON** at the UDD root (NOT inside
`Profile N/`). This file holds the OS-level cookie-encryption key. If
`Local State` is out of sync between real Chrome and Chrome-Debug, every
cookie value decrypts to bytes that don't match what the server-side
session expects, and you get a "cookies stale, please sign in" loop.

```python
import pathlib

REAL_CHROME = pathlib.Path.home() / "Library/Application Support/Google/Chrome"
CHROME_DEBUG = pathlib.Path.home() / "Library/Application Support/Google/Chrome-Debug"

def bridge_cookies(profile: str, *, dst_dir: pathlib.Path | None = None) -> None:
    """Selective copy real Chrome → Chrome-Debug for one profile. Idempotent."""
    src = REAL_CHROME
    dst = dst_dir or CHROME_DEBUG
    src_p = src / profile          # e.g. ".../Chrome/Profile 3"
    dst_p = dst / profile          # e.g. ".../Chrome-Debug/Profile 3"
    if not src_p.exists():
        raise FileNotFoundError(f"Real Chrome {profile!r} not found at {src_p}")
    (dst_p / "Network").mkdir(parents=True, exist_ok=True)
    # Top-level Local State holds the os_crypt key — must be in sync.
    if (src / "Local State").exists():
        (dst / "Local State").write_bytes((src / "Local State").read_bytes())
    for f in COOKIE_FILES:
        s = src_p / f
        if s.exists():
            (dst_p / f).write_bytes(s.read_bytes())
    for f in NETWORK_COOKIE_FILES:
        s = src_p / "Network" / f
        if s.exists():
            (dst_p / "Network" / f).write_bytes(s.read_bytes())
```

### Critical rule — never bridge while real Chrome is open

Chrome holds an **exclusive SQLite lock** on `Cookies` and `Cookies-journal`
while it's running. Copying the file mid-write produces a half-written
DB — your debug UDD reads it and crashes on first request, OR (worse) reads
inconsistent bytes that decrypt to empty cookie values and you silently sign
out.

Always check first:

```python
import subprocess

def real_chrome_is_running() -> list[str]:
    out = subprocess.run(
        ["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
        capture_output=True, text=True,
    )
    return [p for p in out.stdout.split() if p.strip()]

if real_chrome_is_running():
    # Skip bridge — Chrome-Debug already has SOME cookies from a prior
    # bridge. They may be stale; if so the script will surface
    # "redirected to sign-in" and the user closes Chrome and retries.
    pass
else:
    bridge_cookies(profile)
```

The "skip bridge if Chrome is up" branch is **the** observed pattern in
production. Asking the user to "please close Chrome before this script runs"
loses every time; degrading gracefully wins.

### Why bytes-copy and not `shutil.copy`

The `Cookies` files are SQLite databases with WAL mode. `shutil.copy` and
`cp` work, but `Path.write_bytes(Path.read_bytes())` is atomic on most
filesystems (one `write(2)` syscall), simpler to reason about across OSes,
and avoids the "permissions copied across" surprises that `shutil.copy2`
introduces. Pick one and stick with it.

### Bridge timing

Bridge **once at the start** of each automation run, then NEVER again during
the run. Re-bridging mid-run while Chrome-Debug holds open file handles on
those SQLite files corrupts them in flight. If a session goes stale during a
multi-hour run, kill Chrome-Debug, re-bridge, re-launch — don't try to bridge
in place.

---

## Building block 3 — launching Chrome with CDP enabled

The launch command:

```python
import re, subprocess, time, urllib.request, json

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

def launch_chrome_for(
    profile: str,
    *,
    work_dir: pathlib.Path,
    user_data_dir: pathlib.Path | None = None,
    headless: bool = False,
) -> tuple[subprocess.Popen, str]:
    """Launch Chrome for `profile`, return (proc, cdp_port).
    Caller must terminate proc when done."""
    udd = user_data_dir or CHROME_DEBUG
    work_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = work_dir / "chrome.stderr"
    stderr_path.write_text("")
    args = [
        CHROME_BIN,
        # --remote-debugging-port=0 → kernel picks a free port; Chrome
        # writes the chosen port to stderr. Hardcoding 9222 is the common
        # bug source — two Chromes can't share the same port and you'll
        # get connect_over_cdp confusion.
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={udd}",
        f"--profile-directory={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=AutomationControlled",
        "--noerrdialogs",
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        # Mute every tab globally — heavy if your script opens many
        # auto-playing video tabs. YouTube still counts views from muted
        # plays, so it doesn't change application behaviour.
        "--mute-audio",
    ]
    if headless:
        args += [
            # Modern headless (Chrome 109+). Renders the same DOM as
            # windowed mode — the legacy --headless lite-rendering would
            # break selectors that depend on viewport size.
            "--headless=new",
            "--window-size=1366,900",
        ]
    args.append("about:blank")  # initial URL — without this Chrome opens
                                 # the default new-tab page which reads
                                 # network and slows startup
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w"),
        # New POSIX session so Chrome survives the Python parent's exit.
        # Required if your script does `exec` / `os._exit` and you want
        # the user to keep clicking around in the Chrome it spawned.
        start_new_session=True,
    )
    cdp_port = _wait_for_cdp_port(proc, stderr_path)
    return proc, cdp_port


def _wait_for_cdp_port(
    proc: subprocess.Popen, stderr_path: pathlib.Path,
) -> str:
    """Wait for Chrome to print 'DevTools listening on ws://127.0.0.1:<N>'.

    The stderr-text scrape is the ONLY method guaranteed to return THIS
    chrome process' port. lsof on the proc.pid CAN return a SIBLING
    chrome's listener on multi-Chrome laptops (Chrome.app on macOS
    appears to leak file descriptors across sibling browser PIDs in
    boot races) — validated bug, surfaced as Playwright hanging 180s
    on a busy sibling's CDP target list before timing out.

    Strategy:
      1. Wait UP TO 30s for stderr scrape (chrome usually prints within
         1-3s; 30s covers ridiculously loaded laptops).
      2. Only fall back to lsof if stderr is STILL empty after that
         deadline. Each lsof candidate is verified by HTTP-GET'ing
         /json/version and checking that "Browser" starts with "Chrome/"
         (excludes node.js / Electron CDP listeners which serve the same
         endpoint with a different Browser string).
    """
    cdp_port = None
    deadline = time.time() + 30.0
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            stderr_text = stderr_path.read_text()
        except OSError:
            stderr_text = ""
        m = re.search(r"ws://127\.0\.0\.1:(\d+)", stderr_text)
        if m:
            cdp_port = m.group(1)
            break
    if not cdp_port:
        # Last-ditch lsof fallback — only fires if stderr is empty
        # (e.g. launchd lost it). Verify candidates via /json/version.
        for _ in range(10):
            time.sleep(0.3)
            try:
                ls = subprocess.run(
                    ["lsof", "-p", str(proc.pid),
                     "-iTCP", "-sTCP:LISTEN", "-n"],
                    capture_output=True, text=True, timeout=2,
                )
                for line in ls.stdout.splitlines():
                    if "LISTEN" not in line or "127.0.0.1" not in line:
                        continue
                    pm = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if not pm:
                        continue
                    candidate = pm.group(1)
                    if _cdp_alive(int(candidate)):
                        cdp_port = candidate
                        break
                if cdp_port:
                    break
            except Exception:
                pass
    if not cdp_port:
        proc.kill()
        raise RuntimeError(
            f"no CDP port discovered\n{stderr_path.read_text()}")
    # Final safety: even when stderr scrape wins, double-check the
    # port is alive. A printed-but-not-yet-listening port is a known
    # Chrome startup race.
    deadline = time.time() + 8.0
    while time.time() < deadline and not _cdp_alive(int(cdp_port)):
        time.sleep(0.2)
    if not _cdp_alive(int(cdp_port)):
        proc.kill()
        raise RuntimeError(
            f"chrome announced port {cdp_port} but /json/version "
            f"never responded; stderr:\n{stderr_path.read_text()}")
    return cdp_port


def _cdp_alive(port: int, *, timeout: float = 2.0) -> bool:
    """True iff /json/version on `port` responds AND is served by Chrome
    (not a node.js / Electron V8 inspector — they answer the same
    endpoint with `Browser: node.js/v…`)."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout,
        ) as r:
            if r.status != 200:
                return False
            payload = json.loads(r.read())
    except Exception:
        return False
    browser = str(payload.get("Browser", ""))
    return browser.startswith("Chrome/") or browser.startswith("Edge/")
```

### Why `--remote-debugging-port=0` and not `9222`

Hardcoded ports are the single largest source of "two scripts conflicting"
bugs. With `--remote-debugging-port=0` the kernel allocates a free port and
Chrome prints it; you read the port back from stderr. Even if the user has
nine other CDP-using tools (VS Code debugger, Postman browser, etc.),
nothing collides.

### Why the `_cdp_alive` Browser check

Several developer tools speak CDP on localhost — node.js / Electron
debuggers (VS Code, Slack, Discord, …) all expose `/json/version`. They
respond with `"Browser": "node.js/v…"` instead of `"Browser": "Chrome/…"`.
Without this filter, your script will sometimes connect to the user's VS
Code debugger by accident and fail with an inscrutable `Invalid URL:
undefined` from Playwright deep inside the connect path.

### Why `start_new_session=True`

Without it, sending `SIGTERM` to your Python script propagates to Chrome
through the controlling-terminal group and kills the browser too — even if
you wanted to leave it running so the user can keep interacting with it.
With `start_new_session=True`, Chrome lives in its own POSIX session and
ignores TTY signals; you have to `proc.terminate()` explicitly.

### Singleton lock cleanup

Sometimes Chrome was killed without cleaning its `SingletonLock` /
`SingletonSocket` / `SingletonCookie` files in the UDD root. The next
launch hangs forever waiting for "the other Chrome" to release the lock.
Defensive cleanup before every launch is cheap:

```python
def _clear_singleton(udd: pathlib.Path) -> None:
    for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (udd / f).unlink(missing_ok=True)
```

Call this **before** `launch_chrome_for`. It does NOT cause data loss — the
files are stale lock pointers, not actual user data.

---

## Building block 4 — Playwright connecting over CDP

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    # Critical — DO NOT call browser.new_context().
    # connect_over_cdp returns a browser object whose contexts[0] is the
    # ALREADY-SIGNED-IN context that Chrome launched with.
    # browser.new_context() creates a fresh blank context (no cookies);
    # any work done in it requires re-signing in.
    ctx = browser.contexts[0]

    page = ctx.new_page()
    page.set_default_timeout(15000)  # YouTube SPA hydration is slow;
                                     # 5s default is too aggressive
    page.goto("https://www.youtube.com/", wait_until="domcontentloaded")
    # … your automation here …
    page.close()
```

The single most-common bug here is calling `browser.new_context()` instead of
`browser.contexts[0]`. The former gives you a brand-new context that is NOT
signed in (it has no cookies), and you spend an hour debugging why "the
script clicks the button but nothing happens" — the button is the sign-in
button, not the action button.

### Async vs sync Playwright

`sync_playwright()` is fine for sequential single-browser automation.
`async_playwright()` matters if you're driving 5+ browsers concurrently in
one process — but you usually want **one process per browser** (or one
`subprocess.Popen` per UDD) anyway because Chrome itself isn't async-friendly
when you start asking it to navigate a dozen tabs concurrently. The async
API saves you a thread, not the underlying CPU.

### Don't trust `wait_until="load"`

Modern SPAs (YouTube, Google Docs, anything React-based) emit `load` in a
few hundred ms but the actual hydrated React tree appears 5-15 seconds
later. The selectors you care about are not in the DOM at `load` time. The
options that actually work:

- `wait_until="domcontentloaded"` for navigation, then
- `page.wait_for_selector(<the button you'll click>, state="attached", timeout=12000)` — gates on the specific lane being present. Best signal-to-noise.
- `wait_until="networkidle"` — the silver bullet that doesn't work; YouTube
  has long-poll websockets that NEVER go idle. Use sparingly and with a
  short timeout; treat the timeout itself as a signal that you should
  proceed with a `wait_for_selector`.

### Don't trust `wait_until="commit"` either

`commit` returns as soon as the navigation commits (a few hundred ms) — it
DOES return faster than `domcontentloaded`, but you then need to manually
gate on a selector. It's a useful primitive for "open many tabs in
parallel" patterns where you can afford to come back to each tab later;
it's a footgun if you immediately try to read state.

---

## Coexistence with the user's real Chrome

The whole point of the dedicated UDD is so your script never disrupts the
user. Three concrete rules:

1. **Never `assert_chrome_closed()`.** Some older docs (this codebase
   included, before 2026-05-12) had a defensive check that raised if real
   Chrome was running. The Right Move is to skip the bridge and proceed —
   if cookies are stale, the script will gracefully report "redirected to
   sign-in", the user closes Chrome and retries. The hard-fail variant
   trains the user to swat the script away.
2. **Different UDD = different process tree = different SingletonLock.**
   Real Chrome's `~/Library/.../Chrome/SingletonLock` and Chrome-Debug's
   `.../Chrome-Debug/SingletonLock` are independent. They can run side by
   side forever. The user can launch a fresh real Chrome window mid-run
   and your script doesn't notice.
3. **Don't share the binary's stderr/stdout.** When you `subprocess.Popen`
   Chrome, redirect stdout/stderr to a file inside `work_dir`. If the
   parent Python is itself running under `launchd` or a TTY-less context,
   inheriting Chrome's stderr can cause early-shutdown-on-broken-pipe
   issues.

### `--keep-open` flows

If your script needs to leave the user IN the Chrome window after exiting
(e.g. "automation finished — review the result and click Submit"), set
`start_new_session=True` and DO NOT terminate the proc. You can also pass
`--no-startup-window` and let Playwright's first navigation be the window
the user sees.

---

## Parallel runs — per-profile sibling user-data-dirs

One UDD = one Chrome at a time. To run, say, three signed-in profiles in
parallel, you need **three separate UDDs**, each bridged separately, each
launched with its own port:

```
~/Library/Application Support/Google/Chrome              <- real Chrome
~/Library/Application Support/Google/Chrome-Debug        <- single instance
~/Library/Application Support/Google/Chrome-Debug-1      <- parallel #1
~/Library/Application Support/Google/Chrome-Debug-2      <- parallel #2
~/Library/Application Support/Google/Chrome-Debug-3      <- parallel #3
```

Each gets its own:

- Cookie bridge from real Chrome (the source `Profile N` is the same; the
  destination differs per UDD).
- `SingletonLock` cleanup (in its own dir).
- Chrome process with its own `--remote-debugging-port=0`.
- Playwright `connect_over_cdp` to its own port.

The implementation:

```python
def launch_for_parallel_run(profile: str, slot: int, **kw):
    udd = pathlib.Path.home() / "Library/Application Support/Google" / f"Chrome-Debug-{slot}"
    bridge_cookies(profile, dst_dir=udd)
    _clear_singleton(udd)
    return launch_chrome_for(profile, user_data_dir=udd, **kw)
```

**Don't** try `--user-data-dir=…/Chrome-Debug --profile-directory="Profile 3"`
twice in parallel. Even though it's "the same profile", Chrome serialises on
the per-UDD lock and the second launch hangs.

### What's the practical parallelism limit?

CPU-bound. Each Chrome with a few open YouTube tabs can take 1-2 cores at
peak. On a 12-core M-series Mac, ~5 parallel Chromes is comfortable; 10 is
the upper bound before everything starts oscillating between idle and
unresponsive. Tune your worker pool to that.

---

## Headless mode considerations

Use `--headless=new` (Chrome 109+), NOT the legacy `--headless`. The legacy
mode renders with a stripped layout engine that breaks anything depending on
viewport-width selectors (almost every modern site). The new mode renders
the same DOM as windowed mode.

Pin a desktop viewport size:

```bash
--headless=new --window-size=1366,900
```

Without `--window-size`, the SPA picks a mobile or tablet layout based on
default viewport heuristics, and your selectors silently miss. 1366×900 is a
safe "looks-like-laptop" choice that matches the dimensions sites design
their desktop SPA against.

### Headless quirks vs windowed

YouTube specifically:

- `studio.youtube.com` redirects to a brand's `/channel/<UC>` page in
  windowed mode. In headless it sometimes loads `studio.youtube.com` as a
  blank SPA shell forever. Workaround: probe `www.youtube.com/account`
  instead — its HTML always contains `/channel/UC…` links pointing to the
  active brand, so a regex can extract it without depending on SPA
  redirect behaviour.
- The `Continue` button on Studio's first-launch modal needs to be
  dismissed even in headless (it shows up but doesn't have user input to
  proceed); selector
  `ytcp-button:has-text('Continue'), tp-yt-paper-button:has-text('Continue')`.

When in doubt, develop in windowed mode (you can SEE what Chrome sees), then
flip to headless once everything works.

---

## Long-running operation — macOS LaunchAgent pattern

For a script that needs to run forever (long-poll for work, restart on
crash, survive reboots), the LaunchAgent pattern:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
                       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yourorg.your-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>cd /Users/you/yourproject &amp;&amp; PYTHONPATH=. /Users/you/yourproject/.venv/bin/python -m your_module</string>
    </array>
    <!-- Run on login -->
    <key>RunAtLoad</key>
    <true/>
    <!-- Restart on crash, but throttle so a tight crash loop
         doesn't burn the laptop -->
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/tmp/your-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/your-agent.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/you</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

Drop into `~/Library/LaunchAgents/com.yourorg.your-agent.plist` and:

```bash
U=$(id -u)
launchctl enable    gui/$U/com.yourorg.your-agent
launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.yourorg.your-agent.plist
launchctl kickstart -k gui/$U/com.yourorg.your-agent
```

Verify it's up:

```bash
launchctl print gui/$(id -u)/com.yourorg.your-agent | grep -E 'state|pid ='
# expect: state = running, pid = N
```

### `enable + bootstrap + kickstart` vs the legacy `load`

Modern macOS (Sonoma+, 2023-onwards) handles agent lifecycle via the
domain-based subcommands. The legacy pair `launchctl load
~/Library/LaunchAgents/foo.plist` and `unload` was deprecated; in some
states (a previously-`bootout`'d agent), `load` is a silent no-op.

Use the modern triple:

```bash
launchctl enable    gui/$U/com.yourorg.your-agent     # remove from disabled list
launchctl bootstrap gui/$U <plist-path>               # register with launchd
launchctl kickstart -k gui/$U/com.yourorg.your-agent  # force restart now
```

To stop persistently:

```bash
launchctl bootout  gui/$U/com.yourorg.your-agent      # remove from launchd
launchctl disable  gui/$U/com.yourorg.your-agent      # stay disabled across logins
```

The order matters — `bootout` without `disable` will re-load on next login;
`disable` alone leaves the running process up.

### Why `ThrottleInterval`

Without it, a script that crashes immediately on launch becomes a tight
loop — launchd respawns it 10× per second, eats 100% of one core, fills your
log file, drains battery, and the user has no idea why their MacBook fan is
on. `ThrottleInterval=10` caps the respawn rate to once per 10 seconds.

### A note on macOS Background Task Management

Since Ventura (2022) the system surfaces "<app> is running in the background"
notifications for any LaunchAgent registered AFTER its first install. The
user has to approve it once via System Settings → Login Items. If your agent
isn't running and you can't figure out why, check there first.

---

## Cloud / laptop split — running browser work from a serverless control plane

You often have a control plane on Cloud Run / Lambda / similar and the
**actual browser work has to happen on the laptop** — Cloud Run can't run
Chrome reliably (no display server, the bundled Chromium does work but
requires the bridge here, which means the user's encrypted cookies aren't
available in the cloud anyway).

The pattern:

```
[Cloud control plane]                    [Laptop agent]
                                              │ poll
        ┌─────────────────────┐               │
        │ POST /api/<thing>   │ ──┐           │
        │                     │   │ enqueue   │
        └─────────────────────┘   │ task      │
                                  ▼           │
                          ┌──────────────┐    │
                          │ task queue   │    │
                          │ (Firestore / │    │
                          │  SQS / etc.) │    │
                          └──────┬───────┘    │
                                 │            │
                                 │  long-poll │
                                 │  /lease    ▼
                                 ◀────  ┌─────────────────┐
                                  task  │ poll loop       │
                                 ────▶  │ — claim task    │
                                  ack   │ — drive Chrome  │
                                 ◀────  │   (this doc)    │
                                        │ — POST result   │
                                        └─────────────────┘
```

Key decisions:

- **Long-poll, not webhook.** Webhooks require the laptop to be
  externally-addressable. Long-polling against an authenticated cloud
  endpoint is firewall-friendly (the laptop initiates the connection). Use
  a 30-65 second poll deadline; the cloud holds the request open until
  there's work or the deadline expires.
- **Idempotent task definitions.** The laptop may crash mid-task; the
  cloud will re-lease. Make your tasks safe to re-run. (For browser
  automation that means: probe state first, only act if the state
  doesn't already match the goal.)
- **Per-kind capacity caps.** Some browser flows are cheap (visit one
  page, click one button); others are expensive (open 50 tabs, watch for
  10 minutes). Cap concurrency per kind so a flood of one type doesn't
  starve the other.
- **Authentication via OIDC.** If your cloud is Cloud Run + IAM, the
  laptop can authenticate by minting an ID token from `gcloud auth
  print-identity-token` and sending it as `Authorization: Bearer <jwt>`.
  Tokens last ~1 hour; cache them in-process and refresh.

Reference implementation skeleton:

```python
import json, os, socket, subprocess, threading, time, urllib.error, urllib.request

CONTROL_URL = os.environ["CONTROL_URL"]
AGENT_ID = os.environ.get("AGENT_ID") or f"laptop-{socket.gethostname()}"
CAPS = ["my_browser_task"]
LEASE_TTL_S = 600

def _id_token() -> str:
    return subprocess.check_output(
        ["gcloud", "auth", "print-identity-token"], text=True, timeout=15,
    ).strip()

def _post(path: str, body: dict, *, timeout: float = 65) -> dict:
    req = urllib.request.Request(
        CONTROL_URL.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_id_token()}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def claim_task() -> dict | None:
    """Long-poll for one matching task. Returns None on timeout."""
    try:
        return _post("/agent/lease", {
            "agent_id": AGENT_ID, "caps": CAPS, "lease_ttl_s": LEASE_TTL_S,
        }, timeout=65) or None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            time.sleep(60); return None
        raise

def execute(task: dict) -> dict:
    # YOUR browser-automation code here. Should be defensive — the cloud
    # may have already lost interest in this task (lease expired) by the
    # time you finish.
    return {"status": "ok", "result": "..."}

def ack(task_id: str, result: dict) -> None:
    _post(f"/agent/ack/{task_id}", result, timeout=20)

def main_loop() -> None:
    while True:
        task = claim_task()
        if task is None:
            continue
        try:
            result = execute(task)
            ack(task["task_id"], {"status": "completed", **result})
        except Exception as e:
            ack(task["task_id"], {"status": "error", "error": str(e)})
```

### Cloud-side state for laptop-side work

If the user wants to see live status of the laptop's work in a cloud
dashboard, push state from the laptop **to** cloud storage (GCS / S3 /
similar) at every meaningful step. Read-side (the dashboard) reads from
cloud storage and never tries to talk directly to the laptop. This survives
laptop sleep/wake, IP changes, NAT, everything.

---

## Multi-identity flows — switching brand accounts inside one Google login

A single Google account can host multiple "brand accounts" / YouTube
channels. The signed-in cookie selects ONE active brand at a time; other
brands need an explicit switch. The reliable switch path on YouTube as of
2026-05:

1. Read the current active brand:
   ```python
   page.goto("https://www.youtube.com/account",
             wait_until="domcontentloaded", timeout=30000)
   import re
   ucs = re.findall(r"/channel/(UC[A-Za-z0-9_-]{20,})", page.content())
   from collections import Counter
   active_uc = Counter(ucs).most_common(1)[0][0] if ucs else None
   ```
   Why `/account`: its HTML reliably contains the active brand's UC; works
   in both windowed and headless. `studio.youtube.com` (also valid) is
   flakier in headless.

2. If `active_uc != target_uc`, navigate to the **direct switcher URL**:
   ```python
   page.goto("https://www.youtube.com/channel_switcher",
             wait_until="domcontentloaded", timeout=30000)
   page.locator(f"a[href*='/channel/{target_uc}']").first.click(timeout=5000)
   ```
   The avatar-dropdown chain (avatar → "Switch account" → submenu row) is
   the alternative documented path; it is *much* less reliable under load.
   Direct-URL works first try in 95%+ of cases.

3. Verify by re-running step 1 in a **fresh page** (the SPA caches the old
   brand context per-tab). Don't just check
   `page.url` — that doesn't reflect the active brand.

### Why brand switching matters

Many YouTube actions (Like, Subscribe, Comment) attribute to the **currently
active brand**. If your script clicks Subscribe on a video while the
personal-Google-account brand is active instead of the brand account you
wanted, the subscription lands on the wrong identity. UI gives no warning.
**Always verify the active brand before any state-mutating action.** Pay the
~1-2 second per-action cost; the alternative is hours of debugging "why
isn't the right account showing up as subscriber".

---

## Probing UI state robustly — `aria-pressed` over text

The single biggest selector pitfall in YouTube automation:

```
button[aria-label*='like this video' i]
```

This matches BOTH the Like button (`aria-label="like this video along
with N other people"`) AND the Dislike button (`aria-label="Dislike this
video"`) — because "like this video" is a substring of "Dislike this video".
On the regular `/watch` page DOM order made `.first` return the Like
button; on `/shorts` the order is reversed and `.first` returns Dislike.
Result: a script that "subscribes to channels" actually clicks DISLIKE on
every Short.

The fix is to lead with a **structurally-unique** selector and add
defence-in-depth `:not()` clauses:

```python
LIKE_SELECTORS = (
    # Universal — works on /watch AND /shorts. The :not() excludes
    # the sibling dislike-button-view-model's child button.
    "like-button-view-model button:not(dislike-button-view-model button)",
    # Legacy fallbacks with explicit Dislike guards.
    "button[aria-label*='like this video' i][aria-pressed]:not([aria-label*='dislike' i])",
    "ytd-toggle-button-renderer #like-button button:not([aria-label*='dislike' i])",
)
```

And probe state via `aria-pressed` (a true boolean attribute YouTube sets
when the user has liked) rather than text content (which is "Like" /
"Liked" / "1.2K likes" and varies by surface):

```python
def probe_like(page) -> tuple[str, object | None]:
    for sel in LIKE_SELECTORS:
        try:
            btn = page.locator(sel).first
            pressed = btn.get_attribute("aria-pressed", timeout=2000)
            if pressed == "true":  return "liked", btn
            if pressed == "false": return "unliked", btn
        except Exception:
            continue
    return "unknown", None
```

Same pattern for Subscribe (use button text + aria-label combined, since
the subscribe button doesn't expose aria-pressed):

```python
SUBSCRIBE_SELECTORS = (
    "ytd-subscribe-button-renderer button",
    "yt-subscribe-button-view-model button",
    "button[aria-label*='Subscribe to' i]",
    "ytd-watch-metadata #subscribe-button-shape button",
)

def probe_subscribe(page) -> tuple[str, object | None]:
    for sel in SUBSCRIBE_SELECTORS:
        try:
            btn = page.locator(sel).first
            txt = (btn.text_content(timeout=2000) or "").strip().lower()
            label = (btn.get_attribute("aria-label", timeout=1000) or "").lower()
            combined = f"{txt} {label}"
            if "subscribed" in combined or "unsubscribe" in combined:
                return "subscribed", btn
            if "subscribe" in combined:
                return "unsubscribed", btn
        except Exception:
            continue
    return "unknown", None
```

### `dispatch_event("click")` vs `click()`

YouTube's like / subscribe buttons sometimes fail Playwright's actionability
check ("element is outside viewport" / "element is hidden by overlay") even
though they're visible to the user. The pragmatic remedy:

- For Like: skip the actionability check entirely:
  ```python
  btn.dispatch_event("click", timeout=8000)
  ```
- For Subscribe: scroll into view, then force-click:
  ```python
  btn.scroll_into_view_if_needed(timeout=3000)
  btn.click(force=True, timeout=6000)
  ```

`force=True` skips actionability but still uses the real input pipeline
(better cross-platform behaviour). `dispatch_event("click")` skips the
input pipeline entirely and just fires the JavaScript event — necessary
when the button is rendered behind a theatre-mode overlay.

### Verify after click

Always re-probe AFTER the click + a small dwell (~1.5-2.5s). YouTube's
state mutation lags the click; a probe immediately after returns the OLD
state and you'd think the click failed.

```python
btn.click(force=True, timeout=6000)
time.sleep(2.5)  # let the server commit
state2, _ = probe_subscribe(page)
if state2 != "subscribed":
    # retry / log error / surface to user
    ...
```

---

## Anti-automation observations

What we observed driving ~50 signed-in profiles for several months:

- **Action bursts attract scrutiny.** 6 subscribe clicks in 30 seconds from
  one IP across 6 brand accounts on the same Google login looks like a
  spam pattern. Slow bursts down to 2-5 seconds between clicks.
- **Account age + activity matter.** A brand account that's 4 days old,
  has 0 uploaded videos, 0 subscribers itself, and the default profile
  picture is invisible to YouTube's spam filter — its sub clicks register
  in the burner's own subscription list, but **don't tally in the target
  channel's public subscriber count** (subscribers from accounts the
  filter classifies as inauthentic are excluded; see [YouTube Help —
  Subscriber count](https://support.google.com/youtube/answer/3045552)).
- **One Google login hosting 40+ brand accounts is a strong signal.**
  Brand-account fan-out happens, but 40+ is well outside normal user
  behaviour. A single visit to channel-switcher returning 40 brand options
  is a red flag in itself.
- **Same IP for everything compounds the above.** A single MacBook
  subscribing from 49 brand accounts within 5 minutes is much more
  automation-shaped than the same activity spread across 49 different
  IPs / devices over a week.
- **`--disable-blink-features=AutomationControlled` helps but isn't
  magic.** It drops the `navigator.webdriver` signal but plenty of other
  fingerprinting heuristics (timing of mouse moves, lack of mouse moves,
  battery / vendor strings) remain. If you're going up against a serious
  anti-automation system, the browser flag is a small piece.

The neutral observation, applicable to any signed-in automation: **if the
account looks like a person it'll be treated like a person; if it looks
like a script it'll be treated like a script regardless of how
sophisticated the automation is**. Spend your engineering effort on making
the accounts look like people (profile pic, handle, a few real-looking
posts/uploads, a slow ramp of activity) rather than on making the script
look more like a person.

---

## Failure-mode table — symptoms → causes → fixes

| Symptom                                                                                            | Cause                                                                                                                                       | Fix                                                                                                                                                                                                                                              |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Chrome stderr `DevTools remote debugging requires a non-default data directory.`                   | You passed the user's real Chrome UDD with `--remote-debugging-port`.                                                                       | Use a sibling UDD (e.g. `Chrome-Debug`); see §1. There is no flag bypass.                                                                                                                                                                        |
| `connect_over_cdp` hangs 180s then times out.                                                      | (a) Wrong port — lsof returned a sibling Chrome's port. (b) Connecting to a node.js / Electron CDP listener served by VS Code etc.          | Verify Chrome's stderr scrape. Add the `Browser: Chrome/…` filter on `/json/version` to skip non-Chrome CDP servers.                                                                                                                              |
| Page loads but the user is on the sign-in screen.                                                  | Cookies stale OR the bridge ran while real Chrome was open and copied a half-written `Cookies` SQLite.                                      | Close real Chrome; re-bridge cookies; relaunch Chrome-Debug. If frequent: have the bridge SKIP itself when real Chrome is up; the script then reports the "redirected to sign-in" state and the user closes Chrome and retries.                  |
| `playwright.launch_persistent_context(<real UDD>)` runs but the page acts signed-out.              | The fresh Playwright Chromium binary can't read macOS Keychain-encrypted cookies (binary-fingerprint ACL mismatch).                         | Use the user's real Chrome binary (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`) with `--user-data-dir=…/Chrome-Debug`. Don't use Playwright's bundled Chromium for signed-in flows.                                            |
| Two parallel scripts conflict / hang on launch.                                                    | Both pointing at the same UDD; Chrome's SingletonLock serialises them.                                                                      | Per-script sibling UDDs (`Chrome-Debug-1`, `-2`, …). Each gets its own bridge + launch + port.                                                                                                                                                   |
| Like click hits Dislike on Shorts.                                                                 | Selector `aria-label*='like this video' i` matches Dislike too; `.first` picks Dislike on Shorts DOM order.                                 | Use the structural selector `like-button-view-model button:not(dislike-button-view-model button)` and add `:not([aria-label*='dislike' i])` defence to fallbacks.                                                                                |
| Subscribe button clicked but state didn't change on re-probe.                                      | (a) Wrong active brand. (b) YouTube's optimistic UI rendered "Subscribed" but server rejected. (c) Tab navigated away before commit.        | (a) Verify active brand BEFORE every action. (b) Sleep 2.5s after click before re-probe. (c) Don't navigate away until you've verified.                                                                                                          |
| Headless mode shows blank page on `studio.youtube.com`.                                            | Studio's SPA fails to initialise in headless without user input.                                                                            | Probe `www.youtube.com/account` instead — extracts active brand UC from the page HTML directly via regex, no SPA dependency.                                                                                                                     |
| Long-running launchd agent silently stopped working.                                               | (a) Agent was `bootout`'d but `disable` flag wasn't cleared. (b) macOS Background Task Management revoked permission.                       | `launchctl print gui/$UID/<label>` shows current state. If "could not find service": run the `enable + bootstrap + kickstart` triple. If approved-but-not-running: check log files for repeated crashes; raise `ThrottleInterval`.                |
| `cloud → laptop` queue accumulates tasks that never run.                                           | Laptop agent died and didn't restart (e.g. `disabled` in launchd). Queue keeps accepting work; nothing leases.                              | One-line health probe: `launchctl print gui/$UID/<label> 2>&1 \| head -3`. If output is `Could not find service` or `state = disabled`, run the enable+bootstrap+kickstart triple. Add to your dashboard's "is the laptop agent up" indicator.   |
| Chrome-Debug starts opening hundreds of tabs / heavy CPU.                                          | Your script forgot to close pages, OR a runaway loop kept calling `ctx.new_page()`.                                                         | Cap pages per ctx in your script. Defensively call `page.close()` in `finally` blocks. Set a per-task `MAX_OPEN_TABS` and bail if exceeded.                                                                                                      |
| Cookies bridge runs fine but Login Data prompts user to import passwords on next real Chrome open. | You bridged `Login Data` over the user's existing one without reconciling its `webdata` schema — Chrome detects mismatch on next start.     | Either don't bridge `Login Data` (most automations don't need it; cookies + Local State are enough for most session work), OR bridge ONE-WAY into Chrome-Debug and never the reverse direction.                                                  |
| `aria-label` text-based selectors break after a YouTube redesign.                                  | YouTube reshuffles polymer components every few months. Text-based label selectors are fragile to language localisation too.                | Lead with structural selectors (`view-model` element names, ID attributes); fall back to label-text only for back-compat. Run a simple e2e check after any selector change to catch regressions early.                                            |

---

## Reusable Python snippets

Self-contained, drop-in. Combine and adapt for your own project.

### Snippet 1 — bridge + launch + connect end-to-end

```python
"""signed_in_chrome.py — drive a signed-in Chrome profile via Playwright CDP.

Usage:
    from signed_in_chrome import attach_signed_in
    with attach_signed_in("Profile 3", work_dir=pathlib.Path("/tmp/work")) as page:
        page.goto("https://gmail.com")
        # ... do stuff ...
"""
from __future__ import annotations
import contextlib, json, pathlib, re, subprocess, time, urllib.request

HOME = pathlib.Path.home()
REAL_CHROME = HOME / "Library/Application Support/Google/Chrome"
CHROME_DEBUG = HOME / "Library/Application Support/Google/Chrome-Debug"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

COOKIE_FILES = (
    "Cookies", "Cookies-journal", "Login Data", "Login Data-journal",
    "Web Data", "Web Data-journal", "Preferences",
)

def real_chrome_running() -> bool:
    out = subprocess.run(
        ["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
        capture_output=True, text=True,
    )
    return bool([p for p in out.stdout.split() if p.strip()])

def bridge_cookies(profile: str, *, dst: pathlib.Path = CHROME_DEBUG) -> None:
    src = REAL_CHROME
    src_p = src / profile
    dst_p = dst / profile
    if not src_p.exists():
        raise FileNotFoundError(f"profile {profile!r} not in real Chrome")
    (dst_p / "Network").mkdir(parents=True, exist_ok=True)
    if (src / "Local State").exists():
        (dst / "Local State").write_bytes((src / "Local State").read_bytes())
    for f in COOKIE_FILES:
        s = src_p / f
        if s.exists():
            (dst_p / f).write_bytes(s.read_bytes())
    for f in ("Cookies", "Cookies-journal"):
        s = src_p / "Network" / f
        if s.exists():
            (dst_p / "Network" / f).write_bytes(s.read_bytes())

def _clear_singleton(udd: pathlib.Path) -> None:
    for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (udd / f).unlink(missing_ok=True)

def _cdp_alive(port: int, *, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout,
        ) as r:
            data = json.loads(r.read())
    except Exception:
        return False
    return str(data.get("Browser", "")).startswith(("Chrome/", "Edge/"))

def launch_chrome(
    profile: str,
    *,
    work_dir: pathlib.Path,
    udd: pathlib.Path = CHROME_DEBUG,
    headless: bool = False,
) -> tuple[subprocess.Popen, str]:
    work_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = work_dir / "chrome.stderr"
    stderr_path.write_text("")
    args = [
        CHROME_BIN,
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={udd}",
        f"--profile-directory={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--noerrdialogs", "--hide-crash-restore-bubble",
        "--mute-audio",
    ]
    if headless:
        args += ["--headless=new", "--window-size=1366,900"]
    args.append("about:blank")
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w"),
        start_new_session=True,
    )
    # Discover CDP port
    cdp_port = None
    deadline = time.time() + 30.0
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            text = stderr_path.read_text()
        except OSError:
            text = ""
        m = re.search(r"ws://127\.0\.0\.1:(\d+)", text)
        if m:
            cdp_port = m.group(1); break
    if not cdp_port:
        proc.kill()
        raise RuntimeError(f"no CDP port:\n{stderr_path.read_text()}")
    # Confirm alive
    deadline = time.time() + 8.0
    while time.time() < deadline and not _cdp_alive(int(cdp_port)):
        time.sleep(0.2)
    if not _cdp_alive(int(cdp_port)):
        proc.kill()
        raise RuntimeError(f"CDP port {cdp_port} not responding")
    return proc, cdp_port

@contextlib.contextmanager
def attach_signed_in(
    profile: str,
    *,
    work_dir: pathlib.Path,
    udd: pathlib.Path = CHROME_DEBUG,
    headless: bool = False,
):
    """Yield a Playwright Page already signed in for `profile`."""
    from playwright.sync_api import sync_playwright
    if not real_chrome_running():
        bridge_cookies(profile, dst=udd)
    _clear_singleton(udd)
    proc, port = launch_chrome(profile, work_dir=work_dir, udd=udd, headless=headless)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(15000)
            try:
                yield page
            finally:
                try: page.close()
                except Exception: pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
```

### Snippet 2 — discover available profiles

```python
import json, pathlib, re

REAL_CHROME = pathlib.Path.home() / "Library/Application Support/Google/Chrome"

def discover_profiles() -> list[str]:
    """Returns ['Default', 'Profile 1', 'Profile 2', ...]."""
    return sorted([
        p.name for p in REAL_CHROME.iterdir()
        if p.is_dir() and (p.name == "Default" or re.fullmatch(r"Profile \d+", p.name))
    ])

def profile_email_map() -> dict[str, str]:
    """Map 'Profile N' -> 'user@example.com' from Chrome's Local State."""
    ls = json.loads((REAL_CHROME / "Local State").read_text())
    return {
        k: v["user_name"]
        for k, v in ls["profile"]["info_cache"].items()
        if v.get("user_name")
    }

def profile_for_email(email: str) -> str | None:
    return next((p for p, e in profile_email_map().items() if e == email), None)
```

### Snippet 3 — robust like / subscribe state probes

```python
LIKE_SELECTORS = (
    "like-button-view-model button:not(dislike-button-view-model button)",
    "button[aria-label*='like this video' i][aria-pressed]:not([aria-label*='dislike' i])",
    "ytd-toggle-button-renderer #like-button button:not([aria-label*='dislike' i])",
)

SUBSCRIBE_SELECTORS = (
    "ytd-subscribe-button-renderer button",
    "yt-subscribe-button-view-model button",
    "button[aria-label*='Subscribe to' i]",
    "ytd-watch-metadata #subscribe-button-shape button",
)

def probe_like(page) -> tuple[str, object | None]:
    for sel in LIKE_SELECTORS:
        try:
            btn = page.locator(sel).first
            pressed = btn.get_attribute("aria-pressed", timeout=2000)
            if pressed == "true":  return "liked", btn
            if pressed == "false": return "unliked", btn
        except Exception:
            continue
    return "unknown", None

def probe_subscribe(page) -> tuple[str, object | None]:
    for sel in SUBSCRIBE_SELECTORS:
        try:
            btn = page.locator(sel).first
            txt = (btn.text_content(timeout=2000) or "").strip().lower()
            label = (btn.get_attribute("aria-label", timeout=1000) or "").lower()
            combined = f"{txt} {label}"
            if "subscribed" in combined or "unsubscribe" in combined:
                return "subscribed", btn
            if "subscribe" in combined:
                return "unsubscribed", btn
        except Exception:
            continue
    return "unknown", None
```

### Snippet 4 — verify YouTube active brand

```python
import re
from collections import Counter

def active_brand_uc(page) -> str | None:
    """Return the currently active brand's UC (channel id) for the
    signed-in user. Works in headless and windowed mode."""
    # Open a FRESH page — the SPA caches the channel context per-tab,
    # re-using a tab that just did a switch click reads the OLD UC.
    verify = page.context.new_page()
    verify.set_default_timeout(15000)
    try:
        verify.goto("https://www.youtube.com/account",
                    wait_until="domcontentloaded", timeout=30000)
        ucs = re.findall(r"/channel/(UC[A-Za-z0-9_-]{20,})", verify.content())
        if not ucs:
            return None
        # Most-frequent UC = active brand (page is dotted with links to
        # "your channel"). Mode beats picking the first since some
        # markup chunks reference other channels in passing.
        return Counter(ucs).most_common(1)[0][0]
    finally:
        try: verify.close()
        except Exception: pass

def switch_brand(page, target_uc: str) -> bool:
    """Switch active brand. Returns True if `target_uc` is active after
    the call. Idempotent."""
    if active_brand_uc(page) == target_uc:
        return True
    page.goto("https://www.youtube.com/channel_switcher",
              wait_until="domcontentloaded", timeout=30000)
    link = page.locator(f"a[href*='/channel/{target_uc}']").first
    if not link.count() or not link.is_visible(timeout=4000):
        return False
    link.click(timeout=5000)
    # Wait for the SPA navigation to commit
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    return active_brand_uc(page) == target_uc
```

---

## Comparison with alternatives

| Tool                                             | Reads user's signed-in cookies?                        | macOS keychain ACL?                       | Survives parallel runs?                          | Notes                                                                                                                                                                                              |
| ------------------------------------------------ | ------------------------------------------------------ | ----------------------------------------- | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Playwright + CDP attach** (this doc)           | ✓ via cookie bridge                                    | ✓ (uses real Chrome binary)               | ✓ via per-profile sibling UDDs                   | Most reliable end-to-end. Steepest setup. Recommended for any signed-in flow.                                                                                                                       |
| `playwright.launch_persistent_context(real_udd)` | Tries to                                               | ✗ Fresh Playwright Chromium binary fails  | ✗ One UDD lock                                   | Looks tempting; doesn't work for signed-in Google flows on macOS. Cookies decrypt to garbage.                                                                                                       |
| `playwright.launch_persistent_context(fresh_udd)` + manual sign-in | ✗ requires re-sign-in once   | n/a                                       | ✓ if separate UDDs                               | Fine when the user is OK with a one-time `playwright open` to sign in. The dedicated-UDD pays off as soon as you have 2FA / WebAuthn keys you don't want to register a 2nd time.                  |
| **Selenium + ChromeDriver**                      | ✓ via `--user-data-dir` (pre-v136 Chrome only)         | ✓                                         | ✗ One UDD lock                                   | Pre-v136 Chrome would happily accept `--user-data-dir=<real>` with `--remote-debugging-port`. As of v136 the same security policy blocks this; you end up doing the cookie bridge anyway.       |
| **`browser_use` library**                        | ✓ wraps Playwright                                     | ✓ if you pass real-Chrome binary          | ✓                                                | Higher-level abstraction (LLM-driven). Sits on top of the same primitives in this doc. Use it when your script's actions are ambiguous; bare Playwright when they're deterministic.              |
| **Puppeteer + CDP**                              | Same as Playwright (same protocol)                     | Same                                      | Same                                             | Node.js equivalent. Same architectural pattern; different language stack. Snippets above translate 1:1 with `puppeteer.connect({ browserURL: 'http://127.0.0.1:<port>' })`.                       |
| **Headless Chrome via `chromedp` (Go)**          | ✓ via `--user-data-dir`                                | ✓                                         | ✓                                                | Same architecture. Go alternative if you don't want a Python/Node.js stack.                                                                                                                        |
| **Browser extension**                            | ✓ trivially (runs in user's session)                   | n/a                                       | n/a                                              | Wins if you can ship an extension. Loses if your automation must be ephemeral / per-task / cross-machine.                                                                                          |
| **Direct API calls**                             | n/a                                                    | n/a                                       | ✓                                                | Always preferred when the platform exposes an API. Fall back to UI automation only when there's no API for what you need (or rate-limit / quota constraints make API calls infeasible).            |

---

## References

- [Chromium v136 commit — disallow remote debugging on default UDD](https://chromium-review.googlesource.com/c/chromium/src/+/5719976)
- [Playwright `BrowserType.connect_over_cdp` docs](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)
- [Playwright `launch_persistent_context` docs](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [Chrome DevTools Protocol — `/json/version`](https://chromedevtools.github.io/devtools-protocol/#endpoints)
- [macOS keychain ACL — `find-generic-password`](https://developer.apple.com/documentation/security/keychain_services)
- [YouTube Help — How subscriber count is calculated](https://support.google.com/youtube/answer/3045552)
- [Apple — `launchd.plist(5)`](https://www.manpagez.com/man/5/launchd.plist/)
- [Apple — `launchctl(1)` (modern subcommands)](https://ss64.com/osx/launchctl.html)

---

## Changelog

- **2026-05-13** — Initial extraction from the YouTube cross-engagement
  system that originally validated these patterns. Captures the
  architecture, the `aria-pressed` / brand-switch / multi-UDD details, and
  the cloud-laptop split pattern. The originating system was retired (see
  [Anti-automation observations](#anti-automation-observations)); the
  techniques apply to any signed-in browser automation.
