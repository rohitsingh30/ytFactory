# Cloud Run render worker (Layer 2)

> **Status:** scaffolded 2026-05-09. Real-mode handlers wired
> 2026-05-09. **LLM backend dispatcher landed 2026-05-10** —
> `pipeline/llm/cli.py` now dispatches `call_claude_cli` to one of
> three implementations (`cli` / `azure_openai` / `anthropic_sdk`) on
> the `YTFACTORY_LLM_BACKEND` env. **Default in cloud = Azure OpenAI**
> (reuses the chat-assistant's `AZURE_OPENAI_*` secrets — no separate
> spend). Anthropic SDK is the optional alternative.

This is the cloud-native replacement for the laptop agent. The product
no longer depends on a Mac being awake.

## What it is

A Cloud Run **Job** (not a Service — Jobs run-to-completion) deployed
as `ytfactory-render-worker-v2` in `asia-southeast1`. The control plane
triggers one execution per render via the `google-cloud-run` SDK.

```
   user clicks Render
        │
        ▼
   POST /api/render  (control plane)
        │
        │  jobs/<job_id> created in Firestore
        │  YTFACTORY_RENDER_BACKEND=cloudrun → control/cloud_run.trigger_render_job()
        ▼
   Cloud Run Job execution
        │
        │  reads jobs/<job_id> from Firestore
        │  walks 7 stages (rewrite → cast → images → tts → asr → compose → upload)
        │  writes timeline events back to jobs/<job_id> per stage
        │  uploads mp4 + thumb to gs://ytfactory-prod-v2-artifacts/jobs/<job_id>/
        ▼
   UI poll loop (/api/jobs/<job_id>) sees live progress
```

## LLM backends — three choices

| Backend | When | Cost story |
|---|---|---|
| `cli` | Laptop dev (`claude` binary on PATH) | Flat-rate via Pro/Max OAuth plan. Free per call. |
| **`azure_openai`** (cloud default) | Cloud Run JOB | Reuses your existing `AZURE_OPENAI_*` deployment — same one the chat assistant already uses. **No new bill.** |
| `anthropic_sdk` | Cloud Run JOB (alternative) | Pay-per-token Anthropic API. Opens a separate billing line. |

Selection order (implemented in `pipeline/llm/cli.py::_choose_backend`):

1. `YTFACTORY_LLM_BACKEND` env, if set to a valid value
   (`cli` / `azure_openai` / `anthropic_sdk`).
2. Else: if the `claude` binary is on `PATH` → `cli` (laptop dev: free
   OAuth-billed Pro/Max plan).
3. Else: if Azure env present → `azure_openai`.
4. Else: if `ANTHROPIC_API_KEY` set → `anthropic_sdk`.
5. Final fallback: `cli` (will surface a clear error if `claude` isn't
   installed).

Vision-aware kwargs (`add_dirs`, `allowed_tools`) are only supported
on the `cli` backend today — the SDK backends raise `ClaudeCLIError`
if you pass them. Stages that need vision (anatomy_check, critic,
imitate_analyze) are laptop-only for now; the cloud render worker only
invokes pure-text stages (rewrite, cast, prompts).

### Azure OpenAI tier mapping

The pipeline calls each stage with a tier alias (`haiku`/`sonnet`/`opus`).
On Azure, those map to deployment names — overridable per-tier:

```bash
# Per-tier overrides (default: gpt-4o-mini for haiku/sonnet, gpt-4o for opus)
AZURE_OPENAI_MODEL_HAIKU=gpt-4o-mini    # cheap fast (rewrite, cast)
AZURE_OPENAI_MODEL_SONNET=gpt-4o-mini   # mid (prompts)
AZURE_OPENAI_MODEL_OPUS=gpt-4o          # premium (critic)

# Or one-size-fits-all override
AZURE_OPENAI_MODEL=gpt-4o-mini
```

If your Azure project has a `gpt-5.3-chat` deployment (per
`README.md`), point the right tier at it:
`AZURE_OPENAI_MODEL_OPUS=gpt-5-3-chat`.

## Render modes

| Mode | What runs |
|---|---|
| **`real`** (default) | rewrite → Azure OpenAI; cast+images+tts+asr+compose → `pipeline.render.shorts` subprocess against cloud TTS + image services; upload → GCS. |
| `stub` | Each stage sleeps ~2s; the upload stage drops a placeholder mp4. Useful when LLM keys aren't wired yet. |

