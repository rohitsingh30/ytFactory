# /update-docs — learnings index

One-line entries per regression caught during a run. Detail lives in
sibling topic files in this dir or in the dual-saved memory/project doc.

## CLASS-OF-BUG (rule changes to SKILL.md)

- 2026-05-10 — **doc-sweep rule.** Dual-save (memory + one project
  doc) wasn't enough — findings live across many docs. SKILL.md now
  has Section 4D ("Sweep related docs") + Quality gate 8 that blocks
  the report unless every related doc is listed. Detail:
  `learnings/doc_sweep.md`. Memory:
  `feedback_update_docs_doc_sweep.md`.
- 2026-05-10 — **Three new cross-channel rules from the cloud-skill
  retirement** (saved to memory + project docs, surfaced here so
  future `/update-docs` runs find the precedent):
  1. `feedback_admin_panel_first.md` + `docs/admin_panel_first.md` —
     ops/observability surfaces belong in web-next, not CLI.
  2. `feedback_skill_harness_drift_audit.md` — periodic grep of
     `.claude/skills/` for harness signals (recipe in
     `make-skill/learnings/heuristics.md §4`).
  3. `feedback_renderer_owns_universal_pre_steps.md` — if every
     `/make-*` does step X before render handoff, X goes in the
     renderer entrypoint.

## ONE-OFFs noted (no project-doc needed)

- 2026-05-11 — **Selector edits need post-edit grep-verify.** The
  LIKE_SELECTORS dislike-guard fix recurred as a regression mid-
  session — the file briefly went back to the broken substring-only
  selectors before being re-fixed. Suspected cause: I claimed the
  edit was applied without re-grepping the file. Selectors don't
  have unit-test coverage, so an inverted match silently ships and
  disengages until a live probe catches it. **New discipline:** for
  any LIKE_SELECTORS / SUBSCRIBE_SELECTORS / similar edit that has
  no unit test, follow with
  `grep -n -A6 "^LIKE_SELECTORS = " <file>` immediately and
  visually confirm the new string. Future audit recipe (catch
  siblings):
  `grep -rn "aria-label\*=" pipeline/cross_engage/ web/ control/ | grep -v dislike` —
  any match without an explicit `:not([aria-label*='dislike' i])`
  is a candidate for the same misclassification. Memory:
  `feedback_burner_engage_cloud_v2.md` (extended with 2026-05-11
  recurrence note).

- 2026-05-10 — **Two-code-path-for-same-symptom heuristic.** When
  the user says "still no stats" after a fix lands, the response
  pattern is: don't assume the fix is wrong, look for a SECOND code
  path that produces the same surface signal. The 2026-05-10
  YouTube-stats permanent-fix shipped a `YOUTUBE_API_KEY` wire that
  lit up `/api/dashboard/videos` (cards) but `/api/channels` (gallery)
  was a separate stack with its own cache. Two surfaces both
  rendering "subscriber count" looked like one feature; under the
  hood they were independent endpoints. Sweep recipe:
  `grep -rn "subscriber_count\|youtube_video_count\|total_views" web/ control/ pipeline/schemas/`.
  If repeats, escalate to CLASS-OF-BUG: heuristic for the
  classifier — "find every code path that produces the same UI
  metric BEFORE declaring done". Memory:
  `feedback_stats_two_code_paths.md`. Doc:
  `docs/youtube_stats_refresh.md` § Anti-patterns observed.

- 2026-05-10 — **MEMORY.md size cap raised 24 KB → 48 KB.** The
  initial 24 KB was set when the index was ~40 entries. After
  trimming 5 oversized entries down to ≤200 chars (saved 1.3 KB,
  bringing total to ~45.5 KB), the file was still ~21 KB over the
  old cap. Root cause: 100+ legitimate index entries across 7
  channels + cross-channel + skill self-learnings; structural floor
  is ~40-45 KB. Raised cap rather than fight a healthy growth
  pattern. New-line Q2 discipline (≤200 chars / 1 sentence)
  unchanged. Re-evaluate if index >60 KB OR a single section becomes
  un-screenful — then the right move is structural (split per-section
  indexes into per-section files), not another cap bump. SKILL.md
  Quality gate 3 + CAP HISTORY block updated. Future audit recipe:
  `wc -c ~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md` —
  if approaching 60 KB, draft the structural-split proposal.

