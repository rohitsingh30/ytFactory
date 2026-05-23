"""Single-bed MusicComposer — picked by ``spec.music_policy = SINGLE_BED``.

Loops one music bed under the full narration duration with no
ducking — the long-form sleep history pattern.

The bed-synthesis chain was inlined from the legacy
``pipeline.render.long_form.build_music_bed`` on 2026-05-14 as
part of the bigbang follow-up.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    MusicComposer,
    Section,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg
from pipeline.render.telemetry_helpers import emit_json_artifact, track_event

_logger = logging.getLogger(__name__)


def _synthesize_ambient_bed(out_path: Path, duration_s: float) -> Path:
    """Synthesize a low ambient drone bed via ffmpeg lavfi.

    Three slow-detuned sine waves at low frequencies (~82/123/164 Hz)
    with low-pass filter + reverb. Not as polished as a real ambient
    track but lets the pipeline render end-to-end without a curated bed.

    Inlined from ``pipeline.render.long_form.build_music_bed``
    2026-05-14. Behavior unchanged.
    """
    flt = (
        "sine=frequency=82:duration={d}[s1];"
        "sine=frequency=123:duration={d}[s2];"
        "sine=frequency=164:duration={d}[s3];"
        "[s1][s2][s3]amix=inputs=3:duration=longest:weights=1.0 0.6 0.4,"
        "lowpass=f=400,aecho=0.6:0.5:1000:0.4,volume=-22dB[a]"
    ).format(d=duration_s)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-filter_complex", flt, "-map", "[a]",
        "-c:a", "pcm_s16le", str(out_path),
    ], purpose="single_bed_synth")
    return out_path


class SingleBed:
    """Loop one ambient bed for the full duration, no ducking.

    Defaults match long_form.py's historical behavior — when no
    curated bed is available, synthesizes the ambient drone via
    ffmpeg lavfi. Channels with a real bed file under
    ``<channel>/music/<bed>.wav`` use that instead.
    """

    def compose(
        self,
        spec: Any,
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path:
        _ = sections  # unused for single-bed
        out_path = Path("/tmp") / f"music_bed_{int(narration_duration_s * 1000)}.wav"

        bed_name = spec.music.default_bed
        if bed_name and bed_name != "off":
            bed_path = self._resolve_bed_path(spec, bed_name)
            if bed_path and bed_path.exists():
                # Loop the curated bed via ffmpeg stream_loop + atrim.
                try:
                    track = {
                        "track_id": bed_name,
                        "mood": "default",
                        "source": str(bed_path),
                        "duration_s": narration_duration_s,
                    }
                    track_event("music.pick", category="pipeline", metadata=track)
                    run_ffmpeg([
                        "-stream_loop", "-1", "-i", str(bed_path),
                        "-t", f"{narration_duration_s:.3f}",
                        "-c:a", "pcm_s16le",
                        str(out_path),
                    ], purpose="single_bed_loop")
                    emit_json_artifact(
                        "music",
                        {"track": track, "mood": "default", "duck_curve": None},
                    )
                    return out_path
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("single_bed: failed to loop bed %s (%s) "
                                    "— falling back to synth", bed_path, exc)

        # No bed file → synth the ambient drone. Same fallback shape
        # legacy long_form.build_music_bed used.
        track = {
            "track_id": "ambient_synth",
            "mood": "ambient",
            "source": "ffmpeg_lavfi",
            "duration_s": narration_duration_s,
        }
        track_event("music.pick", category="pipeline", metadata=track)
        result = _synthesize_ambient_bed(out_path, narration_duration_s)
        emit_json_artifact(
            "music",
            {"track": track, "mood": "ambient", "duck_curve": None},
        )
        return result

    def _resolve_bed_path(self, spec: Any, bed_name: str) -> Path | None:
        """Find the bed mp3/wav under <channel>/music/ or
        spec.extra['music_dir']."""
        music_dir = (spec.extra or {}).get("music_dir")
        if music_dir:
            for ext in (".wav", ".mp3"):
                p = Path(music_dir) / f"{bed_name}{ext}"
                if p.exists():
                    return p
        from pipeline.paths import RenderPaths  # noqa: PLC0415
        try:
            rp = RenderPaths.from_channel_dir(spec.channel)
            for ext in (".wav", ".mp3"):
                p = rp.root / "music" / f"{bed_name}{ext}"
                if p.exists():
                    return p
        except Exception:  # noqa: BLE001
            pass
        return None


register_plugin("music", "single_bed", SingleBed())
assert isinstance(SingleBed(), MusicComposer)


__all__ = ["SingleBed"]
