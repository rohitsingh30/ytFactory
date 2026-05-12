"""Pin TTS ⫽ video-prep overlap on the long-form orchestrator.

Two render modes both have parallel-eligible halves:

* ``image_panels`` — :func:`pipeline.render.long_form._generate_panel_stills`
  must start before :func:`synth_long_narration` returns.
* ``archival_footage`` — :func:`pipeline.render.long_form._trim_shotlist_clips`
  must start before :func:`synth_long_narration` returns.

We use :class:`threading.Event` for deterministic happens-before
assertions. A timing-based sleep + ``assert t1 < t0`` would flake on
slow CI.

Negative cases:

* When ``YTFACTORY_DISABLE_STAGE_OVERLAP=1``, the orchestrator must
  fall back to sequential and call the legacy wrappers
  (:func:`build_image_panels_video` / :func:`build_video_track`) so
  the historical test fixture still pins them.
* When ``image_provider`` is local-GPU (``z_image_turbo`` etc), the
  image_panels orchestrator must fall back to sequential — local
  diffusion concurrent with cloud TTS would re-introduce the
  Metal contention the existing :func:`reset_mlx_state` boundary
  guards against.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from pipeline.render import long_form as render_long_form


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Tiny test harness — minimal channel skeleton + main() runner.
# ---------------------------------------------------------------------------


def _setup_channel(
    tmp: Path,
    *,
    tts_provider: str,
    render_mode: str,
    image_provider: str | None = None,
    panels: list[dict] | None = None,
    has_shotlist: bool = True,
) -> tuple[Path, str]:
    """Build a minimal channel skeleton on disk and return
    ``(channel_dir, slug)``."""
    slug = "ovl-slug"
    ch = "ovlchan"
    channel_dir = tmp / ch
    for sub in [
        "narrations", "shotlist", "long_form", "music", "branding",
        f"footage/long_sources",
        f"cache/{slug}",
    ]:
        (channel_dir / sub).mkdir(parents=True, exist_ok=True)

    # Minimal long_form config — gate on cloud providers.
    lf: dict = {
        "tts_provider": tts_provider,
        "tts_voice": "pipeline/voice_refs/sarah.wav",
        "tts_ref_text": "ref text",
        "tts_speed": 0.95,
        "tts_post_atempo": 1.0,
        "render_mode": render_mode,
        "captions_enabled": False,
        "watermark": {"enabled": False},
    }
    if image_provider is not None:
        lf["image_provider"] = image_provider
    cfg = {"name": "Overlap Channel", "long_form": lf}
    (channel_dir / "config.yaml").write_text(yaml.safe_dump(cfg))

    script: dict = {
        "narration": "First sentence. Second sentence. Third sentence.",
    }
    if panels is not None:
        script["panels"] = panels
    (channel_dir / "narrations" / f"{slug}.json").write_text(json.dumps(script))

    if has_shotlist:
        # Materialise the source mp4 so shotlist trim doesn't FileNotFoundError.
        src = channel_dir / "footage" / "long_sources" / "test.mp4"
        src.write_bytes(b"\0" * 16384)
        shotlist = {
            "clips": [
                {"source": "test.mp4", "in_s": 0.0, "out_s": 5.0},
                {"source": "test.mp4", "in_s": 5.0, "out_s": 10.0},
            ]
        }
        (channel_dir / "shotlist" / f"{slug}.json").write_text(json.dumps(shotlist))

    return channel_dir, slug


def _write_wav(p: Path) -> None:
    """Cheap WAV header + 1024 silence frames so probe_duration mocks fire."""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF" + b"\0" * 1024)


def _write_big(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * 16384)


def _run_overlap_main(
    *,
    tmp: Path,
    channel_dir: Path,
    slug: str,
    tts_mock: MagicMock,
    video_prep_mock: MagicMock,
    fake_video: Path,
    fake_music: Path,
    fake_wav: Path,
    out_mp4: Path,
    duration: float = 600.0,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Wire the granular mocks needed for an overlap-path render."""
    fake_video.write_bytes(b"\0" * 16384)
    _write_wav(fake_wav)
    _write_wav(fake_music)
    _write_big(out_mp4)

    argv = [
        "render_long_form",
        "--channel", channel_dir.name,
        "--slug", slug,
    ]

    env = dict(os.environ)
    env.pop("YTFACTORY_DISABLE_STAGE_OVERLAP", None)
    if extra_env:
        env.update(extra_env)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(sys, "argv", argv))
        stack.enter_context(patch.object(render_long_form, "REPO_ROOT", tmp))
        stack.enter_context(patch.dict(os.environ, env, clear=False))
        stack.enter_context(
            patch.object(render_long_form, "_preflight_power_check", MagicMock())
        )
        stack.enter_context(patch.object(render_long_form, "_load_env", MagicMock()))
        stack.enter_context(
            patch("pipeline.images.images_cloudrun.reset_circuit_breaker", MagicMock())
        )
        stack.enter_context(
            patch.object(render_long_form, "synth_long_narration", tts_mock)
        )
        stack.enter_context(
            patch("pipeline.probe.probe_duration", MagicMock(return_value=duration))
        )
        stack.enter_context(
            patch.object(render_long_form, "_probe_duration",
                         MagicMock(return_value=duration))
        )
        stack.enter_context(
            patch("pipeline.preflight.reset_mlx_state", MagicMock())
        )
        # The dispatch under test — patch the granular halves so the
        # overlap path goes through them.
        stack.enter_context(
            patch.object(render_long_form, "_generate_panel_stills", video_prep_mock)
        )
        stack.enter_context(
            patch.object(render_long_form, "_trim_shotlist_clips", video_prep_mock)
        )
        # Assembly half — return the fake video path.
        stack.enter_context(
            patch.object(
                render_long_form,
                "_assemble_panel_kenburns",
                MagicMock(return_value=fake_video),
            )
        )
        stack.enter_context(
            patch.object(
                render_long_form,
                "_concat_and_pad",
                MagicMock(return_value=fake_video),
            )
        )
        # Wrappers — assert NOT called when overlap is on.
        stack.enter_context(
            patch.object(
                render_long_form,
                "build_image_panels_video",
                MagicMock(return_value=fake_video),
            )
        )
        stack.enter_context(
            patch.object(
                render_long_form,
                "build_video_track",
                MagicMock(return_value=fake_video),
            )
        )
        stack.enter_context(
            patch.object(
                render_long_form,
                "build_music_bed",
                MagicMock(return_value=fake_music),
            )
        )
        stack.enter_context(
            patch.object(
                render_long_form, "final_mux", MagicMock(return_value=out_mp4)
            )
        )
        stack.enter_context(patch.object(render_long_form, "_ffmpeg", MagicMock()))
        stack.enter_context(
            patch("subprocess.check_output", MagicMock(return_value=b"600.0\n"))
        )
        render_long_form.main()


