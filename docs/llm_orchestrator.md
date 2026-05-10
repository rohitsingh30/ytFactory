# Constraint-aware LLM orchestrator (cross-channel)

> **Landed 2026-05-10** in commits `d2ff3b5` (RewriteContract) and
> `ae27385` (pipeline runner + cache + fix router + critic +
> flavours). Validated end-to-end on the cake-story smoke
> (`cake-orch-v8` shipped a real 8.45 MB mp4 to GCS).

## Why this exists

Pre-orchestrator, every LLM stage looked like:

1. Hand-write a prompt next to (but not derived from) the validator.
2. `subprocess.run(["claude", "-p", prompt])` once.
3. Way later — sometimes 30 images later — `script_check.report()`
   raises and the entire render aborts.

Three problems compound:

- **Drift** — prompt examples and validator regex aren't from the
  same source (see `docs/prompt_validator_drift_invariant.md`).
- **No retry** — single LLM shot, no awareness of which constraint
  failed.
- **Wasted cost** — TTS/image work happens before the validator fires.

## The pattern

```
StageContext  →  StageContract.gather_constraints()
                 StageContract.build_prompt(constraints)         # validator-derived examples
                 ↓
                 call_claude_cli (via dispatcher → Azure / Anthropic / cli)
                 ↓
                 StageContract.validate(output)                   # SAME validator the renderer runs
                 ↓
                 errors? → StageContract.regen_prompt(prev, fixes) → retry up to YTFACTORY_LLM_MAX_RETRIES
                 no errors → StageResult(output, attempts, warnings, ...)
```

The runner is `pipeline/llm/orchestrator.py::run_stage`. Default
retry budget is 2 (env `YTFACTORY_LLM_MAX_RETRIES`).

**Proven in production.** cake-orch-v6 took 3 attempts on the rewrite
stage — `missing_field` on attempt 1, `missing_cta` on attempt 2,
clean on attempt 3. The render shipped instead of crashing.

## The seven pieces

| # | Piece | File | Status |
|---|---|---|---|
| 1 | `Fix` object — typed report from any validator | `pipeline/llm/fix.py` | live |
| 2 | `StageContract` Protocol + `run_stage` runner | `pipeline/llm/orchestrator.py` | live |
| 3 | Validators expose constraints AS DATA | `pipeline/llm/{script_check,cast_lint,prompt_lint,image_lint}.py` | partial — script_check + cast_lint + prompt_lint + image_lint done; others to come |
| 4 | AI-judge stages (vision LLM) | `pipeline/llm/judges/` | scaffolded (image_judge), not wired yet |
| 5 | Content-addressable cache | `pipeline/llm/cache.py` | live (LocalCache + GCSCache + NoopCache) |
| 6 | Fix routing protocol | `pipeline/llm/fix_router.py` | live |
| 7 | Post-render critic loop | `pipeline/llm/contracts/critic_contract.py` + runner cascade | live in tests, not wired into cloud worker yet |

Plus:

- **RenderJob context bag** — `pipeline/llm/render_job.py` — single
  object flowing through every stage.
- **STAGE_DAG + topo runner** — `pipeline/llm/dag.py` +
  `pipeline_runner.py`.
- **Channel × format flavours** — `pipeline/llm/contracts/flavours/` —
  AITAFlavour + CliffhangerFlavour today; ranked / kathaa / etc. to
  come.

## What's wired into production today

- **rewrite stage** uses the orchestrated path by default
  (`pipeline/llm/rewrite.py` calls `_rewrite_via_orchestrator`).
  Fall back to legacy single-shot via `YTFACTORY_REWRITE_USE_LEGACY=1`.
- **All other stages** still go through the legacy single-shot path
  inside `pipeline/render/shorts.py`. Cast / prompts / image / critic
  contracts EXIST and are tested but not yet swapped in.

The cloud worker shells out to `pipeline.render.shorts` so the
orchestrator only owns rewrite end-to-end on the cloud path. The
`pipeline_runner.run_pipeline()` entry point exists but isn't yet
called from `cloud/render-worker-v2/entrypoint.py`. That's the
follow-up integration work; orchestrator-side everything is built
and tested.

## Cache reuse (for when the runner integration lands)

Bazel-style content-addressable. Cache key = SHA-256 of (stage +
channel + channel_cfg + upstream + sub_index). Granular per artifact:

- Beat #14 image regen → invalidates ONE key, the other 29 hit cache.
- Same prompt + same input + same model → identical key → cache hit
  → 0 API cost.

Two backends share one interface:

- `LocalCache` (`~/.cache/ytfactory/`) for laptop dev.
- `GCSCache` (`gs://<YTFACTORY_CACHE_BUCKET>/cache/`) for cloud.

Selection via env: `YTFACTORY_CACHE_BACKEND=gcs` +
`YTFACTORY_CACHE_BUCKET=ytfactory-prod-v2-cache`.

## Critic-FIX cascade (designed, tested, not yet on the prod path)

After compose, the critic stage runs as an LLM call. Verdict is
SHIP / FIX / BLOCK. On FIX:

1. Group `Fix` list by owning stage via `fix_router.cascade()`.
2. `cache.delete()` the affected keys (and every transitively
   downstream stage's key).
3. Re-queue the stages.
4. Bounded by `YTFACTORY_CRITIC_MAX_PASSES` (default 2).

## Tests

- `tests/test_llm_rewrite_orchestrated.py` — RewriteContract end-to-end
  (16 tests, including the cake-story regression).
- `tests/test_llm_pipeline_runner.py` — runner + cache + fix router +
  DAG + critic cascade + flavours (27 tests).
- `tests/test_llm_dispatcher.py` — dispatcher routing (29 tests).
- `tests/test_pipeline_llm.py` — Azure + Anthropic adapters (35 tests).

All 152 LLM-suite tests green as of `f8070a1`.

## Toggling

```bash
# Increase retry budget per stage:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_LLM_MAX_RETRIES=3

# Disable cache (benchmarking):
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_CACHE_DISABLED=1

# Bypass orchestrator for rewrite (compare legacy path):
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_REWRITE_USE_LEGACY=1
```

## Related

- `docs/llm_backend_dispatcher.md` — the layer below this.
- `docs/prompt_validator_drift_invariant.md` — the bug class this
  catches.
- `docs/cloudrun_render_worker.md` — the worker that ultimately calls
  this.
- Memory: `feedback_llm_orchestrator.md`.
