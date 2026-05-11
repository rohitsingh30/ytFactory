"""Cinematic editing layer for ytFactory.

Public API:

* :class:`Edl` / :func:`build_edl_from_planner_json` — schema + parser
* :func:`pipeline.editing.planner.plan_edit` — LLM → EDL JSON
* :func:`pipeline.editing.executor.execute_local` — EDL → mp4 (laptop)
* :func:`pipeline.editing.cloudrun.execute_edit` — EDL → mp4 (cloud, with circuit breaker + laptop fallback)
* :func:`pipeline.editing.cloudrun.reset_circuit_breaker` — clear per-render breaker

Architecture matches ``pipeline.images`` / ``pipeline.tts``:

* Planner emits closed-form JSON (whitelisted filters, whitelisted LUTs).
  The LLM never emits free-form ffmpeg commands.
* Executor compiles the EDL to a single ``ffmpeg -filter_complex`` invocation
  (or 2-pass for loudnorm).
* Cloud client posts the EDL + signed-URL inputs to ``cloud/editing-agent``
  Cloud Run service; falls back to local executor on
  :class:`CloudRunUnavailable`.
* Per-render circuit breaker mirrors :mod:`pipeline.images.images_cloudrun`
  so a single cloud failure trips the whole render to local fallback rather
  than burning the per-call timeout 30+ times in a row.
"""

from .schema import (  # noqa: F401
    Edl,
    Shot,
    Filter,
    Transition,
    AudioConfig,
    MusicConfig,
    LetterboxConfig,
    CaptionsConfig,
    EditMode,
    LUT_WHITELIST,
    FILTER_WHITELIST,
    TRANSITION_WHITELIST,
    build_edl_from_planner_json,
)
from .circuit_breaker import (  # noqa: F401
    reset_circuit_breaker,
    breaker_open,
    trip_breaker,
    CloudRunUnavailable,
)
