---
name: aita_cliffhanger channel — Part 1 + sub-gated Part 2 autoship
description: AITA variant that cuts on a cliffhanger; Part 2 auto-renders + uploads when Part 1 crosses +100 subscribers since upload (within 7 days).
type: project
originSessionId: 7c46b191-b5e3-440e-828f-57b261d1b72d
---
Three Part-1 channels (`channels/aita_cliffhanger_{animated,text,cooking}.yaml`)
and three Part-2 channels (`channels/aita_cliffhanger_part2_{animated,text,cooking}.yaml`).

**Part-1 mechanics** (`cliffhanger: true` on the YAML):
- `pipeline.rewrite._closer_block` → `_cliffhanger_closer_block` (cut before
  kicker, in-progress stakes only, natural "subscribe for Part 2" CTA).
- `pipeline.script_check._CTA_RE` → `_CLIFFHANGER_CTA_RE` (accepts
  subscribe / part 2 / bell / "drops next" phrasings).
- Visual closer panel from `closer_format: "SUBSCRIBE, for Part 2"`.
- Niche `aita_cliffhanger` registered with its own channel_dir
  `reddit_amitheasshole_cliffhanger`.

**Part-1 → Part-2 sidecar** written by `pipeline.upload.write_part2_pending`
right after a successful Part-1 upload. Path:
`data/intermediate/<part1_chan>/part2_pending/<slug>.json`. Sidecar carries:
`baseline_subs` (snapshotted at upload time via `get_channel_sub_count`),
`threshold_subs_delta` (default 100, from `part2_trigger.subs_delta` in the
Part-1 YAML), `window_expires_at` (default +7 days), `part2_channel` (the
finale YAML), `raw_story`, `part1_narration`, `cast_path`, `account`.

**Part-2 watcher** (`pipeline/part2_watcher.py`) — standalone script,
`python -m pipeline.part2_watcher [--once]`. Polls every 30 min, groups
sidecars by account (1 quota unit/account/tick), drops expired records,
fires `rewrite_part2()` + subprocess `make_shorts.py --upload` for any
sidecar where `current_subs - baseline_subs >= threshold`. Carries
cast.json + raw.json from Part-1 channel_dir to Part-2 channel_dir for
narrator/source continuity. Sidecar deleted on success; left in place
on failure for next-tick retry.

**Part-2 rewriter** — `pipeline.rewrite.rewrite_part2(raw_story,
part1_narration, channel_cfg)` uses `_PART2_PROMPT` which shares
`_SHARED_CRAFT_RULES` (subtitle shape + prosody) with `_BASE_PROMPT`
but completely overrides OPENING/SPICY/structure: 1-2 sentence recap,
then kicker → verdict → aftermath, standard AITA closer.

**OAuth** — `SCOPES` in `pipeline/upload.py` now includes
`youtube.readonly` (needed by `get_channel_sub_count`). First run on a
pre-existing account triggers a re-auth.

**Why +100 / 7 days:** operator picked +100 explicitly (scrappy, fires
often). 7 days catches the Shorts engagement tail without a stale queue.
Daily upload-quota cap of 10K units = ~6 Part-2 auto-uploads/day max
(1600 units per upload).

**How to apply:**
- Watcher is NOT auto-spawned by the website yet. Run it manually via
  `python -m pipeline.part2_watcher` under tmux/launchd until that's wired.
- If you change `_SHARED_CRAFT_RULES` in `pipeline/rewrite.py`, both Part-1
  and Part-2 narrations pick up the change. Don't duplicate prosody/subtitle
  rules into either prompt.
- Part-2 Watcher failures are deliberately non-fatal at every layer: a bad
  sidecar logs and skips, a failed render leaves the sidecar for retry,
  a bad sub-count read skips the whole account that tick.
