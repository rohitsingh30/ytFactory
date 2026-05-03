# Watching hathi-raja-kahan-chale.mp4 (v5 — silenceremove + fadeout attempted)

**One-line gut take**: The two seam issues from v4 are still there — and after looking at the actual audio levels, I now understand WHY my "fixes" didn't fix them.
**Score (1–10, 10 = I'd share)**: **6 / 10** (same as v4, no real change)

The visual core stayed solid: yellow elephant locked, on-brand, multi-mascot beats working. But the audio seams I claimed I fixed are still broken — the fixes were aimed at the wrong layer.

---

## The actual root causes (from the data)

### Issue 1: Caption "Suno" at t=0, audio sung-Suno lands later

**What the audio data shows:**
- `narration.wav` at t=0: **-15 dB** (loud, already in vocal)
- t=0.5s: -19 dB
- t=0.8s: -20 dB
- The LOUDEST sample is at t=0

**What this means:** silenceremove with `start_threshold=-22dB` was too aggressive. It clipped INTO the actual sung "Suno" — eating the leading "S" attack. The song now starts mid-syllable. Whisper still labeled the resulting 0-1s span as "Suno" because that's the closest match, but the clearly-voiced "Suno" inside the trimmed audio doesn't fully sound until ~0.3-0.5s in. Caption "Suno" appears at t=0 → **the caption is now visible BEFORE the cleanly-pronounced word in the audio**, even though my fix was supposed to do the opposite.

### Issue 2: 1.41s of frozen elephant + 2.26s of caption-without-audio at the end

**What the audio data shows:**
- `narration.wav` duration: **60.0s** exactly
- `beats.json` last word "par!" timestamp: **62.26s**
- mp4 total: 63.67s

**What this means:** Whisper hallucinated word timestamps **past** the actual audio end (a known issue for sung audio — pipeline/beats.transcribe_words has a monotonic-clamp guard but no audio-duration cap). The compose pipeline overlays per-word caption PNGs at those Whisper timestamps, including the ones that fall after the audio has ended. Result:
- t=60s: audio ends (fadeout finishes cleanly — the afade fix worked)
- t=60-62.26s: caption "Phir milenge agle station par!" rolls **in silence** because the audio is over but Whisper said words happen here
- t=62.26-63.67s: 1.41s of frozen elephant, no caption, no audio

The user feels two weird beats: silent caption roll, then frozen-silent elephant.

---

## Per-frame fix table (the engineer)

| beat | timestamp | lens | what's wrong (concrete) | classification | the fix |
|---|---|---|---|---|---|
| 0 | 0.0–0.5s | **L2** | silenceremove with -22 dB threshold ate the leading "S" of "Suno". Audio starts mid-vocal at -15 dB (already loud); no ramp. Caption "Suno" rendered at 0-1s but actual clear vocal "Suno" lands ~0.3s in. | **CLASS-OF-BUG** | Two compounding fixes: (a) Drop silenceremove from `pipeline/audio.trim_song_for_short` defaults — it can't tell instrumental-pickup from vocal-attack on Suno songs. Replace with a fixed `audio_trim_start_s` knob that the user tunes per-channel (already exists; should be the only mechanism). (b) Sample audio RMS BEFORE passing to Whisper; if t=0 is already > -18 dB, the trim_start_s was over-aggressive — log a warning so the user knows to reduce it. |
| 14 | 60.0–62.26s | **L2, L5** | Whisper word timestamps for "Phir milenge agle station par!" extend to 62.26s but audio ends at 60s. Captions overlay during dead audio → user sees Hindi-English roll silently. | **CLASS-OF-BUG** | `pipeline/beats.transcribe_words` already has a monotonic-clamp guard from earlier this session. Add a SECOND clamp: cap every word.end (and word.start) to the actual audio duration. ffprobe the input WAV at the start of transcribe_words to get the cap. Drop any word whose start ≥ audio_duration. |
| (tail) | 62.26–63.67s | **L4** | 1.41s of frozen elephant with no audio, no caption. The fadeout (afade) WORKED — last 0.8s of audio fades — but then there's still a closer_hold_s of 1.0s (already tightened from 1.5s). | **CLASS-OF-BUG (smaller)** | For sung-audio channels, lower `closer_hold_s` further to 0.4-0.5s. The visual CTA (thumbs-up + play button drawn into the frame) reads in <0.5s; longer holds drag past the song fadeout. Or: align tail-pad-end to the LAST word.end (post-cap), not the cap + closer_hold_s — so silent-caption span gets folded into the closer hold. |

---

## Class-of-bug fixes

- **silenceremove can't tell instrumental from vocal-attack** — drop from default, rely on fixed `audio_trim_start_s` from YAML. Detected RMS at t=0 of the trimmed audio gives a "trimmed too aggressively" warning. (`pipeline/audio.trim_song_for_short`) — UPDATE the existing fix; my v5 attempt was wrong.

- **Whisper hallucinates word timestamps past audio duration** — `pipeline/beats.transcribe_words` — extend the existing monotonic sanitiser to also clamp word.start and word.end to `<= audio_duration_s`. Drop any word whose start ≥ duration. (principle: NEW — sung-audio overshoots ASR)

- **Closer hold for sung-audio channels** — `channels/rhymetimejunction/config.yaml` `closer_hold_s` should be even shorter (0.4-0.5s) since the embedded CTA reads instantly. (principle: NEW — closer_hold_s tunes per audio_provider; already partly applied, needs another notch down)

---

## What pulled me in

- **Visual style throughout** — 15 yellow elephant + crown + brick-red blanket beats hold consistently. No grey, no bear, no anime drift. Real progress.

## What pulled me out

- **t=0**: caption "Suno" before clear vocal — same as before, my fix was wrong-aimed.
- **t=60-62.26s**: "Phir milenge agle station par!" caption rolling in silence — different bug than I thought.
- **t=62.26-63.67s**: frozen elephant, dead silence — closer hold still too long.

## If I were the creator, the single highest-leverage change is:

**Cap Whisper word timestamps to audio duration in `pipeline/beats.transcribe_words`.** That single 5-line change fixes the silent-caption-roll at the end immediately. It also makes the post-caption tail naturally short. Then we tune `closer_hold_s` down to 0.5s and the closer feels intentional instead of broken.

The start-caption fix needs the OPPOSITE of what I did — REMOVE silenceremove, increase `audio_trim_start_s` from 3.0s to ~3.3s so the "S" of "Suno" doesn't get clipped.
