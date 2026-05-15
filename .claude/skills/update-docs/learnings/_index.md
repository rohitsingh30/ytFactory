# /update-docs — learnings index

One-line entries per regression caught during a run. Detail lives in
sibling topic files in this dir or in the dual-saved memory/project doc.

## CLASS-OF-BUG (rule changes to SKILL.md)

- 2026-05-13 — **Big-rip findings: cleanup-finally overwrite + deploy
  silent failures + system-removal checklist.** A single session that
  ripped out the burner-channel system surfaced three durable
  cross-channel rules. (1) `_bump_action(state, "Chrome left running …")`
  in a `finally` block overwrote the diagnostic `"brand-account
  switch failed …"` written by the `try` body — masked the actual
  failure cause across 20 runs and materially contributed to retiring
  the system instead of fixing it. Pattern: gate info-bumps on
  `state.phase != "failed"` (`docs/finally_cleanup_overwrite_guard.md`).
  (2) `cloud/web-next/deploy.sh` uses `(cd web-next && npm run build >/dev/null)`
  which swallows pre-build TS errors, and hardcodes `--service-account=
  web-next-runner@…` referencing a SA that never existed in IAM
  (live service was on default compute SA all along). Two-trap fix
  in `docs/cloud_deploy_script_safety.md`. (3) Removing a multi-layer
  system needs the full per-layer checklist in
  `docs/system_removal_checklist.md` — code + tests + docs + cloud
  (Firestore tasks, GCS blobs, Secrets, IAM) + local (launchd, /tmp,
  ~/.config/) + redeploy verification. Sweep recipes in each doc.
  Memory: `feedback_finally_cleanup_overwrite_guard.md` +
  `feedback_cloud_deploy_silent_failures.md` +
  `feedback_system_removal_checklist.md`.

- 2026-05-13 — **Firestore `jobs/*` collection has no stuck-pending
  reaper.** `web/server.py::_periodic_queue_reaper` only walks
  `agent_tasks/*` (laptop-agent leasing). When a Cloud Run JOB
  worker crashes pre-writeback (e.g. "Internal error running task"),
  the `jobs/<id>` doc stays at `status=pending, stage=dispatching`
  forever and ghosts `/api/queue` Queued column. Caught when a
  3.5-day-old test render kept showing in the Queued column with
  the Cloud Run execution itself reporting `Completed`
  with failure. Mitigation design (Option A sweeper extending the
  existing reaper loop + Option B SIGTERM/atexit writeback in 4
  render entrypoints) shipped to `docs/jobs_collection_reaper.md`;
  inline `KNOWN GAP` note added to
  `control/routes/render_routes.py::get_queue_state` docstring;
  cross-link added to `docs/laptop_agent_cloud_contract.md` flavour 6.
  Memory: `feedback_jobs_collection_no_reaper.md`. Sweep recipe to
  surface siblings (any other Firestore collection with the same
  "writer might die before update" pattern):
  `grep -rn "create_job\|create_render\|jobs_mod.update" control/ pipeline/ web/ --include='*.py' | grep -v test_`.

- 2026-05-13 — **S1.21 (per-service runtime SAs) created `web-runner@`
  but per-secret IAM bindings did NOT carry over from the legacy
  `tts-runner@`.** First redeploy after the SA flip succeeded
  through Cloud Build, then failed at `gcloud run deploy` step
  with `Permission denied on secret … must be granted
  roles/secretmanager.secretAccessor` repeated 7× (one per secret
  `cloud/web-server/deploy.sh::--set-secrets` mounts). Idempotent
  fix script `cloud/iam/grant_web_runner_secrets.sh` shipped;
  table entry in `docs/iam_per_service.md` flipped from `⚠️ pending`
  to `✅ 05-13`; pre-flight reminder added to
  `cloud/web-server/deploy.sh` so the next agent who hits this
  doesn't re-debug from scratch; cross-reference added to
  `docs/deploy.md § Subsequent updates`. Memory:
  `feedback_web_runner_secret_accessor_post_s121.md`. Sibling SAs
  (`render-runner`, `image-runner`, `weights-runner`,
  `cobalt-runner`, `stats-refresh-runner`) still rely on manual
  per-binding `gcloud secrets add-iam-policy-binding` runs — same
  trap if any of them ever needs >3 secret mounts.

