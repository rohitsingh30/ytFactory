---
name: voice-bench
description: >-
  For a given channel, render the SAME 30-second narration sample through every cloud TTS voice in parallel (chatterbox, f5, higgs, cosyvoice, indicparler, indicf5 — the 5+ voices we already pay GPU credits for) and produce a side-by-side comparison so the channel's voice choice is data-driven. Outputs N mp3s + spectrograms + a one-page markdown report scoring each voice on RTF, hallucination rate (Whisper word-error vs source script), prosody variance, hard-cut artifacts, and per-minute cost. Ships the winner as a recommended tts_provider line + ref-audio path patch for the channel's config.yaml, and writes <channel>/learnings/voice_choice.md. Use when the user says "what's the best voice for <channel>", "which TTS should we use", "/voice-bench", "A/B the voices", "compare cloudrun voices", "why are we on chatterbox", or after a TTS service is (re)deployed. For per-render warm-up use POST /api/cloud/warm. For audio quality critique of an EXISTING render use /critique-audio.
---

# /voice-bench — data-driven cloud TTS voice selection per channel

## Why this skill exists

We pay for **6 cloud TTS voices** — chatterbox, f5, higgs, cosyvoice,
indicparler, indicf5. Every channel YAML hardcodes one. None of those
choices are backed by a side-by-side benchmark on the channel's own
content. The defaults came from "what was newest when this channel
launched" — chatterbox for English Shorts because it shipped during
mystoriesanimated's bring-up, indicparler for Hindi because it was the
only Hindi cloud option at the time IndicF5 launched.

We have at least three known mismatches right now:

- `cosyvoice` is "NOT useful for Hindi (proven)" per
  `docs/cloudrun_tts.md` — but did anyone bench it for English narrative
  content? Maybe its prosody is better than chatterbox for sleep-history.
- `higgs` is the most expressive voice cloned per the original mirror,
  but every English channel still routes to chatterbox by default.
- `indicparler` is description-driven (Hindi voice via prompt) — but
  IndicF5 has zero-shot voice cloning. Which actually sounds better
  on a 50-min katha?

This skill answers all three with a 3-minute benchmark.

## How to run it

### 1. Pick the channel + sample

```
/voice-bench <channel>                     # uses channel's most recent narration
/voice-bench <channel> --slug <slug>       # specific past render's narration
/voice-bench <channel> --text "<inline>"   # paste a 30-sec sample directly
/voice-bench --hindi                       # bench Hindi-only voices on a default sample
/voice-bench --english                     # bench English-only voices on a default sample
```

If no sample is provided, pull a random 30-second window from the most
recent successful narration in `<channel>/narrations/`. Strip pronounce
hints + bracket directions — feed each voice the same raw text.

### 2. Confirm scope

```
voice-bench:
  channel  = cosmosdecoded
  sample   = "On May 29 1919, two photographic plates from Príncipe …" (32 s @ 165 wpm)
  voices   = chatterbox, f5, higgs, cosyvoice  (English only — channel is English)
  est cost = ~$0.04 (4 × ~$0.01 per L4-min)
```

Skip Hindi voices for English channels and vice versa (use
`pipeline/niches.py:NICHE_CHANNEL` + the channel's `lang` field in
`config.yaml` to decide). The user can `--all` to override.

### 3. Render in parallel

```python
import concurrent.futures, time
from pipeline.tts import cloudrun

def synth_one(voice, text, out_path):
    t0 = time.time()
    cloudrun._synth_cloudrun(text=text, voice=voice, out_path=out_path)
    return {"voice": voice, "wall_s": time.time() - t0, "path": out_path}

VOICES = ["chatterbox", "f5", "higgs", "cosyvoice"]  # from step 2
with concurrent.futures.ThreadPoolExecutor(max_workers=len(VOICES)) as ex:
    results = list(ex.map(
        lambda v: synth_one(v, sample_text, f"data/_bench/voice/{channel}/{v}.mp3"),
        VOICES,
    ))
```

