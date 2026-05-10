# Post-upload debrief — TIFU 4-pack 2026-05-08

First multi-render of `/make-mystories-short --source tifu`. Surfaced
~12 distinct learnings across 7 render iterations. This doc is the
canonical walkback per CLAUDE.md §"Post-upload analysis rule".

## Renders shipped

| slot (UTC) | slot (IST) | slug | dur | size |
|---|---|---|---|---|
| 07:13 | 12:43 | tifu-...-dentist-named-my | 68.8s | 20.6 MB |
| 09:13 | 14:43 | tifu-...-love-you-too-boss | 70.0s | 23.2 MB |
| 11:13 | 16:43 | tifu-...-asking-for-trainee | 70.6s | 13.4 MB |
| 13:13 | 18:43 | tifu-...-mechanic-2011-cult | 64.6s | 19.0 MB |

All four uploaded as `privacy=private` with `publishAt` set to the slot
time. YouTube auto-flips to public at that timestamp.

## Iterations + classifications

### 1. CLASS-OF-BUG: Cloud Chatterbox max_new_tokens=1000 (296 WPM chipmunk audio)

**Symptom:** 178-word narration rendered to 34.2s wav = **296 WPM**
(normal English narration is ~140-180 WPM). Audio raced; word-level
captions raced; viewer couldn't parse anything.

**Root cause:** `chatterbox/tts.py:249` hardcodes
`max_new_tokens=1000` with a TODO. T3 generates ~33 tokens/s of audio,
so the cap = ~30s of acoustic frames. For longer text, the model
crams ALL words into 30s — racing speech.

**Fix:** Sentence-chunker added to `cloud/tts-chatterbox/server.py`
(`_chunk_text()` + `_CHUNK_WORD_TARGET=35` + `_BREATH_SILENCE_S=0.18`).
Each chunk produces ≤13s of audio, safely under cap. Chunks
concatenated with a short breath silence between. Cloud Build +
redeploy 19 min; revision `00006-9gd`. Validation: same 178-word
input → 68.0s wav = **157 WPM** ✓.

**Memory:** `feedback_chatterbox_1000_token_cap.md`
**Affects:** every English channel using `cloudrun_chatterbox` (16 YAMLs).

### 2. CLASS-OF-BUG: FLUX.2 klein at 4 steps under-trains anatomy

**Symptom:** First 4 mp4s shipped with weird-hand glitches (extra
fingers, fused fingers, soft-blob hands). The
`tifu.yaml:image_steps: 4` was the Schnell-style floor; FLUX.2 klein's
quality knee for anatomy is at 6-8.

**Fix:** `tifu.yaml:image_steps: 4 → 8`. Per-image render time
roughly doubles to ~10s on Cloud Run L4; total Short adds ~3 min,
but hands clean up dramatically.

**Memory:** `feedback_image_steps_quality_knee.md`
**Pending:** apply to other character-variant YAMLs (aita_animated,
aita_cliffhanger_animated, hindutavaanimated config) once validated
in production.

### 3. CLASS-OF-BUG: Quality gate had no anatomy detection

**Symptom:** Even at 8 steps, occasional hand glitches still leaked.
The existing `pipeline/llm/quality_gate.check_image` only checked
brightness, edge density, file size, optional OCR — nothing for
anatomy.

**Fix:** Added `pipeline/llm/anatomy_check.py` — haiku vision via
`Read` tool, focused yes/no rubric for hand/finger glitches. Wired
into `quality_gate.check_image(anatomy_check=True)` and exposed via
YAML `anatomy_check: true`. Cost ~$0.002/image, latency ~15s/call,
fail-open semantics so a flaky vision call never blocks. tifu.yaml
opted in. 120/120 anatomy-pass on the v4 re-renders.

**Memory:** `feedback_anatomy_qc_haiku_vision.md`
**Pending opt-ins:** aita_animated, aita_cooking, all character variants.

### 4. CLASS-OF-BUG: Image cache hits skip QC re-validation

**Symptom:** Flipped `anatomy_check: true` and re-ran. Only 3 of 31
images ran QC — the other 28 were prompt-sha256 cache hits and
bypassed `quality_gate.check_image` entirely. Workaround:
`rm img_*.png` for affected slugs to force regen.

**Fix:** Logged as task #11 — render loop should re-evaluate cached
images under updated QC flags. Workaround documented, real fix
pending.

**Memory:** `feedback_image_cache_qc_bypass.md`

### 5. CLASS-OF-BUG: Parallel renders > cloud `--max-instances` crash

