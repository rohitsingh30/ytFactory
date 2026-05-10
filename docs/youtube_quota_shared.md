# YouTube Data API quota is shared project-wide

**Established 2026-05-08** during the scrollpulse OAuth onboarding. The
user assumed "fresh OAuth on a fresh channel ⇒ fresh quota" — it does
NOT. Documenting the actual model so the next person doesn't hit the
same surprise.

## Model

All ytfactory YouTube tokens are issued by the **same Google Cloud
project** (client_id `170961694807-...`). Google's quota is per-PROJECT,
not per-token, per-OAuth-account, or per-channel. The 6 channel tokens
(`cosmosdecoded`, `hindutavaanimated`, `historyrecapped`,
`mystoriesanimated`, `scrollpulse`, `sportsrecapped`) all share the
same daily 10,000-unit pool.

When ANY channel exhausts the project's daily quota, ALL channels see
`403 quotaExceeded` on:

- `videos.insert` (resumable upload init) — 1,600 units
- `channels.list?mine=true` — 1 unit (channel-id resolution)
- `subscriptions.insert` (cross-subscribe) — 50 units
- `videos.rate` (cross-engagement like) — 50 units
- `commentThreads.insert` — 50 units

The 10,000-unit daily allocation can fund roughly: 6 video uploads +
~70 cross-channel likes + ~70 subscriptions before exhaustion.

## Reset

Quota resets daily at **midnight Pacific Time** (08:00 UTC during PDT,
07:00 UTC during PST). Google does not pro-rate or partial-reset.

## Same-day ship path when quota is exhausted

Manual Studio upload via [studio.youtube.com](https://studio.youtube.com)
ALWAYS works — Studio is a first-party UI flow, doesn't consume the
public Data API quota. When quota is burned and you need to ship today:

1. Open `studio.youtube.com/channel/upload`.
2. Switch to the target channel via top-right avatar if needed.
3. Drag the mp4 from Finder.
4. Set unlisted/public, fill title/desc/tags manually.
5. After upload, defer `pipeline.research.cross_engage backfill` to the
   next day's quota window.

Per-slug deferred work goes in `<channel>/uploads/_pending.md` so the
next session picks up cross-engage automatically.

## Detecting quota exhaustion

Any of:

- `python -m pipeline.research.cross_engage refresh-ids` returns
  `quotaExceeded` for **multiple** channels (not just one).
- `pipeline.upload.youtube_upload` raises `HttpError 403` with
  `reason: 'quotaExceeded'`.
- `inspect_token_status('<slug>')['state']` is `ok` but a channel-id
  call still 403s — that's the smoking gun (token is fine, project is
  out).

## Capacity-increase paths

If the daily quota becomes a real constraint:

1. **Quota-increase request** via Google Cloud Console
   (`console.cloud.google.com → APIs & Services → YouTube Data API v3 →
   Quotas → Edit quotas`). Requires written justification + audit
   review (~7-day turnaround historically).
2. **Multi-project sharding** — issue separate client_ids from
   different Google Cloud projects, point each ytfactory channel at
   its own project. Decouples quotas at the cost of OAuth-flow
   plumbing and ~30 min of GCP-console work per project.
3. **Resumable upload reuse** — if the same mp4 uploads partially fail,
   resume the existing upload session (free) instead of starting a new
   `videos.insert` (1,600 units).

Right now (2026-05-08) the 10K/day pool is enough for ~6 channels at
1 upload/day each + light engagement. Sharding becomes worth it once
we ship 2+ uploads/day across the network.

## See also

- [`mystoriesanimated/learnings/youtube_upload_quota.md`](../mystoriesanimated/learnings/youtube_upload_quota.md)
  — DIFFERENT issue: per-channel `uploadLimitExceeded` (daily upload
  count cap from YouTube's content-policy side, not the Data API
  quota).
- `pipeline/upload.py::inspect_token_status` — token-level state checker
- `pipeline/research/cross_engage.py` — cross-channel engagement CLI
  (most quota-hungry consumer)
