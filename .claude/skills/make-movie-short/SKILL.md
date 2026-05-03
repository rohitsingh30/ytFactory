---
name: make-movie-short
description: Turn a Reddit story into a fully-directed animated/sketch movie Short. Acts as an experienced screenwriter + director + DP + expert AI-prompter — writes the narration, breaks it into a numbered shot list with camera angle, framing, lighting, blocking, character action and palette per shot, then assembles a detailed image-gen prompt for every shot so the renderer can produce a movie, not a slideshow. Use when the user asks for a "cinematic", "movie", "directed", "shot-by-shot", or "full screenplay" Short — e.g. "make me an AITA movie short", "do the full directed version of this story", "make a sketch-animated short with proper camera work". For the simple flat narration version, use /make-script instead.
---

# /make-movie-short — Reddit story → fully-directed animated Short

This skill is the cinematic sibling of ``/make-script``. ``/make-script``
gets you a 50-80 word narration; this skill goes further — it acts as
an **experienced screenwriter + director + DP + expert AI prompter**
and produces a complete **shot-by-shot movie spec** with camera angles,
framing, lighting, character blocking, palette and per-shot AI prompts.
The output drops into ``data/intermediate/<channel>/`` so
``make_shorts.py`` can render it like any other Short.

You wear four hats in sequence: **Writer → Director → DP → Prompter**.
Don't skip a hat. Each one feeds the next.

## How to run it

### 1. Get a story

If the user gave you a Reddit URL or an existing slug, use it. Otherwise:

```bash
.venv/bin/python pull_stories.py reddit --subreddit AmItheAsshole --limit 1 --channel aita_animated
```

(Or any subreddit they named.) Then ``Read``
``data/intermediate/<channel>/raw/<slug>.json`` — that's your source
material.

If the user pointed at a slug that already has a script
(``data/intermediate/<channel>/scripts/<slug>.json``), skip to step 3
but still re-read the raw story; you need its visual richness for the
shot list, not just the trimmed narration.

### 2. Hat 1 — Writer: hook-first narration (50-80 words, ~10-20s)

Same rubric as ``/make-script``:

- Hook in the first ~1.5s — 5-10 words, curiosity gap or surprising
  claim. **Not** "Hi guys" / "today's story is".
- 50-80 words total. Conversational, present tense.
- End with a question or twist that drives comments.
- Don't editorialize. Let the facts hit.

**Hard gates from ``pipeline/script_check.py`` (it runs on render and
will fail the build, not warn — bake these in up front):**

- **First 8 words** must contain at least one of: a ``?``, the literal
  word ``AITA``/``WIBTA``, or a claim verb from the script_check
  whitelist (``refused``, ``told``, ``caught``, ``threw``, ``broke``,
  ``banned``, ``divorced``, ``adopted``, ``moved``, …). Easiest path
  for AITA channels: open with ``"AITA for <verb>ing …?"``.
- **First 30 words** must contain a number/quantity (e.g. ``2-bedroom``,
  ``three bottles``, ``$400``, ``two weeks``). This is the wedge.
- **AITA channels — last 2 sentences combined** must speak the
  LIKE-if-YTA / COMMENT-if-NTA split. The visual closer panel from
  ``pipeline/captions.render_closer_panel`` does NOT satisfy this gate —
  the spoken narration must contain it too. Per stored memory feedback.
- **Last sentence** must contain a vote-prompt CTA (``AITA?``,
  ``WIBTA?``, ``Was I wrong?``, ``What would you do?``). The
  LIKE/COMMENT line alone doesn't count — append a separate ``AITA?``
  sentence after it.

Canonical AITA closer that passes all three: ``"… LIKE if I'm YTA,
COMMENT if NTA. AITA?"``

Save to ``data/intermediate/<channel>/scripts/<slug>.json`` with the
same schema ``/make-script`` uses (``slug``, ``hook``, ``narration``,
``title_options``, ``source_url``, ``source``).

### 3. Hat 2 — Director: cast + tone + look

Decide:

- **Logline** — one sentence answering "what is this Short about,
  emotionally?". Not the plot — the feeling.
- **Tone** — pick one: comedic / tense / cringe / wholesome / dark /
  bittersweet. The whole shot list serves this.
