---
name: clone-video-format
description: Take a video URL (YouTube / Shorts / TikTok / Reels / X / raw mp4), download a low-res sample, study its DNA — hook, pacing, caption style, voice profile, beat structure, palette, transitions, closer, source-content type — then auto-pick the closest existing ytFactory channel and hand the format-fingerprint JSON to /make-skill so a new /<trigger> command gets authored that reproduces the format. Use when the user says "clone this video", "make a skill that copies this format", "reverse-engineer this short", "I want our pipeline to produce videos like <link>", or pastes any video URL with intent to replicate. Produces data/format_clones/<slug>/fingerprint.json then invokes /make-skill. For critiquing one of OUR own videos use /critique-video. For authoring a script in an EXISTING format use the relevant /make-* skill directly. For pure channel scaffolding use /create-youtube-channel.
learnings_consulted:
  - .claude/skills/clone-video-format/learnings/_index.md
  - .claude/skills/make-skill/learnings/heuristics.md
  - .claude/skills/critique-video/SKILL.md
---

# /clone-video-format — reverse-engineer a video into a working ytFactory skill

> **Cross-cutting skill — not a niche producer.** This skill does not
> author channel narrations or modify per-channel state via the
> website state API. The NicheVideo + state-client contract in
> CLAUDE.md applies only if this skill incidentally needs to read
> channel state as context.

You become **forensic-editor + showrunner-architect**. Given any video
URL, you watch it the way a senior YouTube editor watches a competitor's
Short, distill its DNA into a structured fingerprint, match it to the
closest existing ytFactory channel, then hand the fingerprint to
`/make-skill` so the meta-skill authors a new `/<trigger>` command
that can produce videos in that style going forward.

This skill **does not render anything itself**. It produces
`data/format_clones/<slug>/fingerprint.json` and chains into
`/make-skill`. The eventual output of the chain is a new SKILL.md
under `.claude/skills/<trigger>/`.

## How to run it

### 1. Confirm the URL and sample budget

Take the URL the user pasted. Detect the platform from the host:

| host pattern | platform |
|---|---|
| `youtube.com`, `youtu.be`, `youtube.com/shorts/` | YouTube |
| `tiktok.com`, `vm.tiktok.com` | TikTok |
| `instagram.com/reel/`, `instagram.com/p/` | Instagram Reels |
| `x.com/`, `twitter.com/` | X video |
| `*.mp4`, `file://`, absolute path | raw video |

Probe duration before downloading (cheap metadata-only call):

```bash
.venv/bin/python -m yt_dlp --no-warnings --skip-download \
  --print "%(duration)s|%(width)s|%(height)s|%(fps)s|%(title)s" "<URL>"
```

For raw-path inputs, use `ffprobe -v error -show_entries
format=duration:stream=width,height,r_frame_rate -of default=nw=1
"<path>"`.

Decide the sample budget:

- **≤ 90s (Shorts / Reels / TikTok)** → download the whole thing at
  480p max.
- **90s–15 min (mid-form)** → download whole thing at 360p. Even at
  the 15-min boundary this is ~50 MB and downloads in seconds.
- **> 15 min (true long-form / sleep history / 30-min Top-10)** →
  fetch whole at 360p (recommended; ~120 MB at 30 min, still fast),
  or run yt-dlp **three separate times** with one
  `--download-sections` per call and concat with ffmpeg. **DO NOT
  stack multiple `--download-sections` flags in a single yt-dlp
  invocation** — they overwrite each other and only the LAST one
  applies (silently). Surfaced 2026-05-08; full note at
  [`learnings/yt_dlp_section_overwrite.md`](learnings/yt_dlp_section_overwrite.md).

State the URL, detected platform, duration, aspect, and the sample
plan in **one line** before downloading. No further user input
needed for this stage.

### 2. Download and slug-out

Pick a slug from the URL (last path segment, sluggified). Output
dir: `data/format_clones/<slug>/`.