**Symptom:** Kicked 3 parallel TIFU renders. Cloud
`cloudrun_flux2_klein --max-instances=2` saturated. Read-timeouts at
15min cap → render-level circuit breaker tripped → fell back to
local mflux. 3 concurrent mflux processes thrashed unified memory →
**Metal GPU Timeout** (`kIOGPUCommandBufferCallbackErrorTimeout`)
killed mechanic mid-render.

**Fix:** Updated rule: `parallel render count ≤ cloud
--max-instances`. To run >2 wide, redeploy image service with
larger `--max-instances`. Memory:
`feedback_parallel_bulk_renders.md` (updated with cap rule). Skill
mirrors patched (`.claude/skills/make-mystories-short/SKILL.md` §5
+ rules; `.claude/skills/make-history-short/SKILL.md` §7b).

**Memory:** `feedback_parallel_bulk_renders.md`
**Project doc:** `docs/parallel_bulk_renders.md`

### 6. CLASS-OF-BUG: Cloud TTS missing-URL raises plain RuntimeError

**Symptom:** First render attempt without `.env` sourced raised
`RuntimeError("CLOUDRUN_TTS_CHATTERBOX_URL ... required")`. Documented
fallback path is "missing → local F5", but
`pipeline/tts/cloudrun.py:_service_url` raises plain `RuntimeError`,
which `_synth_cloudrun_chatterbox` doesn't catch (it only catches
`CloudRunUnavailable`). So fallback never engaged.