- 2026-05-13 — **Cloud-Run-shape assumptions baked into helpers
  silently break in JOB context.** Multiple `K_SERVICE`-only checks
  in `pipeline/observability/` + 15 per-service `cloud/<svc>/otel_init.py`
  copies fell through to "console" exporter mode in render-worker-v2
  (a Cloud Run JOB) because JOBs set `CLOUD_RUN_JOB`/`CLOUD_RUN_EXECUTION`
  not `K_SERVICE`. Sibling pattern to 2026-05-11
  `feedback_otel_init_copy_path_per_context.md` (per-context Dockerfile
  COPY paths) — both are "auto-patch helper assumed one Cloud Run shape".
  Sweep recipe shipped in `docs/cloud_run_job_subprocess_debugging.md`:
  `grep -rn "K_SERVICE" pipeline/ cloud/ scripts/ --include='*.py' --include='*.sh' | grep -v "CLOUD_RUN_JOB\|CLOUD_RUN_EXECUTION"`
  — every hit is a candidate for `_on_cloud_run()` normalisation.
  Memory: `feedback_cloud_run_job_subprocess_debug.md`.

- 2026-05-12 — **`/update-docs` runs were stranding their own output
  unstaged.** Audit caught 16 modified + 1 new file (full prior run's
  output: composite-index findings, action-cardinality fixes, slim-
  image runtime-deps contract) sitting unstaged for hours because
  the SKILL.md never required a commit step. Persistence isn't done
  until git history on origin reflects it. New SKILL.md Section 8
  ("Commit + push") + Quality gate 9 ("Clean-tree verifier"). Memory:
  `feedback_update_docs_auto_commit.md`. Sweep recipe to catch
  similar laptop-vs-origin drift on other skills:
  `for d in .claude/skills/*/; do echo "=== $d"; grep -l "git add\|git commit" "$d"SKILL.md 2>/dev/null || echo "(no commit step)"; done` —
  every "(no commit step)" hit on a skill that writes files is a
  candidate for the same patch.
