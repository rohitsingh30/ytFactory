"""Test-only AudioSynthesizer that loads a pre-recorded wav.

Used by engine integration tests to isolate engine wiring from real
TTS plugin behavior (which calls cloud TTS or local kokoro — both
unavailable / non-deterministic in CI).

Implements the full :class:`AudioSynthesizer` Protocol semantically:
copies the fixture wav into ``work_dir/narration.wav``, probes the
actual duration via ffprobe, computes the voice fingerprint from the
spec just like a real synthesizer would. Engines see no difference
between this and a real impl.

Fixture path comes from ``spec.extra["audio_fixture_path"]``.
"""
from __future__ import annotations

from pathlib import Path
from shutil import copy2
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    AudioSynthesizer,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration
from pipeline.render.shared.voice_fingerprint import compute_fingerprint


class AudioFromFixture:
    """Loads a narration wav from
    ``spec.extra['audio_fixture_path']``.

    Raises :class:`ValueError` if the path is missing — the test is
    misconfigured if it picked this plugin without setting the path.
    """

    def synth(
        self,
        spec: Any,  # RenderSpec
        script: dict[str, Any],
        work_dir: Path,
    ) -> AudioResult:
        fixture_path = (spec.extra or {}).get("audio_fixture_path")
        if not fixture_path:
            raise ValueError(
                "AudioFromFixture: spec.extra['audio_fixture_path'] is "
                "required when using this plugin."
            )
        src = Path(fixture_path)
        if not src.exists():
            raise FileNotFoundError(
                f"AudioFromFixture: fixture not found at {src}"
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / "narration.wav"
        copy2(src, out)
        return AudioResult(
            narration_path=out,
            duration_s=probe_duration(out),
            voice_fingerprint=compute_fingerprint({
                "tts_provider": "fixture",
                "tts_voice": str(src.name),
            }),
            chunk_timings=None,
        )


register_plugin("audio", "audio_from_fixture", AudioFromFixture())
assert isinstance(AudioFromFixture(), AudioSynthesizer)


__all__ = ["AudioFromFixture"]
