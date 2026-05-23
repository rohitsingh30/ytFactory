# Technical Debt Register

Running ledger of debt owed by the codebase. "Debt" = something that
isn't deliberately the way it is — it accreted, it costs interest
every time we touch the surface, and paying it down is net positive
even without new feature work. (Distinct from
`improvement-opportunities.md`, which is net-new value.)

Effort scale:
- **S** = single-PR, < 1 day
- **M** = focused sprint, 2-5 days
- **L** = multi-PR refactor, 1-2 weeks
- **XL** = architectural pass, > 2 weeks

---

## D1. Duplicate route + state files (7 pairs) — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 2 (migrate
callers) + Phase 3 (delete legacy) lock `control/core/*` +
`control/routes/*` as canonical (ADR-028). Entry kept for historical
context.

The 7 file pairs the session catalogued (Q53 + Phase 2):
`control/{jobs,queue,scheduler,rate_limit,storage,schema,auth}.py` vs
`control/core/*.py`, and the 5 route-file pairs (agent, dashboard,
render, scheduler, niche) — all with diverged logic per `diff -q`.

**Where:** `control/agent_routes.py` ↔ `control/routes/agent_routes.py`,
`control/dashboard_routes.py` ↔ `control/routes/dashboard_routes.py`,
`control/render_routes.py` ↔ `control/routes/render_routes.py`,
`control/scheduler_routes.py` ↔ `control/routes/scheduler_routes.py`,
`control/niche_routes.py` ↔ `control/routes/niche_routes.py`. Same
pattern for `control/auth.py` ↔ `control/core/auth.py`,
`control/jobs.py` ↔ `control/core/jobs.py`,
`control/schema.py` ↔ `control/core/schema.py`.

**What's owed:** Pick one location. `web/server.py:1792-1813` imports
exclusively from `control.routes.*` and `control.core.*`. `control/server_dev.py:24-39`
imports BOTH flat AND nested. Production and laptop dev run different
code today. `diff -q` confirms all 5 route-file pairs differ.

**Why it's debt:** Migrating to a `routes/` layout half-completed.
Confirmed not deliberate — `control/server_dev.py` mixes both
nests for the same APIRouter slot (`render_router_v2`, etc.),
which would never be a design choice.

**Effort:** **M** — port any unique handlers from flat to nested,
delete flat, update `server_dev.py` imports, smoke-test the
laptop dev FastAPI app boots.

---

## D2. Prompt-refiner module identifies itself as klein-specific but runs against Z-Image-Turbo in production

**Where:** `pipeline/images/prompt_refiner.py:1` (module docstring:
"LLM prompt-refiner pre-step for FLUX.2 [klein]"); env
`YTFACTORY_PROMPT_REFINER=1` is set at
`cloud/render-worker-v2/deploy.sh:106`; all 6 channel YAMLs route to
`cloudrun_z_image_turbo` (verified via the deploy.sh env var
`CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL`). FLUX.2 klein has no live
deploy.sh in `cloud/`.

**What's owed:** Replace the refiner with a Z-Image-Turbo-shaped
refiner (per onboarding-qa Q67–Q69 research). Today's refiner outputs
4–10 word `refined_visual` + BFL composition vocab + Qwen3-tuned
prompting; Z-Image-Turbo's documented sweet spot is 80–250 words,
structured per the 8-section template (shot+subject / age+appearance
/ clothing+palette / environment / lighting / mood / style+medium /
safety). Negative prompts are ignored at the model layer
(guidance_scale=0.0), so positive-only framing is mandatory.

**Why it's debt:** Architectural mismatch is not deliberate — the
team moved off klein but the refiner module never followed. Every
short rendered today through the refiner is suboptimally prompted.

**Effort:** **M** — single-target rewrite, no klein backward-compat;
update the cached field shape; bump `REFINER_VERSION`.

---

## D3. Dead-coded closer panel logic in `pipeline/compose.py:711-719` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md P5.1 ports the
closer panel to `pipeline/render/overlays/closer_panel.py` as an
`OverlayProducer` (ADR-011) and deletes the legacy stub. Entry kept
for historical context.