**Fix:** Pending (task #6). `_service_url` should raise
`CloudRunUnavailable` (the documented sentinel) when env var is
missing. Workaround for this session: source `.env` before each
render.

### 7. CLASS-OF-BUG: Cloud Run ID token expires mid-render

**Symptom:** `image_steps=8` doubled per-image time. The 30+ image
gen calls in a single render pushed past gcloud ID token TTL → 401
on call ~24/31 mid-flight. Cache progress preserved; re-running
resumed and finished.

**Fix:** Pending (task #9). `pipeline/images_cloudrun.py:_post_generate`
should retry on 401 after refreshing token, OR refresh per-request
rather than caching for the run.

### 8. CLASS-OF-BUG: _CTA_RE regex too strict on natural rewriter output

**Symptom:** `script_check.py` rejected 3 of 4 TIFU narrations on
first authoring pass. Required vote-prompt CTA in last 2 sentences,
but matching only "what would you have done" (inverted) and "comments
below|comments your". Rewriter naturally produces "what you would
have done" and "in the comments" — both miss.

**Workaround:** Hand-edited each script's last paragraph to put the
question last.

**Fix:** Pending (task #5). Loosen `_CTA_RE`:
- `\bwhat (would you|you would) (have )?(do|done)\b`
- `\b(in the |in )?comment[s]?( below| your)?\b`

### 9. CLASS-OF-BUG: Rewriter prompt calibrated for racing TTS

**Symptom:** `pipeline/llm/rewrite.py:_BASE_PROMPT` says
"110-160 words, ~22-32 seconds at typical TTS pace". With
post-fix Chatterbox at 142 WPM, 140 words = ~60s, 178 words = ~75s.
The "22-32s spoken" target was implicitly calibrated to the *broken*
racing TTS.

**Result:** All 4 TIFU mp4s ended up 64-71s, just over the YouTube
Shorts 60s cap. They post as regular videos.

**Fix:** Pending (task #8). Two paths:
- (A) Tighten rewriter to emit 50-80 words / 22-32s at 142 WPM, OR
- (B) Update `tifu.yaml:duration_target_s: [10, 20] → [50, 60]` to
  reflect production reality.

Path (A) restores Shorts-cap compliance; (B) accepts long-shorts.

### 10. ONE-OFF: Rewriter dialogue capitalization

**Symptom:** Narration text "Then Dr, martin walks in." (comma not
period; lowercase martin).

**Pipeline fix needed:** Add post-rewrite linter in `pipeline/llm/script_lint.py`
for sentence-initial caps on dialogue tokens.

### 11. ONE-OFF (per critique-video): Cast-router displaces channel narrator

**Symptom:** Dentist v1 frame at 30s showed two old male dentists,
narrator (mom) had vanished from the dental chair scene where she
should still be present. Cast-router routed beat to "Dr. Martin"
and the prompt dropped the narrator entirely.

**Fix needed:** `pipeline/llm/cast_router.py` should preserve the
narrator as a co-character when she's on-stage in the beats.json
transcript. Logged in critique markdown.

### 12. ONE-OFF (per critique-video): Closer panel doesn't render closer_format text

**Symptom:** TIFU closer panel showed only the bell icon — no LIKE/COMMENT
text. `tifu.yaml:closer_format` is "LIKE if you've been there,
COMMENT your worst." but the rendered panel rastered just the glyph.

**Fix needed:** `pipeline/captions.py:render_closer_panel` should
read `closer_format` and rasterize the text.

## What worked

- **Chunked Chatterbox** — 296 WPM → 157 WPM, single-deploy fix.
- **image_steps 4 → 8** — visible quality jump, cost is +5 min/render.
- **Per-frame anatomy gate (fail-open)** — caught no fails on the
  v4 renders, but the existence of the gate catches the regression
  surface; if a future render slips a glitch through, the gate
  rejects + retries with bumped seed.
- **Parallel = 2-wide pair** — twice the throughput vs serial,
  exactly fits cloud capacity, no failures.

## What regressed (fix in next session)

- Open class-of-bugs: tasks #5, #6, #8, #9, #11.
- Critique-video found unfixed visual issues: cast-router displacing
  narrator (#11 pipeline/llm/cast_router.py), closer panel missing
  closer_format text (#12 pipeline/captions.py), rewriter dialogue
  capitalization (#10 pipeline/llm/script_lint.py), FLUX.2 style
  drift across frames (still latent at 8 steps; less acute but
  visible).

## Pointers

- Skill: `.claude/skills/make-mystories-short/SKILL.md`
- Variant: `mystoriesanimated/variants/tifu.yaml`
- Rendered mp4s: `mystoriesanimated/reddit_tifu/shorts/`
- Critique: `mystoriesanimated/reddit_tifu/critiques/<slug>/critique.md`
- Cloud Chatterbox: `cloud/tts-chatterbox/server.py`
- Anatomy gate: `pipeline/llm/anatomy_check.py`
- Parallel rule: `docs/parallel_bulk_renders.md`

## Update — fixes shipped after the initial 4-pack debrief

Same session, same day. After uploading the 4 TIFU mp4s and getting
the user's "fix everything + bulk produce 100 shorts" follow-up, we
landed all remaining open class-of-bugs from this list:

| # | fix | file:function |
|---|---|---|
| 5 | `_CTA_RE` accepts "in the comments" / "what you would have done" | `pipeline/llm/script_check.py:_CTA_PATTERNS` |
| 6 | Missing CLOUDRUN_TTS_*_URL → `CloudRunUnavailable` (fallback now engages) | `pipeline/tts/cloudrun.py:_service_url` |
| 8 | Rewriter 110-160w → 110-135w; script_lint cap 160 → 135 | `pipeline/llm/rewrite.py:_BASE_PROMPT` + `pipeline/llm/script_lint.py:TARGET_WORDS_HI` |
| 9 | 401 mid-render → token cache pop + retry once | `pipeline/images_cloudrun.py:_post_generate` |
| 11 | Cached image QC re-validation under YAML flag flips | `pipeline/render/shorts.py` cache-hit branch |
| 17 | Sentence-init lowercase + Dr, /Mr, typo auto-fix | `pipeline/llm/script_lint.py:_capitalize_sentence_starts` |
| 18 | Cast-router witness-verb guard (narrator stays on screen) | `pipeline/llm/cast_router.py:_is_witness_beat` |

Plus extended the anatomy gate (`anatomy_check: true`) to all 6
character variants (was only on `tifu.yaml`):
- `aita_animated.yaml` (image_steps 4 → 8)
- `aita_text.yaml` (image_steps 4 → 8)
- `aita_cliffhanger_animated.yaml` (image_steps 4 → 8)
- `aita_cliffhanger_text.yaml` (image_steps 4 → 8)
- `wiki_oddities.yaml` (image_steps 4 → 8)
- `today_in_history.yaml` (image_steps 4 → 8)

And kicked the 100-shorts pipeline:
- 103 scripts pulled across 5 niches
- bulk render queue running detached (PID 90245 + chain script 95839)
- daily upload cron loaded (`com.ytfactory.upload-next` launchd plist)

Full runbook: [`100_variety_shorts_2026_05_08.md`](./100_variety_shorts_2026_05_08.md).

**Definition of "next /make-mystories-short = smooth-as-butter":**
all 7 class-of-bugs above closed; no manual `.env` sourcing, no
hand-edited closers, no scripts over 60s cap, no chipmunk audio,
no narrator-vanishing-on-witness-beats, no Dr/martin typos. The
only manual step is the AskUserQuestion source picker.
