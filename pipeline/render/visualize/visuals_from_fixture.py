"""Test-only VisualProducer that loads a pre-rendered mp4.

Used by engine integration tests to isolate engine wiring from real
visual_mode plugins (which call Flux cloud + ffmpeg chains, all
slow + non-deterministic for CI).

Fixture path read from ``spec.extra['visuals_fixture_path']``.
"""
from __future__ import annotations

from pathlib import Path
from shutil import copy2
from typing import Any

from pipeline.render.contracts import (
    Timeline,
    VisualProducer,
    VisualTrack,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration


class VisualsFromFixture:
    """Loads a VisualTrack from an mp4 file pointed at by
    ``spec.extra['visuals_fixture_path']``.

    Copies the fixture into ``work_dir`` so the engine + downstream
    compose stage operate on a stable per-render path. Probes the
    duration via ffprobe (real, not from-fixture) so the engine sees
    the actual measured duration.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        fixture_path = (spec.extra or {}).get("visuals_fixture_path")
        if not fixture_path:
            raise ValueError(
                "VisualsFromFixture: spec.extra['visuals_fixture_path'] is "
                "required when using this plugin."
            )
        src = Path(fixture_path)
        if not src.exists():
            raise FileNotFoundError(
                f"VisualsFromFixture: fixture not found at {src}"
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        dst = work_dir / "visual_track.mp4"
        copy2(src, dst)
        return VisualTrack(
            video_path=dst,
            duration_s=probe_duration(dst),
            extras={"source": "fixture", "fixture_path": str(src)},
        )


register_plugin("visualize", "visuals_from_fixture", VisualsFromFixture())
assert isinstance(VisualsFromFixture(), VisualProducer)


__all__ = ["VisualsFromFixture"]
