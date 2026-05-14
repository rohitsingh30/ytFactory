# 2026-05-14 post-audit pipeline conventions

This is the agent-facing reference for the conventions added during
the 2026-05-13 → 2026-05-14 critique sweep. **If you're an agent
modifying any of `pipeline/llm/critic.py`, `pipeline/llm/critic_axes.py`,
`pipeline/era_anchor.py`, `pipeline/era_taxonomy.yaml`,
`pipeline/critic_long_form.py`, `pipeline/beats.py`,
`pipeline/thumbnails.py`, `pipeline/llm/rewrite.py`,
`pipeline/upload/upload.py`, `pipeline/render/shorts.py`, or
`pipeline/images/images.py` — read this first.**

The trigger for the sweep was a single GitHub Copilot CLI session
(2026-05-13) that audited 27 rendered videos in Firestore and found:

- All 27 had `critique.verdict = "SHIP"` despite obvious bugs
  (gibberish AI-text inside panels, WW1 trench infantry rendered for
  a 1258 historical event, 5 different anonymous footballers across
  5 beats of one Ronaldinho Short, etc.).
- The cloud render worker was hardcoding SHIP because the real
  vision-bearing critic couldn't run on cloud (Claude CLI tooling not
  available on Azure OpenAI).
- 9 cake-AITA / 6 baghdad-mongols / 5 ronaldinho re-renders within 6
  days because the scheduler had no topic-uniqueness guard.

**Audit catalogue (full data):** `docs/pipeline_bug_catalogue_v2_2026-05-14.html`.

**Plan + commit log:** `~/.copilot/session-state/0630f00a-ca59-4c2e-a03d-32dcee981b84/plan.md`.

---

## 6 root-cause classes the sweep addressed

| # | Root cause | Fix surface |
|---|---|---|
| **R1** | Critic asked LLM for free-form `verdict` with no rubric → defaults to SHIP | `pipeline/llm/critic_axes.py` (new) — 6-axis scoring; verdict derived deterministically |
| **R2** | Image-gen has no `negative_prompt` (FLUX.2 klein is guidance-distilled) | `ANTI_TEXT_SUFFIX` + per-era negatives appended to positive prompt instead |
| **R3** | Cast routing falls through to nothing for voice_only channels with unnamed beats | `_derive_protagonist_anchor` in `pipeline/render/shorts.py` |
| **R4** | Long-form fetched reddit body but never piped it into LLM context | Wired into `rewrite_long_form.py:846` (`notes = body or ...`); plus essay-drift + source-fidelity validators |
| **R5** | Beat segmenter time + punctuation only; broke mid-clause on stop words | `_FORBIDDEN_END_TOKENS` + `_shift_to_acceptable` in `pipeline/beats.py` |
| **R6** | Scheduler had no topic-uniqueness guard + no `internal_only` flag | 30-day dedupe via Firestore + `is_test_fixture_topic` regex autoflag |

---

## New conventions (read these BEFORE editing the linked files)

### 1. Critic axes — `pipeline/llm/critic_axes.py`

The post-render critic emits per-axis scores 1-10, NOT a free-form
verdict:

```python
AXES = (
    ("hook_strength",      "first 1.5s stop-the-scroll"),
    ("caption_legibility", "readable on a phone, no AI-gibberish"),
    ("cast_continuity",    "same person every panel"),
    ("mute_mode_score",    "story comprehensible without sound"),
    ("source_fidelity",    "honours the source URL/body"),
    ("closer_strength",    "closer panel + spoken CTA align"),
)
```

Verdict derivation (deterministic post-LLM in
`derive_verdict(axes)`):

- Any axis ≤ 3 → `BLOCK` (irrecoverable).
- Any axis < 7 → `FIX` (regen pass on the failing axis).
- All axes ≥ 7 → `SHIP`.

**Adding a new axis:** edit `AXES` + bump
`tests/test_critic_axes.py`. The schema fragment from
`axes_json_schema()` is auto-included in the LLM prompt via
`render_axes_block()`.

