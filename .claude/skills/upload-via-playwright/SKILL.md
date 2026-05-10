---
name: upload-via-playwright
description: Upload a rendered mp4 to YouTube via Playwright + a non-default Chrome user-data-dir (Chrome-Debug) attached to the user's signed-in profile via CDP — the universal fallback path when the YouTube Data API quota is exhausted (403 quotaExceeded across all channels because the project's daily 10K-unit pool is shared). Drives studio.youtube.com end-to-end (Create → Upload videos → set unlisted/public → publish), then opens the live video's edit page to set title + description + tags, THEN cycles through every other owned channel's Profile to like the video + subscribe to the source channel if not already. Also exposes /pw-upload as an alias. Use when the user says "upload via playwright", "/pw-upload", "manual studio upload", "API quota dead, ship anyway", or after `pipeline.upload.youtube_upload` raises HttpError 403 quotaExceeded. For the API path use `pipeline.upload.youtube_upload`. For the bigger why-it-works writeup see docs/playwright_with_signed_in_chrome.md.
---

# /upload-via-playwright — universal Studio-UI upload fallback

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

> **2026-05-09 — WEBSITE-FIRST + CLOUD-ONLY (post nuclear cleanup).**
> The cloud ytfactory-web service's `POST /api/uploads/from_job` is
> now the canonical upload entry (default
> `https://ytfactory-web-7hwnzw7lya-as.a.run.app` per
> `pipeline.skill_dispatch`); on YouTube API 403 quotaExceeded it
> returns `state: "quota_exhausted"` with a `playwright_fallback_hint`.
> Server-side Playwright drive (this skill's flow ported into the
> cloud render-worker so the website handles the entire fallback
> automatically) is a P3.5 follow-up. Until that lands, this skill is
> the manual fallback the user invokes when the publish button surfaces
> `quota_exhausted`. Cross-engagement is the one stage that stays on
> the laptop because per-account Chrome profiles can't be cleanly
> sandboxed in Cloud Run. CLAUDE.md "Website-first production" tracks
> status.

Bypass the API. Drive YouTube Studio in a real, signed-in Chrome — exactly the
way you would manually — but automated, with cookie-bridging from your real
Chrome profile so there's no re-auth click needed.

You wear two hats: **operator** (one-shot ship a single mp4) and **bot herder**
(after upload, fan out cross-channel engagement via the same Playwright path
since `cross_engage` API calls are also dead).

The technique reference is at [`docs/playwright_with_signed_in_chrome.md`](/Users/rohit/ytFactory/docs/playwright_with_signed_in_chrome.md) — read it once if you're new.

## How to run it

### 1. Confirm scope (AskUserQuestion, prefilled)

Two questions max. Defaults from the channel's config.yaml + narration JSON.

**Q1. What are we uploading?**
- (Recommended) `<channel>/shorts/<slug>.mp4` (most recently rendered Short)
- Specific path I'll paste
- Other (free-text)

**Q2. Privacy?**
- Public (recommended for shipped channels)
- Unlisted (preview before share — recommended for first ships per channel)
- Private (dry run / scheduled later)
- Don't upload — render the metadata flow only (debug)

Title / description / tags come from `<channel>/narrations/<slug>.json` if
present (`title_options[0]`, `description`, `tags[]`). If missing, ask in
one consolidated prompt; never field-by-field.

### 2. Stage 1 — preflight

Run these in order; abort with a clear message if any fail.

1. **API-first probe** — call `pipeline.upload.inspect_token_status(account=<channel>)`. If `state == "ok"`, attempt `pipeline.upload.youtube_upload(...)` first. Only fall through to Playwright on `HttpError 403 quotaExceeded`. Surface a `--force-playwright` flag for testing the PW path even when API is healthy.
2. **Chrome closed** — `pgrep -f "Google Chrome.app/Contents/MacOS/Google Chrome"` must return empty. Cookie SQLite DBs are locked while Chrome runs. If procs exist, **list them and ASK before killing** — don't auto-pkill (cron jobs run their own Chrome).
3. **Profile mapping** — read `~/Library/Application Support/Google/Chrome/Local State` and find the Profile-N whose `user_name` matches the OAuth account email for the target channel. Cache the result at `~/.config/ytfactory/profile_map.json`. The known mapping (2026-05-08):
   - scrollpulse → Profile 3 (rsinghtomar3011@gmail.com)
   - mystoriesanimated → Profile 5 (sanimated219@gmail.com)
   - others auto-detect on first run.
