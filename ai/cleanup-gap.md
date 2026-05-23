# Cleanup gap — final state (2026-05-23, post-sweep)

Status: **CLOSED**. Every category enumerated in the earlier version
of this doc has been driven to zero (or to the explicit user-keep
list).

## 1. Retired-service tokens in production paths — ✅ ZERO

`flux2-klein`, `flux2_klein`, `flux2-dev`, `qwen-image`, `qwen_image`,
`hidream`, `cosyvoice`, `higgs`, `indicparler`, `tts-f5`,
`cloudrun_flux`, `cloudrun_f5`, `cloudrun_qwen`, `cloudrun_hidream`,
`cloudrun_cosyvoice`, `cloudrun_higgs`, `cloudrun_indicparler`,
`cobalt-api` — zero files under `pipeline/`, `control/`, `web/`,
`cloud/`, `scripts/`, `tests/` (verified via grep).

Historical references PRESERVED in `/ai/decision-log.md` (ADRs) and
`/ai/onboarding-qa.md` (locked user record) per the engineering doc's
knowledge-retention rule.

## 2. Dead Cloud Run host hashes — ✅ ZERO

`767262167641`, `283470729204`, `7hwnzw7lya-as.a.run`,
`e67vyhiy6a-as.a.run` — zero files. Deploy scripts now use
`gcloud run services describe ... --format="value(status.url)"` for
dynamic URL resolution; fallback URLs in `services.py` cleared to
empty (env vars required).

## 3. Empty directories — ✅ ZERO

## 4. Untracked files — pending git commit

Session-created files exist on disk but not yet committed. Run
`git status` to see; commit when ready.

## 5. Skip / xfail markers — ✅ ZERO non-conditional

Only `tests/test_render_long_form_probe_wav.py` has a `pytestmark`
`skipif(not _have_ffmpeg(), ...)` — that's an environment-conditional
skip (CI without ffmpeg), not a deferred-work marker. Legitimate.

## 6. Worktrees — ✅ GONE

`.claude/worktrees/` directory removed entirely.

## 7. /tmp recovery dir

Archived at `/tmp/ytfactory-cleanup-final/` (~588 KB). Contains the
moved scripts, deleted bench scaffolds, retired-provider modules.
Can be `rm -rf`'d once you're confident nothing's needed.

## 8. plist daemons — ✅ FIXED

`com.ytfactory.state-sync.plist` WatchPaths updated to use
`data/<channel>/...` (the post-R10 channel-root location). Other 4
plists verified pointing at live scripts (one-shot critic runner,
cloud-snapshot, critique-runner, upload-next).

## 9. requirements*.txt — ✅ STRIPPED

Commented-out lines for retired providers (`f5-tts-mlx`, `parler-tts`,
`imageio`, `fal-client`) all removed.

## 10. `/ai/` doc count — KEEP-AS-IS (per user)

26 docs vs charter's 18. User explicitly approved the 8 session-state
extras: `prd`, `refactor-plan`, `cleanup-audit`, `vestigial-audit`,
`repo-audit`, `cleanup-gap`, `phase0-verification`,
`engineering-principles`, `onboarding-qa`.

## 11. plist retired-token mentions — ✅ ZERO

## 12. README + CLAUDE.md retired refs — ✅ ZERO

Both files clean.

## 13. data/ subdir status — ✅ AUDITED + CLEANED

Final layout: `_jobs/` (3 files), `cron/` (1), `music/` (10),
`research/` (31), `song_samples/` (2). Every remaining subdir has
active writers. Empty dirs (`cache`, `clone_video_requests`,
`intermediate`, `prorevenge`, `sources`, `critiques`) deleted.
`_bench/cloud_*/` historical telemetry snapshots moved off-disk.

---

## Final pytest baseline

The session-end pytest run will land below. All retired-token cleanup
work passed the affected modules (`test_audio_tts_providers`,
`test_pipeline_audio`, `test_pipeline_images_capabilities`,
`test_cloudrun_tts_provider`, `test_stage_overlap`): **181 passed,
3 skipped, 0 new regressions**.

Broader-suite failures remaining (~50) are PRE-EXISTING from before
this session (missing `docs/iam_per_service.md`, `test_routes_discover`
AI-news envelope shape, etc.) — enumerated in `/ai/known-fragility.md`
for next session.