- 2026-05-11 — **Firestore `where + order_by` queries silently 400
  on missing composite index, and a too-broad try/except hides it
  from the UI.** Studio Queue page's Completed column showed "Empty"
  for 2 days while Firestore held 18+ real terminal jobs — the
  `jobs(status, updated_at)` composite index was never deployed and
  the single `try/except` around both `/api/queue` Firestore calls
  swallowed the `FailedPrecondition`. Standing rule landed in
  `docs/data_flows.md` § "composite-index discipline" (split each
  query's try/except, surface failures via typed `warnings: dict[str,
  str]` on the response, UI MUST render an actionable banner instead
  of an "Empty" placeholder when the backend reports a section
  warning). Sweep recipe to catch siblings:
  `grep -rn "\.where(.*\.order_by(" control/ web/server.py pipeline/ --include='*.py'` —
  every match is a candidate that needs a composite index in
  `firestore.indexes.json` AND a per-query try/except. Memory:
  `feedback_queue_endpoint_silent_index_swallow_2026_05_11.md`.
- 2026-05-11 — **Action-cardinality mismatch is a recurring perf
  pattern.** Sibling modules
  `pipeline/cross_engage/burner_engage.py` (cloud worker,
  `MODE_SUBSCRIBE_ONLY`) and
  `pipeline/cross_engage/cross_engage_burner_attached.py` (laptop
  attach-mode CLI, `--no-like`) both shipped with subscribe-only flows
  that iterated the catalog video-by-video despite Subscribe being a
  per-channel action. Two independent fixes landed (dedupe-then-open-
  one-video for the cloud worker on 2026-05-11; `/channel/<UC>`
  direct navigation for the CLI on 2026-05-11) — the meta-pattern
  was the same: project the catalog to the action's cardinality
  key BEFORE iterating, and use the lightest YouTube surface that
  exposes the action key (channel page for Subscribe; video page
  for Like / Watch / Comment). Sweep recipe to catch siblings:
  `grep -rn "for .* in catalog" pipeline/cross_engage/ pipeline/upload/` —
  every match should be checked: if the inner action is per-channel
  (Subscribe) or per-creator, the loop is iterating the wrong set.
  Memory: `feedback_subscribe_only_fast_path.md` +
  `feedback_subscribe_only_dedupes_catalog.md` (cross-linked).
  Project doc: `docs/cross_channel_engagement.md` § "Subscribe-only
  fast path".
- 2026-05-11 — **SW helper extraction loses `event` scope** (caught
  by user report `Uncaught (in promise) ReferenceError: event is not
  defined at staleWhileRevalidate (sw.js:118:7)`). Every `/api/*`
  GET broke with `ERR_FAILED` in any tab that had registered the
  SW. Rule now lives in `docs/service_worker_handler_scope.md` +
  memory `feedback_sw_handler_scope.md`. Sweep recipe:
  `grep -nE "event\.(waitUntil|respondWith|request|data|clientId)"
  web-next/public/sw.js` — every match must be inside an
  `addEventListener` callback OR a helper that takes `event` as a
  named parameter. Corollary: bump `CACHE_VERSION` on every SW
  change so the activate handler drops poisoned cache. Stale claim
  edited inline: `docs/web_perf_pass_2026_05_11.md` line 61
  (`ytfactory-api-v1` → `v2`).
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

- 2026-05-12 — **Operator kill-switch ritual was incomplete in the
  one place it was documented.** `docs/burner_channels.md` Failure
  Modes table row (added 2026-05-10) listed `launchctl unload` only
  as the way to stop the agent that spawns burner Chrome. Without
  `launchctl disable gui/$UID/...`, the next login reloads the
  KeepAlive plist and the user re-experiences the same "Chrome
  windows opening unexpectedly" bug. Fixed inline in that table cell
  AND co-located the full kill-switch runbook in
  `docs/laptop_agent_cloud_contract.md` (where every drift-flavour
  debug session ends up). Sweep recipe to catch sibling stale
  "stop the agent" guidance:
  `grep -rn "launchctl unload" docs/ */learnings/ .claude/skills/*/SKILL.md` —
  every hit on a `com.ytfactory.*` plist should pair `unload` with
  `disable gui/$(id -u)/<label>` if the goal is "stop across login
  too", not just "stop this session". Audit found 5 hits on
  2026-05-12: 3 for `com.ytfactory.laptop-agent` (two updated, one
  is a smoke-test restart recipe in `cross_engage_cloud_v2.md` that
  ALSO calls `launchctl load` so the recipe is complete by intent —
  added a sidebar pointing at the kill-switch runbook); 2 for
  `com.ytfactory.upload-next` in older docs which is already disabled
  and renamed `.disabled-2026-05-09` per the nuclear-cleanup doc.
  No new project doc — covered in
  `docs/laptop_agent_cloud_contract.md` § "Operator kill-switch".
  Memory: `feedback_laptop_agent_cloud_contract.md` (2026-05-12 ext
  block).
- 2026-05-11 — **Stale "Pending" claim audit while fixing
  `/api/queue` silent-empty.** During the related-doc sweep for the
  composite-index fix, `docs/full_cloud_cutover_2026_05_09.md:224`
  said the `tasks(kind,status,created_at)` index was "creating at
  session end" — actually `state=READY` for days. Edited inline,
  added a "Resolved (2026-05-11 audit)" line so the timeline survives.
  Sweep recipe to catch sibling stale-claim docs:
  `grep -rn "Pending:.*index\|composite index.*pending\|index.*was creating" docs/` —
  every match should be re-verified against
  `gcloud firestore indexes composite list --project=ytfactory-prod-v2`.
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
- 2026-05-11 — **Hot-path-from-N-way-dropdown UX heuristic.** When
  the operator repeatedly picks one option from a multi-option
  dropdown ("subscriber only mode … is easy to do, so remove that
  from options and direct button for it"), the right move is to
  promote that option to a **dedicated direct button beside** the
  dropdown — not to reorder the menu, not to make it the default
  highlight. Keep the backend enum unchanged (server still validates
  all original modes); the UI just hard-wires the button's onClick
  to `start("<mode>")` and filters that mode out of the dropdown
  items via `ENGAGE_MODES.filter((m) => m !== "<mode>").map(...)`.
  Concrete instance: burner-channels Subscribe button promoted from
  `subscribe_only` dropdown item, deployed in
  `ytfactory-web-next-00014-6g9` 2026-05-11. Two project docs that
  described the UI as a "four-mode dropdown" went stale at the same
  moment — fixed inline this run:
  `docs/cross_engage_cloud_v2.md` § Engagement modes (table now has
  a UI-surface column + paragraph explaining the split) +
  `docs/burner_channels.md` § architecture-refresh banner. Future
  audit recipe to catch sibling stale "N-option dropdown" claims
  whenever a UI surface gets refactored:
  `grep -rnE "dropdown.*option|option.*dropdown|N.{0,5}mode.{0,15}dropdown|four[- ]mode.{0,15}dropdown" docs/ <channel>/learnings/ --include='*.md'` —
  cross-reference any hit against the current `web-next/` page to
  confirm the doc still matches reality. Escalate to CLASS-OF-BUG
  if the same hot-path-promotion happens on a second admin surface
  (would deserve its own `docs/web_ux_hot_path_buttons.md`); single
  observation today.
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
- 2026-05-10 (late) — **Test-suite recovery: 149 → 0 fails.** Five
  durable findings persisted from a session where another agent
  committed in parallel + stashed my WIP, and the unstash re-surfaced
  multiple test-pollution clusters:
  1. `feedback_sys_modules_test_pollution.md` + `docs/test_isolation.md`
     extension — conftest autouse fixture snapshots/restores both
     `sys.modules["googleapiclient.*"]` AND `google.cloud.{storage,
     firestore}` package-attr bindings every test. Fixed 35
     cross-file fails. Sweep:
     `grep -rnE "sys\.modules\[\"(googleapiclient|google\.cloud|pipeline\.|control\.)" tests/`.
  2. `feedback_apfs_package_shadowing.md` + `docs/case_insensitive_package_shadowing.md` —
     `pipeline/audio.py` (1873 lines) shadowed by empty
     `pipeline/audio/` package on APFS. Same hazard hit
     `pipeline/images.py`. Sweep:
     `find pipeline/ -maxdepth 2 -name "*.py" | sed 's|.py$||' | sort > /tmp/f && find pipeline/ -maxdepth 2 -mindepth 2 -type d > /tmp/d && comm -12 /tmp/f /tmp/d`.
  3. `feedback_cloudrun_local_fallback_pattern.md` +
     `docs/cloudrun_local_fallback_pattern.md` — `_local_fallback_or_raise`
     so missing `mlx` in post-cleanup venv re-raises original
     `CloudRunUnavailable` not `ModuleNotFoundError`. Wired into 6
     TTS providers. Sweep:
     `grep -nE "if _fallback_disabled|raise CloudRunUnavailable" pipeline/tts/cloudrun.py | grep -v _local_fallback_or_raise`.
  4. `feedback_cloudrun_tts_server_validation.md` + `docs/cloudrun_tts.md`
     § Known operational notes — every TTS server's
     `_ref_audio_to_path` MUST guard empty/short b64 → 400, not 500
     after librosa "Format not recognised". Chatterbox + indicf5
     hardened + deployed (builds `b9fd027c` SUCCESS). Sweep:
     `grep -nE "_ref_audio_to_path|base64.b64decode\(req\." cloud/tts-*/server.py`.
  5. Updated existing `feedback_test_isolation_control_storage_attr.md`
     with the new conftest-level recommended fix. Updated
     `pipeline/audio/__init__.py` docstring with pointer to shadowing
     doc.

  Meta-pattern (worth a future SKILL.md rule if it repeats): when
  Class A is "tests install fakes without restore" AND Class B is
  "package-attr binding bypasses sys.modules patch", the systemic
  fix is suite-wide conftest snapshot/restore, NOT per-test cleanup.
  Per-test workarounds don't compose across N test files.

  Stash-pop after parallel-agent commit — workflow gotcha: when
  another agent commits while you're mid-edit, your work gets
  stashed and the pop can leave a mix of conflicts + silent
  overwrites. Run `git status --short | head -20` AND
  `pytest --tb=no -q | tail -3` BEFORE assuming all my fixes
  survived. Several files silently re-overwritten on the pop this
  session — no CLASS-OF-BUG yet (single observation), escalate if
  it repeats.

## ONE-OFF (audit recipes for the next run)

- 2026-05-12 — **JSONL-era stale `/api/telemetry/*` doc claims
  survived the OTel migration.** While fixing the empty-dashboard
  bug (shadow-buffer rollout), the related-doc sweep surfaced two
  documents still describing the legacy JSONL telemetry backend,
  routes that no longer exist, and pipeline.telemetry semantics
  from before pipeline.observability:
  - `web/README.md` lines 108-196 — describes `/api/telemetry/llm`,
    `/api/telemetry/latency`, `/api/telemetry/stages` with fields
    (`image_retries`, `error_groups`, `slowest_recent_job`,
    `_traceback_fingerprint`) that came from `web/server.py` JSONL
    rollups. The current routes at `control/routes/telemetry_routes.py`
    expose a different subset (overview / stages / services /
    timeline / errors / links / init_status) and the underlying
    store is OTel logs, not JSONL shards.
  - `docs/pipeline_latency_2026.md` lines 325-414 — describes
    `pipeline.telemetry.read_events()` "caches parsed shards" /
    "blocking JSONL reads in async routes" / per-route latency
    benchmarks against the JSONL backend that no longer exists.

  These were too broad to refactor as a side-quest of the empty-
  dashboard fix. Captured here for the next run that touches
  /api/telemetry/* doc surface. Sweep recipe:

  ```bash
  grep -rnE "JSONL|/api/telemetry/llm|/api/telemetry/latency|image_retries|error_groups|slowest_recent_job|_traceback_fingerprint" docs/ web/ control/ 2>&1 | grep -v ".pyc"
  ```

  Each hit is a candidate for "rewrite to describe the OTel backend
  + actual route surface in `control/routes/telemetry_routes.py`".

- 2026-05-11 — **Stale `--max-instances=2` banner survived
  earlier doc-edit pass.** When my image-fan-out commit
  (`a3c4e47`) updated 4 docs (cloudrun_image.md +
  pipeline_latency_2026.md + parallel_bulk_renders.md +
  cloud-flux deploy.sh) for the new max-instances=3 ceiling, the
  banner block at `docs/cloudrun_image.md:9` AND the inline
  comment at `pipeline/images/images_cloudrun.py:221` were both
  missed (also the banner had `--concurrency=2` which has been
  wrong since service inception — actual is concurrency=1).
  Doc-edits that target deeper sections leave the banner / TL;DR
  / file header lines stale; reader landing on the top sees
  outdated state. **New discipline:** when a config knob changes
  AND the doc opens with a "> **Status (date):** ..." banner,
  ALWAYS edit the banner first (it's load-bearing for any reader
  who skims). Sweep recipe to catch siblings before next change:
  `grep -n "^>" docs/cloudrun_*.md docs/cloud_*.md docs/pipeline_*.md | grep -E "max-instances|--concurrency|--min-instances"`.
  Fixed inline this run; both the banner + the inline comment
  now reflect max-instances=3 + concurrency=1.

- 2026-05-11 — **Stale GPU quota arithmetic across deploy.sh
  comments.** Sweep `grep -rnE "5.{0,2}GPU.{0,8}quota|6.{0,2}GPU.{0,8}quota|3 services × 2|matches our 5-GPU" docs/ pipeline/ cloud/`
  found 9 hits (TTS deploy.sh comments + tts-f5 banner). All
  said "3 services × 2 = 6 GPUs" or "matches our 5-GPU quota".
  Reality (deploy gate enforced 2026-05-11): **L4 quota in
  asia-southeast1 is 3, not 5-6**. The wrong arithmetic was
  speculative — sum of per-service max-instances, not the actual
  project-region GPU quota. Left the TTS comments intact this
  run (they describe **capacity**, not quota; and they're not
  on the production hot-path that the user touches today). When
  any of those TTS services next get redeployed, the comment at
  the top of each `cloud/tts-*/deploy.sh` should add a banner
  "Real GPU quota in asia-southeast1 = 3 (cross-service);
  capacity sum is bookkeeping, not concurrency budget." Audit
  recipe to flag siblings on the next change:
  `grep -rnE "× 2 = [0-9]+ GPUs?|matches our [0-9]+-GPU quota|GPU.{0,3}quota.{0,5}=.{0,5}[0-9]+" docs/ pipeline/ cloud/ --include='*.md' --include='*.py' --include='*.sh'`.
  Project doc updated this run with discovery story +
  verification recipe: `docs/cloud_run_quota_self_service.md` §
  "GPU quota in asia-southeast1 = 3".

- 2026-05-11 — **Latency-pareto entries can claim falsehoods
  about implementation.** `docs/pipeline_latency_2026.md` § 6 #5
  (pre-fix) said "Beat-prompt authoring: 30-90 s — Claude CLI
  sequential per-prompt calls. Could be parallelized." I almost
  shipped a parallelization for it before grepping the actual
  callsite. Reality: `pipeline/llm/prompts.py:700::author_beat_prompts`
  already batches ALL beats into ONE Claude CLI call (a single
  `call_claude_cli` site, returning a JSON array of N objects
  in one prompt). The pareto was authored from speculation, not
  by re-checking the code. **New discipline:** any "could be
  parallelized" / "is sequential" / "is per-X" claim in a
  latency doc MUST be paired with a `grep -n` recipe pointing to
  the line proving it. Pre-fix the pareto cited no source.
  Fixed inline 2026-05-11 (commit `a3c4e47`) — entry now reads
  "Beat-prompt authoring: 30-90 s — Claude CLI **batched** call
  (already returns ALL prompts in one shot) — bottleneck is
  Claude CLI's own latency; parallelizing would split the batch
  and lose cross-beat coherence." Future audit recipe to flag
  unsourced pareto claims:
  `grep -nE "sequential.*calls?|per-[a-z]+ call|could be parallelised?|could be parallelized" docs/pipeline_latency_*.md docs/cloudrun_*.md`
  — every match should cite a `pipeline/<module>.py:<line>`
  source or be rewritten.

- 2026-05-11 — **Pre-existing voice-override test failures
  surfaced (NOT mine).** `tests/test_render_shorts.py::TestPureHelpers::test_apply_form_overrides_voice_bare_name_on_kokoro_channel_flips`
  + `..._voice_path_style_updates_voice` fail on pristine HEAD
  (verified via stash round-trip in this session). They depend
  on the `_apply_form_overrides` helper that exists in the
  user's WIP version of shorts.py but NOT in HEAD. Will likely
  auto-resolve when the WIP `_make_short_impl` extraction lands
  (commit pending). Future test run after that lands should
  re-verify. Audit recipe:
  `python -m pytest tests/test_render_shorts.py::TestPureHelpers -k "voice" --tb=line 2>&1 | tail -5` —
  if still failing once `_apply_form_overrides` is on HEAD, it's
  a real regression worth a CLASS-OF-BUG entry.

- 2026-05-11 — **Self-verifier grep failed when needle had backticks.**
  This skill's Q1 dual-save verifier did `grep -c "Bulk-create from the
  dashboard"` against `docs/burner_channels.md` and got `0` even though
  the heading was clearly there. Root cause: the verifier shell-script
  had the needle inside a heredoc/bash block that interpreted backticks
  as command substitution before grep ran, so the actual pattern grep
  saw was an empty / mangled string. Easy to misread as "the doc edit
  didn't land" → re-edit → duplicate content.

  **Fix recipe for future runs:** when the dual-save verifier needs to
  check for a heading containing backticks / em-dashes / other shell
  metachars:
  - Use `grep -F` (fixed-string) AND quote the needle with single
    quotes, OR
  - Pipe through `LC_ALL=C cat | grep -F`, OR
  - Use `awk '/Pattern/{print}'` which doesn't shell-expand the regex.

  Verified in this session: `awk '/14a. Stderr-first/{print NR": "$0}'`
  found my edit immediately after the backticked grep returned `0`.
  Don't trust a `0` from the verifier without a second method when the
  needle has shell metachars.


