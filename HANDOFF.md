# ytFactory — Current State (handoff)

Last updated: 2026-05-03. End of session that audited end-to-end latency and shipped the engineering view + reliability + persistence-pipe + caption-prerender + benchmark sweep (see "Latency engineering" section below).

This document is the single source of truth for **what exists today**, what runs where, and the loose ends. Pair with `DESIGN.md` (vision + principles) and `MEMORY.md` files in `~/.claude/projects/-Users-rohit-ytFactory/memory/`.

---

## TL;DR

You have:

1. A **fully autonomous pipeline** that turns a niche keyword into a finished 1080×1920 mp4 Short. No manual rewriting, no manual prompt-authoring.
2. A **web UI** (FastAPI + single-page Tailwind/vanilla JS) that wraps the pipeline. Pick a niche → pick a voice → generate. The UI's right panel evolves audio→video so the ~7-minute wait isn't dead time.
3. An **auto-critique loop** that watches every Short through 15 lenses and returns class-of-bug fixes (not just per-video patches).

## Production channels

Active publishing targets — every other channel YAML in `channels/` is
a variant or research config.

| Channel | YAML | Niche | Aesthetic |
|---|---|---|---|
| **MyStoriesAnimated** | `channels/mystoriesanimated.yaml` | Reddit animated stories | Flat 2D crayon, pastel fills, round-headed character. Slideshow + Ken Burns. |
| **SportsStoriesAnimated** | `channels/sportstoriesanimated.yaml` | Animated football (soccer) moments | Tifo Football editorial line-art, cream parchment. Slideshow + Ken Burns, with `kind: footage` beats that cut to real broadcast clips at the climactic moment. |

Channel icon + banner (YouTube spec: 800×800 / 2048×1152) live at
`data/intermediate/<channel>/branding/{avatar.png,banner.png}`.

Run it:
```bash
# Terminal 1: backend
YTFACTORY_TOKEN=$(cat /tmp/ytfactory_token) \
  .venv/bin/uvicorn web.server:app --host 127.0.0.1 --port 8765

# Terminal 2 (optional, for public URL):
cloudflared tunnel --url http://127.0.0.1:8765

# Browser: http://127.0.0.1:8765/auth?token=<TOKEN>
```

---

## File map

```
ytFactory/
├── pipeline/
│   ├── llm.py              ← claude CLI subprocess wrapper (single LLM entry)
│   ├── visualizability.py  ← raw-story heuristic gate (drops dialogue-heavy stories)
│   ├── rewrite.py          ← stage 3 — autonomous narration rewrite via claude CLI
│   ├── cast.py             ← stage 3.5 — per-story narrator description
│   │                          (incl. relationship-marker → demographic inference)
│   ├── audio.py            ← stage 4 — Kokoro TTS, lang inferred from voice prefix
│   ├── beats.py            ← stage 5 — Whisper word timestamps + clause-aligned beats
│   ├── align.py            ← source-text alignment (no silently dropped words)
│   ├── prompts.py          ← stage 5.5 — per-beat {key_visual, scene} via claude CLI
│   │                          (cast.default_emotion threaded; antagonist-presence rule)
│   ├── images.py           ← stage 6 — Flux Schnell / SDXL-Lightning + lint_prompt
│   ├── quality_gate.py     ← post-image QC (size/dims/std-dev/edge density + retry)
│   ├── compose.py          ← stage 7 — ffmpeg slideshow + closer panel + tail-hold
│   ├── captions.py         ← caption PNG render + render_closer_panel(closer_format)
│   ├── critic.py           ← stage 7.5 — 15-lens auto-critique with system_corrections
│   └── script_check.py     ← hard-gate: AITA closer format, hook, wedge
├── make_shorts.py          ← orchestrator. New flags: --tts-voice, --no-critic
├── pull_stories.py         ← end-to-end: fetch → visualizability → rewrite → cast
│                              (--no-llm raw-only mode for debug)
├── channels/
│   ├── aita_animated.yaml  ← closer_format, closer_hold_s, image_style_prefix (relaxed)
│   └── ...
├── web/
│   ├── server.py           ← FastAPI + SSE + token auth
│   └── static/
│       ├── index.html      ← single-page UI (Tailwind CDN, vanilla JS)
│       └── voice_samples/  ← lazy-cached voice preview wavs
├── data/
│   ├── intermediate/<channel>/{raw,scripts,cast}/<slug>.json
│   ├── cache/<slug>/{narration.wav,beats.json,prompts.json,closer_panel.png,img_NN.png}
│   ├── shorts/<slug>.mp4
│   └── critiques/<slug>/{frames/,score.json,...}
└── HANDOFF.md              ← this file
```