**Don't add a 7th axis without thinking carefully** — every axis is
a hard veto on SHIP, so axes that fire on too many videos turn the
gate into a no-op (everything BLOCKs). Calibrate by re-running the
critic on the audit's 27 videos.

**Cloud worker reality:** the vision-bearing critic still doesn't
run on cloud (Claude CLI vision is laptop-only until Azure OpenAI
vision plumbing lands in `pipeline/llm/cli.py`). The cloud worker
writes `{verdict: "UNGATED", ...}` instead of hardcoded SHIP. Upload
gate refuses non-SHIP, so UNGATED renders never auto-publish.

### 2. Era anchor — `pipeline/era_anchor.py` + `pipeline/era_taxonomy.yaml`

For historyrecapped (and any future history channel), the rewriter
LLM emits `metadata.era_anchor: <kebab-case key>` in the script
JSON. The renderer reads it and prepends `[ERA — <costume tokens>]`
to every panel image-gen prompt BEFORE character + scene, so
diffusion attention is anchored to the right period.

Taxonomy keys today:
```
13c-mongol-yuan-warband    1c-roman-legion-segmentata
12c-norman-knight-mailcoat 1c-roman-republic-toga
16c-tudor-court            16c-mughal-court
17c-mughal-shahjahan       18c-georgian-britain
ww1-1914-1918-trench       ww2-1939-1945-european-theater
cold-war-1947-1991-civilian byzantine-1453-fall-of-constantinople
edo-japan-1603-1868-samurai ancient-egypt-pharaonic-new-kingdom
```

**Adding a new era:** append to `pipeline/era_taxonomy.yaml`.
`tokens` should be SPECIFIC (concrete clothing items, weapons,
helmet style, mounts, period palette) — abstract era names like
"medieval" are too broad to anchor. `negatives` is advisory.

**Updating the rewriter prompt to know about a new era:** add the
key to the enumerated list in `_BASE_PROMPT` (around the
`metadata.era_anchor` block). Otherwise the LLM won't pick the new
key (it sticks to the enumerated examples).

**Backward-compat alias:** `metadata.era_lock` is also accepted
(legacy spelling from a pre-Phase-4b test fixture).

### 3. Anti-text suffix — `pipeline/images/images_cloudrun.py::ANTI_TEXT_SUFFIX`

Every cloud image-gen prompt gets `(no readable text in image, no
signs, no captions, no banners, no inscribed words, no jersey
lettering, no sponsor logos, no street signs, no readable book
covers, no name tags, plain backgrounds, no watermark)` appended.

**Why not `negative_prompt`:** FLUX.2 klein is guidance-distilled;
its server explicitly documents that `negative_prompt` is a no-op
on distilled models. Distilled models DO parse parenthesized
in-prompt negation, so we use the positive prompt instead.

**Per-channel override:** not wired today. Channels that legitimately
need text in images (e.g. a hypothetical "screenshot of a tweet"
channel) would need an override. Add via
`pipeline/images/images_cloudrun.py::_append_anti_text_suffix(model=...)`.

### 4. `_FORBIDDEN_END_TOKENS` — `pipeline/beats.py`

Beat splitter forbids the left side of a split ending on:
- Articles: a, an, the
- Possessives: my, your, his, her, our, their, ...
- Common prepositions: of, for, to, with, in, on, at, by, from, ...
- Connectives: and, or, but, so, as, if, ...
- Demonstratives: this, that, these, those

When tier-2 (conjunction split) or tier-3 (middle-word split) would
land a stop-word ending, `_shift_to_acceptable` walks ±3 words to
find an acceptable boundary. If none exists within ±3, falls back
to the original index — the `max_s` contract takes precedence.

**Adding a new stop-word:** append to `_FORBIDDEN_END_TOKENS`.
Lowercase, with-no-punct (the matcher strips trailing
punctuation before lookup).

### 5. `internal_only` proposal flag + test-fixture autoflag