- **Cast** — the narrator is **NOT** the channel default. Per
  principle #24, the channel ``character_description`` is age/gender-
  ambiguous and was silently producing boy-cartoons voicing
  grandmothers. **Never hand-write the narrator description by
  copying the channel YAML.** Instead, invoke
  ``pipeline/cast.py:author_cast`` so the LLM infers age, gender,
  wardrobe from the script's relationship markers (DIL → grandmother,
  toddler → young-adult parent, etc.):

  ```bash
  .venv/bin/python -c "
  from pathlib import Path
  import json, yaml
  from pipeline import cast
  raw = json.loads(Path('data/intermediate/<channel>/raw/<slug>.json').read_text())
  cfg = yaml.safe_load(Path('channels/<channel>.yaml').read_text())
  cast.author_cast(
      raw_story=raw,
      channel_cfg=cfg,
      out_path=Path('data/intermediate/<channel>/cast/<slug>.json'),
  )
  "
  ```

  ``author_cast`` writes the per-story cast.json with
  ``narrator.description`` (full appearance line), ``default_emotion``,
  ``age_band``, ``gender``. Read it back and use ``narrator.description``
  in the shotlist's ``cast[]`` entry verbatim.

  If you genuinely can't run the script (sandbox, missing venv), as
  a fallback you may hand-write the cast file — but you MUST infer
  the demographic yourself from the script using the same
  relationship-marker rules (see ``pipeline/cast.py``'s ``_PROMPT``
  for the full list). Do NOT copy the channel default.

  Per-story supporting characters (boyfriend, boss, sister, midwife)
  also go into the same cast file under ``supporting`` — append them
  manually after ``author_cast`` runs, since the schema reserves
  the field but ``author_cast`` doesn't populate it in v1.
- **Look** — copy the channel's ``image_style_prefix`` from YAML;
  that's the locked aesthetic. Don't reinvent it. You're directing
  *within* the channel's house style.

### 4. Hat 3 — DP: shot-by-shot decomposition

Break the narration into **one shot per ~2 seconds** of screen time
(target the channel's ``beat_target_s``, usually 2.0s; never longer
than ``beat_max_s``, usually 3.2s). A 16-second short is ~8 shots
plus a closer. Match the shot count to the natural beats of the
narration — every clause that lands a fact gets its own shot.

For **every** shot, write all of these fields. No "TBD"s. If you
don't have an opinion, you haven't directed yet — go back and pick
one.

