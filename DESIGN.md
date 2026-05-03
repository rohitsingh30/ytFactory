# ytFactory — Design Document

An automated factory for producing viral YouTube Shorts at scale by converting
long-form source material (Reddit-story videos, sitcom episodes, Wikipedia
articles, podcasts, etc.) into 10–20 second vertical animated-image videos
through a modular AI pipeline.

---

## 1. Vision

Build a pluggable system where one **source adapter** mines raw content, a
**shared backbone** transforms it into a finished Short, and one **uploader**
publishes to YouTube. Each new content niche is a new adapter, not a new
pipeline.

The factory should:

- Produce ≥10 watchable Shorts per day per channel with no manual editing.
- Cost <$0.10 per Short on average.
- Lock visual + audio style per channel so the YouTube algorithm recognizes
  the channel identity (this is non-negotiable for Shorts performance).
- Be cache-and-resume safe — re-running on the same source skips completed
  stages.

---

## 2. Use Cases (concrete examples)

### 2.1 Reddit-story compilation → multi-Short factory

**Input**: an existing 30–60 minute YouTube video (e.g. an r/AITA narrator
reading several stories over Subway Surfers gameplay).

**Output**: 5–15 individual Shorts, one per story, each 10–20s, with
AI-generated illustrated images that change every ~2s.

This is the **flagship use case** for v0.

### 2.2 Sitcom "Top 5 funniest moments" compilation

**Input**: a folder of full Family Guy / Simpsons / Rick and Morty episodes.

**Output**: themed Top-5 Shorts ("Top 5 Stewie burns from Season 4").

⚠️ Copyright reality: Fox/Disney IP triggers Content ID. Works as a
traffic/audience-growth play, not direct monetization.

### 2.3 Direct-from-Reddit story Shorts

**Input**: Reddit JSON API for a chosen subreddit's top posts.

**Output**: same as 2.1, but sourced from text rather than extracted from
a long video. Same pipeline minus the transcription stage.

### 2.4 Wikipedia oddities daily

**Input**: `List_of_unusual_deaths`, `List_of_common_misconceptions`, etc.

**Output**: 10–20s "Did you know" Shorts.

### 2.5 Today-in-history daily

**Input**: Wikipedia "On this day" feed.

**Output**: One historical-event Short per day, narrated over illustrated
key moments.

---

## 3. Pipeline Architecture

The pipeline is 8 sequential stages. Each is a separate module with cached,
resumable output.

```
┌─────────────┐  ┌─────────┐  ┌─────────┐  ┌──────┐  ┌───────┐  ┌────────┐  ┌─────────┐  ┌────────┐
│ 1.Transcribe│→ │2.Segment│→ │3.Rewrite│→ │4.TTS │→ │5.Beats│→ │6.Images│→ │7.Compose│→ │8.Upload│
└─────────────┘  └─────────┘  └─────────┘  └──────┘  └───────┘  └────────┘  └─────────┘  └────────┘
```

### Stage 1 — Transcribe

Run Whisper (large-v3 or distilled) on the source video to get word-level
timestamps for the entire transcript. If the source has embedded subtitles,
extract those instead — much cheaper.

- Tool: `whisper` (OpenAI) or `whisperx` for word alignment
- Output: `transcript.json` with `[{word, start, end}]`

### Stage 2 — Segment (story boundaries)

Feed the transcript to Claude with a prompt: "Split this into individual
self-contained stories. Return `[{title, start_ts, end_ts, summary}]`."

A 40-minute Reddit-narration video typically yields 5–15 stories. This is
where the leverage lives — one source becomes many Shorts.

- For sitcom-clip pipeline: replace this stage with **scene + joke detection**
  (PySceneDetect + LLM ranking + audio energy peaks).
- For text-source pipelines (Reddit JSON, Wikipedia): this stage is the
  source-adapter's responsibility instead.

### Stage 3 — Rewrite (hook + condense)

For each story, Claude rewrites it as a 10–20s narration:

- **Hook in the first 1.5s** (single highest-leverage element)
- 50–80 words total
- Conversational, present tense
- Ends with a question or twist (drives comment engagement)

Optionally, return the rewritten script segmented into ~2-second beats
already, with one image-prompt per beat.

### Stage 4 — Voiceover (TTS)

Generate audio with **ElevenLabs** (recommended) or OpenAI TTS (cheaper).

Decisions:

- **One voice per channel**, locked. Channel identity.
- **Re-TTS rather than reusing the source's original audio** — gives full
  pacing control over the hook. Original-audio reuse is an option only if
  the source narrator's voice IS the brand.
- Add subtle pacing: pause 200ms after the hook, slight speedup mid-story.

### Stage 5 — Beat split

Run Whisper again on the **TTS output** to get word-level timestamps for the
new audio (TTS pacing differs from any original). Then split into beats
**at sentence boundaries** — one beat per spoken sentence (or two short
sentences merged). A beat may be 1.4s or 2.6s; image swaps line up with
natural pauses, and one beat = one rendered image.

Algorithm (`pipeline/beats.py:split_into_beats`, three passes):

