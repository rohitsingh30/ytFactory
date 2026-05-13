"""LLM client for in-pipeline authoring + critique calls.

Three backends, selected by the ``YTFACTORY_LLM_BACKEND`` env var:

* ``cli`` (default on laptop) — shells out to the ``claude`` binary.
  Free per call against the user's Claude Pro/Max OAuth plan. Supports
  vision-aware calls via ``add_dirs`` + ``allowed_tools=["Read"]``.

* ``azure_openai`` (default in cloud render-worker) — uses the
  ``openai.AzureOpenAI`` SDK against the same Azure deployment that
  serves the chat assistant. Reuses the existing
  ``AZURE_OPENAI_*`` secrets — no separate spend.

* ``anthropic_sdk`` — uses the ``anthropic`` SDK with
  ``ANTHROPIC_API_KEY``. Pay-per-token, opens a separate billing line.

The public entry point ``call_claude_cli`` keeps its name (every existing
call site already uses it) but now dispatches on the env var. Tier
aliases (``haiku`` / ``sonnet`` / ``opus``) are mapped to concrete model
IDs / deployment names per backend — the call sites stay tier-agnostic.

Vision-aware features (``add_dirs`` + ``allowed_tools``) are only
implemented on the ``cli`` backend today; the SDK backends raise
``ClaudeCLIError`` if those kwargs are passed. The cloud render-worker
only invokes pure-text stages (rewrite, cast, per-beat prompts) so this
limitation doesn't block production.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import telemetry as _tlm

logger = logging.getLogger(__name__)


CLAUDE_BIN = "claude"

# Re-exported so tests can monkeypatch ``llm_cli.subprocess.run`` and
# ``llm_cli.shutil.which`` without poking at the stdlib module directly.
import shutil  # noqa: E402

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

BACKEND_CLI = "cli"
BACKEND_AZURE = "azure_openai"
BACKEND_ANTHROPIC = "anthropic_sdk"
_VALID_BACKENDS = {BACKEND_CLI, BACKEND_AZURE, BACKEND_ANTHROPIC}


def _shutil_which(name: str) -> str | None:
    """Wrapper around :func:`shutil.which` so tests can mock binary discovery."""
    return shutil.which(name)


def _choose_backend() -> str:
    """Decide which LLM backend to use.

    Order of precedence:
      1. ``YTFACTORY_LLM_BACKEND`` env var, if set to a valid value.
      2. If the ``claude`` binary is on PATH → ``cli`` (laptop dev: free
         OAuth-billed Pro/Max plan).
      3. Else if Azure creds are set → ``azure_openai``.
      4. Else if ``ANTHROPIC_API_KEY`` is set → ``anthropic_sdk``.
      5. Final fallback → ``cli`` (call will fail if claude isn't on PATH,
         but this surfaces the misconfiguration loudly).
    """
    explicit = (os.environ.get("YTFACTORY_LLM_BACKEND") or "").strip().lower()
    if explicit in _VALID_BACKENDS:
        return explicit
    if explicit:
        logger.warning("ignoring unknown YTFACTORY_LLM_BACKEND=%r — auto-detecting", explicit)

    if _shutil_which(CLAUDE_BIN):
        return BACKEND_CLI
    if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
        return BACKEND_AZURE
    if os.environ.get("ANTHROPIC_API_KEY"):
        return BACKEND_ANTHROPIC
    return BACKEND_CLI


def _should_use_sdk() -> bool:
    """True when the configured backend is one of the cloud SDKs."""
    return _choose_backend() in (BACKEND_AZURE, BACKEND_ANTHROPIC)


# Back-compat alias for callers / tests that reference the older name.
_select_backend = _choose_backend

# Conservative budget cap per call. Pipeline now defaults to opus
# everywhere (see _DEFAULT_MODEL_BY_STAGE), where output tokens are
# pricier; the cap is bumped accordingly. The CLI bills via OAuth on
# Pro/Max plans, so this is mostly a runaway-protection guard rather
# than a true cost lever.
DEFAULT_BUDGET_USD = 2.00

# Bumped from 300s — opus on long narrations + the rewrite prompt's
# big SPICY/PROSODY block can take 5-7 min. Per-call timeouts can
# still be set lower at the call site for fast stages (cast, prompts).
DEFAULT_TIMEOUT_S = 600

# Strip ANSI colour codes that might leak into stderr-mingled output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ClaudeCLIError(RuntimeError):
    """The `claude` CLI exited non-zero or returned an error envelope."""


# Per-stage model defaults — single source of truth so tuning the
# cost/quality balance is one edit.
#
# All stages default to OPUS. Earlier mixed-tier configs (haiku for
# cast, sonnet for rewrite/prompts) produced enough validator-tripping
# narrations and shallow image prompts that the time saved on the
# small models was paid back in retries and class-of-bug critic
# corrections downstream. With OAuth-billed Pro/Max plans, switching
# to opus everywhere is effectively a flat-rate upgrade — the time
# cost is real (~30-60% slower per stage) but quality per click is
# materially better and rules adherence on the rewrite SPICY/PROSODY
# directives is much more reliable.
#
# Override at runtime per-stage via the env var YTFACTORY_MODEL_<STAGE>
# (e.g. ``YTFACTORY_MODEL_REWRITE=sonnet`` for fast iteration on
# everything except the stage you're tuning, or ``=haiku`` to dial
# down a stage that doesn't need top-tier judgment for your use case).
_DEFAULT_MODEL_BY_STAGE: dict[str, str] = {
    "cast": "opus",
    "rewrite": "opus",
    "rewrite_long_form": "opus",  # Audit D3.47 — explicit so the env var is discoverable
    "prompts": "opus",
    "critic": "opus",
    "audio_critic": "opus",
    "imitate_analyze": "opus",
    "imitate_apply": "opus",
}


# Per-stage output-token budget for the SDK backends (Azure
# `max_completion_tokens`, Anthropic `max_tokens`).
#
# Why this exists: both SDK clients defaulted to 4096 output tokens —
# Azure via the deployment's implicit cap, Anthropic via a hardcoded
# `max_tokens=4096` in `_call_anthropic_sdk`. A 30-min long-form
# rewrite expects ~4500 narration words plus a sectioned panels JSON
# wrapper — well past 4096 tokens of prose. The 2026-05-12 render
# (job 8413e79d) requested 30 min and got back a 2289-word script
# (≈17 min) because the response truncated mid-stream. Pin per-stage
# so the long stages have headroom while the short stages stay
# conservative (cheap + fast).
#
# Override per-stage at runtime via ``YTFACTORY_MAX_TOKENS_<STAGE>``
# (e.g. ``YTFACTORY_MAX_TOKENS_REWRITE_LONG_FORM=16000``).
_DEFAULT_MAX_TOKENS_BY_STAGE: dict[str, int] = {
    # Long-form rewrite: 30-min script ~ 4500 words ~ 6500 prose
    # tokens, plus the sectioned + panels JSON envelope ≈ 8800 tokens
    # of OUTPUT.
    #
    # 2026-05-13 — bumped 12000 → 32000 after gpt-5.3-chat truncated
    # the Leigh Occhi long-form rewrite mid-string ("could reshape
    # what we think happened to "). Reasoning deployments (gpt-5.x /
    # o1 / o3) consume max_completion_tokens for INVISIBLE reasoning
    # tokens — observed reasoning_tokens for a 30-min long-form is
    # ~15-20k, leaving only ~-5k for the actual output if budget is
    # 12k. We need budget ≥ reasoning + output; 32000 gives headroom
    # for reasoning ~20k + output ~9k + 30% margin. The dispatcher
    # also auto-retries with doubled budget on finish_reason="length"
    # (capped at 64k) as a safety net for stages that go even bigger.
    #
    # Cost note: gpt-5.3-chat charges per OUTPUT token, including
    # reasoning. 32000 max means up to 32000 × $/tok price — but most
    # calls finish well under cap, so the typical render isn't
    # affected. Override down via YTFACTORY_MAX_TOKENS_REWRITE_LONG_FORM
    # if a particular channel runs gpt-4o (no reasoning) and 12k is
    # plenty.
    "rewrite_long_form": 32000,
    # Shorts rewrite + cast + prompts run on much shorter outputs.
    # 2026-05-13 — bumped 4096 → 8192 to give reasoning deployments
    # the same headroom. Short-form output is ~1500 prose tokens, but
    # gpt-5.x reasoning ~3-4k means 4k cap leaves no output budget.
    "rewrite": 8192,
    "cast": 8192,
    "prompts": 8192,
    "critic": 8192,
    "audio_critic": 8192,
    "imitate_analyze": 8192,
    "imitate_apply": 8192,
}

# Floor for any stage we haven't pinned explicitly. Keeps the old
# behaviour (4096 default) for any caller that passes a stage name
# we haven't classified yet.
_FALLBACK_MAX_TOKENS = 4096

# Auto-bump ceiling for the finish_reason=length retry path. If a
# stage's budget × 2 would exceed this, we don't retry — we surface
# the clear "bump YTFACTORY_MAX_TOKENS_<STAGE>" error so the operator
# audits the prompt before throwing more $/tokens at it. 64k matches
# Azure gpt-5.x's typical per-deployment hard cap; raise via
# YTFACTORY_MAX_TOKENS_AUTO_BUMP_CEILING if a future deployment
# supports more.
def _max_token_auto_bump_ceiling() -> int:
    raw = os.environ.get("YTFACTORY_MAX_TOKENS_AUTO_BUMP_CEILING", "").strip()
    if raw:
        try:
            return max(_FALLBACK_MAX_TOKENS, int(raw))
        except ValueError:
            pass
    return 64000

_MAX_TOKEN_AUTO_BUMP_CEILING = _max_token_auto_bump_ceiling()


def _reasoning_tokens(usage: Any) -> int | None:
    """Extract the reasoning_tokens count from an Azure OpenAI usage
    object. gpt-5.x / o1 / o3 deployments include this in
    ``completion_tokens_details.reasoning_tokens``. Older deployments
    don't expose it (returns ``None``).
    """
    if usage is None:
        return None
    details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return None
    return getattr(details, "reasoning_tokens", None)


# ---- Reasoning-effort per stage (Tier 1A token-cost optimisation, 2026-05-13) ----
#
# gpt-5.x / o1 / o3 reasoning deployments accept a ``reasoning_effort``
# parameter (``minimal`` / ``low`` / ``medium`` / ``high``) that
# controls how many INVISIBLE reasoning tokens the model burns before
# emitting output. Reasoning tokens are billed as output tokens but
# don't appear in the response — they're pure cost.
#
# Observed on gpt-5.3-chat (2026-05-13):
#   reasoning_effort=high       (default) → ~15-20k reasoning tokens
#   reasoning_effort=medium               → ~5-8k reasoning tokens
#   reasoning_effort=low                  → ~1-3k reasoning tokens
#   reasoning_effort=minimal              → ~0-200 reasoning tokens
#
# Most pipeline stages don't NEED deep reasoning — they're transformation
# tasks (assign character names to shotlist, format image prompts,
# restructure JSON). Setting ``minimal`` for these stages saves ~70%
# of total token consumption per render with no observed quality
# degradation. Only stages that genuinely benefit from planning
# (long-form rewrite, critique) stay at ``medium``.
#
# Override per-stage via ``YTFACTORY_REASONING_EFFORT_<STAGE>`` env
# (one of minimal/low/medium/high — invalid values are ignored).
# Set to ``off`` / ``none`` to omit the param entirely (useful when
# diagnosing a stage that needs the deployment default).
_DEFAULT_REASONING_EFFORT_BY_STAGE: dict[str, str] = {
    "rewrite_long_form": "medium",  # planning a 30-min script benefits from reasoning
    "critic":            "medium",  # we want thoughtful critique
}
_FALLBACK_REASONING_EFFORT = "minimal"
_VALID_REASONING_EFFORTS = {"minimal", "low", "medium", "high"}


def reasoning_effort_for(stage: str | None) -> str | None:
    """Pick the ``reasoning_effort`` value for an Azure OpenAI call.

    Returns one of ``minimal`` / ``low`` / ``medium`` / ``high`` — or
    ``None`` to OMIT the parameter entirely (used when the operator
    explicitly disables via env ``off`` / ``none``).
    """
    env_key = f"YTFACTORY_REASONING_EFFORT_{(stage or 'STAGE').upper()}"
    raw = os.environ.get(env_key, "").strip().lower()
    if raw in _VALID_REASONING_EFFORTS:
        return raw
    if raw in {"off", "none"}:
        return None
    if not stage:
        return _FALLBACK_REASONING_EFFORT
    return _DEFAULT_REASONING_EFFORT_BY_STAGE.get(stage, _FALLBACK_REASONING_EFFORT)


# Per-deployment cache: does this Azure deployment accept the
# ``reasoning_effort`` param? Legacy chat-completions deployments
# (gpt-4o, gpt-4-turbo) reject it with a 400 unsupported_parameter.
# Cache populated lazily — first call passes the param; on a 400 we
# strip + remember. Skip discovery entirely by setting
# ``AZURE_REASONING_EFFORT_DISABLE=1`` (legacy deployments).
_AZURE_REASONING_EFFORT_SUPPORTED: dict[str, bool] = {}


def _azure_supports_reasoning_effort(deployment: str) -> bool:
    """Whether this deployment accepts ``reasoning_effort``. Defaults
    to True (try once, learn from rejection). Honours
    ``AZURE_REASONING_EFFORT_DISABLE=1`` to skip entirely.
    """
    if (os.environ.get("AZURE_REASONING_EFFORT_DISABLE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return _AZURE_REASONING_EFFORT_SUPPORTED.get(deployment, True)


def _remember_azure_reasoning_effort_support(deployment: str, supported: bool) -> None:
    """Cache the discovered reasoning_effort support for a deployment."""
    _AZURE_REASONING_EFFORT_SUPPORTED[deployment] = supported


def model_for(stage: str) -> str:
    """Return the tier alias (haiku/sonnet/opus) for a pipeline stage."""
    env_key = f"YTFACTORY_MODEL_{stage.upper()}"
    return os.environ.get(env_key) or _DEFAULT_MODEL_BY_STAGE.get(stage, "opus")


def max_tokens_for(stage: str | None) -> int:
    """Return the SDK-backend output-token budget for a pipeline stage.

    Single source of truth so tests + dispatcher agree. Env-overridable
    per stage so a runaway long-form prompt can be bumped without a
    code change.
    """
    if not stage:
        return _FALLBACK_MAX_TOKENS
    env_key = f"YTFACTORY_MAX_TOKENS_{stage.upper()}"
    raw = os.environ.get(env_key)
    if raw:
        try:
            return max(256, int(raw))
        except ValueError:
            pass
    return _DEFAULT_MAX_TOKENS_BY_STAGE.get(stage, _FALLBACK_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Per-backend tier → concrete-model maps
# ---------------------------------------------------------------------------

# Azure deployment names. Override per-tier via env so the same code
# works against any Azure deployment naming scheme.
#
# Defaults are the conservative cost choices (gpt-4o-mini for the cheap
# tiers, gpt-4o for opus). Production deployments routinely override the
# generic alias via ``AZURE_OPENAI_MODEL`` so call sites pick the tier
# and the env decides the deployment (e.g. set
# ``AZURE_OPENAI_MODEL=gpt-5.4-chat`` to route every Azure call there).
_AZURE_TIER_DEFAULTS: dict[str, str] = {
    "haiku":  "gpt-4o-mini",
    "sonnet": "gpt-4o-mini",
    "opus":   "gpt-4o",
}


def _azure_model_for(tier: str) -> str:
    """Resolve a tier alias (``haiku``/``sonnet``/``opus``) to an Azure
    deployment name. Per-tier env wins over the generic env wins over
    the built-in default.
    """
    env_key = f"AZURE_OPENAI_MODEL_{tier.upper()}"
    explicit = os.environ.get(env_key)
    if explicit:
        return explicit
    if (generic := os.environ.get("AZURE_OPENAI_MODEL")):
        return generic
    return _AZURE_TIER_DEFAULTS.get(tier, _AZURE_TIER_DEFAULTS["opus"])


# Back-compat alias.
_azure_deployment = _azure_model_for


# Per-deployment "which output-token cap key does this model want?" cache.
# gpt-4o (chat completions) and most legacy deployments accept ``max_tokens``;
# gpt-5.x / o1 / o3 reasoning deployments require ``max_completion_tokens``
# and reject ``max_tokens`` outright (Azure 400). We can't tell which a given
# Azure deployment wants from its NAME (the user's `AZURE_OPENAI_MODEL`
# environment variable is opaque), so we discover at first call: try the
# default, on the well-known "use max_completion_tokens instead" 400, swap
# AND remember. Subsequent calls in the same process skip the round-trip.
#
# To skip discovery entirely (recommended in production where the deployment
# is known), set ``AZURE_OPENAI_TOKEN_PARAM=max_completion_tokens`` (or
# ``=max_tokens``) on the cloud render-worker. This avoids the wasteful
# fail-then-retry on every LLM call when the deployment is gpt-5.x.
_AZURE_TOKEN_PARAM_BY_DEPLOYMENT: dict[str, str] = {}
_VALID_AZURE_TOKEN_PARAMS = {"max_tokens", "max_completion_tokens"}


def _azure_token_param(deployment: str) -> str:
    """Return ``max_tokens`` or ``max_completion_tokens`` — whichever this
    Azure deployment expects.

    Resolution order:
      1. ``AZURE_OPENAI_TOKEN_PARAM`` env (forces a specific param,
         skips runtime discovery — recommended in production).
      2. Process cache (populated by previous swap-retry).
      3. Default ``max_tokens`` (the historical chat-completions key —
         covers gpt-4o + every pre-reasoning deployment).
    """
    env = (os.environ.get("AZURE_OPENAI_TOKEN_PARAM") or "").strip()
    if env in _VALID_AZURE_TOKEN_PARAMS:
        return env
    return _AZURE_TOKEN_PARAM_BY_DEPLOYMENT.get(deployment, "max_tokens")


def _remember_azure_token_param(deployment: str, param: str) -> None:
    """Cache the discovered token-cap key for a deployment, so subsequent
    calls in this process skip the failed-then-retry handshake.

    Honours the env override: if ``AZURE_OPENAI_TOKEN_PARAM`` is set, the
    cache is irrelevant (and we don't pollute it with a value that may
    contradict what the operator wired up).
    """
    if param not in _VALID_AZURE_TOKEN_PARAMS:
        return
    if (os.environ.get("AZURE_OPENAI_TOKEN_PARAM") or "").strip() in _VALID_AZURE_TOKEN_PARAMS:
        return
    _AZURE_TOKEN_PARAM_BY_DEPLOYMENT[deployment] = param


# Anthropic SDK model IDs. Override per-tier via env. Unknown tier
# names pass through verbatim — lets call sites that already know the
# concrete model id (e.g. ``model="claude-opus-4-7"``) keep working.
#
# Audit Q2.13 — opus pinned to claude-opus-4-7 (2026-05 GA) so the
# Anthropic SDK call site matches the laptop CLI's actual model. Older
# 4-5 was a stale default from the initial dispatcher land. Operators
# who want a specific minor revision still override via
# ``ANTHROPIC_MODEL_OPUS=claude-opus-4-5-20250605`` etc.
_ANTHROPIC_TIER_DEFAULTS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-5",
    "opus":   "claude-opus-4-7",
}


def _anthropic_model_for(tier: str) -> str:
    env_key = f"ANTHROPIC_MODEL_{tier.upper()}"
    if (explicit := os.environ.get(env_key)):
        return explicit
    return _ANTHROPIC_TIER_DEFAULTS.get(tier, tier)


# Back-compat alias for the older name from the first draft.
_anthropic_model = _anthropic_model_for


def _infer_stage_from_model(model: str) -> str:
    """Best-effort fallback when a caller didn't pass ``stage=``.

    Today every stage in ``_DEFAULT_MODEL_BY_STAGE`` resolves to the same
    model so this only disambiguates when an env override changes one
    stage's model. Worst case we tag ``"unknown"`` — strictly better than
    the pre-fix behaviour where every llm_call event bucketed as ``"?"``.

    Call sites should pass ``stage="cast"`` etc. explicitly; this helper
    is only here so a single forgotten call site doesn't poison the
    whole telemetry rollup.
    """
    matches = [s for s, m in _DEFAULT_MODEL_BY_STAGE.items()
               if (os.environ.get(f"YTFACTORY_MODEL_{s.upper()}") or m) == model]
    if len(matches) == 1:
        return matches[0]
    return "unknown"


def call_claude_cli(
    prompt: str,
    *,
    output_json: bool = True,
    json_schema: dict | None = None,
    add_dirs: list[Path] | None = None,
    allowed_tools: list[str] | None = None,
    model: str = "haiku",
    timeout_s: int = DEFAULT_TIMEOUT_S,
    budget_usd: float = DEFAULT_BUDGET_USD,
    stage: str | None = None,
) -> str | dict:
    """Call the configured LLM backend and return text or parsed JSON.

    Backend is chosen via :func:`_select_backend` (env-driven). The
    function name is preserved for back-compat — every existing call
    site already uses ``call_claude_cli`` and is unaffected by the
    backend switch.

    Args:
        prompt: The user message.
        output_json: Parse the response as a JSON object/array.
        json_schema: Optional JSON Schema for structured-output
            validation. Honoured by ``cli`` (passes ``--json-schema``)
            and Azure (uses Responses API ``response_format=json_schema``);
            on Anthropic SDK we fall back to ``response_format=json_object``
            + a "respond with JSON only" hint appended to the prompt.
        add_dirs: Filesystem dirs the model can read (vision-aware
            calls). **Only supported by the cli backend** — passing
            this to the SDK backends raises ``ClaudeCLIError`` because
            the SDKs need the images explicitly inlined as content
            parts and the call sites (anatomy_check, critic,
            imitate_analyze) only run on the laptop today.
        allowed_tools: Tool whitelist. Same restriction as ``add_dirs``.
        model: Tier alias (``haiku`` / ``sonnet`` / ``opus``). Mapped
            to a concrete model per backend (see ``_AZURE_TIER_DEFAULTS``
            and ``_ANTHROPIC_TIER_DEFAULTS``).
        timeout_s: Per-call timeout in seconds.
        budget_usd: Hard cost cap (passed to the cli backend; the SDK
            backends instead cap output via the configured per-stage
            output budget — see :func:`max_tokens_for` and
            ``_DEFAULT_MAX_TOKENS_BY_STAGE``. Audit Q2.14 — pre-fix
            this docstring claimed SDK backends rely on
            ``max_completion_tokens``; that's only true for Azure
            OpenAI's gpt-5.x / o1 / o3 reasoning deployments — the
            Anthropic SDK uses ``max_tokens=``. The dispatch lives
            in ``_call_azure_openai`` and ``_call_anthropic_sdk``).
        stage: Pipeline stage name recorded in telemetry.

    Returns:
        ``dict`` / ``list`` if ``output_json`` else ``str``.

    Raises:
        ClaudeCLIError: on any backend failure (subprocess error, SDK
            HTTP error, JSON parse error, schema-validation error).
    """
    backend = _select_backend()
    resolved_stage = stage or _infer_stage_from_model(model)

    # Vision / tool-use is currently CLI-only.
    if backend != BACKEND_CLI and (add_dirs or allowed_tools):
        raise ClaudeCLIError(
            f"backend {backend!r} does not support add_dirs / allowed_tools "
            f"(stage={resolved_stage!r}). Vision-aware stages "
            "(anatomy_check, critic, imitate_analyze) require "
            "YTFACTORY_LLM_BACKEND=cli."
        )

    if backend == BACKEND_AZURE:
        return _call_azure_openai(
            prompt,
            output_json=output_json,
            json_schema=json_schema,
            model=model,
            timeout_s=timeout_s,
            stage=resolved_stage,
        )
    if backend == BACKEND_ANTHROPIC:
        return _call_anthropic_sdk(
            prompt,
            output_json=output_json,
            json_schema=json_schema,
            model=model,
            timeout_s=timeout_s,
            stage=resolved_stage,
        )
    return _call_claude_cli_subprocess(
        prompt,
        output_json=output_json,
        json_schema=json_schema,
        add_dirs=add_dirs,
        allowed_tools=allowed_tools,
        model=model,
        timeout_s=timeout_s,
        budget_usd=budget_usd,
        stage=resolved_stage,
    )


# Public alias — newer code can call ``call_llm`` instead of the
# legacy ``call_claude_cli`` name. Both go through the same dispatcher.
call_llm = call_claude_cli


def _call_claude_cli_subprocess(
    prompt: str,
    *,
    output_json: bool = True,
    json_schema: dict | None = None,
    add_dirs: list[Path] | None = None,
    allowed_tools: list[str] | None = None,
    model: str = "haiku",
    timeout_s: int = DEFAULT_TIMEOUT_S,
    budget_usd: float = DEFAULT_BUDGET_USD,
    stage: str | None = None,
) -> str | dict:
    """Original ``claude``-binary subprocess implementation.

    Identical to the pre-dispatcher behaviour. Kept private so the
    public entry point can dispatch on backend without breaking the
    existing pure-CLI test surface.

    **Audit T1.4 — token-cap asymmetry with SDK backends.** The
    Anthropic CLI (``claude -p``) does NOT expose a ``--max-tokens``
    flag — the only output-size lever it offers is ``--max-budget-usd``
    (a soft dollar cap, surfaced via ``budget_usd`` here). The SDK
    backends (``_call_azure_openai`` and ``_call_anthropic_sdk``)
    DO honour ``max_tokens_for(stage)``; the CLI cannot. To keep
    observability symmetric we surface the would-be cap in telemetry
    metadata and, when the caller didn't pass an explicit non-default
    budget, scale ``--max-budget-usd`` by the stage's max_tokens so
    the dollar cap at least roughly matches the token cap the SDK
    backends would apply (formula: ``max_tokens × $75/MTok × 5x``,
    using the worst-case opus output rate × 5 for input + safety
    margin). Operators can override either dimension via env:
    ``YTFACTORY_MAX_TOKENS_<STAGE>`` or per-call ``budget_usd=``.
    """
    cap_tokens = max_tokens_for(stage)
    # Audit T1.4: derive a per-stage budget cap when the caller
    # accepted the default. Conservative formula: cap_tokens at the
    # opus output rate ($75/MTok) × 5 to cover input + safety margin.
    # This ensures stages that bumped max_tokens above the SDK-backend
    # default of 4096 (e.g. rewrite_long_form @ 12k) get a budget
    # ceiling that won't truncate mid-rewrite. SDK backends apply a
    # HARD cap on max_completion_tokens; the CLI dollar cap is SOFT
    # (model can refuse mid-stream once budget is exhausted) — the
    # asymmetry is documented in the docstring + surfaced in telemetry.
    if budget_usd == DEFAULT_BUDGET_USD:
        derived_budget = max(DEFAULT_BUDGET_USD, cap_tokens * 75 / 1_000_000 * 5)
        budget_usd = round(derived_budget, 4)
    cmd: list[str] = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format", "json",
        "--model", model,
        "--no-session-persistence",
        "--max-budget-usd", str(budget_usd),
    ]

    # We can't use --bare here: it bypasses the OAuth keychain and
    # requires ANTHROPIC_API_KEY, which the user doesn't have set
    # (Claude Pro/Max OAuth login). The --setting-sources user-only
    # combo still skips project CLAUDE.md / hooks while letting auth
    # work via the keychain.
    cmd.extend(["--setting-sources", "user"])
    cmd.append("--disable-slash-commands")

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    else:
        # Disable ALL tools when the caller doesn't explicitly opt in.
        cmd.extend(["--tools", ""])

    if add_dirs:
        cmd.append("--add-dir")
        cmd.extend(str(p) for p in add_dirs)

    if json_schema is not None:
        cmd.extend(["--json-schema", json.dumps(json_schema)])

    job_id = os.environ.get("YTFACTORY_JOB_ID") or None
    tlm_meta = {
        "backend": BACKEND_CLI,
        "model": model,
        "prompt_chars": len(prompt),
        "schema": json_schema is not None,
        "tools": list(allowed_tools or []),
        "stage": stage or _infer_stage_from_model(model),
        # Audit T1.4: surface the would-be max_tokens cap for symmetry
        # with the SDK backends, even though the CLI can't enforce it
        # natively. Plus the derived dollar cap that approximates it.
        "max_tokens_requested": cap_tokens,
        "budget_usd_cap": budget_usd,
    }
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta, "error": f"timeout {timeout_s}s"})
        raise ClaudeCLIError(f"claude CLI timed out after {timeout_s}s") from e

    stdout = _ANSI_RE.sub("", proc.stdout or "")
    stderr = _ANSI_RE.sub("", proc.stderr or "")

    if proc.returncode != 0:
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta, "error": f"rc={proc.returncode}",
                             "stderr_head": stderr[:200]})
        raise ClaudeCLIError(
            f"claude CLI exited {proc.returncode}\n"
            f"stderr: {stderr[:500]}\nstdout: {stdout[:500]}"
        )

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCLIError(
            f"claude CLI returned non-JSON output:\n{stdout[:1000]}"
        ) from e

    if envelope.get("is_error"):
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta,
                             "error": str(envelope.get("result") or "envelope")[:200]})
        raise ClaudeCLIError(
            f"claude CLI error envelope: {envelope.get('result') or envelope}"
        )

    # Record success once we know the call returned cleanly. Token + cost
    # fields come from the CLI envelope when present (it surfaces them as
    # `usage.input_tokens` / `usage.output_tokens` / `total_cost_usd`).
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    _tlm.track(
        "llm_call",
        category="llm",
        success=True,
        duration_ms=int((time.time() - t0) * 1000),
        job_id=job_id,
        metadata={
            **tlm_meta,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": envelope.get("total_cost_usd"),
        },
    )

    # When `--json-schema` is passed, the CLI parses + validates the model's
    # output and puts the resulting object in `structured_output`; `result`
    # stays empty in that mode. Prefer it when present.
    if output_json and json_schema is not None:
        structured = envelope.get("structured_output")
        if isinstance(structured, (dict, list)):
            return structured
        # Fall through to result parsing if the CLI didn't populate it.

    result_text = envelope.get("result")
    if result_text is None:
        raise ClaudeCLIError(f"claude CLI envelope missing 'result': {envelope}")

    if not output_json:
        return result_text

    # Parse the inner JSON. Tolerate models that wrap output in ```json fences.
    return _parse_inner_json(result_text)


def _parse_inner_json(text: str) -> dict | list:
    """Extract a JSON object/array from a model response, tolerant of
    markdown fences and surrounding chatter.
    """
    s = text.strip()

    # Strip ```json ... ``` fences if present.
    if s.startswith("```"):
        # remove leading fence
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        # remove trailing fence
        s = re.sub(r"\s*```\s*$", "", s)

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Last resort: find the first {...} or [...] block.
    # Audit Q2.16 — the bracket-balance counter must skip over JSON
    # string literals so an embedded ``"narration": "She said 'I'm
    # {done}.'"`` doesn't make depth go negative mid-string and short-
    # circuit the scan. We track an in-string flag plus an escape flag
    # so ``"foo \" {bar}"`` (an escaped quote inside a string) keeps
    # the in-string state correctly.
    for opener, closer in (("{", "}"), ("[", "]")):
        i = s.find(opener)
        if i == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for j in range(i, len(s)):
            ch = s[j]
            if escape:
                escape = False
                continue
            if in_str:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[i : j + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    raise ClaudeCLIError(
        f"could not parse JSON from model output:\n{text[:1000]}"
    )


# ---------------------------------------------------------------------------
# Azure OpenAI backend (default in cloud render-worker)
# ---------------------------------------------------------------------------

# JSON-only nudge appended to the user prompt when ``output_json=True``
# and we're not relying on the deployment's structured-output mode.
_JSON_HINT = (
    "Respond with ONLY a JSON object or array — no prose, no markdown "
    "fences, no commentary before or after."
)


def _augment_prompt_for_json(prompt: str, json_schema: dict | None) -> str:
    """Append a JSON-only instruction (and the schema, if any) to the prompt.

    Both Azure and Anthropic backends use this so the model returns
    parseable JSON even when the SDK doesn't expose strict structured-
    output modes. The exact phrasing is what the spec tests assert on:
    ``"matching this schema"`` when a schema is provided, ``"ONLY a JSON
    object"`` otherwise.
    """
    if json_schema is None:
        return f"{prompt}\n\n{_JSON_HINT}"
    schema_text = json.dumps(json_schema, indent=2, sort_keys=True)
    return (
        f"{prompt}\n\n"
        "Return a single JSON value matching this schema (no prose, no "
        "markdown fences, no commentary):\n"
        f"{schema_text}"
    )


def _build_azure_client(timeout_s: int) -> Any:
    """Construct a fresh Azure OpenAI client per call.

    Per-call (rather than process-cached) keeps the timeout honest —
    each stage can specify its own — and means a credential refresh
    just works without a process restart.
    """
    endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip()
    api_key = (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip()
    if not endpoint or not api_key:
        raise ClaudeCLIError(
            "azure_openai backend selected but AZURE_OPENAI_ENDPOINT / "
            "AZURE_OPENAI_API_KEY are not set"
        )
    try:
        from openai import AzureOpenAI  # type: ignore  # noqa: PLC0415
    except ImportError as e:
        raise ClaudeCLIError(
            "azure_openai backend requires the `openai` package "
            "(pip install openai>=1.40)"
        ) from e
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        timeout=float(timeout_s),
    )


def _call_azure_openai(
    prompt: str,
    *,
    output_json: bool,
    json_schema: dict | None,
    model: str,
    timeout_s: int,
    stage: str | None = None,
) -> str | dict:
    """Call Azure OpenAI chat-completions with our prompt → text or parsed JSON.

    Args:
        prompt: User prompt. When ``output_json=True`` it gets a
            JSON-only suffix appended (and the schema, if supplied) so
            the model knows to skip prose.
        output_json: Parse the response as JSON.
        json_schema: When set, the request uses
            ``response_format={"type":"json_schema", ...}`` (a hard
            structured-output mode); on a deployment that doesn't
            support it, we retry once **without** ``response_format``
            and parse the response client-side.
        model: Tier alias (``haiku``/``sonnet``/``opus``) — resolved to
            an Azure deployment name via :func:`_azure_model_for`.
        timeout_s: Per-call timeout in seconds, passed to the SDK
            client constructor.
        stage: Pipeline stage tag for telemetry.
    """
    client = _build_azure_client(timeout_s)
    deployment = _azure_model_for(model)
    user_prompt = (
        _augment_prompt_for_json(prompt, json_schema)
        if output_json else prompt
    )

    job_id = os.environ.get("YTFACTORY_JOB_ID") or None
    tlm_meta = {
        "backend": BACKEND_AZURE,
        "model": deployment,
        "tier": model,
        "prompt_chars": len(prompt),
        "schema": json_schema is not None,
        "stage": stage,
        "token_param": _azure_token_param(deployment),
        "reasoning_effort": reasoning_effort_for(stage)
            if _azure_supports_reasoning_effort(deployment) else None,
    }

    # Cap output tokens per stage. Without this, Azure deployments default
    # to ~4096 → long-form rewrite truncates the script mid-stream. See
    # _DEFAULT_MAX_TOKENS_BY_STAGE for the table and the 2026-05-12
    # 17-min-vs-30-min post-mortem (job 8413e79d).
    #
    # The KEY is deployment-dependent: gpt-4o + chat-completions models
    # accept ``max_tokens``; gpt-5.x / o1 / o3 reasoning deployments require
    # ``max_completion_tokens`` and reject ``max_tokens`` outright (Azure
    # 400 unsupported_parameter). _azure_token_param() consults the env
    # override + per-deployment process cache so the second call onward in
    # this process skips the discovery handshake.
    token_param = _azure_token_param(deployment)
    kwargs: dict[str, Any] = {
        "model": deployment,
        "messages": [{"role": "user", "content": user_prompt}],
        token_param: max_tokens_for(stage),
    }

    # Tier 1A token-cost optimisation (2026-05-13): pass reasoning_effort
    # to gpt-5.x / o1 / o3 deployments so non-reasoning stages (cast,
    # prompts, shorts rewrite, etc) skip the ~15-20k invisible
    # reasoning-token burn that gpt-5.3-chat does by default. Stripped
    # via swap-retry for legacy deployments that reject it (gpt-4o).
    if _azure_supports_reasoning_effort(deployment):
        re_value = reasoning_effort_for(stage)
        if re_value is not None:
            kwargs["reasoning_effort"] = re_value
    if output_json:
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": stage or "response",
                    "schema": json_schema,
                    "strict": False,
                },
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}

    t0 = time.time()
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        # Two known self-healing recoveries:
        #
        # (1) ``max_tokens`` ↔ ``max_completion_tokens`` swap. gpt-5.x /
        # o1 / o3 reasoning deployments require ``max_completion_tokens``
        # and reject ``max_tokens``; gpt-4o + chat-completions accept the
        # opposite. We can't tell at config time which the operator
        # wired up, so we try the cached/default key and on the
        # well-known 400 ("Unsupported parameter: 'max_tokens' is not
        # supported with this model. Use 'max_completion_tokens'"
        # — and the symmetric reverse) we swap once, retry, AND remember
        # via _remember_azure_token_param so subsequent calls in this
        # process skip the round-trip. Also remembered: a successful
        # first-try also warms the cache (below) so the env-default of
        # ``max_tokens`` is overridden if discovery contradicts it.
        # Set ``AZURE_OPENAI_TOKEN_PARAM=max_completion_tokens`` in env
        # to skip discovery entirely (recommended in production where
        # the deployment is fixed and known — gpt-5.3-chat in cloud
        # render-worker, see cloud/render-worker-v2/deploy.sh).
        #
        # (2) ``response_format``-related rejection. Older deployments
        # don't support either json_schema or json_object. Retry once
        # WITHOUT response_format, parse client-side.
        #
        # (3) ``reasoning_effort`` rejection. Legacy chat-completions
        # deployments (gpt-4o, gpt-4-turbo) reject this param with a
        # 400 unsupported_parameter. Strip + remember in the per-
        # deployment cache so subsequent calls skip the parameter
        # entirely. Disable globally via AZURE_REASONING_EFFORT_DISABLE=1.
        #
        # Any other failure → ClaudeCLIError.
        msg = str(e)
        retry_label: str | None = None
        swap_to: str | None = None
        reasoning_effort_dropped = False

        if "max_tokens" in msg and "max_completion_tokens" in msg:
            if "max_tokens" in kwargs:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                swap_to = "max_completion_tokens"
                retry_label = "max_completion_tokens"
            elif "max_completion_tokens" in kwargs:
                kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
                swap_to = "max_tokens"
                retry_label = "max_tokens"
        elif (
            "reasoning_effort" in msg
            and "reasoning_effort" in kwargs
        ):
            kwargs.pop("reasoning_effort", None)
            reasoning_effort_dropped = True
            retry_label = "no_reasoning_effort"
        elif (
            output_json
            and "response_format" in msg
            and "response_format" in kwargs
        ):
            kwargs.pop("response_format", None)
            retry_label = "no_response_format"

        if retry_label:
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e2:  # noqa: BLE001
                _tlm.track("llm_call", category="llm", success=False,
                           duration_ms=int((time.time() - t0) * 1000),
                           job_id=job_id,
                           metadata={**tlm_meta, "error": str(e2)[:200],
                                     "retry": retry_label})
                raise ClaudeCLIError(
                    f"azure_openai chat error (post-retry={retry_label}): {e2}"
                ) from e2
            # Retry succeeded — cache the discoveries so subsequent
            # calls in this process pick the right shape on first try.
            if swap_to:
                _remember_azure_token_param(deployment, swap_to)
            if reasoning_effort_dropped:
                _remember_azure_reasoning_effort_support(deployment, False)
        else:
            _tlm.track("llm_call", category="llm", success=False,
                       duration_ms=int((time.time() - t0) * 1000),
                       job_id=job_id,
                       metadata={**tlm_meta, "error": msg[:200]})
            raise ClaudeCLIError(f"azure_openai chat error: {e}") from e
    else:
        # First-try success — also warm the cache so the cache reflects
        # observed truth (matters when the deployment was switched out
        # under us mid-process and the cached value is stale, or when
        # the very first call succeeded with the default ``max_tokens``
        # and we want subsequent calls to skip even the cache lookup
        # cost). Cheap; idempotent.
        _remember_azure_token_param(deployment, token_param)

    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    finish_reason = getattr(resp.choices[0], "finish_reason", None)

    # Detect output truncation — if Azure returned finish_reason=length,
    # the model hit max_completion_tokens before finishing. Auto-retry
    # ONCE with doubled budget (capped at MAX_TOKEN_AUTO_BUMP_CEILING)
    # so reasoning-deployment renders don't silently lose half the
    # script. The 2026-05-13 5e37f76b post-mortem: gpt-5.3-chat
    # consumed all 12000 max_completion_tokens on invisible reasoning
    # tokens, leaving the JSON cut off at "could reshape what we
    # think happened to ".
    if finish_reason == "length":
        cap_used = kwargs.get(token_param) or max_tokens_for(stage)
        bumped = min(cap_used * 2, _MAX_TOKEN_AUTO_BUMP_CEILING)
        if bumped > cap_used:
            logger.warning(
                "azure_openai stage=%s deployment=%s truncated at "
                "max_completion_tokens=%d (finish_reason=length, "
                "completion_tokens=%s, reasoning_tokens=%s) — auto-"
                "retrying once with bumped cap=%d",
                stage, deployment, cap_used,
                getattr(usage, "completion_tokens", "?"),
                _reasoning_tokens(usage),
                bumped,
            )
            kwargs[token_param] = bumped
            t0_retry = time.time()
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                _tlm.track("llm_call", category="llm", success=False,
                           duration_ms=int((time.time() - t0_retry) * 1000),
                           job_id=job_id,
                           metadata={**tlm_meta, "error": str(e)[:200],
                                     "retry": "max_tokens_doubled",
                                     "max_tokens_first": cap_used,
                                     "max_tokens_retry": bumped})
                raise ClaudeCLIError(
                    f"azure_openai chat error (post-retry=max_tokens_doubled "
                    f"to {bumped}, original truncated at {cap_used}): {e}"
                ) from e
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            finish_reason = getattr(resp.choices[0], "finish_reason", None)

    # If we STILL got truncated after the retry (or hit the ceiling so
    # no retry was attempted), surface a clear actionable error
    # instead of letting _parse_inner_json fail with the misleading
    # "could not parse JSON" message.
    if finish_reason == "length":
        completion = getattr(usage, "completion_tokens", None)
        reasoning = _reasoning_tokens(usage)
        cap_used = kwargs.get(token_param) or max_tokens_for(stage)
        msg = (
            f"azure_openai stage={stage} deployment={deployment} "
            f"output truncated by max_completion_tokens={cap_used} "
            f"(finish_reason=length, completion_tokens={completion}, "
            f"reasoning_tokens={reasoning}). Bump via env: "
            f"YTFACTORY_MAX_TOKENS_{(stage or 'STAGE').upper()}={cap_used * 2}. "
            f"Reasoning deployments (gpt-5.x / o1 / o3) consume "
            f"max_completion_tokens for INVISIBLE reasoning tokens — "
            f"see docs/llm_max_tokens.md."
        )
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta, "error": msg[:200],
                             "finish_reason": "length",
                             "max_tokens_used": cap_used,
                             "completion_tokens": completion,
                             "reasoning_tokens": reasoning})
        raise ClaudeCLIError(msg)

    _tlm.track(
        "llm_call",
        category="llm",
        success=True,
        duration_ms=int((time.time() - t0) * 1000),
        job_id=job_id,
        metadata={
            **tlm_meta,
            "input_tokens":  getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "reasoning_tokens": _reasoning_tokens(usage),
            "finish_reason": finish_reason,
        },
    )

    if not output_json:
        return text
    return _parse_inner_json(text)


# ---------------------------------------------------------------------------
# Anthropic SDK backend (alternative cloud option)
# ---------------------------------------------------------------------------


def _build_anthropic_client(timeout_s: int) -> Any:
    """Construct a fresh Anthropic client per call.

    The SDK auto-loads ``ANTHROPIC_API_KEY`` from env on construction
    (and raises ``AuthenticationError`` on the first API call if it's
    missing) — we don't pre-validate so test fakes can stub the
    constructor without setting an env var.
    """
    try:
        from anthropic import Anthropic  # type: ignore  # noqa: PLC0415
    except ImportError as e:
        raise ClaudeCLIError(
            "anthropic_sdk backend requires the `anthropic` package "
            "(pip install anthropic>=0.40)"
        ) from e
    return Anthropic(timeout=float(timeout_s))


def _call_anthropic_sdk(
    prompt: str,
    *,
    output_json: bool,
    json_schema: dict | None,
    model: str,
    timeout_s: int,
    stage: str | None = None,
) -> str | dict:
    """Call the Anthropic Messages API → return text or parsed JSON.

    The SDK doesn't expose JSON-schema-strict output, so we lean on
    prompt augmentation: ``_augment_prompt_for_json`` adds a "respond
    only with JSON" tail (and embeds the schema when supplied), and we
    parse client-side via :func:`_parse_inner_json`. Schema validation
    is left to the caller (most call sites do a shape check on the
    returned dict).
    """
    client = _build_anthropic_client(timeout_s)
    model_id = _anthropic_model_for(model)
    user_prompt = (
        _augment_prompt_for_json(prompt, json_schema)
        if output_json else prompt
    )

    job_id = os.environ.get("YTFACTORY_JOB_ID") or None
    tlm_meta = {
        "backend": BACKEND_ANTHROPIC,
        "model": model_id,
        "tier": model,
        "prompt_chars": len(prompt),
        "schema": json_schema is not None,
        "stage": stage,
    }

    t0 = time.time()
    try:
        resp = client.messages.create(
            model=model_id,
            # Stage-aware output budget — long-form rewrite needs ~12k,
            # everything else stays at the conservative 4096. Pre-fix
            # this was hardcoded 4096 and silently truncated 30-min
            # scripts. See _DEFAULT_MAX_TOKENS_BY_STAGE.
            max_tokens=max_tokens_for(stage),
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta, "error": str(e)[:200]})
        raise ClaudeCLIError(f"anthropic_sdk error: {e}") from e

    parts = [
        getattr(blk, "text", "")
        for blk in (resp.content or [])
        if getattr(blk, "type", "text") == "text" and getattr(blk, "text", None)
    ]
    text = "".join(parts).strip()
    usage = getattr(resp, "usage", None)
    _tlm.track(
        "llm_call",
        category="llm",
        success=True,
        duration_ms=int((time.time() - t0) * 1000),
        job_id=job_id,
        metadata={
            **tlm_meta,
            "input_tokens":  getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        },
    )

    if not output_json:
        return text
    return _parse_inner_json(text)


# ---------------------------------------------------------------------------
# Test-only helpers
# ---------------------------------------------------------------------------


def _reset_clients_for_tests() -> None:
    """Backwards-compatible no-op.

    The Azure / Anthropic clients are now built per call, so there is
    nothing to clear. Kept callable so any older test that imported
    this helper continues to work.
    """
    return None
