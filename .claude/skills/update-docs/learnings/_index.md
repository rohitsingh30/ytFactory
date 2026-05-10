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
