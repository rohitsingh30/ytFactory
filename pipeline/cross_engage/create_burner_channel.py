"""Create a new "burner" YouTube channel under an existing Google account.

A *burner* (per ``pipeline.burner_engage``) is a YouTube channel we own
but which is **not** in the production ``CHANNEL_REGISTRY`` — it exists
purely to like/subscribe/watch our production catalog from a fresh
identity. New burners are spawned as **brand accounts** under an
existing Google account (default ``rsinghtomar54@gmail.com``).

Burners are intentionally throwaway — names default to a random
no-space alphanumeric string (e.g. ``afddfdf``, ``khihfcgghdfj``,
``kjgjj``) to match the look of the existing burners on the host
account. Pass ``--display-name <foo>`` if you actually care about a
specific name.

Pre-condition: YouTube must be signed-in for the target Google account
in real Chrome's matching profile. The cookie bridge copies whatever
session cookies are currently alive there — a "signed out" status on
YouTube in real Chrome → "Choose an account" picker in the bridged
session → script aborts. Sign in via real Chrome on the right Profile
once and the bridge does its job.

The actual create-channel flow (validated 2026-05-09)
-----------------------------------------------------
YouTube **changed** the create-channel UX recently. The older
``/create_channel`` URL no longer prompts for a name when the account
already has a personal channel — it silently creates a default-named
brand account and redirects you to its page. The current flow:

  1. ``GET /account``                               (Settings → Account)
  2. **CLICK** ``yt-button-shape a[aria-label='Create a channel']``
     — must be a real click; the JS handler (``force-new-state="true"``)
     opens a modal. Direct ``page.goto(href)`` skips the handler and
     lands you on the silent personal-channel redirect path.
  3. Modal "**How you'll appear**" appears (``tp-yt-paper-dialog``
     with ``ytd-channel-creation-*`` content).
  4. Fill dialog ``input[type='text']`` ``[0]`` = channel name (Material
     floating-label, no ``placeholder``/``aria-label`` attributes —
     identify by index INSIDE the dialog scope).
  5. Fill dialog ``input[type='text']`` ``[1]`` = handle (no leading
     ``@``; YouTube prepends it).
  6. If handle taken: a suggestion link ``a:has-text('@<suggested>')``
     appears inside the dialog. Click it to apply.
  7. Wait for "Create channel" button to enable (handle availability
     check is async — usually 1-3 s).
  8. Click dialog ``button[aria-label='Create channel']``.
  9. After successful create, page redirects to
     ``/channel/<NEW_UC_ID>``. Extract the ``UC<22>`` ID from the URL.

Failure modes:
  * "**Failed to create channel. Please try changing your channel name
    and try again.**" → per-account rate-limit OR name was used in a
    failed attempt earlier today. Wait ~24h or pick a different name.

Mechanics
---------
Same Chrome-Debug + CDP machinery as ``upload_via_playwright`` and
``cross_engage_via_playwright`` — bridge cookies from real Chrome's
``Profile N`` (auto-mapped via ``profile_email_map()``), launch a fresh
Chrome with ``--remote-debugging-port=0`` against the bridged
user-data-dir, attach Playwright over CDP, never call ``new_context``
(the existing context carries the signed-in YouTube session).

After successful create:

1. Extract the new channel's ID from ``/channel/UC...``
2. Append the burner to ``~/.config/ytfactory/channel_ids.json`` so
   ``list_burner_channels()`` picks it up.
3. Add the slug → email mapping to ``~/.config/ytfactory/profile_map.json``
   so ``_account_email_for_token`` can resolve it later.
4. Print next steps for OAuth (``pipeline.upload.upload.authenticate <slug>``).

Usage
-----
::

    # Default — random no-space name (matches burner aesthetic):
    /Users/rohit/ytFactory/.venv/bin/python -m pipeline.create_burner_channel

    # Specific name (no spaces in slug; spaces collapse for slug):
    /Users/rohit/ytFactory/.venv/bin/python -m pipeline.cross_engage.create_burner_channel \\
        --display-name cosmicdrift

    # Different host account:
    /Users/rohit/ytFactory/.venv/bin/python -m pipeline.cross_engage.create_burner_channel \\
        --email rsinghtomar30@gmail.com

Flags
-----
* ``--keep-open``  — leave Chrome up so you can verify the new channel
                     in YouTube Studio before tear-down.
* ``--no-register`` — skip the channel_ids.json + profile_map.json
                      writes (dry-run-ish — useful when the user wants
                      to register manually after eyeballing).
* ``--dry-run``    — drive the wizard up to (but not clicking) the final
                     "Create channel" button.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import re
import shlex
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Re-use the proven Chrome-Debug helpers — same launch flags, same
# cookie-bridge contract, same singleton-lock cleanup. Forking would
# leave us maintaining two copies of "how to talk to Chrome".
from pipeline.cross_engage.cross_engage_via_playwright import (
    _clear_singleton,
    assert_chrome_closed,
    bridge_cookies,
    launch_chrome_for,
    profile_email_map,
)

DEFAULT_EMAIL = "rsinghtomar54@gmail.com"
TOKEN_DIR = pathlib.Path.home() / ".config" / "ytfactory"
CHANNEL_IDS_PATH = TOKEN_DIR / "channel_ids.json"
PROFILE_MAP_PATH = TOKEN_DIR / "profile_map.json"
DEFAULT_WORK_DIR = pathlib.Path("/tmp/pw-create-burner")

ACCOUNT_URL = "https://www.youtube.com/account"
# Selector for the in-page "Create a channel" CTA. Validated 2026-05-09:
# YouTube wraps it in a <yt-button-shape> Spec component; the inner <a>
# carries aria-label="Create a channel" and a force-new-state JS hook.
CREATE_CHANNEL_BTN_SEL = "yt-button-shape :is(a, button)[aria-label='Create a channel']"
# Modal selector — Polymer paper-dialog hosting <ytd-channel-creation-*>
DIALOG_SEL = "tp-yt-paper-dialog"
DIALOG_TITLE_TEXT = "How you'll appear"

UC_ID_RE = re.compile(r"(?:/channel/|next=[^\"]*?(?:/|%2F)channel(?:/|%2F))(UC[A-Za-z0-9_-]{20,})")


# ── name + slug derivation ────────────────────────────────────────────────

def random_burner_name(*, length: int | None = None) -> str:
    """Generate a random lowercase-letters-only name in the style of the
    existing burners on the host account (``afddfdf``, ``khihfcgghdfj``,
    ``kjgjj``, ``newrtrudaj``). Length 6-12 chars feels right — short
    enough to type, long enough that handle collisions are rare.

    Letters only (no digits) — matches the existing burner aesthetic
    AND avoids YouTube's per-handle "looks like a username" warnings
    that fire when digits are present.
    """
    if length is None:
        length = random.randint(6, 12)
    return "".join(random.choices(string.ascii_lowercase, k=length))


# Wordlists for realistic_burner_name(). All entries are lowercase
# alphanumeric only (no spaces, no hyphens, no underscores) so the
# resulting display-name is also a valid slug AND a valid YouTube
# handle. Curated to avoid loaded / brand / political terms — generic
# nature/everyday words that look like a real person's screen name.
_FIRST_NAMES = (
    "mike", "leo", "sam", "alex", "ryan", "kai", "ben", "luke", "ethan",
    "noah", "owen", "max", "jack", "finn", "theo", "miles", "felix", "dean",
    "ivan", "milo", "drew", "cole", "reed", "rhys", "zane", "wade", "kyle",
    "neil", "evan", "scott", "tyler", "blake", "jude", "jonah", "graham",
    "henry", "asher", "elias", "rohan", "arjun", "vikram", "ravi", "neel",
    "anya", "leah", "maya", "iris", "ruby", "ada", "elle", "chloe", "nora",
    "ivy", "june", "lena", "mira", "naomi", "tess", "zoe", "freya", "stella",
)
_NOUNS = (
    "draft", "harbor", "rider", "canyon", "drifter", "lantern", "nomad",
    "ember", "ridge", "valley", "summit", "comet", "meadow", "haven",
    "voyage", "compass", "echo", "current", "trail", "atlas", "horizon",
    "bayou", "delta", "marsh", "creek", "dune", "fjord", "glade", "knoll",
    "moor", "oasis", "shore", "tundra", "arc", "spire", "arrow", "raven",
    "falcon", "owl", "wolf", "fox", "lynx", "otter", "heron", "swift",
    "robin", "finch", "wren", "lark", "hawk", "studio", "lab", "press",
)
_ADJECTIVES = (
    "blue", "red", "fast", "slow", "calm", "bright", "dark", "warm", "cool",
    "wild", "tall", "swift", "quiet", "bold", "lone", "gold", "silver",
    "north", "south", "east", "west", "high", "low", "open", "still", "raw",
    "sharp", "soft", "deep", "wide", "long", "short", "fresh", "clear",
    "amber", "indigo", "olive", "rust", "smoke", "ivory", "ash", "moss",
)
_NOUNS_SHORT = (
    "pine", "owl", "fox", "bay", "moon", "sun", "lake", "hill", "river",
    "wave", "cloud", "stone", "wood", "field", "mist", "frost", "rain",
    "wind", "snow", "leaf", "rock", "ice", "sand", "fire", "salt", "trail",
    "path", "road", "ridge", "peak", "cove", "reef", "vale", "glen",
    "coast", "creek", "harbor", "haven", "isle", "marsh", "sky",
)


def realistic_burner_name(*, max_len: int = 18, attempts: int = 12) -> str:
    """Generate a realistic-looking screen-name (NO spaces, lowercase
    alphanumeric only, ≤ ``max_len`` chars).

    Picks one of four styles uniformly, retrying on length overflow
    or empty result. Falls back to ``random_burner_name`` if every
    attempt overshoots — guarantees a non-empty return.

    Styles:
      1. firstname + noun           e.g. ``mikedraft``, ``leoharbor``
      2. single noun                e.g. ``canyon``, ``drifter``
      3. firstname + 2-3 digits     e.g. ``mike42``, ``leo07``
      4. adjective + short noun     e.g. ``bluepine``, ``fastcoast``

    Why these constraints:
      * Display name **= handle = slug** in our flow; YouTube handles
        require lowercase alphanumeric (handles do allow ``.`` and
        ``_`` and ``-`` but our slug regex strips them, so easier to
        avoid).
      * No spaces — user explicit requirement.
      * Length cap 18 — YouTube handles cap at 30, but anything past
        ~18 starts looking spammy / random-string-ish.
      * Length floor 4 — handles must be ≥ 3 chars; we add a buffer
        for collision-suggestion suffixes.
    """
    for _ in range(attempts):
        style = random.choice((1, 2, 3, 4))
        if style == 1:
            name = random.choice(_FIRST_NAMES) + random.choice(_NOUNS)
        elif style == 2:
            name = random.choice(_NOUNS)
        elif style == 3:
            name = random.choice(_FIRST_NAMES) + str(random.randint(0, 99)).zfill(2)
        else:
            name = random.choice(_ADJECTIVES) + random.choice(_NOUNS_SHORT)
        # Defensive: belt-and-suspenders strip — wordlists are already
        # clean but a future edit might slip a hyphen / space in.
        name = re.sub(r"[^a-z0-9]+", "", name.lower())
        if 4 <= len(name) <= max_len:
            return name
    # All attempts overshot or undershot; fall back to the original
    # gibberish generator so we always return SOMETHING usable.
    return random_burner_name()


def derive_slug(display_name: str) -> str:
    """Normalize a display name into a usable slug.

    Lowercase, drop everything that isn't alphanumeric. We deliberately
    drop hyphens/underscores/spaces so the slug matches the
    ``youtube_token_<slug>.json`` convention used elsewhere in the
    pipeline (``alphanumeric only`` is the only assumption we make about
    slug shape — see ``CONFIG_DIR / f"youtube_token_{safe}.json"`` in
    ``pipeline.upload``).
    """
    return re.sub(r"[^a-z0-9]+", "", display_name.lower())


def derive_handle(slug: str) -> str:
    """Default handle to the slug. YouTube will suggest an alternative
    (e.g. ``cosmicdrift-x4m``) if the bare handle is taken; we'll click
    the suggestion link in that case."""
    return slug


# ── coexistence: detect / attach to a running Chrome-Debug ────────────────

CHROME_DEBUG_DIR = "/Users/rohit/Library/Application Support/Google/Chrome-Debug"


def _cdp_port_for_pid(pid: int) -> int | None:
    """Return the CDP port a given Chrome PID is listening on.

    Two-step approach because ``lsof -p <pid> -iTCP`` doesn't reliably
    surface the remote-debugging socket (Chrome on macOS keeps it on
    the parent process but lsof sometimes lists it under a helper):

    1. Walk this PID's open file descriptors (``lsof -p <pid>``) to
       find the ``chrome.stderr`` Chrome itself is writing to. That
       file is per-launch and contains the ``ws://127.0.0.1:<PORT>``
       header Chrome prints on startup. Authoritative — there's
       exactly one such file per running Chrome instance.
    2. Parse the port from that stderr's contents. ``_cdp_alive``
       confirms it's actually responsive.
    """
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True)
    stderr_path: str | None = None
    for line in out.stdout.splitlines():
        if "chrome.stderr" in line:
            # `lsof -p` rows: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            parts = line.split(None, 8)
            if len(parts) >= 9:
                stderr_path = parts[-1].strip()
                break
    if not stderr_path:
        return None
    try:
        text = pathlib.Path(stderr_path).read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"ws://127\.0\.0\.1:(\d+)", text)
    return int(m.group(1)) if m else None


def find_running_chrome_debug(profile: str, user_data_dir: str | None = None) -> tuple[int, int] | None:
    """Find a Chrome-Debug PID + CDP port already running on ``profile``.

    Returns ``(pid, cdp_port)`` if exactly one match is found, else None.
    "Match" means a Google Chrome process whose argv contains both
    ``--user-data-dir=<user_data_dir>`` and ``--profile-directory=<profile>``.
    Defaults to the canonical ``CHROME_DEBUG_DIR`` — pass an explicit
    path to scope to a sibling instance (e.g. the engage-only one).

    The CDP port is recovered via ``lsof`` on the matched PID — that's
    authoritative (PID-scoped). The earlier impl scanned
    ``/tmp/pw-*/chrome.stderr`` files for any alive ``ws://127.0.0.1:<PORT>``
    and returned the first hit, which conflated ports across different
    Chrome instances when multiple stderr files existed (validated bug
    2026-05-09: returned engage PID 69932 with port 58780 belonging to
    canonical PID 46010 — would have engaged in user's match window).
    """
    udd = user_data_dir or CHROME_DEBUG_DIR
    # Strict-token needles: the argv has Chrome's flags space-separated,
    # so the user-data-dir value must be followed by whitespace OR end
    # of string. Substring-only match misclassified the canonical
    # ``Chrome-Debug`` udd as matching a process actually using
    # ``Chrome-Debug-engage`` (validated bug 2026-05-09).
    needle_dir = f"--user-data-dir={udd} "
    needle_prof = f"--profile-directory={profile} "
    # macOS `pgrep -af` doesn't print argv (BSD vs GNU diff); use ps.
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid,command"],
        capture_output=True, text=True,
    )
    matches: list[tuple[int, str]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if "Google Chrome.app/Contents/MacOS/Google Chrome" not in line:
            continue
        # Skip Chrome helpers (renderers, gpu, etc.)
        if " --type=" in line:
            continue
        try:
            pid_str, _, argline = line.partition(" ")
            pid_i = int(pid_str)
        except ValueError:
            continue
        if needle_dir in argline and needle_prof in argline:
            matches.append((pid_i, argline))
    if len(matches) != 1:
        return None
    pid = matches[0][0]

    # Authoritative port discovery via lsof on this exact PID.
    port = _cdp_port_for_pid(pid)
    if port and _cdp_alive(port):
        return pid, port
    return None


def _cdp_alive(port: int, *, timeout: float = 2.0) -> bool:
    """Probe ``http://127.0.0.1:<port>/json/version`` to confirm CDP is live
    AND served by Chrome (not by some other CDP-speaking host).

    Filters out two classes of laptop false positive:
      1. Non-CDP localhost listeners (HTTP 200 check).
      2. node.js / Electron app debuggers (VS Code, Slack, …) that
         ALSO answer ``/json/version`` because V8's inspector
         implements the same endpoint. They list as
         ``"Browser": "node.js/vXX"``; Chrome lists as
         ``"Browser": "Chrome/XX"``. Without this guard, Playwright's
         ``connect_over_cdp`` would attach to a node.js V8 inspector
         and fail with ``Invalid URL: undefined`` on the next /json
         enumeration call. Validated bug 2026-05-11.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout,
        ) as r:
            if r.status != 200:
                return False
            payload = json.loads(r.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    browser = str(payload.get("Browser", ""))
    return browser.startswith("Chrome/") or browser.startswith("Edge/")


# ── pre-flight ─────────────────────────────────────────────────────────────

def resolve_profile(email: str, override: str | None) -> str:
    if override:
        return override
    rev = {v: k for k, v in profile_email_map().items()}
    if email not in rev:
        raise SystemExit(
            f"email {email!r} not found in real Chrome's Local State.\n"
            f"Sign into the account in real Chrome once first.\n"
            f"Known: {sorted(rev.keys())}"
        )
    return rev[email]


def assert_no_collision(slug: str, *, allow_existing: bool = False) -> None:
    """Refuse to overwrite an existing burner unless ``allow_existing``."""
    token = TOKEN_DIR / f"youtube_token_{slug}.json"
    if token.exists() and not allow_existing:
        raise SystemExit(
            f"OAuth token already exists at {token}. Pick a different slug "
            f"or pass --allow-existing if you want to recreate this burner."
        )
    ids = _load_json(CHANNEL_IDS_PATH, {})
    if slug in ids and not allow_existing:
        raise SystemExit(
            f"slug {slug!r} already in {CHANNEL_IDS_PATH.name} "
            f"(channel_id={ids[slug].get('channel_id')!r}). "
            f"Pick a different slug or pass --allow-existing."
        )


# ── small JSON helpers ─────────────────────────────────────────────────────

def _load_json(path: pathlib.Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ── wizard driver ─────────────────────────────────────────────────────────

def _shoot(page, work_dir: pathlib.Path, name: str) -> None:
    try:
        page.screenshot(path=str(work_dir / f"{name}.png"), full_page=False)
    except Exception as e:
        print(f"  ⚠ screenshot {name} failed: {e}", flush=True)


def _dump_snapshot(page, work_dir: pathlib.Path, name: str) -> None:
    """Save the page DOM + URL + title as a debug artifact. Invaluable
    when YouTube drifts and selectors stop matching — diff two snapshots
    to find the new role/name.

    (Playwright Python dropped the public ``page.accessibility`` API in
    1.45+; we save a stripped HTML snapshot instead, which is what we
    actually grep for new selectors anyway.)
    """
    try:
        html = page.content()
        (work_dir / f"{name}.html").write_text(html)
        meta = {"url": page.url, "title": page.title()}
        (work_dir / f"{name}.meta.json").write_text(json.dumps(meta, indent=2))
    except Exception as e:
        print(f"  ⚠ snapshot {name} failed: {e}", flush=True)


def _maybe_dismiss_overlays(page) -> None:
    """YouTube ships a rotating cast of cookie banners + 'try the new
    YouTube' modals + first-launch "Select a channel" pickers.
    Shotgun the most common 'dismiss' affordances.

    Special case: "Select a channel" modal — appears when an account
    has multiple brand accounts AND YouTube hasn't recorded a default
    pick. Blocks all interaction. Implemented as
    ``ytd-channel-switcher-renderer`` inside ``tp-yt-paper-dialog``.
    We click the first option to dismiss (the actual choice doesn't
    matter for our flow — we'll switch to the right brand via the
    channel-switcher anyway).
    """
    # Dismiss "Select a channel" modal first (it's a hard block).
    try:
        if page.locator("ytd-channel-switcher-renderer").first.is_visible(timeout=1500):
            row = page.locator("ytd-channel-switcher-renderer ytd-account-item-renderer").first
            if row.count() and row.is_visible(timeout=1500):
                print("     → dismissing 'Select a channel' modal (clicking first row)", flush=True)
                row.click(timeout=3000)
                time.sleep(2.0)
    except Exception:
        pass
    for sel in (
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "tp-yt-paper-button:has-text('Accept all')",
        "button:has-text('Reject all')",
        "button:has-text('No thanks')",
        "button:has-text('Got it')",
        "button:has-text('Dismiss')",
    ):
        try:
            page.locator(sel).first.click(timeout=1500)
            time.sleep(0.4)
        except Exception:
            continue


def _open_create_modal(page, work_dir: pathlib.Path, *, personal_handle: str, email: str) -> None:
    """Land on /channel_switcher and click "Create a channel" to open the modal.

    Critical: must CLICK the element. Direct ``page.goto`` to its href
    bypasses YouTube's JS handler and silently creates a default-named
    channel without a name prompt (validated 2026-05-09 — that's how we
    accidentally spawned ``newrtrudaj`` and ``khihfcgghdfj``).

    Why ``/channel_switcher`` and not ``/account`` directly: the
    "Create a channel" CTA on plain ``/account`` only renders when the
    active YouTube channel context is the personal channel. Brand
    accounts hide it. ``/channel_switcher`` redirects to ``/account``
    but in "All channels" view, which renders a "+ Create a channel"
    tile **regardless of the active context** (validated 2026-05-09).

    The ``personal_handle`` arg is unused on this path but kept for
    backwards compat — earlier we needed to switch contexts via the
    avatar menu before /account would show the CTA. The
    /channel_switcher path is simpler and works from any context.
    """
    del personal_handle  # kept for back-compat; /channel_switcher path doesn't need it

    target = "https://www.youtube.com/channel_switcher"
    print(f"  → goto {target}", flush=True)
    page.goto(target, wait_until="commit", timeout=30000)

    # First-shot dismissal: blocking modals like "Select a channel"
    # may render on initial load before the polling loop starts. Get
    # them out of the way once before we start watching for the CTA.
    time.sleep(2.0)
    _maybe_dismiss_overlays(page)

    # /channel_switcher 302s to /account in ~0.8 s. Race the redirect:
    # the Create-a-channel button shows up on the post-redirect /account
    # page in "All channels" view. Poll for it instead of waiting for
    # full load. Two transient-redirect classes to handle:
    #   - /signin_prompt — auto-redirects after a few seconds
    #   - accounts.google.com/v3/signin/accountchooser — needs a CLICK
    #     on the matching account row before it auto-completes
    btn_loc = page.locator(CREATE_CHANNEL_BTN_SEL).first
    deadline = time.time() + 30.0
    found = False
    last_url = ""
    accountchooser_clicked = False
    signin_prompt_seen_at: float | None = None
    overlay_dismissed = False
    while time.time() < deadline:
        url = page.url
        if url != last_url:
            print(f"     url={url[:120]}", flush=True)
            last_url = url
            overlay_dismissed = False  # re-try overlays on new URL

        # Dismiss any blocking modal (e.g. "Select a channel") that's
        # parking us on an interstitial. Run once per URL.
        if not overlay_dismissed:
            _maybe_dismiss_overlays(page)
            overlay_dismissed = True

        # Real, irrecoverable sign-in: ServiceLogin or password challenge.
        if "accounts.google.com/ServiceLogin" in url or "/signin/v2/identifier" in url or "/signin/challenge" in url:
            _dump_snapshot(page, work_dir, "01-account")
            raise SystemExit(
                "redirected to sign-in (password challenge) — the bridged "
                "cookies didn't carry the YouTube session. Sign in via real "
                "Chrome on the matching Profile once, close Chrome entirely, "
                "then retry."
            )

        # Account-chooser interstitial: click the matching email row to advance.
        if "/signin/accountchooser" in url and not accountchooser_clicked:
            email_token = email.split("@", 1)[0]
            print(f"     account-chooser interstitial — clicking row matching {email_token!r}", flush=True)
            try:
                row = page.get_by_text(email_token, exact=False).first
                if row.count() and row.is_visible(timeout=3000):
                    row.click(timeout=5000)
                    accountchooser_clicked = True
                    time.sleep(3.0)
                    continue
            except Exception as e:
                print(f"     ⚠ couldn't click account row: {type(e).__name__}: {e}", flush=True)

        # Transient: youtube.com/signin_prompt redirects on its own
        # after a few seconds. Note when we first saw it; bail only if
        # it's still there 12 s later (real stuck state).
        if "/signin_prompt" in url:
            if signin_prompt_seen_at is None:
                signin_prompt_seen_at = time.time()
            elif time.time() - signin_prompt_seen_at > 12:
                _dump_snapshot(page, work_dir, "01-stuck-signin-prompt")
                raise SystemExit(
                    f"stuck on /signin_prompt for 12s — see "
                    f"{work_dir}/01-stuck-signin-prompt.html"
                )

        if btn_loc.count() > 0:
            found = True; break
        time.sleep(0.4)

    _shoot(page, work_dir, "01-account")
    if not found:
        _dump_snapshot(page, work_dir, "01-no-create-button")
        raise SystemExit(
            "couldn't find the 'Create a channel' button on /account after "
            f"15s. YouTube UI may have drifted — see "
            f"{work_dir}/01-no-create-button.html"
        )

    _maybe_dismiss_overlays(page)
    print("  → click 'Create a channel'", flush=True)
    btn_loc.click(timeout=8000)
    time.sleep(3.0)

    # Two possible modals appear after the click:
    #   1. "How you'll appear" — the create form (Name + Handle + Create button).
    #   2. "Get advanced features" — phone-verification gate. Can't automate
    #      (SMS/voice OTP); this account is structurally blocked from creating
    #      more channels until the user verifies a phone number.
    deadline = time.time() + 15.0
    modal_kind: str | None = None
    while time.time() < deadline:
        if page.locator(f"text={DIALOG_TITLE_TEXT}").count():
            modal_kind = "create_form"
            break
        if page.locator("text='Get advanced features'").count() or page.locator("button:has-text('Verify')").count():
            modal_kind = "phone_verification_required"
            break
        time.sleep(0.5)

    if modal_kind == "phone_verification_required":
        _shoot(page, work_dir, "02-verification-required")
        _dump_snapshot(page, work_dir, "02-verification-required")
        raise SystemExit(
            f"YouTube requires phone verification on {email!r} before "
            f"creating more channels. Open Chrome → that profile → click "
            f"Verify in the modal → complete SMS/voice OTP. Then retry. "
            f"Screenshot: {work_dir}/02-verification-required.png"
        )

    if modal_kind != "create_form":
        _dump_snapshot(page, work_dir, "02-no-modal")
        _shoot(page, work_dir, "02-no-modal")
        raise SystemExit(
            "neither 'How you\\'ll appear' nor 'Get advanced features' modal "
            f"appeared after Create-channel click. See {work_dir}/02-no-modal.html"
        )

    time.sleep(1.0)
    _shoot(page, work_dir, "02-modal-open")


def _dialog(page):
    """Locator for the channel-creation dialog. Always grab the LAST
    matching element — earlier paper-dialogs may exist for unrelated
    flows; the channel-creation one is mounted last."""
    return page.locator(DIALOG_SEL).last


def _fill_modal(page, work_dir: pathlib.Path, *, display_name: str, handle: str) -> None:
    """Fill the Name + Handle inputs.

    Material floating-label pattern means the inputs have NO ``id``,
    NO ``placeholder``, NO ``aria-label``. The label text is rendered
    as a sibling ``<label>``. Easiest stable selector: nth visible
    text input INSIDE the dialog. ``[0]`` = Name, ``[1]`` = Handle.
    Validated 2026-05-09.
    """
    dialog = _dialog(page)
    inputs = [i for i in dialog.locator("input").all() if i.is_visible(timeout=300)]
    if len(inputs) < 2:
        _dump_snapshot(page, work_dir, "03-modal-inputs-missing")
        raise SystemExit(
            f"expected ≥2 visible inputs in the channel-creation dialog, "
            f"found {len(inputs)}. See {work_dir}/03-modal-inputs-missing.html"
        )
    name_box, handle_box = inputs[0], inputs[1]

    print(f"  → fill Name: {display_name!r}", flush=True)
    name_box.click(timeout=5000)
    name_box.fill("")
    name_box.type(display_name, delay=40)  # paced — YouTube has bot heuristics
    time.sleep(0.5)

    print(f"  → fill Handle: {handle!r}", flush=True)
    handle_box.click(timeout=5000)
    handle_box.fill("")
    handle_box.type(handle, delay=40)
    time.sleep(2.0)  # let async availability check settle


def _resolve_handle_collision(page, work_dir: pathlib.Path) -> str | None:
    """If the typed handle is taken, YouTube renders a suggestion link
    ``<a>@<suggested></a>`` under the field. Click it to apply.

    Returns the new handle if a suggestion was applied, else None.
    """
    dialog = _dialog(page)
    # Suggestion text starts with '@' and is wrapped in an <a>. Detect
    # by looking for any <a> containing '@' inside the dialog (the only
    # @-prefixed link in this dialog is the suggestion).
    suggestion = dialog.locator("a:has-text('@')").first
    try:
        if suggestion.count() and suggestion.is_visible(timeout=1000):
            txt = (suggestion.text_content() or "").strip()
            print(f"  → handle taken; clicking suggestion {txt!r}", flush=True)
            suggestion.click(timeout=5000)
            time.sleep(2.0)
            _shoot(page, work_dir, "04-suggestion-applied")
            return txt.lstrip("@") or None
    except Exception:
        pass
    return None


def _create_button(page):
    """The dialog's "Create channel" button (NOT YouTube's top-bar Create)."""
    return _dialog(page).get_by_role(
        "button", name=re.compile(r"^create channel$", re.I)
    ).first


def _wait_button_enabled(btn, *, timeout_s: float = 12.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if btn.is_enabled():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_for_channel_id(page, work_dir: pathlib.Path, timeout_s: float = 60.0) -> str | None:
    """Spin until the URL reveals the new ``UCxxx`` channel ID.

    On success YouTube redirects to ``/channel/<UC_ID>`` (and the page
    title becomes the new channel's display name). The redirect can
    happen 30-60 s after the Create click on slow networks. We use
    ``page.wait_for_url`` which is event-driven (not polling) and
    catches navigations reliably.
    """
    try:
        page.wait_for_url(UC_ID_RE, timeout=timeout_s * 1000)
    except Exception:
        # Fall through to a manual recheck below.
        pass

    final_url = page.url
    m = UC_ID_RE.search(final_url)
    if m:
        return m.group(1)

    # Last-ditch: scrape the rendered HTML for a channelId. YouTube
    # sometimes mounts the new channel into the existing /account view
    # without a top-level URL change.
    try:
        html = page.content()
        m2 = re.search(r'"channelId":"(UC[A-Za-z0-9_-]{20,})"', html)
        if m2:
            return m2.group(1)
    except Exception:
        pass

    _shoot(page, work_dir, "07-no-channel-id-after-create")
    _dump_snapshot(page, work_dir, "07-no-channel-id-after-create")
    return None


# ── OAuth in the attached Chrome ──────────────────────────────────────────

OAUTH_URL_RE = re.compile(r"https://accounts\.google\.com/[^\s\"'<>]+")


def _drive_oauth_consent(page, work_dir: pathlib.Path, *, host_email: str, max_steps: int = 10) -> bool:
    """Click through Google's OAuth consent screens automatically.

    Validated flow (2026-05-09 against client_id ending …bdoft.apps):
      1. Account picker (``/accountchooser``): click the row matching ``host_email``.
      2. (Sometimes) warning interstitial (``/oauth/warning``): click Continue.
      3. Consent summary (``/oauth/v2/consentsummary``): tick "Select all"
         to grant ALL requested scopes, then click Continue.
      4. Redirect to ``localhost:8089/?code=…`` → callback server picks
         up the code, OAuth subprocess exits.

    Returns True if we reach the localhost callback. False if we get
    stuck — caller should fall back to user-drives-it mode.
    """
    for step in range(max_steps):
        time.sleep(2.0)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        url = page.url
        print(f"  [consent step {step}] {url[:120]}", flush=True)
        if "localhost:8089" in url or "127.0.0.1:8089" in url:
            print("  ✓ landed on callback URL", flush=True)
            _shoot(page, work_dir, f"08c-step{step:02d}-callback")
            return True

        clicked = False

        # 1. Account picker
        if "accountchooser" in url or "Choose an account" in (page.title() + " "):
            row = page.locator(f"[data-email='{host_email}']").first
            if row.count() == 0:
                row = page.locator(f"text={host_email}").first
            if row.count():
                print(f"  → click account row {host_email!r}", flush=True)
                row.click(timeout=5000)
                clicked = True

        # 2. Consent summary — tick "Select all" if not already, then Continue
        if not clicked and "consentsummary" in url:
            sel_all = page.get_by_role("checkbox", name=re.compile(r"select all", re.I)).first
            if sel_all.count() == 0:
                sel_all = page.locator("input[type=checkbox]").first
            try:
                if sel_all.count() and not sel_all.is_checked():
                    print("  → tick 'Select all' (grant all requested scopes)", flush=True)
                    sel_all.check(timeout=5000)
                    time.sleep(0.5)
            except Exception as e:
                print(f"  ⚠ Select-all tick failed (may already be checked): {e}", flush=True)
            # Google's consent-summary "grant" button has been a moving
            # target — historically Continue, sometimes Allow, sometimes
            # rendered as a div with role=button and an aria-label rather
            # than text content. Try every shape we've seen, in order of
            # how recently we last saw each one win.
            consent_selectors = [
                # Role + accessible-name (Continue / Allow). Most stable
                # when present.
                lambda: page.get_by_role("button", name=re.compile(r"^continue$", re.I)).first,
                lambda: page.get_by_role("button", name=re.compile(r"^allow$", re.I)).first,
                # 2026-05-11: consentsummary now ships an "Update" CTA on
                # the new "additional permissions" UI variant. Same
                # role=button, different label.
                lambda: page.get_by_role("button", name=re.compile(r"^update$", re.I)).first,
                # Last-resort: text-content match on the visible button —
                # picks up div-styled "buttons" that don't carry role.
                lambda: page.locator("button:has-text('Continue'), [role='button']:has-text('Continue')").first,
                lambda: page.locator("button:has-text('Allow'), [role='button']:has-text('Allow')").first,
                # Older variant: `submit` input with value="Continue".
                lambda: page.locator("input[type='submit'][value='Continue']").first,
            ]
            for build in consent_selectors:
                try:
                    cand = build()
                    if cand.count() and cand.is_visible(timeout=1500):
                        label = (cand.get_attribute("aria-label") or
                                 cand.text_content() or "consent CTA").strip()[:40]
                        print(f"  → click consent CTA: {label!r}", flush=True)
                        cand.click(timeout=8000)
                        clicked = True
                        break
                except Exception:
                    continue

        # 3. Generic Continue / Allow / Accept fallback (warning, recovery)
        if not clicked:
            for txt in ("Continue", "Allow", "Accept", "I agree", "Next"):
                btn = page.get_by_role("button", name=re.compile(rf"^{txt}$", re.I)).first
                try:
                    if btn.count() and btn.is_visible(timeout=1000):
                        print(f"  → click '{txt}'", flush=True)
                        btn.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue

        if not clicked:
            print("  ⚠ no actionable element found — falling back to manual drive", flush=True)
            _shoot(page, work_dir, f"08c-step{step:02d}-stuck")
            _dump_snapshot(page, work_dir, f"08c-step{step:02d}-stuck")
            return False

    print(f"  ⚠ {max_steps} consent steps exhausted without callback", flush=True)
    return False


def oauth_in_attached_chrome(slug: str, page, work_dir: pathlib.Path, *, host_email: str, timeout_s: int = 600) -> bool:
    """Run ``pipeline.upload.upload.authenticate(slug, interactive=True)`` BUT
    route Google's auth URL through the already-attached Chrome page
    instead of opening a new system-default browser.

    Flow:
      1. Spawn the authenticate() subprocess (with ``open_browser=False``
         via the helper module — it prints the auth URL and blocks on a
         local HTTP server at ``localhost:8089/?code=...``).
      2. Scrape the auth URL from its stdout.
      3. ``page.goto(auth_url)`` in the attached Chrome window — user
         clicks through Google's account picker + consent IN THIS
         WINDOW (no second browser to context-switch to).
      4. Google redirects to ``localhost:8089`` → subprocess's callback
         server captures the code, exchanges it for the token, writes
         ``~/.config/ytfactory/youtube_token_<slug>.json``, exits 0.
      5. We wait for that exit, propagate the result.

    Returns True on success.

    Note: the Google consent flow REQUIRES human interaction (account
    picker, "Choose YouTube channel" picker for brand accounts, "Allow"
    button). We don't try to automate clicks there — Google has strong
    anti-automation heuristics on consent screens AND the user might
    want to verify the channel selection visually. The script just
    parks the URL in the right window so they don't have to copy-paste.
    """
    print(f"\n[create-burner] starting OAuth for slug={slug!r} in attached Chrome…", flush=True)

    # Park the attached Chrome on about:blank BEFORE the subprocess
    # binds localhost:8089. Critical when --keep-open or a previous
    # OAuth attempt left a stale ``localhost:8089/?state=OLD&code=OLD``
    # tab open: the moment the new subprocess opens port 8089, Chrome's
    # network layer auto-refetches the stale URL → server gets the OLD
    # state → ``MismatchingStateError: CSRF Warning! State not equal in
    # request and response`` → subprocess exits rc=1 → token never
    # written. Hard to debug because the auto-driver also reports
    # "stuck on consent summary" misleadingly. Validated 2026-05-11:
    # the second create_burner run on the same Chrome failed exactly
    # this way; clearing the page makes the subprocess see only the
    # new state.
    #
    # We do this in TWO places:
    #   (1) Park OUR page on about:blank (no-op if it's already blank).
    #   (2) Walk EVERY page in EVERY context and either close or
    #       redirect any tab still on a localhost:8089/* URL — those
    #       are leftovers from a prior OAuth attempt and chrome will
    #       refetch them when our new server binds the port.
    try:
        page.goto("about:blank", wait_until="commit", timeout=8000)
        time.sleep(0.5)
    except Exception as e:
        print(f"  ⚠ couldn't park page on about:blank ({type(e).__name__}: {e})", flush=True)
    try:
        browser = page.context.browser
        ctxs = browser.contexts if browser is not None else [page.context]
        cleared = 0
        for ctx in ctxs:
            for stale in list(ctx.pages):
                if stale is page:
                    continue
                try:
                    surl = stale.url or ""
                except Exception:
                    surl = ""
                if "localhost:8089" in surl or "127.0.0.1:8089" in surl:
                    try:
                        # Navigate to about:blank first so closing
                        # doesn't leave a queued reload, then close.
                        stale.goto("about:blank", wait_until="commit", timeout=4000)
                    except Exception:
                        pass
                    try:
                        stale.close(run_before_unload=False)
                        cleared += 1
                    except Exception:
                        pass
        if cleared:
            print(f"  → closed {cleared} stale localhost:8089 tab(s) "
                  "to prevent CSRF state mismatch", flush=True)
    except Exception as e:
        print(f"  ⚠ stale-tab sweep failed ({type(e).__name__}: {e}) — proceeding", flush=True)

    auth_log = work_dir / "oauth.log"
    auth_log_fh = auth_log.open("w")
    # IMPORTANT: pass YTFACTORY_OAUTH_OPEN_BROWSER=0 so the subprocess'
    # ``flow.run_local_server`` does NOT also open the auth URL in the
    # system default browser. We're going to ``page.goto(auth_url)`` it
    # in the attached Chrome ourselves a moment later — without this
    # gate, macOS LaunchServices opens the URL in whatever's pinned as
    # default (Edge, Safari, Firefox, …) and the user gets a confusing
    # split flow where the consent screen is in one browser and our
    # auto-driver is poking at a separate Chrome window. Validated
    # 2026-05-11: the dashboard's bulk-create flow opened OAuth in
    # Edge for a user whose default wasn't Chrome.
    sub_env = os.environ.copy()
    sub_env["YTFACTORY_OAUTH_OPEN_BROWSER"] = "0"
    # Belt-and-suspenders: also clear YTFACTORY_CHROME_PROFILE_DIR so
    # the subprocess can't accidentally take the "pre-launch a Chrome
    # against this udd" path either — we already HAVE the right Chrome
    # attached and pointed at the URL.
    sub_env.pop("YTFACTORY_CHROME_PROFILE_DIR", None)
    proc = subprocess.Popen(
        [
            ".venv/bin/python", "-u",
            "-c",
            "from pipeline.upload.upload import authenticate; authenticate(%r, interactive=True)" % slug,
        ],
        cwd="/Users/rohit/ytFactory",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=sub_env,
    )
    auth_url: str | None = None
    deadline = time.time() + 60.0  # only wait for URL line
    try:
        while time.time() < deadline and proc.poll() is None:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                time.sleep(0.1)
                continue
            auth_log_fh.write(line)
            auth_log_fh.flush()
            print(f"  [auth] {line.rstrip()}", flush=True)
            m = OAUTH_URL_RE.search(line)
            if m:
                auth_url = m.group(0).rstrip(",.;)\\")
                break
        if not auth_url:
            print("  ✗ couldn't extract auth URL from authenticate() output", flush=True)
            try: proc.terminate(); proc.wait(timeout=5)
            except Exception: proc.kill()
            return False

        print(f"  → loading auth URL in attached Chrome:", flush=True)
        print(f"     {auth_url}", flush=True)
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            # Google sometimes returns a non-2xx during the OAuth dance
            # but the page still renders — soft-warn and continue.
            print(f"  ⚠ goto to auth URL: {type(e).__name__}: {e}", flush=True)
        time.sleep(2.0)
        _shoot(page, work_dir, "08-oauth-loaded")

        print(f"\n  ── Driving Google consent automatically… ──", flush=True)
        consent_ok = _drive_oauth_consent(page, work_dir, host_email=host_email)
        if not consent_ok:
            print(f"\n  Auto-consent stuck. Complete it manually in the Chrome window.\n", flush=True)

        # Stream subprocess output as user clicks through.
        end = time.time() + timeout_s
        while time.time() < end and proc.poll() is None:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                auth_log_fh.write(line); auth_log_fh.flush()
                print(f"  [auth] {line.rstrip()}", flush=True)
            else:
                time.sleep(0.5)

        rc = proc.poll()
        if rc is None:
            print(f"  ✗ OAuth subprocess timed out after {timeout_s}s", flush=True)
            try: proc.terminate(); proc.wait(timeout=5)
            except Exception: proc.kill()
            return False
        if rc != 0:
            print(f"  ✗ OAuth subprocess exited rc={rc}; see {auth_log}", flush=True)
            return False

        # Drain anything left and confirm the token file.
        for line in (proc.stdout.readlines() if proc.stdout else []):
            auth_log_fh.write(line)
            print(f"  [auth] {line.rstrip()}", flush=True)
        token_path = TOKEN_DIR / f"youtube_token_{slug}.json"
        if token_path.exists():
            print(f"  ✓ OAuth complete — token at {token_path}", flush=True)
            _shoot(page, work_dir, "09-oauth-done")
            return True
        print(f"  ✗ OAuth subprocess exited 0 but token file missing: {token_path}", flush=True)
        return False
    finally:
        auth_log_fh.close()


# ── core flow ─────────────────────────────────────────────────────────────


def drive_create(
    page,
    *,
    work_dir: pathlib.Path,
    display_name: str,
    handle: str,
    personal_handle: str,
    email: str,
    dry_run: bool,
) -> dict:
    """Walk the brand-account create flow. Returns a result dict."""
    result: dict = {
        "display_name": display_name,
        "handle_typed": handle,
        "handle_actual": handle,
        "step_reached": "init",
        "channel_id": None,
        "final_url": None,
        "errors": [],
    }

    _open_create_modal(page, work_dir, personal_handle=personal_handle, email=email)
    result["step_reached"] = "modal_open"

    _fill_modal(page, work_dir, display_name=display_name, handle=handle)
    _shoot(page, work_dir, "03-modal-filled")
    result["step_reached"] = "modal_filled"

    create_btn = _create_button(page)
    if not _wait_button_enabled(create_btn, timeout_s=4.0):
        # Likely a handle collision. YouTube's UI shows a suggestion;
        # click it to fix the field.
        applied = _resolve_handle_collision(page, work_dir)
        if applied:
            result["handle_actual"] = applied
            create_btn = _create_button(page)
        if not _wait_button_enabled(create_btn, timeout_s=12.0):
            _dump_snapshot(page, work_dir, "04-create-btn-disabled")
            error_text = _dialog(page).locator("text=/(?:not available|failed)/i").first
            try:
                msg = (error_text.text_content() or "").strip() if error_text.count() else ""
            except Exception:
                msg = ""
            result["errors"].append(
                f"Create-channel button stayed disabled. Dialog says: {msg!r}"
            )
            return result

    if dry_run:
        print("  ⏸ --dry-run: stopping before final Create-channel click.", flush=True)
        return result

    print("  → click Create channel", flush=True)
    try:
        create_btn.click(timeout=8000)
    except Exception as e:
        result["errors"].append(f"Create-channel click {type(e).__name__}: {e}")
        _shoot(page, work_dir, "05-click-failed")
        return result
    result["step_reached"] = "create_clicked"
    time.sleep(3.0)
    _shoot(page, work_dir, "05-after-create-click")

    # Hard-fail signal: dialog stays open with red error text.
    err = _dialog(page).locator("text=/Failed to create channel/i").first
    try:
        if err.count() and err.is_visible(timeout=1000):
            txt = (err.text_content() or "").strip()
            result["errors"].append(f"YouTube refused: {txt!r}")
            _shoot(page, work_dir, "06-create-refused")
            _dump_snapshot(page, work_dir, "06-create-refused")
            return result
    except Exception:
        pass

    channel_id = _wait_for_channel_id(page, work_dir, timeout_s=60.0)
    result["final_url"] = page.url
    # Belt-and-suspenders: if the polling missed the redirect but
    # page.url settled afterward, recover the UC ID here.
    if not channel_id:
        m = UC_ID_RE.search(result["final_url"])
        if m:
            channel_id = m.group(1)
            print(f"  ✓ recovered channel ID from final_url: {channel_id}", flush=True)
    if channel_id:
        result["channel_id"] = channel_id
        result["step_reached"] = "channel_created"
        print(f"  ✓ channel created — {channel_id}", flush=True)
    else:
        result["errors"].append("no UC<id> visible 45s after Create click")
    _shoot(page, work_dir, "07-final")
    return result


# ── post-create registration ──────────────────────────────────────────────

def register_burner(slug: str, *, channel_id: str, title: str, email: str) -> None:
    """Wire the new burner into channel_ids.json + profile_map.json so
    ``pipeline.burner_engage`` discovers it on the next worker run.

    Idempotent — re-running with the same args is a no-op overwrite.
    """
    ids = _load_json(CHANNEL_IDS_PATH, {})
    ids[slug] = {
        "channel_id": channel_id,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
    }
    _save_json(CHANNEL_IDS_PATH, ids)

    profile_map = _load_json(PROFILE_MAP_PATH, {})
    rec = profile_map.get(slug, {}) if isinstance(profile_map.get(slug), dict) else {}
    rec["email"] = email
    profile_map[slug] = rec
    _save_json(PROFILE_MAP_PATH, profile_map)


# ── entry point ───────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> dict:
    if args.display_name:
        display_name = args.display_name
    elif args.name_style == "realistic":
        display_name = realistic_burner_name()
    else:
        display_name = random_burner_name()
    slug = args.slug or derive_slug(display_name)
    if not slug:
        raise SystemExit(
            f"can't derive slug from display-name {display_name!r}: "
            f"strip-to-alphanumeric is empty. Pass --slug explicitly."
        )
    handle = args.handle or derive_handle(slug)

    profile = resolve_profile(args.email, args.profile)
    print(f"[create-burner] account: {args.email} → {profile}", flush=True)
    print(f"[create-burner] slug:    {slug}", flush=True)
    print(f"[create-burner] name:    {display_name!r}", flush=True)
    print(f"[create-burner] handle:  {handle!r} (YouTube may suggest an alt if taken)", flush=True)

    assert_no_collision(slug, allow_existing=args.allow_existing)

    work_dir = (args.work_dir / slug).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"[create-burner] work:    {work_dir}", flush=True)

    # Coexistence path: if a Chrome-Debug is already running on this
    # profile, attach to its CDP port instead of launching a new one.
    # This:
    #   * Avoids the SingletonLock conflict that bricks parallel launches.
    #   * Preserves any live YouTube session the user signed into in
    #     that Chrome (the destructive cookie bridge would wipe it).
    #   * Means we never need to kill the user's Chrome.
    proc = None
    attached = False
    sibling_udd = pathlib.Path.home() / "Library" / "Application Support" / "Google" / f"Chrome-Debug-{profile.replace(' ', '_').lower()}"
    if not args.force_fresh:
        # Try canonical Chrome-Debug first (the one the user might be
        # using interactively), then the per-profile sibling we'd
        # launch ourselves on a fresh run.
        for udd_candidate in (None, str(sibling_udd)):
            existing = find_running_chrome_debug(profile, user_data_dir=udd_candidate)
            if existing is not None:
                attach_pid, port = existing
                udd_label = "canonical Chrome-Debug" if udd_candidate is None else f"sibling {sibling_udd.name}"
                print(f"[create-burner] attaching to running {udd_label} PID={attach_pid} CDP=ws://127.0.0.1:{port}", flush=True)
                attached = True
                break

    if not attached:
        # Per-profile sibling user-data-dir so this run can coexist with
        # OTHER Chrome-Debug instances on different profiles. The
        # canonical Chrome-Debug user-data-dir holds a SingletonLock
        # per-profile-N — using a sibling avoids the global "Chrome
        # already running" check tripping when we're targeting a
        # different account altogether.
        bridge_cookies(profile, dst_dir=sibling_udd)
        _clear_singleton(sibling_udd)
        proc, port = launch_chrome_for(profile, work_dir=work_dir, user_data_dir=sibling_udd)
        print(f"[create-burner] chrome:  PID={proc.pid} CDP=ws://127.0.0.1:{port} (udd={sibling_udd.name})", flush=True)

    from playwright.sync_api import sync_playwright
    result: dict = {"profile": profile, "email": args.email, "slug": slug, "errors": []}
    # Track whether OAuth ran INSIDE the lifecycle below — used by the
    # post-loop reporting so we don't double-fire it.
    oauth_ok: bool | None = None
    oauth_ran_inline = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]  # signed-in context — DO NOT new_context()
            page = ctx.new_page()
            page.set_default_timeout(15000)
            try:
                wizard = drive_create(
                    page,
                    work_dir=work_dir,
                    display_name=display_name,
                    handle=handle,
                    personal_handle=args.personal_handle,
                    email=args.email,
                    dry_run=args.dry_run,
                )
                result.update(wizard)
            except Exception as e:
                result["errors"].append(f"drive_create {type(e).__name__}: {e}")
                _shoot(page, work_dir, "99-error")
                _dump_snapshot(page, work_dir, "99-error")
                raise

            # ── Inline OAuth (Chrome is still alive here) ──────────────
            # Run OAuth INSIDE the same playwright session as the wizard
            # so it works even on --force-fresh without --keep-open
            # (which is exactly what the dashboard's bulk-create flow
            # uses to avoid stale-localhost:8089 tab pollution between
            # tasks). Pre-2026-05-11 OAuth was deferred to a SECOND
            # ``with sync_playwright()`` block AFTER the chrome teardown
            # finally; that path required ``attached or args.keep_open``
            # because otherwise the chrome was already dead by then.
            # Bulk-create can't use either of those: attaching to the
            # user's interactive Chrome accumulates stale OAuth tabs
            # across runs (CSRF state mismatch on task N), and
            # --keep-open leaves the udd locked so task N+1's
            # bridge_cookies fails. The fix: run OAuth here while we
            # still own the chrome.
            if (result.get("channel_id") and not args.no_oauth
                    and not args.dry_run):
                # Register first so the burner is discoverable even if
                # OAuth fails halfway (we can re-OAuth manually later).
                if not args.no_register:
                    register_burner(
                        slug,
                        channel_id=result["channel_id"],
                        title=display_name,
                        email=args.email,
                    )
                    print(
                        f"[create-burner] registered in "
                        f"{CHANNEL_IDS_PATH.name} + {PROFILE_MAP_PATH.name}",
                        flush=True,
                    )
                oauth_ran_inline = True
                page2 = ctx.new_page()
                page2.set_default_timeout(15000)
                try:
                    oauth_ok = oauth_in_attached_chrome(
                        slug, page2, work_dir, host_email=args.email,
                    )
                    result["oauth_complete"] = bool(oauth_ok)
                except Exception as e:
                    print(
                        f"[create-burner] ⚠ OAuth failed: "
                        f"{type(e).__name__}: {e}",
                        flush=True,
                    )
                    result["oauth_complete"] = False
                    oauth_ok = False
                finally:
                    try:
                        page2.close()
                    except Exception:
                        pass

            # ── Chrome teardown bookkeeping (post-OAuth) ───────────────
            if attached:
                # We didn't launch this Chrome — leave it alone.
                print(f"[create-burner] leaving attached Chrome running (we didn't launch it).", flush=True)
                proc = None
            elif args.keep_open:
                # Chrome was launched with start_new_session=True
                # (per launch_chrome_for) so it's already in its own
                # POSIX session and survives our exit. Stash the CDP
                # port so a follow-up run with --attach <port> can
                # re-grab the same session.
                print(f"[create-burner] --keep-open: detached Chrome PID {proc.pid} CDP=ws://127.0.0.1:{port}", flush=True)
                (work_dir / "cdp_port.txt").write_text(str(port))
                proc = None
    finally:
        if proc is not None:
            time.sleep(1.0)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            time.sleep(1.0)

    summary_path = work_dir / "result.json"
    summary_path.write_text(json.dumps(result, indent=2))
    print(f"[create-burner] result:  {summary_path}", flush=True)

    # Register if not already done inline (covers the OAuth-skipped
    # path: --no-oauth, --dry-run, no channel_id).
    if (result.get("channel_id") and not args.no_register
            and not oauth_ran_inline):
        register_burner(
            slug,
            channel_id=result["channel_id"],
            title=display_name,
            email=args.email,
        )
        print(f"[create-burner] registered in {CHANNEL_IDS_PATH.name} + {PROFILE_MAP_PATH.name}", flush=True)
    elif result.get("channel_id") and args.no_register:
        print("[create-burner] --no-register: skipped channel_ids.json + profile_map.json updates.")

    # ── Legacy post-teardown OAuth path ─────────────────────────────────
    # Only fires when the inline OAuth above DIDN'T run AND we still
    # have a Chrome to talk to (attached / --keep-open). Kept for the
    # case where someone passes --keep-open and wants the OAuth to
    # happen against the still-alive chrome window.
    if (result.get("channel_id") and not args.no_oauth
            and not oauth_ran_inline):
        if attached or args.keep_open:
            try:
                from playwright.sync_api import sync_playwright as _sp
                with _sp() as pw2:
                    browser2 = pw2.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    ctx2 = browser2.contexts[0]
                    page2 = ctx2.new_page()
                    page2.set_default_timeout(15000)
                    oauth_ok = oauth_in_attached_chrome(slug, page2, work_dir, host_email=args.email)
                    result["oauth_complete"] = bool(oauth_ok)
            except Exception as e:
                print(f"[create-burner] ⚠ OAuth-in-attached-chrome failed: {type(e).__name__}: {e}", flush=True)
                result["oauth_complete"] = False
        else:
            print(
                "[create-burner] skipped OAuth (Chrome closed). Re-run with "
                "--keep-open OR re-run authenticate manually:\n"
                f"     /Users/rohit/ytFactory/.venv/bin/python -c "
                f'"from pipeline.upload.upload import authenticate; authenticate({slug!r}, interactive=True)"'
            )

    # Final report
    if result.get("channel_id"):
        print()
        if oauth_ok is True:
            print(f"✅ burner ready — {slug!r} channel + OAuth both done.")
        elif oauth_ok is False:
            print(f"⚠ channel ready ({slug!r}) but OAuth incomplete — re-run authenticate manually:")
            print(f"   /Users/rohit/ytFactory/.venv/bin/python -c \\")
            print(f"     \"from pipeline.upload.upload import authenticate; authenticate('{slug}', interactive=True)\"")
        else:
            print(f"✅ channel created ({slug!r}). Skipped OAuth (--no-oauth).")
        print(f"   verify discovery: .venv/bin/python -m pipeline.cross_engage.burner_engage list")

    # Re-write summary now that result has oauth_complete set.
    summary_path.write_text(json.dumps(result, indent=2))

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--display-name", default=None, help="Channel display name as shown on YouTube (≤50 chars). Random no-space name auto-generated when omitted (matches existing burner aesthetic).")
    ap.add_argument(
        "--name-style", default="random", choices=("random", "realistic"),
        help=(
            "Name generator when --display-name is omitted. "
            "'random' (default): gibberish lowercase letters matching the "
            "existing burner aesthetic. 'realistic': real-sounding screen "
            "names like mikedraft / canyon / leo07 / bluepine — used by "
            "the dashboard's bulk-create button."
        ),
    )
    ap.add_argument("--slug", default=None, help="Local identifier (lowercase alphanumeric). Derived from --display-name if omitted.")
    ap.add_argument("--handle", default=None, help="Initial @handle to try (no leading @). Defaults to slug. YouTube will suggest an alt if taken.")
    ap.add_argument("--email", default=DEFAULT_EMAIL, help=f"Google account that hosts the new brand-account channel (default: {DEFAULT_EMAIL}).")
    ap.add_argument("--all-emails", action="store_true", help="Probe EVERY signed-in Google account in real Chrome's Local State. Auto-enables --dry-run + --no-register + --no-oauth so we don't burn rate-limit. Reports which accounts can/can't create.")
    ap.add_argument("--personal-handle", default="newrtrudaj", help="The @handle of the host account's personal (non-brand) YouTube channel. Required because the 'Create a channel' CTA on /account only renders in personal-channel context. Default 'newrtrudaj' is rsinghtomar54's personal channel.")
    ap.add_argument("--profile", default=None, help="Override Chrome profile dir (e.g. 'Profile 1'); auto-resolved from --email otherwise.")
    ap.add_argument("--work-dir", type=pathlib.Path, default=DEFAULT_WORK_DIR, help="Where per-run artifacts (screenshots, snapshots, chrome.stderr, result.json) land. A subdir named <slug> is created inside.")
    ap.add_argument("--keep-open", action="store_true", help="Leave Chrome running after the wizard so you can verify the new channel in YouTube Studio.")
    ap.add_argument("--force-fresh", action="store_true", help="Always launch a new Chrome-Debug (bypass attach-to-existing). Default is to detect a running Chrome-Debug on the target profile and attach to it via CDP.")
    ap.add_argument("--no-oauth", action="store_true", help="Don't run pipeline.upload.upload.authenticate after channel-create. Default is to OAuth the new burner inside the same attached Chrome window.")
    ap.add_argument("--no-register", action="store_true", help="Don't update channel_ids.json + profile_map.json after success.")
    ap.add_argument("--allow-existing", action="store_true", help="Don't bail if the slug already has a token or channel_ids entry.")
    ap.add_argument("--dry-run", action="store_true", help="Drive everything except the final 'Create channel' click.")
    args = ap.parse_args()

    if args.display_name and len(args.display_name) > 50:
        raise SystemExit(f"display-name too long ({len(args.display_name)} > 50)")

    if args.all_emails:
        # Probe every signed-in account. Auto-enable safety flags so
        # we don't accidentally spawn brand accounts on accounts that work.
        # Each per-account run uses --force-fresh so it launches a separate
        # Chrome process per Profile (one Chrome-Debug per email is wrong
        # — they all share the same user-data-dir SingletonLock).
        args.dry_run = True
        args.no_register = True
        args.no_oauth = True
        args.allow_existing = True

        emails = sorted(profile_email_map().values())
        emails = [e for e in emails if e]
        print(f"[probe-all] testing create capability on {len(emails)} accounts (dry-run)\n", flush=True)
        results: list[dict] = []
        for i, email in enumerate(emails, 1):
            print(f"\n{'='*60}\n[{i}/{len(emails)}] probing {email}\n{'='*60}", flush=True)
            args.email = email
            args.display_name = random_burner_name()
            args.slug = derive_slug(args.display_name)
            args.handle = args.slug
            try:
                result = run(args)
                results.append({
                    "email": email,
                    "step_reached": result.get("step_reached"),
                    "errors": result.get("errors") or [],
                    # "modal_filled" + no errors == we got through the
                    # form and the Create button enabled, so this account
                    # CAN create. We just stopped before clicking.
                    "can_create": result.get("step_reached") == "modal_filled" and not result.get("errors"),
                })
            except SystemExit as e:
                results.append({"email": email, "errors": [str(e)], "can_create": False})
            except Exception as e:
                results.append({"email": email, "errors": [f"{type(e).__name__}: {e}"], "can_create": False})

        agg_path = args.work_dir / "all-emails-probe.json"
        agg_path.parent.mkdir(parents=True, exist_ok=True)
        agg_path.write_text(json.dumps(results, indent=2))
        print(f"\n\n{'='*60}\nPROBE SUMMARY\n{'='*60}")
        for r in results:
            status = "✅ can create" if r.get("can_create") else "✗ blocked"
            err = "; ".join(r.get("errors") or [])[:80]
            print(f"  {status:18s}  {r['email']:35s}  step={r.get('step_reached', '—'):20s}  {err}")
        print(f"\nfull report: {agg_path}")
        return 0

    result = run(args)
    ok = bool(result.get("channel_id")) or (args.dry_run and not result.get("errors"))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
