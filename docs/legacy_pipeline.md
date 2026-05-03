# Pipeline — how a Reddit post becomes a YouTube Short (legacy)

> **Status: legacy reference.** This document describes the monolithic
> pipeline as it stood pre-migration. Sections about `make_spec.py`,
> `render_from_spec.py`, and the spec-driven path describe code that
> has been **removed** — the only active rendering path is the
> slideshow path via `scripts/make_shorts.py`. The full system is being split
> into a hybrid cloud + laptop architecture; see
> [architecture.md](./architecture.md) for the target design.

---

## At a glance

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  pull        │→ │  /make-script│→ │  make-spec   │→ │  render      │
│  stories     │  │  (rewrite)   │  │  (fill in)   │  │  from spec   │
└──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘
   raw/<slug>.json   scripts/<slug>.json  specs/<slug>.yaml   shorts/<slug>.mp4
```

Each stage is a separate command, each stage's output is an inspectable
file on disk, each stage is cache-and-resume safe — re-running on the
same slug skips work that's already done.

---

## File layout

```
ytFactory/
├── channels/
│   └── <name>.yaml                    ← channel manifest + spec template
├── sources/                            ← per-niche source adapters (pull stage)
├── pipeline/                           ← internal helpers (TTS, beats, captions, …)
├── pull_stories.py                     ← stage 1: scrape raw text
├── make_spec.py                        ← stage 3: fill spec template
├── render_from_spec.py                 ← stage 4: render mp4 from spec
├── pull_backgrounds.py                 ← bg-loop puller (channel-specific helper)
├── data/
│   ├── intermediate/<channel>/
│   │   ├── raw/<slug>.json             ← stage 1 output
│   │   ├── scripts/<slug>.json         ← stage 2 output (from /make-script)
│   │   └── specs/<slug>.yaml           ← stage 3 output
│   ├── cache/<slug>/                   ← per-Short cache (TTS, beats, PNGs, segments)
│   ├── shorts/<slug>.mp4               ← stage 4 output (the final video)
│   └── critiques/<slug>.md             ← /critique-video output
├── assets/
│   └── cooking_loops/                  ← cached bg loops for the aita_cooking channel
├── mystoriesanimated/cooking_bg_queue.yaml          ← curated queue of YouTube bg sources w/ chop ranges (no downloads)
├── scripts/_cooking_bg_research.py        ← yt-dlp metadata-only helper that populates the queue
└── tests/                              ← unit tests for the spec interpreter
```

---

## Channel = manifest + spec template

A channel YAML in `channels/` is two things stapled together:

1. **Manifest fields** at the top level — `name`, `source_adapter`,
   `source` (subreddit, listing, etc). Used by `scripts/pull_stories.py` to
   know what to scrape.
2. **`spec_template:` block** — a *partial* video spec with
   placeholders like `${script.narration}` that `make_spec.py` fills
   in per-Short.

This is what makes the system extensible. To add a new channel
(different niche, different look) you write one YAML file. No Python.

Example: `mystoriesanimated/variants/aita_cooking.yaml` — pulls from r/AmItheAsshole and
renders the AITA-text-on-cooking-bg format.

---

## Stage 1 — pull stories

Scrape raw text from a source.

```bash
.venv/bin/python scripts/pull_stories.py reddit \
    --subreddit AmItheAsshole \
    --limit 5 \
    --channel aita_cooking
