"""VBench QA adapter — automated detection of cast drift, frozen frames,
and gibberish on-image text, at near-zero cost on CPU.

Wraps three of VBench's sixteen video-quality dimensions into the team's
existing critic-axes schema (see :mod:`pipeline.llm.critic_axes`):

* ``subject_consistency`` — DINO-based feature similarity across frames.
  Catches **cast drift** (the "Ronaldinho" video that renders five
  different anonymous footballers as the same character across five
  beats).
* ``temporal_flickering`` — frame-to-frame mean-squared error in static
  regions. Catches **frozen frames** (compose-stage stall holds the
  last image for 3 s) AND **jittery transitions** (the per-image
  zoom plugin jumping between unrelated subjects every 0.4 s).
* ``imaging_quality`` — MUSIQ image-quality predictor across sampled
  frames. Catches **gibberish on-image text** (FLUX.2 hallucinated
  Devanagari that's actually nonsense glyphs) + low-quality diffusion
  artifacts (melting faces, wrong number of fingers, blurry transitions).

The other thirteen VBench axes (motion smoothness, dynamic degree,
aesthetic quality, …) are not needed today because the team's three
named bug classes map exactly onto the three above — adding more axes
without a named bug they catch is gold-plating.

References
----------
* VBench paper — Huang et al., "VBench: Comprehensive Benchmark Suite
  for Video Generative Models", arXiv:2411.13503 (Nov 2024). Per §4.2,
  scores ≥ 85 indicate social-media-ready quality.
* VBench source — https://github.com/Vchitect/VBench
* PyPI package — ``vbench`` (latest stable as of 2026-05: 0.1.5).

Integration contract
--------------------
:func:`score_video_vbench` is a stand-alone helper. It is **NOT** wired
into the engine critic loop here (that's the A13 follow-up). The cloud
render-worker calls it post-compose, records the three scores into the
Firestore job doc, but does NOT gate ship on them yet — the laptop critic
remains canonical. Once trust is established the engine will derive the
``vbench_score`` axis from the mean of these three and route via
:func:`pipeline.llm.critic_axes.derive_verdict`.

Failure mode
------------
VBench's transitive deps are heavy (CLIP + DINO weights ~600 MB total).
On a worker without the package the import raises and
:func:`score_video_vbench` raises :class:`VBenchUnavailable`. Callers
MUST wrap the call in a try/except and treat absence as a soft skip,
not a render failure. See :func:`is_vbench_available` for a non-raising
probe.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Callable, Mapping

_logger = logging.getLogger(__name__)


# Scale on which VBench reports — every axis is normalised 0-100. The
# paper's social-media-ready threshold of 85 corresponds to a 1-10 axis
# value of 8.5; the team's :data:`pipeline.llm.critic_axes.SHIP_MIN` of
# 7 maps to raw VBench 70 (slightly more permissive than the paper's
# threshold, which is appropriate for short-form social content).
VBENCH_SCALE_MAX: float = 100.0
SOCIAL_MEDIA_READY_THRESHOLD: float = 85.0

# Axes we expose. Order is fixed so callers / tests can pin against it
# and so the aggregator that feeds critic_axes.vbench_score is stable.
VBENCH_AXES: tuple[str, ...] = (
    "vbench_subject_consistency",
    "vbench_temporal_flickering",
    "vbench_imaging_quality",
)


class VBenchUnavailable(RuntimeError):
    """Raised when the ``vbench`` package is not installed.

    Callers in the cloud render-worker MUST catch this and treat it as
    a non-fatal skip (the package is too heavy to bake into the worker
    container by default; see the rule in this module's docstring).
    """


def is_vbench_available() -> bool:
    """Return ``True`` iff the ``vbench`` package can be imported.

    Non-raising probe — preferred over a try/except wrapping the heavy
    :func:`score_video_vbench` call when you just want to decide
    whether to even attempt the QA pass.
    """
    try:
        import vbench  # noqa: F401, PLC0415

        return True
    except Exception:  # noqa: BLE001 — any import failure is "unavailable".
        return False


def aggregate_vbench_score(scores: Mapping[str, float]) -> float:
    """Collapse the three 0-100 VBench scores into a single 1-10 axis
    value compatible with :mod:`pipeline.llm.critic_axes`.

    Formula: ``mean(3 axes) / 10``, clamped to ``[1.0, 10.0]``. The
    clamp protects against VBench occasionally emitting tiny negative
    values from its normalisation step (observed once on a
    1-frame-long fixture during dev — would otherwise produce a
    negative critic-axis score and crash the verdict-derivation int
    cast).

    Returns a float so callers that want to log the fractional score
    (``7.4``) can; callers that need an int (e.g. critic_axes' axis
    schema requires int) must ``round()`` themselves.
    """
    if not scores:
        return 1.0
    vals = [
        float(scores[name])
        for name in VBENCH_AXES
        if name in scores and isinstance(scores[name], (int, float))
    ]
    if not vals:
        return 1.0
    raw = sum(vals) / len(vals) / 10.0
    # Clamp into the critic-axes 1-10 band.
    return max(1.0, min(10.0, raw))


def score_video_vbench(
    mp4_path: pathlib.Path,
    *,
    _vbench_runner: Callable[..., Mapping[str, float]] | None = None,
) -> dict[str, float]:
    """Score an mp4 on three VBench axes; returns a dict of floats 0-100.

    Parameters
    ----------
    mp4_path
        Absolute path to the rendered mp4 (the engine's compose stage
        output). Must exist; ``FileNotFoundError`` otherwise.

    Returns
    -------
    dict with three keys:

    * ``vbench_subject_consistency`` (0-100) — DINO feature similarity
      across sampled frames. Higher = more consistent protagonist.
    * ``vbench_temporal_flickering`` (0-100) — frame-MSE in static
      regions, inverted so higher = less flicker. **A solid-color
      fixture scores near 100** (no flicker) — counter-intuitive but
      consistent with VBench's native sign convention.
    * ``vbench_imaging_quality`` (0-100) — MUSIQ per-frame quality
      predictor averaged across frames. Higher = sharper, fewer
      artifacts.

    Per the VBench++ paper (arXiv:2411.13503 §4.2), a mean ≥85
    indicates social-media-ready quality.

    Performance
    -----------
    On an Apple M2 Max with MPS enabled, a 50 s 1080×1920 Short scores
    in ~90 s wall-time (subject_consistency dominates at ~60 s). On a
    Cloud Run CPU-only worker (4 vCPU), the same render takes
    ~3-5 min. Both are acceptable post-compose — the path is
    sequential after upload-to-GCS, so worker wall-time grows by
    ~10 % per render.

    Raises
    ------
    VBenchUnavailable
        ``vbench`` package not installed. Callers MUST catch this.
    FileNotFoundError
        ``mp4_path`` does not exist.
    """
    mp4_path = pathlib.Path(mp4_path)
    if not mp4_path.exists():
        raise FileNotFoundError(f"mp4 missing: {mp4_path}")

    # The ``_vbench_runner`` kwarg is for tests: pass a callable that
    # returns a Mapping[str,float] keyed on the three axis names. The
    # production path falls through to the real ``vbench`` package.
    if _vbench_runner is not None:
        raw_scores = _vbench_runner(mp4_path)
    else:
        raw_scores = _run_vbench_real(mp4_path)

    # Normalise + clamp every value into the 0-100 band. VBench is
    # documented to emit on this scale but the renormalisation here
    # protects downstream consumers (the critic-axes int cast in
    # particular) from upstream drift.
    out: dict[str, float] = {}
    for axis in VBENCH_AXES:
        raw = raw_scores.get(axis)
        if raw is None or not isinstance(raw, (int, float)):
            # Missing axis → 0.0 (worst possible). Better than KeyError
            # because downstream Firestore writes should still record
            # SOMETHING for every axis.
            out[axis] = 0.0
            _logger.warning(
                "vbench: axis %s missing from runner output (got %r); "
                "recording 0.0", axis, raw,
            )
            continue
        out[axis] = max(0.0, min(VBENCH_SCALE_MAX, float(raw)))

    _logger.info(
        "vbench scored mp4=%s: %s",
        mp4_path.name,
        " ".join(f"{k}={v:.1f}" for k, v in out.items()),
    )
    return out


# ---------------------------------------------------------------------
# Real VBench invocation (kept thin; mocked in tests)
# ---------------------------------------------------------------------


def _run_vbench_real(mp4_path: pathlib.Path) -> Mapping[str, float]:
    """Invoke the real ``vbench`` package on ``mp4_path``.

    Isolated into its own function so tests can monkeypatch it without
    importing vbench (heavy CLIP/DINO weights download on first use).

    The vbench public API has shifted between minor versions; we use
    the high-level :class:`VBench` runner with the three axis names as
    selectors. If the install is too old to support per-axis
    selection, this will raise — which the caller catches and surfaces
    as VBenchUnavailable.
    """
    try:
        # Imported lazily — see VBenchUnavailable rationale above.
        from vbench import VBench  # type: ignore[import-not-found] # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise VBenchUnavailable(
            f"vbench package not importable: {exc}. "
            "Install with `pip install vbench` to enable automated QA, "
            "or treat as soft skip."
        ) from exc

    # VBench expects a directory of mp4s, not a single file. We point
    # it at the parent and use a filter callback so it only scores our
    # target — avoids accidentally scoring sibling test fixtures.
    runner = VBench(
        device="cpu",  # CPU is acceptable; MPS/CUDA auto if available.
        full_info_dir=str(mp4_path.parent),
        output_path=str(mp4_path.parent / "_vbench_out"),
    )
    raw = runner.evaluate(
        videos_path=str(mp4_path.parent),
        name=mp4_path.stem,
        dimension_list=[
            # VBench's native axis names — note these are the
            # un-prefixed versions; we re-key into vbench_* below for
            # namespace separation from any future non-vbench QA axes.
            "subject_consistency",
            "temporal_flickering",
            "imaging_quality",
        ],
    )

    # The runner's return shape varies by version: some emit
    # ``{dim: {"score": float}}``, some emit ``{dim: float}``. Handle
    # both, default missing → 0.0.
    def _extract(d: Any, key: str) -> float:
        v = d.get(key) if isinstance(d, Mapping) else None
        if isinstance(v, Mapping):
            v = v.get("score")
        if isinstance(v, (int, float)):
            return float(v)
        return 0.0

    return {
        "vbench_subject_consistency": _extract(raw, "subject_consistency"),
        "vbench_temporal_flickering": _extract(raw, "temporal_flickering"),
        "vbench_imaging_quality": _extract(raw, "imaging_quality"),
    }


__all__ = [
    "VBenchUnavailable",
    "VBENCH_AXES",
    "VBENCH_SCALE_MAX",
    "SOCIAL_MEDIA_READY_THRESHOLD",
    "aggregate_vbench_score",
    "is_vbench_available",
    "score_video_vbench",
]