| field | what to write | example |
|---|---|---|
| ``shot`` | 1-indexed integer | ``3`` |
| ``narration_line`` | exact words spoken in this shot (verbatim slice of the narration) | ``"and three bottles of wine"`` |
| ``duration_s`` | seconds, decimal | ``1.9`` |
| ``shot_size`` | ECU / CU / MCU / MS / MLS / WS / EWS / insert / OTS / POV | ``MCU`` |
| ``camera_angle`` | eye-level / high / low / overhead / dutch / worm's-eye | ``low`` |
| ``camera_movement`` | static / push-in / pull-out / pan-L / pan-R / tilt-up / tilt-down / dolly / handheld-shake. (For 2D sketch animation: pretend it's a still — most shots will be ``static`` with the occasional ``slow push-in``.) | ``slow push-in`` |
| ``lens_feel`` | wide (24mm) / normal (50mm) / portrait (85mm) / macro. Sets distortion + intimacy, not a literal lens. | ``portrait — flattering, intimate`` |
| ``framing`` | rule-of-thirds placement, headroom, leadroom, what's foreground/midground/background | ``subject on right third, three wine bottles dominate left two-thirds foreground`` |
| ``lighting`` | key direction, hardness, color temp, mood | ``warm tungsten from camera-left, soft shadow on right cheek, low-key`` |
| ``palette_accent`` | the ONE color or object that should pop in this frame | ``the deep red of the wine`` |
| ``character_in_frame`` | which cast id(s) are visible. ``none`` for inserts. | ``narrator`` |
| ``character_action`` | what they're physically doing — verbs, posture, hands | ``leaning back, arms crossed, lips pressed flat`` |
| ``character_emotion`` | one word, readable in a thumbnail | ``indignant`` |
| ``background`` | what's behind them, blurred or sharp | ``blurred restaurant interior, warm-yellow ambient lights, hint of another table`` |
| ``props_in_focus`` | named objects sharp in frame | ``three tall green wine bottles, white folded paper bill`` |
| ``cut_from_prev`` | hard cut / match cut / smash cut / fade / dissolve. (For shorts: 95% hard cuts.) | ``hard cut`` |
| ``directors_note`` | the ONE thing that makes this shot work. Optional but encouraged. | ``the punchline is the bottles, not her face — give them the screen`` |

Save the shot list to
``data/intermediate/<channel>/shotlist/<slug>.json`` with the schema
in §6.

### 5. Hat 4 — Prompter: assemble the per-shot AI prompt

For every shot, fold the directorial fields into a single rich image
prompt. The renderer (``pipeline/prompts.py`` schema) takes
``{key_visual, scene}``, so:

- **``key_visual``** — the punchline of the shot in 4-10 words. The
  ONE thing that has to be readable in a thumbnail. Pick the
  ``palette_accent`` object or the character's ``character_emotion``
  expression, whichever lands the narration line harder.
- **``scene``** — **25-35 words** (NOT 40 — the renderer's lint
  triggers a warning at >50 tokens, and a 40-word English sentence
  with hyphenations and adjectives lands around 55 tokens). Assemble
  from ``framing`` + ``character_action`` + ``background`` +
  ``props_in_focus`` + ``camera_angle`` + ``lighting``. Concrete
  nouns and verbs. No editorializing ("a beautiful…", "a stunning…").
  Drop adjectives before drop nouns when trimming.

**Hard rules** — these are the same rules baked into
``pipeline/prompts.py`` and breaking them will make the rendered
Short worse, not more cinematic:

1. **NEVER** ask for in-frame text: no signs, labels, logos, menus,
   billboards, screens-with-words, license plates, headlines, name
   tags. Diffusion renders them as gibberish. Show the **object**
   (a phone with a red angry-face emoji), not the **text**
   (a phone showing "I hate you").
2. **One main subject per shot.** Never "three friends", "a group",
   "several people". If the narration mentions a group, focus on
   the narrator's reaction OR an object (the wine bottles, the
   bill, the empty chair). For multi-character shots use OTS
   framing — one character's shoulder foreground, the other in
   focus — that reads as one subject.
3. **Do NOT re-describe the narrator's appearance** in ``scene`` —
   the channel's ``character_description`` is prepended automatically
   by the orchestrator. Just say "the character" / "she" / refer to
   their action.
4. **Beat 0 (the hook)** must contain at least 2 concrete tokens
   from the channel's ``opening_image_directives.example_tokens``
   (specific room / specific prop / specific posture). No generic
   "shocked face on plain background" hook shots.
5. ``scene`` stays under ~40 words. Diffusion attention dilutes
   past that.
6. Camera-language goes in ``scene`` as plain English, not jargon.
   Write ``"shot from below looking up"``, not ``"low-angle 24mm
   wide"``. The diffusion model understands the first, hallucinates
   off the second.

