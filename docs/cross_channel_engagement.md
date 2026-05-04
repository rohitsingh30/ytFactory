# Cross-channel engagement

Every owned YouTube channel automatically **likes**, **subscribes to**,
and registers a **natural view** for every other owned channel's
uploads. Running this from the same toolchain that publishes the videos
gives every new Short an immediate engagement floor (likes ≥ N-1, view
count ≥ 1, sibling channels already subscribed) without manual work.

## Wiring

- **Module**: `pipeline/cross_engage.py`
- **Hook**: called from `pipeline.upload.upload_short` right after
  `write_upload_record(...)` succeeds. Runs in a daemon thread so it
  doesn't slow the upload return.
- **Disable**: set env `YTFACTORY_CROSS_ENGAGE=0` before running the
  pipeline (CI smoke tests, debugging, etc.).

## Sibling discovery

A "sibling" is any account with a cached OAuth token at
`~/.config/ytfactory/youtube_token_<account>.json`. Adding a new channel
to the engagement pool = OAuthing it once via
`pipeline.upload.authenticate(account=...)`. No registry to maintain.

The dummy account `default` is excluded; backup tokens
(`*.fluted-backup`) are filtered out by extension check.

## OAuth scope changes

`pipeline/upload.py` now requests three scopes:

```
youtube.upload     # video.insert
youtube.readonly   # channels.list (Part-2 sub gate, channel-id resolve)
youtube            # videos.rate (likes), subscriptions.insert
```

Existing tokens (minted before the `youtube` write scope was added) are
detected as scope-stale (we read scopes from the token JSON directly —
`Credentials.scopes` is overwritten at construction time so it can't
detect drift). On drift, `authenticate()` triggers a re-auth in the
browser.

### Refresh-token preservation

Google deduplicates `refresh_token` issuance for previously-consented
apps even with `prompt=consent + access_type=offline`. To handle this,
`authenticate()`:

1. Reads the prior token's `refresh_token` from disk before kicking off
   the new flow.
2. Uses `prompt='consent select_account'` to maximise odds of fresh
   issuance.
3. After the new OAuth response, if `creds.refresh_token` is None,
   splices the prior `refresh_token` forward — the old refresh_token
   still works because Google's user-grant record is updated server-side
   when consent is re-given, even if no new refresh_token is returned.
4. If neither path yields a refresh_token, raises with instructions to
   revoke the app at `https://myaccount.google.com/connections` and
   re-run.

`authenticate()` also has a fallback for tokens already saved without
`refresh_token`: it constructs `Credentials` manually with
`refresh_token=None` so the still-valid access_token (~1h) keeps working
while the operator schedules a proper revoke + re-auth.

## Dashboard hookup

`web/server.py` exposes:

- `GET  /api/youtube/auth/status` — per-account state derived from
  `pipeline.upload.inspect_token_status()`. States: `ok`,
  `no_refresh_token`, `missing_scopes`, `unreadable`, `missing`.
- `POST /api/youtube/auth/start/{account}` — spawns a subprocess that
  runs the OAuth flow on `localhost:8089`, parses the auth URL out of
  the subprocess's stdout, and returns it. The dashboard pops a new tab
  with that URL; the subprocess waits on the localhost callback.
- `GET  /api/youtube/auth/poll/{account}` — polls the in-flight
  subprocess until it exits (completed / failed).
- `POST /api/youtube/cross_engage/subscribe_all` — runs
  `cross_engage.subscribe_all_pairs()` server-side.

`web/static/dashboard.html` shows a "YouTube auth · cross-engagement"
tile with one card per sibling account (current state + Re-auth button)
and a single "Cross-subscribe all" button.

## Channel-ID registry

`~/.config/ytfactory/channel_ids.json` maps each account → that
account's YouTube channel ID. Resolved lazily via
`channels.list(mine=True)` and cached. Force-refresh with
`python -m pipeline.cross_engage refresh-ids`.

## Three engagement actions

1. **subscribe_pair** — `subscriptions().insert` with the target
   channel ID. Catches `subscriptionDuplicate` so re-runs are no-ops.
2. **like_video** — `videos().rate(rating="like")`. YouTube treats
   re-rating with the same value as a no-op, so this is naturally
   idempotent.
3. **play_view** — Playwright headless Chromium tab with `--mute-audio`
   plus `--autoplay-policy=no-user-gesture-required`. Navigates to
   `youtube.com/watch?v=<id>`, dismisses the consent banner, calls
   `video.play()` defensively, sleeps for `duration_s` (default 45),
   closes. **Anonymous** — no login. We don't want a pattern of every
   sibling account watching every other sibling video; an anonymous
   view is what a real viewer does.

## Quota cost per upload

| Action | Cost | × N siblings | Notes |
|---|---|---|---|
| channels.list (mine=True) | 1u | once per channel ever (cached) | |
| videos.rate (like) | 50u | × (N-1) | 4 channels = 200u/upload |
| subscriptions.insert | 50u | one-time at setup | not per upload |
| Playwright view | 0u | once per upload | no API call |

Default daily quota is 10,000u. With N=5 channels this caps engagement
at ~50 uploads/day from any single account, which is far above
realistic publishing cadence.

## Setup procedure

1. **First time only** — auth every sibling account so its token file
   exists. The simplest path is to attempt an upload from each channel,
   which triggers the browser flow.
2. After scopes expanded, **re-auth** any account whose token predates
   the change. `authenticate()` will print
   `cached token for X missing scopes [...] ; re-auth required` and
   open the browser flow automatically.
3. Resolve channel IDs:
   ```
   python -m pipeline.cross_engage refresh-ids
   ```
4. Cross-subscribe every owned channel pair (one-time):
   ```
   python -m pipeline.cross_engage subscribe-all
   ```
5. From this point on, every successful `upload_short` call
   automatically fans out engagement. No further setup.

## CLI

```
python -m pipeline.cross_engage list             # show siblings + channel IDs
python -m pipeline.cross_engage refresh-ids      # re-resolve channel IDs
python -m pipeline.cross_engage subscribe-all    # one-time cross-subscribe pass
python -m pipeline.cross_engage like  VIDEO_ID --uploader X     # all siblings≠X like VIDEO_ID
python -m pipeline.cross_engage view  VIDEO_ID [--seconds 45] [--headed]
python -m pipeline.cross_engage engage VIDEO_ID --uploader X    # full sync fan-out (likes + view)
```

## Disabling

- Per-process: `YTFACTORY_CROSS_ENGAGE=0 python -m ...`
- Globally: comment out the `cross_engage.engage_after_upload(...)` call
  at the bottom of `upload_short()` in `pipeline/upload.py`.