4. **Chrome-Debug Profile-N exists** — if not, auto-bridge from real Chrome (see Stage 2). Don't bail.
5. **mp4 + metadata sanity** — file exists, ≤256 MB (Studio's web-upload cap), title ≤100 chars, description ≤5000, tags total ≤500 chars.
6. **SingletonLock cleanup** — `rm -f ~/Library/Application Support/Google/Chrome-Debug/{SingletonLock,SingletonSocket,SingletonCookie}`.

### 3. Stage 2 — cookie bridge (idempotent)

Same-user macOS Keychain → cookies copied from real Chrome decrypt cleanly in Chrome-Debug. Skip if Chrome-Debug's cookies are <24 h fresher than real Chrome's (no-op).

```python
SRC = "~/Library/Application Support/Google/Chrome"
DST = "~/Library/Application Support/Google/Chrome-Debug"
PROFILE = f"Profile {N}"   # auto-detected from registry
```

Files to copy (dst-relative paths in parentheses):

- `Local State` (top-level — has `os_crypt.encrypted_key`)
- `<PROFILE>/Cookies` + `<PROFILE>/Cookies-journal`
- `<PROFILE>/Network/Cookies` + `<PROFILE>/Network/Cookies-journal` (Chrome v123+)
- `<PROFILE>/Login Data` + `<PROFILE>/Login Data-journal`
- `<PROFILE>/Web Data` + `<PROFILE>/Web Data-journal`
- `<PROFILE>/Preferences`

### 4. Stage 3 — Chrome launch + Playwright attach

```python
chrome = subprocess.Popen([
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "--remote-debugging-port=0",
    "--remote-debugging-address=127.0.0.1",
    f"--user-data-dir={CHROME_DEBUG}",
    f"--profile-directory={PROFILE}",
    "--no-first-run", "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=AutomationControlled",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "about:blank",
], stderr=open(stderr_path, "w"))
# Scrape ws://127.0.0.1:<port> from stderr; ~10s window.
```

Then:

```python
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    ctx = browser.contexts[0]   # signed-in context — DO NOT new_context()
    page = ctx.new_page()
```

If the URL redirects to `accounts.google.com/v3/signin/confirmidentifier` after
the bridge, Profile-N's session in Chrome-Debug is stale. Re-bridge ONCE; if
still stale, raise — the user must sign in manually via Chrome-Debug.

### 5. Stage 4 — drive Studio UI for upload

```
1. page.goto("https://studio.youtube.com/")
2. dismiss first-time modals: Continue / Got it / Escape × 3
3. click Create button: ytcp-icon-button#upload-icon (or aria-label-based fallback)
4. click "Upload videos" submenu
5. attach file: page.locator("input[type='file']").set_input_files(mp4)
6. wait ~6s for Studio to render the form
7. (optional Stage 5 sets metadata HERE, before publish)
8. walk Next buttons (×4): details → checks → video elements → visibility
9. click radio for chosen privacy (UNLISTED / PUBLIC / PRIVATE)
10. click ytcp-button#done-button (Save/Publish)
11. wait 8s for confirmation dialog
12. scrape https://youtu.be/<id> or youtube.com/shorts/<id> from page content
```

### 6. Stage 5 — set title / description / tags via the live edit page

The upload-form selectors for title/desc/tags are flaky. Setting them on the
edit page **after** upload works reliably — selectors validated 2026-05-08.

```
edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
page.goto(edit_url, wait_until="domcontentloaded")
sleep 6  # SPA hydration
```

Selectors (in priority order; first hit wins):

- **Title:** `#title-textarea div#textbox[contenteditable='true']`
  fallback: `ytcp-social-suggestion-input#title-textarea div#textbox`
- **Description:** `#description-textarea div#textbox[contenteditable='true']`
  fallback: `ytcp-mention-textbox#description-textarea div#textbox`
- **Show more:** `ytcp-button#toggle-button` (expands to reveal Tags)
- **Tags input:** `ytcp-form-input-container#tags-container input` — type each tag + Enter
- **Save:** `ytcp-button#save`

After Save, wait 6 s; the toast "Changes saved" or "Updated" appears.

### 7. Stage 6 — cross-channel like + subscribe (Playwright cycle)

This is the engagement equivalent of `cross_engage backfill --like --subscribe`,
but driven via UI so it works during quota exhaustion.

**Implementation:** [`pipeline/cross_engage_via_playwright.py`](/Users/rohit/ytFactory/pipeline/cross_engage_via_playwright.py)
— call `fanout(video_url, source_profile="Profile <N>")`. Validated 2026-05-08
end-to-end against `l_7wQXOeVPc` with 4 sibling profiles → 4/4 likes + all
subscriptions OK.

For each owned channel in the registry that is **not the source**:

1. Close current Chrome session (the one signed in as the source channel).
2. Bridge cookies for the sibling Profile (idempotent — only if needed).
3. Launch Chrome with `--profile-directory="Profile <sibling>"`.
4. Navigate to the live video URL (use `youtube.com/watch?v=<id>` form, NOT shorts/ — the watch page exposes both like + subscribe in stable selectors).
5. **Like** — selector probe via `LIKE_SELECTORS` tuple:
   - `button[aria-label*='like this video' i][aria-pressed]` (primary)
   - state: `aria-pressed="true"` → liked, `="false"` → unliked
   - If liked: skip (idempotent).
   - If unliked: **`button.dispatch_event("click")`** — DO NOT use `click()` or `click(force=True)`. YouTube renders multiple like buttons (theater/standard layouts); Playwright's actionability checks fight `scroll_into_view_if_needed` and time out with "element is not visible". `dispatch_event` delivers the click via DOM regardless of viewport state. Wait 5 s for YouTube's debounce + UI re-render, then re-probe.
6. **Subscribe** — selector probe via `SUBSCRIBE_SELECTORS` tuple:
   - `ytd-subscribe-button-renderer button` (primary)
   - state: text/aria-label contains "subscribed" → already subscribed, contains "subscribe" → unsubscribed.
   - If subscribed: skip.
   - If unsubscribed: `button.dispatch_event("click")`. Wait 4 s. Re-probe.
7. Close, move to next sibling.

Don't fan out in parallel — Chrome-Debug is single-user-data-dir per process,
parallel would lock-conflict. Sequential is ~30 s per sibling × N siblings.

**Note: the cookie bridge wipes Chrome-Debug's local engagement state when
re-bridged, but YouTube's server-side state persists.** So re-running the
fanout shows `already_subscribed` for previously-subscribed channels (state
probe reads server data once page loads). Idempotent.

Surface a `--no-engagement` flag to skip this stage.

### 8. Stage 7 — persist the upload record

Match the existing `<channel>/uploads/<slug>.json` schema:

```json
{
  "slug": "<slug>",
  "video_id": "<11-char>",
  "url": "https://youtube.com/shorts/<id>",
  "channel": "<slug>",
  "channel_id": "UC<...>",
  "channel_account_email": "<gmail>",
  "channel_chrome_profile": "Profile <N>",
  "privacy": "unlisted",
  "uploaded_at": "<iso8601-utc>",
  "method": "playwright_studio_chrome_debug_profile_<N>",
  "title_set": true,
  "description_set": true,
  "tags_set": true,
  "engagement": {
    "siblings_liked":     ["<slug>", "<slug>"],
    "siblings_subscribed": ["<slug>", "<slug>"],
    "siblings_skipped":    [{"slug": "<x>", "reason": "already_subscribed"}]
  }
}
```

### 9. Report back

```
✅ uploaded — https://youtube.com/shorts/<id>
   method: playwright (API was 403 quotaExceeded)
   account: <email> via Profile <N>

✅ metadata set — title, description, <N> tags

🔁 cross-engagement (Playwright cycle):
   ✓ siblingA  liked  + subscribed
   ✓ siblingB  liked  + already subscribed
   ⏭ siblingC  liked  + skipped subscribe (cooldown < 24h)
   ✗ siblingD  failed — see /tmp/pw-upload/<slug>/sibling-d-error.png

upload record: <channel>/uploads/<slug>.json
```

## Output paths

- `pipeline/upload_studio_playwright.py` — the Playwright upload module
- `pipeline/cross_engage_via_playwright.py` — UI-driven cross-engagement
- `pipeline/playwright_chrome_session.py` — shared `chrome_debug_session()` context manager (extract during first impl per heuristic #51)
- `pipeline/playwright_studio_selectors.py` — named selector constants
- `~/.config/ytfactory/profile_map.json` — auto-rebuilt email → Profile-N
- `<channel>/uploads/<slug>.json` — upload record (existing schema, augmented `engagement` block)
- `/tmp/pw-upload/<slug>/` — per-run artifacts: `chrome.stderr`, screenshots at every stage, error captures

## Quality gates

Run BEFORE any irreversible action:

1. **Chrome-closed gate** — abort if real Chrome is running. Don't auto-kill.
2. **Profile-mapping gate** — Profile-N's email MUST match the channel's OAuth account. Cross-channel cookie bridging would be a security incident.
3. **mp4-size gate** — ≤256 MB (Studio web upload cap). For larger files, surface a manual-upload-needed message.
4. **Metadata-length gate** — title ≤100 chars, description ≤5000, tags total ≤500.
5. **API-first gate** — try `pipeline.upload.youtube_upload` first if `inspect_token_status == "ok"`. PW upload costs ~3-5 minutes vs API's ~15 s. Only fall through on quotaExceeded.
6. **Cookie-freshness gate** — if real Chrome's `Cookies` mtime is older than 30 days, the YouTube session is likely expired. Surface to the user; don't waste a 5-min run on a guaranteed re-auth redirect.
7. **Post-upload verification gate** — re-fetch the edit page, confirm title/desc/tags actually saved (Studio sometimes silently drops changes if you click Save before its debounce settles).

## Important rules

- **NEVER use `~/Library/Application Support/Google/Chrome` as `--user-data-dir`.** Chrome v136+ rejects remote debugging there (hardcoded). Always use `Chrome-Debug`.
- **NEVER call `browser.new_context()` after `connect_over_cdp`.** Use `browser.contexts[0]` — the existing one carries the signed-in state. A new context is logged out.
- **NEVER auto-pkill running Chrome.** Cron jobs (cross_engage, x_scrape, etc.) own their own Chrome instances. List PIDs and ask.
- **NEVER assume Studio's selectors are stable.** Snapshot on every miss; surface to the user as "Studio drifted, here's the snapshot, please point me at the new selector".
- **NEVER skip the API-first probe.** PW is the fallback, not the default. Wasted Playwright runs cost ~5 min each; the API call is 15 s.
- **NEVER ship to the wrong channel.** The Profile-mapping gate (rule 2) is the only thing standing between us and "uploading scrollpulse content to mystoriesanimated by accident." Don't bypass it.
- **NEVER upload Public on first ship per channel.** Default to Unlisted; let the user promote after preview.
- **ALWAYS use `.venv/bin/python`** for any helper invocations.

## Self-learning hook

After the user runs `/critique-video` on the uploaded result, OR if any
Stage fails:

1. Classify:
   - **ONE-OFF** (one selector miss, one stale cookie) → fix the artifact + 1-line note in `learnings/_index.md`.
   - **CLASS-OF-BUG** (selector drift across multiple runs, profile mapping wrong, etc.) → fix in `pipeline/playwright_studio_selectors.py` OR `pipeline/playwright_chrome_session.py` + write a topic file `learnings/<topic>.md` + mirror to `docs/<topic>.md` if cross-channel.
2. Update `MEMORY.md` index per CLAUDE.md dual-save rule.
3. If a selector misses **twice**, escalate to a hard quality gate: snapshot the page on miss + surface a "Studio drifted, please update the selector at file:line" message before continuing.

## Why this skill is separate from `pipeline.upload.youtube_upload`

`youtube_upload` is the API path. It's faster (~15 s vs ~5 min), cheaper to
debug (`videos.insert` returns structured errors), and doesn't drag a real
Chrome process around. **It's the default.** Use it when quota is healthy.

This skill is the **fallback layer** for when the API path is unavailable —
quota exhausted, OAuth scope missing, the channel hasn't enabled API access
yet, or you specifically need the engagement-via-UI path (because the API's
`videos.rate` and `subscriptions.insert` are also blocked by the shared
project quota when uploads are blocked).

The two are wired in series: this skill calls `pipeline.upload.youtube_upload`
first, falls through on `403 quotaExceeded`, and only then drives Playwright.

## Sibling skill (NOT in this skill — author next)

The user wants `/playwright-engagement-loop` — a 24/7 daemon that opens every
uploaded video in tabs of a SEPARATE non-signed-in Chrome profile (so YouTube
doesn't flag "watching your own content"), tab-switches every 30 s to bypass
Chrome's inactive-tab throttling, restarts on Chrome crash. Different concern,
different skill. See `learnings/_index.md` for the placeholder.

## Reference

- [`docs/playwright_with_signed_in_chrome.md`](/Users/rohit/ytFactory/docs/playwright_with_signed_in_chrome.md) — full technique writeup, Chrome v136+ caveat, cookie bridge, common pitfalls
- [`docs/youtube_quota_shared.md`](/Users/rohit/ytFactory/docs/youtube_quota_shared.md) — why API uploads can fail across channels simultaneously
- [`docs/handoff_2026_05_08_scrollpulse_first_ship.md`](/Users/rohit/ytFactory/docs/handoff_2026_05_08_scrollpulse_first_ship.md) — first session that proved the technique end-to-end
- `~/shofferAi/apps/playwright/scripts/playwright-mcp-with-chrome.sh` — canonical reference launch script (different concern: MCP server bridge, not one-shot upload)
- `pipeline.upload.youtube_upload` — the API path this skill falls back from
- `pipeline.research.cross_engage` — API-driven cross-engagement (sibling, also fails on quota; this skill's Stage 6 is its UI-driven counterpart)