```bash
mkdir -p data/format_clones/<slug>

# Default for ≤15 min sources (Shorts / mid-form / short long-form):
# fetch whole at 360p — simpler and faster than windowing.
.venv/bin/python -m yt_dlp \
  -f "bestvideo[height<=360]+bestaudio/best[height<=360]" \
  --merge-output-format mp4 \
  -o "data/format_clones/<slug>/source.mp4" "<URL>"

# Only for >15 min sources where windowing is genuinely worth it:
# run three SEPARATE yt-dlp invocations and concat. NEVER stack
# multiple --download-sections flags in one call (they overwrite,
# silently — see learnings/yt_dlp_section_overwrite.md).
# .venv/bin/python -m yt_dlp ... --download-sections "*0-60" -o "win1.mp4" "<URL>"
# .venv/bin/python -m yt_dlp ... --download-sections "*<M1>-<M2>" -o "win2.mp4" "<URL>"
# .venv/bin/python -m yt_dlp ... --download-sections "*<L>-inf" -o "win3.mp4" "<URL>"
# (then ffmpeg concat into source.mp4)

# Raw-mp4 path → just symlink
ln -sf "<absolute-path>" "data/format_clones/<slug>/source.mp4"
```

If yt-dlp returns a "Sign in to confirm you're not a bot" or a
geoblock, DO NOT silently swallow — surface it to the user with the
exact error and stop. (Heuristic 50.)

Pull thumbnail + title + description for context (YouTube only):

```bash
.venv/bin/python -m yt_dlp --skip-download --write-thumbnail \
  --write-info-json -o "%(title)s.%(ext)s" "<URL>"
```

### 3. Sample frames the same way /critique-video does

This is the **same pattern** as `/critique-video` (see
`.claude/skills/critique-video/SKILL.md` step 3) — do not fork it.
If we add a third caller, extract to `pipeline/video_sample.py` as
a separate refactor PR (heuristic 51 surfaced finding).

```bash
mkdir -p data/format_clones/<slug>/frames

# 1 fps base + dense hook coverage
ffmpeg -y -i source.mp4 -vf fps=1 -q:v 2 frames/t_%03d.png
ffmpeg -y -ss 0.3 -i source.mp4 -frames:v 1 -q:v 2 frames/t_hook_03.png
ffmpeg -y -ss 0.8 -i source.mp4 -frames:v 1 -q:v 2 frames/t_hook_08.png
ffmpeg -y -ss 1.5 -i source.mp4 -frames:v 1 -q:v 2 frames/t_hook_15.png

# Audio extraction for transcript + WPM
ffmpeg -y -i source.mp4 -vn -ac 1 -ar 16000 audio.wav
```

Then `view` every PNG in numeric order. You're looking for:

- **Hook (0–1.5s)**: title overlay? talking head? cold cut to action?
- **Visual style**: real footage / AI image / 2D animation /
  split-screen (gameplay strip + content) / Reddit cards / talking
  head / kinetic typography / slideshow / etc.
- **Aspect**: 9:16, 1:1, 16:9, 4:5.
- **Caption treatment**: none / burned-in / animated word-by-word /
  Reddit-style bubble cards / TikTok-style colored chunks /
  subtitled bottom-third.
- **Palette + lighting**: dominant colors, contrast level,
  filter/grade, dark-mode vs light.
- **Transitions**: hard cut / whip / zoom-punch / crossfade / smash.
- **On-screen text style**: font weight, capitalization, color,
  position, animation.
- **Thumbnail style** (if YouTube): face / text-overlay / split /
  none.

### 4. Transcribe + measure pacing

Use the cached whisper-mlx (heuristic 19/20 — reuse caches, never
re-download model weights). Whisper writes word-level timestamps.

**IMPORTANT — `python -m mlx_whisper.transcribe` is broken
(silent-exit, RuntimeWarning, no output).** Use the Python API
directly. Full note:
[`docs/whisper_mlx_cli_bug.md`](../../../docs/whisper_mlx_cli_bug.md) +
skill mirror at
[`learnings/mlx_whisper_cli_python_api.md`](learnings/mlx_whisper_cli_python_api.md).

```python
# Inline-script via .venv/bin/python -c '...' or a tmp helper:
import mlx_whisper, json
result = mlx_whisper.transcribe(
    'audio.wav',
    path_or_hf_repo='mlx-community/whisper-medium-mlx',
    word_timestamps=True,
)
json.dump(result, open('audio.json', 'w'))
```

(If `mlx_whisper` isn't on the env, fall back to `openai-whisper`
or `whisper.cpp` via `pipeline/audio.py:transcribe()` — do NOT call
the OpenAI API.)

From the transcript JSON compute:

- **WPM** over the whole clip (ignore first/last 2s).
- **Speech-to-silence ratio** (silence = gaps > 250ms).
- **Hook line** (every word in t < 1.5s).
- **Closer line** (every word in t > duration − 4s).
- **Beat boundaries** — silence gaps > 600ms OR shot-cut alignment
  (cross-reference with frame deltas; an abrupt frame change
  inside speech often marks a beat boundary).