- 2026-05-11 — **Auto-invocation missed across an entire session
  with 6+ durable corrections.** Today's session had 6 trigger
  conditions fire (2 user-flagged regressions, 2 explicit
  durable rules, 1 manual git/deploy debrief, 1 user request to
  finally clean things up — none of these auto-invoked the
  skill). User had to explicitly run `/update-docs` 12+ hours
  into the session. Suspected cause: each fix landed inside a
  larger flow ("fix → commit → deploy → fix something else") so
  no single moment felt like an end-of-unit. **Discipline:** if
  ≥3 durable rules / surprises accumulate without a save, the
  skill must auto-invoke. Don't wait for the user. Future audit:
  `grep -nE "from now on|always|never|the rule is" <recent
  conversation>` — every match is a missed trigger candidate.

- 2026-05-11 — **`edit` tool leaves duplicate top-level symbols
  when a refactor replaces "the second of two adjacent functions".**
  While converting `VariantCard` to a chip pill in
  `web-next/app/app/create/page.tsx`, my edit replaced the
  `VariantCard` body with a combined `function VariantList { ... }
  function VariantCard { ... }` block — which silently created a
  DUPLICATE top-level `function VariantList(...)` because the
  original `VariantList` (above the matched `VariantCard`) was
  untouched. TypeScript's compiler tolerates redeclaration in
  module scope (last definition wins) so `tsc --noEmit` passed
  clean; ESLint also did not flag. Caught by an immediate `view`
  after the edit and fixed in the same turn (deleted the older
  flat-grid `VariantList`).

  **Audit recipe** — after any refactor that "replaces a function
  via edit" inside a file with multiple sibling helpers, run:

  ```bash
  grep -nE '^function [A-Z][A-Za-z]+\(' web-next/app/app/<file>.tsx \
    | awk -F'function ' '{print $2}' | awk '{print $1}' | sed 's/(//' \
    | sort | uniq -c | awk '$1 > 1 {print "DUPLICATE: "$2" ("$1"x)"}'
  ```

  Any non-empty output means an edit refactor created a redeclaration.
  Same recipe works for any `.ts` / `.tsx` / `.js` / `.py` (adjust the
  regex anchor — `^def ` for Python). Add to the wizard-edit checklist
  in `docs/web_next_wizard_orphan_audit.md` if this fires twice.

