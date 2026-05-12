---
name: external_song mode incompatible with cron-driven renders
description: rhymetimejunction's audio_provider=external_song requires a per-job-id WAV the curator can't pre-stage, so cron renders fail 100% on the audio precondition; switch to audio_provider=sunoapi for cron lanes
type: project
---

# `audio_provider: external_song` is incompatible with cron-driven renders

**Established 2026-05-13** after the `/api/scheduler/tick` cron fired
22 times in 36 h for `hathi-raja-kahan-chale` and every attempt died
~32-46 s in with:

```
channel uses audio_provider: external_song but the source audio file does not exist:
  /workspace/rhymetimejunction/songs/hathi-raja-kahan-chale-2972b69e.wav
```

**Why:** the renderer suffixes the channel slug with `-<jobid8>`
(e.g. `hathi-raja-kahan-chale` → `hathi-raja-kahan-chale-2972b69e`)
to bust per-render caches in the cast / audio / image stages. So
even a curator who pre-staged `songs/hathi-raja-kahan-chale.wav`
in GCS would never see it picked up — the runtime path is always
`songs/<channel-slug>-<jobid8>.wav` and the `<jobid8>` is unknown
until the worker starts.

`external_song` mode was introduced as a single-render-test mode
(see `pipeline/channels/rhymetimejunction.yaml` v0 NOTE block:
"to avoid re-burning credits on a render that's primarily testing
visuals + the new flat-crayon style"). It assumes a human in the
loop knows the slug ahead of time. The cron lane has no human
gate, so this mode is structurally broken there.

**Fix shipped 2026-05-13:**

1. `pipeline/channels/rhymetimejunction.yaml`:
   `audio_provider: external_song` → `audio_provider: sunoapi`
   (preserves the comment explaining when external_song is the
   right choice for one-off renders).
2. `gs://ytfactory-prod-v2-state/rhymetimejunction/config.yaml`
   uploaded with the same edit so the cloud worker picks it up.
3. `sunoapi-key` secret created in Secret Manager with the value
   from `~/.env::SUNOAPI_API_KEY`; `roles/secretmanager.secretAccessor`
   granted to `render-runner@`; mounted on
   `ytfactory-render-worker-v2` JOB env as `SUNOAPI_API_KEY`.
4. `pipeline/channels.yaml`: `rhymetimejunction.in_rotation: false`
   (temporarily) until a manual cron tick confirms sunoapi → song →
   mp4 works end-to-end. Flip back to `true` after one clean render.

**Why sunoapi is cron-safe:** `pipeline/render/shorts.py:357` keys
the audio cache off the `(model, vocal, style, lyrics)` fingerprint
of the suno_prompt block — NOT the slug. Same lyrics produce the
same cache hit regardless of the runtime `<slug>-<jobid8>` suffix.
A cron tick that re-renders the same narration JSON pays the
sunoapi cost once and replays the cached WAV every subsequent tick.

**How to apply (rule for future channels):**

- A channel intended for cron rotation MUST NOT use
  `audio_provider: external_song`. The check belongs in
  `pipeline/channels.py` (audit at load time when `in_rotation: true`)
  but isn't wired yet.
- A channel using `external_song` MUST set `in_rotation: false`
  AND have a comment explaining the human-in-the-loop assumption.
- Renderer's slug-suffix behaviour is documented in
  `pipeline/render/shorts.py::_external_song_path` —
  any new audio provider that participates in the cron lane MUST
  cache by content fingerprint, not slug.

**Cost note:** sunoapi.org generation is ~$0.05-0.10/song +
60-90 s of latency per fresh fingerprint. The 22 failed cron ticks
in 36 h burned ~22 × ~45 s of Cloud Run JOB compute for nothing
(~$0.20-0.30 in metered seconds). Sunoapi mode would have paid
that cost ONCE for the first tick and replayed cache 21 times.

**Memory pointer:** `feedback_rhymetimejunction_external_song_cron.md`

**Related:**
- `pipeline/channels/rhymetimejunction.yaml` — the flipped config
- `pipeline/render/shorts.py:325-410` — audio_provider dispatch +
  `_external_song_path` + sunoapi cache fingerprint
- `pipeline/tts/song.py:240-260` — sunoapi key check (raises a
  clear "set SUNOAPI_API_KEY" error if missing)
- `docs/channel-learnings/rhymetimejunction/channel.md` —
  channel-wide rules; this file extends the "Sung upgrade paths"
  section there
