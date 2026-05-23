# One-and-final refactor plan

> **Status snapshot (2026-05-23, mid-refactor):**
> - Done on main: prod-v2 → v3 sweep (22 files), CLAUDE.md cleanup, deploy.sh dead-env-URL strip, channel YAML stale-comment strip, root-waste deletion (mystoriesanimated/, out/, prorevenge/, data/loop/, root __pycache__/, .DS_Store), .gitignore stale-line strip (5 lines), README.md rewrite (301 lines), `/make-channel` skill authored.
> - In flight (subagents): W1 text/LLM reliability (P3.1-P3.6, P5.3, P5.4, P6.1), W2 image/cast/TTS/overlay (P3.7, P4.1-P4.4, P5.1, P5.2), ADRs+fragility+tech-debt sync, vestigial audit, comprehensive cleanup audit, Phase 0 GCP verification.
> - W3 (cleanup+splits+docs) was killed mid-task; partial uncommitted work in `.claude/worktrees/agent-a3a906514b0653e74/`. Phase 3 deletions + Phase 4 splits remain.
> - Channel-root `<repo>/<channel>/` → `<repo>/data/<channel>/` move logged as task #46 (deferred until W1/W2 merge).

---



Mandate: cover every principle in `/ai/engineering-principles.md` against the current codebase. One pass. No regressions. End state = all 7 channels producing clean automated end-to-end videos (Q26, Q28 MVP definition).

This file is the contract for the refactor. It maps each charter principle to a concrete gap, an action, and a success criterion.

---

## Part A — Charter principle → project gap map

### Principle 1: Lean repo — every layer must have a purpose, every dependency justified, every abstraction must solve a real problem