- 2026-05-11 — **Frontend commits don't auto-deploy — user has to
  ask "deployed?".** After landing `bb87749` (Source card removal +
  flat niche grid) I reported "Done — committed" without running
  `bash cloud/web-next/deploy.sh`. The user pinged "deployed?" 11
  minutes later. The agent's mental model conflated `git commit` with
  "shipped" because the wider repo includes some auto-deploy
  workflows. For `web-next/`, deploy is always manual (the `.next/`
  prebuild + Cloud Build + Cloud Run revision swap takes ~3 min and
  has no GitHub Actions trigger today).

  **Discipline:** when a commit changes any file under `web-next/`
  AND the user's reported issue is something they'd visually verify
  on the live site (vs a build/test fix), either:
  1. Run `bash cloud/web-next/deploy.sh` in the same turn as the
     commit and report both, OR
  2. Explicitly state "committed locally; not deployed yet — run
     `bash cloud/web-next/deploy.sh` to ship" so the user isn't left
     hard-reloading a stale UI.

  Same rule for `cloud/web-server/` (FastAPI backend) and
  `cloud/<service>/` images — `git push` without a `bash
  cloud/<service>/deploy.sh` ships nothing. Audit recipe to find
  un-deployed FE commits between sessions:

  ```bash
  git log --since='1 day' --name-only -- web-next/ | head -40
  gcloud run services describe ytfactory-web-next \
    --region=asia-southeast1 --format='value(status.latestReadyRevisionName)'
  # Cross-reference revision creation timestamp against the commit
  # timestamps — any FE commit older than the latest revision is
  # un-deployed.
  ```

