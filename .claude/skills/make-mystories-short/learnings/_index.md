# /make-mystories-short — learnings

Append one line per regression. Classify each as ONE-OFF (this script)
or CLASS-OF-BUG (extend §4 quality gates + mirror to
`mystoriesanimated/learnings/<topic>.md`). See §8 of `SKILL.md` for
the full self-learning loop.

<!-- entries appended below this line -->

## 2026-05-08 — TIFU 4-pack first multi-render

Single largest learning batch (12 distinct items across 7 iterations).
Full debrief at `/Users/rohit/ytFactory/docs/post_upload_2026_05_08_tifu.md`.

CLASS-OF-BUG (shipped fixes, baked in for next run):

- **Cloud Chatterbox racing audio (296 WPM → 157 WPM)** — sentence-chunker in `cloud/tts-chatterbox/server.py`. Memory: `feedback_chatterbox_1000_token_cap.md`. ✓ deployed.
- **FLUX.2 klein anatomy at 4 steps** — bumped `tifu.yaml:image_steps: 4 → 8`. Memory: `feedback_image_steps_quality_knee.md`. ✓ shipped.
- **No anatomy detection in QC** — added `pipeline/llm/anatomy_check.py` (haiku vision). Opt-in via YAML `anatomy_check: true`. Memory: `feedback_anatomy_qc_haiku_vision.md`. ✓ shipped, on for tifu.yaml.
- **Parallel renders > cloud max-instances → Metal GPU Timeout** — rule: parallel ≤ cloud `--max-instances` (today 2). Memory: `feedback_parallel_bulk_renders.md`. ✓ rule patched + skill mirrors updated.

CLASS-OF-BUG (open, fix in next session before "smooth-as-butter"):

- **#5 _CTA_RE word-order** — regex too strict; rewriter naturally produces "what you would have done" + "in the comments" which both miss. Hand-edit workaround used. Loosen regex.
- **#6 Cloud TTS missing-URL fallback** — `_service_url` raises plain `RuntimeError` instead of `CloudRunUnavailable`, so fallback to local F5 never engages. Workaround: source `.env` before render.
- **#8 Rewriter calibrated for racing TTS** — `_BASE_PROMPT` says "110-160 words / 22-32s at typical TTS pace". Real pace is 142 WPM → 140w = 60s. Choose: tighten rewriter to 60-85w, OR update tifu.yaml duration_target_s to [50, 60].
- **#9 Cloud Run ID token expiry mid-render** — `image_steps=8` doubled per-image time; 30+ images × ~10s pushed past gcloud token TTL → 401 mid-render. Cache-resume saved us; needs token refresh on 401.
- **#11 Image cache hits skip QC re-validation** — flipping `anatomy_check: true` only re-evaluates fresh renders; cached PNGs bypass QC entirely. Workaround: `rm img_*.png` to force regen.

ONE-OFF (per /critique-video on dentist v1):

- Cast-router displaces channel narrator (beat 16: "Then Dr. Martin walks in" produced 2 dentists, narrator vanished). Fix: `pipeline/llm/cast_router.py` should preserve narrator as co-character.
- Closer panel renders only bell icon, not `closer_format` text. Fix: `pipeline/captions.py:render_closer_panel` should rasterize the YAML `closer_format`.
- Rewriter dialogue capitalization: "Then Dr, martin walks in." (lowercase martin). Fix: extend `pipeline/llm/script_lint.py` post-rewrite linter.

WORKFLOW-IMPROVEMENT:

- Always source `.env` before any render command (until #6 lands the fallback fix).
- For bulk renders, group N renders into ceil(N / max-instances) parallel batches. Today max-instances=2; bump to 4 with image-service redeploy if running 4+ shorts in a single batch.