- **Voice profile** — gender / age band / accent (US / UK / Indian
  English / Hindi / etc.) / style (calm narrator / hype VO /
  ASMR / character voice). Note if it sounds AI-generated
  (Eleven / TTS artefacts) vs human.

### 5. Build the format fingerprint

Send all of the above (frames + transcript + metadata) to `claude
-p` via `pipeline/llm.py` (heuristic 22 — never the Anthropic SDK
directly). Use this prompt skeleton:

```
You are a forensic video-format analyst. Given a sampled video
(frames + transcript + metadata), output a JSON fingerprint matching
the schema below. Be specific. Refuse to guess; mark unknowns as null.

REQUIRED SCHEMA (every key must be present, null OK):
{
  "source_url": "<original URL>",
  "platform": "youtube|tiktok|reels|x|raw",
  "sampled_at": "<ISO-8601>",
  "duration_s": <int>,
  "aspect": "9:16|1:1|16:9|4:5",
  "fps": <int>,
  "format_bucket": "shorts|mid_form|long_form|sleep|sports_doc|movie",
  "source_content_type": "story|list|countdown|explainer|reaction|news|mythology|tutorial|interview|other",
  "hook": {
    "duration_s": <float>,
    "pattern": "title_overlay|cold_cut|question|stat_bomb|character_intro|other",
    "transcript": "<verbatim>",
    "visual": "<one-line description>"
  },
  "beats": [
    {"index": 1, "start_s": <float>, "end_s": <float>,
     "transcript_excerpt": "<>", "visual": "<>", "transition_in": "<>"}
  ],
  "pacing": {
    "wpm": <int>,
    "silence_ratio": <float>,
    "avg_beat_s": <float>
  },
  "voice": {
    "gender": "m|f|n",
    "age_band": "child|teen|young_adult|adult|elderly",
    "accent": "us|uk|in|other",
    "style": "calm|hype|asmr|character|narrator|other",
    "is_ai_likely": <bool>
  },
  "captions": {
    "present": <bool>,
    "style": "none|burned_in|word_by_word|reddit_card|tiktok_chunk|subtitle",
    "color": "<hex>",
    "position": "top|center|bottom|bottom_third"
  },
  "visual_style": {
    "kind": "real_footage|ai_image|animation|split_screen|reddit_card|talking_head|typography|slideshow|other",
    "palette": ["<hex>", "<hex>", "<hex>"],
    "lighting": "high_key|low_key|natural|stylized",
    "filter_or_grade": "<one-line>"
  },
  "transitions": ["hard_cut", "whip", "zoom_punch", "crossfade", "smash"],
  "on_screen_text": {
    "present": <bool>,
    "font_weight": "bold|regular",
    "case": "upper|sentence|title",
    "animation": "static|pop|slide|none"
  },
  "closer": {
    "present": <bool>,
    "transcript": "<verbatim or null>",
    "cta_type": "like|comment|subscribe|follow|next_video|none"
  },
  "thumbnail_style": "face|text_overlay|split|none|unknown",
  "title": "<verbatim>",
  "description_excerpt": "<first 200 chars or null>",
  "suggested_channel": "<best matching ytFactory channel slug or 'new'>",
  "suggested_trigger": "/make-<short-name>",
  "duplicate_of_existing_skill": "<existing /make-* trigger or null>"
}
```

Write the result to `data/format_clones/<slug>/fingerprint.json`.

### 5b. Write a long-form ANALYSIS.md alongside the fingerprint

The fingerprint captures the structured DNA. The **reasoning, gotchas,
quality-gate justifications, and decision log** belong in a separate
long-form document at
`data/format_clones/<slug>/ANALYSIS.md`. The next agent picking up
`/make-skill` (often a fresh session days later) reads ANALYSIS.md
cold and proceeds without re-deriving anything.

Sections to cover (mirror the canonical structure from the
2026-05-08 hearts-scottish-system run):

