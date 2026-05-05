---
name: make-rhyme
description: Author a bilingual Hinglish nursery rhyme script for the Rhyme Time Junction channel — Hindi verse + English bridge, recurring mascots Laddu/Jalebi/Tuk-Tuk/Dadi, and a Suno prompt the user copy-pastes to generate the sung song. Use when the user asks for a kids' rhyme, names a classic ("Hathi Raja", "Twinkle Twinkle", "Machhli Jal Ki Rani"), or says "make me a rhyme".
---

# /make-rhyme — author a Rhyme Time Junction script + Suno prompt

This skill is the rhyme-channel equivalent of `/make-script`: it produces
the `script.json` for one rhyme PLUS a Suno prompt the user uses to
generate the sung song externally. Output is one rhyme per invocation
(unlike `/make-script` which can produce multiple in batch).

The channel is `rhymetimejunction`. See `rhymetimejunction/config.yaml`
for the design contract — bilingual Hinglish, recurring mascots,
external Suno song, continuous animation.

## When to use

- User names a classic rhyme: "Hathi Raja Kahan Chale", "Twinkle Twinkle",
  "Machhli Jal Ki Rani", "Lakdi Ki Kathi", "Wheels on the Bus"
- User says "make me a kids' rhyme" or "make me a rhyme about <topic>"
- User asks to extend an existing compilation with a new rhyme

If the user asks for AITA / sports / war / oddities content, redirect to
`/make-script`. This skill is rhymes only.

## How to run it

### 1. Pick the rhyme + slug

If the user named a specific rhyme, use it. Otherwise ask. For each
rhyme, derive a slug from the title:

| rhyme | slug |
|---|---|
| Hathi Raja Kahan Chale | `hathi-raja-kahan-chale` |
| Twinkle Twinkle Little Star | `twinkle-twinkle` |
| Machhli Jal Ki Rani Hai | `machhli-jal-ki-rani` |
| Lakdi Ki Kathi | `lakdi-ki-kathi` |
| Wheels on the Bus (Hinglish) | `wheels-on-the-bus-hinglish` |

Slug lives in `rhymetimejunction/narrations/<slug>.json`.

### 2. Author the script

Author the rhyme as a 90-120s standalone (the channel's pilot format —
compilations come later). Structure:

| seconds | beat | what |
|---|---|---|
| 0-5s | cold open | Dadi greets at the Junction; mascots wave. ONE bilingual sentence. |
| 5-25s | Hindi verse 1 | Two lines of the classic Hindi verse. |
| 25-45s | Hindi verse 2 | Two more lines. |
| 45-65s | English bridge | New English lyrics, same melody, story progression. |
| 65-85s | story beat | Mascot reaction / comic beat with Tuk-Tuk. |
| 85-105s | chorus repeat | Hindi chorus, all mascots together. |
| 105-115s | outro | Dadi waves goodbye, "next stop" tease. |

Word count target: 100–150 words across all lyrics + spoken bridges.
Each numbered beat above is ONE entry in the `lyrics: [...]` block.

#### Lyric authoring rules

- **Bilingual mix:** at least one full Hindi verse + one full English
  verse + one chorus that returns to Hindi. Not all-English (loses
  the Hinglish lane); not all-Hindi (alienates the diaspora kids
  audience the channel is built for).
- **Use the original Hindi verse for classics** — don't paraphrase
  "Hathi Raja kahan chale" into something else. The recognition
  moment is what makes it land for the parents watching.