`ShortProposal.internal_only: bool = False`. When True, the proposal
is dev / smoke-test fixture and:
- Hidden from production renders dashboard.
- Never auto-uploaded.
- Auto-set by `control/core/scheduler.py::is_test_fixture_topic`
  when the topic matches:
  - `\bsmoke[- ]?test\b`
  - `\bdescriptor[- ]?registry\b`
  - `\b(slice|verify|verification)\s+\d+`
  - `\bregress(ion)?\s+(test|fixture)\b`
  - `\bdebug[- ](render|test|build)\b`
  - `\b(internal|dev|debug|qa)[- ](only|fixture|test)\b`
  - `\b(test|fixture)\s+all\s+knobs\b`
  - kebab-slug forms: `smoke-test`, `debug-render`, `dev-fixture`,
    `qa-test`.

**Adding a new fixture pattern:** append to `_TEST_FIXTURE_PATTERNS`.
False positives are fine (operator can clear `internal_only` manually);
false negatives (real test fixtures slipping through) are the
actually-bad case.

### 6. Topic-uniqueness window — `control/core/scheduler.py`

`_recently_rendered_slugs(channel, since_days=30)` queries Firestore
for done jobs. `_next_unrendered` filters its candidate list by both
`uploaded` AND `recently_rendered`. The window is configurable via
`YTFACTORY_TOPIC_DEDUPE_DAYS` env (default 30, 0 = disable).

Degrades gracefully — Firestore unavailable → empty set → no dedupe
applied → falls back to legacy upload-only check.

### 7. Long-form essay-drift + source-fidelity validators — `pipeline/critic_long_form.py`

Two new soft-warn validators on top of the existing 5:

- `check_no_essay_drift(narration)` — opener-only check (first 300
  chars) for stock LLM essay-style phrases like "imagine a
  completely ordinary", "right now wherever you are", "attention
  is the new currency". Soft-warn (not hard-fail) so legitimate
  philosophical openers aren't blocked.

- `check_source_fidelity(narration, raw_body, *,
  min_overlap_frac=0.30)` — counts content-word overlap between the
  source body and generated narration. Below 30% overlap → soft warn
  surfacing the missing entities. Skipped when raw_body is None
  (LLM-only source) or too sparse (<30 anchor words).

**Adding a new banned phrase:** append to `BANNED_STOCK_ANECDOTES`
(hard-fail) or `ESSAY_DRIFT_OPENERS` (soft-warn).

### 8. Caption position lower-third — `pipeline/compose.py::_WORD_CAPTION_Y_FRAC`

Word-karaoke captions land at `y = 0.78 * frame_height` (lower-third).
Pre-2026-05-14 default was 0.45 (vertical centre = character
mid-section / belt buckle, occluded by portrait subject). Don't
revert without re-testing on real renders.

### 9. CTR thumbnail picker — `pipeline/thumbnails.py::pick_scene` + `score_frame`

Cloud worker calls `auto_thumbnail` (curated headline + scene).
`pick_scene` defaults to `img_00` (curated hook beat) but falls
through to highest-scoring viable frame when img_00 fails the
quality_gate. Score = 0.5·edge_density + 0.3·stddev + 0.2·luma.
Pure Pillow — no OpenCV dep.

### 10. Fiction disclosure footer — `pipeline/upload/upload.py::derive_metadata`

Per-channel YAML opt-in via `upload.fiction_disclosure: true`.
When enabled AND source is LLM-only (raw is None / empty, OR
raw.source_kind=='llm'), appends a transparency line to the YouTube
upload description. **Metadata-only** — does NOT modify the video
itself, preserves the explicit prior UX decision against on-video
watermarks.

Custom text via `upload.fiction_disclosure_text`; empty string
disables.

---

## What's NOT done — open follow-ups

### Phase 3b — FLUX.2 multi-reference editing

`cloud/image-flux2-klein/server.py` only exposes `/generate`.
Multi-reference editing requires deploying **FLUX.2 [Edit]** as a
SEPARATE Cloud Run service (separate weights staging, separate dep
tree, IAM grants). Not a code change — multi-day infra decision.

Until 3b lands, the description-only protagonist anchor from Phase 3
(`833038c`) is the strongest character lock the pipeline can express.
Combined with the per-axis critic gate from Phase 1, broken cast-drift
renders won't auto-ship anyway — they'll get FIX/BLOCK verdicts.

