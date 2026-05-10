# /upload-via-playwright — learnings

## Day 0 (2026-05-08) — proven on scrollpulse + mystoriesanimated

The ad-hoc `/tmp/upload_via_pw.py` and `/tmp/update_metadata_pw.py` scripts
shipped both videos. This skill formalizes that flow. Initial calibration
data carried over:

- Profile 3 → rsinghtomar3011@gmail.com → scrollpulse channel UCnCXcsVuniK5lYrjZ48Brxg
- Profile 5 → sanimated219@gmail.com → mystoriesanimated channel UCNIeHBf--KUegdiJZs_LSZg
- Cookie bridge confirmed — Local State + Profile/Cookies + Network/Cookies + Login Data + Web Data + Preferences. macOS Keychain handles `os_crypt` decryption identically across user-data-dirs.
- Studio upload-form selectors for title/desc on the upload step are flaky (timed out on both runs). Skill design: defer metadata to the post-upload edit page (selectors validated working there).
- Studio edit-page selectors validated 2026-05-08:
  - `#title-textarea div#textbox[contenteditable='true']`
  - `#description-textarea div#textbox[contenteditable='true']`
  - `ytcp-button#toggle-button` (Show more → expose tags)
  - `ytcp-form-input-container#tags-container input` (type each tag + Enter)
  - `ytcp-button#save`

## Open issue carried in from day 0

- Profile 5 (mystoriesanimated) edit page hit `ERR_ABORTED` on direct goto
  to `/video/<id>/edit`. Workaround: navigate to studio root first, then
  goto edit URL with retry. Need to verify whether this happens on every
  Profile 5 run or was transient.
- Stage 6 cross-engagement Playwright cycle is described but NOT YET
  implemented in `pipeline/cross_engage_via_playwright.py`. First run of
  this skill will need to land that module.

## Sibling skill to author next

`/playwright-engagement-loop` — 24/7 video-loop daemon. Separate concern.
- Open every `<channel>/uploads/*.json` URL in N Chrome tabs
- SEPARATE Chrome profile (not signed-in — avoids "watching own content" flag)
- Use `Page.bringToFront()` round-robin every ~30 s (Chrome throttles inactive tabs to 1 fps; bringToFront unthrottles)
- Auto-restart on Chrome crash via launchd or a watchdog process
- Telemetry: log per-tab playtime so we can verify watch hours land

## Class-of-bug / pipeline-bug fixes that should land during first impl

(per heuristic #51 efficiency scout)

- Extract `chrome_debug_session(channel_slug)` context manager into
  `pipeline/playwright_chrome_session.py`. Currently the launch+CDP+attach
  sequence appears in three places (upload, metadata-edit, cross-engage).
- Extract Studio selector constants into `pipeline/playwright_studio_selectors.py`
  with auto-snapshot-on-miss. When Studio drifts, one file updates.
- Delete `/tmp/upload_via_pw.py` + `/tmp/update_metadata_pw.py` after the
  pipeline/ versions land.

## 2026-05-08 — Stage 6 E2E run validated (4/4 siblings)

Production module `pipeline/cross_engage_via_playwright.py` shipped + ran
clean on `https://www.youtube.com/watch?v=l_7wQXOeVPc` from Profile 3 source.
4 sibling profiles (1, 4, 5, 8) all returned `like=OK`; all 4 came back
`sub=already_subscribed` (3 of them got fresh subscribes on a prior partial
run; Profile 5 was already-subscribed from the earlier e2e test). Total
~2 min for 4 profiles sequential.

**KEY FINDING — Like button click pattern:**
- ❌ `btn.click(timeout=8000)` — fires TimeoutError (actionability check
  fails on YouTube's hidden-sibling like buttons in theater layouts).
- ❌ `btn.scroll_into_view_if_needed()` then `btn.click(force=True)` —
  scroll fails first with "element is not visible".
- ✅ `btn.dispatch_event("click", timeout=8000)` — delivers the DOM
  click regardless of visibility/actionability. Works first-try on every
  Profile tested.

**KEY FINDING — Subscribe pattern is robust:**
- `btn.click(force=True)` works first-try on Subscribe button. Less
  hidden-sibling pollution in the subscribe DOM region than the like region.
- We use the same `dispatch_event` for safety, but the simpler click would
  also work for subscribe.

**Selector lists validated:**
```python
LIKE_SELECTORS = (
    "button[aria-label*='like this video' i][aria-pressed]",  # primary, used
    "button[aria-label*='Like this' i]",
    "ytd-toggle-button-renderer #like-button button",
    "like-button-view-model button",
)
SUBSCRIBE_SELECTORS = (
    "ytd-subscribe-button-renderer button",                    # primary, used
    "yt-subscribe-button-view-model button",
    "button[aria-label*='Subscribe to' i]",
    "ytd-watch-metadata #subscribe-button-shape button",
)
```

**Cookie-bridge observation:**
Re-bridging cookies real-Chrome → Chrome-Debug **wipes local engagement UI
state** (the page re-loads thinking we haven't liked/subscribed). But
YouTube's server-side state persists, so the post-load state probe
correctly reads `already_subscribed` / `already_liked` from server data.
Re-running the fanout is idempotent at YouTube's end.

**Profile→email map (real Chrome) as of run:**
- Profile 1 → rsinghtomar54@gmail.com
- Profile 3 → rsinghtomar3011@gmail.com  ← scrollpulse source (skip in fanout)
- Profile 4 → rohit30.iitkgp@gmail.com  ← hindutavaanimated
- Profile 5 → sanimated219@gmail.com    ← mystoriesanimated
- Profile 8 → rsinghtomar30@gmail.com

Profile-to-channel mapping for Profile 1 + Profile 8 is still ambiguous
(brand accounts can own multiple channels per Google email). Per-channel
mapping should be derived from each `~/.config/ytfactory/youtube_token_<slug>.json`'s
embedded user info, OR by inspecting `channel_ids.json` titles vs the
profile that's signed in.
