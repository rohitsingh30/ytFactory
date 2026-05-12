# LLM SDK output-token cap (PIPELINE-BUG, 2026-05-12)

> **TL;DR** — `pipeline/llm/cli.py::_call_azure_openai` set NO
> `max_tokens`/`max_completion_tokens` (Azure deployment defaulted to
> 4096), and `_call_anthropic_sdk` hardcoded `max_tokens=4096`. A
> 30-min long-form rewrite expects ~4500 narration words plus a
> sectioned panels JSON wrapper — well past 4096 tokens. The
> response truncated mid-stream, producing a 17-min script for a
> 30-min request. Fixed by `max_tokens_for(stage)` table —
> `rewrite_long_form` gets 12000, every other stage stays at 4096.

## What surfaced this

Same render job as `cloud_tts_loudness.md` —
[8413e79d…](https://console.cloud.google.com/run/jobs/details/asia-southeast1/ytfactory-render-worker-v2/executions/ytfactory-render-worker-v2-9j8nr).
User requested a 30-minute long-form. The rendered mp4 came back at
17:15. Probe of the script artifact:

```python
>>> import json
>>> with open('script.json') as f: d = json.load(f)
>>> sum(len(s['narration'].split()) for s in d['sections'])
2289   # 4500 expected for 30 min @ 150 wpm
```

`pipeline/llm/rewrite_long_form.py::_planned_sections_and_panels(1800)`
correctly computed `words_target=4500`. The prompt to the LLM said
"plan ~4500 total narration words". The response stopped at 2289.

## Root cause

`pipeline/llm/cli.py` had two backends for long-form work:

```python
# _call_azure_openai — pre-fix
kwargs: dict[str, Any] = {
    "model": deployment,
    "messages": [{"role": "user", "content": user_prompt}],
    # ← no max_tokens set; Azure defaults to deployment cap (4096)
}
client.chat.completions.create(**kwargs)

# _call_anthropic_sdk — pre-fix
client.messages.create(
    model=model_id,
    max_tokens=4096,   # hardcoded
    messages=[{"role": "user", "content": user_prompt}],
)
```

A `LongFormScript` JSON envelope for a 30-min video carries:

- 4500 prose words ≈ 6500 tokens (English ratio ~1.4 tokens/word)
- 10 section headers + visual_briefs ≈ 300 tokens
- 24 panel scenes ≈ 1200 tokens
- JSON structural overhead (keys, quotes, commas) ≈ 800 tokens

Total ≈ 8800 tokens of output. The 4096 cap chops it at ~3000 prose
words → ~17 min when narrated.

Equally bad: cloud renders silently truncated their output and the
schema validator passed on the truncated dict (sections array was
non-empty, every required field present, just shorter than asked).
No error surfaced upstream. The render proceeded normally, the user
got a 17-min file, the cause was invisible without reading the LLM
call telemetry.

## Fix (shipped commit `bfbbec1`, 2026-05-12)

`pipeline/llm/cli.py` — new `_DEFAULT_MAX_TOKENS_BY_STAGE` table
+ `max_tokens_for(stage)` accessor:

```python
_DEFAULT_MAX_TOKENS_BY_STAGE: dict[str, int] = {
    "rewrite_long_form": 12000,   # 30-min script ~ 6500 prose tokens + JSON wrapper, +50% headroom
    "rewrite":           4096,
    "cast":              4096,
    "prompts":           4096,
    "critic":            4096,
    "audio_critic":      4096,
    "imitate_analyze":   4096,
    "imitate_apply":     4096,
}

def max_tokens_for(stage: str | None) -> int:
    """Per-stage SDK-backend output-token budget.
    Env-overridable per stage via YTFACTORY_MAX_TOKENS_<STAGE>.
    """
```

Both backends now read it:

```python
# _call_azure_openai
kwargs["max_tokens"] = max_tokens_for(stage)

# _call_anthropic_sdk
client.messages.create(
    model=model_id,
    max_tokens=max_tokens_for(stage),
    ...
)
```

## Stage naming convention (important for env overrides)

`max_tokens_for(stage)` is keyed on the EXACT stage string the call
site passes. The current stages:

| stage string         | source call site                                      |
|----------------------|-------------------------------------------------------|
| `rewrite`            | `pipeline/llm/rewrite.py::rewrite()` (Shorts)         |
| `rewrite_long_form`  | `pipeline/llm/rewrite_long_form.py::rewrite_long_form()` |
| `cast`               | `pipeline/llm/cast.py`                                |
| `prompts`            | `pipeline/llm/prompts.py`                             |
| `critic`             | `pipeline/llm/critic.py` + `pipeline/llm/audio_critic.py` |
| `imitate_analyze`    | `pipeline/llm/imitate.py` (vision-aware, CLI-only)    |
| `imitate_apply`      | `pipeline/llm/imitate.py`                             |

Adding a new stage: register a default in `_DEFAULT_MAX_TOKENS_BY_STAGE`
(falls back to 4096 if unregistered — usually fine for short outputs).

Env override pattern:

```bash
# Bump long-form to 16000 for a 60-min experiment without code change
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_MAX_TOKENS_REWRITE_LONG_FORM=16000
```

## Why max_tokens, not max_completion_tokens, on Azure (with self-heal)

The `openai` Python SDK accepts both for chat completions. Reasoning
models (o1, o3, **and gpt-5.x as of 2026-05-12**) REQUIRE
`max_completion_tokens` and reject `max_tokens`; gpt-4o and earlier
accept either, with `max_tokens` being the canonical name.

**Self-healing retry (commit e600e22, 2026-05-12):**
`_call_azure_openai` sends `max_tokens` first; on the specific 400
("Unsupported parameter: 'max_tokens' is not supported with this
model. Use 'max_completion_tokens' instead.") it retries once with
the kwarg renamed (same value, same intent). Mirrors the existing
`response_format` self-heal. No code change needed when a deployment
silently moves to a reasoning family.

This replaces the earlier prescription in this section ("swap to
`max_completion_tokens` in `_call_azure_openai` (single-line change)
— OR set the env var") which **never actually landed in code**. The
2026-05-12 prod incident hit the un-shipped path: `gpt-5.3-chat`
became a reasoning deployment, every Azure call 400'd, the discover
LLM brainstorm fallback silently returned `[]`, and the cascade-on-
top of a Reddit-403 surfaced as a 502 on
`/api/discover/mystoriesanimated`. See
`feedback_doc_aspirational_claims.md` for the meta-pattern.

## Verification

```python
from pipeline.llm.cli import max_tokens_for
assert max_tokens_for("rewrite_long_form") >= 8000
assert max_tokens_for("rewrite") == 4096
assert max_tokens_for("totally_new_stage") == 4096   # safe default
```

Pin: `tests/test_llm_dispatcher.py::MaxTokensForTest` (8 cases) +
`MaxTokensWiredIntoBackendsTest` (3 cases).

## Cross-references

- Pipeline source: `pipeline/llm/cli.py::max_tokens_for`,
  `pipeline/llm/cli.py::_call_azure_openai`,
  `pipeline/llm/cli.py::_call_anthropic_sdk`
- Backend dispatcher overview: `docs/llm_backend_dispatcher.md`
- Per-stage model defaults (sister table): `_DEFAULT_MODEL_BY_STAGE`
  in `pipeline/llm/cli.py`
- Memory: `feedback_llm_sdk_max_tokens.md`
- Original render that surfaced this: job `8413e79dfbc244c3b27a46271d95f46d`