**Where:** `pipeline/compose.py:711-719`.

**What's owed:** Delete `closer_caption_rows: list[Path] = []` /
`closer_panel_path = None` hardcoded stub. Remove the
`closer_format`/`closer_panel_path` kwargs from the surrounding
function signature (callers must be migrated). Port to an
`OverlayProducer` impl at `pipeline/render/overlays/closer_panel.py`
per the engine architecture's overlay slot (onboarding-qa Q72).

**Why it's debt:** Code says "accepted for back-compat but ignored."
Either the back-compat is needed (and there's a caller out there
silently getting nothing), or it's not (and the stub should be
deleted). Current state is the worst of both.

**Effort:** **S** for the delete; **M** if porting to the overlay
slot with full caller migration.

---

## D4. Legacy `pipeline.render.video.render()` alongside `render_via_engines()` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 2 + Phase 3
delete the legacy `render()`; `render_via_engines` is locked as the
canonical entry (ADR-027). Entry kept for historical context.

**Where:** `pipeline/render/video.py:81` (`render()`) — 45-line
function that just dispatches to `render_long_form()` for LONG_FORM
and raises `NotImplementedError` for SHORT / sports_doc /
footage_only. Module docstring `pipeline/render/video.py:1-43`
describes a Slice 2 state that no longer exists.