## 2026-05-12 — Quality gate 10: fix-claim verifier + stale-Cloud-Run-URL grep recipe

### CLASS-OF-BUG: aspirational doc claims (Quality gate 10)

`docs/laptop_agent_cloud_contract.md` Drift flavour 2 said the `_ack`
"failed"→"error" fix shipped on 2026-05-11. Audit on 2026-05-12 found
the actual code in `pipeline/laptop_agent.py::_ack` was still sending
`"failed"`. The pinned regression test
(`tests/test_laptop_agent.py::TestAck::test_ack_failed`) asserted
`args[1]["status"] == "failed"` — it passed because the test agreed
with the buggy code. **158 stuck-LEASED `burner_engage` tasks
accrued in 24 h post-claim** before being noticed.

The doc was written by a prior `/update-docs` run (commit `4a31e85`,
docs-only). The fix it claimed was *intended* but nobody verified
either the code or the test before the doc shipped.

**New rule (now Quality gate 10 in SKILL.md § Section 6):** every
"Fix:" / "Mitigation:" / "Now does X:" line in a project doc this
run MUST:

1. Cite a commit SHA (`commit abc1234 (2026-MM-DD)`). "Now does X"
   without a SHA is ambiguous between intended and shipped — block,
   replace with "Planned (PR/branch-name):" if not in the tree.