- §0 Executive summary (recommendation, channel match, hood gate)
- §1 The artefact + audit trail (download method, sample plan, traps hit)
- §2 The hook — full transcript, mechanism, authoring rule for the new skill
- §3 Pacing + beats — wpm, silence, beat boundaries, role table
- §4 Voice + TTS inheritance
- §5 Visual fingerprint (frame-by-frame, source-class taxonomy, watermarks)
- §6 Captions + on-screen text
- §7 Closer + CTA pattern + gates
- §8 Channel match scoring + duplicate-guard reasoning
- §9 Pipeline integration: reused / skipped / net-new
- §10 Stage-by-stage authoring flow for the generated skill
- §11 Quality gates (full numbered list)
- §12 Output schemas
- §13 Cast handling
- §14 Footage sourcing allow-list
- §15 Per-render YAML overrides (if any)
- §16 Renderer touch-up plan with LoC estimates
- §17 Self-learning loop + dual-save destinations
- §18 Bug-museum — every trap that *will* fire on production runs
- §19 Anti-patterns
- §20 Open questions / TBDs deferred to first ship
- §21 Decision log (every choice + justification)
- §22 References (files the next agent must read)
- §23 Handoff to the next agent (numbered checklist)

Target length: 8,000-12,000 words. **This is not optional** — the
skill `/make-skill` consumes ANALYSIS.md as the source-of-truth for
authoring decisions. Without it, the meta-skill has only a structured
JSON to work from and misses the *why* behind each field.

Reference example: `data/format_clones/hearts-scottish-system/ANALYSIS.md`.

### 6. Auto-pick the closest existing channel (and detect duplicates)

Score the fingerprint against every existing channel. Use this rubric:

```
score = match(format_bucket, channel_format) * 3
      + match(aspect, channel_aspect) * 2
      + match(source_content_type, channel_niche_keywords) * 3
      + match(voice.accent + voice.style, channel_tts_voice) * 1
```

Channel format / aspect / niche keywords come from each
`<channel>/learnings/channel.md` and `<channel>/config.yaml`. Niche
routing source-of-truth: `pipeline/niches.py:NICHE_CHANNEL`.

Cheat sheet (do NOT hardcode — read on each run, channels evolve):

| signal | likely channel |
|---|---|
| AITA / Reddit story / animated character art | `mystoriesanimated/reddit_amitheasshole` |
| Reddit story + Subway-Surfers / Minecraft strip | `scrollpulse` |
| Hindi mythology / Mahabharat / Ramayan | `hindutavaanimated` |
| Sleep-cadence history narration, slow Ken Burns | `historyrecapped/sleep` |
| Top-5 / countdown / sports highlights | `sportsrecapped/ranked` |
| Last-N head-to-head sports | `sportsrecapped` (use /make-last5) |
| Cosmos / space / NASA explainer | `cosmosdecoded` |
| AI / tech recap | `scrollpulse` (use the `ai_recap` variant — `scrollpulse/variants/ai_recap.yaml`) |
| Bilingual Hindi-English nursery rhyme + mascots | `rhymetimejunction` |
| Long-form sports doc 20-30m | `sportsrecapped` (doc) |

**If `duplicate_of_existing_skill` is non-null** — the format is
already covered by an existing `/make-*`. STOP. Do not invoke
`/make-skill`. Tell the user "this is what `<existing-trigger>`
already does — use that instead" and offer to run that skill on the
fingerprint as a sanity check.

**If no channel scores ≥ 5** — set `suggested_channel: "new"` and
tell the user no existing channel fits; recommend running
`/create-youtube-channel` first, then re-running this skill.

### 7. Quality gates (before chaining into /make-skill)

Mechanical checks — block the chain on any failure:

1. **Fingerprint completeness** — every required key in the schema
   above is present. `null` is allowed only where the schema permits
   it. Reject incomplete fingerprints (don't pass garbage to
   /make-skill).
2. **Sample sufficiency** — at least 6 frames sampled AND a
   transcript with ≥10 words. Re-sample at higher density if not.
3. **Channel match sanity** — `suggested_channel` exists on disk
   (`<channel>/learnings/channel.md` readable). Or it is the literal
   string `"new"`.
4. **Duplicate guard** — if `duplicate_of_existing_skill` is
   non-null, hard-stop with the redirect message; do NOT continue.
5. **Trigger uniqueness** — `grep -r "^name: <suggested_trigger>"
   .claude/skills/ ~/.claude/skills/`. On collision, suffix with the
   channel slug (`/make-aita-text` not `/make-aita`).
6. **Cloud-TTS rule echo** — note in the handoff that the generated
   skill MUST default to a `cloudrun_*` TTS provider on its target
   channel (heuristic 11). If the matched channel's `config.yaml`
   currently has a local provider, flag it for fix BEFORE running
   `/make-skill` (do not paper over it inside the new skill).
7. **Aspect / duration sanity** — fingerprint aspect matches the
   matched channel's aspect; fingerprint duration falls within the
   matched channel's duration band. Mismatch → tell the user and ask
   whether to (a) widen the channel's band, or (b) pick a different
   channel.