- **Rhyme + meter:** every Hindi line must rhyme with another within
  the verse (per the original rhyme's structure); English bridge
  must rhyme similarly (AABB or ABAB).
- **Mascot integration:** at least one verse must name a mascot doing
  something specific. "Laddu spots the elephant", "Jalebi feeds it
  bananas", "Tuk-Tuk barks at it from the rooftop".
- **NO scary content.** No predator/prey violence, no death, no
  injury, no shouting. Kids' channel — the YouTube COPPA classifier
  is sensitive on this and the channel YAML has `made_for_kids: true`.
- **Spoken bridges (between sung verses):** Dadi voice (Kokoro
  `hf_alpha` — the only Hindi-capable TTS in the repo). Short — ≤8
  words each. Intro and outro only; verse-to-verse should flow
  musically without spoken interruption.

### 3. Author the Suno prompt

Suno generates the sung song from a lyrics block + a style cue. The
prompt has two parts:

**Style cue** (one short sentence, goes in Suno's "Style" field):
```
Cheerful upbeat children's nursery rhyme. WARM INDIAN FEMALE LEAD VOCALIST
FLUENT IN HINDI, authentic desi accent on Hindi words, no English accent
on Devanagari. Kids choir on chorus, gentle acoustic guitar + tabla, 120 BPM,
warm Hinglish bilingual, picture-book joyful tone.
```

**Why the explicit "Indian female vocalist fluent in Hindi" clause is
NON-NEGOTIABLE:** Suno V4_5 defaults to an English-accented vocalist when
given any English in the style prompt. The vocalist then pronounces
Devanagari lyrics with an English accent — "हाथी" comes out as "haa-thee"
mispronounced, jarring for Indian parents. The clause forces Suno to
pick a Hindi-native singer profile from its training distribution.

Tweak per rhyme:
- Mahabharat-adjacent (none currently): swap "warm Hinglish" → "epic kathaa style", keep the Indian-vocalist clause
- Faster rhymes (Wheels on the Bus): bump BPM to 140
- Quieter rhymes (Twinkle): "lullaby tempo, 80 BPM, no tabla"
- Pure-Hindi-only rhymes: drop "Hinglish" and tighten to "warm Hindi children's rhyme, female lead vocalist native Hindi speaker"

**Lyrics block** (goes in Suno's "Lyrics" field): paste ALL the sung
verses (Hindi + English bridge + Hindi chorus). Skip Dadi's spoken
bridges — those are TTS'd separately by the pipeline. Suno tags help
structure the song:

```
[Verse 1]
हाथी राजा कहाँ चले
सुंदर सुंदर बेले लाले

[Verse 2]
मेरे घर भी आते जाना
केले खाना दूध पीना

[Chorus]
हाथी राजा hathi raja
Laddu calls and Jalebi waves

[Bridge — English]
Walking down the marigold lane
Trunk swinging in the warm sunshine
Coming to my house for tea
Bananas and milk for him and me

[Outro]
हाथी राजा कहाँ चले
सुंदर सुंदर बेले लाले
```

### 4. Output the script.json

Schema (extends the standard ytFactory script schema with rhyme-only
fields):

```json
{
  "slug": "hathi-raja-kahan-chale",
  "hook": "Dadi waves at the Junction — \"Suno bachcho, hathi raja aaye!\"",
  "narration": "<full lyrics + spoken bridges joined as plain text — used by the existing beats / captions / alignment path>",
  "lyrics": [
    {
      "beat": 0,
      "kind": "spoken_bridge",
      "lang": "hi",
      "voice": "dadi",
      "text": "Suno bachcho, hathi raja aaye!"
    },
    {
      "beat": 1,
      "kind": "sung_verse",
      "lang": "hi",
      "verse_label": "verse_1",
      "text": "हाथी राजा कहाँ चले\nसुंदर सुंदर बेले लाले"
    },
    {
      "beat": 2,
      "kind": "sung_verse",
      "lang": "hi",
      "verse_label": "verse_2",
      "text": "मेरे घर भी आते जाना\nकेले खाना दूध पीना"
    },
    { "...": "..." }
  ],
  "suno_prompt": {
    "style": "Cheerful upbeat children's nursery rhyme, female lead vocal with kids choir, gentle acoustic guitar + tabla, 120 BPM, warm Hinglish bilingual, picture-book joyful tone.",
    "lyrics": "[Verse 1]\nहाथी राजा कहाँ चले\n...\n[Outro]\nहाथी राजा कहाँ चले\nसुंदर सुंदर बेले लाले"
  },
  "title_options": [
    "Hathi Raja Kahan Chale | हाथी राजा कहाँ चले — Bilingual Kids Rhyme",
    "Hathi Raja Visits the Junction | Hinglish Nursery Rhyme",
    "हाथी राजा - The Elephant King | Hindi + English Kids Song"
  ],
  "thumbnail_hint": "Laddu and Jalebi waving at a cartoon elephant covered in marigolds, Tuk-Tuk pug puppy at their feet, bright sunshine.",
  "source": "manual:nursery-rhyme-classic"
}
```

The `lyrics` and `suno_prompt` fields are rhyme-channel-only; the rest
of the pipeline (beats, captions, compose) reads `narration` like every
other channel, so the existing wiring works on a rhyme script as long
as `narration` contains the lyrics joined as plain text.

### 5. Tell the user what to do next

Print a clear hand-off block:

```
✓ wrote rhyme script: rhymetimejunction/narrations/hathi-raja-kahan-chale.json

Next steps:
  1. Generate the song on Suno:
     - Open https://suno.com/create
     - Paste the "style" field into the Style box
     - Paste the "lyrics" field into the Lyrics box
     - Generate (free tier gives 2 takes — pick the better one)
     - Download the WAV
  2. Drop the WAV at:
     rhymetimejunction/songs/hathi-raja-kahan-chale.wav
     (NOT data/intermediate/<channel>/songs/ — pipeline reads from
     <out_dir>/songs/<slug>.wav and out_dir defaults to "data".)
  3. Render via the website at http://127.0.0.1:8765 — pick
     "Rhyme Time Junction", select the slug, hit Generate.
     The pipeline will detect the song WAV and skip TTS automatically
     (audio_provider: external_song in the channel YAML).
```

## Important rules

- Always use `.venv/bin/python` for any helper scripts.
- Don't touch files in `pipeline/` or `make_shorts.py` — that's
  pipeline territory, not script-authoring territory.
- Don't run any rendering stages yourself (no TTS, no image gen, no
  ffmpeg). The skill stops at producing the script.json + the Suno
  hand-off.
- Don't generate the Suno song programmatically. There's no official
  Suno API; unofficial wrappers get DMCA'd. Manual is the supported
  path.
- Don't author content that violates COPPA (see "Lyric authoring
  rules" above — no scary content, no violence, no shouting).
- If the user asks for an English-only rhyme, **still produce a
  Hinglish version** — that's the channel's differentiator. Suggest
  flipping which language carries the chorus (English chorus + Hindi
  bridge) for variety, but don't drop the bilingual angle entirely.
- One rhyme per invocation. If the user asks for a compilation
  ("make a 5-rhyme compilation"), produce 5 separate script.json
  files — one Suno song per rhyme, then a compose step (TBD pipeline
  work) stitches them together.
