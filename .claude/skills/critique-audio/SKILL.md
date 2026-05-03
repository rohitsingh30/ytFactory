---
name: critique-audio
description: Listen to a synthesised narration WAV as if you were a podcast / audiobook editor AND a TTS pipeline engineer. Catches pronunciation bugs (acronyms read as words, numbers digit-spelled, all-caps tokens letter-spelled), pacing issues (WPM out of range, monotonous tempo), missing breath points, and script flaws (run-on sentences, awkward phrasing, robotic CTA). Surfaces fixes both for THIS narration (one-off rewrites) AND the pipeline (class-of-bug code changes that prevent regression on future renders). Use when the user says "critique this audio", "the audio sounds rushed", "AITA is being mispronounced", "the closer sounds robotic", "build audio critique", or after generating a fresh narration.wav.
---

# /critique-audio — review a narration WAV the way a podcast editor would

Two jobs at once:

1. **Be the audio editor.** Pretend you're producing a top-tier
   storytelling podcast. The narration just came back from the TTS
   booth. Listen for: misread words, robotic pacing, dead air,
   missed pauses, and any moment that breaks the spell of a real
   human telling a story.

2. **Be the pipeline engineer.** For every issue you spot, classify
   it ONE-OFF (fix this script) vs CLASS-OF-BUG (fix the pipeline so
   the next 100 narrations don't repeat it). Class-of-bug findings
   MUST land in the codebase AND in this critic's own evaluation
   lenses (see step 7) — that's the feedback loop that makes the
   pipeline get smarter every render.

This skill is the human-readable mirror of ``pipeline/audio_critic.py``.
The module produces structured JSON; this skill produces a markdown
review the user can read AND a punch-list of code changes.

## How to run it

### 1. Pick the audio + script

- If the user passed a path, use it.
- Otherwise pick the most recently modified ``data/cache/<slug>/narration.wav``,
  OR a WAV in ``~/Downloads/`` that the user mentioned in chat
  (``audio_v3_bella.wav``, ``audio_f5_phil.wav``, etc.).
- For the script: find ``data/intermediate/*/scripts/<slug>.json`` for the
  matching slug, OR ask the user for the script path if the WAV doesn't
  resolve to a slug. The narration text lives in ``narration`` field.

State the audio path and script path in one line before continuing.

### 2. Run the structured critic

The heavy lifting (Whisper transcription, metric computation,
opus call) is already in ``pipeline/audio_critic.py``. Don't
re-implement — just invoke it:

```bash
.venv/bin/python -m pipeline.audio_critic \
    <audio_path> <script_path> \
    --out data/critiques/<slug>/<slug>.audio.score.json
```

That emits a JSON with ``score``, ``one_line_take``, ``top_issues``,
``per_finding``, ``script_corrections``, ``system_corrections``,
``highest_leverage_change``. Read that file — it's your structured
input. The opus call already walked the lenses; your job now is to
turn the JSON into a readable review and to ACT on the
``system_corrections``.

### 3. Walk the lenses one more time (sanity check)

The structured critic uses these lenses. Read the JSON's findings
and confirm they correspond to lenses; if you see something the
JSON missed, add it.

| # | lens | the question |
|---|---|---|
| L1 | **Pronunciation** | Words mis-rendered? Letter-spelled acronyms ("OUT" → "O U T"), digit-spelled numbers ("two zero zero zero"), names mispronounced. Cite source AND transcript form. |
| L2 | **Pacing** | WPM in 130-180 audiobook zone? 200+ is rushed; <110 is sedated. Tempo variation: does it slow on the kicker, speed on setup, or stay monotone? |
| L3 | **Pauses** | Does it breathe? Each ``\n\n`` paragraph break in the source SHOULD produce a >0.5s silence gap. Long stretches >5s without any silence ≥0.3s are run-ons. Pauses mid-clause are wrong-place pauses. |
| L4 | **Script** | Punctuation quality. Run-ons, missing periods, filler words ("just", "really", "kind of"), ambiguous pronouns. Sentences that don't stand alone as a subtitle. |
| L5 | **Emotion** | The script has dramatic beats — pivot, kicker, closer. Are they delivered flat or do they land? (Hard to tell from transcript alone but flag throwaway moments that should hit.) |
| L6 | **Closer** | Does it land cleanly? ≥0.5s silence before it? The narration MUST end with a NATURAL human CTA ("AITA? Tell me in the comments." / "What would you have done?") — NOT the robotic preset ("LIKE if YTA, COMMENT if NTA"). YTA/NTA in the closer must be expanded ("you're the asshole" / "not the asshole"), never letter-spelled or phoneticised. |
| L7 | **Acronym expansion** | AITA-class acronyms (AITA, WIBTA, YTA, NTA, ESH, NAH, MIL, FIL, SIL, BIL, DIL, OOP, NC) MUST expand to natural phrases in the audio: "AITA" → "am I the asshole", "MIL" → "mother in law". If transcript shows letters (Y T A) OR phoneticised garbage ("Aira"/"Antigua"), that's a class-of-bug — ``pipeline/audio.py:_ACRONYM_PHRASES`` is missing or wrong for that token. |
| L8 | **Euphemism handling** | Single-letter euphemisms ("f off", standalone "F"/"B") read as letter names ("eff", "bee") or get swallowed. Should be "eff off" / "freaking" / "damn" in source, OR handled by ``normalize_for_tts``. Flag any standalone single-letter euphemism token in the source. |
| L9 | **All-caps leakage** | Source ALL-CAPS emphasis tokens ("WHOLE", "AGAIN", "SIX") should be lowercased by ``normalize_for_tts`` before TTS. Verify by checking transcript: if these source tokens are still letter-spelled, the normalizer regressed. |
| L10 | **Binding integrity** | Word-overlap ratio between source and transcript ≥0.5? If <0.5, the wrong audio is bound to this script — declare binding failure as the top issue and skip rating other lenses. |

### 4. Write the human-readable review

Write to ``data/critiques/<slug>/<slug>.audio.md`` and print it
inline. Structure:

```markdown
# Listening to <slug> narration

**One-line gut take**: <does this sound human? would a podcast
listener stop after 5 seconds?>
**Score (1–10, 10 = I'd publish)**: <n>

## What I heard (the editor)
- **Pacing** — <WPM, breath points, where it rushes / drags>
- **Pronunciation** — <every misread word, source form vs transcript form>
- **Closer** — <does the ending land? human or robotic CTA?>
- **Other** — <emotion, fluency, dead air>

## Per-finding fix table (the engineer)

| category | timestamp | what's wrong | class | the fix |
|---|---|---|---|---|
| pronunciation | 12.4s | source "AITA?" → transcript "Aira?". Acronym phoneticised as a word. | **CLASS-OF-BUG** | `pipeline/audio.py:_ACRONYM_PHRASES` already maps AITA→"am I the asshole" but the case-insensitive matcher missed lowercase variants. Fix landed; verify on next render. |
| pacing | – | 257 WPM total — way above 130-180 storytelling zone. | **CLASS-OF-BUG** | Drop `tts_speed` to 0.90 in `channels/aita_animated.yaml`. Combined with the per-paragraph 0.55s pause stitching this lands ~150 WPM. |
| ... |

## Class-of-bug fixes for the next 100 Shorts

For each CLASS-OF-BUG row above, write the system correction with a
file:function target. Reference DESIGN.md §14 principles where one
already exists; flag NEW principles otherwise.

- **<issue_class>** — `<file:function>` — <fix>
  (principle: <#N or NEW>)

## What sounded right
- <specific moment + why>

## What broke immersion
- <specific timestamp + which lens caught it>

## Highest-leverage single change
<one thing — prefer a CLASS-OF-BUG fix over a one-off if both apply>
```

### 5. SHIP the class-of-bug fixes (this is the loop)

For each ``system_corrections`` entry in the JSON, do BOTH of the
following before closing the task:

**5a. Apply the code fix.** Edit the target file (``pipeline/audio.py``,
``pipeline/rewrite.py``, ``pipeline/script_check.py``,
``channels/<channel>.yaml``, etc.) per the ``fix`` field. Run any
relevant smoke test.

**5b. Codify it as a critic lens.** Open
``pipeline/audio_critic.py`` and ADD a new lens to the
``EVALUATION LENSES`` block in ``_CRITIC_PROMPT`` — phrased so the
critic on the next render automatically re-checks for regression.
The lens text should include:

- A short name (e.g. "L11 BACKGROUND HISS")
- The signal to check (e.g. "transcript timing inconsistencies")
- The class-of-bug threshold (e.g. "if X happens, declare CLASS-OF-BUG")

The critic is the pipeline's persistent memory. EVERY shipped fix
that originated from a critic finding lives in this lens list. If
you skip 5b, the next render's critic doesn't know the bug exists
and can't catch a regression. This is Principle #37 in DESIGN.md.

### 6. Update DESIGN.md if the fix is principle-worthy

If the class-of-bug fix is a substantive new system rule (not just
a one-line whitelist tweak), add a new row to ``DESIGN.md`` §14
table — same format as the existing rows: principle name (bold),
mechanism (file:function), originating critique reference. Bump
the implementation tier list at the bottom of the section.

### 7. Tell the user what you did

Print three things:
1. The score (1–10) and one-line take
2. The human-readable findings (top 3–5 issues, plain English)
3. The list of code files you patched + the new critic lens you
   added (so they can review your fixes before re-rendering)

Then if appropriate suggest re-running the synth to verify the
fixes ("``python -m pipeline.audio /path/to/script.json
audio_v5.wav --voice af_bella``" or whatever the project uses).

## What this skill is NOT

- **Not a generic audio quality scorer.** This is for narration that's
  intended to sound like a real human storyteller. Music, sound
  design, and ambient audio are out of scope.
- **Not a substitute for the user actually listening.** The skill
  gives you the bugs; the user gives you the taste call. Always
  invite them to listen and tell you whether the fixed version
  actually sounds better.
- **Not a one-shot evaluation.** The point is the iteration loop.
  Run it after every render. Each finding either ships as a fix +
  lens, or gets explicitly downgraded to "tolerable taste call".

## Quick reference

- Module that does the structured analysis: ``pipeline/audio_critic.py``
- Stage docstring: top of that file
- Schema: ``_CRITIC_SCHEMA`` (bounded JSON)
- Prompt: ``_CRITIC_PROMPT`` — this is where you ADD lenses on each shipped fix
- Output JSON: ``data/critiques/<slug>/<slug>.audio.score.json``
- Markdown review: ``data/critiques/<slug>/<slug>.audio.md``
- DESIGN.md principles to know: #33 (TTS number normalisation), #34 (flat-prosody compensation), #35 (acronym expansion), #36 (verbal CTA must be human), #37 (the feedback loop itself)
