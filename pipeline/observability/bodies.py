"""``track_io`` — emit a telemetry event that carries input + output body bytes.

Why this exists
---------------

``track()`` and ``timed()`` capture *structural* metadata (counts,
durations, token counts, finish_reason, success/fail). They DON'T
capture the actual prompt/response text — and without that, every
"why does this look wrong" investigation devolves into guessing from
the final mp4.

``track_io`` is the canonical helper for "I'm about to emit an event,
and I also want the raw input + output bytes attached so a future
operator can diff them." It:

1. Truncates each side to :data:`TEL_BODY_MAX_CHARS` (default 4000).
2. SHA-256s the *full* (pre-truncation) string so identical inputs are
   recognisable across calls even after truncation.
3. Redacts API keys / bearer tokens / common secret shapes BEFORE
   truncation — paste-safe.
4. Routes through :func:`track` so the standard counter / log /
   histogram / RenderContext threading still applies.
5. Is fully guarded by ``YTFACTORY_TELEMETRY_BODIES`` env (default ON
   in cloud, OFF on laptop). When OFF, body fields are dropped but
   the event still emits with metadata + hashes (cheap mode).
6. Never raises — telemetry must never break the pipeline.

Usage
-----

::

    from pipeline.observability import bodies as _bodies

    _bodies.track_io(
        "llm_call",
        category="llm",
        success=True,
        duration_ms=int((time.time() - t0) * 1000),
        job_id=job_id,
        input_text=full_prompt,
        output_text=raw_response_text,
        input_meta={"stage": "prompt_refine", "model": "haiku"},
        output_meta={
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "finish_reason": finish_reason,
        },
    )

The emitted metadata shape (after the helper merges everything):

::

    {
        # operator-supplied input_meta
        "stage": "prompt_refine",
        "model": "haiku",
        # operator-supplied output_meta
        "input_tokens": 1234,
        "output_tokens": 5678,
        "finish_reason": "stop",
        # body fields (added by this helper)
        "input_chars": 12345,                 # full length
        "input_sha256": "ab12cd34ef56...",    # of full string
        "input_preview": "<first 4000 chars>",
        "output_chars": 6789,
        "output_sha256": "01ab23cd45...",
        "output_preview": "<first 4000 chars>",
        "input_truncated": true,
        "output_truncated": false,
    }
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Optional

from .telemetry import track

# ---- knobs -----------------------------------------------------------

# Per-field char cap. Larger = more detail in logs, more egress cost.
# 4000 chars is roughly one Cloud Logging entry's reasonable max — Cloud
# Logging accepts up to ~256kB per entry but the UI / Logs Explorer
# truncates anything over ~32kB in the preview pane. 4000 keeps the
# whole entry readable inline.
TEL_BODY_MAX_CHARS = int(os.environ.get("YTFACTORY_TEL_BODY_MAX_CHARS", "4000"))

# Master switch. Default is ON in cloud (K_SERVICE / K_JOB env signals
# Cloud Run / Cloud Run Job), OFF on laptop. Operators can force either
# way via YTFACTORY_TELEMETRY_BODIES={1,0}.
_BODIES_ENV = os.environ.get("YTFACTORY_TELEMETRY_BODIES")


def _is_cloud_run() -> bool:
    """True if we're running inside a Cloud Run service or Cloud Run job."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))


def bodies_enabled() -> bool:
    """Whether body fields should be attached to telemetry events.

    Resolution order:
    1. ``YTFACTORY_TELEMETRY_BODIES=1`` → on, regardless of environment.
    2. ``YTFACTORY_TELEMETRY_BODIES=0`` → off, regardless of environment.
    3. Else (unset or empty): on in Cloud Run (K_SERVICE or CLOUD_RUN_JOB
       set), off elsewhere.

    Cheap mode (off) still emits the event with hashes + structural
    metadata — only the *body* text fields are dropped.
    """
    env = os.environ.get("YTFACTORY_TELEMETRY_BODIES", "")
    if env.strip() != "":
        return env.strip() not in ("0", "false", "False")
    return _is_cloud_run()


# ---- secret redaction ------------------------------------------------