```

Subcommands: `reddit`, `wiki`, `tih`, `youtube`. Each calls into a
source adapter under `sources/` and emits one JSON per story to
`<channel>/raw/<slug>.json`:

```json
{
  "slug": "amitheasshole-aita-...",
  "title": "AITA for ruining...",
  "body": "I need some opinions on this situation...",
  "source": "reddit:AmItheAsshole",
  "url": "https://reddit.com/r/AmItheAsshole/comments/.../",
  "metadata": {
    "score": 9269, "num_comments": 1992,
    "author": "dil-issue-1046",
    ...
  }
}
```

These raw files are the source of truth; later stages reference them
via `${raw.metadata.author}` etc.

---

## Stage 2 — write the script

Either by hand or via the `/make-script` skill. The skill reads each
raw story and writes a 50-80 word hook-first narration to
`<channel>/narrations/<slug>.json`:

```json
{
  "slug": "amitheasshole-aita-...",
  "hook": "My DIL wants a water birth in my living room.",
  "narration": "My DIL wants a water birth... AITA for refusing to host this in my house?",
  "title_options": [
    "AITA for refusing a water birth in my living room?",
    "...",
    "..."
  ],
  "source_url": "https://reddit.com/r/AmItheAsshole/...",
  "source": "reddit:AmItheAsshole"
}
```

The rewrite rubric (hook in first 1.5s, 50-80 words, end on a
question) is enforced by `/make-script` and `pipeline/script_check.py`.

### SportsStoriesAnimated — full pipeline notes

This channel diverges from the AITA-class flow in five places. Read
this whole subsection before authoring a sports short.

**1. Pipeline order — `wiki_research` runs BEFORE `cast`.** See Stage
2.5 below. The dossier is the source of truth for who appears, their
era-specific kit, jersey numbers, body type, and pronunciations.

**2. Cast comes from the dossier (no LLM call).**
`pipeline.cast.cast_from_dossier(dossier, channel_cfg, out_path)`
transforms `dossier.people[]` into `supporting[]` with each entry
carrying a deterministic per-character seed (sha256-hash of the
canonical name). The cast file is rewritten on every dossier edit.

**3. Narrator is voice-over only — never on screen.** The channel
YAML sets `narrator_visual_mode: voice_only`. The pipeline interprets
this in three places:
- `scripts/make_shorts.py` clears the channel-wide `character_description`,
  so no "narrator persona" is prepended to per-beat image prompts.
- `pipeline/prompts.py` switches the prompt-author to a voice-only
  preamble that lists the dossier's real people and FORBIDS the
  phrases "the character", "the narrator", "a man watching" etc.
  Every beat must depict either (a) a specific named real person
  from the dossier, or (b) a pure scene/object/match-context shot
  (empty stadium, scoreboard, ball on the spot, trophy, fans).
- The image-gen loop locks the seed per-character via
  `_seed_for_beat` (matches a beat's scene/key_visual against the
  cast `supporting[]` aliases; uses that character's seed if matched,
  channel seed otherwise).

**4. Players are recognisable by KIT + JERSEY NUMBER, not face
likeness.** No IP-Adapter, no LoRA training. Cartoon Aguero is
"compact Argentine forward, MCFC sky-blue 11/12 home, white shorts,
shirt #16, short black hair, clean-shaven" — viewers fill in the
identity. Avoids publicity-rights gray zone and a 15-25 GB model
download. Cross-beat consistency comes from the per-character seed
lock, not from facial conditioning.

**5. Pre-TTS pronunciation respelling.** The dossier's
`pronunciation_dict` flows into `audio.synthesize` so Kokoro reads
"ah-GWAIR-oh" instead of mangling "Aguero". Captions and
ASR-source-text alignment continue to see the original spelling
(applied only on the TTS-input string, not the global narration).

**6. Footage cut-in is the channel USP.** A sports short without at
least one `kind: footage` beat is INCOMPLETE. The script JSON
declares per-beat footage cuts; `compose.compose_hybrid` stitches the
real broadcast clip in place of the static cartoon image at the
named beat. yt-dlp downloads the source video once (cached by
11-char video id), ffmpeg frame-accurately trims to `[in_s, out_s]`,
scale-crops to 1080×1920. Footage clip's source audio is amix'd under
the narration at the volume specified by `footage[].audio_mix` (default
0.7) — so the iconic call ("AGUEROOOOO") plays under the narrator.

**7. Captions are word-pop centred (TikTok / Submagic style).**
`compose.compose_hybrid` calls `compose_clips` with
`caption_style="per_word"` so each word flips on at its ASR-aligned
timestamp, centred via `_WORD_CAPTION_Y_FRAC` (~0.45 of the height).
This is the same caption logic the slideshow `compose()` path uses;
sports inherits it for higher retention vs bottom-third per-beat
captions.

### Sports-specific class-of-bug rules (from critique 2026-05-02)

These rules govern every sports short to prevent recurring failures
caught on the v5 Aguero render. See `data/critiques/aguero-9320.md`
for the full audit.

- **Cast-locked tokens are server-side enforced, not LLM-suggested.**
  When a beat's scene names a `supporting[]` character, the kit + shirt
  number + era tokens from `cast.json` must be injected programmatically
  into the scene string AFTER the LLM returns — never trusted to be
  copied verbatim by the prompt-author. The Aguero v5 render shipped
  Dzeko in #4 (correct: #10) and Kompany in a teal kit because the LLM
  paraphrased the dossier. Implementation slot:
  `pipeline/prompts.py:author_beat_prompts` post-processing step that
  lints + injects.

- **Aesthetic anchors lead, not trail.** The channel `image_style_prefix`
  (e.g. "cream parchment background, hand-drawn line-art") must appear
  at the FRONT of every per-beat prompt, immediately after the subject
  — not at the end where CLIP attention has dropped. Without this, the
  background/style drifts when the scene string is heavy on action or
  emotion tokens (v5 Dzeko + Kompany beats both lost the parchment bg;
  the fan-celebration beat drifted into a clean anime style). Slot:
  `pipeline/images.py:build_full_prompt`.

- **Sports hook must put a person or score in the first frame.** Beat-0
  cannot be a wide static establishing shot (empty stadium, blank pitch,
  generic logo). The first 1.5 seconds must contain a named player in
  motion, a scoreboard, or the iconic broadcast clip itself. Enforce via
  `sportstoriesanimated/config.yaml:opening_image_directives` +
  `pipeline/script_check.py` reject. Football fans scroll past empty
  pitches in <0.5s.

- **Kit-vocabulary lint (hoops vs stripes).** A "hooped" jersey (QPR
  style) renders as VERTICAL stripes by default in z_image_turbo — same
  as Argentina or Inter. Scene strings must say "horizontal blue and
  white bands" or "vertical stripes" explicitly. New lint in
  `pipeline/prompts.py` flags ambiguous "stripes" / "hoops" without an
  axis qualifier.

- **Sponsor/logo gibberish text lint.** Scene strings containing
  `sponsor`, `Etihad`, `Adidas`, `Umbro`, `crest`, `logo`, `patch` are
  rendered as garbled text by diffusion (the v5 Dzeko render had a
  fake "915" stamped on the chest). Existing
  `pipeline/prompts.py:_lint_text_in_image` extends to flag these and
  rewrite to a SHAPE description ("plain blue chest panel").

- **Sports shorts must establish WHO + WHEN within the first frame.**
  A `match_context_overlay` primitive (NEW) renders a small pill from
  `dossier.match.{result, date, venue}` and overlays it during beat 0.
  Cold viewers landing on the Short shouldn't have to infer which
  match they're watching from kit colors alone.

- **Compose is the contract — applies to artefacts too.** `beats.json`
  must be re-serialised after `make_shorts._attach_footage_to_beats`
  tags beats with `kind: footage`. Currently the on-disk record always
  shows `kind: animated` even for beats that were composed from a
  broadcast clip, which makes critics and re-renders inconsistent.

### Choosing optimal footage cut-in points (SportsStoriesAnimated)

The `script.footage[]` block determines WHEN the cartoon-to-broadcast
cut happens and WHICH window of the source clip plays. Four craft
rules, in order of importance:

**1. Cut TO footage at the narrative pivot — not the punchline.**
The pivot is the beat where the climactic action *begins*, not where
it lands. Common pivots:
- "Last kick of the season" → cut here
- "He doesn't even hesitate" → cut here
- "And here it comes" → cut here
- ❌ "Aguero scores" (this is too late — the punchline has happened
  before the broadcast clip starts)
The pivot beat is the LAST narration the viewer hears before the cut.
After the cut, the broadcast commentary owns the storytelling.

**2. Footage window covers buildup → action → call peak (5-10s).**
- 1-2s of buildup (the pass into space, the shooting motion starting)
- The action itself (the strike, the catch, the dunk)
- 2-3s after the audio peak (the iconic call, the crowd erupting)

Don't trim too tight. Viewers need the breath after the climax for
the emotional release. For Aguero 93:20: source clip's audio peak
("AGUEROOOOO") is at t=7.0-7.5s; window of `in_s=4.0, out_s=12.0`
covers ~3s buildup + strike + 4.5s call/crowd payoff.

**3. Trim narration that overlaps the footage window.** The broadcast
commentary tells the story during the cut. Original narration like
"Balotelli to Aguero. One touch. Aguero scores." becomes redundant
and overlapping if it plays during the footage. Two options:
- ✅ **Remove those narration beats from the script entirely** (lets
  the footage own that moment) — this is the preferred approach.
- ⚠️ Keep them but accept they'll be ducked to silence during the
  cut window via `compose.compose_clips`'s narration-duck logic
  (works, but the narrator-going-silent gap can feel awkward).
After the footage, narration resumes with a payoff line ("Forty-four
years of pain. Ended by one touch.") rather than re-describing what
the broadcast just showed.

**4. Aspect mode — blurred-letterbox, not center-crop.** 16:9 broadcast →
9:16 portrait via `pipeline/footage.py` uses a `filter_complex` that
overlays a width-fit foreground (1080 wide, broadcast aspect preserved)
on a heavily-blurred + dimmed background (cover-scaled + cropped + gblur
sigma=24). Result: ball, passer, and runner all visible; no >1x source
upscale. Don't override this with center-crop — sports actions typically
live at the edges of the broadcast frame and a center-crop hides them.

**5. ASR-aligned cut points — pin `in_s` / `out_s` to the
commentator's actual words.** Audio-RMS peak alone misses the
anchors (the buildup phrase that flows INTO the goal, the post-call
payoff that flows OUT of it). Transcribe the source clip first:

```bash
.venv/bin/python -c "
from pipeline import asr
from pathlib import Path
src = Path('<channel>/footage/sources/<video_id>.mp4')
result = asr.transcribe(src, provider='whisper_mlx',
                        model='mlx-community/whisper-large-v3-mlx-4bit')
