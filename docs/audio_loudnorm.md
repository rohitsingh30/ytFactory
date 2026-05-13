# `docs/audio_loudnorm.md` — long-form mux loudness post-mortem

The first end-to-end successful long-form render (job `0c05c335…` on
2026-05-13) shipped a 16:01 mp4 whose narration was inaudible on
phone speakers. Mean volume measured via `volumedetect`: **-28.4 dB**
(YouTube's spoken-word target is -14 LUFS — we were 14 LU below).
Audio stream WAS present (AAC LC, 44.1 kHz mono) and contained real
narration data. It was just dramatically too quiet to hear at normal
volume.

This doc explains the chain, the four bugs that combined to produce
the silent output, the fix stack landed on 2026-05-13, and the
regression tests that pin them.

---

## The audio chain (post-fix)

```
narration WAV (Cloud Run TTS — Chatterbox, ~-30 LUFS naturally)
      │
      ├─ pass 1: loudnorm measurement
      │   ffmpeg -i narration.wav \
      │     -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json \
      │     -f null -
      │   → measured_I, measured_LRA, measured_TP, measured_thresh
      │
      ├─ pass 2: loudnorm with measured values + linear=true
      │   loudnorm=I=-16:TP=-1.5:LRA=11
      │     :measured_I=...
      │     :measured_LRA=...
      │     :measured_TP=...
      │     :measured_thresh=...
      │     :offset=...
      │     :linear=true
      │   → narration at -16 LUFS ±0.5 LU
      │
      └─ volume={narration_db}dB trim — default 0.0 (was -6.0)
          → narration at -16 LUFS

music WAV (royalty-free ambient bed)
      └─ volume={music_db}dB — default -28 dB (-12 dB under narration)
          → music at -28 dB

[narration][music] amix=inputs=2:duration=first:dropout_transition=2
                       :normalize=0  ← KEY: do NOT auto-attenuate
      → final mix at -16 LUFS

→ AAC encode @ 192 kbps
→ post-mux verification: ffmpeg -af volumedetect on output mp4
   - if mean_volume deviates >4 LU from -16, raise RuntimeError
   - obs.track("audio_postmux_verify") for telemetry
```

---

## The four bugs that combined to produce -28.4 LUFS

### Bug D1 — `amix` halved the narration

**File**: `pipeline/render/long_form.py::final_mux` (the `a_flt` filter chain)

Pre-fix amix filter:
```
[narr][bed]amix=inputs=2:duration=first:dropout_transition=2[a]
```

`amix` defaults to `normalize=1`, which divides every input by the
input count to prevent clipping. Mixing 2 inputs → each gets halved
(-6 dB). Narration entered at -22 LUFS, exited at -28 LUFS. **Music
being quiet didn't help — `amix` halves regardless of input level.**

Fix: append `:normalize=0` to the amix filter so the filter sums
without auto-attenuation. We've already pre-normalised both inputs
upstream; we don't need amix to second-guess us.

### Bug D2 — `narration_db = -6.0` default trimmed the wrong way

**File**: `pipeline/render/long_form.py::final_mux` signature

Pre-fix default was `-6.0 dB`. Loudnorm normalises to -16 LUFS (already
2 dB below YouTube's -14 LUFS target — to leave headroom). Adding
`-6 dB` on top trimmed to -22 LUFS — already below YouTube's
auto-normalisation amplification ceiling.

Fix: change the default to `0.0`. Rely on loudnorm alone to land at
-16 LUFS; let YouTube's auto-normalisation bring that the final
2 dB up to -14 LUFS for spoken-word programmes.

### Bug D3 — Single-pass loudnorm is unreliable

ffmpeg's loudnorm in single-pass mode estimates input loudness on the
fly using a sliding-window EBU R128 algorithm. ffmpeg's own docs say
single-pass deviates ±3 LU from the target. For very-quiet input
(Cloud Run Chatterbox emits at ~-30 LUFS, 14 LU below F5), single-pass
guesses badly — typically under-amplifying by 5-7 LU.

Fix: switch to two-pass. First run measures `input_i / input_lra /
input_tp / input_thresh` and prints them as JSON. Second run pipes
those values back into loudnorm via `measured_I=...:measured_LRA=...`
etc, plus `linear=true` for distortion-free gain application. Lands
within ±0.5 LU of target.

When the measurement step fails (binary missing, parse error,
timeout), `_measure_loudness` returns `None` and the code falls back
to single-pass. Telemetry attribute `audio.loudnorm_mode` tags each
render so we can dashboard the noisy fallbacks.

### Bug D4 — No post-mux verification

The renderer never measured the post-mux mp4's loudness. A 6 dB miss
shipped silently as READY. Pre-fix, the only signal that the audio
was wrong was a downstream user trying to listen to the mp4.

Fix: after `final_mux` returns, run `ffmpeg -af volumedetect` on the
output mp4 and parse `mean_volume`. If the deviation from -16 LUFS
target exceeds ±4 LU (chosen to catch 5+ dB misses without tripping
on normal program-loudness variation), raise `RuntimeError`. The
worker catches the raise and marks the job FAILED with a clear
"audio loudness check failed" error so it doesn't go ready and ship.

`obs.track("audio_postmux_verify", metadata={"mean_volume_db",
"within_tolerance", "target_lufs"})` lands on every render so the
telemetry dashboard can plot loudness over time and surface
regressions.

---

## Combined effect (the math)

| Stage | Pre-fix LUFS | Post-fix LUFS |
|---|---|---|
| Narration WAV (Cloud Run TTS) | -30 | -30 |
| After loudnorm (single-pass → two-pass) | -18 (±3 error) | -16 (±0.5 error) |
| After `volume={narration_db}dB` trim (-6 dB → 0 dB) | -24 | -16 |
| After `amix:normalize` (1 → 0) | **-30** | **-16** |
| YouTube auto-normalisation target (-14 LUFS) | 16 LU below | 2 LU below ✅ |

Pre-fix the chain ATE 12 dB of signal level between TTS and final mp4.
Post-fix the chain preserves the -16 LUFS target across the whole
pipeline.

---

## Tests pinning the fix

`tests/test_final_mux_audio_chain.py`:

- `test_amix_filter_string_includes_normalize_zero` — pin filter
  contains `:normalize=0`. Catches D1 regression.
- `test_default_narration_db_is_zero` — pin `final_mux(narration_db=...)`
  default is `0.0`. Catches D2 regression.
- `test_two_pass_loudnorm_filter_chain_when_measured` — pin two-pass
  filter shape contains all five `measured_*` keys + `linear=true`.
  Catches D3 regression.
- `test_single_pass_fallback_when_measure_fails` — pin fallback path
  tags loudnorm_mode="single_pass_fallback" so dashboards can flag it.
- `test_verify_audio_loudness_passes_within_tolerance` — synthesise
  10 s of speech at known loudness, mux, verify within ±4 LU.
- `test_verify_audio_loudness_fails_when_too_quiet` — synthesise
  silent narration, mux, expect `RuntimeError("audio loudness check
  failed")`.

---

## Rollback

If a future change reintroduces the silent mp4, the following one-liner
fully reverts the audio chain to pre-2026-05-13:

```python
# pipeline/render/long_form.py::final_mux
narration_db: float = -6.0,  # was 0.0
# and replace the a_flt block with the single-pass single-line:
a_flt = (
    f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume={narration_db}dB[narr];"
    f"[2:a]volume={music_db}dB[bed];"
    f"[narr][bed]amix=inputs=2:duration=first:dropout_transition=2[a]"
)
```

Don't do this. The combined fixes are why long-form audio is now
audible.

---

## Related files

- `pipeline/render/long_form.py:_measure_loudness` — two-pass pass 1
- `pipeline/render/long_form.py:_verify_audio_loudness` — post-mux gate
- `pipeline/render/long_form.py:final_mux` — the assembled chain
- `tests/test_final_mux_audio_chain.py` — regression tests
- `pipeline/observability` — telemetry attributes pinned per render
