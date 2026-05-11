# Burner channels — what they are, how to spin one up

A **burner channel** is a YouTube brand account we own but which is NOT
in `pipeline.customization.CHANNEL_REGISTRY`. Burners exist purely to
**like / subscribe / watch our production catalog from a fresh
identity** — seeding views, watch-time and the recommendation graph
without using a production channel's reputation.

The runtime that drives engagement from a burner is
[`pipeline.burner_engage`](../pipeline/burner_engage.py); the spin-up
side is [`pipeline.create_burner_channel`](../pipeline/create_burner_channel.py).

## Where they live

All burners are **brand accounts** under one Google host account
(default `rsinghtomar54@gmail.com`, real Chrome `Profile 1`). Switch
hosts via the `--email` flag if you spread burners across accounts.

**Source of truth (2026-05-10):** the committed manifest
[`pipeline/burners.yaml`](../pipeline/burners.yaml). Every burner row
declares `slug`, `youtube_title`, `youtube_channel_id`, `google_email`.
`pipeline.channels._load_burners` parses it on import and exposes
`BURNERS`; `pipeline.cross_engage.burner_engage.list_burner_channels()`
(and therefore the `/app/burner-channels` dashboard) iterate that
tuple. **Adding / removing a burner = edit `burners.yaml` and ship.
No other file is consulted to decide whether a burner exists.**

Per-laptop runtime files still exist as caches the laptop-side
Playwright runner uses to avoid re-deriving things (token, host
profile mapping, engage progress), but they are **not** discovery
inputs:

| Surface | Path | Role |
|---|---|---|
| Burner manifest (SoT) | `pipeline/burners.yaml` | one row per burner, committed |
| OAuth token (per burner) | `~/.config/ytfactory/youtube_token_<slug>.json` | runtime cache, per laptop |
| Slug → channel_id index | `~/.config/ytfactory/channel_ids.json` | runtime cache, per laptop |
| Slug → host email map | `~/.config/ytfactory/profile_map.json` | runtime cache, per laptop |
| Engage-worker state | `data/burner_engage/<slug>.json` | per-run progress |
| Per-create artifacts | `/tmp/pw-create-burner/<slug>/` (screenshots, html dumps, oauth.log) | debug |

> **Promotion gap (open follow-up, 2026-05-10):** `register_burner()`
> in `pipeline/cross_engage/create_burner_channel.py` writes
> `channel_ids.json` + `profile_map.json` after a successful create
> but does NOT append the new row to `pipeline/burners.yaml`. Until
> someone hand-edits the YAML and redeploys, a freshly-minted burner
> won't appear in the dashboard or `pipeline.burner_engage list`. See
> `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_burner_yaml_sot_drift.md`.

