# LLM backend dispatcher (cross-channel)

> **Landed 2026-05-10** in commit `c4689f7`. Closes the gap that
> kept the Cloud Run render-worker stuck in `mode=stub`: even with
> `YTFACTORY_RENDER_MODE=real`, every stage call would have raised
> `FileNotFoundError: claude` because the worker image doesn't ship
> the binary.

## What changed

`pipeline/llm/cli.py::call_claude_cli` is now a **dispatcher**, not a
thin subprocess wrapper. It routes on `YTFACTORY_LLM_BACKEND` to one
of three implementations:

| backend | when | cost story |
|---|---|---|
| `cli` | laptop dev (`claude` binary on PATH) | flat-rate via Pro/Max OAuth, free per call |
| **`azure_openai`** (cloud default) | render-worker JOB | reuses chat-assistant's `AZURE_OPENAI_*` deployment — **no new bill** |
| `anthropic_sdk` | cloud alternative | pay-per-token Anthropic API; opens a separate billing line |

Auto-detection (when `YTFACTORY_LLM_BACKEND` not set):

1. `claude` binary on PATH → `cli` (laptop)
2. else `AZURE_OPENAI_*` env present → `azure_openai`
3. else `ANTHROPIC_API_KEY` set → `anthropic_sdk`
4. else `cli` (will surface a clear error if claude isn't installed)

`call_llm` is a public alias — newer code can use the friendlier name.

## Tier mapping

Every existing call site passes a tier alias (`haiku` / `sonnet` /
`opus`). The dispatcher resolves it per backend:

```bash
# Azure: AZURE_OPENAI_MODEL_<TIER> > AZURE_OPENAI_MODEL > defaults
AZURE_OPENAI_MODEL_HAIKU=gpt-4o-mini
AZURE_OPENAI_MODEL_OPUS=gpt-5.3-chat
# Anthropic: ANTHROPIC_MODEL_<TIER> > defaults (haiku-4-5, sonnet-4-5, opus-4-5)
```

Production today uses a single `AZURE_OPENAI_MODEL=gpt-5.3-chat`
deployment (the chat assistant's deployment) for all tiers — the
generic env applies to every tier alias.

## JSON output

- `cli` — keeps `--json-schema` structured output mode.
- `azure_openai` — `response_format={"type":"json_schema",...}` when
  schema set; `json_object` otherwise. Falls back to no
  `response_format` if the deployment rejects it.
- `anthropic_sdk` — augments user prompt with JSON-only instructions
  ("matching this schema" / "ONLY a JSON object"); parses client-side
  via `_parse_inner_json`.

## Reasoning-effort per stage (2026-05-13)

The Azure backend resolves a `reasoning_effort` per-call via
`reasoning_effort_for(stage)` and passes it as a kwarg if (and only
if) the deployment accepts it. Per-deployment support is cached the
same way as `AZURE_OPENAI_TOKEN_PARAM` (try → cache rejection →
skip on subsequent calls).

Default is `minimal` for transformation stages; `medium` for
`rewrite_long_form` and `critic` (where reasoning genuinely helps).
Saves ~70% of token cost per render. Full design in
[`docs/llm_reasoning_effort.md`](./llm_reasoning_effort.md).

## Vision-aware kwargs

`add_dirs` + `allowed_tools` are CLI-only today. The SDK backends
raise `ClaudeCLIError` if you pass them — gating critic /
anatomy_check / imitate_analyze to the laptop. When IP-Adapter image
inlining lands the SDK paths can take vision too.

## Toggling

```bash
# Switch the live cloud worker to Anthropic SDK instead of Azure:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_LLM_BACKEND=anthropic_sdk \
  --update-secrets=ANTHROPIC_API_KEY=anthropic-key:latest

# Or back to stub mode while debugging:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_RENDER_MODE=stub
```

**Beware:** `--update-env-vars` is destructive on this Cloud Run
revision shape — it COLLAPSES every env not named in the flag down to
just the new ones. Lost the entire `AZURE_OPENAI_ENDPOINT` +
`CLOUDRUN_*_URL` set during cake-orch-v3 → v4. See
`docs/cloud_run_set_secrets_destructive.md`. Always inspect the env
after this command and re-add anything missing.

## What this enables

- The cake-orch-v8 smoke ran fully end-to-end through Azure
  `gpt-5.3-chat` with no laptop dependency.
- The orchestrator's auto-retry (see `docs/llm_orchestrator.md`)
  can now actually fire from the cloud — previously every retry
  would have crashed identically.
- Anthropic SDK is one env flag away if Azure becomes the wrong
  choice for any reason.

## Tests

- `tests/test_llm_dispatcher.py` — dispatcher routing, tier mapping,
  vision-kwargs gating (29 tests).
- `tests/test_pipeline_llm.py` — Azure + Anthropic adapter spec
  (35 pre-existing tests, all green against the new impl).

## Related

- `docs/llm_orchestrator.md` — what runs on top of this dispatcher.
- `docs/llm_max_tokens.md` — per-stage output-token cap. The dispatcher
  reads `_DEFAULT_MAX_TOKENS_BY_STAGE` to keep long-form rewrite from
  silently truncating at 4096 tokens. (2026-05-12)
- `docs/prompt_validator_drift_invariant.md` — what the orchestrator
  catches when LLMs drift from validators.
- `docs/cloudrun_render_worker.md` — the cloud worker that uses this.
- Memory: `feedback_llm_backend_dispatcher.md`,
  `feedback_llm_sdk_max_tokens.md`.