- 2026-05-10 — **`image-flux2-klein` warm-pool holds 8 vCPU even with
  `min-instances=0`.** During the cross-engage cloud deploy the
  `ytfactory-web` rollout failed with `Quota exceeded for total
  allowable CPU per project per region` because the always-warm GPU
  services (flux2-klein 8 vCPU + tts-chatterbox 8 vCPU + tts-indicf5
  8 vCPU = 24) plus rolling-deploy double-image overhead pushed past
  the default 20 vCPU quota. Reducing `min-instances` to 0 doesn't
  immediately drop quota usage — Cloud Run holds the warm pool for
  some window. Solved by self-service quota bump (60 vCPU) — see
  `docs/cloud_run_quota_self_service.md`. Not worth a project doc on
  its own; relevant only as backstory for the quota recipe. Future
  audit: if other regions/projects hit the same wall, run
  `gcloud beta quotas preferences create` rather than reducing min
  instances.

- 2026-05-10 — **`control/server_dev.py` was missing the
  `niche_specs_routes` mount.** Only `web/server.py` (the prod
  monolith) wired it. Local dev hitting `POST
  /api/channels/<ch>/niches/draft` returned 404 even when uvicorn was
  up. **Fixed inline** in this session by appending the import +
  `app.include_router(niche_specs_router_v2)` block. Future audit
  recipe to catch sibling missing routers:
  `diff <(grep "include_router" control/server_dev.py | sed 's/.*router as //;s/).*//;s/_router_v2//') <(grep "include_router" web/server.py | sed 's/.*router as //;s/).*//;s/_router_v2//')`
  — any router in web/server.py but not server_dev.py is a candidate.
  Pre-existing — not unique to this session.