---

## What the autonomous pipeline does

For one CLI invocation `pull_stories.py reddit --subreddit AmItheAsshole --limit 1`:

1. **Pull** — reddit JSON API → raw text
2. **Visualizability filter** (`pipeline/visualizability.py`) — drops dialogue-heavy / abstract stories
3. **Rewrite** (`pipeline/rewrite.py`) — claude CLI authors hook-first narration with the channel's `closer_format` baked in
4. **Cast** (`pipeline/cast.py`) — claude CLI authors a narrator description matched to the OP's age/gender (relationship-marker inference: "my DIL" → middle-aged narrator, never the channel's default kid character)
5. Writes `raw/<slug>.json`, `scripts/<slug>.json`, `cast/<slug>.json`

Then `make_shorts.py --script <path>`:

6. **TTS** (`pipeline/audio.py`) — Kokoro synth, lang inferred from voice id
7. **Beats** (`pipeline/beats.py` + `pipeline/align.py`) — Whisper word timestamps, clause-aligned beats, no silently-dropped source words
8. **Per-beat prompts** (`pipeline/prompts.py`) — claude CLI authors `{key_visual, scene}` per beat. Inputs: beat list + full source story + cast.narrator + cast.default_emotion + channel style_prefix + opening_image_directives. Constraints: no text-bait, no plural subjects, scene ≤50 tokens, antagonist-presence rule (if beat names "my friends", scene must have a visual hint of them), emotion-tracks-narration rule.
9. **Image gen** (`pipeline/images.py`) — Flux Schnell, with `pipeline/quality_gate.py` post-check that retries with bumped seed on flat/black output
10. **Compose** (`pipeline/compose.py`) — ffmpeg slideshow with Ken Burns + crossfades + caption-vs-xfade alignment + closer panel (positioned at top of frame, not over character) + tail hold (capped 1s) + caption-panel deduplication
11. **Auto-critique** (`pipeline/critic.py`) — 15 lenses (Hook, Sync, Continuity, Pacing, Captions, AI-glitch, Composition, Palette, Mute mode, Thumbnail, Story arc, Closer/CTA, Source fidelity, Re-watchability, Comments-bait). Returns `{score, top_issues, beat_corrections, system_corrections}`. If score < `min_critic_score` AND `beat_corrections` are present, it patches prompts.json + regenerates affected images + recomposes once.

---

## What the web UI does

`http://127.0.0.1:8765/` — two-step picker → render lobby → result, all in one page. The right preview panel evolves:

| Stage event | Right-panel state |
| --- | --- |
| Job submitted | Spinner + "Standing by — voice arrives in ~5 seconds" |
| `tts.done` | `<audio controls>` of the narration + voice label card |
| `compose.done` | `<video controls autoplay>` + Download button |

### Endpoints

| Method | Path | What |
| --- | --- | --- |
| GET | `/` | Picker page (HTML) |
| GET | `/auth?token=...` | Set cookie, redirect to / |
| GET | `/healthz` | `{ok, auth_required}` |
| GET | `/api/niches` | 6 curated niches |
| GET | `/api/voices` | **38 voices across 9 languages** + sample URLs + language list |
| GET | `/api/voices/{id}/sample.wav` | Lazy-synth + cache. Sample text matched to voice's lang. |
| POST | `/api/jobs` | Body: `{niche, options:{voice}}` → `{job_id}`. Spawns `pull_stories.py` then `make_shorts.py --tts-voice <id>` as subprocesses; tails stdout, parses progress prefixes (`[1/4]`, `[prompts]`, `[3/4] mflux: generating N images`, `[critic] score=`), emits typed SSE events. |
| GET | `/api/jobs/{id}` | Snapshot — events log + state + beat_prompts + mp4_url |
| GET | `/api/jobs/{id}/events` | SSE stream of stage events |
| GET | `/api/jobs/{id}/audio` | narration.wav (live) |
| GET | `/api/jobs/{id}/short` | final mp4 |
| GET | `/api/jobs/{id}/thumb/{i}` | per-beat img_NN.png |
| GET | `/api/jobs/{id}/closer` | closer_panel.png |

### Voice list (`/api/voices`)

| Lang | Count | Voices |
| --- | --- | --- |
| 🇺🇸 English (US) | 12 | Bella ★, Heart, Sarah, Nicole, Nova, Aoede, Michael, Adam, Liam, Eric, Onyx, Puck |
| 🇬🇧 English (UK) | 8 | Emma, Alice, Isabella, Lily, George, Daniel, Lewis, Fable |
| 🇪🇸 Español | 2 | Dora, Alex |
| 🇫🇷 Français | 1 | Siwis |
| 🇮🇹 Italiano | 2 | Sara, Nicola |
| 🇧🇷 Português | 2 | Dora, Alex |
| 🇮🇳 Hindi | 4 | Alpha, Beta, Omega, Psi |
| 🇯🇵 Japanese | 3 | Alpha, Nezumi, Kumo |
| 🇨🇳 Mandarin | 4 | Xiaoxiao, Xiaobei, Yunxi, Yunjian |

