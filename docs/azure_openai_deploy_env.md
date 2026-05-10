# Azure OpenAI on Cloud Run — env-var triplet rule

> **CLASS-OF-BUG, 2026-05-10.** A Cloud Run service that uses Azure
> OpenAI needs **all four** values mounted on the service revision —
> `AZURE_OPENAI_API_KEY` (secret) + `AZURE_OPENAI_ENDPOINT` +
> `AZURE_OPENAI_API_VERSION` + `AZURE_OPENAI_MODEL` (env vars). With
> only the API key, the openai SDK builder silently bails and every
> Azure-backed feature fails closed.

## The bug we hit

`ytfactory-web` was deployed with `AZURE_OPENAI_API_KEY` mounted from
secret `azure-openai-key` but **no `AZURE_OPENAI_ENDPOINT` env var**.
Two user-facing features silently died:

| Feature | File | Failure mode |
|---|---|---|
| Chat assistant ("Describe your Short") | `control/chat_service.py:96-105` | `_build_client()` returned `None` → `is_configured = False` → endpoint replied "Chat AI not configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY …" |
| Niche-form **Generate** auto-fill | `control/routes/niche_specs_routes.py:148-151` | `_draft_via_azure()` returned `None` → fell through to `_draft_stub()` → user got the deterministic stub (truncated label, generic prompt-style-guide) instead of an Azure-generated NicheDoc |

Both paths gate on `endpoint AND key`. Missing the endpoint is just as
fatal as missing the key — but Cloud Run's secret/env split makes it
look like only the key is required (because that's the one that goes
through `--set-secrets`).

> **Cross-reference (2026-05-10):** `--set-secrets` is destructive
> — see [`cloud_run_set_secrets_destructive.md`](./cloud_run_set_secrets_destructive.md).
> When adding a new secret to a Cloud Run service via `--update-secrets`,
> the SAME session must amend `cloud/<service>/deploy.sh` so the next
> redeploy doesn't regress the addition.

## The check, in code

```python
endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
api_key  = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
if not endpoint or not api_key:
    return None  # silent degradation
```

There is no startup error, no warning log, no health-check signal.
The service comes up green, then every Azure call short-circuits.

## The rule

**Every Cloud Run service that talks to Azure OpenAI must mount the
full triplet, not just the key:**

```bash
gcloud run services update <service> \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-secrets="AZURE_OPENAI_API_KEY=azure-openai-key:latest" \
  --update-env-vars="\
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com,\
AZURE_OPENAI_API_VERSION=2025-04-01-preview,\
AZURE_OPENAI_MODEL=gpt-5.3-chat"
```

For deploy scripts, bake the triplet into the `--set-env-vars` line so
re-deploys can't regress. See `cloud/web-server/deploy.sh:70` for the
canonical pattern (post-2026-05-10).

`docs/cloudrun_render_worker.md:144-145` already documented this for
the render-worker JOB. The same rule applies to **every Cloud Run
service that imports `openai.AzureOpenAI`**.

## Affected services + audit recipe

| Service | Uses Azure? | Triplet mounted? |
|---|---|---|
| `ytfactory-web` | ✅ chat + niche-draft | ✅ post-2026-05-10 (revision `ytfactory-web-00026-gw2`) |
| `ytfactory-render-worker-v2` JOB | ✅ rewrite, cast, prompts, critic, audio_critic, imitate (when `YTFACTORY_LLM_BACKEND=azure_openai`) | ✅ already had it (deploy script + runbook) |
| `ytfactory-clone-video-worker` | optional (`AZURE_OPENAI_WHISPER_DEPLOYMENT`) | n/a — only if Whisper deployment configured |

To audit any future Cloud Run service for this drift:

```bash
# 1. Find services that import AzureOpenAI:
grep -rln "from openai import AzureOpenAI\|openai\.AzureOpenAI" \
  --include="*.py" pipeline/ control/ web/ cloud/

# 2. For each one, check the deployed revision env:
gcloud run services describe <service> \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --format='value(spec.template.spec.containers[0].env[].name)' \
  | tr ';' '\n' | grep AZURE_OPENAI

# 3. Expect: AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT +
#           AZURE_OPENAI_API_VERSION + AZURE_OPENAI_MODEL
#    (model + version may be missing if defaults are acceptable, but
#    ENDPOINT is mandatory.)
```

## How the bug surfaced

The user clicked **+ Add a new niche → Generate** on the hosted UI.
The dialog populated the form — but with the deterministic stub's
fields (`label = "Animated retelling of horror reddit stories with
creepy whis"` truncated at 60 chars), not the real Azure response.
This was hard to spot because the UI does succeed — there's no error
toast, no 5xx — the user just gets low-quality auto-fill content.

After mounting the triplet on the revision, the same prompt produced
`label="Whispered Reddit Horror"`, `source_ref="r/nosleep"`, and
genuine voice/tone guidance.

## Why this is CLASS-OF-BUG, not ONE-OFF

Every future Cloud Run service that integrates Azure (e.g., a future
`ytfactory-vision-worker`, `ytfactory-comment-bot`, …) will hit the
same trap if the deploy script mounts only the key. The failure mode
is silent and looks like "AI is dumb today" rather than "AI is
unconfigured", which delays detection. The deploy-script audit
recipe (above) is the durable countermeasure.

## Cross-references

- `cloud/web-server/deploy.sh` — fixed (env triplet now in --set-env-vars)
- `cloud/render-worker-v2/deploy.sh` — already correct
- `docs/cloudrun_render_worker.md:144-145` — documented for the JOB
- `docs/architecture.md` — **fixed inline 2026-05-10**: Deployment
  section now points at `cloud/web-server/deploy.sh` (the source of
  truth for env vars + concurrency + secrets) instead of a stale
  inline gcloud block; (still shows `ytfactory-prod` / `ytfactory-control`
  in the "retired" prose, which is intentional).
- `control/chat_service.py:96-105` — the `endpoint AND key` gate
- `control/routes/niche_specs_routes.py:148-151` — the `endpoint AND
  api_key` gate
- Memory: `feedback_azure_openai_deploy_triple.md`