# ---------------------------------------------------------------------------
# Overlap pinning — image_panels mode
# ---------------------------------------------------------------------------


class TestImagePanelsOverlap:
    def test_panel_stills_starts_before_tts_returns(self, tmp_path: Path) -> None:
        """Pin: ``_generate_panel_stills`` enters its body BEFORE
        ``synth_long_narration`` returns. Deterministic via Event."""
        stills_entered = threading.Event()
        tts_can_finish = threading.Event()

        def _tts(**_kw) -> tuple[Path, list[Path]]:
            # Wait for the stills branch to enter — pin overlap.
            assert stills_entered.wait(timeout=3.0), \
                "_generate_panel_stills never entered while TTS was running — " \
                "stages are NOT overlapping (regression)"
            tts_can_finish.set()
            wav = tmp_path / "ovlchan" / "cache" / "ovl-slug" / "narration.wav"
            _write_wav(wav)
            return (wav, [wav])

        def _stills(**kw) -> list[Path]:
            stills_entered.set()
            # Wait for TTS to acknowledge our entry, then return.
            assert tts_can_finish.wait(timeout=3.0), \
                "TTS never finished after stills branch entered"
            cache_dir = kw["cache_dir"]
            (cache_dir / "panels").mkdir(parents=True, exist_ok=True)
            pngs: list[Path] = []
            for i, _p in enumerate(kw["panels"]):
                png = cache_dir / "panels" / f"panel_{i:03d}.png"
                _write_big(png)
                pngs.append(png)
            return pngs

        tts_mock = MagicMock(side_effect=_tts)
        stills_mock = MagicMock(side_effect=_stills)

        panels = [{"scene": "a temple", "hold_s": 20}]
        channel_dir, slug = _setup_channel(
            tmp_path,
            tts_provider="cloudrun_chatterbox",
            render_mode="image_panels",
            image_provider="cloudrun_flux2_klein",
            panels=panels,
        )
        cache_dir = channel_dir / "cache" / slug
        _run_overlap_main(
            tmp=tmp_path,
            channel_dir=channel_dir,
            slug=slug,
            tts_mock=tts_mock,
            video_prep_mock=stills_mock,
            fake_video=cache_dir / "video.mp4",
            fake_music=cache_dir / "music_bed.wav",
            fake_wav=cache_dir / "narration.wav",
            out_mp4=channel_dir / "long_form" / f"{slug}.mp4",
        )

        # Both halves must have run.
        tts_mock.assert_called_once()
        stills_mock.assert_called_once()

    def test_overlap_skipped_when_image_provider_local(
        self, tmp_path: Path
    ) -> None:
        """``z_image_turbo`` is local-GPU — overlap must be DISABLED
        even though TTS is cloud (Metal contention guard)."""
        stills_mock = MagicMock(return_value=[])

        def _tts(**_kw) -> tuple[Path, list[Path]]:
            wav = tmp_path / "ovlchan" / "cache" / "ovl-slug" / "narration.wav"
            _write_wav(wav)
            return (wav, [wav])

        tts_mock = MagicMock(side_effect=_tts)

        panels = [{"scene": "a scene", "hold_s": 20}]
        channel_dir, slug = _setup_channel(
            tmp_path,
            tts_provider="cloudrun_chatterbox",
            render_mode="image_panels",
            image_provider="z_image_turbo",  # local — gate denies overlap
            panels=panels,
        )
        cache_dir = channel_dir / "cache" / slug
        _run_overlap_main(
            tmp=tmp_path,
            channel_dir=channel_dir,
            slug=slug,
            tts_mock=tts_mock,
            video_prep_mock=stills_mock,
            fake_video=cache_dir / "video.mp4",
            fake_music=cache_dir / "music_bed.wav",
            fake_wav=cache_dir / "narration.wav",
            out_mp4=channel_dir / "long_form" / f"{slug}.mp4",
        )

        # Sequential fallback ⇒ wrapper called, granular NOT called.
        tts_mock.assert_called_once()
        stills_mock.assert_not_called()

    def test_overlap_disabled_via_env(self, tmp_path: Path) -> None:
        """``YTFACTORY_DISABLE_STAGE_OVERLAP=1`` forces sequential."""
        stills_mock = MagicMock(return_value=[])

        def _tts(**_kw) -> tuple[Path, list[Path]]:
            wav = tmp_path / "ovlchan" / "cache" / "ovl-slug" / "narration.wav"
            _write_wav(wav)
            return (wav, [wav])

        tts_mock = MagicMock(side_effect=_tts)

        panels = [{"scene": "a scene", "hold_s": 20}]
        channel_dir, slug = _setup_channel(
            tmp_path,
            tts_provider="cloudrun_chatterbox",
            render_mode="image_panels",
            image_provider="cloudrun_flux2_klein",
            panels=panels,
        )
        cache_dir = channel_dir / "cache" / slug
        _run_overlap_main(
            tmp=tmp_path,
            channel_dir=channel_dir,
            slug=slug,
            tts_mock=tts_mock,
            video_prep_mock=stills_mock,
            fake_video=cache_dir / "video.mp4",
            fake_music=cache_dir / "music_bed.wav",
            fake_wav=cache_dir / "narration.wav",
            out_mp4=channel_dir / "long_form" / f"{slug}.mp4",
            extra_env={"YTFACTORY_DISABLE_STAGE_OVERLAP": "1"},
        )

        tts_mock.assert_called_once()
        stills_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Overlap pinning — archival_footage mode
