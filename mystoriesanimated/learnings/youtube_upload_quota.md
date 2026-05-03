---
name: mystoriesanimated YouTube upload quota
description: Daily YouTube upload limit on the mystoriesanimated channel — hit during the 2026-05-02 auto-loop, causes uploadLimitExceeded errors that no amount of retrying clears
type: project
originSessionId: 5e765d5b-0fe6-4e45-ab49-d0138cfa29f4
---
`mystoriesanimated` channel can hit a daily upload cap that surfaces as: `YouTube API error 400 ... 'reason': 'uploadLimitExceeded' ... 'The user has exceeded the number of videos they may upload.'`

**Why:** Hit on 2026-05-02 during the auto-upload loop after a successful re-auth. YouTube enforces a per-channel/day video upload limit (typically 15/day for unverified channels). The token was fresh and valid; the rejection is at the YouTube layer, not OAuth.

**How to apply:**
- When the upload loop hits this error, STOP — don't retry, don't kick off another ~1hr render. Tell the user; the cooldown is unpredictable.
- **Privacy does NOT bypass this.** Tried `privacy: "private"` + `force: true` on 2026-05-02 — same error. Private/unlisted/public all count against the cap.
- Phone verification does NOT lift this cap. It's a separate per-channel anti-spam throttle that YouTube applies based on upload pattern + content signature, not on verification status.
- Don't confuse this with `interactive=False` / token errors — those auto-recover via the OAuth flow. `uploadLimitExceeded` is account-level and cannot be self-healed.
- Bursting uploads (e.g. 8 AI-generated shorts in 8 minutes) tightens the cap further. Concrete observation 2026-05-02: the channel did 8 in 8 min on 2026-05-01 15:41–15:49 UTC, then was blocked at upload #9 even after >27h elapsed. That's longer than the documented 24h rolling window — YouTube tightened the cap.
- Long-term fixes (all require user action — flag, don't patch around):
  1. Spread uploads to 1–2/day at human-paced intervals
  2. Get the channel above 1k subs / monetized → caps lift
  3. Request a YouTube Data API quota bump (GCP form, 1–6 wk turnaround) — but this is for the API quota, NOT the channel cap; not directly applicable here
  4. Use a different YouTube account that hasn't been flagged
- If the loop should still produce shorts during the cooldown, render-only (skip the upload step) and queue mp4s on disk under `data/shorts/` for batch upload later when the cap recovers.