Save the assembled prompts to
``data/cache/<slug>/prompts.json`` as the JSON array
``pipeline/images.load_prompts`` expects. **Every entry MUST include a
``narration_line`` anchor** — it's how the renderer aligns each prompt
to the right beat (principle #26). The anchor is the same string as
the corresponding shot's ``narration_line`` in the shotlist:

```json
[
  {
    "narration_line": "and three bottles of wine",
    "key_visual": "three tall green wine bottles standing in a row",
    "scene": "on a restaurant table in foreground, the character blurred behind them leaning back with arms crossed, warm tungsten light from camera-left, low-key shadows"
  },
  ...
]
```

The orchestrator picks this up automatically and skips the
LLM-authoring step in ``pipeline/prompts.py`` because the file
already exists. The shotlist sidecar
(``data/intermediate/<channel>/shotlist/<slug>.json``) is also read at
beat-split time — its ``narration_line`` strings become forced beat
boundaries, so each shot becomes exactly one beat regardless of
duration heuristics. Sub-sentence cuts (e.g. splitting "water labor
right where I watch TV. I told her no." into two shots) are now
legal.

### 6. Output schema — ``shotlist/<slug>.json``

The shot list is the directorial source of truth. Renderer doesn't
read it directly, but it's what you'd hand to a human animator and
it's what the user reviews / iterates on.

```json
{
  "slug": "aita-cake",
  "logline": "A baker says no to a free wedding cake — and the family loses it.",
  "tone": "tense, cringe-comedy",
  "channel": "aita_animated",
  "look": "<copied verbatim from channels/<channel>.yaml image_style_prefix>",
  "cast": [
    {
      "id": "narrator",
      "role": "POV",
      "appearance": "<copied verbatim from channel YAML character_description>"
    },
    {
      "id": "sister",
      "role": "antagonist",
      "appearance": "tall, long blonde hair pulled back, white blouse, anxious eyes"
    }
  ],
  "shots": [
    {
      "shot": 1,
      "narration_line": "I refused to bake my sister's wedding cake",
      "duration_s": 1.9,
      "shot_size": "MCU",
      "camera_angle": "eye-level",
      "camera_movement": "static",
      "lens_feel": "portrait — intimate",
      "framing": "subject right-third, kitchen counter runs across foreground",
      "lighting": "warm morning light from window camera-left, soft shadows",
      "palette_accent": "yellow t-shirt against pale beige background",
      "character_in_frame": "narrator",
      "character_action": "hands flat on the kitchen counter, leaning slightly forward, mouth set",
      "character_emotion": "resolute",
      "background": "blurred kitchen — wooden cabinets, hint of a window",
      "props_in_focus": "rolling pin and a closed recipe book on the counter",
      "cut_from_prev": "hard cut",
      "directors_note": "her POSTURE is the hook — not her face. She's standing her ground.",
      "key_visual": "a small character standing firm at a kitchen counter",
      "scene": "the character with both hands flat on a wooden kitchen counter, leaning forward, a rolling pin and closed recipe book in foreground, blurred kitchen cabinets behind, warm morning light from the left"
    }
  ],
  "closer": {
    "shot": "closer",
    "duration_s": 1.8,
    "narration_line": "AITA?",
    "shot_size": "MCU",
    "camera_angle": "eye-level",
    "framing": "subject centered, plain background to leave room for caption overlay",
    "character_action": "looking directly at camera, palms slightly open in question",
    "character_emotion": "asking",
    "text_overlay": "LIKE if YTA, COMMENT if NTA. AITA?",
    "key_visual": "the character looking straight at camera with open palms",
    "scene": "the character centered, palms turned slightly upward in a questioning gesture, plain pale beige background, soft even light"
  }
}
```

### 7. Render — and let the in-pipeline critic run

Don't render in this skill — hand off. Print:

```
✓ wrote script:   data/intermediate/<channel>/scripts/<slug>.json
✓ wrote shotlist: data/intermediate/<channel>/shotlist/<slug>.json (N shots + closer)
✓ wrote prompts:  data/cache/<slug>/prompts.json (N entries)

next: render via the local website at http://127.0.0.1:8765 — pick the
matching niche (the channel YAML this script targets), pick a voice,
hit Generate. The website spawns make_shorts.py and streams stage
progress, per-beat thumbnails, and a final mp4 preview.

(CLI fallback if the website is down: see web/README.md to start
uvicorn, or run
  .venv/bin/python make_shorts.py --script data/intermediate/<channel>/scripts/<slug>.json --channel channels/<channel>.yaml --slug <slug>
directly. Both paths use the same pipeline, so any pipeline edit
propagates to both — but the website is the user's primary path.)
```

**The website passes ``--no-critic`` by default** because the in-pipeline
critic is heavy and the UI offers it as a separate "Run auto-critic"
step. If you specifically need the auto-critique-and-regenerate loop
(class-of-bug surfacing + auto-rerendered weak beats), run the CLI
form above without ``--no-critic``. Otherwise the website's critic
button (or ``/critique-video``) covers the human-readable pass.

The in-pipeline critic
(``pipeline/critic.py``, Stage 7.5) runs by default and is the whole
point of the auto-critique loop. It samples the rendered mp4, walks
every frame through the L1–L15 lenses, classifies each issue ONE-OFF
vs CLASS-OF-BUG, and either:

- regenerates weak beats automatically when ``score >= min_critic_score``
  but ``beat_corrections`` is non-empty, or
- prints class-of-bug ``system_corrections`` to stdout so the user can
  apply them in code/schema before the next render.

The score lands at ``data/critiques/<slug>/<slug>.score.json``. Read
it after the render finishes — top_issues + per_frame_findings +
system_corrections is the full report.

If the user wants to see the shot list before rendering, walk them
through it shot-by-shot (use the ``directors_note`` field to explain
your intent) and let them push back. **Re-author the shot list** if
they want a different camera/framing/tone — don't just edit the
prompts in place; the shot list is the source of truth and the
prompts get re-derived from it.

### 8. After render — apply the critique loop

When the render completes:

1. **Read** ``data/critiques/<slug>/<slug>.score.json``. Surface the
   ``score``, ``one_line_take``, and ``highest_leverage_change`` to
   the user in 3 lines.
2. **System corrections first.** For every entry in
   ``system_corrections``, those are CLASS-OF-BUG fixes against the
   pipeline (``pipeline/*.py``, ``channels/<channel>.yaml``,
   ``script_check.py``, etc.). Either fix them yourself (if they're
   small + the user agrees) or write a TODO list the user can act on.
   Class fixes lift the next 100 Shorts; one-off patches only lift
   this one.
3. **Per-frame findings second.** For every ``per_frame_findings``
   entry not already covered by a class fix, the orchestrator either
   already auto-applied the ``beat_corrections`` patch (re-rendered
   weak beats) or surfaced them. Verify the regen happened by
   re-reading the score file.
4. **Run /critique-video** if the user wants the human-readable
   viewer-perspective walkthrough on top of the structured JSON
   from the in-pipeline critic. They are mirrors of each other —
   same lens framework, different output format.

Repeat from step 4 (DP) of THIS skill if the critic finds shot-list
level problems (camera, blocking, palette). Don't whack-a-mole the
prompts.json — re-author the shot list and let prompts get re-derived.

## Important rules

- **Channel aesthetic is locked.** Re-read
  ``channels/<channel>.yaml`` and copy ``image_style_prefix`` and
  ``character_description`` verbatim. You're directing *within* the
  house style — not redesigning it. (See memory: "Lock aesthetic at
  channel, vary character per story".)
- **One image per shot, one shot per ~2 seconds.** Don't try to
  pack two camera angles into one beat — the renderer outputs one
  image per beat. If you want a cut, that's a new shot.
- **No in-frame text in any prompt** — including the closer. Closer
  text is overlaid by ``pipeline/captions.render_closer_panel``, not
  drawn by diffusion. (See memory: "AITA closer panel —
  like/comment split". The closer text is the channel YAML's
  ``closer_format``, not something you write.)
- **The narrator's description is locked at the channel level.**
  Per-story characters (the sister, the boss) go into
  ``data/intermediate/<channel>/cast/<slug>.json`` — write them
  there too if you invent any.
- **Don't run the render pipeline.** No TTS, no image gen, no
  ffmpeg. This skill stops at producing the three files in §7. The
  user kicks off the render themselves from the website (or CLI as a
  fallback) once they've reviewed the shot list.
- **Don't touch ``pipeline/`` or ``make_shorts.py``** — the user is
  iterating on those. You only write into
  ``data/intermediate/<channel>/`` and ``data/cache/<slug>/``.
- **Always use ``.venv/bin/python``** for ``pull_stories.py``.
- **One Short per run.** This skill is detail-heavy. If the user
  asks for 5 movie shorts, do them one at a time and confirm the
  first lands before continuing — a bad shot-list template
  multiplied by 5 is wasted work.
- **Auto-critique is the default, not optional.** Never pass
  ``--no-critic``. The in-pipeline critic runs the full L1–L15 lens
  pass and emits both per-frame fixes and class-of-bug system
  corrections. Skipping it defeats the entire feedback loop that
  makes the pipeline get smarter over time.
- After rendering, the natural human-readable follow-up is
  ``/critique-video`` — mention it in the hand-off line for cases
  where the user wants the markdown reaction in addition to the
  structured score JSON the in-pipeline critic already wrote.
