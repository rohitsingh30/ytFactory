# Cloud TTS loudness gap (CLASS-OF-BUG, 2026-05-12)

> **TL;DR** — Every Cloud Run TTS provider in our stack (Chatterbox,
> Higgs Audio, Indic-Parler, F5) emits audio 15-25 dB quieter than the
> on-laptop F5/Kokoro fallbacks the channel YAMLs were originally
> tuned against. Without a normalization step the rendered mp4 lands
> at mean_volume ≈ -32 dB which reads as silent on phone speakers.
> The fix is a single-pass `loudnorm=I=-16:TP=-1.5:LRA=11` on the
> narration leg in every renderer's final mux, BEFORE the
> channel-YAML `volume()` trim is applied.

## What surfaced this

Render job [8413e79d…](https://console.cloud.google.com/run/jobs/details/asia-southeast1/ytfactory-render-worker-v2/executions/ytfactory-render-worker-v2-9j8nr)
on 2026-05-12 — a 30-min mystoriesanimated long-form. User reported
"no audio? wtf". Probe of the rendered mp4:

```bash
$ ffmpeg -i short.mp4 -af volumedetect -f null /dev/null
mean_volume: -32.1 dB    # YouTube spoken-word target ≈ -16 LUFS
max_volume:  -12.0 dB
```

The audio track was present (mono AAC, 24 kHz, 1035s). It was just
~16 dB below YouTube's preferred loudness — barely audible on a
phone speaker, "completely silent" through QuickTime preview at
default volume.

## Root cause

The channel YAML's audio mix levels were calibrated against on-laptop
TTS providers (F5-TTS via mflux, Kokoro for Hindi). Both produce audio
peaking around 0 dBFS. The `audio_narration_db: -6` trim brings that
down to a comfortable -6 dB, then `audio_music_bed_db: -28` keeps
music well below it. The math worked.

Cloud TTS providers (Chatterbox cloned from `sarah.wav`, Higgs Audio,
Indic-Parler) don't peak at 0 dBFS — they emit at roughly -16 to
-20 dBFS source. After the `-6 dB` channel trim and amix with the
music bed, the result lands at -32 dB. Source amplitude varies between
providers AND between voice clones AND between sentences — there is
no single channel YAML that can compensate for this without
normalization.

The channel YAML knew about the Chatterbox slowdown (`atempo` for
chunked-TTS pacing) but nothing about amplitude. The output is
well-paced and inaudible.

## Fix (shipped commit `bfbbec1`, 2026-05-12)

`pipeline/render/long_form.py::final_mux` — prepend a single-pass
`loudnorm` to the narration leg only, BEFORE the `volume()` trim
applies:

```python
a_flt = (
    f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume={narration_db}dB[narr];"
    f"[2:a]volume={music_db}dB[bed];"
    f"[narr][bed]amix=inputs=2:duration=first:dropout_transition=2[a]"
)
```

This brings every TTS provider to a consistent floor (`I=-16` LUFS,
YouTube's spoken-word target) regardless of source amplitude, then
the channel YAML's `audio_narration_db` setting keeps its original
"trim around the canonical narration level" semantics — channels
that prefer narration a few dB below the floor still get that.

**Music leg deliberately untouched.** Auto-gaining the music bed is a
known antipattern: loudnorm on music would pump volume during
narration gaps as it tries to maintain target loudness, producing
the "music swelling between sentences" effect that's the dead
giveaway of poorly-mixed YouTube content.

## Why a single-pass is enough

Two-pass loudnorm gives marginally tighter LUFS adherence (within
~0.5 LU vs ~1-2 LU for single-pass) but doubles render time on the
TTS leg. For our spoken-word use case the precision difference is
inaudible against the music bed AND against the variability of
listener playback environments (phone speaker vs Bluetooth vs
headphones). Single-pass keeps the filter chain a one-liner.

If a future channel needs broadcast-grade loudness compliance
(e.g. radio simulcast), bump to two-pass:

```bash
ffmpeg -i narration.wav -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=summary -f null -
# Read the measured_I / measured_TP / measured_LRA / measured_thresh values
ffmpeg -i narration.wav -af loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=...:measured_TP=...:measured_LRA=...:measured_thresh=...:linear=true narration_norm.wav
```

## Currently in scope

This fix lives in `pipeline/render/long_form.py::final_mux`. The
shorts renderer (`pipeline/render/shorts.py` → `pipeline/compose.py`)
has historically had its own loudness handling because Shorts narration
is short enough (< 60s) that amplitude variability is less consequential
— but the same fix should be considered there if the user reports
inaudible Shorts narration from a Cloud TTS provider. **Open
follow-up:** audit `pipeline/compose.py::compose_*` for the same gap.

`footage_only.py` and `sports_doc.py` route through `long_form.py`'s
`final_mux` for the actual mux call, so they inherit the fix
automatically.

## Channels affected (every one with `tts_provider: cloudrun_*`)

Per `pipeline/channels/*.yaml`, all 16 production YAML configs already
route to a cloudrun TTS provider (per the 2026-05-06 cutover — see
`docs/cloudrun_tts.md`). Every long-form render published through
this pipeline since the cloud TTS migration carried this loudness
gap; **the fix above is what makes those renders audible**.

The Hindi channel (`hindutavaanimated`) is on Kokoro `hf_alpha`
locally and is NOT affected — Kokoro emits near-0-dBFS source like
the rest of the laptop providers.

## Verification

For any new cloud-TTS render:

```bash
gsutil cp gs://ytfactory-prod-v2-artifacts/jobs/<id>/video/<slug>.mp4 .
ffmpeg -i <slug>.mp4 -af volumedetect -f null /dev/null 2>&1 | grep volume
# mean_volume should be in the -18 dB to -22 dB range
# max_volume should be in the -2 dB to -6 dB range
# Anything below -25 dB mean is the bug returning.
```

Pin: `tests/test_render_long_form.py::FinalMuxTests::test_narration_leg_runs_through_loudnorm`.

## Cross-references

- Pipeline source: `pipeline/render/long_form.py::final_mux`
- TTS routing: `docs/tts_stack.md`, `docs/cloudrun_tts.md`
- Cloud TTS migration: CLAUDE.md § "Cloud-first TTS migration (2026-05-06)"
- Memory: `feedback_cloud_tts_loudness_gap.md`
- Original render that surfaced this: job `8413e79dfbc244c3b27a46271d95f46d`