2. If the doc names a specific test as pinning the fix, spot-check
   it: read the test, verify it would fail on the buggy state. A
   test that asserts the BUGGY behaviour is the canonical failure
   mode here.

Memory: `feedback_doc_aspirational_claims.md`.
Project doc: `docs/laptop_agent_cloud_contract.md` § Drift flavour 7.

### ONE-OFF: stale Cloud Run URL `ytfactory-control-7hwnzw7lya...` (404)

The agent source default + the source-of-truth plist
`control/com.ytfactory.laptop-agent.plist` referenced
`https://ytfactory-control-7hwnzw7lya-as.a.run.app` — a Cloud Run
service that no longer exists (returns 404). The installed plist at
`~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist` had been
manually patched to `ytfactory-web-7hwnzw7lya-as.a.run.app`, which
masked the bug from the running agent but left the source of truth
broken for any future fresh install. Fixed inline in commit `610461a`.

**Sweep grep recipe** (run on every `/update-docs` invocation that
touches Cloud Run URLs):

```bash
# Find any reference to a defunct service name
grep -rln --include='*.py' --include='*.md' --include='*.plist' \
  --include='*.yaml' --include='*.sh' \
  "ytfactory-control-7hwnzw7lya\|ytfactory-control-as\|ytfactory-prod[^-]" \
  /Users/rohit/ytFactory 2>/dev/null

# Cross-check live services
gcloud run services list --project=ytfactory-prod-v2 --region=asia-southeast1 \
  --format='value(name,URL)'
```

Any source/doc reference that doesn't appear in `gcloud run services list`
is stale. Add to inline edit OR (if the meta-pattern recurs across
many docs) a dedicated `docs/cloud_run_url_audit.md` to log the next
audit's coverage.

### WORKFLOW-IMPROVEMENT: diagnose-slow-queue-via-Firestore-direct

When `/update-docs` is invoked because "the queue is slow", the
investigative recipe should be: query Firestore directly for
`(status, kind)` counts FIRST (zombies, kind mismatches, ratios),
then read the agent log SECOND. Recipe captured in
`docs/laptop_agent_cloud_contract.md` § Diagnostic recipes.

This was a 2026-05-12 root-cause: "Subscribe All takes hours" felt
like a backend latency issue (which is what the agent log suggested)
when it was actually three stacked bugs (single-threaded agent +
50 wrong-kind zombies + 158 stuck-LEASED zombies). One Firestore
query revealed the entire shape in 2 s.
