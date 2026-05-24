"""Source of truth for $/hr and $/token rates used by the cost report.

Why this exists
---------------
The cost aggregator (``scripts/cost_report.py``) reads structured
telemetry events from Cloud Logging and converts duration / token
counts into dollars. The conversion rates live here — in a single
module — so the cost report, future admin-UI dashboard, and any
ad-hoc estimator import the same numbers.

Updating rates
~~~~~~~~~~~~~~
* When a Cloud Run service shape changes (CPU / GPU / memory), update
  the matching row in ``CLOUD_RUN_RATES_USD_PER_HOUR`` AND the
  corresponding ``cloud/<svc>/deploy.sh``. The two must stay in sync.
* When the Azure deployment behind a tier changes, add the new model
  name to ``LLM_RATES_USD_PER_MILLION_TOKENS``. We never delete old
  rows — historical cost reports need to look up retired models.
* Env vars (``YTFACTORY_LLM_<MODEL>_INPUT_USD_PER_M`` /
  ``..._OUTPUT_USD_PER_M``) override individual model rates without
  a code change — useful when Azure changes pricing mid-month.

Rates are quoted in USD because that's what the GCP / Azure invoices
use. Conversion to other currencies happens at display time, not in
this module.
"""
from __future__ import annotations

import os
from typing import Dict


# ---------------------------------------------------------------------------
# GPU / CPU compute — Cloud Run services, $ per wall-clock hour
# ---------------------------------------------------------------------------
#
# Sources:
# * docs/cost_optimized_deploy.md §3.4 (hourly rate table)
# * Each cloud/<svc>/deploy.sh comment block (CPU + GPU SKU)
# * GCP Cloud Run pricing page (Nov 2025) for L4 ($0.626/hr) +
#   CPU+memory billing
#
# These are FULL price (no committed-use discount). If you negotiate a
# CUD, override via env var YTFACTORY_RATE_<SERVICE>_USD_PER_HOUR.
_DEFAULT_CLOUD_RUN_RATES: Dict[str, float] = {
    # L4 GPU services — 1× L4 + matching CPU/memory + per-request
    "ytfactory-image-z-image-turbo": 0.626,   # 8 vCPU, 32 GiB, 1× L4
    "tts-chatterbox":                0.514,   # 4 vCPU, 16 GiB, 1× L4
    "ytfactory-tts-indicf5":         0.514,   # 4 vCPU, 16 GiB, 1× L4
    "ytfactory-asr-whisper":         0.514,   # 4 vCPU, 16 GiB, 1× L4
    "ytfactory-editing-agent":       0.514,   # 4 vCPU,  8 GiB, 1× L4 (optional 8th stage)
    # CPU-only — render worker JOB + web BFFs
    "ytfactory-render-worker-v2":    0.116,   # 4 vCPU,  8 GiB, no GPU
    "ytfactory-web":                 0.040,   # 1 vCPU,  2 GiB
    "ytfactory-web-next":            0.020,   # 1 vCPU, 512 MiB
}


def cloud_run_rate(service: str) -> float:
    """USD per wall-clock hour for the named Cloud Run service.

    Lookup order: env var override → built-in table → 0.0 (caller
    must handle 'unknown service' explicitly — we don't want to
    silently undercount).
    """
    env_key = f"YTFACTORY_RATE_{service.upper().replace('-', '_')}_USD_PER_HOUR"
    explicit = os.environ.get(env_key)
    if explicit:
        try:
            return float(explicit)
        except ValueError:
            pass
    return _DEFAULT_CLOUD_RUN_RATES.get(service, 0.0)


