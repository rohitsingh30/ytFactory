---
name: Long-form captions — authored text + chunk timing, never whisper
description: Sleep-mode caption stage MUST use the authored narration JSON text aligned to cached TTS chunk durations. The whisper-transcribe path is kept only as a fallback for imported scripts where no authored text exists.
type: feedback
---

History Recapped long-form sleep videos render captions from the **authored narration text** in `historyrecapped/narrations/<slug>.json`, timed against the cached TTS chunk durations. The legacy whisper-aligned path is preserved as a fallback but is never the default.

**Why:** The first v0.1 render of `western-front-1914-1918-sleep` (2026-05-04) used the whisper-aligned caption builder. The result was unusable — three concrete failure modes, all of which are intrinsic to running speech-to-text on our own TTS output:

1. **Case + punctuation are lost.** Whisper outputs lowercase, comma-free word streams. The cue would read `"railway lines the french by contrast can supply it only along a single secondary road that runs north from a small town called barladuk"` instead of `"...railway lines. The French, by contrast, can supply it only along a single secondary road that runs north from a small town called Bar-le-Duc."`
2. **Proper nouns get mangled.** `Bar-le-Duc` → `barladuk`. `Verdun`, `Ypres`, `Stosstruppen`, `Bruchmüller`, `Voie Sacrée` all become similarly bad. Whisper's English LM cannot guess French/German proper nouns from phonetic-distance.
3. **Hallucinations during quiet stretches.** At 5400s the burned cue read `"the German army is to be sent to Paris by the end of the month."` — that sentence is **not in the narration**. The actual audio at that timestamp was the closer ask. mlx-whisper has known hallucination behavior on long audio with silence.

Every one of those bugs vanishes the moment we feed the authored text instead of re-transcribing.

**How to apply:**

- `build_caption_pngs_from_chunks(narration_text, chunk_wavs, join_silence_s, ...)` in `historyrecapped/scripts/render_long_form.py` is the new default path. It:
  1. Re-splits the authored narration with the same `_split_into_chunks(text, target_chars)` algorithm used during TTS, producing a chunk-text list of identical length to the cached chunk wavs (asserted at runtime).
  2. ffprobes each chunk wav for its post-atempo duration.
  3. Each chunk i starts at `sum(prev durations) + i * join_silence_s` from the start of `narration.wav`.
  4. Inside each chunk, splits text on `.!?` into sentences and distributes time proportionally to character count — gives sentence-level cues that follow the spoken cadence.
  5. Renders one PNG per cue with proper case, punctuation, and proper nouns intact.

- The opt-in `long_form.caption_align: whisper` in `historyrecapped/config.yaml` keeps the old `build_caption_pngs` path callable for imported scripts that lack a canonical narration JSON.

- Re-render workflow when a captioning bug ships:
  ```bash
  rm historyrecapped/cache/<slug>/captions/cap_*.png
  rm historyrecapped/shorts/<slug>.mp4
  .venv/bin/python historyrecapped/scripts/render_long_form.py --channel historyrecapped --slug <slug>
  ```
  TTS chunks and the trimmed clip cache survive; only the captions stage + final mux re-run.

- **Class-of-bug rule:** never whisper-transcribe text we already authored. Whisper is the right tool for SOURCE audio (e.g. footage commentator alignment in sports-stories Shorts) where the text is unknown. For OUR TTS output, the authored text is canonical and whisper is strictly worse.

Memory mirror: `/Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/feedback_long_form_captions_authored_not_whisper.md`
