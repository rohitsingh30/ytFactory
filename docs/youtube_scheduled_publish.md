# YouTube scheduled-publish pattern

**Established 2026-05-08** for the cosmosdecoded 5-shorts release +
extended for the cron uploader.

## How to schedule a private video for public release

```python
from pipeline import upload as up
from googleapiclient.discovery import build

creds = up.authenticate("<channel>", interactive=False)
yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

yt.videos().update(
    part="status",
    body={
        "id": video_id,
        "status": {
            "privacyStatus": "private",   # MUST stay private
            "publishAt": "2026-05-08T15:30:00.000Z",  # ISO 8601 UTC, .000Z
            "selfDeclaredMadeForKids": False,
        },
    },
).execute()
```

When the publishAt timestamp arrives, YouTube atomically flips
`privacyStatus` from `private` to `public`. The video is now
discoverable.

## Format gotchas

- **`publishAt` must be in UTC** with `Z` suffix or `+00:00`.
  Local-time strings get rejected.
- **Milliseconds suffix `.000Z`** is required by some clients;
  always include them.
- **`privacyStatus` must be `private`** at the time of update;
  `unlisted` does NOT flip to `public` at publishAt — it stays
  unlisted.
- **`selfDeclaredMadeForKids` must be set** in the same update or
  YouTube returns 400 with "Self-declared MFK status missing".

## Common cadence patterns

| Use case | Cadence |
|---|---|
| Initial channel batch (5 shorts) | every 3 hours |
| Backlog drain (100 shorts) | 10/day at 2-hour intervals from 9am IST |
| Daily release (1/day) | same time every day, e.g. 17:00 IST |
| News-channel daily | weekday 9am, weekend 11am |

For Cosmos Decoded the cron uploader uses
`SCHEDULE_HOURS_IST = [9, 11, 13, 15, 17, 19, 21, 23, 1, 3]` —
10 slots/day = matches `10/day at 2-hour intervals` rule.

## Computing the publish-at timestamp (REVISED 2026-05-08)

**Bug found in v1:** the original implementation computed the *next* IST
slot strictly greater than `now_ist.hour` and scheduled the publish for
that slot. With cron firing AT 9, 11, 13, ... IST, this meant a 9:00
cron firing scheduled the upload for 11:00 publish — a 2-hour lag the
user did not want. First production-evidence: alphafold-2020-protein
uploaded at 13:01:16 IST 2026-05-08, scheduled for 15:00:00 IST publish
(next slot from 13). User wanted publish AT the firing time.

**Fix:** schedule for `now + 5 minutes` (the +5min satisfies YouTube's
"publishAt must be future" rule and gives the upload network round-trip
a buffer to settle). The cron schedule itself dictates the publish
cadence — no need to compute "next slot" at all.

```python
def publish_at_utc():
    import datetime as dt
    # Use timezone-aware UTC to silence deprecation warning.
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    publish_at = now_utc + dt.timedelta(minutes=5)
    return publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
```

The cron firing at 9, 11, 13, ... IST → each firing schedules ~5 min
ahead → user-visible cadence is "1 publish every 2 hours starting at
≈9:05 IST". Matches the user's requirement.

**Lesson:** when cron itself defines the cadence, the script's job is
"publish ASAP after this firing", not "compute the cadence again". The
double-cadence-computation was the bug.

## Auditing scheduled videos

YouTube Studio → Content → Filter by "Scheduled" shows the queue.

Programmatic:

```python
yt.videos().list(part="status", id=video_id).execute()["items"][0]["status"]
# {"privacyStatus": "private", "publishAt": "2026-05-08T15:30:00.000Z", ...}
```

## Cancelling / rescheduling

To cancel a scheduled publish, do another `videos().update` with
`status.publishAt` removed (or set to a far-future date). The video
remains private indefinitely.

To reschedule, just update with a new `publishAt`.

## Memory pointer

`feedback_youtube_scheduled_publish.md`.