Each cloud service is its own L4 → no GPU contention. With all 4
warm (call `cloud/warm_tts_services.sh chatterbox f5 higgs cosyvoice indicparler indicf5` (or `POST /api/cloud/warm`) first), this finishes in ~30-60 s.

### 4. Score each output

Per voice:

| Metric | How |
|---|---|
| **RTF** | `wall_s / audio_duration_s` from step 3 + `ffprobe` |
| **WER** | transcribe with `whisper-large-v3` (local), normalize, `jiwer.wer(reference, hypothesis)`. Reference is the source script. |
| **prosody variance** | F0 std-dev across the clip via `librosa.pyin` — too low = monotone, too high = unstable |
| **hard cuts** | count zero-crossing discontinuities > 0.1 s of silence mid-sentence |
| **cost / min** | from `data/_bench/cloud_cost.jsonl` per-service projected cost ÷ minutes synthesized |
| **subjective** | (deferred to operator — skill writes the mp3s and spectrograms; operator listens) |

### 5. One-page comparison report

```
voice-bench: cosmosdecoded  2026-05-10
sample: "On May 29 1919, two photographic plates …" (32 s ref @ 165 wpm)

VOICE         RTF    WER    PROSODY  HARD-CUTS  $/MIN   NOTES
chatterbox    0.21   0.012  0.42     0          $0.014  current default; clean; mid-prosody
f5            0.24   0.008  0.51     0          $0.012  best WER; warmer cadence
higgs         0.31   0.018  0.68     1          $0.018  most expressive but 1 hard cut at 14.2s
cosyvoice     0.27   0.041  0.39     0          $0.016  WER 4.1% — model misread "Príncipe" as "principal"

WINNER (data-driven): f5
  - lowest WER (0.8% vs chatterbox 1.2%)
  - higher prosody variance (warmer cadence — fits cosmos channel arc)
  - same cost (cloud minutes priced uniformly)
  - no hard cuts

LISTEN BEFORE COMMITTING:
  data/_bench/voice/cosmosdecoded/chatterbox.mp3
  data/_bench/voice/cosmosdecoded/f5.mp3
  data/_bench/voice/cosmosdecoded/higgs.mp3
  data/_bench/voice/cosmosdecoded/cosyvoice.mp3

PROPOSED PATCH:
  cosmosdecoded/config.yaml
  - tts_provider: cloudrun_chatterbox
  + tts_provider: cloudrun_f5
  + tts_voice_ref: cosmosdecoded/branding/voice_ref/sarah.wav  # if zero-shot

  apply with:  /voice-bench cosmosdecoded --apply
```

### 6. The `--apply` step

Only on explicit `--apply` flag (never auto):

1. Patch `<channel>/config.yaml` with the winner.
2. Patch every variant YAML under `<channel>/variants/` similarly.
3. Write `<channel>/learnings/voice_choice.md` with the full report
   embedded so future operators see why this voice was picked.
4. Confirm the winner is green via the `/app/cloud` Health row for
   `<winner-service>` (or `curl /api/cloud/health | jq '.rows[] | select(.short=="<winner-service>")'`)
   before committing the patch.
5. Do NOT auto-commit. Print the diff and ask the operator to review.

### 7. Self-learning

- Append per-run results to `data/_bench/voice_bench.jsonl`.
- If the same voice wins on 3 consecutive benchmarks for the same
  channel/lang pair, propose locking it in
  `<channel>/learnings/channel.md` as a hard rule (operator confirms).
- If WER on any voice drifts > 2× over 30 days, flag as cloud regression
  → recommend `/app/cloud` Health section (or `GET /api/cloud/health`) + possibly `gcloud builds submit` per `docs/cloud_service_dep_playbook.md`
  to redeploy with the prior known-good upstream commit.

## Cross-references

- `docs/cloudrun_tts.md` — voice availability + per-service quirks
  (esp. cosyvoice-not-useful-for-Hindi).
- `pipeline/tts/cloudrun.py` — the dispatcher this skill calls.
- `/critique-audio` — orthogonal — judges ONE rendered audio for
  quality; voice-bench picks WHICH voice to render with in the first
  place.
- `<channel>/learnings/voice_choice.md` — output of `--apply`.