# ---------------------------------------------------------------------------
# LLM tokens — $ per million tokens, per model
# ---------------------------------------------------------------------------
#
# Schema: model_name -> (input_$_per_million, output_$_per_million).
#
# Token costs vary by model AND by which provider serves it. Telemetry
# always emits BOTH ``ytfactory.meta.model`` and ``ytfactory.meta.tier``;
# we prefer the concrete model name and fall back to tier-defaults.
#
# Sources:
# * Azure OpenAI pricing page (snapshot Nov 2025)
# * Anthropic public price list (snapshot Nov 2025)
#
# Update procedure: when Azure ships a new deployment, add the deployment
# name as a row here. The ``YTFACTORY_LLM_<MODEL>_INPUT_USD_PER_M`` and
# ``..._OUTPUT_USD_PER_M`` env vars override individual entries without
# a deploy.
_DEFAULT_LLM_RATES: Dict[str, tuple[float, float]] = {
    # Azure OpenAI — current production default for every tier per
    # _AZURE_TIER_DEFAULTS in pipeline/llm/cli.py is OVERRIDDEN to
    # 'gpt-5.3-chat' via AZURE_OPENAI_MODEL env var. List both the
    # generic deployment alias and the GPT-4o family it sits on so
    # historical events resolve correctly.
    "gpt-5.3-chat":  (5.00, 15.00),   # treat as gpt-4o-class until Azure publishes
    "gpt-5.4-chat":  (5.00, 15.00),
    "gpt-4o":        (2.50, 10.00),
    "gpt-4o-mini":   (0.15,  0.60),
    # Anthropic direct (if the anthropic_sdk backend is used)
    "claude-sonnet-4.6":  (3.00, 15.00),
    "claude-haiku-4.5":   (1.00,  5.00),
    "claude-opus-4.7":   (15.00, 75.00),
}

# Tier-default model used when an event has no concrete ``meta.model``
_TIER_TO_DEFAULT_MODEL: Dict[str, str] = {
    "haiku":  "gpt-4o-mini",
    "sonnet": "gpt-5.3-chat",
    "opus":   "gpt-5.3-chat",
}


def llm_token_rates(model: str | None, *, tier: str | None = None) -> tuple[float, float]:
    """Return ``(input_$_per_million, output_$_per_million)`` for a model.

    If ``model`` is missing or unknown, fall back to the default model
    for the supplied ``tier``. If both are missing/unknown, returns
    ``(0.0, 0.0)`` — the caller can detect the zero and report
    "unknown-model: N events" so we don't silently undercount.
    """
    def _rate(m: str) -> tuple[float, float] | None:
        env_in = os.environ.get(
            f"YTFACTORY_LLM_{m.upper().replace('-', '_').replace('.', '_')}_INPUT_USD_PER_M"
        )
        env_out = os.environ.get(
            f"YTFACTORY_LLM_{m.upper().replace('-', '_').replace('.', '_')}_OUTPUT_USD_PER_M"
        )
        base = _DEFAULT_LLM_RATES.get(m)
        if base is None and (env_in is None and env_out is None):
            return None
        bi, bo = base if base else (0.0, 0.0)
        try:
            if env_in is not None:
                bi = float(env_in)
            if env_out is not None:
                bo = float(env_out)
        except ValueError:
            pass
        return bi, bo

    if model:
        r = _rate(model)
        if r is not None:
            return r

    if tier:
        fallback_model = _TIER_TO_DEFAULT_MODEL.get(tier)
        if fallback_model:
            r = _rate(fallback_model)
            if r is not None:
                return r

    return 0.0, 0.0


def llm_call_cost_usd(
    *,
    model: str | None,
    tier: str | None,
    input_tokens: float,
    output_tokens: float,
) -> float:
    """Compute the USD cost of a single LLM call from its token counts."""
    in_rate, out_rate = llm_token_rates(model, tier=tier)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


def gpu_seconds_cost_usd(*, service: str, seconds: float) -> float:
    """Compute USD cost from raw GPU/CPU wall-clock seconds for a service."""
    rate = cloud_run_rate(service)
    return rate * (seconds / 3600.0)


__all__ = [
    "cloud_run_rate",
    "llm_token_rates",
    "llm_call_cost_usd",
    "gpu_seconds_cost_usd",
]