1. Cut at every sentence terminator `.!?`.
2. If a single sentence exceeds `max_s` AND has a clause break (`,;:`),
   split it at the comma nearest the middle. Otherwise leave it whole —
   we never cut mid-clause; sentences too long without commas are a
   script-quality issue fixed upstream by `rewrite.py`'s
   subtitle-friendly prompt rules (see Principle #30).
3. Merge adjacent groups when one is shorter than `min_s` AND the
   merged duration is under `max_s` AND the merge stays at ≤2 sentences.

Defaults: `target_s=1.8 / max_s=2.8 / min_s=1.0`. Yields ~8–12 beats
on a 12–18s narration, which feeds Stage 6 with the same number of
distinct images.

### Stage 6 — Image generation

For each beat:

1. Claude writes an image prompt derived from that beat's text + a fixed
   style prefix (e.g. *"3D Pixar style, soft cinematic lighting, shallow
   depth of field"*).
2. Generate via **Flux Schnell** through Replicate (~$0.003/image, fast,
   good quality) or local **SDXL** (free with a GPU).
3. **Lock the seed + style prefix** across the whole channel — this is what
   creates visual cohesion.
4. For recurring characters: Flux + a character LoRA, or Midjourney `--cref`.

Typical: 8–12 images per Short (one per beat, see Stage 5).

### Stage 7 — Compose (the actual video)

Use **ffmpeg** (Python via subprocess) to assemble:

- Each image displayed for its beat's exact duration
- **Ken Burns** per image (`zoompan` filter): a fast punch-in 1.0→1.12
  over the first ~12 frames to land the cut, then a slow continued
  drift 1.12→1.16 across the rest of the clip — guarantees no two
  frames are pixel-identical (see Principle #6)
- **Black-bridge transitions** ~180ms between images
  (`xfade transition=fadeblack`, NOT `fade`) — content frames never
  overlap, so two AI-generated images can't blend into a ghost
  double-exposure (see Principle #27)
- **Karaoke-style captions** burned in, synced to word timestamps —
  non-negotiable for Shorts retention
- TTS audio as primary track
- Optional background music at low volume (-20dB)
- Output: 1080×1920, 30fps, H.264, faststart

Alternative: **Remotion** (React-based programmatic video). Easier to
template, harder to deploy. ffmpeg first.

### Stage 7.5 — Multi-lens auto-critique loop

Every rendered Short is automatically critiqued before it ships.
``pipeline/critic.py`` samples the mp4 densely (1 fps) with ffmpeg,
reads ``beats.json`` for the word-level audio timeline, and walks
every frame through **15 explicit lenses**:

| # | lens | catches |
|---|---|---|
| L1 | Hook (0.0–1.5s) | scroll-past in 1.5s; generic hook frames |
| L2 | Audio-visual sync | image-vs-spoken-word lag |
| L3 | Character continuity | morphing character; broken channel/cast lock |
| L4 | Pacing / retention | dead spots; image held too long |
| L5 | Caption legibility | contrast, size, position, line wrap on phone |
| L6 | AI-glitch | fingers, faces, gibberish text, anatomy |
| L7 | Composition | thirds, headroom, leadroom, joint crops |
| L8 | Palette / channel aesthetic | drift from locked house style |
| L9 | Mute mode | story breaks without sound (80% of viewers) |
| L10 | Thumbnail (frame 0) | would frame 0 stop a scroll thumb |
| L11 | Story arc / escalation | flat energy; missing "wait what" beat |
| L12 | Closer / CTA | panel legibility, hold time, LIKE/COMMENT split |
| L13 | Source fidelity | narration sanded off the punchline |
| L14 | Re-watchability | tap-replay detail in the hook |
| L15 | Comments-bait | drives specific opinions vs vague mush |

The critic returns a structured JSON to
``data/critiques/<slug>/<slug>.score.json``:

```json
{
  "score": 7,
  "one_line_take": "...",
  "top_issues": [...],
  "per_frame_findings": [
    {"beat": 4, "timestamp_s": "7.0-9.6s", "lenses": ["L2","L3"],
     "what_is_wrong": "...", "classification": "class-of-bug",
     "fix": "..."}
  ],
  "beat_corrections": {"4": "rewrite to put wine bottles in foreground"},
  "system_corrections": [
    {"issue_class": "narrator-emotion-mismatch",
     "where": "pipeline/cast.py:author_cast",
     "fix": "...", "principle": "#15 (NEW)"}
  ],
  "highest_leverage_change": "..."
}
```

**Two-tier fix model**, per principle #21 (§14):

- ``beat_corrections`` — ONE-OFF prompt edits. Auto-applied by
  ``regenerate_with_corrections``: the affected beats' images get
  deleted and regenerated from the patched prompt, then a single
  recompose runs. Costs minutes, fixes this video only.
- ``system_corrections`` — CLASS-OF-BUG fixes targeting
  ``pipeline/*.py``, ``script_check.py``, channel YAMLs, or the
  principles list itself. Surfaced to stdout for the operator to
  apply in code. Costs an edit, fixes the next 100 Shorts.

The critic prefers system corrections over beat corrections when both
apply — being wrong towards system fixes is cheaper than being wrong
towards whack-a-mole patches (memory: ``feedback_engineer_class_of_bug``).

If the score is below ``min_critic_score`` (channel YAML, default 6)
and beat_corrections are non-empty, the orchestrator regenerates the
weak beats once and re-scores. If the score doesn't recover, the
operator inspects the score JSON and the printed system corrections
to decide whether to re-author the shot list (for /make-movie-short
runs) or accept the Short as-is.

Skipping the critic with ``--no-critic`` is reserved for fast
iteration on the renderer itself — never for production runs. The
human-readable mirror is the ``/critique-video`` skill, which uses
the same lens framework and writes a markdown reaction to
``data/critiques/<slug>.md``.

### Stage 8 — Upload

YouTube Data API v3:

- Title = the rewritten hook line (Claude can A/B suggest 3 variants)
- Description includes `#shorts` + source attribution + relevant hashtags
- Tags derived from story content via Claude
- Schedule across the day for algorithmic spread

v0 can leave this manual; automate once 5+ shorts are reliably watchable.

---

## 4. Source Categories — Where Ideas Come From

The source-adapter is the only stage that varies per niche. Six broad
categories, each with many specific sources.

### A. Text-from-the-internet → animated-image Shorts

Reuses the full pipeline as-is. Cheapest, highest volume.

- Reddit niches: AITA, TIFU, MaliciousCompliance, ProRevenge,
  EntitledParents, NoSleep, Relationship_Advice, ChoosingBeggars, LetsNotMeet,
  Glitch_in_the_Matrix
- 4chan greentexts (already formatted as beats)
- Twitter/X viral threads
- Quora absurd questions/answers
- Wikipedia oddities — `List_of_unusual_deaths`, `List_of_common_misconceptions`
- Two-sentence horror / micro-fiction
- Court case summaries (CourtListener API)
- True crime case files
- Historical events ("On this day in 1923…")
- Mythology & folklore (Greek, Norse, Japanese yokai, African folktales)
- Urban legends / cryptids
- Conspiracy theories (neutral framing)
- Religious/Bible stories retold

### B. Existing long-form video → clipped Shorts

Mining = transcribe + LLM-rank + cut.

- Animated sitcoms (Family Guy, Simpsons, Rick and Morty, Bojack, South Park)
- Live-action sitcoms (Office, Seinfeld, Always Sunny, Friends)
- Podcast clips (JRE, Lex Fridman, Diary of a CEO, Huberman)
- Interview shows (Hot Ones, Theo Von, late-night)
- Stand-up specials
- Gaming streams/VODs (Twitch clips API gives pre-ranked)
- Sports broadcasts
- Iconic movie scenes
- YouTube video essays (extract the killer insight)
- News broadcasts
- Reddit-story compilation channels (the v0 use case)
- Court trial livestreams

### C. Structured-data → listicle Shorts

Pure data-in / video-out. Most automatable.

- "Top 10 X" from any ranked dataset (countries by GDP, fastest animals,
  tallest buildings, deadliest diseases, richest people)
- Wikipedia "List of" pages
- IMDb top X by genre/decade
- Sports stats leaderboards
- Stock movers (yfinance)
- Crypto top gainers/losers
- Box office weekend recap
- Steam top-played
- Spotify chart movement
- "X vs Y" head-to-heads (sizes, speeds, prices, dates)

### D. Trend-riding / news Shorts

Time-sensitive, hardest to automate well, highest virality ceiling.

- Daily AI news recap (HN front page + arXiv + tech RSS)
- Tech news daily
- Breaking news explainer
- Crypto/market news
- Science paper of the week (arXiv → ELI5)
- Sports recap (yesterday's games)
- Patch notes as Shorts (gaming, software releases)
- GitHub trending repos explained

### E. Knowledge / explainer Shorts

Persona-driven, builds repeat audience.

- Today-in-history daily
- Etymology / word origins
- Animal facts / weird biology
- Geography quirks
- Physics/math explainers (one concept per Short)
- Book summaries (one chapter or one idea per Short)
- Famous quote + backstory
- Logical fallacies / cognitive biases (~365 episodes worth)
- Programming tips (one trick per language per Short)

### F. Synthetic / generated Shorts

LLM generates the source itself. Infinite supply, low authenticity.

- AI-generated original creepypasta
- "What if" scenarios — alternate history, sci-fi premises
- Hypothetical ethics dilemmas (trolley-problem variants)
- AI-generated "fake but plausible" historical anecdotes ⚠️ disclose
- Tarot / horoscope of the day
- Daily riddle / lateral thinking puzzle
- Joke of the day
- Daily affirmations / motivation

### Picking for v0 — three filters

1. **Source supply** — does this source produce 10+ items per day on
   autopilot? (Reddit ✅, Family Guy ❌ — finite library)
2. **Algorithmic format fit** — does the content type already perform on
   Shorts? (Reddit stories ✅, podcast clips ✅, listicles ✅,
   philosophical explainers ⚠️)
3. **Copyright cleanliness** — UGC/public-domain ✅, broadcast TV/podcasts
   ⚠️ (Content ID hits)

**Recommended v0 portfolio**: Reddit stories (A) + Wikipedia oddities (A)
+ Today-in-history (E). All three feed the same animated-image pipeline,
all have infinite source supply, all are copyright-clean. **One backbone,
three channels.**

---

## 5. Tech Stack

**Decision: local-first stack for v0.** Zero paid APIs, runs entirely on
the developer's Apple Silicon Mac. Production-grade paid services are an
opt-in upgrade for v1.

### v0 stack (local, $0/short)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for ML/media glue |
| Video composition | **ffmpeg** (subprocess) | Fast, battle-tested. Remotion if templating gets painful. |
| Transcription | **mlx-whisper** | Apple-native, fastest on M-series. WhisperX as fallback when word-alignment quality matters most. |
| LLM (during dev) | **Claude Code (this CLI)** | Free for the developer; segmentation + rewriting done interactively in the dev session and dropped to JSON for the pipeline to consume. |
| TTS | **Kokoro TTS** (`Kokoro-82M`) | Best open-source TTS quality-per-effort. Near-ElevenLabs quality, ~real-time on M-series, ~330 MB model. Piper as fallback if Kokoro is fiddly. |
| Image gen | **MLX-SDXL-Turbo** for v0; **MFLUX (Flux schnell, 4-bit)** for v0.5+ | SDXL-Turbo: ~3 GB, ~1–3s/image. MFLUX: ~7 GB, ~10–20s/image, noticeably better quality + style consistency. Start small. |
| Job queue | **SQLite + a worker loop** | No need for Celery/Redis at v0 |
| Storage | Local filesystem | Add S3 only when scaling beyond one machine |
| Upload | **YouTube Data API v3** (deferred to v0.5) | Manual upload for v0 |

### v1 upgrade path (paid, autonomous)

When the local pipeline is proven and we want to run unattended at scale:

| Layer | Upgrade | Why |
|---|---|---|
| LLM | **Anthropic API (Claude)** | Required once the pipeline runs without a human in the dev session |
| TTS | **ElevenLabs** (optional) | Higher quality + voice cloning. ~$0.03/short. Only if Kokoro proves to be the weak link. |
| Image gen | **Flux Schnell via Replicate or fal.ai** (optional) | ~$0.003/image, faster than local, hosted. Only if local image gen becomes a bottleneck. |

### API keys

**v0**: none required.

**v1+**:

- `ANTHROPIC_API_KEY` (always, once autonomous)
- `ELEVENLABS_API_KEY` (optional quality upgrade)
- `REPLICATE_API_TOKEN` or `FAL_KEY` (optional speed upgrade)
- `YOUTUBE_OAUTH_CLIENT_ID` + `YOUTUBE_OAUTH_CLIENT_SECRET` (for auto-upload)

### System dependencies

- `ffmpeg` (Homebrew, ~250 MB installed)
- Python 3.11+ with a project venv

### Disk footprint

| Stack | Total | Notes |
|---|---|---|
| **v0 minimum** | **~5.5 GB** | ffmpeg + venv + Kokoro + Whisper-medium + MLX-SDXL-Turbo + working data |
| **v0.5 recommended** | ~10–11 GB | Same + swap SDXL-Turbo for MFLUX (Flux schnell 4-bit, ~7 GB) and upgrade Whisper to large-v3-4bit |
| **Premium** | ~28–30 GB | Flux-dev full precision + Whisper large-v3 full precision |

Models cache in `~/.cache/huggingface/hub/` and `~/.cache/mflux/`, not in
the project dir. The project dir itself stays ~1 GB including `data/`.

---

## 6. Project Layout

```
ytFactory/
├── pipeline/
│   ├── transcribe.py       # Stage 1
│   ├── segment.py          # Stage 2 (per-source-type variants in /sources)
│   ├── rewrite.py          # Stage 3
│   ├── audio.py            # Stage 4 (TTS)
│   ├── beats.py            # Stage 5
│   ├── images.py           # Stage 6
│   ├── compose.py          # Stage 7 (ffmpeg)
│   └── upload.py           # Stage 8 (YouTube API)
├── sources/                # Source adapters — one per niche
│   ├── reddit_video.py     # 2.1: long-form Reddit-narration video
│   ├── reddit_api.py       # 2.3: direct Reddit
│   ├── sitcom_clips.py     # 2.2: scene+joke mining for Family Guy etc.
│   ├── wikipedia.py        # 2.4: oddities pages
│   └── today_in_history.py # 2.5
├── channels/               # Per-channel config (style, voice, niche)
│   ├── aita_animated.yaml
│   ├── wiki_oddities.yaml
│   └── today_in_history.yaml
├── data/
│   ├── sources/            # Input long-form videos / scraped text
│   ├── shorts/             # Final .mp4 outputs
│   └── cache/              # Per-stage cached intermediates
├── make_shorts.py          # Orchestrator — run full pipeline on one source
├── pyproject.toml
└── .env                    # API keys
```

### Channel config example (`channels/aita_animated.yaml`)

```yaml
name: AITA Animated
source_adapter: reddit_video
tts_provider: kokoro              # v0: kokoro | v1: elevenlabs
tts_voice: af_bella                # Kokoro voice ID
image_provider: mlx_sdxl_turbo    # v0: mlx_sdxl_turbo | v0.5: mflux | v1: replicate_flux
image_style_prefix: |
  Simple minimalist 2D doodle illustration, hand-drawn ink line art,
  chibi style, round oversized head with tiny dot eyes and small smile,
  blocky simple body, flat colors, white background, no shading,
  cute children's-book aesthetic, single character centered
image_reference: assets/character_ref.jpg   # for IP-Adapter (v0.5+)
image_seed: 42
duration_target_s: [10, 20]
hook_emphasis: high
upload_schedule: ["09:00", "13:00", "18:00", "21:00"]
```

### Style decision (locked)

The character style is a hand-drawn minimalist 2D chibi doodle (per user
reference sketch saved at `assets/character_ref.jpg`):

- Flat 2D, hand-drawn ink line, no shading
- Round oversized head, tiny dot eyes, simple smile
- Blocky/geometric body, short hair
- White background, single character centered

This style is intentionally chosen because (a) simple line art is
*easier* for small image models (SDXL-Turbo) to render consistently,
(b) it's distinctive vs the saturated 3D-Pixar default everyone else
uses on Shorts, and (c) it suits the AITA-storytelling tone.

**Character consistency strategy across a Short (5–10 images):**

| Phase | Method | Consistency |
|---|---|---|
| v0 | Locked seed + identical character description prefix in every prompt | ~70% |
| v0.5 | IP-Adapter, fed the reference sketch alongside each prompt | ~85% |
| v1 | Tiny character LoRA trained on 10–20 variants | ~95% |

Start with v0 method; upgrade only if drift is visibly distracting.

---

## 7. Cost Model

### v0 (local stack)

Per Short: **$0** (all compute is local).

Per-Short *time* on M-series Mac, end-to-end: ~3–5 minutes
(image gen dominates; ~30s for TTS + Whisper + compose, ~2–4min for
5–10 SDXL-Turbo / MFLUX images).

One-time cost: ~5.5 GB disk for the minimum stack (see §5).

### v1 (autonomous, paid)

Once we add API keys for unattended operation:

| Item | Cost |
|---|---|
| Whisper transcription | ~$0.001 (amortized, once per source) |
| Claude (segment + rewrite + image prompts) | ~$0.01 |
| ElevenLabs TTS | ~$0.03 |
| Flux Schnell images (10×) | ~$0.03 |
| ffmpeg compose | $0 |
| YouTube upload | $0 (10k units/day quota) |
| **Total** | **~$0.07 / Short** |

Throughput from one 40-minute source video → ~10 Shorts → **~$0.70**.

At 30 Shorts/day across three channels: **~$2.10/day**, **~$63/month**.

### Hybrid (cheapest autonomous mode)

Keep TTS and image-gen local, pay only for Claude:

| Item | Cost |
|---|---|
| Claude (segment + rewrite + image prompts) | ~$0.01 |
| Local TTS (Kokoro) + local images (MFLUX) | $0 |
| **Total** | **~$0.01 / Short** |

Tradeoff: ~3–5 min/Short wall-clock vs ~30s for fully-paid stack.

---

## 8. What Actually Drives Virality (constraints)

These are non-optional for Shorts performance:

1. **Hook in the first 1.5 seconds.** Most retention drop happens here.
   Claude's rewrite prompt must lead with the most curiosity-inducing line.
2. **Burned-in karaoke captions.** Non-negotiable. Most Shorts viewers watch
   muted; even those who don't get a retention boost from word-by-word
   highlighting.
3. **Style consistency per channel.** Same voice, same image style, same
   format. The algorithm uses this to classify and recommend.
4. **Vertical 9:16, full bleed.** No black bars, no letterbox.
5. **Length 10–20s** for v0. Watch-through rate is highest in this band;
   longer Shorts can work but only with proven hooks.
6. **End on a question or twist** to drive comments. Comments → algo boost.
7. **Volume matters but only after format is locked.** Don't scale to 10/day
   until 1/day is reliably watchable.

---

## 9. Decisions

### Resolved (this session)

1. **Local-first stack for v0.** No paid APIs. Kokoro TTS + MLX-SDXL-Turbo
   + mlx-whisper + ffmpeg. LLM work done by Claude Code (this CLI) during
   dev, dropped to JSON for the pipeline to consume.
2. **Image gen for v0**: MLX-SDXL-Turbo (~3 GB, fast). Upgrade to MFLUX
   (Flux schnell 4-bit, ~7 GB) in v0.5 if quality is the weak link.
3. **TTS**: Kokoro (`Kokoro-82M`). Piper as fallback.
4. **Original audio vs re-TTS** for Reddit-video sources: **re-TTS**. Hook
   pacing matters more than voice continuity.
5. **Source input strategy for v0**: hardcoded sample text first to prove
   stages 4–7 (rendering pipeline). Then plug in stages 1–3 (transcribe →
   segment → rewrite) once a Short renders.
6. **Disk budget**: ~5.5 GB minimum, ~10–11 GB if upgrading to MFLUX.

### Resolved (cont.)

7. **Image style** — locked to a **hand-drawn minimalist 2D chibi doodle**
   per user reference sketch (see §6). Distinctive, easy for small models,
   suits the storytelling tone.
8. **Character consistency** — v0 uses locked seed + prompt prefix;
   upgrade to IP-Adapter (v0.5) or LoRA (v1) only if drift becomes
   visibly distracting.

### Still open

1. **First channel niche** — pick one of A/B/C portfolios.
   *Recommendation: AITA Animated.*
2. **Background music** — yes/no for v0?
   *Recommendation: skip for v0, add in v1 from YouTube Audio Library.*
3. **Auto-upload vs manual review** for v0/v0.5.
   *Recommendation: manual review for first 50 shorts, then auto.*

---

## 10. v0 Scope

**Goal**: produce one watchable Short, end-to-end, with the local-only
stack and zero paid APIs.

### Two-phase v0

**Phase 1 — Render pipeline MVP (stages 4–7)**:
Hardcoded sample narration text → Kokoro TTS → mlx-whisper word
timestamps → MLX-SDXL-Turbo images → ffmpeg compose → one .mp4 in
`data/shorts/`. Proves the rendering chain works before investing in the
mining stages.

**Phase 2 — Source mining (stages 1–3)**:
Long-form source video → mlx-whisper transcribe → Claude Code segments
into stories interactively (drops `stories.json`) → rewrite into hook
scripts → feed into Phase 1's render pipeline. One source video → many
Shorts.

### In scope

- Stages 1–7 working with the local stack
- Single channel config (AITA Animated)
- ffmpeg composer with Ken Burns + crossfade + karaoke captions
- Local-only output to `data/shorts/`

### Out of scope for v0

- Stage 8 (auto-upload) — manual upload from `data/shorts/`
- Anthropic API (LLM work done by Claude Code in dev session)
- ElevenLabs / Replicate / fal.ai (paid TTS / image gen — v1 upgrade)
- Multi-channel orchestration
- Web UI / dashboard
- Trend-mining
- A/B title testing
- Background music (add in v1)

### Definition of done for v0

One command,

```
python make_shorts.py data/sources/some_aita_video.mp4 \
    --channel channels/aita_animated.yaml
```

produces N watchable .mp4 files in `data/shorts/`, each 10–20s, with
captions, narration, and animated images — using only local models.

---

## 11. Roadmap

- **v0** — Local-only stack (Kokoro + MLX-SDXL-Turbo + mlx-whisper +
  ffmpeg). One source, one channel, manual upload. LLM work via Claude
  Code in dev session. **$0/short.**
- **v0.5** — Upgrade image gen to MFLUX (Flux schnell). Auto-upload via
  YouTube Data API. Scheduling. Basic metrics dashboard. **(Metrics
  dashboard landed early — see "Telemetry" in `web/README.md` and
  the 📊 button in the web UI; per-stage durations + per-niche success
  rate + LLM token/cost rollup, all from the local JSONL log.)**
- **v1** — Add Anthropic API key to run unattended. Three channels in
  parallel (AITA / Wiki / TIH). Background music. A/B title generation.
  Optional: ElevenLabs / Replicate upgrades for quality/speed.
- **v1.5** — Sitcom clip pipeline (Family Guy etc.). New source adapter,
  same backbone.
- **v2** — Trend mining: daily auto-pull from Reddit/HN/X to drive sources.
- **v2.5** — AI video generation (Veo / Kling) replaces image slideshow for
  premium channels.
- **v3** — Self-tuning: feed performance data (CTR, retention) back into
  hook/style prompts.

---

## 12. Copyright / Ethics

| Source | Risk | Mitigation |
|---|---|---|
| Reddit (UGC) | Low | Attribute source thread in description |
| Reddit-compilation video | Low–Medium | Re-TTS (don't reuse audio); attribute original |
| Wikipedia | None | CC-BY-SA, attribute |
| Sitcom episodes | **High** (Content ID) | Treat as audience-growth play, not monetization |
| Podcasts | Medium–High | Get permission, or treat as growth play |
| AI-generated content | None | Disclose synthetic if it could mislead (esp. historical) |

General principle: **derivative work is fine if value-additive and
attributed**; pure rehosting is not.

---

## 13. Why This Works (and Where It Doesn't)

**Works because**:
- Shorts has lower production-quality bar than long-form YouTube
- Captions + clear narration + decent visuals = watchable
- Style-locked channels rank well algorithmically
- One source video → many Shorts is genuine leverage

**Plateau risks**:
- Pure "topic in / video out" without taste typically caps around 1–5K views
- Style fatigue if every Short looks identical (mitigation: subtle prompt
  variation within the locked style)
- Algorithmic suppression if YouTube classifies the content as low-effort AI
  slop (mitigation: keep narration tight, avoid generic stock-feeling visuals)

The differentiator over time is **hook quality + niche tightness**, not
production polish. A human-curated final pass on titles + first lines is
worth more than another rendering improvement.

---

## 14. Principles & Lessons (system rules)

These are concrete rules extracted from critiques of rendered Shorts.
Each rule should be **enforced by the pipeline**, not by the operator
remembering it on every authoring pass. When a rule fails (a rendered
Short shows the failure mode again), the fix lives in code or schema,
not in re-authoring that one Short.

**The process**: every Short rendered → reviewed (manually or by an
LLM critic) → any new failure mode added back to this list with its
mechanism. The list grows; the system learns.

| # | Principle | Mechanism (where in the system) |
|---|---|---|
| 1 | **Caption text and image content must transition together.** Misalignment ≥0.3s reads as a glitch. | `pipeline/compose.py` — caption overlay windows center on the xfade midpoint, not raw beat boundaries. |
| 2 | **Character is locked PER STORY** (not per channel) — the narrator must match the source story's voice. Per-beat prompts describe SCENE only. | `pipeline/cast.py` authors a per-story `cast.json` from the source story + channel aesthetic; `make_shorts.py` reads `narrator.description` and prepends it to every per-beat prompt. Channel YAML keeps style only. Channel-level `character_description` is the backwards-compat fallback. |
| 3 | **Diffusion can't render legible text.** Any prompt asking for visible text → AI gibberish. | `pipeline/images.py` — prompt linter (`_TEXT_BAIT`) strips/warns on `{label, sign, text, writing, letters, logo, brand, signage, dollar sign, words}` PLUS prop categories that almost always render with garbled inscribed text (`invitation, certificate, diploma, contract, prescription, receipt, greeting card, business card, boarding pass, ticket`). A second linter (`_PROP_TEXT_PATTERN_RE`) catches inscribed-text patterns: `X reading Y`, `X that says Y`, `X displaying Y`. The upstream `pipeline/prompts.py` system prompt also lists these as forbidden so the LLM author doesn't emit them. Originated from the wedding-Short critique where an "envelope reading WEDDING INVITATION" rendered as "wedding TO invitating". |
| 4 | **An AITA Short without a closing CTA is algorithmic suicide.** Narration must end on a vote-prompt. | `pipeline/script_check.py` — last sentence must contain `"?"`, `AITA`, `WIBTA`, or "comment". Validates Scripts before render. |
| 5 | **Hook in 1.5s + inciting wedge before 3s.** Anything between the hook and the wedge is filler that costs retention. | `pipeline/script_check.py` — beat 0 must contain a question/claim; beat 1 or 2 must contain a number or specific noun. |
| 6 | **Visible motion is non-optional on Shorts; no frame may be pixel-identical to the previous one.** Subtle Ken Burns reads as a static slideshow; FROZEN frames (≥1s identical pixels) crater retention even when the audio is good. | `pipeline/compose.py:_kenburns_filter` — initial punch-in 1.0→1.12 over `PUNCH_FRAMES` to land the cut, then a slow continued drift 1.12→1.16 across the rest of the clip. No two frames in any beat share pixel content. Originated from the wedding-Short critique where beats 0/1/2/5 each held a single still frame for 2–4 identical seconds. |
| 7 | **First image can't lean on diffusion defaults.** They converge to "shocked anime girl" cliché. | Channel YAML `opening_image_directives` — beat 0 prompt must contain ≥2 concrete tokens (specific room/prop/posture). Validated by prompt builder. |
| 8 | **One beat per sentence (or two short sentences merged), never mid-clause.** Breaking before "and"/"but" produces dangling captions; sentence-level granularity also gives denser visuals (more images per Short) and shorter on-screen captions. | `pipeline/beats.py:split_into_beats` — three-pass split: (1) cut at every sentence terminator `.!?`; (2) split a sentence only if it exceeds `max_s` AND has a clause break (`,;:`) — never cut mid-clause; (3) merge adjacent short groups (<`min_s`) but cap at 2 sentences per beat. Defaults `target_s=1.8 / max_s=2.8 / min_s=1.0`. Long sentence-without-comma stays whole — the script-quality fix lives upstream in `pipeline/rewrite.py` (Principle #30). |
| 9 | **Source-text alignment never silently drops source words.** Gaps mean captions go missing (we lost "four hundred dollars" in aita02). | `pipeline/align.py` — when a source word has no Whisper match, INSERT with timestamp interpolated from neighbors. |
| 10 | **Critique is a system input, not a one-off note.** Every Short reviewed feeds back into this list. | Process: this section grows over time. New failure modes added with their mechanism, never just patched on the offending Short. |
| 11 | **Diffusion drops later/longer prompt tokens.** Attention dilutes past ~50 scene tokens, and Lightning at 4 steps amplifies the effect — the visual punchline gets lost. Author the must-show element FIRST and short. | Prompts schema: per-beat prompts are `{key_visual, scene}` objects. Builder emits `(key_visual:1.4), scene` so attention concentrates on the punchline. Linter warns when `scene` exceeds ~50 tokens. |
| 12 | **One main subject + one secondary action per beat.** "Three friends each cutting into steak" gives 2 friends, no steak. Multi-subject scenes are unreliable below Flux quality. | `pipeline/images.py` — linter flags plural-subject patterns (`three|four|five|several|multiple` + `friends/people/figures/characters`). The fix is to split: one beat shows the friends, another shows the steak. |
| 13 | **Visual punchlines must be weighted, not just present.** Specific items dropped because they competed with secondary detail (e.g. "twenty-dollar bill on table" lost to "holding coat, head turned"). | The prompt builder always emits `key_visual` with explicit attention weight (e.g. `(twenty-dollar bill on table:1.4)`) ahead of secondary detail. |
| 14 | **Character identity at this scale needs IP-Adapter, not just verbal descriptions.** Locked seed + "round-headed character with brown hair" yields ~70% consistency at best across different scenes. Image-conditioned anchoring is required for higher. | `pipeline/images.py` — wire IP-Adapter (h94/IP-Adapter SDXL, ~700 MB). Reference is `cfg["character_reference_image"]` if set; otherwise the orchestrator auto-bootstraps by generating beat 0 without IP-Adapter and using `img_00.png` as the reference for beats 1..N. Scale 0.5–0.7 keeps character recognisable while letting scenes vary. |
| 15 | **AITA closer must be the LIKE-if-YTA / COMMENT-if-NTA split.** Vague "vote in comments" closers underperform — viewers need a binary action prompt. | Channel YAML `closer_format` string; `pipeline/script_check.py` errors when AITA-class channels (those with `closer_format`) don't end on the split pattern; `pipeline/captions.py:render_closer_panel` derives the on-screen panel from the same string. |
| 16 | **Closer panel must not overlap the character body.** A dark panel across the torso reads as "the character changed clothes" and breaks identity. | `pipeline/compose.py` — closer-panel overlay is positioned in the TOP quarter of the frame (`closer_y = 320`), not over the character. |
| 17 | **Captions and closer panel must not duplicate text.** When the panel says "LIKE if YTA / COMMENT if NTA" and the spoken caption also says it, the screen has competing text. | `pipeline/compose.py:_beat_overlaps_closer` suppresses the spoken caption on beats whose substantive words mostly overlap the panel. Heuristic: ≥50% word overlap → no caption that beat. |
| 18 | **Narrator emotional tone must propagate per-beat.** Without it, every beat defaults to a neutral smile, which mismatches conflict-heavy narration. | `pipeline/cast.py` outputs `narrator.default_emotion`; `pipeline/prompts.py` instructs the LLM to vary expression per beat to match what the narrator is *saying right now* — never default-smile. |
| 19 | **Quality-gate every generated image before it ships.** Flux/SDXL can produce all-black, ghost-doubled, or low-edge-density frames; no post-gen check means they reach compose. | `pipeline/quality_gate.py` checks file size, dimensions, channel std-dev, and Sobel edge density. `make_shorts.py` retries with bumped seed up to 2× per beat. |
| 20 | **Source visualizability filters at pull-time.** Stories that are mostly internal monologue or dialogue are unrenderable; they waste TTS + Flux cycles. | `pipeline/visualizability.py` scores raw stories on concrete-noun count, action-verb count, length, dialogue share. `pull_stories.py` drops stories below the per-channel threshold (default 0.4). |
| 21 | **Critic returns class-of-bug fixes, not just per-beat patches.** A one-off prompt edit fixes one video; an `system_correction` fixes the next 100. | `pipeline/critic.py` requires every issue be classified ONE-OFF vs CLASS-OF-BUG. Class fixes are surfaced as `system_corrections` with file:function targets. Memory: `feedback_engineer_class_of_bug.md`. |
| 22 | **Critique runs through every lens, every render — not just "does it look ok".** A single-axis "is the hook strong" question misses sync, continuity, mute-mode, thumbnail, comments-bait, etc. | `pipeline/critic.py` instructs the LLM through 15 explicit lenses (L1–L15: hook, A/V sync, character continuity, pacing, captions, AI-glitch, composition, palette, mute mode, thumbnail, arc, CTA, source fidelity, re-watch, comments-bait). Per-frame findings are emitted with `lenses[]` so post-hoc analytics can group failures by lens. The `/critique-video` skill mirrors the same lens framework for human-readable mode. |
| 23 | **Auto-critique is the default, never opt-in.** Skipping the critic means a Short ships without a feedback signal, which means the pipeline doesn't get smarter. | `make_shorts.py` defaults `run_critic=True`. The `--no-critic` flag is reserved for fast iteration on the renderer itself; production runs and skill-driven runs (`/make-script`, `/make-movie-short`) MUST not set it. The make-movie-short SKILL.md explicitly forbids it. |
| 24 | **Narrator demographic is inferred from script relationship markers, not the channel default.** Channel `character_description` is age/gender-ambiguous on purpose ("round-headed character") so it can host any narrator — but the renderer was silently taking that default for every story, producing boy-cartoons voicing grandmothers. | `make_shorts.py` auto-invokes `pipeline/cast.py:author_cast` when no `data/intermediate/<channel>/cast/<slug>.json` exists, using the raw source story. `cast.py`'s prompt has explicit relationship-marker → demographic mapping (DIL/MIL → middle-aged grandparent in cardigan, "my toddler" → young-adult parent, "my husband" → adult, etc.). When two markers conflict the OLDER demographic wins. The skill (`/make-movie-short`) MUST NOT hand-write the cast file — it must invoke `author_cast` so the LLM does the inference. Originated from critique `aita-birth-pool`. |
| 25 | **Caption canvas height is a function of line count; the overlay y-position is computed from the actual PNG height with a fixed bottom safe-zone.** Long captions (3+ lines) used to overflow the fixed 320px PNG and clip at the frame edge ("birth?" cut off the hook on aita-birth-pool). | `pipeline/captions.py:render_beat_caption` sets `canvas_h = max(default, padding*2 + lines*line_h + 20)` so the PNG always fits the wrapped text. `pipeline/compose.py` reads each caption PNG's actual height before placing it: `y_pos = HEIGHT - cap_h - 240` (240px reserved for YouTube Shorts UI: progress bar, channel chip, like/comment buttons). |
| 26 | **The shotlist drives the beat splitter, not the other way around.** Index-based zip of prompts-to-beats slides off-by-one any time `beats.py` glues two short sentences into one beat or splits a clause where the shotlist didn't expect a cut. Wrong image lands on the wrong narration line; "I told her no" loses its dedicated visual. | `pipeline/beats.py:split_into_beats` accepts `forced_narration_lines` (sourced from `data/intermediate/<channel>/shotlist/<slug>.json`). When provided, it delegates to `split_with_forced_boundaries`, which finds each line as a contiguous word subsequence in the TTS-aligned timeline and forces a beat boundary at the end. Beats become 1:1 with shots by construction. As a safety net, `pipeline/images.py:load_prompts` accepts `beat_texts` and uses Jaccard token-overlap to bind each beat to its best-matching prompt by `narration_line` anchor (Phase-1) — survives splitter drift even when force-split isn't used. The skill (`/make-movie-short`) MUST emit a `narration_line` field on every entry in `prompts.json` AND on every shot in `shotlist/<slug>.json`. Originated from critique `aita-birth-pool` v2 — off-by-one slide on beats 6→8. |
| 27 | **Beat transitions route through black, never content-to-content cross-dissolve.** Two independently-generated AI images alpha-blended mid-transition produce a melted-face / ghost-double-exposure frame — viewers screenshot it. | `pipeline/compose.py` — `xfade` uses `transition=fadeblack` (not `fade`) in BOTH `compose()` and `compose_clips()`; `XFADE` reduced to 0.18s so the black flash is brief but content frames never overlap. Originated from the wedding-Short critique where t_03 and t_13 showed visible double-exposure between adjacent AI-generated beat images. |
| 28 | **Closer-panel CTA language must not appear in narration captions.** When `LIKE if YTA` leaks into a non-closer beat caption AND the panel is also up, the screen has competing CTAs in two styles — muted viewers get jarring duplicate calls to action. | `pipeline/script_check.py` — `_CTA_LEAK_RE` scans every beat except the last (closer is positionally always the last beat) for `{LIKE if, COMMENT if, YTA, NTA}`. Error severity on AITA-class channels (`closer_format` set), warning otherwise. Originated from the wedding-Short critique where beat 4's caption read "The wedding's a year away. LIKE if YTA,". |
| 29 | **PRIMARY SUBJECT IN FRAME: the visual agent must match the grammatical subject of the spoken beat.** When narration says "my three kids are burnt out caregiving" and the image shows the dad embracing grandma, muted viewers get the inverted story — this is the single most damaging audio-visual sync defect. | `pipeline/prompts.py:_extract_subject` — heuristic regex extracts the head NP of each beat (pronoun, possessive-NP with optional count and adjectives, definite NP), trimmed of trailing verbs/adverbs and conjunction tails. The extracted subject is injected into the per-beat prompt as `[PRIMARY SUBJECT IN FRAME: <subj>]` and a system rule (#9 in `_SYSTEM`) binds the LLM author to use exactly that subject as the visual protagonist; counts must be honoured (`my three kids` → exactly three young adults visible, dad excluded). Originated from the wedding-Short critique — the critic flagged this as the highest-leverage single change. |
| 30 | **Narration shape: 110–160 words, 10–15 standalone sentences, 22–32s spoken — and SPICY.** Sentence count IS image count (one beat per sentence per Principle #8), so the rewrite-stage word target directly caps how many images a Short can carry. The earlier 50–80 word ceiling produced only 5–7 beats — a flat slideshow. Long run-on narration ("I refused, and then she yelled, and my mom called") also collapses two beats into one cluttered caption AND one image. Spicy = named antagonist with relationship, specific numbers/dates, an escalation curve, one unmistakable villain action, a "wait what" pivot mid-narration, and stated stakes — without these the closer feels academic and comments don't land. | `pipeline/rewrite.py:_BASE_PROMPT` — explicit length block (110–160 words, 10–15 sentences) + spicy block (named antagonist, specific stakes, escalation, pivot) + subtitle-friendly block (5–12 words/sentence, hard cap 14, periods not commas, banned filler words, no semicolons/em-dashes, one concrete visual per sentence) + a self-check directive instructing the LLM to recount sentences before returning. Pairs with Principle #8 (sentence-level splitter) so 1 sentence → 1 beat → 1 image at the floor. |
| 31 | **The critic emits schema-constrained output.** Free-form prose padding inflates output tokens (the dominant cost/latency on the auto-critique stage) without adding actionable signal. | `pipeline/critic.py` — `_CRITIC_SCHEMA` is passed as `--json-schema` to the claude CLI. Caps every free-form string with `maxLength` and bounds `per_frame_findings`/`top_issues`/`system_corrections` with `maxItems`. Vision-tool access (`Read`, `Bash` over the sampled frames) is preserved; only the final emit is constrained. Saves output tokens on the dominant-cost stage (~$0.30 / ~4 min wall on observed runs) without losing per-frame detail. |
| 32 | **Pull a candidate POOL, not the top-of-day single.** Reddit's top-of-day rotates slowly; pulling `--limit 1` from `top/day` returns the same post for hours, so every click within a 24h window re-renders the same Short. The first-pulled story is also rarely the spiciest of the day. The fix is a pool of N candidates, deduped by `metadata.post_id` against already-rendered stories, then heuristically scored for drama and emitted top-1. | `sources/drama.py:score_drama` — heuristic regex score on (named antagonists with relationship+name, conflict verbs, escalation markers, "wait what" pivots, dollar/numeric stakes, named events with social stakes, narrator-stated stakes) plus log-scaled Reddit upvotes/comments. `pull_stories.py` adds (a) a skip-seen guard that scans `data/intermediate/<channel>/raw/*.json` for `metadata.post_id` and drops repeats, (b) a `--pick {all,best}` flag that drama-scores survivors and emits only the top one when `best`. `web/server.py` calls `pull_stories.py reddit --listing top --timeframe week --limit 15 --pick best`. The web flow stays one-Short-per-click, but each click now picks the spiciest of 15 fresh weekly candidates rather than yesterday's one stale top-of-day. |
| 33 | **TTS-input must be number-normalised before synth.** Kokoro reads "$2000" as "two zero zero zero" and even bare "$400" as digit-by-digit — gibberish. The same normalised text must also feed source-text alignment (otherwise the source has "$2000" but Whisper transcribed "two thousand dollars" and alignment falls apart, breaking caption sync). | `pipeline/audio.py:normalize_for_tts` — pure-Python expander (no num2words dep) that converts currency (`$X`, `$X,XXX`, `$X.YY`, `$XK`, `$X.XM`) and bare integers up to 999,999,999 to spelled-out English. Word-boundary lookarounds skip phone numbers (`555-1234`) and timestamps (`10:30`). Idempotent. `synthesize()` runs it inside, and `make_shorts.py` runs it on the `text` variable BEFORE both TTS and `align.align_source_to_whisper` so beat captions, audio, and ASR transcripts all agree. The rewrite prompt also asks for spelled-out numbers as defence in depth — even if the normaliser has a gap, the LLM doesn't emit numerals. |
| 34 | **The narrator voice is flat — the WORDS have to do prosody.** Kokoro (and most neural TTS at this tier) produces near-monotone delivery. Sentence rhythm and punctuation are the only levers for engagement. A narration that's 12 sentences all 8 words long sounds robotic regardless of voice. | `pipeline/rewrite.py:_BASE_PROMPT` PROSODY block: (a) varies sentence length (long set-up → short punch → long context → short reveal); (b) allows one-word / two-word sentences for dramatic landings on the pivot and kicker, capped at ~2 per Short; (c) one ellipsis (...) per Short on the pivot reveal; (d) em-dashes only BETWEEN sentences (interruption beat), never within; (e) mid-narration question marks after reveals to invite the viewer to think before the answer; (f) numbers ALWAYS spelled out (Principle #33 backstop). The pre-return self-check directive instructs the LLM to re-read aloud and rewrite if rhythm doesn't vary. |
| 35 | **Acronyms must be EXPANDED to natural phrases for TTS, not letter-spelled or left for the model to phoneticise.** Kokoro renders bare "AITA" as "Aira", "YTA" as "Zira", "NTA" as "Antigua" — phonetic-guess garbage. Letter-spacing ("A I T A") works mechanically but sounds robotic. The correct human-narrator behaviour is to SAY THE WORDS the acronym stands for. | `pipeline/audio.py:normalize_for_tts` — `_ACRONYM_PHRASES` dict maps each AITA-class abbreviation to its natural phrase (`AITA` → "am I the asshole", `YTA` → "you're the asshole", `MIL` → "mother in law", etc.) and general initialisms like `ER`/`USA`/`UK` to their letter-spaced form. Captions are derived downstream from Whisper, so the spoken phrase becomes the caption — that's what was actually said, which is correct. The visual closer panel still uses the original acronyms via `compose.py:render_closer_panel` from `cfg["closer_format"]`, so the panel reads "LIKE if YTA / COMMENT if NTA / AITA?" regardless of audio normalisation. |
| 36 | **Verbal CTA must sound human, not like a YouTube preset.** Real Shorts narrators DO end with a spoken call-to-action — but they say it conversationally ("Am I the asshole? Tell me in the comments." / "What would you have done?"). They DON'T read off the YouTube checklist ("LIKE if YTA, COMMENT if NTA. AITA?"). The robotic preset format breaks the storytelling spell — that's the actual bug. The verbal closer should be 1–2 sentences in natural language. The visual LIKE/COMMENT panel renders alongside in parallel for muted viewers. | `pipeline/rewrite.py:_closer_block` lists GOOD examples (natural human-storyteller endings) and BAD examples (robotic preset language) the LLM must avoid. `pipeline/script_check.py` no longer requires the panel anchor tokens (`like`/`yta`/`comment`/`nta`) in the narration tail — the question-mark check from `_CTA_RE` is sufficient. The visual panel is still rendered independently by `compose.py:render_closer_panel` from `cfg["closer_format"]` for the closer beat + tail-hold seconds, so the like/comment split keeps appearing on screen for muted viewers regardless of what the narrator chooses to say. The audio critic's L6 lens flags any future regression to robotic preset phrasing. |
| 37 | **Critic findings feed back into the critic itself (regression-proof learning loop).** When the audio critic surfaces a `system_correction` and we ship a code fix for it, that issue MUST be added back to the critic's evaluation lenses BEFORE the task is closed. Otherwise the next render's critic has no memory that the issue was ever a problem and can't catch a regression. The lens list is the pipeline's persistent memory — every shipped fix grows it. Same pattern as Principle #10 for video critic, made operational here. | `pipeline/audio_critic.py:_CRITIC_PROMPT` — the EVALUATION LENSES block is intentionally append-only. New lenses (L7 acronym expansion, L8 euphemism handling, L9 all-caps leakage, L10 binding integrity, L11 trailing-Whisper-repetition, L12 quoted-dialogue framing) were added at the same commit as the corresponding `audio.py`/`asr.py`/`rewrite.py` fixes. The critic's prompt instructs the LLM to propose new lens text in `system_corrections.principle` so future findings auto-suggest their own regression coverage. Process rule: any PR that fixes an audio bug surfaced by the critic MUST also patch this lens list. |
| 38 | **Whisper hallucinates same-token runs on trailing silence.** Real failure observed: 13× `"that"` appended after a clean closing line, because the audio faded into 200 ms of silence and Whisper looped on its language-model's most likely next-token. Without a guard the artifact poisons captions, breaks source-vs-transcript alignment ratios, and makes binding-integrity checks unreliable. | Two-pronged defense, both applied: `pipeline/asr.py:_strip_trailing_repetition` post-processes every transcribe result and drops trailing runs of ≥4 identical normalised tokens (preserves legitimate doubles like "no, no!"). `pipeline/audio.py:_trim_trailing_silence` shaves the trailing silence off the synthesized WAV before Whisper sees it (≤-45 dB threshold, ~50 ms cushion to avoid clicks) so there's nothing to hallucinate over in the first place. Critic lens L11 verifies on every render. |
| 39 | **Quoted dialogue must be framed with a speaker tag — TTS does not voice-act.** A bare quoted line `"Don't feel good. Gotta bail."` reads identically to narration in the audio because Kokoro/F5-TTS use the same voice for both. The listener has no way to know they're hearing dialogue. The fix is text-side: speaker tag before the quote with a comma to trigger a prosodic beat, OR paraphrase as narration. | `pipeline/rewrite.py:_BASE_PROMPT` — explicit rules: keep quoted lines short (3–7 words, ≤10 max); tag the speaker before the quote with a comma (`She texted, "My back hurts."`); prefer paraphrase (`She called me selfish.`) over speech-act when natural. Critic lens L12 flags any bare-quote regression and proposes the rewrite. |
| 40 | **Compose is the contract: the compose stage owns truth, not the cache.** Multi-second caption drift was the most-reported sync bug across renders. Root cause: `caption_NN.png` files cached on disk across runs, never invalidated when `beats.json` was re-split. caption[i] held text from old-beat[i] but was overlaid at new-beat[i]'s timing → drift growing with index. The patch-level fix (content-hash the cache) treats the symptom; the architectural fix is that compose **does not trust** any cached per-beat artefact — it wipes and regenerates. | `pipeline/compose.py:_wipe_stale_per_beat_artefacts` runs at the top of every `compose()` call, deleting all `caption_*.png` and `word_*.png` plus any `img_NN.png` where NN ≥ `len(beats)`. The `if not cp.exists()` cache guard on caption render is removed — captions are always re-rendered fresh from the current `beats.json`. Pre-flight `assert len(image_paths) == len(beats)` fails loud. Default render path is `caption_mode="word"` (per-word karaoke captions, see Principle #42), which makes residual sync slips invisible at the word grain anyway. The PNG cache layer that produced the bug is gone. Critic lens L14 verifies the contract on every render. **Streaming-compose extension (2026-05):** the wipe was extracted as `compose.wipe_stale_per_beat_artefacts(cache_dir, n_beats)` and is called from `make_shorts.py` BEFORE the image stage starts. A background thread then runs `compose.prerender_word_captions()` in parallel with image gen, populating `word_NNNN.png` files that the compose call later finds on disk. `_compose_with_word_captions` skips render for files already present (idempotent on file presence). The "compose owns truth" invariant holds — the wipe still happens before any cached artefact is read — but ~7-15 s of CPU caption work is lifted out of the critical path. Pre-flight `len(image_paths) == len(beats)` is now upgraded to a **disk presence check** (`make_shorts.py:1245-`): if any `img_NN.png` is missing the job fails fast with a structured error instead of letting ffmpeg crash with `Error opening input file`. |
| 41 | **IP-Adapter character lock is for narrator-led beats only.** When a beat depicts a non-narrator subject (the antagonist, the couple, the MIL), forcing the narrator's `img_00` reference produces Frankenstein composites — narrator's face on the antagonist, floating-head over a multi-character scene. The `[PRIMARY SUBJECT IN FRAME: <subj>]` annotation that `pipeline/prompts.py:_extract_subject` injects already tells us which beats are narrator-led. | `make_shorts.py` — IP-Adapter ref decision branches on the beat's subject: if `subj` ∈ {`I`/`we`/`my [partner|family|kids]`/`our X`}, keep the narrator reference; otherwise pass `ip_adapter_image=None`. Antagonist beats render cleanly without narrator-bleed. Multi-character beats fall back to richer scene descriptions (cast.json's `supporting:[]` array). |
| 42 | **Per-word karaoke captions in centre frame, not per-beat blocks at the bottom.** Strongest single retention lever for short-form captions per OpusClip + Submagic 2026 benchmarks. Per-word display means a 200 ms timing slip is imperceptible; the multi-second drift class that ruined renders becomes invisible even when the underlying timing isn't perfect. Modern Shorts/TikTok caption look. | `pipeline/captions.py:render_word_caption` produces a tight transparent PNG of one word with thick stroke + drop shadow (no background bar). `pipeline/compose.py:_compose_with_word_captions` overlays each word at `y = HEIGHT * 0.45` with `enable='between(t, word.start, word.end)'`. Word-level timestamps already exist in `beats[i].words` (Whisper-derived). Closer-panel-overlap suppression preserved (drops words whose lowercase token ∈ panel content tokens). Default `caption_mode` is `"word"`; `"beat"` mode kept as legacy. |
| 43 | **Per-paragraph speed modulation simulates prosody on flat TTS.** Kokoro produces flat-prosody audio at any single speed; the listener hears every paragraph at the same tempo and the narration reads as monotone regardless of voice. We synthesise each paragraph as its own Kokoro call already (per-paragraph pause stitching for breath points), so the speed knob slots in cleanly: hook reads slightly slower for clarity, closer reads slower for impact, exclamation-heavy paragraphs read faster, ellipsis-trailing paragraphs read slower for the hanging-beat effect. Net tempo spread ~14% across a typical narration — enough to be audibly varied without sounding cartoonish. Cheapest available form of "voice modulation" without changing models. | `pipeline/audio.py:_modulate_paragraph_speed` — heuristic per-paragraph speed multiplier driven by punctuation patterns and structural position. Channel YAML's `tts_modulation:` block overrides individual factors (`hook_speed_factor`, `closer_speed_factor`, `excited_speed_factor`, `hanging_speed_factor`, `thoughtful_speed_factor`); pass `enabled: false` to disable. Voice fingerprint (`make_shorts.py:_voice_fingerprint`) includes the modulation block so changing a factor invalidates `narration.wav` and a new tempo curve renders on the next click. Critic lens L15 detects flat-tempo regression by checking per-paragraph WPM spread <5% as a class-of-bug signal. |

### Implementation tiers

- **Tier A** (pure code fixes, broad benefit): #1, #6, #8, #9, #16, #17, #27, #38, #40, #41, #42, #43.
- **Tier B** (schema/config + validators): #2, #3, #4, #5, #7, #11, #12, #13, #15, #18, #19, #20, #23, #24, #25, #26, #28, #29, #30, #31, #32, #33, #34, #35, #36, #39.
- **Tier C** (process/docs): #10, #21, #22, #37.
- **Tier D** (model/runtime upgrades — heavier lift): #14.

When future critiques arrive, classify each piece of feedback:
1. Is this a *one-off authoring miss* (e.g. a typo in a particular
   script)? — fix the artifact only.
2. Is this a *class of bug* the pipeline could let through again? —
   add a new rule here, fix it in code/schema, never re-fix on a
   single Short.
