"""Ducked-loop MusicComposer — short engine default.

Loops a music bed under the narration, ducked when narration is
loud. Selected by ``spec.music_policy = MusicPolicy.DUCKED_LOOP``.

The ffmpeg amix chain was inlined from the legacy
``pipeline.render.shorts._mix_music_bed_under_narration`` on
2026-05-14 as part of the bigbang follow-up.
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


def _mix_bed_under_narration(
    narration_path: Path,
    bed_path: Path,
    out_path: Path,
    *,
    bed_db: float = -28.0,
) -> Path:
    """ffmpeg amix the bed under the narration WAV at ``bed_db``.

    Bed audio is looped (``-stream_loop -1``) to cover the full
    narration duration, then attenuated to ``bed_db`` so it sits
    under spoken word without competing. Output is mono PCM s16le
    matching the pipeline's narration WAV format so downstream
    stages are agnostic to whether a bed was applied.

    Narration is intentionally NOT attenuated — bed alone is moved
    down. Preserves the same dialog level the ASR/beats stage saw.

    Inlined from ``pipeline.render.shorts._mix_music_bed_under_narration``
    2026-05-14.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "-i", str(narration_path),
        "-stream_loop", "-1", "-i", str(bed_path),
        "-filter_complex",
        f"[1:a]volume={bed_db}dB[bed];"
        f"[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0[a]",
        "-map", "[a]",
        "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
        str(out_path),
    ]
    run_ffmpeg(cmd, purpose="ducked_loop_mix")
    return out_path


