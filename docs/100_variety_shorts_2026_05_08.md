# 100-variety-shorts pipeline — 2026-05-08

The MyStoriesAnimated channel's first end-to-end "kick once, walk
away" bulk run. 103 scripts pulled across 5 niches; 99 mp4s
queued for cloud render in 2-wide parallel; daily upload cron
loaded for 10/day at 2.4-hr slots starting 07:00 IST tomorrow.

This doc is the runbook: monitor, recover, top-up, kill switches.

## Inventory

```
mystoriesanimated/reddit_amitheasshole       64 scripts
mystoriesanimated/aita_cooking               20 scripts
mystoriesanimated/reddit_tifu                10 scripts (4 already rendered + uploaded)
mystoriesanimated/wiki_oddities               4 scripts
mystoriesanimated/today_in_history            5 scripts
                                            ───
                                            103 scripts
```

## Render mechanics

Two background processes serve the queue:

```
PID 90245   scripts/bulk_render_queue.py
            --channels <5 niches> --split-aita 22:22 --parallel 2
            --log /tmp/bulk_render.log
            (pass 1: enumerated 79 jobs at startup, when AITA had 44)

PID 95839   /tmp/chain_second_render.sh
            (parked; polls every 60s for PID 90245 exit, then
             kicks pass 2 with --split-aita 30:30 to mop up the
             20+ AITA scripts that arrived from the top-up pull
             after pass 1 enumerated)
```

Render time per Short on a warm cloud pipe: ~10-12 min (chunked
Chatterbox TTS ~5 min + 30 FLUX.2 klein images @ ~12s each + 30
anatomy-gate haiku calls @ ~3s each + compose ~1 min). At 2-wide
parallel: ~5-6 min wall-clock per Short.

Total ETA from kick (1:37 PM IST 2026-05-08): **~10 hours** for
99 mp4s. Completion expected ~midnight IST tomorrow.

## Variant routing

```
script in dir                     → variant YAML
─────────────────────────────────  ────────────────────────────────
reddit_amitheasshole/scripts/     → aita_animated.yaml (first 22 alpha)
                                    aita_text.yaml     (next 22+ alpha)
                                    pass 2 uses 30:30 split
aita_cooking/scripts/             → aita_cooking.yaml
reddit_tifu/scripts/              → tifu.yaml
wiki_oddities/scripts/            → wiki_oddities.yaml
today_in_history/scripts/         → today_in_history.yaml
```

All 6 variant YAMLs are now at the post-fix quality bar:
`image_steps: 8` + `anatomy_check: true`.

## Monitoring (during render)

```bash
# Are the renders alive?
ps -p 90245 95839 -o pid,etime,command

# How many mp4s landed so far?
ls mystoriesanimated/*/shorts/*.mp4 | wc -l

# Per-render stdout (verbose)
tail -f /tmp/bulk_render.log

# Controller summary (pass/fail per render)
tail -f /tmp/bulk_render.controller.log

# Image cache ticking (live indicator while renders are mid-image-gen)
watch 'ls mystoriesanimated/*/cache/*/img_*.png 2>/dev/null | wc -l'

# Any failures?
grep ✗ /tmp/bulk_render.controller.log
```

## Cron uploads

Loaded at `~/Library/LaunchAgents/com.ytfactory.upload-next.plist`.
Calls `scripts/upload_next.py --count 1` every 2.4 hrs in 10 slots/
day. First fire: **07:00 IST tomorrow 2026-05-09**.

```
slot times (system-local time, configure Mac TZ to IST for IST slots):
  07:00, 09:24, 11:48, 14:12, 16:36, 19:00, 21:24, 23:48, 02:12, 04:36
```

`upload_next.py` rotates through niches alphabetically (cursor
state at `mystoriesanimated/.upload_rotation_cursor.json`) so the
public feed alternates AITA / cooking / TIFU / TIH / wiki for
visual variety — never 30 AITA-animated in a row. Each upload
goes private with `publishAt = now + 20min`; YouTube auto-flips
to public 20 min later.

## Cron management

```bash
# Status
launchctl list | grep com.ytfactory

# Pause for the night (won't fire until reloaded)
launchctl unload ~/Library/LaunchAgents/com.ytfactory.upload-next.plist

# Resume
launchctl load ~/Library/LaunchAgents/com.ytfactory.upload-next.plist

# Manual fire (force one upload now)
.venv/bin/python scripts/upload_next.py --count 1

# Inspect the rotation cursor
cat mystoriesanimated/.upload_rotation_cursor.json
```

## Recovery procedures

### Bulk render dies mid-flight

Cause: laptop sleep, `kill 90245`, system crash.