for seg in result.get('segments', []):
    for w in seg.get('words', []):
        print(f'  [{w[\"start\"]:5.2f}-{w[\"end\"]:5.2f}]  {w[\"word\"].strip()}')
"
```

Find three anchors in the output:
- **IN word** — first word of the buildup phrase ("headed", "He shoots", "Last second", "Watch this")
- **CALL word** — the goal-call peak (the player's surname shouted)
- **OUT word** — last word of the post-call payoff ("They do it! City!", "It's in the net", "Champions!")

Set `in_s = IN word's start`, `out_s = OUT word's end + 0.4s breath`.
Don't round to seconds — commentators talk fast and 200ms matters.
Combined with rule #4 (don't extend into stylized post-call replay
segments), the commentary-arc and editorial-cut typically land at
the same OUT.

**6. Audio peak detection (RMS sweep) — fallback when commentary is
hard to time-code.**
```bash
.venv/bin/python -m yt_dlp -q -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best" \
    -o /tmp/clip.mp4 "<youtube_url>"
ffmpeg -y -i /tmp/clip.mp4 -ac 1 -ar 8000 -f wav /tmp/clip.wav
.venv/bin/python -c "
import soundfile as sf, numpy as np
audio, sr = sf.read('/tmp/clip.wav')
w = int(0.5 * sr)
peaks = [(i / sr, np.sqrt((audio[i:i+w]**2).mean()))
         for i in range(0, len(audio) - w, int(0.1 * sr))]
peaks.sort(key=lambda x: -x[1])
print('top 5 RMS peaks:', peaks[:5])
"
```
Top peak = call moment. Set `in_s ≈ peak - 2.5s`, `out_s ≈ peak + 3s`.

### SportsStoriesAnimated — end-to-end cookbook

Distilled from shipping Aguero 93:20 (v1 → v16, 16 iterations) and
Iniesta 2010 WC (v1 → v3c, 3 iterations) on 2026-05-02..03. Every
step below is load-bearing — each one was bought with at least one
failed render.

**Step 1 — Author the raw story.** Hand-write
`sportstoriesanimated/raw/<slug>.json` with
`{slug, title, body, source, url, metadata}`. The body should run
~150-300 words covering the full match context (date, venue,
scoreline, key people, what made the moment iconic). Wikipedia
articles are the natural source.

**Step 2 — Generate the dossier (Stage 2.5).**
```bash
.venv/bin/python -m pipeline.wiki_research \
    --raw sportstoriesanimated/raw/<slug>.json \
    --out sportstoriesanimated/dossier/<slug>.json \
    --wiki-query "<specific search; e.g. 2010 FIFA World Cup Final>" \
    --channel-yaml sportstoriesanimated/config.yaml
```
The LLM produces `people[]` (with era-specific kits + per-character
seeds), `match{}`, `key_moments[]`, and a `pronunciation_dict{}`. The
search query usually needs to be tighter than the slug — for the
Aguero match the slug-based search hit the season page (5k chars);
the explicit "Manchester City 3-2 Queens Park Rangers" query hit the
dedicated 22k-char match article.

**Step 3 — Patch pronunciation respellings for Kokoro.** The LLM
emits all-caps stress markers ("ah-GWAIR-oh") which Kokoro reads as
letter-spelled tokens. Hand-patch the dossier:
```python
fixes = {
    'Aguero': 'Aggwairo',         # not "ah-GWAIR-oh"
    'Dzeko':  'Jecko',
    'Iniesta': 'Een-YESS-ta',
    'siempre con nosotros': 'see-EM-pray con no-SO-tross',
    # …
}
```
Kokoro-friendly form: single-word respellings, mixed case, no
embedded all-caps stress markers, no internal hyphens. Verify by
running `pipeline.asr.transcribe` on the rendered narration.wav and
checking what Whisper hears.

**Step 4 — Generate cast.json from dossier (no LLM).**
```python
from pipeline import cast, wiki_research
cast.cast_from_dossier(
    dossier=wiki_research.load_dossier(<dossier path>),
    channel_cfg=<yaml>,
    out_path=<cast path>,
)
```
Each `supporting[]` entry gets a deterministic seed (sha256 of the
canonical name), enabling per-character cross-beat consistency.

**Step 5 — Find the source clip + commentator timestamps.** Web-search
"<player> <moment> YouTube ALL ANGLES commentary". Prefer "All
Angles & All Commentary" compilations (78s Aguero, 52s Iniesta) —
they typically open directly on the live broadcast with full
commentator call. Avoid clips that bake in stylized post-call replay
(desat B&W, "GOAL" graphics) within the window.

Probe duration:
```bash
.venv/bin/python -m yt_dlp --print "%(id)s|%(duration)s|%(title)s" "<url>"
```

Download:
```bash
.venv/bin/python -m yt_dlp -q -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best" \
    --merge-output-format mp4 \
    -o "sportstoriesanimated/footage/sources/<id>.mp4" "<url>"
```

Transcribe to find the commentary arc:
```python
from pipeline import asr
result = asr.transcribe(<src>, provider='whisper_mlx',
                        model='mlx-community/whisper-large-v3-mlx-4bit')
for seg in result['segments']:
    for w in seg['words']:
        print(f'  [{w["start"]:5.2f}-{w["end"]:5.2f}]  {w["word"]}')
```
Identify three anchors — IN word (buildup phrase like "headed",
"Iniesta's in the middle"), CALL word (the player's surname shouted),
OUT word (the post-call payoff like "Spain have won the World Cup
for the first time in history"). Set `in_s = IN word's start`,
`out_s = OUT word's end + 0.4s breath`. Commentators talk fast — 200ms
matters.

**Step 6 — Author the script with footage block.**
`sportstoriesanimated/narrations/<slug>.json`:
```jsonc
{
  "slug": "...",
  "hook": "<14-word hook>",
  "narration": "<6-12 sentences ending in LIKE/COMMENT closer>",
  "title_options": [...],
  "source_url": "...",
  "source": "manual:sports_moments",
  "footage": [
    {
      "match_text": "Last touch",        // pivot beat (NOT the punchline)
      "url": "https://www.youtube.com/watch?v=<id>",
      "in_s": 0.0,                       // ASR-pinned to IN word
      "out_s": 14.5,                     // ASR-pinned to OUT word + breath
      "audio_mix": 0.75,                 // broadcast volume under narrator
      "black_intro": true                // 1-2s suspense beat before broadcast
    }
  ]
}
```

