"""Cross-side code shared by the cloud control plane AND the laptop agent.

What lives here:
- `schema.py` — pydantic / dataclass envelopes that round-trip through
  Firestore + HTTP (TaskEnvelope, JobEnvelope, ShortProposal, lease/heartbeat
  request-response shapes).

What does NOT live here (intentionally):
- LLM wrappers (`pipeline/llm.py`)
- Prompt authoring (`pipeline/prompts.py`)
- Cast routing (`pipeline/cast_router.py`)

These were considered for a move during the cloud migration, but they
have a tight web of intra-pipeline dependencies:
    prompts → images, beats, script_lint, llm
    llm → telemetry
    cast_router → llm
Moving any one without the full graph is impractical, and the cloud
control plane never imports them anyway. Their natural home stays in
`pipeline/`. They'll move only if/when the legacy monolith finally
goes away (today, `make_shorts.py` is still subprocessed by the render
worker so the chain is alive).
"""
