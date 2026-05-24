# Improvement Opportunities

Net-new value proposals. Distinct from `tech-debt.md` (which is debt
owed against current behaviour). Items here would add a capability,
improve a metric, or open a new option — not just bring the code in
line with existing intent.

Seeded from the CONSOLIDATED ATTACK SET in `/ai/onboarding-qa.md`
(lines 421-461) — those 17 items are the prioritised improvement
opportunities the user surfaced in the brainstorm. Plus additional
items from the code reads.

Effort scale: S < 1 day, M = 2-5 days, L = 1-2 weeks, XL > 2 weeks.

Each entry: **What to add**, **Why valuable**, **Effort**, **Where
it lands in code**.

---

## CONSOLIDATED ATTACK SET (from onboarding-qa.md, ordered by user)

**Status 2026-05-23:** All 17 items (O1–O17) below are IN-FLIGHT in the
W1/W2/W3 worktree work locked in refactor-plan.md Part E:
- **W1**: O1, O2, O3, O4, O5, O6, O14, O15, O17 (P3.1–P3.6, P5.3, P5.4, P6.1)
- **W2**: O7, O8, O9, O10, O11, O12, O13 (P3.7, P4.1–P4.4, P5.1, P5.2)
- **W3**: cleanup + god-file splits + docs (Phase 2-3, Phase 4, Phase 6)

Entries are kept here for completeness; they should be considered
in-flight rather than open opportunities. Do not seed new work from
this section while the worktree work is open.


### Long-form rewrite (items 1-7)

#### O1. Add `word_count` field to section-body JSON output
- **What:** Section-body LLM emits `{narration, sentences, word_count, …}`.
  Validator uses ACTUAL count (computed) as hard gate; emitted-vs-actual
  delta becomes telemetry only.
- **Why:** Forces the model to count its own words (anchoring effect).
  Reduces length-instruction-following failures that produced jobs
  `a734babb` / `7743ca76`.
- **Effort:** **S**.
- **Where:** `pipeline/llm/rewrite_long_form.py` — extend
  `_SECTION_BODY_PROMPT_TEMPLATE` (line 275) + the response schema
  in `_call_section_body_llm` (line 880).

#### O2. Tighten gates to per-section ±10% / total ±15%
- **What:** Replace `HARD_FLOOR_FRAC=0.50` and `HARD_SECTION_FLOOR_FRAC=0.40`
  with the locked ±10% / ±15% values per onboarding-qa Q59.
- **Why:** The current 50%/40% floors fire too late — by the time
  they trigger the render has burned ~30 min of compute and TTS
  spend. Tighter gates fail faster + trigger granular retry sooner.
- **Effort:** **S**.
- **Where:** `pipeline/critic_long_form.py:222-225`.

#### O3. Outline sum-check
- **What:** After outline returns, validate
  `sum(section.target_words)` is within ±15% of total target. Retry
  outline once with error in prompt; hard-fail if 2nd attempt also
  wrong.
- **Why:** Catches imbalance at outline time instead of after N
  parallel section bodies fan out (the F3 / F8 fragility in
  known-fragility.md).
- **Effort:** **S**.
- **Where:** `pipeline/llm/rewrite_long_form.py:617`
  (`_call_outline_with_retry`).

#### O4. Flip `reasoning_effort` back to `medium` for `rewrite_long_form`
- **What:** Change
  `_DEFAULT_REASONING_EFFORT_BY_STAGE["rewrite_long_form"]` from
  `minimal` to `medium`.
- **Why:** Per onboarding-qa Q57 research, minimal-effort models can't
  count words while emitting. Saves cost but trades length-following
  for it — wrong tradeoff for an MVP that hasn't shipped a single
  clean render. The token-budget headroom is already at 64k (line 220).
- **Effort:** **S** (one constant + verify max_tokens headroom).
- **Where:** `pipeline/llm/cli.py:318`.

#### O5. Replace "Do NOT pad with filler" with outline-LLM-written `quality_goal`
- **What:** Remove the negative-framing block from
  `_SECTION_BODY_PROMPT_TEMPLATE`. Add `quality_goal` per section to
  the outline schema. Section-body prompt receives the goal verbatim.
- **Why:** Per locked principle 5 (onboarding-qa line 470):
  "Negative-framing in prompts often amplifies X." Positive goals
  ("escalate tension", "reveal twist") direct the model better.
- **Effort:** **S** (prompt + schema edit).
- **Where:** `pipeline/llm/rewrite_long_form.py:275` +
  outline-prompt template + schema.

#### O6. Iterative-extend retry for short sections
- **What:** When a section fails its length gate, retry with
  "expand to N words by adding more source detail" + the failed draft
  itself. 1 attempt; hard-fail if 2nd also wrong.