```bash
# Check what's still alive
ps -ef | grep -E "bulk_render_queue|make_shorts" | grep -v grep

# If both pass 1 + chain script are dead, just re-kick. The
# controller skips any slug whose mp4 already exists, so this is
# idempotent.
nohup bash -c '
set -a && source .env && set +a
PYTHONPATH=. .venv/bin/python scripts/bulk_render_queue.py \
  --channels mystoriesanimated/reddit_amitheasshole \
             mystoriesanimated/aita_cooking \
             mystoriesanimated/reddit_tifu \
             mystoriesanimated/wiki_oddities \
             mystoriesanimated/today_in_history \
  --split-aita 30:30 --parallel 2 --log /tmp/bulk_render.log
' > /tmp/bulk_render.controller.log 2>&1 &
disown
```

### Cloud Run service down (5xx persistent)

```bash
# Local fallback already engages via the circuit breaker. To
# force-disable cloud and use local f5_tts + local mflux:
unset CLOUDRUN_TTS_F5_URL CLOUDRUN_TTS_CHATTERBOX_URL
unset CLOUDRUN_IMAGE_FLUX2_KLEIN_URL
# then re-kick the bulk render. But local mflux 2-wide will
# Metal-timeout (see docs/parallel_bulk_renders.md). Drop to 1-wide:
.venv/bin/python scripts/bulk_render_queue.py ... --parallel 1
```

### Cron fires but uploads fail

Tail `/tmp/upload_next.log`. Common failures:
- OAuth token expired → run `python -m pipeline.upload auth refresh`
- Quota hit (10/day cap is YouTube's daily uploadLimitExceeded) →
  cron retries at next slot; transient
- `publishAt` < now → script bumps publish_at by 20 min when it
  fires; should never happen unless system clock is off

## Top-up the queue beyond 100

```bash
set -a && source .env && set +a
.venv/bin/python scripts/pull_stories.py --out . reddit \
  --subreddit AmItheAsshole --listing top --timeframe all \
  --channel mystoriesanimated/reddit_amitheasshole --limit 30 --pick all

# Then re-kick bulk_render_queue.py to render the new ones.
```

`--listing` options: top / hot / new / rising. `--timeframe`:
hour / day / week / month / year / all. The skip-seen guard
dedupes against existing raws so mixing timeframes is safe.

## Class-of-bug fixes baked in this session

The TIFU 4-pack surfaced 9 distinct fixes that were in flight
during this run. Full debrief at
[`post_upload_2026_05_08_tifu.md`](./post_upload_2026_05_08_tifu.md).
Summary:

| # | what | shipped |
|---|---|---|
| 5 | `_CTA_RE` accepts "in the comments" / "what you would have done" | ✓ |
| 6 | Cloud TTS missing-URL → `CloudRunUnavailable` (fallback engages) | ✓ |
| 7 | Chatterbox 296 WPM racing → sentence-chunker (server-side) | ✓ |
| 8 | Rewriter 110-160w → 110-135w (60s Shorts cap at 142 WPM) | ✓ |
| 9 | Cloud Run 401 mid-render → token cache pop + retry | ✓ |
| 10 | Per-frame anatomy QC (haiku vision) | ✓ wired, on for tifu+aita |
| 11 | Cached image QC re-validation when YAML flag flips | ✓ |
| 17 | Sentence-init lowercase + Dr,/Mr, typo fix | ✓ |
| 18 | Cast-router witness-verb guard (narrator stays on screen) | ✓ |

**Remaining open** (visual quality, not blocking ship):
- claude CLI parallel-pull "Exec format error" race during fast
  bulk pulls. Workaround: spread pull across timeframes.
- FLUX.2 klein style adherence drift across consecutive frames
  (less acute at 8 steps but still latent).
- "closer panel only shows bell icon" — turns out this is
  intentional design per 2026-05-02 user feedback. The icons in
  the last beat image ARE the closer.

## What "smooth as butter" means now

After this session, `/make-mystories-short --source <variant>` should:

1. Pull stories without manual `.env` sourcing (#6 fix).
2. Pass `script_check` first try — no manual CTA edits needed (#5).
3. Produce 110-135 word narrations that fit the 60s Shorts cap (#8).
4. Render at correct WPM (Chatterbox chunked, #7).
5. Hand-anatomy clean per-image (anatomy_check, #10/#11).
6. Survive long renders without 401 mid-flight (#9).
7. Stay narrator-on-stage during witness beats (#18).
8. Auto-fix dialogue capitalization typos (#17).

The only manual step is the AskUserQuestion source picker.

## File locations

- `docs/post_upload_2026_05_08_tifu.md` — full session debrief (12 learnings)
- `docs/parallel_bulk_renders.md` — parallel rule + cap-at-max-instances
- `mystoriesanimated/learnings/tifu_v4_2026_05_08.md` — TIFU-specific channel learning
- `scripts/bulk_render_queue.py` — controller (idempotent on re-run)
- `scripts/upload_next.py` — daily cron upload picker
- `control/com.ytfactory.upload-next.plist` — launchd schedule
- `pipeline/llm/anatomy_check.py` — haiku vision anatomy gate
- `cloud/tts-chatterbox/server.py` — chunked synth fix (revision `00006-9gd`)