class DuckedLoop:
    """Ducked music bed — short engine default."""

    def compose(
        self,
        spec: Any,
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path:
        # sections unused for ducked_loop (single bed).
        _ = sections

        bed_name = spec.music.default_bed
        if not bed_name or bed_name == "off":
            track = {
                "track_id": "silent",
                "mood": "none",
                "source": "music_off",
                "duration_s": narration_duration_s,
            }
            track_event("music.pick", category="pipeline", metadata=track)
            from pipeline.render.music.silent import SilentMusic  # noqa: PLC0415
            result = SilentMusic().compose(spec, narration_duration_s)
            emit_json_artifact(
                "music",
                {"track": track, "mood": "none", "duck_curve": None},
            )
            return result

        # Resolve the bed path. Pre-bigbang shorts.py looked it up via
        # cfg["music_bed_default"] under the channel YAML's music_dir;
        # we read from spec.extra (channel YAML loader populates it).
        bed_path = self._resolve_bed_path(spec, bed_name)
        if bed_path is None or not bed_path.exists():
            # Silent-fallback audit (Fix #6, 2026-05-24): when the
            # channel YAML names a real ``music_bed_default`` and the
            # asset is missing on disk (typically Dockerfile-COPY
            # drift, F24), the pre-2026-05-24 behaviour was to log a
            # warning, emit ``music.pick source=missing_bed
            # track_id=silent``, and ship an mp4 with dead silence
            # under the narration. That violates the
            # ``silent_fallback_unshippable_output`` user-directive:
            # "When a stage failure can produce unshippable output (vs
            # degraded-but-watchable), retry-then-RAISE — never
            # ``return {}`` silently." A configured music bed is part
            # of the channel's audio-mix contract; shipping without it
            # is unshippable on channels that lean on ambient
            # atmosphere (sleep history, nosleep long-form on
            # mystoriesanimated). RAISE so the cloud worker surfaces
            # ``stage=compose_failed`` in Firestore + the events log
            # carries a clear ``music_bed_missing`` signal, instead of
            # a successful-looking mp4 of narration over silence.
            #
            # Opt-out: callers that genuinely want silence must set
            # ``spec.music_policy = MusicPolicy.NONE`` (routes through
            # ``SilentMusic`` directly) or ``music_bed_default: off``
            # in the channel YAML (handled by the ``bed_name == "off"``
            # branch above).
            track = {
                "track_id": "silent",
                "mood": "none",
                "source": "missing_bed",
                "duration_s": narration_duration_s,
                "configured_bed": bed_name,
                "resolved_path": str(bed_path) if bed_path else None,
            }
            track_event("music.pick", category="pipeline",
                        success=False, metadata=track)
            emit_json_artifact(
                "music",
                {"track": track, "mood": "none", "duck_curve": None,
                 "error": "music_bed_missing"},
            )
            raise FileNotFoundError(
                f"ducked_loop: configured music_bed_default={bed_name!r} "
                f"not found on disk (resolved={bed_path}). The render "
                f"would have shipped narration over silence — refusing "
                f"to silently degrade. Either drop the bed mp3/wav into "
                f"the channel's music/ dir, set music_bed_default: off "
                f"in the channel YAML, or override music_policy=none "
                f"on the proposal. See memory "
                f"feedback_silent_fallback_unshippable_output."
            )

        # Find the narration WAV the audio stage produced (the engine
        # passes its parent path via spec.extra in tests; in production
        # the work_dir/narration.wav location is conventional).
        narration_path = self._resolve_narration_path(spec)
        if narration_path is None or not narration_path.exists():
            _logger.warning("ducked_loop: narration WAV not found "
                            "(spec.extra['narration_path']=%r); skipping bed",
                            (spec.extra or {}).get("narration_path"))
            track = {
                "track_id": "silent",
                "mood": "none",
                "source": "missing_narration",
                "duration_s": narration_duration_s,
            }
            track_event("music.pick", category="pipeline", metadata=track)
            from pipeline.render.music.silent import SilentMusic  # noqa: PLC0415
            result = SilentMusic().compose(spec, narration_duration_s)
            emit_json_artifact(
                "music",
                {"track": track, "mood": "none", "duck_curve": None},
            )
            return result

        out_path = Path("/tmp") / f"ducked_loop_{int(narration_duration_s * 1000)}.wav"
        bed_db = float(spec.music.music_bed_db)
        track = {
            "track_id": bed_name,
            "mood": "default",
            "source": str(bed_path),
            "duration_s": narration_duration_s,
        }
        duck_curve = {
            "duck_curve_id": "constant_bed_attenuation",
            "max_attenuation_db": abs(bed_db),
            "breakpoints": [
                {"t_s": 0.0, "attenuation_db": bed_db},
                {"t_s": narration_duration_s, "attenuation_db": bed_db},
            ],
        }
        track_event("music.pick", category="pipeline", metadata=track)
        track_event("music.duck", category="pipeline", metadata=duck_curve)
        try:
            _mix_bed_under_narration(
                narration_path=narration_path,
                bed_path=bed_path,
                out_path=out_path,
                bed_db=bed_db,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ducked_loop: ffmpeg amix failed (%s) — silent", exc)
            fallback = {
                "track_id": "silent",
                "mood": "none",
                "source": "duck_mix_failed",
                "duration_s": narration_duration_s,
            }
            track_event("music.pick", category="pipeline", metadata=fallback)
            from pipeline.render.music.silent import SilentMusic  # noqa: PLC0415
            result = SilentMusic().compose(spec, narration_duration_s)
            emit_json_artifact(
                "music",
                {"track": fallback, "mood": "none", "duck_curve": duck_curve},
            )
            return result
        emit_json_artifact(
            "music",
            {"track": track, "mood": "default", "duck_curve": duck_curve},
        )
        return out_path

    def _resolve_bed_path(self, spec: Any, bed_name: str) -> Path | None:
        """Find the bed mp3/wav across three locations, in priority order:

        1. ``spec.extra['music_dir']`` — caller-supplied override.
        2. ``<repo_root>/<channel>/music/<bed>.{wav,mp3}`` — per-channel
           assets (only sportsrecapped currently uses this).
        3. ``<repo_root>/data/music/<bed>.{wav,mp3}`` — central asset
           dir. Every channel YAML's ``music_bed_default`` (ambient_low,
           cinematic, upbeat, etc.) lives here. Added 2026-05-24 after
           the UI-promised-but-backend-missing audit found 3 renders
           killed by ``music_bed_default='ambient_low' not found on
           disk`` — the file existed at ``data/music/ambient_low.mp3``
           but the resolver never looked there.
        """
        music_dir = (spec.extra or {}).get("music_dir")
        if music_dir:
            for ext in (".wav", ".mp3"):
                p = Path(music_dir) / f"{bed_name}{ext}"
                if p.exists():
                    return p
        from pipeline.paths import RenderPaths, PROJECT_ROOT  # noqa: PLC0415
        try:
            rp = RenderPaths.from_channel_dir(spec.channel)
            for ext in (".wav", ".mp3"):
                p = rp.root / "music" / f"{bed_name}{ext}"
                if p.exists():
                    return p
        except Exception:  # noqa: BLE001
            pass
        # Central asset dir — the canonical home of every default bed.
        for ext in (".wav", ".mp3"):
            p = PROJECT_ROOT / "data" / "music" / f"{bed_name}{ext}"
            if p.exists():
                return p
        return None

    def _resolve_narration_path(self, spec: Any) -> Path | None:
        nar = (spec.extra or {}).get("narration_path")
        if nar:
            return Path(nar)
        # Convention: work_dir/narration.wav (engine passes work_dir).
        wd = (spec.extra or {}).get("work_dir")
        if wd:
            return Path(wd) / "narration.wav"
        return None


register_plugin("music", "ducked_loop", DuckedLoop())
assert isinstance(DuckedLoop(), MusicComposer)


__all__ = ["DuckedLoop"]
