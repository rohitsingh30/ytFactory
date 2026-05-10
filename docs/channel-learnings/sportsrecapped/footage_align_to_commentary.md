---
name: Pin sports footage in_s/out_s to commentator's word timestamps via whisper
description: For sports footage cuts, run whisper on the source clip to find the commentator's call words and align in_s/out_s to those — not arbitrary timestamps or audio-RMS peaks alone
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
The right footage cut window is the commentator's complete call arc — from the buildup phrase through the goal call's natural conclusion. Pin in_s/out_s to ACTUAL WORD TIMESTAMPS, not arbitrary seconds or audio-peak heuristics.

**Why:** caught on v10 Aguero render — window was 4.0-8.0s based on audio-peak detection. User flagged: "the cut is chopped in between, not till the net hitting, do it from where the commentator say headed for". Whisper revealed the commentary arc:
- t=3.66s "headed" (buildup phrase the user wanted as the IN)
- t=7.16s "Agüero!" (the strike)
- t=8.52s "They do it!"
- t=9.40s "City!"

Without the word-level timestamps, the audio-RMS peak alone (loudest 0.5s window) only finds the call PEAK — it can't tell us where the buildup phrase begins or where the post-call exit phrase ends. v9-v10 cuts ended right at the call peak (8s), missing "They do it! City!" — which is the iconic 1.6s after-the-call payoff that tells the viewer "the goal happened."

**How to apply:**
- Before picking in_s/out_s for a sports footage cut, run whisper on the source clip:
  ```bash
  .venv/bin/python -c "
  from pipeline import asr
  from pathlib import Path
  src = Path('data/intermediate/<channel>/footage/sources/<video_id>.mp4')
  result = asr.transcribe(src, provider='whisper_mlx',
                          model='mlx-community/whisper-large-v3-mlx-4bit')
  for seg in result.get('segments', []):
      for w in seg.get('words', []):
          print(f'  [{w[\"start\"]:5.2f}-{w[\"end\"]:5.2f}]  {w[\"word\"].strip()}')
  "
  ```
- Identify three anchor words in the commentary arc:
  1. **IN word**: the first word of the buildup phrase ("headed", "He shoots", "Watch this", "Last second")
  2. **CALL word**: the goal call peak ("AGUEROOOOO", "GOAL", the player's surname shouted)
  3. **OUT word**: the last word of the post-call payoff phrase ("They do it! City!", "It's in the net", "Champions!")
- Set `in_s = IN word's start`, `out_s = OUT word's end + 0.4s breath`. Don't snap to seconds — fractional precision matters; commentators speak fast.
- Combine with the existing rules: `out_s` should still NOT extend into the broadcast's stylized post-call replay (desaturation / text graphic — see feedback_sports_footage_dont_overshoot.md). The commentary-arc OUT and the editorial-cut OUT often coincide; pick whichever comes earlier.
- v2 idea (not yet implemented): `pipeline/footage.py:detect_call_arc` helper that takes a source clip and returns suggested in_s/out_s by transcribing + scanning for known call patterns.