### 8. Confirm with the user, then chain into /make-skill

Show the user a 5-line summary:

```
URL:       <url>
Format:    <format_bucket> · <aspect> · <duration_s>s · ~<wpm> wpm
Style:     <visual_style.kind> · captions=<style> · voice=<accent>/<style>
Match:     <suggested_channel>  (score=<n>)
Trigger:   <suggested_trigger>  (no collision)
```

Ask one question: "Author `<suggested_trigger>` for
`<suggested_channel>` based on this fingerprint?" — choices: `Yes,
proceed` / `Pick a different channel` / `Override the trigger
name`.

On `Yes`, invoke `/make-skill` and pass:

- the fingerprint JSON path
- the locked trigger name
- the matched channel slug
- a one-line spec ("Author a `<trigger>` skill for
  `<channel>` that produces videos matching the format described in
  `<fingerprint.json>`. Inherit closer / banned phrasings / TTS
  voice / aspect / footage policy from the channel verbatim.")

`/make-skill` runs its own 4-stage flow from there. Your job is
done after the handoff.

### 9. Self-learning hook

After the user runs the GENERATED skill end-to-end and runs
`/critique-video` on its first output:

1. If a regression traces back to a missing field in the fingerprint
   (e.g., we missed that the source uses zoom-punch transitions
   every 2s and the generated skill produced static cuts), classify:
   - **ONE-OFF** (this fingerprint missed one signal) → fix the
     fingerprint JSON, append a 1-line note to
     `.claude/skills/clone-video-format/learnings/_index.md`.
   - **CLASS-OF-BUG** (the schema itself is missing a field — every
     future clone will miss it) → add the field to the SCHEMA in
     step 5 of this SKILL.md, append a regression note to
     `.claude/skills/clone-video-format/learnings/<topic>.md`, and
     mirror to `~/.claude/projects/-Users-rohit-ytFactory/memory/`
     per CLAUDE.md dual-save.
2. Update MEMORY.md if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a
   pre-render quality gate to step 7 that blocks emit on detection.

## Important rules

- **Never re-implement what `/critique-video` already does.** Use
  the same ffmpeg sampling pattern. If we add a third caller,
  extract to `pipeline/video_sample.py` (heuristic 51).
- **Never call yt-dlp without `.venv/bin/python -m yt_dlp`.** The
  CLI binary is not on PATH; the Python module is.
- **Never call the Anthropic SDK directly.** All LLM calls go
  through `pipeline/llm.py` (`claude -p`) — heuristic 22.
- **Never silently swallow yt-dlp errors** (geoblock / sign-in
  required / age-gate). Surface them to the user — heuristic 50.
- **Never duplicate an existing `/make-*` skill.** If
  `duplicate_of_existing_skill` is non-null, stop and redirect.
- **Never bypass `/make-skill`.** This skill ends at the handoff.
  /make-skill owns SKILL.md authoring, channel inheritance, the
  cloud-TTS banner, the self-learning hook in the generated skill,
  and the dual-save.
- **Never bake the closer / banned phrases into the fingerprint.**
  Those come from the matched channel's `learnings/channel.md`,
  applied later by /make-skill — heuristic 9/10.
- **Never bypass the website-first workflow** (heuristic 49). The
  generated skill produces JSON; the website at `:8765` triggers
  render + upload.
- Always echo the absolute path of `fingerprint.json` in the
  handoff.
- Always use `.venv/bin/python` for any helper command.

## Why this skill is separate from /critique-video and /make-skill

`/critique-video` reads one of OUR mp4s and gives a viewer reaction
+ pipeline-engineering notes. It does not download external URLs,
does not produce a structured fingerprint, does not propose new
skills. Its output is prose for a human reviewer.

`/make-skill` writes a SKILL.md given a 4-line spec. It does not
download a video, does not look at frames, does not classify
formats. Its input is a human-typed intent.

`/clone-video-format` is the bridge: it turns an external URL into
a structured fingerprint that matches `/make-skill`'s expected
input shape. Every step (download, sample, transcribe, fingerprint,
channel-match, duplicate-guard) only makes sense in that bridging
context. Forking the analysis into `/critique-video` would muddle
its viewer-reaction lens; folding it into `/make-skill` would
violate that meta-skill's "input is plain English, not a video
artifact" contract.

## Learnings from prior runs

See `learnings/_index.md`. Empty on day 1; populated as the skill
is exercised against real URLs.
