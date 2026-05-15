---
name: create-youtube-channel
description: End-to-end spin up a new YouTube channel on a specified Google email account — drives the create-channel UI via Playwright (using the user's Chrome profile so Google sessions carry over), runs OAuth, scaffolds the local repo dir + config.yaml, registers with the cross-engagement system, and cross-subscribes to every existing channel. Use when the user says "create a new YouTube channel on <email>", "spin up a channel on <email>", or "add a new channel". Picks niche / channel name / handle randomly from the palette below unless the user specifies them.
---

# /create-youtube-channel — full E2E new channel onboarding

This is the one-shot "I need another channel in the stable" recipe. It
automates everything from the YouTube UI form through the integration
into the cross-engagement system, so once the skill returns the new
channel is a fully wired sibling — OAuth cached, in the registry,
subscribed to/from every other channel, ready to receive auto-engagement
on its uploads.

## Inputs

The user must specify (or you should ask):
- `email` — the Google account the channel will live on. **Required.**

The user MAY specify (otherwise pick randomly per §1):
- `slug` — the lowercase-no-spaces local identifier
- `display_name` — what shows on YouTube
- `handle` — the @ identifier
- `niche` — the content vertical

If the user said "pickrandomly all info", pick everything per §1.

## 0. Pre-flight checks

Run these before anything else; abort with a clear message if any fail.

1. **Chrome profile for the email exists.** Map email → profile dir via:
   ```python
   import json, os
   ls = json.load(open(os.path.expanduser('~/Library/Application Support/Google/Chrome/Local State')))
   {v.get('user_name'): k for k, v in ls['profile']['info_cache'].items()}
   ```
   If the email isn't in the cache, the user needs to sign in via the
   real Chrome app once first.

2. **OAuth client secret in place.** `~/.config/ytfactory/client_secret.json`
   must exist (used by `pipeline.upload.authenticate` later).

3. **Pipeline venv usable.** `/Users/rohit/ytFactory/.venv/bin/python -c "from pipeline.cross_engage import list_sibling_accounts"` succeeds.

4. **Slug + dir collision.** Make sure `/Users/rohit/ytFactory/<slug>/`
   doesn't already exist and `~/.config/ytfactory/youtube_token_<slug>.json`
   doesn't either.

## 1. Random picks (when not user-specified)

Pick a niche that **doesn't duplicate** an existing channel. Read existing
channels by listing `/Users/rohit/ytFactory/*/config.yaml` files. Then
pick from this curated palette (drop any that overlap with existing
niches — e.g. if `historyrecapped` exists, drop "history facts"):

| niche | example name | example @handle |
|---|---|---|
| weird facts / "did you know" | Weird Facts Daily | @weird_facts_daily |
| science explainers | Science Bytes | @sciencebytes |
| money / personal finance | Money Hacks | @money_hacks_daily |
| productivity / habits | Habit Stacks | @habit_stacks |
| philosophy quotes | Stoic Daily | @stoic_daily |
| food / cooking shortcuts | Quick Bites Kitchen | @quick_bites_kitchen |
| travel landmarks | Wanderlust Bites | @wanderlust_bites |
| gaming news | Game Recap Daily | @game_recap_daily |
| celebrity / pop trivia | Pop Trivia Now | @pop_trivia_now |
| tech tips | Tech Tip Tuesday | @techtiptuesday |

Generate **slug** from the name: lowercase, drop spaces, drop
punctuation. Examples: "Weird Facts Daily" → `weirdfactsdaily`.

Generate **handle** by checking `@<slug>` first. If unavailable on
YouTube, the create-channel form will reject it; the skill must catch
that and try `@<slug>_yt`, `@<slug>_<random4digit>` until one sticks.

Use Python's `random.choice` — record the chosen palette row in the
final summary so the user can rename later if they hate it.

## 2. Browser automation — create the channel via YouTube UI

YouTube has no API for creating personal channels; this MUST go through
the UI. Drive it with Playwright using a copy of the user's Chrome
profile so Google sessions carry over.

### 2a. Stage the profile copy

Chrome locks its `user_data_dir` while it's running. Copy the target
profile to a temp dir so we don't disturb the live Chrome session:

```bash
mkdir -p /tmp/yt_pw_profile_<email_safe>
cp -R "$HOME/Library/Application Support/Google/Chrome/<Profile X>" \
       /tmp/yt_pw_profile_<email_safe>/Default
cp "$HOME/Library/Application Support/Google/Chrome/Local State" \
       /tmp/yt_pw_profile_<email_safe>/
```

`<email_safe>` = email with `@.` replaced by `_`. ~200 MB per profile;
delete the temp dir after success.

### 2b. Drive the create-channel form

Use `/Users/rohit/ytFactory/.venv/bin/python` with `playwright.sync_api`
(installed via the cross-engagement work; chromium browser already
downloaded). **Headed mode** — YouTube blocks autoplay and many flows
on the headless-shell build.

The flow varies by Google's UI version. Generic recipe:

1. `chromium.launch_persistent_context(user_data_dir=/tmp/yt_pw_profile_<x>, headless=False, channel='chrome')` — uses the system Chrome binary, looks the most legit.
2. `page.goto('https://www.youtube.com/create_channel')`
3. If redirected to `/account` or signed-out, the profile didn't carry the session — abort and tell the user to sign in via real Chrome first.
4. **Account picker** if multiple accounts: click the avatar in top-right, click "Switch account", click the row matching `email`.
5. **Create-channel form**:
   - Display-name input — fill with `display_name`
   - Handle input — fill with `handle` (without leading `@`)
   - Confirm-handle / TOS checkbox — accept
   - Submit button (label varies — `Create channel`, `Create`)
6. Wait for redirect — typically `studio.youtube.com/channel/<NEW_CHANNEL_ID>`. Extract the channel ID from the URL.
7. If the handle was rejected (red error text), generate a new candidate handle and retry without reloading.

Selector resilience: snapshot via `page.accessibility.snapshot()` and
match on roles + text. **Do not** rely on stable CSS classes — YouTube
ships UI changes weekly.

Take a screenshot at each major step to `/tmp/yt_create_channel_<email_safe>_<step>.png`
so the user can audit visually if something went sideways.

### 2c. Verify

Browse to `https://studio.youtube.com/channel/<channel_id>/dashboard` —
the dashboard should load. Save the new channel's ID + URL +
display_name + handle in a result dict.

## 3. OAuth setup for the new account

Run the OAuth flow so the laptop pipeline can publish + engage as this
new channel:

```
/Users/rohit/ytFactory/.venv/bin/python -c "from pipeline.upload import authenticate; authenticate('<slug>', interactive=True)"
```

This blocks on `localhost:8089`, prints an auth URL. The skill should:
1. Stream the URL to the user.
2. The user clicks → opens it in any browser → signs in as the new
   email → clicks Allow.
3. The localhost callback completes the flow; token written to
   `~/.config/ytfactory/youtube_token_<slug>.json`.

Port 8089 may be in TIME_WAIT from a prior run; bind-probe before
launch:
```bash
until /Users/rohit/ytFactory/.venv/bin/python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',8089)); s.close()" 2>/dev/null; do sleep 3; done
```

After completion, verify the token:
```python
from pipeline.upload import inspect_token_status
inspect_token_status('<slug>')['state']  # must be 'ok'
```

## 4. Local repo scaffold

Create `/Users/rohit/ytFactory/<slug>/` with this layout. Mirror the
structure of `cosmosdecoded/` or another minimal channel — config.yaml +
empty state dirs.

```
<slug>/
  config.yaml          # channel YAML — see template below
  learnings/
    channel.md         # one-paragraph charter
  raw/                 # source material (gitignored at repo root)
  narrations/          # script.json + narration.wav per slug
  cache/               # (gitignored)
  shorts/              # rendered mp4s (gitignored)
  uploads/             # upload records (kept — small JSON)
  branding/            # logo, banner (kept)
```

### config.yaml template

```yaml
slug: <slug>
display_name: "<display_name>"
handle: "@<handle>"
niche: "<niche>"
youtube_channel_id: "<channel_id>"

format:
  short_duration_s: [50, 60]
  resolution: [1080, 1920]
  fps: 30
  caption_emoji_density: every-3-5-words

upload:
  account: <slug>
  privacy: private          # promote to public after format sign-off
  made_for_kids: false
  category_id: "27"          # adjust per niche; see YouTube category IDs
  auto_upload: false

closer: |
  LIKE if you learned something new today.
  SUBSCRIBE for daily <niche>.
```

### learnings/channel.md template

```markdown
# <display_name> — channel charter

Created: <YYYY-MM-DD>
YouTube: https://www.youtube.com/channel/<channel_id>
Handle: @<handle>
Owner email: <email>

**Niche:** <niche>
**Format:** 50-60s YouTube Shorts
**Voice:** TBD — try f5_tts with sarah.wav (production default) or kokoro/af_alloy first.

## Open questions

- Visual style?
- TTS provider + voice ID?
- Daily / weekly cadence?

## Current state

Channel exists on YouTube; nothing shipped yet.
```

## 5. Cross-engagement integration (MANDATORY — do not skip)

Once OAuth is in place AND the YouTube channel ID is known, the new
channel must immediately become a full participant in the network:

```bash
# 5a. Re-resolve channel IDs — picks up the new sibling
python -m pipeline.cross_engage refresh-ids

# 5b. Cross-subscribe — the new channel subscribes to every existing
#     channel AND every existing channel subscribes back. Bidirectional,
#     idempotent.
python -m pipeline.cross_engage subscribe-all

# 5c. BACKFILL — the new channel likes every existing upload across the
#     entire network. This is the "immediate engagement on creation"
#     requirement. With include-self the existing channels also like
#     their own videos (idempotent for those that already did).
python -m pipeline.cross_engage backfill
```

`refresh-ids` writes to `~/.config/ytfactory/channel_ids.json` (the
cross-engage registry). After step 5c, the new channel has:
- liked every existing video on every sibling channel
- subscribed to every other channel
- been subscribed-to by every other channel

Going forward, `pipeline.upload.upload_short()` will fan out likes
to/from this account on every future upload across the network (the
hook in `upload_short` runs `engage_after_upload` automatically).

The new channel has no uploads yet, so nothing to backfill in the OTHER
direction — but on its first upload the existing siblings will like it
via the same hook.

## 6. Memory + project doc updates

### Update MEMORY.md index

Add a line under the "Channels" section in
`~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`:

```
- **<display_name>** — `/Users/rohit/ytFactory/<slug>/learnings/`
  - [channel.md](/Users/rohit/ytFactory/<slug>/learnings/channel.md) — <one-line>
```

### Save project memory

Write a `project_<slug>_channel_created.md` memory entry with:
- date, email, slug, channel_id, handle
- niche pick rationale (which palette row, why)
- next-steps checklist (visual style, voice, first content batch)

Then add a pointer line in MEMORY.md.

## 7. Final report to the user

Print a tight summary:

```
✅ Channel created
   Display name: <display_name>
   Handle: @<handle>
   Channel ID: <channel_id>
   Studio: https://studio.youtube.com/channel/<channel_id>/

✅ OAuth cached at ~/.config/ytfactory/youtube_token_<slug>.json
✅ Repo scaffold: /Users/rohit/ytFactory/<slug>/
✅ Cross-subscribed to/from <N> sibling channels
✅ Memory + docs updated

Next steps:
  - Pick a TTS voice (run /Users/rohit/ytFactory/.venv/bin/python scripts/clone_voice.py …)
  - Write a few scripts via /make-script
  - First render: /Users/rohit/ytFactory/.venv/bin/python -m scripts.make_shorts <slug>
```

## Failure modes — what to do when

| symptom | likely cause | recovery |
|---|---|---|
| Playwright lands on `/signin` | Chrome profile copy lost session | Tell the user to sign in via real Chrome once, retry |
| Account picker doesn't show target email | profile is for the wrong email OR the account isn't logged in | Check `Local State` mapping; pick the right Profile N |
| Create-channel handle field rejects everything | Google's handle anti-spam (new account?) | Wait 24h or use a more common handle |
| OAuth token file written but no `refresh_token` | Google deduped issuance | `pipeline.upload.authenticate` already handles via splice path; if it raises, send the user to https://myaccount.google.com/connections to revoke + redo |
| Port 8089 busy | TIME_WAIT from prior flow | Bind-probe loop — already in §3 |
| `~/.config/ytfactory/channel_ids.json` missing the new entry after `refresh-ids` | OAuth wasn't actually completed | Check token state via `inspect_token_status(slug)`; redo OAuth |

## Notes

- This skill **CAN'T** programmatically agree to YouTube's TOS — that's
  a human action. The Playwright flow clicks the checkbox; if YouTube
  ever requires real human-verification (CAPTCHA), the skill should
  surface a screenshot + ask the user to complete the puzzle in the
  open Playwright window, then resume.
- The skill **should NOT** delete the temp profile copy until success
  is confirmed, in case manual recovery is needed.
- Every channel created via this skill must end up in the
  `cross_channel_engagement` system. If integration fails partway
  through, leave the channel in a "manual recovery needed" state and
  document precisely what step failed.
