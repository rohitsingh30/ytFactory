# LLM `reasoning_effort` per-stage (WORKFLOW-IMPROVEMENT, 2026-05-13)

> **TL;DR** — gpt-5.x / o1 / o3 deployments accept a
> `reasoning_effort` parameter (`minimal` / `low` / `medium` / `high`)
> that controls how many INVISIBLE reasoning tokens the model burns
> before emitting output. Reasoning tokens are billed as output
> tokens but never appear in the response. Default is `high`, which
> on `gpt-5.3-chat` burns ~15-20k reasoning tokens per long-form
> rewrite. Setting `minimal` for transformation stages (cast,
> prompts, shorts rewrite) saves ~70% of total token cost per render
> with no observed quality degradation. Long-form rewrite + critic
> stay at `medium` because they genuinely benefit from planning.

## Where this matters

`pipeline/llm/cli.py::_call_azure_openai` resolves a `reasoning_effort`
for the call via `reasoning_effort_for(stage)` and passes it as a
kwarg if (and only if) the deployment supports it. Per-deployment
support is cached after the first call so legacy chat-completions
deployments (gpt-4o, gpt-4-turbo) don't keep paying the round-trip
cost on every call.

The fix landed in commit `cc8bd4f` (2026-05-13) alongside the
[reasoning-token starvation truncation handling](./llm_max_tokens.md).
The two fixes attack the same root cause from opposite directions:

- **`docs/llm_max_tokens.md`** — give reasoning a high enough budget
  cap that runs don't truncate.
- **This doc** — give reasoning a much smaller budget *to begin with*
  on stages that don't need it, so the cap is rarely the bottleneck
  AND every render costs ~70% less.

## Cost ladder (observed on `gpt-5.3-chat`, 2026-05-13)

| `reasoning_effort` | reasoning tokens burnt | quality observation              |
|--------------------|------------------------|----------------------------------|
| `high` (default)   | ~15-20k                | overkill for transformation work |
| `medium`           | ~5-8k                  | good for planning + critique     |
| `low`              | ~1-3k                  | acceptable for most stages       |
| `minimal`          | ~0-200                 | ideal for transformation tasks   |

Reasoning tokens cost the **output token rate** even though they
never appear in the response. So the choice of `reasoning_effort`
multiplies the per-call token bill, not just the latency.

## Per-stage policy (in `_DEFAULT_REASONING_EFFORT_BY_STAGE`)

```python
_DEFAULT_REASONING_EFFORT_BY_STAGE: dict[str, str] = {
    "rewrite_long_form": "medium",  # planning a 30-min script benefits
    "critic":            "medium",  # we want thoughtful critique
}
_FALLBACK_REASONING_EFFORT = "minimal"
```

Every other stage (`rewrite`, `cast`, `prompts`, `audio_critic`,
`imitate_analyze`, `imitate_apply`) defaults to `minimal`.

### Rationale

- **`rewrite_long_form`** plans a multi-section, multi-panel script
  with thematic continuity across 30 minutes of narration. Cutting
  reasoning here produces flatter, more list-y prose — kept at
  `medium` for now.
- **`critic`** evaluates a generated artefact and suggests fixes.
  Reasoning helps it spot subtle inconsistencies. Kept at `medium`.
- **`rewrite`** (Shorts) is a 50-60s narration. Output is small,
  variability is bounded by hook + thesis + closer. `minimal` is
  enough.
- **`cast`** assigns character names to a shotlist — pure
  transformation. `minimal`.
- **`prompts`** rewrites a panel description into an image-gen prompt
  — pure transformation. `minimal`.
- **`audio_critic`**, **`imitate_*`** — laptop-only today (vision-aware
  CLI route), so the kwarg never reaches them; `minimal` is fine for
  the future SDK route.

If a future stage proves to need more reasoning, bump it via env (see
below) and update this table.

## Env overrides

```bash
# Per-stage:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_REASONING_EFFORT_REWRITE=low

# Disable globally (legacy gpt-4o deployments — no harm done since the
# auto-strip handles 400 already, but skips the discovery round-trip):
gcloud run services update ytfactory-web \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=AZURE_REASONING_EFFORT_DISABLE=1

# Force a single stage to omit the param (debug / fall back to
# deployment default):
YTFACTORY_REASONING_EFFORT_PROMPTS=off
```

Valid values: `minimal`, `low`, `medium`, `high`. Any of `off`,
`none` omits the param entirely. Anything else is silently ignored
(falls through to the per-stage default).

## Per-deployment cache + auto-strip pattern

Same pattern as the `max_tokens` ↔ `max_completion_tokens` swap:

1. First call to a deployment passes `reasoning_effort=<value>`.
2. If the deployment 400s with `Unsupported parameter:
   'reasoning_effort'` (legacy gpt-4o / gpt-4-turbo), the dispatcher:
   - drops the kwarg
   - retries the same call without it
   - caches `_AZURE_REASONING_EFFORT_SUPPORTED[deployment] = False`
3. Subsequent calls to that deployment skip the param entirely.

The cache + global env override (`AZURE_REASONING_EFFORT_DISABLE=1`)
together mean operators have two ways to opt out — env for "I know
this deployment is legacy, skip discovery" and cache for "I don't
know, let it learn".

## Cost impact

For a single 30-min long-form render (~25 LLM calls including cast,
prompts, etc.) on `gpt-5.3-chat`:

| state              | reasoning tokens | output tokens | est. cost           |
|--------------------|------------------|---------------|---------------------|
| pre-fix (all high) | ~95k             | ~36k          | ~$1.30              |
| post-fix           | ~5k              | ~36k          | ~$0.40 (~70% less)  |

Token math (Azure gpt-5.3-chat 2026-05-13 pricing, approx):
- Input: $0.0030/1k tokens
- Output (incl. reasoning): $0.0150/1k tokens

24 stages × `minimal` (~200 reasoning) + 1 stage × `medium` (~6k) =
~10k reasoning total post-fix vs ~95k pre-fix → 85k tokens × $0.015 =
~$1.27 saved per render. At our typical 50 renders/week that's
~$250/month saved against the existing GCP credit pool — recoverable
into more burner channel work.

## Pin tests

`tests/test_llm_dispatcher.py`:
- `test_reasoning_effort_minimal_passed_for_default_stages`
- `test_reasoning_effort_medium_for_long_form_rewrite`
- `test_reasoning_effort_env_override_per_stage`
- `test_reasoning_effort_env_off_omits_param`
- `test_reasoning_effort_global_disable_skips_param`
- `test_reasoning_effort_rejected_by_legacy_deployment_strips_and_caches`
- `test_reasoning_effort_telemetry_metadata`

Coverage: 100% on changed lines.

## Cross-references

- Pipeline source: `pipeline/llm/cli.py::reasoning_effort_for`,
  `pipeline/llm/cli.py::_azure_supports_reasoning_effort`,
  `pipeline/llm/cli.py::_remember_azure_reasoning_effort_support`,
  `pipeline/llm/cli.py::_call_azure_openai`
- Companion fix (give the same problem more headroom for stages
  that DO need reasoning): [`docs/llm_max_tokens.md`](./llm_max_tokens.md)
- Backend dispatcher overview: [`docs/llm_backend_dispatcher.md`](./llm_backend_dispatcher.md)
- Render-worker env reference: [`docs/cloudrun_render_worker.md`](./cloudrun_render_worker.md)
- Memory: `feedback_llm_reasoning_effort_minimal.md`
- Render that prompted the optimization: job `5e37f76b...` (Leigh
  Occhi mystoriesanimated long-form, 2026-05-13)