| Gap | Evidence | Action | Status |
|---|---|---|---|
| Two render entry points (`render()` legacy + `render_via_engines()` engine path) | `pipeline/render/video.py:81` + `:128` | Worker imports `render_via_engines` directly; delete legacy `render()`. | Phase 2 |
| Legacy flat route files `control/{agent,dashboard,niche,render,scheduler}_routes.py` AND `control/routes/*` versions of all 5 | grep shows both exist; `server_dev.py:33-36, 38-44` mounts both | Migrate callers to `control/routes/*`, delete top-level 5. | Phase 2 |
| Duplicated control state modules (`control/jobs.py` vs `control/core/jobs.py`, same for queue/scheduler/rate_limit/storage/schema/auth) | 7 file pairs; diff shows non-trivial drift (not byte-identical) | Pick canonical (`control/core/*` is the destination per server_dev.py:89), merge any diverged code, delete top-level. | Phase 2 |
| `pipeline/upload/x_upload.py` (dead — Q33 confirms X/Twitter upload not used) | Already deleted in prior session per summary | Verify no callers + delete references | DONE |
| Klein refiner referenced everywhere; production uses z-turbo (Q67, Q68) | `pipeline/images/prompt_refiner.py:1` docstring; channel YAMLs use `cloudrun_z_image_turbo` | Replace klein refiner with z-turbo refiner. Single-target rewrite, no klein backward-compat. | Phase 4 (P4.1) |
| 4 dead env URLs in `cloud/render-worker-v2/deploy.sh:106` (flux2-klein, flux2-dev, qwen, indicparler) | Reviewed deploy.sh line 106 | Reduce to only the services that exist + are called: chatterbox, indicf5, z-image-turbo, asr-whisper. | Phase 3 |
| 13 stale `ytfactory-prod-v2` references | grep across 7+ files; bucket migrated to v3 (Q53) | Replace `prod-v2` defaults with `prod-v3` in source code; fix comments. | Phase 3 |
| `pipeline/render/video.py` legacy `render()` (147 LoC dead path) | Lines 81-126 + supporting plumbing | Delete after worker migration | Phase 2 |
| `closer_panel` dead-coded in compose.py:711-719 (Q72) | Lines 711-719 in compose.py | Port to overlays plugin slot `pipeline/render/overlays/closer_panel.py`; delete legacy dead code. | Phase 4 (P5.1) |
| `data/_bench/`, `data/_jobs/`, etc. — verify each is actively used | `ls data/` shows 11 subdirs | Audit each for current writers; delete unused. | Phase 3 |
| Per-channel render output dirs at repo root (`mystoriesanimated/`, `out/`, `prorevenge/`) | `ls /` shows them | Add to .gitignore; move existing content into `data/<channel>/`. | Phase 3 |
| Stale `Dockerfile` at repo root, `EDITING_AGENT_PROMPT.md`, `audit_data/`, `audit_report.html`, `deploy_logs/` | Already moved to /tmp per session summary | Confirm gone | DONE |
| Stale comments in channel YAMLs referencing `workers/heavy/render_short.py` (Q32 — stale) | `grep workers/heavy` channels/*.yaml | Strip stale comments. | Phase 5 |

### Principle 2: Source of truth / ownership — every feature has one owner

| Gap | Evidence | Action | Status |
|---|---|---|---|
| Two server entry points: `web/server.py` (prod, 3876 LoC) + `control/server_dev.py` (laptop dev). Routes drift. | server_dev.py + web/server.py both mount routers; doc explicitly notes "post-2026-05-09 cloud cutover" | Keep BOTH but enforce identical router surface; add a test that diffs `include_router` sets. (Don't delete server_dev — used by `test_e2e_happy_path.py` and `scripts/serve_cloud.sh`.) | Phase 2 |
| CLAUDE.md says `gs://ytfactory-prod-v2-artifacts` (stale per Q53) | `CLAUDE.md:50` | Replace with v3. | Phase 6 |
| README.md is materially stale (Q53 — references v2; references legacy structure) | `README.md:12, 69, 208` | Rewrite to current state. | Phase 6 |
| `pipeline/llm/cli.py` 3-backend dispatcher — `cli` (laptop dev) + `azure_openai` (prod default) + `anthropic_sdk` (cost-fallback). All 3 are INTENTIONAL per deploy.sh:134-137. | grep + deploy.sh | Keep all 3; document the policy in the dispatcher docstring; ensure tests cover each. NO DELETION. | Verify only |
| Plugin system — `register_plugin` × 6 slots × 17+ impls. Multi-impl per slot = JUSTIFIED abstraction per charter. | grep shows 4 music + 6 overlays + 5 visualize + 2 compose registrations | Keep. NO DELETION. | Verify only |

### Principle 3: Operational reliability — Q&A attack set (the 17 items)

| # | Q-Ref | Gap | Action | Phase |
|---|---|---|---|---|
| P3.1 | Q57.1 | Section bodies don't self-count words | Add `word_count` field to section-body JSON; validator uses ACTUAL count; emitted-vs-actual = telemetry only | Phase 1 |
| P3.2 | Q59 | "between min and max" interpreted as min-only | Per-section ±10% of section target, total ±15% of total target | Phase 1 |
| P3.3 | Q63 | No outline sum-check | If `sum(section.target_words)` outside ±15% of user target → retry outline once with error in prompt; hard-fail 2nd | Phase 1 |
| P3.4 | Q57.4 | `reasoning_effort=minimal` (downgraded 2026-05-13) wrong for length-sensitive output | Flip to `medium` for `rewrite_long_form` in `pipeline/llm/cli.py:318` | Phase 1 |
| P3.5 | Q61 | "Do NOT pad with filler" creates contradictory pressure with min-word floor | Remove the block; outline LLM writes per-section `quality_goal` (positive framing); section-body prompt includes it | Phase 1 |
| P3.6 | Q62 | Section retry regenerates from scratch (loses partial work) | Iterative-extend: send failed draft + "expand to N words by adding source detail." 1 attempt; hard-fail 2nd | Phase 1 |
| P3.7 | Q65 | Writeback 80% duration floor kills successful mp4s (`7743ca76` case) | Drop the duration floor; keep only stream sanity (valid h264+aac). If upstream gates work, duration is in band. | Phase 1 |
| P4.1 | Q69 | Klein refiner produces 4-10 word output; z-turbo wants 80-250 words structured | Replace `pipeline/images/prompt_refiner.py` with z-turbo refiner: structured 100-200 word prompts, z-turbo lighting vocabulary, positive-only avoidance | Phase 1 |
| P4.2 | Q66 | Image-gen 10% threshold kills whole render on first beat-failures | Per-beat retry: post-gen luminance/variance check → 1 retry with stronger prompt → hard-fail BEAT (not render) → only kill render if >10% beats fail AFTER retry | Phase 1 |
| P4.3 | Q70 | Cast outputs free-form description; beat prompts paraphrase → drift | Cast emits structured fields per character (age, hair, build, clothing, signature prop); beat prompts prepend spec VERBATIM. Add secondary characters when story requires (AITA antagonist, mythology supporting cast). | Phase 1 |
| P4.4 | Q71 | IndicF5 Hindi noise — `ref_audio_text` may not ship to model | Fix `pipeline/tts/cloudrun.py:929 _synth_cloudrun_indicf5`: verify payload includes `ref_audio_text`; test with golden ref-audio fixture | Phase 1 |
| P5.1 | Q72 | Closer panel dead-coded in compose.py:711-719 | Port to `pipeline/render/overlays/closer_panel.py` as OverlayProducer; toggled by `spec.chapter_cards` or new `spec.closer_panel`. Delete legacy. | Phase 1 |
| P5.2 | Q73 | `spec.caption_style` not wired end-to-end; Devanagari font missing; no caption density gate | Wire `caption_style` through overlay→compose mux; hard-fail (not warn) on caption import break; add Devanagari fonts to worker Dockerfile; add post-render caption density gate | Phase 1 |
| P5.3 | Q74 | No multi-source mixing | Per-niche multi-source config: `pipeline/variants/<channel>/<niche>.yaml` declares which adapters apply. LLM rewrite synthesizes across N raw materials. | Phase 1 |
| P5.4 | Q75 | No regression guard on audio mix | Add a golden-audio fixture test that fails when mix params change unexpectedly | Phase 1 |
| P6.1 | Q21-Q23 | scrollpulse channel YAML missing | Add `pipeline/channels/scrollpulse.yaml` (Reddit auto-pull + split-screen gameplay overlay format); niche variants; branding assets | Phase 1 |
| P6.2 | (charter) | Adding a channel is ad-hoc | Document the channel-creation workflow as a repeatable skill | Phase 6 |

### Principle 4: Continuous critique — `/ai/improvement-opportunities.md`, `/ai/known-fragility.md`, `/ai/tech-debt.md`, `/ai/decision-log.md` continuously updated

| Gap | Action | Phase |
|---|---|---|
| Session findings not mirrored back to these 4 docs | Append ADRs for: gate-as-repair-trigger philosophy, z-turbo as canonical image model, klein refiner deletion, render-engines as canonical path, control/core/* as canonical. Mirror Q-Ref findings into known-fragility.md + tech-debt.md. | Phase 6 |

### Principle 5: Visibility — agent has read access to logs, Firestore, GCS, metrics

Per Q76 ("make sure you have access"): operational, not code. Verify access during Phase 0; surface if blocked.

### Principle 6: Engineering health — observability, debuggability, reliability, deployment complexity, onboarding difficulty

Covered by Phase 1 (reliability), Phase 5 (regression-safe wiring + smoke), Phase 6 (docs).

---

## Part B — Execution phases

### Phase 0 — Pre-flight (read-only, ~30 min)

Goal: confirm assumptions before any destructive change.

- Verify `gs://ytfactory-prod-v3-artifacts` is the live bucket (gcloud)
- Verify GCP project is `ytfactory-prod-v3`
- Verify Cloud Run service list — confirm which services are currently routing traffic
- Confirm test baseline: `.venv/bin/pytest tests/ -x -q` passes
- Capture current git SHA as rollback anchor

### Phase 1 — Fix the 17 Q&A attack items (reliability work)

Touch only what each item needs. No collateral cleanup in this phase — that's Phase 2-3.

**Sequence (dependencies):**
1. P3.4 (`reasoning_effort=medium`) — cheap, instant
2. P3.5 (remove "Do NOT pad" + add quality_goal to outline schema) — outline schema change first
3. P3.1 (`word_count` self-emit field)
4. P3.2 (gate tolerances ±10% / ±15%)
5. P3.3 (outline sum-check + retry)
6. P3.6 (iterative-extend retry shape)
7. P3.7 (writeback gate → stream sanity only)
8. P4.3 (cast structured fields) — affects beat prompts downstream
9. P4.1 (z-turbo refiner replacement) — depends on P4.3 character spec format
10. P4.2 (per-beat retry + image-quality validator)
11. P4.4 (IndicF5 ref_audio_text fix)
12. P5.1 (closer panel → overlays plugin)
13. P5.2 (caption_style wire + Devanagari + density gate)
14. P5.3 (per-niche multi-source config)
15. P5.4 (audio regression guard)
16. P6.1 (scrollpulse YAML + variants + branding)

After each item: add a failing-when-bug-returns test (CLAUDE.md rule).

### Phase 2 — Migrate callers off legacy paths

Goal: stop using legacy code paths. Don't delete yet — that's Phase 3.

1. Worker `entrypoint.py` already uses `render_via_engines` (verify); update any remaining callers
2. server_dev.py + web/server.py both use `control/routes/*` (verified)
3. Diff `include_router(...)` sets between server_dev.py and web/server.py — they must match
4. Find any caller still importing `control.{agent,dashboard,niche,render,scheduler}_routes` and switch to `control.routes.*`
5. Find any caller still importing `control.{jobs,queue,scheduler,rate_limit,storage,schema,auth}` and switch to `control.core.*`
6. Tests: update test imports to canonical paths

### Phase 3 — Delete legacy code (lean)

1. Delete legacy `pipeline/render/video.py::render()` (legacy entry); keep only `render_via_engines` + `render_long_form`
2. Delete `control/{agent,dashboard,niche,render,scheduler}_routes.py` (5 files)
3. Delete `control/{jobs,queue,scheduler,rate_limit,storage,schema,auth}.py` (7 files; canonical is `control/core/*`)
4. Delete the 4 dead env URLs from `cloud/render-worker-v2/deploy.sh:106`
5. Replace 13 stale `ytfactory-prod-v2` defaults with `prod-v3` (source code only; ADRs/historical docs preserve as-is)
6. Add `mystoriesanimated/`, `out/`, `prorevenge/` to `.gitignore`; move existing content under `data/<channel>/`
7. Audit `data/` subdirs: delete any with zero current writers
8. Strip stale "workers/heavy/render_short.py" comments from channel YAMLs

### Phase 4 — Split god files

1. `cloud/render-worker-v2/entrypoint.py` (3132 LoC) → split into stage modules:
   - `bootstrap.py` — env load, ADC, Firestore reader, preflight
   - `stages/rewrite_stage.py`
   - `stages/cast_stage.py`
   - `stages/image_stage.py`
   - `stages/tts_stage.py`
   - `stages/asr_stage.py`
   - `stages/compose_stage.py` (calls `render_via_engines`)
   - `stages/upload_stage.py`
   - `writeback.py`
   - `entrypoint.py` becomes thin orchestrator (~200 LoC)
2. `web/server.py` (3876 LoC) — defer. Mostly route handlers + DI; split is high-risk for current cycle. Add to tech-debt.md instead.

### Phase 5 — Regression-safe wiring + smoke

1. Full pytest run: `.venv/bin/pytest tests/ -x -q` — must pass
2. Per-channel smoke render trigger (one Short per channel, one long per channel that has long-form)
3. Verify each channel's renders land in GCS + reach YouTube (Q26 MVP definition)
4. Capture metrics: success rate, gate-fire rate, retry-success rate, mp4 duration vs target

### Phase 6 — Docs refresh

1. Rewrite `README.md` to current state (v3, current channel list, current architecture)
2. Update `CLAUDE.md`: v2→v3 fixes; mention the 18-doc system; mention z-turbo (not klein)
3. Append ADRs 023-030 to `/ai/decision-log.md`:
   - ADR-023: Gates are repair triggers, not termination signals
   - ADR-024: Z-Image-Turbo is the canonical image model
   - ADR-025: LLM as editor on real material, not author from nothing
   - ADR-026: One LLM call combines pick+synthesize+write
   - ADR-027: `render_via_engines` is the canonical render entry; legacy `render()` deleted
   - ADR-028: `control/core/*` is the canonical state module location
   - ADR-029: All 7 channels MVP target; scrollpulse added
   - ADR-030: Reasoning effort `medium` for length-sensitive rewrites
4. Mirror Phase 1 findings into `/ai/known-fragility.md` + `/ai/tech-debt.md`
5. Update `/ai/improvement-opportunities.md` — strike completed items; surface new ones
6. Document the channel-creation workflow as `.claude/skills/make-channel/SKILL.md` (P6.2)
7. Audit `/ai/*.md` for redundancy; drop or merge any doc whose value is now in another

---

## Part C — What this plan does NOT do (declared scope-cuts)

- Does NOT switch any LLM/TTS/image model (Q46 locked: models out of scope)
- Does NOT introduce new abstractions or plugin slots (charter: anti-overengineering)
- Does NOT split `web/server.py` (too risky this cycle; goes to tech-debt.md)
- Does NOT remove the 3-backend LLM dispatcher (all 3 are intentional per deploy.sh:134-137)
- Does NOT remove the plugin system (multi-impl per slot = justified)
- Does NOT touch the laptop control plane Firestore queue / scheduler protocol (works; out of scope)
- Does NOT add latency / cost optimization (Q24 locked: latency deferred)
- Does NOT modify upload-OAuth flow (works; not the pain)

---

## Part D — Success criteria

Phase 5 smoke run produces, for each of 7 channels:
- 1 Short mp4 in `gs://ytfactory-prod-v3-artifacts/jobs/<id>/short.mp4`
- 1 long-form mp4 (where applicable: history, cosmos, hindutava, sports)
- Each mp4 uploaded to its YouTube channel (no per-channel `uploads/` zero records — Q53 finding)
- Zero `LongFormContractError` or writeback-duration kills across the run
- All gates that fired triggered granular retries (not full-render termination)
- Full pytest passes: `.venv/bin/pytest tests/ -x -q`

If any of those fail, the refactor is not complete.

---

## Part E — Locked decisions

- **Phase 5 smoke**: Real YouTube upload per channel, `privacy=unlisted`. End-to-end proof of Q26 MVP without going public.
- **Phase 4 split scope**: BOTH `entrypoint.py` AND `web/server.py` this cycle.
- **Execution mode**: 3 parallel worktrees, merging back to `main` at end:
  - **W1 (text/llm reliability)**: P3.1, P3.2, P3.3, P3.4, P3.5, P3.6, P5.3 (multi-source), P5.4 (audio regression), P6.1 (scrollpulse YAML)
  - **W2 (image/cast/tts/overlay reliability)**: P3.7 (writeback gate-only change in entrypoint.py — coordinate with W3), P4.1, P4.2, P4.3, P4.4, P5.1, P5.2
  - **W3 (cleanup + god-file split + docs)**: Phase 2-3 deletions, Phase 4 splits, Phase 6 docs
- **Conflict policy**: W2 touches `entrypoint.py` (P3.7 small change) AND `compose.py` (P5.1 closer-panel removal). W3 splits `entrypoint.py`. Resolution: W3 completes split first via blocking handoff with W2. W2 then applies its small change to the new `writeback.py` module.

---

## Part F — Worktree boundaries (file ownership)

| File / dir | W1 | W2 | W3 |
|---|---|---|---|
| `pipeline/llm/rewrite_long_form.py` | own | — | — |
| `pipeline/llm/cli.py` (reasoning_effort) | own | — | — |
| `pipeline/critic_long_form.py` | own | — | — |
| `pipeline/variants/*` | own | — | — |
| `pipeline/channels/scrollpulse.yaml` (NEW) | own | — | — |
| `pipeline/images/prompt_refiner.py` | — | own | — |
| `pipeline/images/ai_beat_slideshow.py` | — | own | — |
| `pipeline/render/visualize/*` | — | own | — |
| `pipeline/llm/cast.py` | — | own | — |
| `pipeline/tts/cloudrun.py` | — | own | — |
| `pipeline/render/overlays/*` (closer_panel new, caption fixes) | — | own | — |
| `pipeline/render/compose.py` (delete dead closer-panel code) | — | own | — |
| `pipeline/render/video.py` (delete legacy `render()`) | — | — | own |
| `control/*.py` top-level (5 routes + 7 state modules — DELETE) | — | — | own |
| `control/server_dev.py` (import cleanup) | — | — | own |
| `web/server.py` (import cleanup + split) | — | — | own |
| `cloud/render-worker-v2/entrypoint.py` (split → stages) | — | (P3.7 small) | own |
| `cloud/render-worker-v2/deploy.sh` (clean env URLs) | — | — | own |
| `README.md`, `CLAUDE.md` | — | — | own |
| `/ai/*.md` (decision-log, fragility, tech-debt updates) | — | — | own |
| `.gitignore` (channel output dirs) | — | — | own |
| 13 prod-v2 → v3 sweep | — | — | own |
| New tests for each fix | per worktree | per worktree | per worktree |
