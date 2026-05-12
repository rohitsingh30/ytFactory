# Live artifact emission — per-stage GCS upload + Firestore preview state

**Established 2026-05-12** as Slice 4 of the unified-renderer rollout.
Pre-fix the dashboard had nothing to show until the FINAL mp4 landed —
a 30-min long-form render meant 25-30 minutes of `compose: real-mode`
with no observable progress.

## The rule

Every render stage calls
`pipeline.render.artifacts.emit_artifact(job_id, kind, local_path)`
the moment it produces its output file. The helper:

1. Uploads to `gs://<YTFACTORY_BUCKET>/jobs/<job_id>/<kind>[/<index>]/<filename>`.
2. Updates the Firestore job doc's `artifacts.<kind>` field with
   structured state: `{status: "ready" | "pending" | "failed", uri,
   version, ...extras}`.
3. **NEVER raises.** GCS upload failures + Firestore unreachability
   log + return `None`. Render correctness must not depend on
   artifact emission.

## Artifact kinds

Canonical (in `pipeline.render.artifacts.KNOWN_KINDS`):

| kind | when | shape |
|---|---|---|
| `script` | rewrite stage produces the canonical script.json | scalar |
| `envelope` | long-form rewriter produces the full sectioned envelope | scalar |
| `narration` | TTS produces narration.wav | scalar (extras: `duration_s`) |
| `beats` | ASR produces beats.json | scalar (extras: `n_beats`) |
| `images` | each per-beat image lands | list (`index=<beat_idx>`) |
| `panels` | each long-form panel lands | list |
| `thumb` | compose produces the thumbnail | scalar |
| `video` | compose produces the final mp4 | scalar (extras: `duration_s`, `width`, `height`, `fps`) |

List-typed kinds (`images`, `panels`) use a Firestore transaction so
per-image worker callbacks don't race when they run in parallel.

## Versioning

Each call increments `artifacts.<kind>.version`. The dashboard reads
the version and refuses to display a cached preview from an earlier
version, which prevents a stale narration.wav from a previous render
attempt from sneaking in after a re-render.

## Wiring (per stage)

- **Worker rewrite stage** (`cloud/render-worker-v2/entrypoint.py`):
  emits `script` after `save_script(script_path)`.
- **Long-form orchestrator** (`pipeline/render/video.py:render_long_form`):
  emits `envelope` + `script` after `rewrite_long_form()` writes
  `narrations/<slug>.json`; emits `video` after the long_form.py
  subprocess returns the mp4.
- **Worker SHORT compose stage** (`cloud/render-worker-v2/entrypoint.py:_stage_render_real`):
  after the renderer subprocess returns, sweeps the cache dir and
  emits `narration`, `beats`, per-beat `images[i]`, `video`, `thumb`.

## Dashboard read path

`/api/jobs/{job_id}/artifact/{kind}[?index=N]` 302-redirects to a
1-hour signed URL for any artifact kind. Single endpoint covers
script / narration / beats / images[i] / envelope / thumb / video /
preview. Reuses the existing `preview.mp4` GCS-signing helper.

`web-next/app/app/render/[jobId]/page.tsx::LiveArtifactsCard`
renders inline previews as artifacts arrive:

- **script** — collapsible text panel with hook excerpt + word count.
- **narration** — HTML5 `<audio>` element + duration label.
- **beats** — download link + beat-count label.
- **images / panels** — responsive grid, one tile per beat,
  loading-spinner placeholder until the URI lands.
- **video** — "Open" button (intermediate) until the final
  `short_uri` is set, then the full preview.

Labeled `Intermediate` until the final mp4 ships so the user
doesn't confuse a half-rendered job with the published output.

## Failure semantics

A half-rendered job with playable WAV + image-failed pill is
explicit + actionable, not confusing. Per-artifact `status: "failed"`
shows the broken stage without obscuring the stages that worked. The
rubber-duck critique on Slice 4 design specifically called this out:
hide-until-done would be worse UX than label-as-intermediate.

## Files

- `pipeline/render/artifacts.py` — `emit_artifact`,
  `emit_artifact_failed`, `KNOWN_KINDS`. Lazy-imports
  google-cloud-storage / firestore so the module can be imported on
  the laptop without those packages.
- `cloud/render-worker-v2/entrypoint.py:_stage_rewrite_real` — emits
  `script` after authoring.
- `cloud/render-worker-v2/entrypoint.py:_stage_render_real` —
  post-render cache-sweep emission.
- `pipeline/render/video.py:render_long_form` — emits long-form
  envelope/script/video.
- `control/routes/render_routes.py:artifact_redirect` — signed-URL
  endpoint.
- `web-next/lib/types.ts:ArtifactEntry` + extended `Job.artifacts` —
  TS shape.
- `web-next/lib/api.ts:jobsApi.artifactUrl` — URL helper.
- `web-next/app/app/render/[jobId]/page.tsx:LiveArtifactsCard` —
  inline preview renderer.

## See also

- Memory: `feedback_live_artifact_emission.md`.