The narration should NOT describe what the footage is going to show.
Cut the description-of-action sentences ("Balotelli to Aguero. One
touch. Aguero scores.") out — the broadcast commentary tells those
moments. Resume with a payoff line ("Forty-four years of pain. Ended
by one touch.") AFTER the footage.

**Step 7 — Render.**
```bash
.venv/bin/python scripts/make_shorts.py \
    --channel sportstoriesanimated/config.yaml \
    --script sportstoriesanimated/narrations/<slug>.json \
    --slug <slug>
```
Time budget: ~30-45 min on M-series for a 14-16 beat short. Most
time is image generation (z_image_turbo at ~70-300s/beat with thermal
throttling). TTS + ASR + prompts + footage trim + compose are
collectively ~3 min. Metal GPU timeouts during sustained image gen
are expected — caches survive, retry the run.

**Step 8 — Verify before upload.**
- Sample the final mp4 with `ffmpeg -ss <t> -i <mp4> -frames:v 1 <png>`
  every 1s to spot AV-sync drift, gibberish text, mismatched scenes.
- Reverse-ASR the rendered narration.wav with whisper to confirm
  pronunciations actually came out right (the dossier respelling
  gets applied at TTS time, but it's worth verifying).
- Watch the footage cut window manually — does it include the call
  peak? Does it stop before the broadcast switches into a
  desaturated/text-graphic post-call replay?

**Step 9 — Upload.**
```bash
.venv/bin/python upload.py run \
    --channel sportstoriesanimated/config.yaml \
    --slug <slug> \
    --skip-critic        # the in-pipeline critic over-rejects sports
```
The channel YAML's `min_score: 7` is calibrated for AITA; sports
shorts often score 3-6 even when they ship clean. Use `--skip-critic`
until we recalibrate the critic for this channel.

### SportsStoriesAnimated — every gotcha we paid for

Symptom-driven reference. Each row is something we shipped broken
at least once before fixing.

| symptom | root cause | fix file:function |
|---|---|---|
| Footage cut renders all black | `-ss` AFTER `-i` makes fade timestamps anchor to source PTS, not output → fade-out fires before output starts | `pipeline/footage.py:fetch_clip` — use `-ss BEFORE -i` so output PTS resets to 0 |
| Footage video stops 2.7s before audio ends (last seconds black) | source is 25fps, intro is 30fps → concat re-encoder drops frames at the rate mismatch | `pipeline/footage.py:fetch_clip` — force `-r 30` on both broadcast trim AND concat output |
| Center-cropped 16:9 → 9:16 loses ball + passer (action lives at edges) | crop=1080:1920 throws away horizontal thirds | `pipeline/footage.py:fetch_clip` — blurred-letterbox via `split=2[bg][fg]; [bg]…gblur…[bg_blur]; [fg]scale=1080:-2; overlay=…` |
| Footage cut chops mid-action ("season" cut off, ball strike cut off) | `out_s` was set by RMS audio peak, not by commentator's word boundaries | Run whisper on the source first; pin `in_s`/`out_s` to word start/end timestamps |
| Footage drags through low-energy post-call replay (B&W + GOAL graphic) | `out_s` extended past the broadcast's first natural editorial cut | Sample frames inside the window every 0.5s; OUT at the desat/graphic transition |
| Footage cut at "Aguero scores" feels late (cartoon already showed it) | `match_text` was the punchline beat, not the buildup pivot | `match_text = "Last kick"` (buildup phrase). Trim narration that overlaps the footage window |
| Narration overlaps Tyler's call during footage | no narration ducking | `compose._extend_beats_for_footage` inserts silence in narration at the inter-beat seam; `compose_clips` adds `volume=0:enable='between(t,...)'` over the footage window. **Insert silence at `beats[i+1].start`, NOT `beats[i].end`** — ASR underestimates word-final consonants by 200ms, inserting at .end cuts the word's tail off |
| Footage clip is shorter than the audio slot ("Aguero scores" beat is 1.74s, footage is 14.5s) | compose_clips trims clips to `video_dur[i]` | `compose._extend_beats_for_footage` extends beat duration to fit clip; shifts subsequent beats |
| "Last kick of the season" plays simultaneously with broadcast cut | the cut hits at narration start | `script.footage[].black_intro: true` prepends N seconds of black + silence (= beat audio duration + 300ms tail-breath buffer); broadcast plays AFTER narration ends |
| Spanish phrase "siempre con nosotros" mangled by Kokoro | not in pronunciation_dict | Add to dossier `pronunciation_dict`: `"siempre con nosotros": "see-EM-pray con no-SO-tross"` |
| Names with all-caps stress markers ("ah-GWAIR-oh") read as letter-spelled by Kokoro | Kokoro treats ALL-CAPS as letter-spell | Patch dossier respellings to single-word lowercase-after-first-letter form (`Aggwairo`, `Jecko`, `Een-YESS-ta`) |
| Subscribe button at bottom-left, no "SUBSCRIBE" text | diffusion can't render legible text inside cartoon | `pipeline/captions.py:render_subscribe_button` — PIL-rendered red button with white "SUBSCRIBE", overlaid centered for the last 3s by `compose_clips(subscribe_button=True)` |
| Numeric clock displays render as gibberish ("I.4." instead of "88") | diffusion can't render digital displays | `pipeline/images.py:_TEXT_BAIT` includes `clock face`, `scoreboard`, `LED display`, `timer` — strip-text-bait pass rewrites the prompt |
| "Football" in narration → American football trophy in image | model defaults to gridiron when it sees "football" | Channel `image_style_prefix` ends with `"association football soccer context (never american football or rugby)"` |
| Wrong jersey number / wrong era kit on a player | LLM paraphrased the dossier's `visual.kit` and `shirt_number` | (Documented as class-of-bug; server-side cast-locked-tokens injection is the unimplemented future fix in `pipeline/prompts.py:author_beat_prompts`) |
| Image #2 shows a trophy when narration says "Spain vs Netherlands" | image cache reuses `img_NN.png` by index even when prompts.json content shifted | `scripts/make_shorts.py` writes `img_NN.prompt.sha256` sidecar; cache-hit only when hash of `(key_visual, scene, character_description, seed)` matches |
| Sports script_check fails because narration isn't AITA-style | `closer_format` opted us into AITA-class strict gating | `sportstoriesanimated/config.yaml: script_check_strict: false` |
| Random "analyst" face appearing between players | sports cast.json had narrator persona description; prompts.py prepended it | `narrator_visual_mode: voice_only` in channel YAML; make_shorts clears `character_description`; prompts.py uses voice-only authoring preamble that lists supporting[] and forbids "the character" / "the narrator" wording |

### `footage` array — schema

For sports shorts, the script JSON can declare per-beat footage cuts
that splice real broadcast clips into the otherwise-animated short
(the channel's USP — see [`feedback_sports_footage_is_usp.md`](.claude/projects/.../memory/)).
Each entry is a substring match against the narration:

```json
{
  "slug": "aguero-9320",
  "narration": "Final day of the Premier League season. ... Aguero scores. ...",
  "footage": [
    {
      "match_text": "Aguero scores",
      "url": "https://www.youtube.com/watch?v=...",
      "in_s": 84.2,
      "out_s": 87.6,
      "audio_mix": 0.0
    }
  ]
}
```

The first beat whose `text` contains `match_text` (case-insensitive)
is tagged `kind: footage`. `pipeline/footage.py` downloads the source
video via yt-dlp, frame-accurately trims to `[in_s, out_s]`, scales
to 1080x1920, and `compose.compose_hybrid` stitches it in place of
the static cartoon image. `audio_mix` (0.0-1.0) is reserved for v2 —
v1 drops the broadcast audio and keeps narration as the only track.

---

## Stage 2.5 — Wikipedia event dossier (sports channels)

For SportsStoriesAnimated, run `pipeline/wiki_research.py` BEFORE
authoring the cast. The dossier is a structured snapshot of the
event from Wikipedia: every named person who matters to the story,
their era-specific kit / body / hair, and a phonetic pronunciation
respelling so Kokoro doesn't mangle "Aguero" / "Dzeko" / "Mbappé".

```bash
.venv/bin/python -m pipeline.wiki_research \
    --raw sportstoriesanimated/raw/<slug>.json \
    --out sportstoriesanimated/dossier/<slug>.json \
    --wiki-query "Manchester City 3-2 Queens Park Rangers 2012" \
    --channel-yaml sportstoriesanimated/config.yaml
```

Output schema (excerpt):

```json
{
  "match": { "title": "...", "date": "YYYY-MM-DD", "summary": "..." },
  "people": [
    {
      "name": "Sergio Agüero",
      "aliases": ["Aguero", "Kun"],
      "role": "Striker, scored the title-winning goal",
      "team": "Manchester City",
      "visual": {
        "body": "short, stocky, low centre of gravity",
        "hair": "short black, clean-shaven",
        "kit": "Man City 2011-12 home: sky blue, white sponsor",
        "shirt_number": "16"
      },
      "pronunciation_phonetic": "ah-GWAIR-oh",
      "seed": 1569866783
    }
  ],
  "pronunciation_dict": { "Aguero": "ah-GWAIR-oh", "Dzeko": "JEK-oh" }
}
```

`cast.cast_from_dossier` then transforms `people[]` into the
cast.json `supporting[]` schema verbatim — no second LLM call. The
pronunciation_dict is loaded by `scripts/make_shorts.py` and passed to
`audio.synthesize` via the new `pronunciation_dict` parameter; the
respellings apply ONLY to the TTS-input string, so captions and ASR
source-text alignment continue to see the original spelling.

---

## Stage 3 — make spec

Combine the script + raw + channel template into a per-Short spec.

```bash
.venv/bin/python make_spec.py \
    --channel mystoriesanimated/variants/aita_cooking.yaml \
    --script  data/intermediate/aita_cooking/scripts/<slug>.json
```

Output: `<channel>/specs/<slug>.yaml`.

`make_spec.py` walks the channel's `spec_template` block recursively,
substituting only `${script.<path>}`, `${raw.<path>}`, and
`${channel.<path>}` references. Other `${...}` references
(template-internal: `${width}`, `${btn.bg}`, `${title}`) are
preserved for the render-time engine.

### Substitution rules

| Form | Behaviour |
|---|---|
| `"${script.narration}"` (entire value) | returns the raw value (string, list, dict, …) |
| `"u/${raw.metadata.author}"` (embedded) | text-interpolates, always stringified |
| `"${width} - 80"` | left untouched (no `script./raw./channel.` prefix) — render-time engine handles it |
| any non-string | passed through unchanged |

### Path lookups

`${script.title_options.0}` — list indexing supported. `0` is the
first option. Negative indices like `title_options.-1` work via the
underlying lookup path.

---

## Stage 4 — render

Read the spec, generate audio, compute beats, render every overlay,
and compose the mp4.

```bash
.venv/bin/python render_from_spec.py \
    --spec data/intermediate/aita_cooking/specs/<slug>.yaml
```

Output: `<slug>.mp4`.

What happens internally (5 sub-stages, each cached under
`data/cache/<slug>/`):

1. **TTS** — `pipeline.audio.synthesize` writes `narration.wav`. Voice
   and provider come from `audio.narration.tts` in the spec.
2. **Beats** — `pipeline.beats.transcribe_words` runs Whisper-MLX on
   the narration, `pipeline.align.align_source_to_whisper` snaps the
   spec text to those word timestamps, `pipeline.beats.split_into_beats`
   slices at clause boundaries. Output: `beats.json`.
3. **Background** — for `type: per_beat_loops`, one cooking-loop mp4
   per beat (cycled) is trimmed + scale/cropped to the spec resolution
   and concat'd into `bg_concat.mp4`.
4. **Resolve overlays** — for each overlay in the spec:
   - `template: ...` → expand the template (substitute params, run
     `each` loops, compute auto-height, render PNG).
   - `type: emoji_pop` → render emoji PNG with Apple Color Emoji.
   - `type: per_beat_caption` → render one caption PNG per beat.
   Then compute the on-canvas (x, y) by resolving the `position:`
   block (regions, anchors, etc.).
5. **Compose** — build one `ffmpeg -filter_complex` graph:
   - bg_concat as the base
   - each overlay as an extra input, gated by `enable=between(t,from,to)`
   - emoji_pop overlays use a `scale=eval=frame:w='<expr>':h=-1` chain
     to animate scale-overshoot
   - narration audio mapped from input 1
   - one h264/aac mp4 out

---

## Stage 8 — upload to YouTube

Once a Short is rendered (and you're happy with it), Stage 8 ships it
to YouTube. Implemented in `pipeline/upload.py` + the `upload.py` CLI.

### One-time per Google account

YouTube wants OAuth 2.0. You do this dance once per Google account; the
refresh token is cached on disk so subsequent uploads are silent.

1. **Make a Google Cloud project** (under the Google account that
   *owns* the target YouTube channel).
   - Go to <https://console.cloud.google.com/projectcreate>, create a
     project (any name; e.g. `ytfactory-mystoriesanimated`).
2. **Enable the YouTube Data API v3**:
   - APIs & Services → Library → search "YouTube Data API v3" → Enable.
3. **Configure the OAuth consent screen**:
   - APIs & Services → OAuth consent screen → User type **External**.
   - App name: anything (e.g. "ytFactory uploader"). Support email: yours.
   - Scopes: add `.../auth/youtube.upload`.
   - Test users: add the same Google account you'll be uploading from.
   - You don't need to publish/verify — staying in "Testing" is fine for
     a personal uploader.
4. **Create OAuth client credentials**:
   - APIs & Services → Credentials → Create Credentials → OAuth client
     ID → Application type **Desktop app**.
   - Download the JSON. Save it to:
     `~/.config/ytfactory/client_secret.json`
5. **Run the auth flow once**:
   ```bash
   .venv/bin/python upload.py auth --account mystoriesanimated
   ```
   A browser opens. Sign in with the same Google account, grant access.
   Token cached at `~/.config/ytfactory/youtube_token_mystoriesanimated.json`.

For a second YouTube channel, repeat steps 1-5 with a different
`--account` name (e.g. `--account aita_main`). The two tokens live
side-by-side; the channel YAML's `upload.account` picks which one is
used per render.

### Channel YAML — `upload:` block

```yaml
upload:
  account: mystoriesanimated   # picks ~/.config/ytfactory/youtube_token_<account>.json
  privacy: private             # private | unlisted | public
  made_for_kids: false
  category_id: "24"            # 24 = Entertainment
  auto_upload: false           # true → scripts/make_shorts.py uploads at the end
  min_score: 0                 # only auto-upload when critic.score ≥ this
  tags: [aita, reddit, shorts]
  description_template: |
    {script.hook}
    Source: {raw.url}
```

`description_template` supports `{script.X}` and `{raw.X}` token
substitution (same lookup logic as `pipeline.upload.render_description`).
Missing keys render as empty.

### Manual upload

```bash
# upload one rendered short to the YouTube channel for mystoriesanimated
.venv/bin/python upload.py run \
    --channel mystoriesanimated/config.yaml \
    --slug aita02 \
    --privacy private

# schedule a public release (YouTube requires privacy=private under the hood)
.venv/bin/python upload.py run \
    --channel mystoriesanimated/config.yaml \
    --slug aita02 \
    --publish-at 2026-05-02T13:00:00Z

# list everything you've uploaded
.venv/bin/python upload.py status
```

### Auto-upload at the end of `scripts/make_shorts.py`

Set `upload.auto_upload: true` in the channel YAML. After the critic
passes (and `score >= upload.min_score`), `scripts/make_shorts.py` calls
`pipeline.upload.upload_short`. CLI overrides:

```bash
# force-upload even if YAML says auto_upload: false
.venv/bin/python scripts/make_shorts.py --upload --channel mystoriesanimated/config.yaml --script ...

# skip upload even if YAML says auto_upload: true
.venv/bin/python scripts/make_shorts.py --no-upload --channel mystoriesanimated/config.yaml --script ...
```

### Idempotency

After a successful upload, ytFactory writes
`data/uploads/<channel_dir>/<slug>.json` with the YouTube `video_id`.
Subsequent `upload.py run` (or `scripts/make_shorts.py` with `auto_upload`)
calls for the same slug short-circuit and do nothing. Pass `--force`
to re-upload (creates a second video on YouTube — there's no in-place
replacement).

### Web UI

The "Upload to YouTube" button on the rendered-Short panel POSTs to
`/api/uploads`, which runs the upload in a worker thread and exposes
progress via `/api/uploads/<slug>`. Privacy is selectable from the
panel; the channel YAML is auto-resolved from the slug's
`data/intermediate/<channel_dir>/` path.

---

## The spec interpreter

(See [SPEC.md](./SPEC.md) for the full format reference.)

### Time tokens

| Token | Resolves to |
|---|---|
| `<number>` | absolute seconds |
| `start` | `0.0` |
| `end` | `narration_dur + tail_pad_s` |
| `closer_start` | `beat[-1].start` |
| `closer_end` | `beat[-1].end` |
| `beat[<i>].start/end` | per-beat refs (negative `i` allowed) |
| `<expr> + <n>` / `<expr> - <n>` | arithmetic on any of the above |

### Position resolution (auto-layout)

- `x: center` / `y_from_bottom: <n>` — layout helpers
- `region: top_third|middle|lower_third` — convenience presets
- `anchor: <overlay>.<corner>` — relative to another overlay's bbox
- `anchor: <overlay>.<named_anchor>.<corner>` — relative to a named
  anchor inside another overlay's template
- `offset: [dx, dy]` — applied after anchor; the **center** of the
  element is placed at the anchor point + offset

### Templates from primitives

The renderer ships only 6 drawing primitives:
`rounded_rect`, `ellipse`, `text`, `text_block`, `image`, `emoji`.
Plus one composing form: `each` (loop with `as`/`index`).

Every composite element (Reddit card, vote panel, score chip, badge…)
is composed from these primitives in the spec. Adding a new visual
element is YAML, not Python.

### Built-in overlay types

Two overlays have intrinsic logic the primitive system can't express:

- **`per_beat_caption`** — one PNG per beat, swapped at beat boundaries.
- **`emoji_pop`** — color emoji with a scale-overshoot or fade-in
  animation driven by ffmpeg-side time expressions.

### Channel YAML defaults

`render_from_spec.py` accepts `--channel <yaml>`; channel keys
deep-merge under the spec (spec wins on conflicts). Useful for shared
templates that span multiple Shorts in a channel.

---

## Adding a new channel

Worked example: a hypothetical `wiki_oddities` channel that puts a
"Did you know?" card over a slow B-roll loop.

1. **Source adapter** — already exists for Wikipedia
   (`sources/wikipedia.py`). If you needed a new source, write one.
2. **Backgrounds** — produce loops in `mystoriesanimated/wiki_oddities_loops/`
   somehow (could write a `pull_backgrounds.py --query "..."`).
3. **Channel YAML** — copy `mystoriesanimated/variants/aita_cooking.yaml` to
   `mystoriesanimated/variants/wiki_oddities.yaml`, change:
   - top-level `name`/`source` to point at Wikipedia,
   - `spec_template.background.source_dir` to your loops dir,
   - `spec_template.audio.narration.tts.voice` if you want a
     different voice,
   - replace the `reddit_card` template + overlay with your own
     "Did you know" card (write it from primitives),
   - drop the `closer_panel` + `thumb_emoji` overlays — Wikipedia
     facts don't need a YTA/NTA vote.
4. **Run the four commands** for one slug. Iterate on the YAML; no
   Python required.

---

## Adding a new overlay type

Two paths, depending on whether the new thing is *layout* or *intrinsic
behaviour*:

### Path A — describe it in the spec (preferred)

If it's a static visual element (a banner, a chip, a badge, a
progress bar), write a template using primitives. No code change.

### Path B — add a built-in overlay type

If the element has intrinsic per-beat or animation logic the primitive
system can't express (e.g. an animated countdown ring), edit
`render_from_spec.py`:

1. Handle the new `type:` in `resolve_overlays` — render its assets.
2. Handle the new `kind` in `compose` — emit the right ffmpeg filter.
3. Document the type in `SPEC.md`.

---

## Common operations

```bash
# pull 5 fresh AITA stories
.venv/bin/python scripts/pull_stories.py reddit \
    --subreddit AmItheAsshole --limit 5 --channel aita_cooking

# write scripts for them via the skill
# (in the Claude Code session) /make-script

# turn one script into a spec
.venv/bin/python make_spec.py \
    --channel mystoriesanimated/variants/aita_cooking.yaml \
    --script  data/intermediate/aita_cooking/scripts/<slug>.json

# render
.venv/bin/python render_from_spec.py \
    --spec data/intermediate/aita_cooking/specs/<slug>.yaml

# view (macOS)
open <slug>.mp4

# react to a rendered Short like a viewer
# (in the Claude Code session) /critique-video <slug>

# pull fresh cooking backgrounds — pick a top recommended entry from
# mystoriesanimated/cooking_bg_queue.yaml (vertical-native first, then chapter-derived
# chop count, then view count) and pass its url
.venv/bin/python scripts/pull_backgrounds.py \
    --url "https://www.youtube.com/watch?v=<id>" \
    --num-clips 5 --clip-len 25 --check-faces
```

The queue at `mystoriesanimated/cooking_bg_queue.yaml` is the source-of-truth for
"what cooking source video to use next." It's populated by
`scripts/_cooking_bg_research.py` (yt-dlp metadata only — no video
downloads, so disk stays small). Each entry records duration,
vertical-native flag, pace/fit scores, and a `chops:` list of
seconds-windows derived from chapters where available. Move an entry
to `status: rejected` (with `rejection_reason`) once you've ruled it
out, or comment it out of the queue once its loops are in
`mystoriesanimated/cooking_loops/`. Re-run the helper to add fresh candidates.

---

## Caching & re-runs

`render_from_spec.py` caches under `data/cache/<slug>/`:

- `narration.wav` — TTS output. Delete to re-TTS.
- `beats.json` — Whisper word timestamps. Delete to re-align.
- `bg_segs/` and `bg_concat.mp4` — per-beat scaled segments.
  Delete to re-cut (run again after pulling new bg loops).
- `ov_<id>.png`, `ov_<id>_<i>.png` — overlay PNGs. Auto-overwritten
  per run; delete the cache dir if a stale render still shows up.

Cheap to nuke and rebuild:

```bash
rm -rf data/cache/<slug>
.venv/bin/python render_from_spec.py --spec <channel>/specs/<slug>.yaml
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "no loops in `assets/<dir>`" | run `pull_backgrounds.py` for that channel first. |
| "unknown name: `<x>`" during overlay resolution | a template references a name that wasn't passed in `args`. Check `params:` matches the args block. |
| "anchor target not yet placed" | overlay A anchors to overlay B but B is later in the list. Reorder so anchor targets come first. |
| Reddit card title says `${title}` literally | YAML quoted-flow trap — `${...}` inside `{...}` confuses YAML. Wrap the value in quotes: `"${title}"`. |
| Caption gaps (dead air) | spec has `extend_to_next_beat: false` — flip to `true`. |
| Voice sounds wrong | check `audio.narration.tts.voice` — Kokoro voice IDs are like `af_bella`, `af_heart`. |

---

## What "done" looks like for v0

- One command per stage, four commands total (or three with the skill).
- Re-running after a YAML edit takes seconds (TTS/beats cached;
  only overlay PNGs + ffmpeg compose redo).
- New visual variations are YAML edits.
- New niches are one-file additions in `channels/`.

The pipeline is intentionally boring — that's the point. The "creative"
part lives in the spec.

---

# Path 2 — `scripts/make_shorts.py` (slideshow / animation with LLM autonomy)

The spec-driven path above is for channels whose visual format is "text
overlay on a background loop" (cooking, etc.). The other path,
`scripts/make_shorts.py`, renders **per-beat illustrated** Shorts: the LLM
authors a recurring narrator + per-beat image prompts, the renderer
makes one image (or animation clip) per beat, ffmpeg crossfades them
with karaoke captions. The autonomy is heavier — three LLM stages
(cast / prompts / critic) plus a render-time image quality gate.

```
┌────────────┐  ┌────────────┐  ┌──────┐  ┌──────────┐  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│ pull       │→ │ /make-     │→ │ cast │→ │ TTS +    │→ │ prompts │→ │ images   │→ │ compose  │→ │ critic   │
│ stories    │  │ script (or │  │ (LLM)│  │ beats    │  │ (LLM)   │  │ + Q-gate │  │ (ffmpeg) │  │ (LLM)    │
│            │  │ -movie-    │  │      │  │ (Whisper)│  │         │  │          │  │          │  │ +regen   │
│            │  │ short)     │  │      │  │          │  │         │  │          │  │          │  │ loop     │
└────────────┘  └────────────┘  └──────┘  └──────────┘  └─────────┘  └──────────┘  └──────────┘  └──────────┘
```

Run it like the spec-driven path's stages 1-2, then:

```bash
.venv/bin/python scripts/make_shorts.py \
    --script  data/intermediate/aita_animated/scripts/<slug>.json \
    --channel mystoriesanimated/variants/aita_animated.yaml
```

`scripts/make_shorts.py` reads the channel YAML for provider knobs (TTS, ASR,
image, motion) and runs the autonomous chain end-to-end.

---

## LLM-driven autonomous stages

All three stages shell out to `claude -p` via `pipeline/llm.py`. No
Anthropic SDK, no API key, no model in the env — uses the user's
existing Claude Code OAuth keychain.

### `pipeline/llm.py` — claude CLI wrapper

`call_claude_cli(prompt, *, output_json, model="haiku", ...)` runs the
CLI in a hardened utility-call shape:

- `--output-format json` — machine-readable envelope, parsed back.
- `--no-session-persistence` + `--setting-sources user` — no project
  CLAUDE.md, no skills, no hooks.
- `--max-budget-usd 0.50` (default) — runaway prompts can't burn dollars.
- `allowed_tools=None` → `--tools ""` so the call can't touch the
  filesystem unless explicitly opted in.
- `--json-schema` for structured output validation when supplied.

`_parse_inner_json` strips ```json fences and falls back to balanced-
brace extraction so models that wrap output in markdown survive.

Errors raise `ClaudeCLIError` with both stderr + stdout snippets.

### `pipeline/cast.py` — per-story narrator

The channel YAML's `image_style_prefix` locks the visual *aesthetic*
(line style, palette). The narrator is authored per-story so the
character on screen matches the source narrator's age, gender,
profession, and emotional tone. Output:

```json
{
  "narrator": {
    "description": "<30-50 word cartoon character description>",
    "default_emotion": "tense | frustrated | amused | desperate",
    "age_band": "child | teen | young-adult | adult | middle-aged | elder",
    "gender": "female | male | non-binary | unspecified"
  },
  "supporting": []
}
```

Lives at `<channel>/cast/<slug>.json`. Falls back to
the channel YAML's `character_description` if the file is missing.

`load_cast(path)` returns the dict or `None` (missing/malformed —
silent fallback so legacy channels keep working).

### `pipeline/prompts.py` — per-beat image prompts

Authors one `{key_visual, scene}` object per beat from the narration +
beats + cast + channel style prefix. The system prompt enforces
DESIGN.md §14 principles:

- #3: bans text-bait words (label, sign, text, logo, …)
- #11: scene under ~50 tokens (attention dilution past that)
- #12: one main subject per beat (no "three friends")
- #13: `key_visual` is the punchline weighted ahead of `scene`
- #7: beat 0 must contain ≥2 concrete tokens

`_validate_and_clean(raw, beats, opening_directives)` enforces the
schema after the LLM returns: count must match `len(beats)`, every
item has a non-empty `scene`, and beat 0 is checked against the
channel's `opening_image_directives` (warning-only).

Output: `data/cache/<slug>/prompts.json`. The orchestrator picks it up
via `pipeline.images.load_prompts` — same path the heuristic fallback
uses.

### `pipeline/critic.py` — auto-critique + regen loop

After compose:

1. `critique_short` ffmpeg-samples the rendered mp4 at 1 fps, then
   sends the frames + `beats.json` to claude (sonnet — vision quality
   matters) with a viewer-and-engineer system prompt. Returns:

   ```json
   {
     "score": 1..10,
     "one_line_take": "would I keep watching",
     "top_issues": ["<issue with timestamp + classification>"],
     "beat_corrections": {"<i>": "<one-off prompt nudge>"},
     "system_corrections": [
       {"issue_class": "...", "where": "pipeline/X:fn",
        "fix": "...", "principle": "DESIGN.md §14 #N or NEW"}
     ],
     "highest_leverage_change": "..."
   }
   ```

2. `regenerate_with_corrections` patches `prompts.json` per the
   `beat_corrections` and deletes the affected `img_NN.png` so the
   next orchestrator pass regenerates only those beats and recomposes.

The critic's distinction between **one-off** and **class-of-bug**
issues is deliberate: class-of-bug fixes live in code/schema and
prevent the next 100 Shorts from hitting the same problem. The
orchestrator surfaces `system_corrections` to stdout so the operator
can fold them back into DESIGN.md §14.

The score is written to `data/critiques/<slug>/<slug>.score.json`.

---

## Story screening & quality gates

Three pre/post checks run automatically; all are pure-Python and
testable.

### `pipeline/visualizability.py`

Pre-TTS gate. Heuristic 0..1 score on a story's "drawability":

- length sanity (200-3000 chars sweet spot),
- concrete nouns per ~100 words (rooms, props, people roles),
- physical-action verbs,
- numbers / dollar amounts (inciting wedges),
- dialogue-heaviness penalty (mostly-quotes stories render weakly).

Below threshold (default 0.4) the orchestrator drops the story before
spending Flux cycles on it.

### `pipeline/script_check.py`

Pre/post-beat narration shape checks (DESIGN.md §14 #4, #5):

- **#4 closer CTA** — last sentence must contain a vote-prompt
  (`AITA?`, `WIBTA?`, `What do you think?`, `?` etc.). For AITA-class
  channels (channel `closer_format` set), the LIKE-if-YTA / COMMENT-if-NTA
  split is *also* required (memory feedback: vague "vote in comments"
  closers underperform).
- **#5 hook in first ~8 words** — must contain `?`, an AITA frame, or
  a strong-claim verb (refused, told, threw, banned, …). 50+ such verbs
  whitelisted.
- **#5 wedge in first ~30 words** — must contain a number or quantity.
- **slow_hook** — beat 0 longer than 2.5s is flagged.

For AITA channels with `closer_format` set, soft principles escalate
from warning → error. `report(issues, fail_on_error=True)` raises.

### `pipeline/quality_gate.py`

Post-image-gen rejection. Catches obvious diffusion failures so the
orchestrator can retry with a bumped seed:

- file size below 30 KB → blank/crashed model,
- pixel std-dev below 12 → nearly-flat (all-black/all-blank),
- edge density below 0.005 → total mush.

Conservative thresholds tuned for the flat-cartoon AITA aesthetic;
high-detail photorealistic channels would need to relax `min_stddev`.

---

## Slash commands (skills)

These run inside the Claude Code session, not as Python CLIs.

| Skill | What it does |
|---|---|
| `/make-script` | Pull from Reddit / Wikipedia / TIH / YouTube and write a 50-80 word hook-first narration. Drops `script.json` per Short. The "Writer" hat only. |
| `/make-movie-short` | Wears four hats — Writer → Director → DP → Prompter — and produces a `script.json` + `cast.json` + `shotlist/<slug>.json` (camera angle, framing, lighting, blocking, palette, per-shot AI prompts). Use for cinematic / "directed" Shorts where you want full shot-by-shot control instead of the autonomous prompts.py author. |
| `/critique-video` | Pretends to be a Shorts viewer scrolling past your rendered mp4. Samples frames at 1 fps, reads each, builds a second-by-second reaction with timestamped complaints, writes `data/critiques/<slug>.md`. Read-only. |

The `/make-movie-short` shotlist lives at
`<channel>/shotlist/<slug>.json` and `scripts/make_shorts.py`
picks it up the same way it picks up cast.json + prompts.json — every
hand-off is a JSON file on disk.

---

## Cooking-bg curation (channel-specific)

The `aita_cooking` channel needs vertical-native or center-croppable
silent cooking footage. Curating sources is an ongoing research task,
so the project keeps a queue file rather than just trusting yt-dlp's
top search hit.

- **`mystoriesanimated/cooking_bg_queue.yaml`** — schema-versioned queue. Each entry
  records `video_id`, `url`, `duration_s`, `view_count`,
  `vertical_native`, `pace_score`, `fit_score`, a list of `chops:`
  (seconds-windows, chapter-derived where possible), `status:
  recommended | rejected | used`, and a free-text `notes:` field.
- **`scripts/_cooking_bg_research.py`** — minimal yt-dlp wrapper. Two
  subcommands: `search <query> [n]` (returns flat metadata) and
  `deep <id1,id2,...>` (per-video chapters, dimensions, tags). Both
  run with `skip_download: True` — no video bytes hit disk during
  research, only metadata.

Workflow: run the research helper to expand the queue, hand-curate
`pace_score`/`fit_score`/`chops:`/`status:`, then pass a recommended
entry's URL to `pull_backgrounds.py` to actually download + slice.

---

## Pipeline modules — quick reference

| File | Purpose |
|---|---|
| `pipeline/audio.py` | TTS (Kokoro / F5-TTS) — provider switchable. |
| `pipeline/asr.py` | Word-level transcription (whisper_mlx / parakeet_mlx). Heavy imports lazy. |
| `pipeline/transcribe.py` | Stage 1 thin wrapper for long-form transcription. |
| `pipeline/segment.py` | Stage 2 — story boundaries inside a long transcript. |
| `pipeline/rewrite.py` | Stage 3 — script rewriter (LLM-authored or heuristic). |
| `pipeline/script_check.py` | Narration-shape validators (CTA, hook, wedge). |
| `pipeline/visualizability.py` | Pre-TTS heuristic gate on story drawability. |
| `pipeline/cast.py` | Per-story narrator authoring via `claude -p`. |
| `pipeline/beats.py` | Beat split at clause boundaries (uses ASR word timestamps). |
| `pipeline/align.py` | Snap source narration to ASR word timestamps. |
| `pipeline/prompts.py` | Per-beat image prompt authoring via `claude -p`. |
| `pipeline/images.py` | Image gen (sd_turbo / sdxl_lightning / mflux) + prompt linter + IP-Adapter wiring. |
| `pipeline/quality_gate.py` | Post-image-gen rejection (size, std-dev, edge density). |
| `pipeline/animation.py` | Per-beat continuous animated clips (AnimateDiff). |
| `pipeline/captions.py` | Karaoke caption PNG renderer. |
| `pipeline/compose.py` | ffmpeg compose: slideshow OR clips path. |
| `pipeline/critic.py` | Post-render auto-critique + regen-weak-beats loop. |
| `pipeline/llm.py` | `claude -p` subprocess wrapper for all autonomous stages. |