★ = default. Sample text is per-language (each voice reads an AITA hook in its own language).

### Auth

`YTFACTORY_TOKEN` env var when starting uvicorn enables three accept paths: cookie `yt_tok=`, query `?token=`, or `Authorization: Bearer`. `/healthz` and `/auth` are open. **Do not expose the server publicly without setting the token** — every job runs subprocesses, so an unauth'd public URL is RCE.

---

## DESIGN.md §14 principles (current count: 21)

| # | Principle | Where enforced |
| --- | --- | --- |
| 1 | Caption + image transition together | `compose.py` xfade midpoint |
| 2 | Character locked **per story** (not channel) | `cast.py` + `make_shorts.py` |
| 3 | No text-bait words | `images.py:lint_prompt` |
| 4 | Closing CTA required | `script_check.py` |
| 5 | Hook ≤1.5s + wedge ≤3s | `script_check.py` |
| 6 | Visible Ken Burns motion | `compose.py` zoom rate ≥0.0040/frame |
| 7 | Opening image needs ≥2 concrete tokens | channel YAML `opening_image_directives` |
| 8 | Beats end on hard punctuation | `beats.py` |
| 9 | Source-text alignment never drops words | `align.py` |
| 10 | Critique is system input | `critic.py` returns `system_corrections` |
| 11 | Diffusion drops late tokens — `key_visual` first | `images.py:build_full_prompt` |
| 12 | One main subject per beat | `images.py:lint_prompt` plural-subject regex |
| 13 | `key_visual` weighted | `(key_visual:1.4)` in build_full_prompt |
| 14 | IP-Adapter for character lock | `images.py` SDXL path (Flux pending Redux) |
| 15 | AITA closer = LIKE-if-YTA / COMMENT-if-NTA | `script_check.py:_AITA_LIKE_NTA_RE` + `closer_format` YAML |
| 16 | Closer panel must not overlap character | `compose.py` closer_y=80 |
| 17 | Caption + closer panel must not duplicate text | `compose.py:_beat_overlaps_closer` |
| 18 | Narrator emotion tracks beat sentiment | `cast.default_emotion` → `prompts.py` |
| 19 | Image quality-gate post-gen | `quality_gate.py` + retry loop |
| 20 | Source visualizability filter | `visualizability.py` at pull time |
| 21 | Critic returns class-of-bug, not just one-off | `critic.py` `system_corrections` schema |

---

## Memory (persists across sessions)

In `~/.claude/projects/-Users-rohit-ytFactory/memory/`:

- `feedback_aita_closer_panel.md` — closer must be LIKE-if-YTA / COMMENT-if-NTA
- `feedback_llm_via_claude_cli.md` — autonomous LLM stages spawn `claude -p`, no SDK
- `feedback_engineer_class_of_bug.md` — every fix classified ONE-OFF vs CLASS-OF-BUG
- `project_per_story_character.md` — character authored per-story, not channel-locked
- `project_aita_cooking_format.md` — separate cooking-bg variant + its constraints

---

## Open / pending

These are real follow-ups, not aspirational:

### Frontend (the natural next session — see NEXT_SESSION.md)

- [ ] Voice picker still renders 38 voices ungrouped. **Add language tabs/filter row** above the voice grid. The `/api/voices` payload already returns `languages: [{code, label, flag}]`.
- [ ] "Run auto-critic" button on the result panel is a placeholder. Wire it to a new `/api/jobs/{id}/critique` endpoint that calls `pipeline/critic.py:critique_short` and streams the 15-lens findings into a panel under the video.
- [ ] Replace Tailwind CDN with built CSS for production polish (Tailwind warns in console).
- [ ] Add a job history sidebar (last 10 jobs from `JOBS` dict) with score badges.
- [ ] Tweak the right panel — currently it's audio→video swap; could also show the **closer panel preview** as a third state (after `compose.start`, before final video).

### Backend / pipeline

