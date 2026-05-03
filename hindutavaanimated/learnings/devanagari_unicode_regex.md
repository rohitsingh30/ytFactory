---
name: Every regex touching narration text must be Unicode-aware
description: ASCII-only regex patterns silently strip Devanagari (and any non-Latin script), producing zero-output failures with no error. Forced 3 separate fixes in one session.
type: feedback
originSessionId: 757022d5-5a62-421f-b1a1-80fe5002bcdf
---
When extending the pipeline to support a non-English channel (Hindi
mahabharat_hindi shipped 2026-05-03), three different files contained
ASCII-only regexes that silently dropped Devanagari characters. Each
caused a downstream feature to produce nothing, with no error message:

| File | Regex | What it broke |
|---|---|---|
| `pipeline/audio.py:_SENTENCE_SPLIT_RE` | `(?<=[.!?])\s+` | Hindi sentences end in danda `।` — splitter saw narration as ONE giant sentence, blew past Kokoro's 510-phoneme cap |
| `pipeline/audio_critic.py:_tokenise` | `[a-z0-9']+` | word_count=0 for Hindi → wpm=0 → every WPM-based critic lens bypassed silently |
| `pipeline/beats.py:_norm_token` | `[^a-z0-9']+` | Word-caption alignment matched zero tokens → ALL captions were empty PNGs → Short rendered with no captions even though "captions=word" mode was enabled |

**Why:** Each regex was written for English and assumed the byte
range. Devanagari (U+0900-U+097F) and any other Unicode script silently
fail to match. There's no error, no warning — the calling code just
sees an empty result and continues.

**How to apply:** when adding ANY new feature that operates on
narration text, check the regex. Default to `\w` with `re.UNICODE`
(or just `\w` in Python 3 — UNICODE is the default mode but be
explicit so the intent shows). For sentence terminators include
Devanagari `।॥` alongside `.!?`.

**Detection (lens L21 in audio_critic.py):** if a critic run returns
word_count_source AND word_count_heard both 0, the tokeniser regressed
— flag and skip pacing-based findings. Same probe applies to any other
metric that gates downstream behaviour: if a metric collapses to 0/empty
on Hindi input, suspect the regex first.

**Why this is a class-of-bug, not a one-off:** the same bug existed in
THREE places, written by different people at different times. Every
new file that touches narration text is at risk. Future Hindi/Tamil/
Bengali/Arabic channels will keep finding more.
