---
name: make-script
description: Pull stories from a chosen source (Reddit / Wikipedia oddities / today-in-history / YouTube long-form video) and produce hook-first script JSON files ready for stages 4-7 of the ytFactory pipeline. Use when the user wants to create new Shorts and asks for a niche by name (e.g. "make me an AITA short", "wiki oddities", "today in history", "use this YouTube video").
---

# /make-script — pull a niche, produce ready-to-render scripts

This skill is the v0 implementation of stages 1-3 of the ytFactory
pipeline (DESIGN.md §3): mine raw text from a chosen source, then
rewrite each item into a 10-20s hook-first narration. Output is one
``script.json`` per Short, ready to be picked up by ``make_shorts.py``
(stages 4-7, the creation half) once the audio/image/compose models
are on disk.

## How to run it

### 1. Pick the niche

If the user didn't say which source they want, ask. The choices are:

| niche key | source | example use |
|---|---|---|
| ``aita`` | r/AmItheAsshole top stories | "make me 5 AITA shorts" |
| ``tifu`` | r/tifu top stories | |
| ``reddit:<sub>`` | any subreddit | "use r/MaliciousCompliance" |
| ``oddities`` | Wikipedia "List of unusual deaths (21st c.)" | "make wiki oddities" |
| ``misconceptions`` | Wikipedia "List of common misconceptions about history" | |
| ``tih`` | Wikipedia "On this day" (today's date by default) | "make me today-in-history shorts" |
| ``youtube:<url>`` | one long-form YouTube video (transcript ➝ many shorts) | "use https://youtu.be/..." |

If the user names a different niche from DESIGN.md §4 that doesn't have
an adapter yet (e.g. "4chan greentext", "TIFU stand-up"), tell them
that adapter doesn't exist and offer to add it.

### 2. Pull stories AND rewrite them — autonomous

`pull_stories.py` now runs the full mining flow in one command:
visualizability filter → claude-CLI rewrite → claude-CLI cast author.
Always use ``.venv/bin/python``.

```bash
# Reddit
.venv/bin/python scripts/pull_stories.py reddit --subreddit AmItheAsshole --limit 5

# Wikipedia oddities
.venv/bin/python scripts/pull_stories.py wiki --page unusual_deaths_21c --limit 5
.venv/bin/python scripts/pull_stories.py wiki --page misconceptions_history --limit 5

# Today in history
.venv/bin/python scripts/pull_stories.py tih --limit 5

# YouTube long-form video (returns ONE raw story = full transcript;
# you're responsible for finding ~5-15 story boundaries inside it)
.venv/bin/python scripts/pull_stories.py youtube "<url>" --channel reddit_video
```

Default ``--limit`` is 10. Cut it down if the user only asked for a few.

Output per story (channel auto-derived from source):

- ``data/intermediate/<channel>/raw/<slug>.json`` — the source story
- ``data/intermediate/<channel>/scripts/<slug>.json`` — hook + narration
- ``data/intermediate/<channel>/cast/<slug>.json`` — narrator description

Pass ``--no-llm`` to skip rewrite + cast (raw-only debug mode).

### 3. (Optional) Override / re-author a script

The autonomous flow handles 99% of cases. You only need to hand-author
a script if:

- The user asked for a specific angle the LLM rewrite missed, OR
- A story dropped out at visualizability filter but the user wants to
  force-include it.

In those cases, write directly to
``data/intermediate/<channel>/scripts/<slug>.json`` matching the schema
below. The pipeline will pick up your hand-authored script over the
auto one (you're overwriting the same path).

#### Rewrite rubric (matches ``pipeline/rewrite.py:_BASE_PROMPT``)

- **Hook** in the first 1.5 seconds — ~5-10 words. A curiosity-gap
  question or a surprising claim. **Not** "Hi guys", "today's story
  is", "in this video".
- Total 50-80 words (~10-20s spoken).
- Conversational, present tense.
- End with a question or twist that drives comments.
- Don't editorialize ("crazy story!"). Let the facts hit.
- For YouTube long-form transcripts: identify each distinct story
  inside the transcript, then write one script per story.

#### Output schema

```json
{
  "slug": "<same slug as the raw story>",
  "hook": "<first ~5-10 words>",
  "narration": "<full 50-80 word narration including the hook>",
  "title_options": ["<title A>", "<title B>", "<title C>"],
  "source_url": "<from the raw story's url field>",
  "source": "<from the raw story's source field>"
}
```

(``source_url`` and ``source`` are extras the rewriter doesn't need
but the uploader stage 8 will, so we carry them through.)

### 4. Report back

When done, print a one-line-per-script summary like:

```
✓ wrote 5 scripts to data/intermediate/aita_text/scripts/
  - aita-cake.json — "AITA for refusing to bake my sister's cake?"
  - aita-toilet-seat.json — "I left dad's underwear on the toilet."
  ...
next: render via the local website at http://127.0.0.1:8765 — pick the
matching niche, hit Generate, and it will run make_shorts.py for you
and stream stage progress back. (See web/README.md for how to start
uvicorn if it isn't already up.)
```

**The user is on the website now, not the CLI.** Don't suggest
``.venv/bin/python scripts/make_shorts.py …`` as the follow-up — the website
spawns it as a subprocess and adds live SSE progress, audio preview,
and per-beat thumbnails. Pipeline edits in ``pipeline/*`` flow through
the website automatically (no web restart needed; the subprocess
re-imports on each job).

## Important rules

- Always use ``.venv/bin/python`` — the project's venv has the
  dependencies (``requests``, ``youtube-transcript-api``).
- Don't touch files in ``pipeline/`` (audio.py, beats.py, images.py,
  compose.py) or ``make_shorts.py`` — those are the creation half and
  the user is iterating on them.
- Never run stages 4-7 yourself in this skill (no TTS, no image
  generation, no ffmpeg). The skill stops at producing
  ``scripts/*.json``.
- For YouTube videos with no captions, recommend the user pass
  ``--whisper-fallback`` (it needs ``yt-dlp`` + the mlx-whisper model
  on disk — flag the disk requirement).
- Save scripts even if some don't pass the rubric — note in the
  summary which ones you skipped and why.

---

## Cloud pre-render hook (mandatory)

Before handing off to `pipeline/render/<entrypoint>.py`, do the
**routing assertion** documented in
[`docs/cloud_prerender_hook.md`](/Users/rohit/ytFactory/docs/cloud_prerender_hook.md):
read the channel `config.yaml` (and variant YAML if applicable) and
assert `tts_provider` + `image_provider` start with `cloudrun_`
(except for documented local-only paths like
`mystoriesanimated/variants/tifu.yaml` and the Hindi `kokoro hf_alpha`
fallback).

**Pre-warm is now automatic** — the renderer entrypoints call
`pipeline.cloud.warm.warm_async(channel)` immediately after argparse,
so the 5-7 min cold-load happens in parallel with the renderer boot.
**Health is now in the admin tab** — `/app/cloud` (sidebar → Cloud)
shows green/yellow/red live; for CI use `/api/cloud/health`. The
`warm-cloud`, `cloud-health`, `cloud-cost`, and
`deploy-cloud-service` skills were retired on 2026-05-10; same code
lives in `pipeline/cloud/` + the admin tab.