# Common shapes we never want to ship to Cloud Logging:
#   * "sk-..." OpenAI/Anthropic API keys (40+ alnum chars)
#   * "Bearer <jwt>" headers (3 dot-separated base64 segments)
#   * Azure-style "...azure.com/.../api-key=..." query params
#   * "AIza..." Google API keys (39 chars)
#   * google.oauth2 "ya29...." access tokens
# Each pattern replaces the matched secret with "[REDACTED:<kind>]".
_REDACTION_PATTERNS = [
    # NOTE: order matters — more specific patterns must come first so a
    # later generic pattern (e.g. ``sk-``) doesn't swallow a more
    # specific match (``sk-ant-``).
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED:sk-ant]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED:sk]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED:google-api-key]"),
    (re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"), "[REDACTED:oauth-access-token]"),
    (re.compile(r"api[-_]key=[A-Za-z0-9_-]{16,}", re.IGNORECASE), "api_key=[REDACTED]"),
    # Cloud Run / Cloud Build oidc tokens are JWTs (three b64 segments)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
     "[REDACTED:jwt]"),
]


def redact_secrets(text: str) -> str:
    """Replace known secret patterns with a static marker.

    Best-effort: if a new secret shape sneaks in we add it to
    _REDACTION_PATTERNS. Never raises.
    """
    try:
        out = text
        for pat, repl in _REDACTION_PATTERNS:
            out = pat.sub(repl, out)
        return out
    except Exception:  # noqa: BLE001
        return text


# ---- core: hash + truncate -------------------------------------------


def _normalise_body(value: Any) -> Optional[str]:
    """Coerce ``value`` into a str for hashing/truncation.

    None → None (caller will skip emission of that side).
    str → str.
    bytes → utf-8 decoded with replacement chars.
    dict / list / other → JSON dump with sort_keys for stable hashing,
        falling back to repr() if not JSON-serialisable.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return repr(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            import json as _json  # local — keep cold path cold
            return _json.dumps(value, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return repr(value)
    return str(value)


def _sha256_short(s: str) -> str:
    """First 16 hex chars of sha256 — enough collision resistance for diffing."""
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def _body_fields(
    side: str,
    raw: Optional[str],
    *,
    capture_body: bool,
) -> dict[str, Any]:
    """Build ``{side}_chars / sha256 / preview / truncated`` from a raw string.

    side: "input" or "output".
    raw: the full pre-truncation string, or None.
    capture_body: when False, omit the preview (keeps hash + length).
    """
    if raw is None:
        return {}
    redacted = redact_secrets(raw)
    full_chars = len(redacted)
    sha = _sha256_short(redacted)
    out: dict[str, Any] = {
        f"{side}_chars": full_chars,
        f"{side}_sha256": sha,
    }
    if capture_body:
        truncated = full_chars > TEL_BODY_MAX_CHARS
        preview = redacted[:TEL_BODY_MAX_CHARS]
        out[f"{side}_preview"] = preview
        out[f"{side}_truncated"] = truncated
    return out


# ---- public API ------------------------------------------------------


def track_io(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: Optional[int] = None,
    job_id: Optional[str] = None,
    input_text: Any = None,
    output_text: Any = None,
    input_meta: Optional[dict[str, Any]] = None,
    output_meta: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a telemetry event with input + output body capture.

    ``input_text`` and ``output_text`` are coerced via
    :func:`_normalise_body` (handles str / bytes / dict / list). When
    bodies are enabled (see :func:`bodies_enabled`), a truncated
    preview is attached to the metadata; the SHA-256 and full length
    are ALWAYS attached so identical inputs can be recognised even in
    cheap mode.

    Never raises — telemetry must never break the pipeline.
    """
    try:
        capture_body = bodies_enabled()
        meta: dict[str, Any] = {}
        if metadata:
            meta.update(metadata)
        if input_meta:
            meta.update(input_meta)
        if output_meta:
            meta.update(output_meta)
        meta.update(_body_fields("input", _normalise_body(input_text),
                                 capture_body=capture_body))
        meta.update(_body_fields("output", _normalise_body(output_text),
                                 capture_body=capture_body))
        track(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata=meta,
        )
    except Exception:  # noqa: BLE001
        # Telemetry failure must never break the pipeline.
        pass


def hash_full(text: Any) -> Optional[str]:
    """Public helper: sha256[:16] of the full input.

    Useful when the caller only wants the hash (e.g. to diff prompts
    across renders) without emitting a full ``track_io`` event.
    """
    norm = _normalise_body(text)
    if norm is None:
        return None
    return _sha256_short(redact_secrets(norm))


def preview(text: Any, *, max_chars: Optional[int] = None) -> Optional[str]:
    """Public helper: redact + truncate a string for safe logging."""
    norm = _normalise_body(text)
    if norm is None:
        return None
    cap = max_chars if max_chars is not None else TEL_BODY_MAX_CHARS
    return redact_secrets(norm)[:cap]


__all__ = [
    "TEL_BODY_MAX_CHARS",
    "bodies_enabled",
    "hash_full",
    "preview",
    "redact_secrets",
    "track_io",
]