Flip via `YTFACTORY_RENDER_MODE` on the JOB.

## Customize-form override contract (2026-05-10)

The worker subprocess auto-forwards every `proposal.channel_overrides`
entry from Firestore to `python -m pipeline.render.shorts` as a
repeated `--override KEY=VALUE` flag. New user-facing knobs on the
create-page form land at the renderer with no worker change required.

The full 3-layer wiring rule (schema → form submit → worker forward)
is documented in [`docs/customize_form_to_render_contract.md`](./customize_form_to_render_contract.md).
The worker side is the third layer; if you're adding a new field, the
schema + submit changes are usually the only two you need to touch.

Anti-pattern this fixes: pre-2026-05-10 the worker had no override
translation, so any new schema field round-tripped through Firestore
and died silently at the worker boundary. Symptom was wrong output,
not a visible error.

## Cloud-native pipeline ports (delivered 2026-05-09)

| Stage | Today (laptop) | Cloud port |
|---|---|---|
| rewrite | `claude` CLI | **Azure OpenAI** (default) or Anthropic SDK; gated by `YTFACTORY_LLM_BACKEND` |
| cast | (renderer-internal) | unchanged — uses the same backend selector |
| images | `cloudrun_flux2_klein` | already cloud-native |
| tts | `cloudrun_chatterbox` / `cloudrun_indicparler` | already cloud-native |
| asr | `whisper-mlx` (Apple-only) | **`faster-whisper`** (CPU) |
| compose | `ffmpeg` | already CPU-friendly |
| upload | local disk → GCS | direct GCS upload |

All adapters are gated by env so **laptop** behaviour is unchanged.

## Deploy

```bash
cd /Users/rohit/ytFactory
./cloud/render-worker-v2/deploy.sh
```

Builds the image (~5 min) and creates/updates the Job in `asia-southeast1`
with `YTFACTORY_LLM_BACKEND=azure_openai` baked in.

**Pre-flight: wire the Azure OpenAI secrets.** Default mode needs
the same `AZURE_OPENAI_*` triple your chat assistant already uses.

```bash
# Create the API key secret (one-time)
echo -n "<your-azure-openai-key>" | gcloud secrets create azure-openai-key \
    --project=ytfactory-prod-v2 --replication-policy=automatic --data-file=-

# Mount onto the JOB + set the endpoint
gcloud run jobs update ytfactory-render-worker-v2 \
    --project=ytfactory-prod-v2 --region=asia-southeast1 \
    --update-secrets=AZURE_OPENAI_API_KEY=azure-openai-key:latest \
    --update-env-vars="^|^AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/|AZURE_OPENAI_MODEL=gpt-4o-mini|AZURE_OPENAI_API_VERSION=2025-04-01-preview"
```

Without these, the JOB will fail at the rewrite stage. Flip to stub
mode if you want to demo wiring without an LLM key.

> **Same triplet rule applies to every Azure-using Cloud Run service —
> not just this JOB.** The `ytfactory-web` service hit the inverse
> failure on 2026-05-10 (mounted only `AZURE_OPENAI_API_KEY`, missing
> `ENDPOINT` / `VERSION` / `MODEL` → chat assistant + niche-form
> auto-generate silently dropped to "AI not configured" stubs).
> Full post-mortem + audit recipe: [`docs/azure_openai_deploy_env.md`](./azure_openai_deploy_env.md).

### Optional: Anthropic SDK as the LLM backend

```bash
echo "sk-ant-..." | gcloud secrets create ytfactory-anthropic-key \
    --project=ytfactory-prod-v2 --replication-policy=automatic --data-file=-

gcloud run jobs update ytfactory-render-worker-v2 \
    --project=ytfactory-prod-v2 --region=asia-southeast1 \
    --update-env-vars=YTFACTORY_LLM_BACKEND=anthropic_sdk \
    --update-secrets=ANTHROPIC_API_KEY=ytfactory-anthropic-key:latest
```

## Activate from the control plane

```bash
export YTFACTORY_RENDER_BACKEND=cloudrun
export YTFACTORY_CLOUDRUN_JOB=ytfactory-render-worker-v2
export YTFACTORY_CLOUDRUN_REGION=asia-southeast1
export GOOGLE_CLOUD_PROJECT=ytfactory-prod-v2
```

`POST /api/render` triggers a Cloud Run Job execution; the UI sees
live progress via `/api/jobs/{id}`.

To switch back to dev: `unset YTFACTORY_RENDER_BACKEND` (defaults to
`sim`, in-process simulated worker).