- **Why:** Current retry regenerates from scratch — wastes the
  partial work, doesn't preserve the narrative voice the model
  already chose, and the model has the same blind-spot on the second
  try.
- **Effort:** **M** (new prompt path, cache prior draft).
- **Where:** `pipeline/llm/rewrite_long_form.py:1049-1081` (retry
  loop in `_attempt`).

#### O7. Drop writeback 80% duration floor; keep only sanity checks
- **What:** Writeback verification becomes "mp4 has valid video +
  audio streams." Drop the duration policer.
- **Why:** Once O1–O6 land, duration in band is a natural consequence.
  The duration gate (which fired on job `7743ca76`) is then noise.
- **Effort:** **S**.
- **Where:** `cloud/render-worker-v2/entrypoint.py` (writeback gate).

### Image-gen (items 8-9)

#### O8. Replace klein refiner with Z-Image-Turbo refiner
- **What:** Single-target rewrite of `pipeline/images/prompt_refiner.py`.
  Output 100-200 word structured prompt per beat (vs today's 4-10
  word `refined_visual`). 8-section template per Z-Image-Turbo
  research (Q67). Drop BFL composition tokens; drop Qwen3-tuning;
  positive-only framing.
- **Why:** Production renders against Z-Image-Turbo today. Klein
  refiner output is in the wrong shape for z-turbo's documented
  sweet spot. Direct quality lift.
- **Effort:** **M** — single-target rewrite, no klein backward-compat;
  bump `REFINER_VERSION` (line 86).
- **Where:** `pipeline/images/prompt_refiner.py`.

#### O9. Per-beat retry with image-quality validator
- **What:** Post-gen luminance/variance check. If validator says
  "low quality," retry once with stronger prompt. If still low,
  hard-fail the beat. The 10% threshold becomes post-retry, not
  first-failure.
- **Why:** Aligns with locked principle "gates should hit and retry,
  that's it" (onboarding-qa line 466). Today's
  `_PER_BEAT_FAILURE_THRESHOLD=0.10` is a first-failure kill —
  one cloud incident → whole short fails.
- **Effort:** **M**.
- **Where:** `pipeline/render/visualize/ai_beat_slideshow.py:90,
  492, 503, 515`.

### Cast (item 10)

#### O10. Structured per-character cast schema
- **What:** Output structured fields per character (age, hair, build,
  clothing, signature prop). Beat prompts literally prepend the spec
  verbatim. Add secondary characters when story requires (AITA
  antagonist, mythology supporting cast).
- **Why:** Character consistency across beats. Today's
  `character_description` (single string in `spec.extra`) is
  paraphrased by each beat prompt → drift across slideshow.
- **Effort:** **M** (cast prompt + schema + beat-prompt assembly).
- **Where:** `pipeline/render/spec_enrich.py:214` (writes),
  cast LLM stage prompt, `ai_beat_slideshow.py` prompt assembly.

### TTS (item 11)

#### O11. Verify + fix `_synth_cloudrun_indicf5` ref_audio_text shipping
- **What:** Trace the HTTP request body actually sent to the IndicF5
  cloud service. Confirm `ref_audio_text` is in the payload.
- **Why:** Suspected root cause of HindutavaAnimated Hindi gibberish
  per onboarding-qa Q71.
- **Effort:** **S** (1 hour to verify; 1 hour to fix if missing).
- **Where:** `pipeline/tts/cloudrun.py:813-840`.

### Compose (item 12)

#### O12. Port closer panel to overlay plugin slot
- **What:** New `pipeline/render/overlays/closer_panel.py` as
  `OverlayProducer`. Toggled by `spec.chapter_cards` or new
  `spec.closer_panel`. Delete the legacy dead code in
  `compose.py:711-719`.
- **Why:** Restores closer-panel functionality (today dead). Matches
  the new engine architecture (`contracts.py:280-330`).
- **Effort:** **M**.
- **Where:** new file + `pipeline/compose.py:711-719` deletion.

### Captions (item 13)

#### O13. Wire `spec.caption_style` end-to-end + hard-fail import + Devanagari fonts + density gate
- **What:** 4 sub-items:
  - Wire `spec.caption_style` fully through overlay producer + compose mux.
  - Hard-fail (not warn) when caption import breaks.
  - Add Devanagari fonts to `cloud/render-worker-v2/Dockerfile`.
  - Add post-render caption density gate.