# ---------------------------------------------------------------------------


class TestArchivalFootageOverlap:
    def test_clip_trim_starts_before_tts_returns(self, tmp_path: Path) -> None:
        """Pin: ``_trim_shotlist_clips`` enters BEFORE
        ``synth_long_narration`` returns. archival_footage path."""
        trim_entered = threading.Event()
        tts_can_finish = threading.Event()

        def _tts(**_kw) -> tuple[Path, list[Path]]:
            assert trim_entered.wait(timeout=3.0), \
                "_trim_shotlist_clips never entered while TTS was running — " \
                "stages are NOT overlapping (regression)"
            tts_can_finish.set()
            wav = tmp_path / "ovlchan" / "cache" / "ovl-slug" / "narration.wav"
            _write_wav(wav)
            return (wav, [wav])

        def _trim(**kw) -> list[Path]:
            trim_entered.set()
            assert tts_can_finish.wait(timeout=3.0), \
                "TTS never finished after trim branch entered"
            cache_dir = kw["cache_dir"]
            (cache_dir / "long_clips").mkdir(parents=True, exist_ok=True)
            paths: list[Path] = []
            for i, _c in enumerate(kw["shotlist"]["clips"]):
                p = cache_dir / "long_clips" / f"clip_{i:03d}.mp4"
                _write_big(p)
                paths.append(p)
            return paths

        tts_mock = MagicMock(side_effect=_tts)
        trim_mock = MagicMock(side_effect=_trim)

        # archival_footage uses image_provider=None (no image branch);
        # gate evaluates only on TTS being cloud.
        channel_dir, slug = _setup_channel(
            tmp_path,
            tts_provider="cloudrun_chatterbox",
            render_mode="archival_footage",
        )
        cache_dir = channel_dir / "cache" / slug
        _run_overlap_main(
            tmp=tmp_path,
            channel_dir=channel_dir,
            slug=slug,
            tts_mock=tts_mock,
            video_prep_mock=trim_mock,
            fake_video=cache_dir / "video.mp4",
            fake_music=cache_dir / "music_bed.wav",
            fake_wav=cache_dir / "narration.wav",
            out_mp4=channel_dir / "long_form" / f"{slug}.mp4",
        )

        tts_mock.assert_called_once()
        trim_mock.assert_called_once()

    def test_local_tts_blocks_overlap(self, tmp_path: Path) -> None:
        """``f5_tts`` (local MLX) — overlap denied, sequential
        wrapper called instead."""
        trim_mock = MagicMock(return_value=[])

        def _tts(**_kw) -> tuple[Path, list[Path]]:
            wav = tmp_path / "ovlchan" / "cache" / "ovl-slug" / "narration.wav"
            _write_wav(wav)
            return (wav, [wav])

        tts_mock = MagicMock(side_effect=_tts)
        channel_dir, slug = _setup_channel(
            tmp_path,
            tts_provider="f5_tts",
            render_mode="archival_footage",
        )
        cache_dir = channel_dir / "cache" / slug
        _run_overlap_main(
            tmp=tmp_path,
            channel_dir=channel_dir,
            slug=slug,
            tts_mock=tts_mock,
            video_prep_mock=trim_mock,
            fake_video=cache_dir / "video.mp4",
            fake_music=cache_dir / "music_bed.wav",
            fake_wav=cache_dir / "narration.wav",
            out_mp4=channel_dir / "long_form" / f"{slug}.mp4",
        )

        tts_mock.assert_called_once()
        # Sequential path → wrapper called, granular NOT called.
        trim_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Stdout markers — the cloud-worker tail-reader picks these up.