## Trigger one execution manually (smoke test)

```bash
gcloud run jobs execute ytfactory-render-worker-v2 \
    --project=ytfactory-prod-v2 \
    --region=asia-southeast1 \
    --update-env-vars='YTFACTORY_JOB_ID=<some-job-id>'
```

## Cost (per render)

- **Azure OpenAI (default)** — covered by your existing chat-assistant
  Azure spend cap (`control/rate_limit.daily_cap_usd`). Current
  pipeline call mix at `gpt-4o-mini` defaults: **~$0.02-0.05/render**.
  At `gpt-4o` for the heavier stages: ~$0.10-0.20/render.
- **Anthropic SDK** — ~$0.05 (all Haiku) → ~$1.00 (all Opus) per render.
- **Stub mode** — $0.
- **Job CPU + GCS** — ~$0.02/render (unchanged across modes).
- **TTS + image GPU services** — per-call billing on existing services.

## Rollback

The laptop agent path still works as a fallback:

```bash
unset YTFACTORY_RENDER_BACKEND  # default = sim
# Or:
export YTFACTORY_RENDER_BACKEND=laptop
.venv/bin/python -m workers.agent.main
```

## Lessons from the cake-orch smoke runs (2026-05-10)

Eight smoke runs to get the website end-to-end. Every fix below
remains in production and is tested.

| run | what failed | fix | commit |
|---|---|---|---|
| v1 | `mode=stub` even though docs claimed `real` was default | env defaulted to `stub` in code; flip on the live JOB | (env) |
| v2 | `FileNotFoundError: claude` on every LLM call | LLM backend dispatcher with Azure backend | `c4689f7` |
| v3 | env wiped + `gcloud not found` for ID-token fetch | metadata-server auth path before falling through to gcloud | `9e36af5` |
| v4 | duplicate `cloudrun_auth` modules drifted | unify `pipeline/cloud/cloudrun_auth.py` to a re-export shim | `ebf4933` |
| v5 | ffmpeg concat list-file used relative chunk paths | resolve absolute paths before writing the list-file | `212ed4c` |
| v5 | FLUX.2 service `CUDA out of memory` (real cause: Qwen3 not in VRAM math) | `enable_model_cpu_offload()` — see `docs/cloudrun_image.md` | `6d90a66` |
| v6 | `RenderPaths.from_channel_yaml` didn't recognize `pipeline/channels/<slug>.yaml` | new path patterns — see `docs/channel_layout.md` | `db15530` |
| v7 | worker mp4-lookup hardcoded `Path(channel_yaml).parent` | look up via the same `RenderPaths` resolver the renderer uses | `d3d1f88` |
| v8 | **mp4 + thumb landed in GCS, status=done** | — | — |

Recurring pattern across all eight: every time a new layer fired,
**a different module on the worker side made a layout assumption that
diverged from what the renderer / image-service actually did**. The
fix is always the same — share one resolver / one source of truth.
The orchestrator pattern (`docs/llm_orchestrator.md`) makes this
explicit at the validator layer; the path bugs above are the same
class of drift at the filesystem layer.

**Operator gotcha:** `gcloud run jobs update --update-env-vars` is
DESTRUCTIVE on this revision shape — it COLLAPSES every env not
named in the flag. Lost the entire Azure + CLOUDRUN_*_URL set during
v3 → v4. Always inspect the env after the command and re-add anything
missing. See `docs/cloud_run_set_secrets_destructive.md`.

## Files

- `cloud/render-worker-v2/Dockerfile` — image
- `cloud/render-worker-v2/requirements.txt` — CPU-only deps + openai SDK + anthropic SDK
- `cloud/render-worker-v2/entrypoint.py` — Firestore-driven main loop with stub + real mode handlers
- `cloud/render-worker-v2/deploy.sh` — Cloud Build + Job deploy
- `control/cloud_run.py` — control-plane trigger
- `pipeline/llm/cli.py` — three-backend selector + Azure adapter + Anthropic adapter (see `docs/llm_backend_dispatcher.md`)
- `pipeline/llm/orchestrator.py` — constraint-aware retry runner (see `docs/llm_orchestrator.md`)
- `pipeline/cloudrun_auth.py` — Google ID token via metadata-server / gcloud
- `pipeline/paths.py` — `RenderPaths.from_channel_yaml` (see `docs/channel_layout.md`)
- `pipeline/asr.py` — `faster_whisper` backend