- [ ] `pipeline/critic.py` `regenerate_with_corrections` only patches `prompts.json` and deletes affected images; should also bump seed and re-run quality_gate per-image.
- [ ] `make_shorts.py` runs critic synchronously after compose; for the web UI it would be better as a separate explicit step (the UI already has `--no-critic` and the placeholder button).
- [ ] `pull_stories.py` only writes one slug per niche per run via `--limit 1` (web UI behavior). For batch backfill, expose a "make 5" option.
- [ ] Channel YAML files for non-AITA niches (`tifu`, `malicious`, `prorevenge`, `oddities`, `tih`) currently fall back to `aita_animated.yaml` in the niche map. Each should get its own with appropriate `closer_format`, `image_style_prefix`, and `opening_image_directives`.
- [ ] Flux character lock — DESIGN.md §14 #14 says IP-Adapter is SDXL-only; Flux Redux integration is pending.
- [ ] **Streaming compose v2** — `compose.prerender_word_captions` already pulls word PNGs out of the critical path. Next step: per-clip pre-render (Ken Burns + caption overlays) into intermediate mp4s as each `img_NN.png` lands, then a final `concat + audio mux`. Estimated 3-7% wall-time gain on a successful job; high-risk refactor (interdependent ffmpeg graph: rank chips, footage cuts, closer panels).
- [ ] **Persistent image worker production-ready** — `YTFACTORY_PERSIST_IMAGE_PIPE=1` ships in v1 with HTTP fallback to local generation on any worker error. Things to confirm under load: GPU lock contention if/when concurrent jobs are enabled, OOM behavior on the server when the pipe lives there.

### Latency engineering (2026-05 session)

The first end-to-end latency analysis ran against 584 telemetry events across 36 jobs. Findings + shipped fixes:

- **Image stage = 97.6% of total stage time** (244 k of 250 k stage-seconds). Every other optimization is dwarfed by anything that touches image gen.
- **Job success rate was 14%** — but ~50% of "failures" were user-cancellations being mis-bucketed as `stage_error`. Now split: `job_cancelled` is its own event, doesn't pollute success rate.
- **Truncated tracebacks** — `event.message[:300]` was keeping the *head* of a Python traceback (boilerplate "Traceback (most recent call last)"). Now keeps the *tail* (last 600 chars) so exception type + message survive into telemetry. `_traceback_fingerprint()` in `/api/telemetry/latency` uses this to bucket errors.
- **Pre-flight image gate** — ffmpeg crashed twice on missing `img_NN.png` / `closer_panel.png` after the image stage silently skipped a beat. `make_shorts.py` now raises a structured `RuntimeError` BEFORE invoking compose if any expected image is missing on disk.
- **IP-adapter image cache** — `pipeline/images.py:_IP_REF_CACHE` keyed on (path, mtime). Was decoding the same PNG ~30-120× per job (every retry × every beat).
- **Word caption pre-render** — `compose.prerender_word_captions` runs in a background thread during image gen, lifting ~7-15 s of CPU work out of the post-image critical path. `compose.wipe_stale_per_beat_artefacts` exposed as a public helper so make_shorts.py can pre-wipe BEFORE the prerender thread starts.
- **Persistent image worker (opt-in)** — `YTFACTORY_PERSIST_IMAGE_PIPE=1` warms the diffusion pipe in the server process; `make_shorts.py` POSTs each render to `/api/_internal/render`. Saves the 15-30 s cold load per job. Falls back to local on any worker error.
- **`/api/telemetry/latency` + UI** — engineering view that ranks stages by % share of total wall time (>50% renders red), per-niche success + p50/p90, image retry %, error groups by traceback fingerprint, slowest-recent-job critical-path waterfall.
- **`scripts/bench_image_providers.py --steps-sweep`** — Pareto frontier across (provider × steps), recommends a fastest + quality-within-30% pick per provider, plus an overall winner.

### Deployment

- [ ] Cloudflared tunnel is manual right now (`cloudflared tunnel --url ...`). For an always-on public URL you'd want a named tunnel + DNS CNAME (matches shofferai's docs in `~/shofferai/.github/skills/tunnel/SKILL.md`).
- [ ] No process supervisor — uvicorn dies if the laptop sleeps. A simple launchd plist would keep it alive.

### Quality

- [ ] `aita02` baseline still has the smiling-character-through-conflict issue. Renders made AFTER the cast.py / prompts.py upgrades (e.g. `aita-birth-pool`) should be the new baseline; old `aita02_v3/v4/v5` artifacts are reference-only.
- [ ] `make-script` skill at `.claude/skills/make-script/SKILL.md` still describes the manual rewrite-rubric flow. Could be slimmed down to "just call `pull_stories.py`".

---

## Known cosmetic noise (not bugs)

- Browser console: `cdn.tailwindcss.com should not be used in production` — fine for local
- Browser console: `favicon.ico 404` — could add `@app.get("/favicon.ico")` returning 204
- Browser console: `Could not establish connection. Receiving end does not exist.` — Chrome extension chatter, not us