- **Why:** Captions are a Q9 reliability pain item ("renders 'succeed'
  but ship with wrong captions"). Per-channel coverage today is
  best-effort, especially Hindi.
- **Effort:** **M**.
- **Where:** `pipeline/render/overlays/word_caption_pngs.py`,
  `pipeline/render/overlays/sentence_caption_ass.py`,
  `cloud/render-worker-v2/Dockerfile:29-38`, new test/gate.

### Source fetching (item 14)

#### O14. Per-niche multi-source config
- **What:** Variant YAMLs declare their source adapters. LLM rewrite
  synthesizes across N raw materials (multiple Reddit + Wiki +
  archive fetches → one engaging script). Per onboarding-qa Q43:
  "Picking and rewriting should be ONE LLM call — give it raw
  fetches, get back a script."
- **Why:** Q31 + Q42 — current rewrite is treated as authoring;
  intent is that LLM is an *editor on real material, not an author
  from nothing*. Multi-source mixing is the natural extension.
- **Effort:** **M-L** (schema + adapter wiring + per-niche rollout
  + rewrite prompt updates).
- **Where:** `pipeline/variants/<channel>/<niche>.yaml`, fetch
  adapters (subreddit / wiki / today-in-history / etc.), rewrite
  LLM stage.

### Audio mix (item 15)

#### O15. Regression-guard tests for audio mix
- **What:** No active improvement. Add tests so no code change
  worsens current audio quality (loudness, peak, dynamic range,
  ducking depth).
- **Why:** Per onboarding-qa Q75: "It's fine, just make sure
  nothing is worsening." Tests are durable; the audit-data summary
  is not.
- **Effort:** **S** (one test file + fixture).
- **Where:** new `tests/audio/test_mix_regression.py`.

### Observability (item 16)

#### O16. Agent-side access to Cloud Logging / Firestore / GCS / metrics
- **What:** Operational, not code. Verify the agent SA has
  `roles/logging.viewer`, `roles/datastore.viewer`,
  `roles/storage.objectViewer`, `roles/monitoring.viewer` on
  ytfactory-prod-v3. Add a `make grant-agent-readonly` script if
  not present.
- **Why:** Per onboarding-qa Q76: "Make sure you have access to
  everything, you are the one who is going to fix." Diagnosis is
  blocked when the agent (Claude) can't read production logs /
  Firestore directly.
- **Effort:** **S**.
- **Where:** `cloud/iam/` scripts + agent-side credentials.

### Channel onboarding (item 17)

#### O17. Add `scrollpulse` channel
- **What:** `pipeline/channels/scrollpulse.yaml` (auto-pull Reddit +
  split-screen gameplay overlay format) + niche variants + branding
  assets. Per onboarding-qa Q21–Q23.
- **Why:** UI references the channel
  (`web-next/app/app/create/page.tsx:567-575`); pipeline can't render
  it. Partial rollout.
- **Effort:** **S** (YAML) — **M** (full channel: YAML + variants +
  branding + first render).
- **Where:** new YAML + assets.

---

## ADDITIONAL OPPORTUNITIES (from code reads)

### O18. Image-gen batch + select pattern for character consistency
- **What:** Per Z-Image-Turbo research (onboarding-qa Q67 sources):
  "batch of 32+ → pick best 'shots from same photoshoot'" pattern.
  Generate N panels per beat, score on a "consistent with character
  spec" rubric (could be LLM-based), keep the best one.
- **Why:** Character consistency is the #1 visible failure mode of
  AI-image slideshows. No LoRA approach (model is fixed per Q46);
  batch-select is the documented workaround. Onboarding-qa open
  thread (line 488) flags this as "v2 addition."
- **Effort:** **L** (the GPU-spend implications matter; need a
  cost-vs-quality A/B before locking in).
- **Where:** `pipeline/render/visualize/ai_beat_slideshow.py` —
  per-beat loop becomes per-beat-batch loop.

### O19. Outline-LLM `quality_goal` field (the structural shape)
- **What:** Extend the outline schema with a `quality_goal: str` per
  section ("escalate tension", "reveal twist", "set scene"). The
  schema lives at `pipeline/llm/script_schema.py` (referenced in
  `pipeline/render/video.py:60`).
- **Why:** Enables O5; once written, the per-section body prompt
  knows what good looks like for THIS section without needing the
  body LLM to infer it.
- **Effort:** **S** (schema bump + outline prompt template).
- **Where:** `pipeline/llm/script_schema.py` +
  outline prompt in `rewrite_long_form.py`.

### O20. Per-render OTel trace for diagnostic
- **What:** Per-stage spans (rewrite outline / rewrite N sections /
  cast / images / tts / asr / compose / upload) emitted via OTel,
  visible in Cloud Trace. Today the stage-overlap timing exists in
  Firestore as substage pills, but cross-stage correlation requires
  manual log grep.
- **Why:** "Why did this render take 38 minutes when the median is
  22?" → currently no answer without grepping logs.
- **Effort:** **M** (instrument every stage entry/exit).
- **Where:** `pipeline/render/short_engine.py`, `long_engine.py`,
  per-plugin entrypoints, `cloud/render-worker-v2/entrypoint.py`.

### O21. Per-render "would have shipped" tag in Firestore
- **What:** Boolean field `would_have_shipped_pre_gates: bool` set
  TRUE if every stage produced an output (even if gates killed it).
  Diagnostic value: "of N renders this week, M would have shipped
  but were gated, K had real errors."
- **Why:** Today the only signal is "succeeded / failed." Distinguishing
  gate-fails from code-fails is critical to prioritising fixes (per
  onboarding-qa Q50: "A MIX — some die at gates, others crash with
  real errors").
- **Effort:** **S** (one bool + populate at relevant exit points).
- **Where:** `cloud/render-worker-v2/entrypoint.py` writeback.

### O22. Granular-retry framework as a first-class abstraction
- **What:** Today retry is bespoke per stage (section-body retry in
  rewrite, per-beat retry NOT YET added in image-gen, etc). Make
  retry a contract on each plugin slot — every plugin declares its
  "smallest retriable unit" and the engine drives the retry.
- **Why:** The locked principle (line 466) makes retry a universal
  mechanic. Embedding it bespoke per stage creates inconsistency
  AND blocks attack-set items 6, 9 from sharing infrastructure.
- **Effort:** **L** (cross-cutting refactor — but worth doing once
  the per-stage fixes O6 + O9 are in place to validate the shape).
- **Where:** `pipeline/render/contracts.py` (extend Protocols),
  each plugin impl.

### O23. End-to-end smoke test triggered on every push
- **What:** CI job that runs a real-mode short render against a
  fixture topic, asserts mp4 lands + duration in band + captions
  density above threshold. Costs ~$0.50/render in cloud spend, run
  on main-branch push only.
- **Why:** MVP target per onboarding-qa Q26 is "one clean automated
  end-to-end render." No CI tests that target. Today's tests are
  unit; end-to-end is run manually via /app/create.
- **Effort:** **M** (test harness + cloud auth in CI).
- **Where:** new `.github/workflows/e2e-render.yml`.

### O24. Stale `pipeline/render/video.py` docstring rewrite — IN-FLIGHT 2026-05-23

**Status:** IN-FLIGHT 2026-05-23 — refactor-plan.md Phase 3 deletes
the legacy `render()` and rewrites the docstring (tech-debt D7,
ADR-027).

- **What:** Rewrite the module docstring to describe CURRENT
  behaviour (engine dispatch via `render_via_engines()`), not the
  pre-bigbang Slice 2 state.
- **Why:** Charter principle "the function body is authoritative"
  but a misleading docstring still costs reader-time on every visit.
- **Effort:** **S**.
- **Where:** `pipeline/render/video.py:1-43`.

### O25. Audit-able `RenderSpec.extra` schema
- **What:** Type-check `spec.extra` keys via a TypedDict or pydantic
  model. Today it's `dict[str, Any]` — any typo silently produces
  empty character lock. Failure mode is per-render character drift
  with no diagnostic.
- **Why:** Eliminates a fragility class (known-fragility F2, F16).
- **Effort:** **M** (gather every key in use; codify; refactor
  callers).
- **Where:** `pipeline/render/spec.py` (RenderSpec definition).

### O26. Make `populate_render_extras` non-optional via engine-internal call
- **What:** Move `populate_render_extras(spec, script)` from being a
  short_engine.py call (line 152) to an `engine.pick_engine()`
  internal — guarantee every entry through engines triggers it.
- **Why:** Eliminates the "tests pre-populate extras and silently
  use stale values" failure mode (F16).
- **Effort:** **S**.
- **Where:** `pipeline/render/engine.py`.

---

### O27. Per-channel multi-source mixing — variant YAML source-adapter declaration

- **What:** Per-niche multi-source config at
  `pipeline/variants/<channel>/<niche>.yaml`. Each variant declares
  which source adapters apply (e.g. `aita_animated → reddit_aita`;
  `krishna_leela → wiki_mahabharat + scripture_text`). Rewrite stage
  receives `raw_sources: [{adapter, payload}, ...]` and synthesizes
  across them in one LLM call (per ADR-014 + ADR-026).
- **Why:** Today's source-adapter wiring is channel-level (one adapter
  per channel YAML). A niche cannot mix adapters; a channel cannot
  vary adapter mix per variant. Locks in editor-on-real-material
  (ADR-025) at the source layer. Variant-level config keeps the
  decision at authoring time (not per-render UI cost) and reuses
  across N renders of the niche. Sibling to O14 (which named the
  shape during the brainstorm); this entry anchors the implementation
  location to refactor-plan P5.3.
- **Effort:** **M-L** (schema + adapter wiring + per-niche rollout +
  rewrite prompt updates).
- **Where:** `pipeline/variants/<channel>/<niche>.yaml` (declare
  `source_adapters: [list]`); `pipeline/llm/rewrite.py` +
  `pipeline/llm/rewrite_long_form.py` (accept raw_sources list);
  per-channel adapter registry under
  `pipeline/sources/<adapter>.py`.

---

### O28. Post-render caption density gate

- **What:** After compose, verify the % of audio time covered by
  caption overlays. Hard-fail if below a per-channel threshold (e.g.
  80% of audio with caption visible). Caption density is computed
  against audio time, not render time — silent gaps (b-roll, music
  beds) don't count toward the denominator.
- **Why:** Captions are a Q9 reliability pain item — "renders 'succeed'
  but ship with wrong captions / frozen frames / missing audio."
  Today there is no gate that verifies captions actually overlay the
  audio; a render can ship with 0% caption coverage and no signal.
  Sibling to O13's caption sub-item; this entry anchors the gate
  shape to refactor-plan P5.2 and ADR-012.
- **Effort:** **S** (new gate + new test fixture covering Hindi
  (HindutavaAnimated) + English (HistoryRecapped) renders).
- **Where:** `pipeline/render/compose/compose.py` (post-mux
  verification); per-channel threshold in
  `pipeline/channels/<channel>.yaml`; test at
  `tests/render/test_caption_density_gate.py`.

---

### O29. `auth_setup.sh` token-refresh after Build SUCCESS (2026-05-23)

- **What:** Expose a `refresh_adc_token` function in
  `cloud/_shared/auth_setup.sh`. In each `cloud/<svc>/deploy.sh`,
  call it once after `Build SUCCESS` and before the `gcloud run
  deploy` step. The current pattern issues a 1-hour token at script
  start; large builds (z-image-turbo at 50+ min) + slow Cloud Run
  image imports push past the TTL, the deploy poll fails with
  `ACCESS_TOKEN_EXPIRED`, and the script exits non-zero even though
  Cloud Run completes the rollout server-side.
- **Why:** False-negative on deploy.sh exit code makes the operator
  doubt the deploy and either re-run (wasting 50 min) or chase a
  phantom bug. Fix is small + structurally clean.
- **Effort:** **S** (5-line shell change per script; ~30 min including
  testing one deploy through).
- **Where:** `cloud/_shared/auth_setup.sh` + every `cloud/<svc>/deploy.sh`.
- **Incident:** 2026-05-23 — z-image-turbo deploy died on token
  expiry mid-poll; rev still shipped. Same risk exists for any
  large-image deploy.

### O30. Preflight render in every `deploy.sh` post-rollout (2026-05-23)

- **What:** After `gcloud run deploy` returns, fire a minimal
  preflight that exercises the new behaviour:
  - **render-worker-v2**: trigger a tiny preflight render via the
    control plane and tail Cloud Logging for `stage.start` within
    60s; exit non-zero if absent.
  - **TTS/ASR servers**: POST a 5-second fixture and verify the
    response shape includes telemetry-emitted fields (proves
    `_tel_track_io` actually wired).
  - **image-z-image-turbo**: POST a 64×64 prompt; verify the
    response + that `image.gen` event with body capture appears in
    Cloud Logging.
- **Why:** Eliminates the "rev-healthy = deploy complete" gap (known-
  fragility F25). 3-5 min of post-deploy spend; saves hours when
  the deploy is structurally broken. Template-able as a shared
  `cloud/_shared/post_deploy_verify.sh`.
- **Effort:** **M** (~1-2 hours per deploy.sh, but template-able to
  one shared script + per-service config).
- **Where:** new `cloud/_shared/post_deploy_verify.sh`; sourced by
  each `cloud/<svc>/deploy.sh` at the end.
- **Incident:** 2026-05-23 — declared "all 6 services telemetry-aware
  ✓" off rev-healthy alone; the actual telemetry path was broken in
  4/6 services (1 hard, 3 silent). ~3 hours of operator time lost.

### O31. Switch deploy.sh from streaming-log mode to async-poll mode (2026-05-23)

- **What:** Several `cloud/<svc>/deploy.sh` files use `gcloud builds
  submit` without `--async`. When run under SA-impersonation auth,
  the impersonated SA may lack `logging.privateLogEntries.list` and
  the CLI dies with "This tool can only stream logs if you are
  Viewer/Owner …" — even though the build itself succeeds
  server-side. The `render-worker-v2/deploy.sh` already uses the
  `--async + manual polling` pattern and is immune. Backport the
  same pattern to the other deploy.shes.
- **Why:** Eliminates the false-negative exit code that comes from
  log-streaming permission gaps. The build runs the same way either
  way; only the CLI's visibility into it changes.
- **Effort:** **S** per script (~20 min × 4 scripts = ~80 min).
- **Where:** `cloud/asr-whisper/deploy.sh`,
  `cloud/tts-chatterbox/deploy.sh`,
  `cloud/tts-indicf5/deploy.sh`,
  `cloud/editing-agent/deploy.sh`.
- **Incident:** 2026-05-23 — asr-whisper deploy.sh exited 1; build
  was SUCCESS server-side.

### O32. `deploy.sh` "Poll URL" output format fix (2026-05-23)

- **What:** Each `cloud/<svc>/deploy.sh` prints a "Poll URL" pointing
  at the Cloud Build console. The URL uses `?region=...` as a query
  param; the console requires `;region=...` as a matrix parameter
  *before* the build ID. The printed URL 404s in the browser.
- **Why:** Operator clicks the link expecting build progress, gets a
  console 404. Wasted seconds become wasted minutes when it happens
  every deploy.
- **Effort:** **S** (one-line change per deploy.sh; ~5 min × 5 scripts).
- **Where:** every `cloud/<svc>/deploy.sh`.

### O33. Move reconciler off laptop-LaunchAgent onto Cloud Scheduler (2026-05-24)

- **What:** Today the job reconciler runs on the laptop via
  `com.ytfactory.job-reconciler.plist` at 5-min intervals. If the
  laptop is closed, asleep, or rebooted, stuck jobs accumulate
  forever. Move the trigger to Cloud Scheduler → Cloud Run cron job
  so it survives laptop state.
- **Why:** F27's investigation showed the reconciler hadn't run on
  this laptop AT ALL since the LaunchAgent was never installed. Stuck
  jobs are a "operator forgot to install the plist on machine N"
  class today; laptop-independent removes the bootstrap step.
- **Effort:** **M** (~2-3 h). Need: enable `cloudscheduler.googleapis.com`,
  small Cloud Run JOB that imports `control.core.reconciler`, Cloud
  Scheduler cron every 5 min, decommission the laptop plist.
- **Where:** new `cloud/reconciler-cron/` service + Scheduler config.
- **Incident:** 2026-05-24 — F27, job `82682e8e` stuck 24h+.

### O34. Bootstrap step: deploy `firestore.indexes.json` on fresh GCP project (2026-05-24)

- **What:** Add `scripts/bootstrap_gcp_project.sh` (or a runbook
  line) that runs `firebase deploy --only firestore:indexes` (or
  gcloud equivalent) when a new project comes online. Today
  `firestore.indexes.json` is the spec but nothing enforces it
  matches deployed state. `gcloud firestore indexes composite list`
  showed 0 indexes on `ytfactory-prod-v3` before 2026-05-24.
- **Why:** Without it, every Firestore-backed feature depending on a
  composite index silently 400s on first call. The web UI handles
  it gracefully (operator banner); the reconciler silently no-ops.
- **Effort:** **S** (~30 min).
- **Where:** `scripts/bootstrap_gcp_project.sh` (new) + setup docs.
- **Counter-test:** verification step: `diff <(gcloud firestore
  indexes composite list --format=json) firestore.indexes.json`
  (normalised).
- **Incident:** 2026-05-24 — F27, /api/queue Completed column dead.

### O35. `verify_web_runner.sh` — distinguish absent from unreadable (2026-05-24)

- **What:** Every `_check_*` helper in `cloud/iam/verify_web_runner.sh`
  uses `gcloud ... 2>/dev/null || true` which silently swallows
  PermissionDenied and reports the binding as MISSING. Replace with
  explicit error-handling: capture gcloud's exit code; if non-zero
  AND the stderr indicates permission denied (rather than "not found"
  or auth missing), emit a SPECIFIC error and exit with code 3
  ("cannot verify — grant the deployer SA roles/iam.securityReviewer
  or run as project owner"). Don't let "I can't tell" masquerade as
  "absent".
- **Why:** F28 — false MISSING report blocked a deploy whose bindings
  were actually present. Wasted 30+ min on a non-bug. Violates
  `feedback_silent_fallback_unshippable_output`.
- **Effort:** **S** (~45 min). Per-helper exit-code check + a unit
  test stubbing gcloud-read to fail with PermissionDenied.
- **Where:** `cloud/iam/verify_web_runner.sh` +
  `tests/test_cloud_deploy_hardening.py`.
- **Incident:** 2026-05-24 — `cloud/web-server/deploy.sh` blocked
  for ~30 min on false MISSING report despite all 8 bindings
  verified present as owner.

### O36. Route long-form image-gen through the refiner (2026-05-24)

- **What:** `pipeline/render/shared/long_form_lib.py::_generate_panel_stills`
  builds the wire prompt via `images.build_full_prompt(refined_visual,
  refined_scene, style_block)` — same shape `ai_beat_slideshow.py`
  (shorts) uses. New `_refine_long_form_panels` helper invokes
  `refine_prompts_batch` once for the panel batch; per-beat empty
  fallback keeps the legacy path for individual slots; whole-batch
  failure RAISES per `feedback_silent_fallback_unshippable_output`.
- **Why:** F29 — the long-form path never reached the refiner, so
  the channel's `style_block`, character continuity, and the
  refiner's "no readable text in image." anti-text prefix were all
  lost. Two consecutive renders shipped with ~58% floating-jersey
  panels.
- **Effort:** **M** — ~224 LoC of new wiring across `long_form_lib.py`,
  `longform_panels.py`, `prompt_refiner.py`, `prompts.py`,
  `entrypoint.py`. Done in `fdc5f65`.
- **Where:** `pipeline/render/shared/long_form_lib.py:598-940` +
  five adjacent files.
- **Counter-test:** `tests/render/shared/test_long_form_refiner_routing.py`
  (6 tests).
- **Status:** **SHIPPED** 2026-05-24 in `fdc5f65`.

### O37. Verb-led anti-text safety SUFFIX (2026-05-24)

- **What:** `pipeline/images/images_cloudrun.py::ANTI_TEXT_SUFFIX` —
  replaced 190-char noun-tag prefix ("Clean surface, unmarked, blank
  jersey, smooth fabric, plain backgrounds, unmarked book covers,
  unlabeled bottles, no signage, no banners, no watermark, no logo,
  no caption, no street signs.") with a 232-char verb-led safety
  clause appended-not-prepended ("Render the scene with no readable
  printed text anywhere in the image, no visible logos on clothing
  or objects, no captions baked into the frame, no street signs or
  banners with readable letters."). Backward-compat aliases kept.
- **Why:** F29 symptom layer — the prior nouns ("blank jersey",
  "smooth fabric", "unmarked book covers", "unlabeled bottles")
  were concrete clothing/fabric/paper-product nouns z-image-turbo
  treated as draw-subjects. Verbs/adverbs don't compete for
  subject attention the way noun tags did.
- **Effort:** **S** (~60 LoC + docstring) — done in `fdc5f65`.
- **Where:** `pipeline/images/images_cloudrun.py:478-560`.
- **Counter-test:** `tests/test_anti_text_suffix_shape.py` (9 tests
  pinning forbidden-noun absence, length ≤250, verb-led, appended-
  not-prepended, idempotent on legacy + refiner + double-apply).
- **Status:** **SHIPPED** 2026-05-24 in `fdc5f65`.

### O38. Per-channel `default_scene_anchor` for scene-only panels (2026-05-24)

- **What:** New top-level field `default_scene_anchor: …` on
  `pipeline/channels/<channel>.yaml`. Threaded through the refiner
  via the new `scene_anchor` parameter (REFINER_VERSION bumped
  v2-zturbo → v3-scene-anchor so cached refined fields auto-
  invalidate at render time via the hash mismatch). Refiner weaves
  the anchor into `refined_scene` when the beat lacks a setting.
- **Why:** On long-form renders, ~75% of panels are scene-only
  (no character token in the brief, just one short sentence like
  "Cloud-filled sky from airplane wing.") — without a setting
  anchor the model has nothing to compose around and falls back to
  the "blank product photo on plain background" attractor. Adding
  a channel-level setting anchor gives the model concrete
  grounding for every scene-only panel.
- **Effort:** **S** (~30 LoC + per-channel YAML entries).
- **Where:** `pipeline/images/prompt_refiner.py` (scene_anchor
  param + rule 3a). `pipeline/channels/mystoriesanimated.yaml` —
  shipped. Pending: `hindutavaanimated.yaml`, `historyrecapped.yaml`,
  `sportsrecapped.yaml`, `cosmosdecoded.yaml`.
- **Counter-test:** `tests/test_prompt_refiner.py` — anchor
  threaded through, hash includes anchor, omitted when unset.
- **Status:** **partially shipped** 2026-05-24 in `fdc5f65`.
  Wiring complete; mystoriesanimated YAML field set; 4 other
  in-rotation channel YAMLs still need their per-channel anchor.

### O39. Z-Image-Turbo `max_sequence_length=1024` to fit refined prompts (2026-05-24)

- **What:** `cloud/image-z-image-turbo/server.py` — pass
  `max_sequence_length=1024` to `ZImagePipeline.__call__`. Default
  is 512.
- **Why:** Our refined prompts + character description + verb-led
  safety SUFFIX can exceed 512 tokens; the SUFFIX is the first
  thing the Qwen3-4B encoder truncates if so, removing the safety
  guard. Tongyi-MAI staff in HF Discussion #8 explicitly: "Locally,
  you can set `max_sequence_length=1024` to accommodate longer
  prompts."
- **Effort:** **S** (1 LoC + comment).
- **Where:** `cloud/image-z-image-turbo/server.py` `/generate` handler.
- **Counter-test:** `tests/test_z_image_turbo_quality_pins.py::
  test_server_passes_max_sequence_length_1024_to_pipe`.
- **Source:** https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8
- **Status:** **SHIPPED** 2026-05-24 in `fdc5f65`.

### O40. Z-Image-Turbo in-domain resolution buckets (2026-05-24)

- **What:** `cloud/image-z-image-turbo/server.py::_ASPECT_DIMS` —
  9:16 = 720×1280, 16:9 = 1280×720 (was 768×1344 / 1344×768).
  Aligned client default in `pipeline/images/images.py::generate`.
- **Why:** Tongyi-MAI staff (QJerry) in HF Discussion #28: "use
  more as long as both width & height are divided by 16 and not
  exceeded 256 pixels fluctuation of 1024 resolution grid (like 768
  ~ 1280)". The prior 768×1344 / 1344×768 buckets were ~64 px
  off-domain on the long side. Side benefit: ~10-12% fewer pixels
  → ~10-12% faster per-panel inference.
- **Effort:** **S** (2 dict entries + 2 default values).
- **Where:** `cloud/image-z-image-turbo/server.py:_ASPECT_DIMS` +
  `pipeline/images/images.py:generate()` width/height defaults.
- **Counter-test:** `tests/test_z_image_turbo_quality_pins.py::
  {test_server_aspect_dims_are_in_domain, test_client_default_width_height_match_in_domain}`.
- **Source:** https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/28
  + https://github.com/SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions
- **Status:** **SHIPPED** 2026-05-24 in `fdc5f65`.

### O41. Z-Image-Turbo `_native_flash` attention backend (~25% speedup) (2026-05-24)

- **What:** `cloud/image-z-image-turbo/server.py::_pipe()` — after
  `enable_attention_slicing()`, call `pipe.transformer.set_attention_
  backend("_native_flash")`. Fails open (try/except logs and keeps
  default SDPA).
- **Why:** Tongyi-MAI HF Discussion #139: "25% faster — saved
  about 52 seconds on a 50-step Full-HD generation" on Blackwell.
  L4 (Ada SM89) lacks FA-3 (Hopper-only) but the "_native_flash"
  backend uses PyTorch SDPA's flash kernel which IS available on
  Ada. Cost ~0, no new dependency.
- **Effort:** **S** (~6 LoC including the try/except + log).
- **Where:** `cloud/image-z-image-turbo/server.py::_pipe()`.
- **Counter-test:** `tests/test_z_image_turbo_quality_pins.py::
  test_server_sets_native_flash_attention_backend`.
- **Source:** https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/139
  + https://huggingface.co/docs/diffusers/main/en/optimization/attention_backends
- **Status:** **SHIPPED** 2026-05-24 in `fdc5f65`.

---

## Prioritisation guidance

Per onboarding-qa Q27 ("no channel meaningfully ahead") + Q28 ("MVP =
everything working"), the right ordering is whatever unblocks the
documented failure classes fastest. Recommended top 5:

1. **O4** (reasoning_effort medium) — 1-line change, immediate impact on
   long-form length-following.
2. **O2** (gate thresholds ±10% / ±15%) — 2-constant change, makes
   gates fire on real failure not catastrophic failure.
3. **O8** (Z-Image-Turbo refiner) — direct visible image-quality lift;
   pays for itself on every short.
4. **O11** (IndicF5 verify) — unblocks one whole channel
   (HindutavaAnimated) if it's the documented cause.
5. **O1 + O3** (word_count emission + outline sum-check) — together
   close the long-form length-following loop properly.

After those 5, the granular-retry shape (O6 + O9) becomes
infrastructure for everything else.
