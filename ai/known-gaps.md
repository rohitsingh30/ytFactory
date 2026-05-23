# Known Gaps — Consolidated Inventory

**What this document is:** A prioritized catalog of KNOWN gaps already surfaced in audit docs, memory, and code — NOT a fresh audit. Every item cites its source (cleanup-gap.md, cleanup-audit.md, vestigial-audit.md, repo-audit.md, known-fragility.md, prd.md, memory/, git log). Items are ordered by blast radius (silent-bad-ship first, worst), then by size of fix within each tier.

**What this is NOT:** A comprehensive code review. This consolidates scattered knowledge. Items not in these sources are not included.

---

## Silent-Bad-Ship Gaps (highest priority)

These allow unshippable output to reach users silently (undetected shippage of garbage, truncated video, wrong audio).

| # | Title | Source | Description | Blast Radius | Cost | Code Anchor |
|---|---|---|---|---|---|---|
| 1 | Short-form rewrite has no upstream length gate; writeback duration-check removed | [This conversation] | Long-form has `SectionTooShortError` retry (@pipeline/llm/rewrite_long_form.py:999, 1245, 1410). Short-form has nothing. Post-cleanup writeback.py no longer fails on short mp4s. If render-worker-v2 redeployed with current HEAD, silent under-length ships. | silent-bad-ship | medium | cloud/render-worker-v2/writeback.py:13; pipeline/llm/rewrite_long_form.py:999 (long-form counterpart) |
| 2 | Spec.caption_style not wired end-to-end → captionless mp4s ship silently | known-fragility.md:F22 | `spec.caption_style` set at engine but lossy through overlay→compose. Caption import failures warn rather than hard-fail; Devanagari font missing from worker Dockerfile. No post-render caption density gate. Renders ship "successful" mp4 with zero captions or captionless halves. Signal is silent (no exception, no telemetry, no gate fire). | silent-bad-ship | medium | pipeline/render/spec.py; pipeline/render/overlays/word_caption_pngs.py; pipeline/render/overlays/sentence_caption_ass.py; cloud/render-worker-v2/Dockerfile:29-38 |
| 3 | Solid-color fallback survives via env override; footage_windows still silent-falls | known-fragility.md:F11, F12 | `YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1` escape hatch + `pipeline/render/visualize/footage_windows.py:57,63,75` (unguarded silent fallbacks). Two silent-fallback escape paths in `ai_beat_slideshow.py:503-506` (0 images), `ai_beat_slideshow.py:515-517` (stitch failed) bypass the audit-gated path. Cloud-image-service total outage → 0 images → solid color short ships silently. | silent-bad-ship | medium | pipeline/render/visualize/_fallback.py:53,93,129,155; pipeline/render/visualize/ai_beat_slideshow.py:503-506, 515-517; pipeline/render/visualize/footage_windows.py:57,63,75 |
| 4 | `_synth_cloudrun_indicf5` may not ship `ref_audio_text` to model | known-fragility.md:F13, F21; prd.md:199 | IndicF5 "Hindi gibberish noise" traced to this function not forwarding `ref_audio_text`. Line 837 has guard but payload-side write may silently drop. Hindi renders return a wav so no TTS-level error surfaces; failure only audible on playback. Every HindutavaAnimated render at risk. | silent-bad-ship | small | pipeline/tts/cloudrun.py:813, 837, 929 |
| 5 | `ai_beat_slideshow.py` has TWO silent-fallback escape paths audit missed | known-fragility.md:F12 | Lines 503-506 (0 images → solid color) + 515-517 (stitch failed → solid color) both call `_fallback_solid_color()` directly, NOT the gated path. Cloud-image-service outage that fails all beats bypasses the 10% gate entirely. | silent-bad-ship | small | pipeline/render/visualize/ai_beat_slideshow.py:503-506, 515-517 |
| 6 | Writeback duration gate kills successful mp4s; rule duplicates upstream gates | known-fragility.md:F23; prd.md:Q54 | Writeback hard-fails when duration < 80% of target (1440s floor for 1800s). Job `7743ca76` produced real 1232s mp4 (perfectly watchable) killed by gate. Duration floor duplicates length policing already in rewrite gates; when upstream gates work, duration is in band. Gate fires on real artifacts operators would ship. | silent-bad-ship | small | cloud/render-worker-v2/entrypoint.py:~2400 |
| 7 | Broad `except Exception` swallows in artifact-emission paths | known-fragility.md:F9 | `pipeline/render/video.py:276-277, 397-398, 426-427` (emit_artifact wrapped in broad except). When artifact pipeline breaks (misconfigured GCS, IAM regression), render proceeds silently; dashboard "live preview" promise goes dark undetected. | silent-bad-ship | small | pipeline/render/video.py:276-277, 397-398, 426-427; pipeline/render/artifacts.py:82, 96, 194, 360 |
| 8 | `part2_watcher` writer/reader split — sidecars accumulate; Part-2 never auto-fires | prd.md:F24 | `pipeline/upload/upload.py` writes `data/intermediate/<chan>/part2_pending/<slug>.json` sidecars on Part-1 cliffhanger uploads. `pipeline/part2_watcher.py` defines the reader but no plist/cron/launchd entry invokes it as daemon. Sidecars accumulate; Part-2 never auto-fires. | wasted-compute (blocks feature) | medium | pipeline/upload/upload.py; pipeline/part2_watcher.py; data/intermediate/` |

---

## Wasted-Compute Gaps

These waste cloud resources or developer time but don't ship bad output.

| # | Title | Source | Description | Blast Radius | Cost | Code Anchor |
|---|---|---|---|---|---|---|
| 9 | Parallel section-body fan-out has no peer awareness → systematic length collapse | known-fragility.md:F3, F18; prd.md:Q55-Q56, Q58-Q63 | N parallel section LLM calls fan out via ThreadPoolExecutor; each sees only its own `target_words`. No cross-section visibility, no peer-budget signal. When one section under-delivers, peers can't compensate; aggregate gate fires post-hoc. Root cause of job `a734babb` (section 7 at 14% of mean). | wasted-compute | medium | pipeline/llm/rewrite_long_form.py:1017, 1109, 880 |
| 10 | Outline LLM can imbalance section allocations within [0.5x, 2x] clamp | known-fragility.md:F8, F19; prd.md:Q55, Q63 | Outline returns per-section targets within [0.5x, 2x] of mean but no sum-check that `sum(section.target_words) ≈ total target`. Outline can pick heavily imbalanced shape (3 long acts + 7 short scenes) with no aggregate validation. Combined with F3 (bodies honor allocations blindly), imbalance compounds into systematic under-delivery. | wasted-compute | small | pipeline/llm/rewrite_long_form.py:617 |
| 11 | Anthropic_sdk LLM backend has tests but zero production deployment | vestigial-audit.md:Audit 2; prd.md:R7 | 3-way dispatcher (cli + azure_openai + anthropic_sdk). `anthropic_sdk` reachable via deploy.sh:134-137 runbook but never actually flipped in production (default is azure_openai). No A/B test infrastructure, no observability docs, no alert on fallback. Maintenance tax for dead code path. | wasted-compute | small | pipeline/llm/cli.py:56-91, 1416-1467; cloud/render-worker-v2/deploy.sh:134-137 |
| 12 | Plugin slots audio/timeline/compose are 1-impl-per-engine; abstraction unused | vestigial-audit.md:Audit 3; prd.md:R8 | 6 plugin slots; 3 are spec-driven (visualize, overlays, music = justify abstraction). 3 others (audio, timeline, compose) hardcoded 1-impl-per-engine with no spec-driven selection. `register_plugin` is overhead; direct imports would be simpler. | wasted-compute | small | pipeline/render/short_engine.py:164-168, 439-466; pipeline/render/long_engine.py:94-98, 224-230 |
| 13 | Stale references to 6 retired services in catalog + preflight | vestigial-audit.md:Audit 1; cleanup-audit.md:2d | `cloud/render-worker-v2/entrypoint.py:41-47, 217-261` still checks/whitelists URLs for `flux2-klein`, `flux2-dev`, `qwen`, `hidream`, `tts-indicparler`, `tts-f5`. `pipeline/cloud/services.py:84-164` lists them in admin-panel catalog. Both are retired per CLAUDE.md. Admin panel shows 6 "unconfigured" chips; preflight searches for unset env vars. | wasted-compute | small | cloud/render-worker-v2/entrypoint.py:41-47, 217-261; pipeline/cloud/services.py:84-164; pipeline/images/images_cloudrun.py:35, 503 |
| 14 | `_PER_BEAT_FAILURE_THRESHOLD=0.10` is a first-failure kill, not post-retry ceiling | known-fragility.md:F20; prd.md:Q66 | 10% threshold fires on FIRST beat-failure aggregate exceeding it — no per-beat retry before kill decision. Cloud-image-service hiccup breaking 12% of beats kills whole short on first attempt. Per ADR-023, gate becomes post-retry ceiling, not first-fail kill. No retry infrastructure exists yet. | wasted-compute | medium | pipeline/render/visualize/ai_beat_slideshow.py:90, 492 |
| 15 | Editing-agent URL missing from deploy.sh:106 env block | vestigial-audit.md:Audit 1 | `CLOUDRUN_EDITING_AGENT_URL` not in `cloud/render-worker-v2/deploy.sh:106` (only TTS/image/ASR are). `--set-env-vars` is REPLACE-not-merge, so manual additions wiped on redeploy. Editing-agent runs against laptop fallback on every post-deploy job unless operator manually re-adds env var. Circuit breaker open → local executor every time. | wasted-compute | small | cloud/render-worker-v2/deploy.sh:106, 79-83; pipeline/editing/cloudrun.py:47 |
| 16 | Cobalt-api deployed but never called; zero Python callers anywhere | vestigial-audit.md:Audit 1 | `cloud/cobalt-api/deploy.sh:17` deploys it; only doc + catalog references. No Python file reads `CLOUDRUN_COBALT_URL`. Service is live but unreachable. Cost wasting; should retire. | wasted-compute | small | cloud/cobalt-api/; pipeline/cloud/services.py:211-218 |

---

## Dev-Friction Gaps

These create friction for contributors / maintainers but don't directly break production.

| # | Title | Source | Description | Blast Radius | Cost | Code Anchor |
|---|---|---|---|---|---|---|
| 17 | Laptop dev + prod servers mount different routes; drift-prone | vestigial-audit.md:Audit 1; cleanup-audit.md:1d | `web/server.py` (prod) imports `control.routes.*` + `control.core.*`. `control/server_dev.py` (laptop) imports both old flat routes AND new v2 routes. Laptop dev runs against different set than prod. Bug reported in dev may not exist in prod (and vice versa). | dev-friction | small | web/server.py:1792-1813; control/server_dev.py:24-39 |
| 18 | Docstring references to non-existent docs | cleanup-audit.md:2g | 8 docs-paths referenced from production code that don't exist: `docs/telemetry.md`, `docs/critique_chat.md`, `docs/critique_runner_ops.md`, `docs/playwright_with_signed_in_chrome.md`, `docs/post-audit-2026-05-14.md`. Broken-promise documentation. | dev-friction | small | pipeline/telemetry.py:2; control/routes/critique_routes.py; pipeline/critique/runner.py; .claude/skills/upload-via-playwright/SKILL.md; .github/workflows/tests.yml:79 |
| 19 | Kwarg-drift between caller + helper at `longform_panels` → `build_image_panels_video` | known-fragility.md:F4 | Pre-2026-05-15 caller passed nonexistent kwargs; broad `except Exception` swallowed TypeError. Current code pins signature but `except Exception` at line 140 STILL swallows future drift. Only safety net is contract pin test `tests/render/visualize/test_long_form_fallback.py::LongformPanelsBuildKwargContractTest`. | dev-friction | small | pipeline/render/visualize/longform_panels.py:127-139, 140 |
| 20 | `spec.extra` is untyped dict; typos produce silent empty defaults | known-fragility.md:F2 | `RenderSpec.extra: dict[str, Any]` is free-for-all namespace. Every plugin reads keys with `.get(name, default)` — no schema. Typo in `spec_enrich.py` (e.g., `caracter_description`) silently produces empty character lock; character drifts and signal is visual-inspection-after-fact. "spec_enrich populates BEFORE plugins read" invariant is implicit, unenforced. | dev-friction | medium | pipeline/render/spec_enrich.py:145, 214; pipeline/render/short_engine.py:157, 177, 414, 445, 454, 458, 462, 466, 571, 588 |
| 21 | Stale per-channel scripts directories pre-generalization | cleanup-audit.md:1a | `scripts/historyrecapped/` + `scripts/sportsrecapped/` (17 files total) are pre-generalization residue. Skills reference new paths under channel dirs, not these. README explicitly says "pre-generalization scripts kept for reference; don't add new work." Dead code clutter. | dev-friction | small | scripts/historyrecapped/; scripts/sportsrecapped/ |
| 22 | 4 orphan cloud/iam/*.sh scripts with zero callers | cleanup-audit.md:1c | `grant_telemetry.sh`, `grant_per_service_telemetry.sh`, `create_per_service_sas.sh`, `wire_token_health_cron.sh` — zero refs anywhere. (Other 4 iam scripts are referenced from production code.) | dev-friction | small | cloud/iam/ |
| 23 | Noqa: BLE001 density signals systematic error-swallowing | known-fragility.md:F14 | 235 occurrences across pipeline/ + control/. Hotspots: research/, niche_specs.py, render/video.py, short_engine.py. Broad-except legitimacy inherited from surrounding pattern. Maintainability risk: no "broad-except budget" ceiling. | dev-friction | medium | grep "noqa: BLE001" across pipeline/ + control/ |

---

## Cosmetic/Future Gaps

These are cleanup/documentation items with minimal impact.

| # | Title | Source | Description | Blast Radius | Cost | Code Anchor |
|---|---|---|---|---|---|---|
| 24 | Stale ytfactory-prod-v2 references in source defaults | cleanup-audit.md:2f; prd.md:P2.5 | 13 sites with bare v2 defaults (firestore.rules, control/core/cloud_run.py, control/core/jobs.py, cloud/render-worker-v2/entrypoint.py ×3, plist files ×2, control/routes/state_routes.py ×2). Env vars override; docs say v3 is live. Drift; misleading for future readers. | cosmetic | small | [see P2.5 subtasks in prd.md] |
| 25 | 4 dead env URLs in cloud/render-worker-v2/deploy.sh:106 | cleanup-audit.md:2f; prd.md:P2.6 | Env block sets URLs for `flux2-klein`, `flux2-dev`, `qwen`, `indicparler` (all retired). Source directories don't exist. Dead config surface. | cosmetic | small | cloud/render-worker-v2/deploy.sh:106 |
| 26 | `cloud/warm_image_services.sh` still references flux2-klein | vestigial-audit.md:2d | References `ytfactory-image-flux2-klein-…run.app` as fallback. Per CLAUDE.md flux2-klein is retired; z-image-turbo is sole production image model. Script needs update to default to `zimage` only, or delete. Contents stale. | cosmetic | small | cloud/warm_image_services.sh |
| 27 | `cloud/_bench/README.md` lists production as v2; treats z-image-turbo as "WIP" | cleanup-audit.md:2e | References `ytfactory-prod-v2` + lists `cloud/image-flux2-klein/` as live + treats `cloud/_bench/image-z-image-turbo/` as "WIP". Opposite is true per CLAUDE.md. Not waste; naming-collision footgun for new contributors. | cosmetic | small | cloud/_bench/README.md |
| 28 | `pipeline/channels.yaml` location collision (file vs directory name) | cleanup-audit.md:2f | Lives at `pipeline/channels.yaml` (file), not under `pipeline/channels/` (the directory of per-channel YAMLs). Could rename to `pipeline/channels_manifest.yaml` for clarity. | cosmetic | small | pipeline/channels.yaml |
| 29 | Module docstring drift in `pipeline/render/video.py:1-43` | known-fragility.md:F15 | Docstring describes pre-bigbang state: says `kind=short` is no-op, `kind=sports_doc` not yet wired. None of this is true. Legacy `render()` still has NotImplementedError stubs. Function body is authoritative; docstring is drift. | cosmetic | small | pipeline/render/video.py:1-43 |
| 30 | Empty `scripts/setup/__init__.py` in empty package | cleanup-audit.md:1e | `scripts/setup/` contains only `__init__.py`; nothing imports `scripts.setup`. Pure dead code. | cosmetic | small | scripts/setup/__init__.py |

---

## Summary statistics

- **Total gaps:** 30 items
- **Silent-bad-ship:** 8 (highest severity)
- **Wasted-compute:** 8
- **Dev-friction:** 7
- **Cosmetic:** 7

**Top-3 silent-bad-ship by impact:**
1. **#1** — Short-form rewrite has no upstream length gate (post-cleanup writeback removed the check)
2. **#2** — Caption style not wired end-to-end (silent mp4s with zero captions)
3. **#3** — Solid-color fallback survives via env override + footage_windows silent paths (black shorts ship silently)

**Phase-0 blocking blockers (before R12 smoke):** #1, #2, #3, #4 must be addressed.

---

**Next step:** Prioritize per operating principle (gate-as-repair-trigger, zero silent-failures), pick a phase, and drive to zero.