# ---------------------------------------------------------------------------


class TestProgressMarkers:
    def test_overlap_emits_done_marker(self, tmp_path: Path, capsys) -> None:
        """The new explicit ``[1/5] tts done X.Ys`` marker MUST appear
        when overlap runs. The cloud worker's ``_classify_long_form_line``
        regex bank parses this to mark the TTS pill done — without it
        the timeline pill would stay stuck in ``running`` forever."""
        def _tts(**_kw) -> tuple[Path, list[Path]]:
            wav = tmp_path / "ovlchan" / "cache" / "ovl-slug" / "narration.wav"
            _write_wav(wav)
            return (wav, [wav])

        def _trim(**kw) -> list[Path]:
            cache_dir = kw["cache_dir"]
            (cache_dir / "long_clips").mkdir(parents=True, exist_ok=True)
            return []

        tts_mock = MagicMock(side_effect=_tts)
        trim_mock = MagicMock(side_effect=_trim)
        channel_dir, slug = _setup_channel(
            tmp_path,
            tts_provider="cloudrun_chatterbox",
            render_mode="archival_footage",
        )
        cache_dir = channel_dir / "cache" / slug
        _run_overlap_main(
            tmp=tmp_path,
            channel_dir=channel_dir,
            slug=slug,
            tts_mock=tts_mock,
            video_prep_mock=trim_mock,
            fake_video=cache_dir / "video.mp4",
            fake_music=cache_dir / "music_bed.wav",
            fake_wav=cache_dir / "narration.wav",
            out_mp4=channel_dir / "long_form" / f"{slug}.mp4",
        )
        out = capsys.readouterr().out
        # New explicit done marker
        assert "[1/5] tts done" in out, (
            "expected new explicit '[1/5] tts done X.Ys' marker for the "
            "cloud worker tail-reader; got:\n" + out
        )
        # Backwards-compat legacy marker still present (older tail
        # readers depend on this — graceful downgrade)
        assert "[1/5] narration" in out, (
            "expected legacy '[1/5] narration N chunks → narration.wav' marker "
            "for backwards compatibility with pre-2026-05-13 cloud workers"
        )
        assert "[2/5] video prep done" in out