### Phase 7b — closer panel re-introduction

Closer panel was DELIBERATELY removed 2026-05-02 per user direction
(last-beat icon embedding into the diffusion image replaced it).
Re-introducing it would override that explicit UX call; needs operator
say-so first.

### Cloud-side vision critic

`pipeline/llm/cli.py` Azure backend doesn't support vision yet, so
the vision-bearing critic at `pipeline/llm/critic.py` only runs
laptop-side. Cloud worker writes UNGATED. Future: add vision support
to the Azure backend (gpt-4o vision via image_url payloads), then
the existing critic.py works on cloud.

---

## How a future agent should extend this

When you fix a NEW class-of-bug (one that would recur on the next
100 renders), follow this pattern:

1. **Identify the root-cause class** — does it fit one of the 6
   above (R1-R6) or is it a 7th? If 7th, add a row to the table at
   the top of this doc.
2. **Add a validator OR an axis** — if catchable post-render, add
   to the critic axes (`pipeline/llm/critic_axes.py`) with a
   threshold. If catchable post-rewrite, add to
   `pipeline/critic_long_form.py` for long-form OR
   `pipeline/llm/script_check.py` for shorts.
3. **Test the validator** — pin the BUG that motivated the fix in
   the test name. Future agents reading the test will understand
   the WHY.
4. **Update this doc** — append a section under "New conventions"
   so future agents discover the new convention without spelunking
   commit history.
5. **Update CLAUDE.md** — add a one-line pointer to this doc if
   the convention is project-wide (don't bloat CLAUDE.md with
   every detail).

---

## Tests that pin the conventions in this doc

- `tests/test_critic_axes.py` (axes scoring + verdict derivation)
- `tests/test_critic_contract.py` (contract behaviour)
- `tests/test_pipeline_critic.py` (critique_short verdict override)
- `tests/test_critic_long_form.py` (long-form validators)
- `tests/test_era_anchor.py` (taxonomy + prefix wiring)
- `tests/test_beats_stop_word_guard.py` (stop-word boundary guard)
- `tests/test_scheduler_dedupe.py` (topic-uniqueness + internal_only
  + test-fixture autoflag)
- `tests/test_compose_resolution.py` (caption lower-third)
- `tests/test_cloudrun_image_provider.py` (anti-text suffix)
- `tests/test_cloudrun_render_worker_thumbnail.py` (auto_thumbnail wiring)
- `tests/test_utils_thumbnails.py` (score_frame + pick_scene fallthrough)
- `tests/test_render_shorts.py::TestDeriveProtagonistAnchor` (cast lock)
- `tests/test_upload_youtube.py::TestFictionDisclosure` (fiction footer)
- `tests/test_llm_rewrite.py::PromptNumbersRuleTest` + `PromptEraAnchorInstructionTest`

If you remove or significantly weaken any of these tests, the
corresponding bug will silently come back.

---

## Commit log (this sweep)

```
b58c0a7 feat(upload): fiction-disclosure footer for LLM-only sources (Phase 4c)
f83e317 feat(rewrite): teach LLM to emit metadata.era_anchor for historical topics
e599f67 feat(thumbnails): score_frame heuristic + pick_scene fallthrough on broken hook
906c7ca feat(era_anchor): historyrecapped costume/period prefix for image-gen
833038c fix(cast): voice-only channels promote single supporting char as protagonist anchor
536b0ce fix(render-worker): use auto_thumbnail composer instead of ffmpeg first-frame
410093b fix(critic_long_form): essay-drift opener + source-fidelity validators
851b73e fix(rewrite): years/dates/counts use digits, not spelled-out words
1a261ca fix(scheduler): topic-uniqueness window + internal_only flag + test-fixture autoflag
01b9aa6 fix(beats): forbid mid-clause splits on stop words
a994672 fix(compose): caption position lower-third (was character belt-buckle)
e93c839 fix(images): anti-text suffix for cloud image gen
ea91523 fix(critic): per-axis verdict gate replaces SHIP rubber-stamp
```

13 commits, 100% diff coverage on each, ~170 new test cases.