**What's owed:** Delete `render()`. Migrate any remaining callers to
`render_via_engines()` for short / long. Update the module docstring.
Keep `render_long_form()` (since it does the rewrite+narration setup
that `render_via_engines()` doesn't).

**Why it's debt:** The bigbang absorbed everything into engines but
left the dispatcher stub. Active footgun (see known-fragility F1, F15).

**Effort:** **S** — most callers already use `render_via_engines()`;
grep `pipeline.render.video.render(` for any laggards.

---

## D5. Subprocess `_extract_last_traceback` machinery in `pipeline/render/video.py:597-737` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 3 deletes
this dead subprocess plumbing alongside the legacy `render()` entry
(ADR-027). Entry kept for historical context.

**Where:** `pipeline/render/video.py:597-737` (~140 LoC: regex
constants, `_is_telemetry_traceback`, `_extract_last_traceback`,
`_format_subprocess_failure`, `_stream_subprocess`,
`_maybe_emit_long_form_progress`, `_LF_DONE_TOKEN_RE`).

**What's owed:** Delete. The functions exist to support a subprocess
invocation pattern that `render_long_form()` no longer uses — line
279-301 explicitly says "Pre-2026-05-15 this shelled out to ... the
new entrypoint at pipeline.render.__main__ is the canonical CLI
replacement, but ... Calling render_via_engines directly in-process
is the cleanest fix." The subprocess machinery survives.

**Why it's debt:** Dead code; carries non-trivial maintenance burden
(the OTel-traceback filter is itself fragile, see known-fragility F7).

**Effort:** **S** — delete + grep callers (likely zero in production
path).

---

## D6. Onboarding-qa CONSOLIDATED ATTACK SET items 1-13 (treat as debt to current behaviour)

**Where:** Multiple sites per onboarding-qa.md "CONSOLIDATED ATTACK
SET" (lines 421-461).

Each item below is a known-non-deliberate gap the user surfaced
through the brainstorm. Listed here because the *current* behaviour
is debt against the *locked* operating principles (onboarding-qa
lines 464-474). These are NOT new feature work — they're "fix the
existing behaviour to match the locked design intent."

### D6.1 — `word_count` field not emitted in section-body JSON output
- Where: `pipeline/llm/rewrite_long_form.py:880` (`_call_section_body_llm`).
- Owed: Force the LLM to emit `word_count` in the JSON envelope so
  it self-counts (anchoring effect). Validator uses ACTUAL count via
  `len(narration.split())` as the hard gate; emitted-vs-actual delta
  becomes telemetry only.
- Effort: **S**.

### D6.2 — Gate thresholds calibrated to the old behaviour, not the locked ±10% / ±15%
- Where: `pipeline/critic_long_form.py:222`
  (`HARD_FLOOR_FRAC=0.50`),
  `pipeline/critic_long_form.py:225` (`HARD_SECTION_FLOOR_FRAC=0.40`).
- Owed: Per-section ±10% of section's target_words; total ±15% of
  total target. The current 50% / 40% floors are way too permissive;
  by the time they fire it's catastrophic under-delivery.
- Effort: **S** (the constants + tests).

### D6.3 — No outline sum-check
- Where: `pipeline/llm/rewrite_long_form.py:617`
  (`_call_outline_with_retry`).
- Owed: After outline returns, validate
  `sum(section.target_words)` is within ±15% of user's total target.
  If not, retry outline once with error in prompt; hard-fail if
  retry also wrong.
- Effort: **S**.

### D6.4 — `reasoning_effort` for `rewrite_long_form` pinned to `minimal`
- Where: `pipeline/llm/cli.py:318`
  (`_DEFAULT_REASONING_EFFORT_BY_STAGE["rewrite_long_form"] = "minimal"`).
- Owed: Flip back to `medium`. The 2026-05-13 calibration to minimal
  saved tokens at the cost of length-instruction-following. Per
  onboarding-qa Q57 the model can't count words while emitting under
  minimal effort.
- Effort: **S** (one constant + verify max_tokens budget headroom).

### D6.5 — "Do NOT pad with filler" negative-framing in section-body prompt
- Where: `pipeline/llm/rewrite_long_form.py:275`
  (`_SECTION_BODY_PROMPT_TEMPLATE`).
- Owed: Remove the negative-framing block; replace with outline-LLM-
  written `quality_goal` per section ("escalate tension", "reveal
  twist"). Per locked principle "negative-framing in prompts often
  amplifies X."
- Effort: **S** (prompt edit + outline schema bump).

### D6.6 — Section-body retry regenerates from scratch instead of iterative-extend
- Where: `pipeline/llm/rewrite_long_form.py:1049-1081` (the retry
  loop inside `_attempt`).
- Owed: On a length-failure, send the failed draft back + "expand to
  N words by adding more source detail." 1 attempt; hard-fail if 2nd
  also wrong.
- Effort: **M** (new prompt path + caching the prior draft).

### D6.7 — Writeback 80% duration floor too coarse
- Where: `cloud/render-worker-v2/entrypoint.py` (writeback gate,
  not shown but referenced in onboarding-qa Q54 + Q65 as firing on
  job `7743ca76`).
- Owed: Drop the duration policer. Replace with sanity check only
  (mp4 has valid video + audio streams). The upstream gates (after
  D6.2 lands) will keep duration in band naturally.
- Effort: **S**.

### D6.8 — `_PER_BEAT_FAILURE_THRESHOLD = 0.10` is a first-failure kill, not a post-retry kill
- Where: `pipeline/render/visualize/ai_beat_slideshow.py:90, 492`.
- Owed: Add per-beat retry with image-quality validator (post-gen
  luminance/variance check). If validator says "low," retry once with
  stronger prompt. Then hard-fail the beat. The 10% threshold becomes
  a post-retry kill.
- Effort: **M**.

### D6.9 — `cast.json` schema doesn't carry structured per-character fields
- Where: `pipeline/render/spec_enrich.py:214` writes a single
  `character_description` string. Beat prompts read this opaque
  string verbatim.
- Owed: Output structured fields per character (age, hair, build,
  clothing, signature prop). Beat prompts literally prepend the
  spec verbatim. Add secondary characters when story requires.
- Effort: **M** (cast prompt + schema + beat-prompt assembly).

### D6.10 — IndicF5 ref_audio_text suspected non-shipping
- Where: `pipeline/tts/cloudrun.py:813-840`
  (`_synth_cloudrun_indicf5`).
- Owed: Verify `ref_audio_text` actually reaches the IndicF5 model
  via the HTTP request body. If not, fix. Specific targeted bug,
  not a refactor.
- Effort: **S** (1 hour to verify + 1 hour to fix if missing).

### D6.11 — Caption import error path not hard-failing in all sites
- Where: `pipeline/render/overlays/word_caption_pngs.py` (24KB; not
  fully audited). Onboarding-qa Q73 calls for "Hard-fail (not warn)
  when caption import breaks." Audit 2026-05-15 caught some sites;
  not all.
- Owed: Audit every caption-related import path. Add Devanagari fonts
  to `cloud/render-worker-v2/Dockerfile` (not present today, line
  29-38).
- Effort: **M**.

### D6.12 — No caption density gate post-render
- Where: No site exists today.
- Owed: Post-render verify caption density (% audio time with caption
  overlay); hard-fail if below threshold.
- Effort: **S** (new test + new gate).

### D6.13 — Per-niche multi-source config is missing
- Where: `pipeline/variants/<channel>/<niche>.yaml` files exist but
  don't declare adapter chains.
- Owed: Per-niche multi-source config. `aita_animated` →
  `reddit_aita`; `krishna_leela` → `wiki_mahabharat + scripture_text`.
  LLM rewrite synthesizes across N raw materials.
- Effort: **M** (schema + adapter wiring + per-niche rollout).

---

## D7. Module docstring drift in `pipeline/render/video.py` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 3 deletes
the legacy `render()` and rewrites the module docstring (ADR-027).
Entry kept for historical context.

**Where:** `pipeline/render/video.py:1-43`.

**What's owed:** Rewrite the docstring to match current behaviour.
The current text describes Slice 2 state (pre-bigbang). Per charter
"the function body is authoritative" — but the docstring being
wrong is still debt because it actively misleads readers.

**Why it's debt:** Documentation drift, not deliberate.

**Effort:** **S** (15-min edit).

---

## D8. `scrollpulse` channel YAML missing (7th channel referenced in UI)

**Where:** `pipeline/channels/` has 6 YAMLs; UI at
`web-next/app/app/create/page.tsx:567-575` references `scrollpulse`
in `FALLBACK_CHANNEL_KEYS`.

**What's owed:** Author `pipeline/channels/scrollpulse.yaml` (auto-pull
Reddit + split-screen gameplay overlay format), niche variants,
branding assets. Per onboarding-qa Q21–Q23.

**Why it's debt:** Partial channel rollout — UI advertises it,
backend can't render it.

**Effort:** **S** for the YAML; **M** including branding + niche
variants + first successful render.

---

## D9. `firestore.rules` describes `ytfactory-prod-v2` but project is `ytfactory-prod-v3`

**Where:** `firestore.rules:2` (header comment: "ytfactory-prod-v2
(2026-05-11)"). CLAUDE.md + deploy.sh use `ytfactory-prod-v3`.

**What's owed:** Update the comment. Verify the rules are deployed
to the v3 project's Firestore database.

**Why it's debt:** Stale comment. Substantive risk if the rules
weren't redeployed when projects migrated.

**Effort:** **S** (5 min to fix comment + verify
`gcloud firestore rules describe` on v3).

---

## D10. Density of `# noqa: BLE001` broad-except suppressions (235 occurrences)

**Where:** Repo-wide. Concentrations at
`pipeline/research/cross_engage.py` (~12),
`pipeline/niche_specs.py:293-406` (8),
`pipeline/render/video.py` (10),
`pipeline/render/short_engine.py` (8),
`pipeline/render/input_registry.py` (7).

**What's owed:** Audit + classify each site (legitimate-defensive
vs. error-swallowing). Tighten exception types where possible. Set
a soft cap so the count doesn't grow.

**Why it's debt:** Each broad-except suppression individually is a
deliberate call. The aggregate density means audit is impossible by
inspection — the lint that should flag a NEW suppression is silenced
in 235 places. Charter's "If a stage can't produce its real output,
raise an exception" gets diluted.

**Effort:** **L** — full audit is multi-PR, but the value compounds
(each tightened site reduces fragility).

---

## D11. `pipeline/render/visualize/ai_beat_slideshow.py:503-517` — two silent fallbacks the 2026-05-15 audit missed

**Where:** Lines 503-506 (0-images branch) and 515-517 (stitch-failed
branch).

**What's owed:** Route both through the audit-gated fail-loud path
(same as `longform_panels.py:233`). Either call into
`_solid_color_override_enabled()` first or directly
`raise RenderFailedError`.

**Why it's debt:** The audit was supposed to catch every silent
fallback in this file; these two slipped through.

**Effort:** **S** (10-line edit + test).

---

## D12. `footage_windows.py` direct silent fallbacks bypass the audit gate

**Where:** `pipeline/render/visualize/footage_windows.py:57, 63, 75`
all call `self._fallback_solid_color()` directly without first
checking `_solid_color_override_enabled()`.

**What's owed:** Route through the gate. Same pattern as
`longform_panels.py:233`.

**Why it's debt:** Inconsistency with the audit pattern. Sibling-site
miss.

**Effort:** **S**.

---

## D13. Hardcoded service URLs in `cloud/render-worker-v2/deploy.sh:106`

**Where:** `cloud/render-worker-v2/deploy.sh:106` —
`CLOUDRUN_TTS_CHATTERBOX_URL=https://tts-chatterbox-e67vyhiy6a-as.a.run.app`
etc. Multiple service URLs hardcoded into the env var string.

**What's owed:** Discover URLs at deploy time via
`gcloud run services describe <svc> --format='value(status.url)'`
and substitute. Today's hardcoding breaks if any service is recreated
(new revision hash).

**Why it's debt:** Brittle service-discovery. Per CLAUDE.md
"`cloud/render-worker-v2/deploy.sh:90` is the source of truth for
Cloud Run Job env vars" — making it source-of-truth AND
hardcoded-string-of-URLs concentrates two failure modes in one line.

**Effort:** **S**.

---

## D14. CLAUDE.md vs production architecture: env vars list includes services that don't exist as deploy.shs

**Where:** `CLAUDE.md:39-53` lists
`CLOUDRUN_TTS_F5_URL`, `CLOUDRUN_TTS_HIGGS_URL`,
`CLOUDRUN_TTS_COSYVOICE_URL`, `CLOUDRUN_IMAGE_FLUX2_KLEIN_URL`,
`CLOUDRUN_IMAGE_QWEN_URL`, `CLOUDRUN_IMAGE_HIDREAM_URL`, etc.
Actual `cloud/` only has `tts-chatterbox`, `tts-indicf5`,
`image-z-image-turbo`. No klein, no f5, no higgs, no cosyvoice, no
qwen, no hidream `deploy.sh` exists in `cloud/`.

**What's owed:** Update CLAUDE.md to match what actually deploys.
Either delete the dead env-var entries or restore the deploy.sh files
if those services are still expected to exist.

**Why it's debt:** Documentation lies. New contributors will set up
the wrong env vars, mis-configure backups, etc.

**Effort:** **S** (5-min cleanup).

---

## D15. Two parallel telemetry traceback heuristics

**Where:** `pipeline/render/video.py:604-714`.

**What's owed:** The whole module is dead per D5; but specifically
the `_TELEMETRY_TRACEBACK_FRAME_RE` regex and the heuristic walking
"last non-telemetry traceback" depend on OTel SDK internals. When
OTel ships a refactor, the regex stops matching, the heuristic
silently picks the OTel traceback as the "real error," and operators
chase phantom bugs (the very situation 8a4f7e15 documented).

**Why it's debt:** Heuristic with a hidden coupling to an external
library's internal frame paths.

**Effort:** **S** if you delete (per D5). **M** if you keep + harden
(version-pin OTel, add a contract test that catches mismatch on
upgrade).

---

## D16. `cloud/render-worker-v2/entrypoint.py` is a 3132-LoC god file

**Where:** `cloud/render-worker-v2/entrypoint.py` (3132 LoC).

**What's owed:** Split into stage modules per refactor-plan.md Phase 4:
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

**Why it's debt:** 3132 LoC in a single file makes the worker's
control flow undebuggable in production. Every stage is interleaved
with bootstrap, retry, telemetry, and writeback concerns. Refactoring
unblocks the writeback gate change (P3.7 — ADR-010) and the per-stage
OTel trace work (O20). The Phase 4 split is locked in refactor-plan
Part B step 1.

**Effort:** **L** — careful module-by-module extraction with the
existing test suite as the contract.

**Where:** `cloud/render-worker-v2/entrypoint.py`.

---

## D17. `web/server.py` is a 3876-LoC god file

**Where:** `web/server.py` (3876 LoC).

**What's owed:** Split into route modules + DI module per
refactor-plan.md Phase 4. The plan initially deferred this split as
"too risky for the current cycle" but Part E locked it back in: "Phase
4 split scope: BOTH `entrypoint.py` AND `web/server.py` this cycle."

**Why it's debt:** Same shape as D16. 3876 LoC of mixed route handlers
+ DI wiring + state plumbing makes any production debug session start
with "find the route handler" instead of "diagnose the bug." Per
ADR-028 the route modules already exist under `control/routes/*` —
the split mostly moves remaining inline routes into those modules and
trims `web/server.py` to a thin FastAPI wiring file.

**Effort:** **L** — high risk relative to D16 because production
traffic flows through this file. Smoke gate in Phase 5 must include a
per-route diff against pre-refactor behaviour.

**Where:** `web/server.py`.

---

## D18. Channel artifact roots at `<repo>/<channel>/` instead of `<repo>/data/<channel>/`

**Where:** Per-channel render output dirs sit at the repo root
(`mystoriesanimated/`, `historyrecapped/`, `cosmosdecoded/`, etc.)
rather than under `data/<channel>/`. Per the `.gitignore` audit in
refactor-plan.md Phase 3 step 6 the dirs are gitignored but their
file-system location pollutes the repo root.

**What's owed:** Move per-channel artifact roots to `data/<channel>/`.
Task #46 (referenced in this session) estimates the surface as ~72
tests + 5 hardcoded callers that reference the current location. Phase
3 of refactor-plan adds the new locations to `.gitignore`, moves
existing content, and patches callers.

**Why it's debt:** Repo root layout suggests `mystoriesanimated/` is a
source-code directory, not a data directory. Onboarding cost is high:
new contributors browse the root, see 7 channel-named directories
alongside `pipeline/`, `control/`, `cloud/`, and have no signal which
are code vs runtime artifacts. Once on `data/<channel>/` the root
contains only source code modules + the `data/` artifacts root.

**Effort:** **M** — patches across ~72 test files + 5 hardcoded
callers; safe per-channel rollout (one channel at a time) keeps the
diff manageable.

**Where:** `<repo>/<channel>/` (7 dirs) → `<repo>/data/<channel>/`;
`pipeline/paths.py`; tests under `tests/`; any hardcoded callers in
`scripts/` and `pipeline/`.

---

## Summary

13 of these (D1, D3, D4, D5, D7, D9, D10, D11, D12, D13, D14, D15, the
D6.x cluster) are pure cleanup — no behaviour change visible to the
user; they reduce future-bug surface. D2 (klein refiner) and D8
(scrollpulse) and the D6.x cluster (attack set) are user-visible:
they unblock the MVP per onboarding-qa Q26 ("ONE clean automated
render shipped end-to-end" hasn't happened yet). D16/D17 (god-file
splits) and D18 (channel artifact roots) are repo-health work that
compounds developer velocity rather than directly unblocking renders.

Recommended ordering: D6.4 + D6.1 + D6.2 + D6.3 first (cheapest
unblocks for the documented `a734babb` / `7743ca76` failure class),
then D2 (Z-Image-Turbo refiner) to unblock image quality, then the
god-file splits (D16, D17) before the per-stage fixes have to interact
with 3000+ LoC files, then the cleanup debts on the side.

As of 2026-05-23 the following are RESOLVED via refactor-plan.md:
D1 (route fork → Phase 2/3, ADR-028), D3 (closer panel → P5.1,
ADR-011), D4 (legacy `render()` → Phase 3, ADR-027), D5 (subprocess
machinery → Phase 3), D7 (docstring drift → Phase 3).