The burner is "registered" — and therefore discoverable by
`pipeline.burner_engage list` — once it appears in
`pipeline/burners.yaml`. The runtime caches above are required for
the engage worker to actually drive it (no token = can't act as the
burner; no profile mapping = can't pick the right Chrome window) but
they're filled in lazily on first run.

## End-to-end create flow

```bash
# Default: random alphanumeric name (matches existing burner aesthetic
# like 'afddfdf', 'gysqzsnph', 'kjgjj'), default host account, attach
# to running Chrome-Debug if present, OAuth in same window.
.venv/bin/python -m pipeline.create_burner_channel

# Specific name (no spaces in slug):
.venv/bin/python -m pipeline.create_burner_channel --display-name cosmicdrift

# Skip the OAuth step, just create the channel:
.venv/bin/python -m pipeline.create_burner_channel --no-oauth
```

The script does, in order:

1. **Resolve Chrome profile** for `--email` via `Local State`.
2. **Detect or launch Chrome-Debug.** If a Chrome process matching
   `--user-data-dir=…/Chrome-Debug` AND `--profile-directory=Profile 1`
   is already running, **attach via CDP** (preserves any live YouTube
   session, no SingletonLock conflict). Otherwise bridge cookies from
   real Chrome, launch a fresh Chrome-Debug.
3. **Goto `/channel_switcher`** → 302s to `/account` "All channels"
   view, which renders a **+ Create a channel** tile regardless of
   which brand-account context is currently active. (Plain `/account`
   only renders the CTA in personal-channel context — `/channel_switcher`
   bypasses that.)
4. **CLICK** the `yt-button-shape a[aria-label='Create a channel']`
   element. Critical: do NOT `goto(href)` directly — the `<a>` carries
   `force-new-state="true"` which intercepts clicks via JS to open the
   modal. Direct navigation skips the handler and silently creates a
   default-named brand account (that's how the orphan brand accounts
   `newrtrudaj`, `khihfcgghdfj`, `kjgjj`, `sampleChannel1` got spawned).
5. **Wait for the "How you'll appear" dialog** (`tp-yt-paper-dialog`
   wrapping `<ytd-channel-creation-*>`).
6. **Fill the dialog inputs** by index inside the dialog scope:
   `[0]` = display name, `[1]` = handle. The Material floating-label
   pattern means the inputs have NO `id`/`placeholder`/`aria-label` —
   index inside the dialog is the most stable selector.
7. **Resolve handle collisions:** if YouTube's async availability check
   marks the handle taken, a suggestion link `<a>@<suggested></a>`
   appears under the field. Click it to apply.
8. **Wait for the dialog "Create channel" button to enable** (handle
   check is async, ~1-3 s).
9. **Click "Create channel".** Watch for two outcomes:
   * **Success** → page redirects to `/channel/<UC_ID>`. Extract the ID.
     Note the redirect can lag 30+ s; the script polls then takes
     `page.url` once more after the deadline.
   * **Refusal** → red error in dialog: "Failed to create channel.
     Please try changing your channel name and try again." This
     usually means a per-account rate-limit — wait ~24h or pick a
     different name. (The error sometimes lies — Google occasionally
     creates the channel server-side anyway. Check the brand-account
     list at `https://myaccount.google.com/brandaccounts` if in doubt.)
10. **Register** the new burner in `channel_ids.json` + `profile_map.json`.
11. **OAuth in the same Chrome window** (default; `--no-oauth` to skip).
    Spawns `pipeline.upload.authenticate(slug, interactive=True)` as a
    subprocess, scrapes the auth URL from its stdout, navigates the
    attached Chrome page to it, then waits for the local callback
    server (port 8089) to complete. **You complete the Google consent
    in the same Chrome window.**
12. Final summary printed; per-run artifacts in
    `/tmp/pw-create-burner/<slug>/` (screenshots `01..09`, `result.json`,
    `oauth.log` if OAuth was attempted).

## Coexistence — never kill Chrome

The script's pre-2026-05-09 versions did `assert_chrome_closed()` and
asked the user to kill any running Chrome. New behavior:

* `find_running_chrome_debug(profile)` scans `ps -axww` for a Chrome
  process whose argv contains both
  `--user-data-dir=…/Chrome-Debug` AND `--profile-directory=<profile>`.
* If found → recover its CDP port from sibling
  `/tmp/pw-*/chrome.stderr` files OR `/tmp/pw-*/cdp_port.txt` sidecars,
  probe `http://127.0.0.1:<port>/json/version`, attach.
* The bridge step is **destructive** (overwrites Chrome-Debug's cookie
  store with real Chrome's). Skipping it on attach preserves any
  YouTube session you signed into in that Chrome.

The user's regular Chrome (different `user-data-dir`) is never
touched — they coexist trivially.

`--force-fresh` opts out of attach mode (always launch new Chrome-Debug).

## OAuth in the same Chrome — the why

Pre-2026-05-09 the user had to:
1. Run the create-channel script
2. Manually run `python -c "from pipeline.upload import authenticate; ..."`
3. Manually copy the auth URL from the terminal
4. Manually paste it into a browser signed into the right account
5. Manually click through Google's consent
6. Wait for the localhost:8089 callback

The current script collapses 1+2+3+4 into one command. The user still
clicks through the Google consent (account picker + "Choose YouTube
channel" picker for brand accounts + "Allow") because Google's consent
screens have anti-automation heuristics. But it happens IN THE SAME
WINDOW — no second-browser context switch.

OAuth runs after registration so a partial result (channel created,
OAuth incomplete) leaves the burner discoverable but flagged as
`profile_known=False / has_token=False` in `burner_engage list`.

## Failure modes & recovery

| Symptom | Likely cause | Fix |
|---|---|---|
| "Choose an account" picker, all rows say "Signed out" | Real Chrome's Profile is signed out of YouTube | Sign into youtube.com in real Chrome on the matching Profile, close Chrome, retry. Or just sign in INSIDE the attached Chrome window — the script will use that session via attach mode. |
| `couldn't find the 'Create a channel' button` | YouTube redrew the `/account` "All channels" view | Inspect `/tmp/pw-create-burner/<slug>/01-no-create-button.html`; update `CREATE_CHANNEL_BTN_SEL` constant. |
| `modal 'How you'll appear' didn't appear` | UI drift; click landed in wrong place | Inspect `02-no-modal.html`; adjust the wait-for-text selector. |
| `Failed to create channel. Please try changing your channel name` | Per-account rate-limit OR name was used in a failed attempt today | Wait 24h OR pick a different name. Sometimes the channel gets created anyway — check `https://myaccount.google.com/brandaccounts`. |
| OAuth subprocess times out | Port 8089 already in use OR you took >10 min on the consent screen | `lsof -nP -iTCP:8089` to find the conflicting process. Re-run authenticate manually. |
| OAuth completes but no `refresh_token` | Google deduped issuance (you've consented before) | Visit `https://myaccount.google.com/connections`, remove the OAuth app, re-run. |
| **Chrome windows opening unexpectedly during unrelated work** | The launchd-managed `pipeline.laptop_agent` long-polls the cloud control plane and spawns `burner_engage` workers that launch fresh-profile Chrome on their own | `tail /tmp/ytfactory-laptop-agent.err` — every spawn logs `executing task <id> kind=burner_engage`. To stop: `launchctl unload ~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist`. NOT caused by code that's "currently running" — the agent is autonomous. (Surfaced 2026-05-10.) |

## Cleaning up unwanted brand accounts

YouTube's brand-account list at
[`https://myaccount.google.com/brandaccounts`](https://myaccount.google.com/brandaccounts)
shows every brand account on the host Google account. For
rsinghtomar54 as of 2026-05-09 we have several stale ones from
earlier scripted attempts (`khihfcgghdfj`, `kjgjj`, `sampleChannel1`,
`newrtrudaj`) that were spawned by the old "goto `/create_channel`"
path. Click into any → "Delete account" to nuke (irreversible).

---

## Cross-engagement from a burner (2026-05-09)

> **Architecture refresh (2026-05-11).** The cross-engage worker no
> longer uses the legacy "open one tab → engage → next" pattern.
> It now opens all 58 catalog tabs upfront in Phase 0 and cycles
> through them in a unified act+verify+watch loop in Phase 1, with
> four user-selectable engagement modes
> (`subscribe_only` / `like_subscribe` / `like_subscribe_view` /
> `complete`). UI surface (later 2026-05-11): the burner row
> exposes a dedicated **Subscribe** button (one-click hot path →
> `subscribe_only`) next to a **Cross-engage ▾** dropdown that
> covers the three higher-intensity modes. See:
>
> - [`docs/all_tabs_cycle_pattern.md`](all_tabs_cycle_pattern.md) — the architecture.
> - [`docs/many_chrome_tabs.md`](many_chrome_tabs.md) — Chrome flags + goto policy needed for 50+ simultaneous tabs.
> - [`docs/cross_engage_cloud_v2.md`](cross_engage_cloud_v2.md) § Engagement modes — the four backend modes + the split UI surface (Subscribe button + 3-option dropdown).
> - [`docs/web_next_clickable_row_pattern.md`](web_next_clickable_row_pattern.md) — every interaction on a burner row (body click, Subscribe, Cross-engage, View live, Last run, Stop, AND the error path of any action) opens the right-rail engage drawer. Added 2026-05-11 after Stop + row-body were inert.
>
> The text below documents the older single-burner-attached script
> (`pipeline.cross_engage_burner_attached`) which is still the
> reference for `--all-burners` cycle runs and for one-off CLI
> debugging, but the dashboard's Subscribe / Cross-engage controls
> use the new cycle pattern via
> `pipeline.cross_engage.burner_engage.run`.

### Page-level dashboard actions (2026-05-11)

Above the per-burner row list, the `/app/burner-channels` page
exposes two **page-level** fan-out buttons:

* **"Subscribe All <N>"** — fans `subscribe_only` engage to every
  *eligible* burner. Eligible = `profile_known: true` AND not
  currently `running`. The badge shows live eligible count; the
  button disables when 0. Backend route:
  `POST /api/burner_channels/subscribe_all_burners`. Each eligible
  burner gets its own `BURNER_ENGAGE` task (cloud) or detached
  `subprocess.Popen` (laptop dev) with `mode=subscribe_only`.
  Per-burner skip reasons are reported in the response toast
  (`already_running`, `no_profile_mapping`, `enqueue_failed`).

* **"Create 50 channels"** — prompts for count (default 50, hard-cap
  100), then enqueues N `CREATE_BURNER` tasks for the laptop agent
  (cloud) or fires N detached `create_burner_channel` subprocesses
  (laptop dev). Backend route:
  `POST /api/burner_channels/create_bulk` with body
  `{count, email?, oauth?}`. The hard cap exists because Google
  rate-limits brand-account creation at ~5-10 successful creates /
  24h / host email (see "Refusal" block above) — accepting more
  than 100 would just stack failed `CREATE_BURNER` tasks in the
  queue. The expected operating outcome is "head succeeds, tail
  fails until tomorrow"; that's normal.

Both routes are idempotent at the per-burner level (a burner that's
already running is reported under `skipped` instead of getting a
duplicate task). Subscribe-All composes with the per-row Subscribe
button — clicking either path while the other is in flight is a no-op
for the already-running burners.

> **2026-05-12 — bulk-fan-out throughput.** The laptop agent now
> processes `BURNER_ENGAGE` and `CREATE_BURNER` tasks via N worker
> threads (default 5) with per-kind `BoundedSemaphore` caps
> (`burner_engage`=4, `create_burner`=1). A 49-burner Subscribe-All
> burst drains in ~minutes instead of the prior ~10 hours of strictly-
> serial leasing. Caps are tunable via
> `YTFACTORY_AGENT_WORKERS` / `YTFACTORY_AGENT_CAP_<KIND>`. Background:
> [`docs/laptop_agent_cloud_contract.md` § Drift flavour 4](./laptop_agent_cloud_contract.md#drift-flavour-4--single-threaded-agent--bulk-fan-out-is-hours-not-seconds-2026-05-12).
> If the queue ever feels stuck, the diagnostic recipe lives in that
> same doc's "Diagnostic recipes" section — query Firestore directly
> for `(status, kind)` counts before reading the agent log.

`CREATE_BURNER` is a new `TaskKind` (Chrome-bound, laptop-only —
never migrated to Cloud Run because `pipeline.cross_engage.create_burner_channel`
needs a real desktop Chrome with the host Google account signed in).
The laptop-side handler lives at
`pipeline/laptop_agent.py::_exec_create_burner` and threads payload
keys (`email`, `display_name`, `slug`, `oauth=False`) through to the
CLI flags.

**Promotion gap still open** — see the warning earlier: a freshly
created burner won't appear in the dashboard until somebody hand-edits
`pipeline/burners.yaml`. The bulk-create button doesn't fix that gap;
it just spins channels up faster.

[`pipeline/cross_engage_burner_attached.py`](../pipeline/cross_engage_burner_attached.py)
drives a burner brand-account through the production catalog —
`like` + `subscribe to source channel` per video — with the same
coexistence patterns:

```bash
.venv/bin/python -m pipeline.cross_engage_burner_attached --slug zgsbhqszdheo
# → engages with all 27 production videos, --limit N for smoke tests
```

### Key differences from `pipeline.burner_engage.run`

The legacy engage daemon (`pipeline.burner_engage.run`) **always
launches a fresh Chrome-Debug** in the canonical user-data-dir AND
**tears it down at the end**. That blocks the user's parallel work
in their main Chrome-Debug window AND wipes any live YouTube session
each launch (via destructive cookie bridge).

The attached variant:

1. **Runs in a sibling user-data-dir** (`Chrome-Debug-engage`) —
   completely separate Chrome process from the user's main window.
   No SingletonLock conflict, no shared focus, no shared cookie SQLite.
2. **Clones the live profile** from canonical Chrome-Debug → engage
   user-data-dir at startup (covers avatar, channel registry, settings).
3. **Harvests live cookies via CDP** from the running canonical
   Chrome-Debug — read-only, no tabs opened in user's window — and
   injects via the engage Chrome's CDP. Sidesteps the locked SQLite
   problem on the source.
4. **Switches active YouTube context to the burner** in the engage
   Chrome via avatar → "Switch account" → `ytd-account-item-renderer:has(
   yt-formatted-string#channel-title:text-is(<title>))`. That selector
   targets the row CONTAINER (not inner text spans which aren't click
   targets — that was a 2026-05-09 bug). Verified by polling
   `studio.youtube.com → /channel/<UC>` redirect via `wait_for_url`.
5. **Engages each catalog video** in its own `ctx.new_page()` (fresh
   page per video — re-using a page after the channel switcher's
   navigation gives `aria-pressed=null` for like/subscribe buttons
   indefinitely). Uses `_probe_like` / `_probe_subscribe` selectors
   from `pipeline.cross_engage_via_playwright` with a 4-attempt retry
   loop (YouTube's SPA hydrates these buttons async after `domcontentloaded`).
6. **Never kills any Chrome.** The engage Chrome stays up so the next
   `--limit N` invocation attaches to it instantly.

### Shorts engagement — partial 2026-05-10 (cloud-flow)

The cloud-driven flow (see
[`docs/cross_engage_cloud_v2.md`](cross_engage_cloud_v2.md))
deliberately uses the `/shorts/<id>` URL form — the Shorts player
auto-loops indefinitely in the same tab, which compounds the
Phase-2 tab-cycle watch-time signal vs the regular `/watch` player
that stops at video end.

What works on Shorts URLs:

- ✅ Brand-switch + verify (via avatar menu + the
  `/account`-based `_read_active_uc`)
- ✅ Tab open + auto-loop watch-time accumulation
- ✅ Phase-2 tab-cycle focus rotation

What's still broken on Shorts URLs:

- ❌ `_probe_like` / `_probe_subscribe` clicks — selectors target
  the `/watch` player DOM (`button#segmented-like-button` style).
  Windowed Chrome masks this through animation/timing leniency the
  headless renderer skips; headless mode exposes the gap
  immediately. P3.5 follow-up: separate Shorts-DOM probe.

### Brand-account switch — mandatory before engagement (2026-05-10)

`burner_engage.run()` MUST call
[`switch_to_burner_brand`](../pipeline/cross_engage/cross_engage_burner_attached.py)
right after Chrome attached and before the engage loop. One Google
account hosts several burners (rs54 hosts afddfdf, ajfsbqe, axkxlwv,
cbxqzlyivk; rs3011 hosts ~10), and YouTube tracks the active brand
via cookies — without an explicit switch the Like/Subscribe lands on
whichever brand was last selected. Live observation 2026-05-10 22:18:
the pre-switch run engaged as `UCxClGoGmVMnkC8ytTOMvQKQ` (a sibling
burner) instead of the requested `UCwKxho5ZLIgoH52eEun8T7g` (afddfdf).

`burner_engage.run` hard-fails (`return 1`, phase=`failed`) if the
switch can't be verified — engaging from the wrong identity is worse
than failing visibly.

### Active-brand verifier — `/account` page, not Studio (2026-05-10)

`_read_active_uc` originally probed `studio.youtube.com` for a
`/channel/UC…` redirect. That works windowed but returns `None` in
headless Chrome (Studio's SPA loads but never initialises the
channel context). New fallback: hit
`https://www.youtube.com/account` and take the most-frequent
`/channel/(UC…)` from its HTML (every link on the account page points
at the active brand). Works headless AND windowed, so it also
robustifies the windowed path against Studio first-run modals or
transient redirects.

### Why we abandoned the "new window via CDP" approach

`Target.createTarget` with `newWindow=True` (CDP spec) should pop a
separate top-level window in the same browser process. Validated
2026-05-09: in current Chrome (v147+) it actually opens a TAB in the
existing window — apparently Chrome ignores the `newWindow` hint when
there's an active focused window of the same profile. So we went back
to the separate-process approach (different `--user-data-dir`) which
gives true visual isolation.

---

## Brand-account switch verification — fresh-tab requirement (2026-05-09)

After clicking a brand-account row in avatar → Switch account, the
**same tab** that did the click reads the OLD active channel context
indefinitely from `studio.youtube.com/` redirects. Studio's SPA caches
the channel-id in tab-local state and never re-fetches even when the
server-side context HAS switched. **Always open a fresh
`ctx.new_page()` to verify the switch took effect.**

This bug stung the first `--all-burners` runs: burner #2 onwards saw
"switch verification failed" forever because the verify check ran on
the same page that did the click. Fixed in
`pipeline/cross_engage_burner_attached.py::switch_to_burner_brand`
by wrapping the studio-redirect probe in an inner function that
spins up + closes its own Page.

Same pattern applies to any brand-account switch verification across
the codebase — `pipeline.upload_via_playwright` and
`pipeline.cross_engage_via_playwright` switch to specific brand
accounts before liking; if/when they grow a "verify the right brand
is active" guard, they should use the fresh-tab pattern.

## Per-PID CDP port discovery — `lsof` open-files trick (2026-05-09)

`find_running_chrome_debug` used to scan `/tmp/pw-*/chrome.stderr`
files for any alive `ws://127.0.0.1:<PORT>` and return the first hit
that responded to `/json/version`. That conflated ports across
different Chrome instances when multiple stderr files existed —
returned engage Chrome PID 69932 paired with port 58780 belonging to
canonical Chrome PID 46010 (the user's match window). Almost engaged
through the user's window.

The robust fix: `lsof -p <PID>` lists open file descriptors for that
specific PID. Chrome holds its `chrome.stderr` open as fd 2; that
file's contents have the right port for THAT instance. Authoritative
because per-PID-scoped:

```python
def _cdp_port_for_pid(pid: int) -> int | None:
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if "chrome.stderr" in line:
            stderr_path = line.split(None, 8)[-1].strip()
            text = pathlib.Path(stderr_path).read_text(errors="replace")
            m = re.search(r"ws://127\.0\.0\.1:(\d+)", text)
            return int(m.group(1)) if m else None
    return None
```

The `--user-data-dir` argv match also needs to be a strict-token
check (trailing space) — substring match made `Chrome-Debug` match
Chrome processes actually using `Chrome-Debug-engage`. Both fixes
shipped together as a single PR-equivalent edit.

## --all-burners cycle (2026-05-09)

`pipeline/cross_engage_burner_attached.py --all-burners` cycles every
registered burner sequentially, switching the active YouTube context
between each and engaging with the catalog. Validated 2026-05-09 with
4 burners (afddfdf, dfuvmnktx, gysqzsnph, zgsbhqszdheo) × 3 catalog
videos = 12 engagement attempts; 4/4 burners completed switch + engage
clean. The unauth'd burners (no OAuth token, only created via
`pipeline.create_burner_channel`) work too — pure-UI engagement is
token-free.

### Subscribe-only fast path (`--no-like`, 2026-05-11)

When `--no-like` is set (and `--no-subscribe` is not), the cycle
auto-takes a fast path that skips the per-video `/watch` round-trip
entirely and visits each unique source channel's
`https://www.youtube.com/channel/<UC>` page directly. ~5-8× faster
per burner because channel pages are far lighter than watch pages
(no embedded player, no related-videos hydration). Same
`_probe_subscribe` selector lane works on both. Channel-id lookup
uses `~/.config/ytfactory/channel_ids.json`; channels missing from
that registry are warned + skipped.

See [`docs/cross_channel_engagement.md` § Subscribe-only fast path](cross_channel_engagement.md#subscribe-only-fast-path---no-like-2026-05-11)
for the full design + the table comparing old per-video vs new
fast-path wall time. The cloud-side worker
(`pipeline.cross_engage.burner_engage.MODE_SUBSCRIBE_ONLY`) already
does the dedupe half (one watch URL per channel instead of per video,
2026-05-11) — adopting the channel-direct trick there is a
non-blocking follow-up.

---

## YouTube's "Get advanced features" phone-verify gate (2026-05-09)

After roughly **1–2 brand-account creates per Google account on a
trust-fresh profile**, YouTube starts gating further creates behind
phone verification. Symptoms:

* The "+ Create a channel" button still appears on `/account` (so my
  CTA detector says "find the create button" worked).
* Clicking it pops a **`<tp-yt-paper-dialog>`** with the heading
  **"Get advanced features"** and a **"Verify"** button (NOT the
  "How you'll appear" name-input modal).
* The verify flow is SMS or voice OTP — not automatable.

`pipeline.create_burner_channel::_open_create_modal` detects this and
exits with a clear message + screenshot pointing the user at the
modal. Real-world rate observed 2026-05-09:

| Account | Burners ship before gate fires |
|---|---|
| `rsinghtomar54@gmail.com` | ≥25 (no gate seen yet — long-trusted account) |
| `rsinghtomar3011@gmail.com` | ≥23 (no gate seen yet) |
| `rohit30.iitkgp@gmail.com` | 1 burner (`xmxemnbbwpr`), gate fired immediately on 2nd attempt |
| `rsinghtomar30@gmail.com` | 0 (gate up before any create) |
| `sanimated219@gmail.com` | 0 (gate up before any create) |
| `rohittomar@docx.co.in` | 0 — Workspace admin block ("not yet eligible") |

The gate is **per-Google-account, not per-day**. Once flipped on, only
SMS/voice OTP unblocks it (one-time, then back to ~unlimited).

## Workspace accounts are hard-blocked (2026-05-09)

`rohittomar@docx.co.in` rendered **"This account is not yet eligible
to use YouTube"** on `/account`. That's a Google Workspace org-policy
block — admin must enable YouTube for the org. No script can bypass.
Skip this email entirely in `--all-emails` probes.

## Per-profile sibling user-data-dir for parallel host runs (2026-05-09)

The legacy `assert_chrome_closed()` guard refused to run if any Chrome
was alive — even another instance on a totally different profile.
Replaced with **per-profile sibling user-data-dirs**:
``Chrome-Debug-profile_<N>`` per Google account. Each Profile-N gets
its own SingletonLock domain, so 6 accounts can have 6 Chrome instances
running simultaneously. Validated 2026-05-09 with 7 concurrent Chromes
(canonical Profile 1 + 5 sibling-profile Chromes + 1 engage Chrome).

Pattern in `pipeline/create_burner_channel.py::run`:
```python
sibling_udd = pathlib.Path.home() / "Library/Application Support/Google" / f"Chrome-Debug-{profile.replace(' ', '_').lower()}"
bridge_cookies(profile, dst_dir=sibling_udd)
launch_chrome_for(profile, work_dir=work_dir, user_data_dir=sibling_udd)
```

The attach-detector also tries the sibling-udd in addition to canonical
when looking for an already-running Chrome to coexist with.

## "Select a channel" first-load modal (2026-05-09)

Accounts with multiple brand accounts but no recorded default channel
show a **`<ytd-channel-switcher-renderer>`** modal on first navigation
to YouTube — blocks all interaction until you pick one. Selector to
dismiss:

```python
page.locator("ytd-channel-switcher-renderer ytd-account-item-renderer").first.click()
```

Picking the first row is fine — we navigate to the right brand-account
context via the channel-switcher in the next step regardless. Affects
rs30, sanimated219 on initial Chrome-Debug launch.

## Account-chooser interstitial — must click the row (2026-05-09)

`accounts.google.com/v3/signin/accountchooser` is a one-shot
interstitial that needs a CLICK on the matching email row to advance —
it doesn't auto-redirect. Different from `accounts.google.com/v3/signin`
(real password challenge — unrecoverable). My script previously bailed
on both as "redirected to sign-in"; now distinguishes them and
auto-clicks the matching email row in accountchooser. Validated on
rs3011 — got past the interstitial and continued to a clean create.

## Create-channel button is `<a>` OR `<button>` (2026-05-09)

`yt-button-shape a[aria-label='Create a channel']` matches some
accounts; `yt-button-shape button[aria-label='Create a channel']`
matches others (rs30 specifically). Use Playwright `:is()` to cover
both:

```
CREATE_CHANNEL_BTN_SEL = "yt-button-shape :is(a, button)[aria-label='Create a channel']"
```

## UC-ID extraction also catches `signin_prompt?next=...` (2026-05-09)

YouTube sometimes returns to a `youtube.com/signin_prompt?app=desktop&next=https%3A%2F%2Fwww.youtube.com%2Fchannel%2FUC...`
URL after a successful create — the channel WAS created (UC ID is in
`next=`) but a sign-in interstitial pre-empted the redirect. Updated
regex catches it:

```python
UC_ID_RE = re.compile(r"(?:/channel/|next=[^\"]*?(?:/|%2F)channel(?:/|%2F))(UC[A-Za-z0-9_-]{20,})")
```

Recovered `eujeurknt` (UCiiYWW5Yz-xkIdaqU1oIlSQ) on rs54 with this
fix — would have been lost as a "no UC visible 45s after Create"
failure otherwise.