- 2026-05-10 — **doc-sweep continuation: stale `ytfactory-control`
  / `ytfactory-prod` (no `-v2`) refs across 4 docs.** After fixing
  `docs/architecture.md` lines 256-269 (replaced stale gcloud
  block with pointer to `cloud/web-server/deploy.sh` — which IS
  the source of truth for env vars + concurrency + secrets), ran
  `grep -rnE "ytfactory-control|ytfactory-prod[^-]" docs/`. Found
  3 sibling runbooks describing pre-Phase-4 state with stale
  project name throughout: `docs/cloudrun_higgs.md`,
  `docs/cloudrun_persistent_weights.md`, `docs/deploy.md`. **Fixed
  by adding status banners** (kept body intact for historical
  accuracy — original deploy commands were correct at the time;
  banner says "substitute --project=ytfactory-prod-v2 throughout
  if re-deploying"). Did NOT touch
  `docs/laptop_nuclear_cleanup_2026_05_09.md` — historical
  post-mortem, dates stamp the period it describes. Audit recipe
  for the next sweep:
  `grep -rnE "ytfactory-control|ytfactory-prod[^-]" docs/ --include='*.md' | grep -v "retired\|legacy\|pre-Phase-4\|substitute\|original\|historical"`
  — anything left over without a "this is historical" qualifier
  is a candidate.
- 2026-05-10 — **operator decision-fatigue UX cue.** When the user
  responds "idk" / "not sure" to a multi-option ask, don't restate
  the open items or re-ask open-ended. Surface a compact decision
  matrix (table: item / what-it-is / my-recommendation) + ONE focused
  question. Worked well in this session (user picked "commit
  everything in one tidy commit" within one turn). Single
  observation; escalates to CLASS-OF-BUG if the pattern repeats and
  needs a project doc.
- 2026-05-10 — `pipeline/cloud/health.py::_probe_one` uses a 15 s
  HTTP timeout, but image services (FLUX2-klein, Z-Image-Turbo) have
  5-7 min cold-load. Cold image services currently classify as
  `red / timeout`. Future fix: when probe times out AND
  `svc.kind == ServiceKind.IMAGE`, classify `yellow` + flag
  `probe_timeout_likely_cold`. Sweep recipe:
  `grep -n "_probe_one\|_classify\|HTTP_TIMEOUT_S" pipeline/cloud/health.py`.
  TODO comment added in `_classify`. Escalate to CLASS-OF-BUG on
  second observation.
- 2026-05-10 — `clone-video-worker` returns Google-frontend 404 on
  `/healthz` even though `cloud/clone-video-worker/server.py`
  defines `@app.get("/healthz")`. Stale URL or skipped redeploy.
  Surfaced by the new `/api/cloud/health` endpoint. Audit recipe:
  `gcloud run services describe ytfactory-clone-video-worker --region=asia-southeast1 --format='value(status.url)'`
  vs `.env` `CLOUDRUN_CLONE_VIDEO_URL`. Pre-existing — outside the
  scope of the admin-panel work, just surfaced by it.
- 2026-05-10 — `.claude/skills/clone-video-format/SKILL.md` had
  stale `/cloud-health clone-video-worker` references (a retired
  skill). **Fixed inline** during this session — replaced with
  `curl /api/cloud/health` + admin-tab pointer. Future audit
  recipe to catch siblings:
  `grep -rn "/cloud-health\|/cloud-cost\|/warm-cloud\|/deploy-cloud-service" .claude/skills/ docs/ pipeline/ control/ web/ web-next/ cloud/ | grep -v "past tense\|memory\|history\|was deleted\|was retired\|2026-05-10"`.
- 2026-05-10 — same sweep surfaced **3 more sibling skills** with
  stale refs: `.claude/skills/{voice-bench,image-edit,parallel-render}/SKILL.md`.
  Stale refs **fixed inline** this session (descriptions + body
  swapped to `POST /api/cloud/warm` / `GET /api/cloud/health` /
  `gcloud builds submit` per playbook). **The 3 skills themselves
  are still candidates for the harness-drift audit** in
  `feedback_skill_harness_drift_audit.md` — `voice-bench` does
  Claude-driven scoring/report-writing (likely keeps), but
  `parallel-render` (ThreadPoolExecutor wrapper around
  `pipeline/render/*.py`) is shell-shaped and could collapse to a
  one-page batch dispatcher in `scripts/`. `image-edit` needs a
  closer read. **Out of scope this session** — flagged for next
  audit; user opt-in required before retiring.
- 2026-05-10 — `tests/test_render_shorts.py::test_qc_retries_ship_anyway_and_ip_adapter`
  asserts `tlm.track.call_count == 4` but production code now emits 8
  (4 image_attempt + 4 stage_done events from the latency-tracking
  rollout). Pre-existing on baseline (confirmed via `git stash`),
  not caused by the Voice/Song flip change. Two-line fix: bump the
  assertion to 8 OR scope it to attempts-only via
  `[c for c in m.tlm.track.call_args_list if c.args[0] == 'image_attempt']`.
- 2026-05-10 — `docs/channel_layout.md` line 125 (`mkdir <channel>/ and
  write <channel>/config.yaml`) is stale relative to the 2026-05-10
  "nuclear cleanup" that consolidated channel YAMLs to
  `pipeline/channels/<slug>.yaml` (per `pipeline.channels.CHANNELS_CONFIG_DIR`).
  Layout doc needs a refresh — out-of-scope for the Voice/Song flip
  task, flagged for next session.
- 2026-05-10 — `docs/architecture.md` line 3 had stale prod URL
  (`ytfactory-control-…us-central1` / project `ytfactory-prod`).
  Actual prod is `ytfactory-web-7hwnzw7lya-as.a.run.app` /
  `ytfactory-prod-v2` / `asia-southeast1` (Phase-4 consolidation).
  **Fixed inline** during the jobs_snapshot_unification work — single
  edit, low risk. Future audit: every doc that names a Cloud Run URL
  should grep for `ytfactory-control` and `ytfactory-prod[^-]` to
  catch the same drift.
- 2026-05-10 — **Web perf pass (dashboard latency).** User report
  "every page on the live site takes 5-10 seconds" — bottleneck was
  sequential GCS waterfalls per dashboard tick (3 separate scans:
  `control.core.storage.list_upload_records`,
  `web/server.py::_iter_all_uploads`, `pipeline/research/youtube._load_cache`'s
  HEAD-on-every-call). Fix shipped 4 cross-channel rules:
  (1) TTL cache + ThreadPoolExecutor for every GCS-walking polled
  handler — `feedback_dashboard_gcs_waterfall.md`;
  (2) `useStaleWhileRevalidate` (sessionStorage) instead of raw
  `useState+useVisiblePoll` for polled pages —
  `feedback_browser_swr_for_polled_pages.md`;
  (3) `Server-Timing` + `Cache-Control` middleware as default —
  `feedback_server_timing_default.md`;
  (4) `control/storage.py` shim reverts mid-session, cross-module
  bust by explicit import instead — `feedback_control_storage_shim_reverts.md`.
  Project doc: `docs/web_perf_pass_2026_05_10.md`. Sweep recipe to
  catch siblings before they regress (run after any new GCS handler):
  `grep -rnE "for [a-z_]+ in (cli|client)\.list_blobs|blob\.reload\(\)|download_as_bytes" pipeline/ control/ web/ --include='*.py'`.
- 2026-05-10 — **`test_mirror_to_gcs_success` order-brittle (pre-existing).**
  `from control import storage` resolves via parent-package attr (set
  by an earlier test that loaded the real module) BEFORE `sys.modules`
  patch lookup, so `patch.dict("sys.modules", ...)` is a no-op when
  the test runs after `test_routes_dashboard.py`. Confirmed
  pre-existing via `git stash` round-trip on the perf-pass changes.
  Workaround: run `tests/test_upload_youtube.py` in isolation. Real
  fix when prioritized: `patch("control.storage", mock, create=True)`.
  Memory: `feedback_test_isolation_control_storage_attr.md`.
- 2026-05-10 — **`web-next/public/` directory was missing.** Cloud
  Build for ytfactory-web-next failed at `COPY web-next/public ./public`
  step because the dir doesn't exist (was never created or got deleted
  in prior cleanup). **Fixed inline** by `mkdir -p web-next/public &&
  touch web-next/public/.gitkeep`. Future audit: if web-next deploy
  fails on the same COPY, check `ls web-next/public` before assuming
  Dockerfile is broken. Recipe: `git ls-files web-next/public/ | head`
  should always have ≥1 entry.
- 2026-05-10 — **cake-orch end-to-end shipped (8 smokes, 7 commits).**
  First production website-driven render exposed eight separate gaps
  in the post-cutover infra. Persisted as 7 cross-channel feedback
  files + 3 new docs (`docs/llm_orchestrator.md`,
  `docs/llm_backend_dispatcher.md`,
  `docs/prompt_validator_drift_invariant.md`) + extensions to
  `docs/cloudrun_image.md` (FLUX OOM root cause + cpu_offload),
  `docs/cloudrun_render_worker.md` (lessons table), `docs/channel_layout.md`
  (4-layout matrix), `docs/full_cloud_cutover_2026_05_09.md` (status
  pointer), `docs/cloud_run_set_secrets_destructive.md` (Job-shape
  evidence), `docs/channel-learnings/mystoriesanimated/channel.md`
  (orchestrator rewrite stage note). Recurring meta-pattern: every
  post-cutover stage made independent layout/auth/path assumptions
  that diverged from sibling modules — fix is always to share one
  resolver / one source of truth (orchestrator pattern at validator
  layer; `RenderPaths` at filesystem layer; `cloudrun_auth` shim for
  modules duplicated across `pipeline/` and `pipeline/cloud/`). Sweep
  recipe to catch sibling drift before next deploy: `find pipeline/
  -name '<basename>.py' | sort | uniq -c | awk '$1 > 1'` for any
  module with two copies; `grep -rn "Path(channel_yaml).parent" cloud/
  pipeline/ control/` for any mp4/output lookup that bypasses
  `RenderPaths`.
