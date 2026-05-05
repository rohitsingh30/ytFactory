# Per-account YouTube publish throttle

Every YouTube channel (== `upload.account` in its `config.yaml`) enforces a
**minimum 1-hour gap between consecutive public moments**. The throttle is
implemented in `pipeline/upload.py` (`compute_throttled_publish_at`) and is
applied automatically inside `upload_short()` right before the
`youtube_upload()` call.

## Why

- YouTube's algorithm prefers a steady cadence over bursty same-hour drops.
- A tight cluster of public videos cannibalises each other's first-24h
  impressions — the second video robs the first of its push.
- Helps any auto-loop (cliffhanger Part-2 watcher, batch ship runs) avoid
  accidentally double-publishing in the same hour.

## How it works

For an upload to account `X`:

1. Scan every `<channel>/uploads/**/*.json` under the project root.
2. Filter records where `record.account == X`.
3. For each record, compute `effective_publish = publish_at or uploaded_at`
   (so a previously-scheduled-but-not-yet-public video still counts).
4. Take the maximum, call it `latest`.
5. If `latest + 1h <= now` → no throttle, publish immediately.
6. Otherwise → set `publish_at = latest + 1h` (RFC 3339 UTC, `Z` suffix)
   so the new video is uploaded private now and YouTube flips it public
   at that timestamp.

The throttle **only fires when the caller did not pass an explicit
`publish_at`**. If the user picks a `Publish at` time in the dashboard,
that wins — we trust the operator.

## Stable queueing

Because step 3 uses the scheduled `publish_at` (not just `uploaded_at`),
shipping N videos back-to-back produces a clean 1-h-spaced queue:

| Action time | Latest seen `effective_publish` | New `publish_at` |
|-------------|---------------------------------|------------------|
| T + 0       | none                            | now (immediate)  |
| T + 5 min   | T + 0                           | T + 1h           |
| T + 10 min  | T + 1h                          | T + 2h           |
| T + 15 min  | T + 2h                          | T + 3h           |

No deduplication needed — each new upload simply lands one hour after the
furthest scheduled neighbour.

## Override

- Pass an explicit `publish_at` through `upload_short` (or fill the
  dashboard "Publish at" picker) — the throttle is bypassed.
- The default 1-hour gap is `pipeline.upload.DEFAULT_PUBLISH_GAP`
  (`timedelta(hours=1)`); change it there if a per-channel knob is ever
  needed.

## Scope

- Per **YouTube account** (the `upload.account` field), not per channel
  directory. Multiple channel_dirs / variants under the same YouTube
  channel share one throttle bucket.
- Different YouTube channels are independent — uploading to
  `mystoriesanimated` does not delay `historyrecapped`.
