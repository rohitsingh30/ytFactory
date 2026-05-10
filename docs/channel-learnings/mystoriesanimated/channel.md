

## Upload throttle
Uploads on this channel auto-enforce a ≥1h gap between consecutive public moments — see [docs/upload_throttle.md](../../docs/upload_throttle.md). Override by passing an explicit `publish_at` (dashboard "Publish at" picker).

## Orchestrated rewrite stage (cross-channel, mirrored here)

`pipeline/llm/rewrite.py` is the FIRST stage to use the
constraint-aware orchestrator (commit `d2ff3b5`, validated
`cake-orch-v8` for this channel). When the LLM violates a
`script_check` rule (e.g. `missing_cta`, `missing_field`), the
orchestrator catches it inline, builds a focused regen prompt, and
retries up to `YTFACTORY_LLM_MAX_RETRIES` (default 2). Cake-orch-v6
took 3 attempts and still shipped; pre-orchestrator the same
narration would have crashed the whole render minutes later.

CTA examples in the rewrite prompt are now derived from
`script_check._CTA_RULES` so prompt and validator can never drift —
see [`docs/prompt_validator_drift_invariant.md`](../../prompt_validator_drift_invariant.md).

If you want to bypass the orchestrator while debugging:
`YTFACTORY_REWRITE_USE_LEGACY=1`. See
[`docs/llm_orchestrator.md`](../../llm_orchestrator.md) for the full
picture.
