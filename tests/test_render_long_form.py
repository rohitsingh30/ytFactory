"""Comprehensive unit tests for pipeline/render/long_form.py.

Covers every function and major branch. All heavy dependencies (ffmpeg,
ffprobe, TTS adapters, image gen, whisper) are mocked so the suite runs
fast in CI without any GPU or ML stack.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from PIL import ImageFont

from pipeline.render import long_form as render_long_form


_TEMP_PARENT = Path(__file__).resolve().parent.parent / ".test-tmp-render-long-form"


@contextlib.contextmanager
def local_tempdir():
    _TEMP_PARENT.mkdir(exist_ok=True)
    td = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
    try:
        yield Path(td.name)
    finally:
        td.cleanup()
        with contextlib.suppress(OSError):
            _TEMP_PARENT.rmdir()


def write_big(path: Path, size: int = 4097) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def write_wav(path: Path, duration_s: float = 1.0, sample_rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)
    return path


class FakeWord:
    def __init__(self, text: str, start: float, end: float):
        self.text = text
        self.start = start
        self.end = end


# ---------------------------------------------------------------------------
# _load_env, _f5_chunk, _kokoro_chunk, _ffmpeg, _atempo, _wav_concat
# ---------------------------------------------------------------------------

class EnvAndAdapterTests(unittest.TestCase):
    def test_load_env_missing_returns(self):
        with local_tempdir() as tmp, patch.dict(os.environ, {}, clear=True):
            render_long_form._load_env(tmp)
            self.assertEqual(os.environ, {})

    def test_load_env_parses_comments_quotes_and_setdefault(self):
        with local_tempdir() as tmp, patch.dict(os.environ, {"EXISTING": "old"}, clear=True):
            (tmp / ".env").write_text(
                "\n# comment\nFOO=bar\nQUOTED=\"two words\"\nSINGLE='one word'\n"
                "SPACED =  value  \nNO_EQUALS\nEXISTING=new\n"
            )
            render_long_form._load_env(tmp)
            self.assertEqual(os.environ["FOO"], "bar")
            self.assertEqual(os.environ["QUOTED"], "two words")
            self.assertEqual(os.environ["SINGLE"], "one word")
            self.assertEqual(os.environ["SPACED"], "value")
            self.assertEqual(os.environ["EXISTING"], "old")

    def test_f5_chunk_prefixes_relative_ref_audio_path(self):
        with local_tempdir() as tmp:
            out = tmp / "out" / "chunk.wav"
            synth = MagicMock()
            with patch.object(render_long_form, "REPO_ROOT", tmp), \
                 patch("pipeline.audio.synthesize", synth, create=True):
                render_long_form._f5_chunk(
                    "hello", "voices/ref.wav", "ref", out, speed=0.7, provider="cloudrun_f5"
                )
            synth.assert_called_once()
            kwargs = synth.call_args.kwargs
            self.assertEqual(kwargs["voice"], str(tmp / "voices/ref.wav"))
            self.assertEqual(kwargs["out_path"], out)
            self.assertEqual(kwargs["provider"], "cloudrun_f5")
            self.assertTrue(out.parent.exists())

    def test_f5_chunk_leaves_absolute_ref_audio_path(self):
        with local_tempdir() as tmp:
            absolute = tmp / "voice.wav"
            out = tmp / "chunk.wav"
            synth = MagicMock()
            with patch("pipeline.audio.synthesize", synth, create=True):
                render_long_form._f5_chunk("hello", str(absolute), "ref", out)
            self.assertEqual(synth.call_args.kwargs["voice"], str(absolute))

    def test_kokoro_chunk_delegates_to_audio_adapter(self):
        with local_tempdir() as tmp:
            out = tmp / "nested" / "kokoro.wav"
            synth = MagicMock()
            with patch("pipeline.audio._synth_kokoro", synth, create=True):
                render_long_form._kokoro_chunk("hello", "af_heart", out, speed=1.2)
            synth.assert_called_once_with(text="hello", voice="af_heart", out_path=out, speed=1.2)
            self.assertTrue(out.parent.exists())

    def test_ffmpeg_success_and_failure(self):
        ok = MagicMock(returncode=0)
        bad = MagicMock(returncode=7)
        with patch("subprocess.run", return_value=ok) as run:
            render_long_form._ffmpeg(["-i", "in", "out"])
        self.assertEqual(run.call_args.args[0][:4], ["ffmpeg", "-y", "-loglevel", "error"])
        with patch("subprocess.run", return_value=bad):
            with self.assertRaises(RuntimeError) as cm:
                render_long_form._ffmpeg(["bad"])
        self.assertIn("ffmpeg failed", str(cm.exception))

    def test_atempo_calls_ffmpeg(self):
        with local_tempdir() as tmp, patch.object(render_long_form, "_ffmpeg") as ff:
            render_long_form._atempo(tmp / "in.wav", tmp / "out.wav", 0.85)
            self.assertIn("atempo=0.85", ff.call_args.args[0])

    def test_wav_concat_rejects_empty_and_builds_concat_list(self):
        with local_tempdir() as tmp:
            with self.assertRaises(ValueError):
                render_long_form._wav_concat_with_silence([], 0.4, tmp / "out.wav")
            wavs = [write_wav(tmp / "a.wav"), write_wav(tmp / "b.wav")]
            with patch.object(render_long_form, "_ffmpeg") as ff:
                render_long_form._wav_concat_with_silence(wavs, 0.25, tmp / "out.wav")
            self.assertEqual(ff.call_count, 2)
            list_txt = tmp / "_concat_list.txt"
            body = list_txt.read_text()
            self.assertIn("_silence.wav", body)
            self.assertIn(str(wavs[0].resolve()), body)

    def test_wav_concat_single_wav_no_silence_entry(self):
        with local_tempdir() as tmp:
            wav = write_wav(tmp / "only.wav")
            with patch.object(render_long_form, "_ffmpeg") as ff:
                render_long_form._wav_concat_with_silence([wav], 0.1, tmp / "out.wav")
            self.assertEqual(ff.call_count, 2)
            self.assertNotIn("_silence.wav", (tmp / "_concat_list.txt").read_text())

    def test_wav_concat_silence_matches_input_sample_rate(self):
        # Audit T1.15 — silence must be rendered at the same sample
        # rate as the inputs so concat-demuxer's -c copy doesn't
        # refuse the heterogeneous stream-parameter set. Cloud
        # Chatterbox returns 22050 Hz mono, IndicF5 returns various
        # rates; pre-fix this hardcoded 44100/mono and broke concat
        # whenever inputs differed.
        from unittest.mock import MagicMock
        with local_tempdir() as tmp:
            wavs = [write_wav(tmp / "a.wav"), write_wav(tmp / "b.wav")]
            # Mock ffprobe to return 22050 Hz mono.
            fake_probe = MagicMock()
            fake_probe.returncode = 0
            fake_probe.stdout = "22050\n1\nmono\n"
            with patch.object(render_long_form, "_ffmpeg") as ff, \
                 patch("subprocess.run", return_value=fake_probe):
                render_long_form._wav_concat_with_silence(
                    wavs, 0.25, tmp / "out.wav",
                )
            # First _ffmpeg call generates the silence wav. Its anullsrc
            # spec MUST carry the probed rate, not 44100.
            silence_call = ff.call_args_list[0].args[0]
            cmd_str = " ".join(silence_call)
            self.assertIn("anullsrc=r=22050", cmd_str)
            self.assertNotIn("anullsrc=r=44100", cmd_str)

    def test_probe_wav_params_falls_back_to_channels_count_when_layout_absent(self):
        # Covers the channel_layout-empty fallback path: ffprobe didn't
        # surface channel_layout for some odd containers, but channels
        # count was reported. _probe_wav_params derives mono/stereo/Nc
        # from the count.
        from unittest.mock import MagicMock
        with local_tempdir() as tmp:
            wav = write_wav(tmp / "x.wav")
            for n_channels, expected_layout in [
                ("1", "mono"),
                ("2", "stereo"),
                ("6", "6c"),
            ]:
                with self.subTest(n_channels=n_channels):
                    fake_probe = MagicMock()
                    fake_probe.returncode = 0
                    # Two-line output: rate + channels (no layout line).
                    fake_probe.stdout = f"48000\n{n_channels}\n"
                    with patch("subprocess.run", return_value=fake_probe):
                        rate, layout = render_long_form._probe_wav_params(wav)
                    self.assertEqual(rate, 48000)
                    self.assertEqual(layout, expected_layout)


    def test_wav_concat_silence_matches_stereo_layout(self):
        from unittest.mock import MagicMock
        with local_tempdir() as tmp:
            wavs = [write_wav(tmp / "a.wav"), write_wav(tmp / "b.wav")]
            fake_probe = MagicMock()
            fake_probe.returncode = 0
            fake_probe.stdout = "48000\n2\nstereo\n"
            with patch.object(render_long_form, "_ffmpeg") as ff, \
                 patch("subprocess.run", return_value=fake_probe):
                render_long_form._wav_concat_with_silence(
                    wavs, 0.25, tmp / "out.wav",
                )
            silence_call = ff.call_args_list[0].args[0]
            cmd_str = " ".join(silence_call)
            self.assertIn("anullsrc=r=48000:cl=stereo", cmd_str)


# ---------------------------------------------------------------------------
# synth_long_narration
# ---------------------------------------------------------------------------

class SynthLongNarrationTests(unittest.TestCase):
    @staticmethod
    def _f5_side_effect(_text, _voice, _ref_text, out_wav, **_kwargs):
        write_big(Path(out_wav), 2048)

    @staticmethod
    def _kokoro_side_effect(_text, _voice, out_wav, **_kwargs):
        write_big(Path(out_wav), 2048)

    @staticmethod
    def _atempo_side_effect(_in_wav, out_wav, _factor):
        write_big(Path(out_wav), 2048)

    def test_unknown_provider_raises(self):
        with local_tempdir() as tmp:
            with self.assertRaises(RuntimeError) as cm:
                render_long_form.synth_long_narration(
                    "Hello.", "v", tmp, 1.0, ref_audio_text="ref", provider="higgs"
                )
            self.assertIn("unsupported provider", str(cm.exception))

    def test_kokoro_serial_path_uses_kokoro_adapter_and_atempo(self):
        with local_tempdir() as tmp:
            with patch.object(render_long_form, "_kokoro_chunk", side_effect=self._kokoro_side_effect) as ko, \
                 patch.object(render_long_form, "_f5_chunk") as f5, \
                 patch.object(render_long_form, "_atempo", side_effect=self._atempo_side_effect) as at, \
                 patch.object(render_long_form, "_wav_concat_with_silence") as concat:
                out, chunks = render_long_form.synth_long_narration(
                    "One. Two.", "af_heart", tmp, 0.9, chunk_target_chars=5, provider="kokoro"
                )
            self.assertEqual(out, tmp / "narration.wav")
            self.assertEqual(len(chunks), 2)
            self.assertEqual(ko.call_count, 2)
            f5.assert_not_called()
            self.assertEqual(at.call_count, 2)
            concat.assert_called_once()

    def test_f5_serial_path_uses_f5_adapter(self):
        with local_tempdir() as tmp:
            with patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect) as f5, \
                 patch.object(render_long_form, "_atempo", side_effect=self._atempo_side_effect), \
                 patch.object(render_long_form, "_wav_concat_with_silence"):
                render_long_form.synth_long_narration(
                    "One. Two.", "voice.wav", tmp, 0.9, chunk_target_chars=5,
                    ref_audio_text="ref", provider="f5_tts"
                )
            self.assertEqual(f5.call_count, 2)
            self.assertEqual({c.kwargs["provider"] for c in f5.call_args_list}, {"f5_tts"})

    def test_cloudrun_workers_one_uses_serial_path_and_prewarm_success(self):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *_exc): return False
            def read(self): return b"ready"

        with local_tempdir() as tmp, patch.dict(os.environ, {"YTFACTORY_CLOUD_TTS_WORKERS": "1"}):
            with patch("pipeline.tts.cloudrun._service_url", return_value="http://tts"), \
                 patch("pipeline.tts.cloudrun._get_id_token", return_value="tok"), \
                 patch("urllib.request.urlopen", return_value=Resp()) as urlopen, \
                 patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect) as f5, \
                 patch.object(render_long_form, "_atempo", side_effect=self._atempo_side_effect), \
                 patch.object(render_long_form, "_wav_concat_with_silence"):
                render_long_form.synth_long_narration(
                    "One. Two.", "voice.wav", tmp, 0.9, chunk_target_chars=5,
                    ref_audio_text="ref", provider="cloudrun_f5"
                )
            urlopen.assert_called_once()
            self.assertEqual(f5.call_count, 2)

    def test_cloudrun_prewarm_failure_is_nonfatal(self):
        with local_tempdir() as tmp, patch.dict(os.environ, {"YTFACTORY_CLOUD_TTS_WORKERS": "1"}):
            with patch("pipeline.tts.cloudrun._service_url", return_value="http://tts"), \
                 patch("pipeline.tts.cloudrun._get_id_token", return_value="tok"), \
                 patch("urllib.request.urlopen", side_effect=OSError("cold")), \
                 patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect), \
                 patch.object(render_long_form, "_atempo", side_effect=self._atempo_side_effect), \
                 patch.object(render_long_form, "_wav_concat_with_silence"):
                render_long_form.synth_long_narration(
                    "One. Two.", "voice.wav", tmp, 0.9, chunk_target_chars=5,
                    ref_audio_text="ref", provider="cloudrun_f5"
                )

    def test_cloudrun_parallel_path_synths_missing_raws_and_atempo(self):
        with local_tempdir() as tmp, patch.dict(os.environ, {"YTFACTORY_CLOUD_TTS_WORKERS": "2"}):
            with patch("pipeline.tts.cloudrun._service_url", return_value="http://tts"), \
                 patch("pipeline.tts.cloudrun._get_id_token", return_value="tok"), \
                 patch("urllib.request.urlopen", side_effect=OSError("skip")), \
                 patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect) as f5, \
                 patch.object(render_long_form, "_atempo", side_effect=self._atempo_side_effect) as at, \
                 patch.object(render_long_form, "_wav_concat_with_silence"):
                render_long_form.synth_long_narration(
                    "One. Two.", "voice.wav", tmp, 0.8, chunk_target_chars=5,
                    ref_audio_text="ref", provider="cloudrun_f5"
                )
            self.assertEqual(f5.call_count, 2)
            self.assertEqual(at.call_count, 2)

    def test_cloudrun_parallel_copy_path_when_atempo_is_one(self):
        with local_tempdir() as tmp, patch.dict(os.environ, {"YTFACTORY_CLOUD_TTS_WORKERS": "2"}):
            with patch("pipeline.tts.cloudrun._service_url", return_value="http://tts"), \
                 patch("pipeline.tts.cloudrun._get_id_token", return_value="tok"), \
                 patch("urllib.request.urlopen", side_effect=OSError("skip")), \
                 patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect), \
                 patch.object(render_long_form, "_wav_concat_with_silence"), \
                 patch.object(render_long_form.shutil, "copy2",
                              side_effect=lambda src, dst: write_big(Path(dst), 2048)) as cp:
                render_long_form.synth_long_narration(
                    "One. Two.", "voice.wav", tmp, 1.0, chunk_target_chars=5,
                    ref_audio_text="ref", provider="cloudrun_f5"
                )
            self.assertEqual(cp.call_count, 2)

    def test_all_chunks_cached_skips_synth_and_concats(self):
        with local_tempdir() as tmp:
            chunk_dir = tmp / "tts_chunks"
            write_big(chunk_dir / "chunk_0000.wav", 2048)
            write_big(chunk_dir / "chunk_0001.wav", 2048)
            with patch.object(render_long_form, "_f5_chunk") as f5, \
                 patch.object(render_long_form, "_atempo") as at, \
                 patch.object(render_long_form, "_wav_concat_with_silence") as concat:
                out, chunks = render_long_form.synth_long_narration(
                    "One. Two.", "voice.wav", tmp, 0.9, chunk_target_chars=5,
                    ref_audio_text="ref", provider="f5_tts"
                )
            f5.assert_not_called()
            at.assert_not_called()
            concat.assert_called_once_with(chunks, 0.4, out)

    def test_serial_atempo_one_uses_copy2(self):
        with local_tempdir() as tmp:
            with patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect), \
                 patch.object(render_long_form.shutil, "copy2",
                              side_effect=lambda src, dst: write_big(Path(dst), 2048)) as cp, \
                 patch.object(render_long_form, "_atempo") as at, \
                 patch.object(render_long_form, "_wav_concat_with_silence"):
                render_long_form.synth_long_narration(
                    "One.", "voice.wav", tmp, 1.0, ref_audio_text="ref", provider="f5_tts"
                )
            cp.assert_called_once()
            at.assert_not_called()

    def test_serial_raw_cached_skips_raw_synth(self):
        with local_tempdir() as tmp:
            raw = tmp / "tts_chunks" / "raw_0000.wav"
            write_big(raw, 2048)
            with patch.object(render_long_form, "_f5_chunk") as f5, \
                 patch.object(render_long_form.shutil, "copy2",
                              side_effect=lambda src, dst: write_big(Path(dst), 2048)), \
                 patch.object(render_long_form, "_wav_concat_with_silence"):
                render_long_form.synth_long_narration(
                    "One.", "voice.wav", tmp, 1.0, ref_audio_text="ref", provider="f5_tts"
                )
            f5.assert_not_called()

    def _run_with_fake_mlx(self, core_mod):
        mlx_pkg = types.ModuleType("mlx")
        mlx_pkg.core = core_mod
        with local_tempdir() as tmp, patch.dict(os.environ, {"YTFACTORY_MLX_FLUSH_EVERY": "1"}), \
             patch.dict(sys.modules, {"mlx": mlx_pkg, "mlx.core": core_mod}), \
             patch.object(render_long_form, "_f5_chunk", side_effect=self._f5_side_effect), \
             patch.object(render_long_form, "_atempo", side_effect=self._atempo_side_effect), \
             patch.object(render_long_form, "_wav_concat_with_silence"):
            render_long_form.synth_long_narration(
                "One. Two.", "voice.wav", tmp, 0.9, chunk_target_chars=5,
                ref_audio_text="ref", provider="f5_tts"
            )

    def test_mlx_flush_clear_cache_branch(self):
        core = types.ModuleType("mlx.core")
        core.clear_cache = MagicMock()
        self._run_with_fake_mlx(core)
        self.assertGreaterEqual(core.clear_cache.call_count, 1)

    def test_mlx_flush_metal_clear_cache_branch(self):
        core = types.ModuleType("mlx.core")
        core.metal = types.SimpleNamespace(clear_cache=MagicMock())
        self._run_with_fake_mlx(core)
        self.assertGreaterEqual(core.metal.clear_cache.call_count, 1)

    def test_mlx_flush_exception_is_logged_not_raised(self):
        core = types.ModuleType("mlx.core")
        core.clear_cache = MagicMock(side_effect=RuntimeError("boom"))
        self._run_with_fake_mlx(core)
        self.assertGreaterEqual(core.clear_cache.call_count, 1)


# ---------------------------------------------------------------------------
# _probe_duration, _trim_clip_letterbox, build_image_panels_video,
# build_video_track, build_music_bed
# ---------------------------------------------------------------------------

class VideoStageTests(unittest.TestCase):
    def test_probe_duration_delegates_to_pipeline_probe(self):
        with local_tempdir() as tmp, \
             patch("pipeline.probe.probe_duration", return_value=12.5) as p:
            self.assertEqual(render_long_form._probe_duration(tmp / "x.wav"), 12.5)
            p.assert_called_once()

    def test_trim_probe_failure_falls_through_to_full_chain(self):
        with local_tempdir() as tmp, \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "ffprobe")), \
             patch.object(render_long_form, "_ffmpeg") as ff:
            render_long_form._trim_clip_letterbox(tmp / "src.mp4", 0, 5, tmp / "out.mp4")
        self.assertIn("split=2", " ".join(map(str, ff.call_args.args[0])))

    def test_trim_exact_match_stream_copies(self):
        with local_tempdir() as tmp, \
             patch("subprocess.check_output", return_value=b"1920\n1080\n"), \
             patch.object(render_long_form, "_ffmpeg") as ff:
            render_long_form._trim_clip_letterbox(tmp / "src.mp4", 1, 4, tmp / "out.mp4")
        args = ff.call_args.args[0]
        self.assertIn("copy", args)
        self.assertNotIn("-filter_complex", args)

    def test_trim_aspect_match_plain_scale(self):
        with local_tempdir() as tmp, \
             patch("subprocess.check_output", return_value=b"1280\n720\n"), \
             patch.object(render_long_form, "_ffmpeg") as ff:
            render_long_form._trim_clip_letterbox(tmp / "src.mp4", 0, 5, tmp / "out.mp4")
        joined = " ".join(map(str, ff.call_args.args[0]))
        self.assertIn("scale=1920:1080", joined)
        self.assertNotIn("gblur", joined)

    def test_trim_aspect_mismatch_full_chain_and_grade_bypasses_shortcuts(self):
        with local_tempdir() as tmp, patch.object(render_long_form, "_ffmpeg") as ff:
            with patch("subprocess.check_output", return_value=b"640\n480\n"):
                render_long_form._trim_clip_letterbox(tmp / "src.mp4", 0, 5, tmp / "out1.mp4")
            with patch("subprocess.check_output", return_value=b"1920\n1080\n"):
                render_long_form._trim_clip_letterbox(
                    tmp / "src.mp4", 0, 5, tmp / "out2.mp4",
                    grade_filter="eq=saturation=1.1"
                )
        self.assertIn("gblur", " ".join(map(str, ff.call_args_list[0].args[0])))
        self.assertIn("eq=saturation=1.1", " ".join(map(str, ff.call_args_list[1].args[0])))

    def test_build_image_panels_single_panel_generates_segment_and_copies(self):
        with local_tempdir() as tmp:
            with patch("pipeline.images.images.generate",
                       side_effect=lambda **kw: write_big(Path(kw["out_path"]))) as gen, \
                 patch.object(render_long_form, "_ffmpeg") as ff:
                video = render_long_form.build_image_panels_video(
                    [{"scene": "temple", "hold_s": 3}],
                    "style", "prov", 10, 4, 320, 180, tmp
                )
            self.assertEqual(video, tmp / "video.mp4")
            gen.assert_called_once()
            self.assertEqual(ff.call_count, 2)
            self.assertIn("copy", ff.call_args.args[0])

    def test_build_image_panels_two_panels_uses_xfade(self):
        with local_tempdir() as tmp:
            with patch("pipeline.images.images.generate",
                       side_effect=lambda **kw: write_big(Path(kw["out_path"]))), \
                 patch.object(render_long_form, "_ffmpeg") as ff:
                render_long_form.build_image_panels_video(
                    [{"scene": "a", "hold_s": 3}, {"scene": "b", "hold_s": 4}],
                    "style", "prov", 1, 4, 320, 180, tmp, crossfade_s=0.5
                )
            self.assertIn("xfade=transition=fade", " ".join(map(str, ff.call_args.args[0])))

    def test_build_image_panels_cached_png_and_segment_skip_work(self):
        with local_tempdir() as tmp:
            write_big(tmp / "panels" / "panel_000.png")
            write_big(tmp / "panel_segments" / "seg_000.mp4")
            with patch("pipeline.images.images.generate") as gen, \
                 patch.object(render_long_form, "_ffmpeg") as ff:
                render_long_form.build_image_panels_video(
                    [{"scene": "cached", "hold_s": 3}], "style", "prov", 1, 4, 320, 180, tmp
                )
            gen.assert_not_called()
            self.assertEqual(ff.call_count, 1)

    def test_build_image_panels_missing_scene_raises(self):
        with local_tempdir() as tmp:
            with self.assertRaises(ValueError):
                render_long_form.build_image_panels_video(
                    [{"hold_s": 3}], "style", "prov", 1, 4, 320, 180, tmp
                )

    def test_build_video_track_empty_and_missing_source_errors(self):
        with local_tempdir() as tmp:
            with self.assertRaises(ValueError):
                render_long_form.build_video_track({"clips": []}, tmp, tmp / "cache", 10)
            cache = tmp / "cache"
            cache.mkdir()
            with self.assertRaises(FileNotFoundError):
                render_long_form.build_video_track(
                    {"clips": [{"source": "missing.mp4", "in_s": 0, "out_s": 1}]},
                    tmp, cache, 10
                )

    def _run_video_track(self, tmp: Path, *, have: float, cached: bool = False):
        src_dir = tmp / "sources"
        write_big(src_dir / "a.mp4")
        cache = tmp / "cache"
        cache.mkdir(exist_ok=True)
        if cached:
            write_big(cache / "long_clips" / "clip_000.mp4", 2048)

        def run_jobs(jobs, **_kw):
            for job in jobs:
                job()

        with patch.object(render_long_form, "_trim_clip_letterbox",
                          side_effect=lambda *a, **k: write_big(Path(a[3]), 2048)) as trim, \
             patch.object(render_long_form, "_probe_duration", return_value=have), \
             patch.object(render_long_form, "_ffmpeg") as ff, \
             patch("pipeline.parallel.run_parallel", side_effect=run_jobs) as rp:
            out = render_long_form.build_video_track(
                {"clips": [{"source": "a.mp4", "in_s": 0, "out_s": 5}]},
                src_dir, cache, 10
            )
        return out, trim, ff, rp

    def test_build_video_track_no_padding_needed(self):
        with local_tempdir() as tmp:
            out, trim, ff, rp = self._run_video_track(tmp, have=10.5)
        self.assertEqual(out.name, "video.mp4")
        trim.assert_called_once()
        rp.assert_called_once()
        self.assertEqual(ff.call_count, 1)

    def test_build_video_track_padding_needed(self):
        with local_tempdir() as tmp:
            _out, _trim, ff, _rp = self._run_video_track(tmp, have=4.0)
        self.assertEqual(ff.call_count, 4)
        self.assertIn("setpts=", " ".join(map(str, ff.call_args_list[1].args[0])))

    def test_build_video_track_cached_clip_skips_trim(self):
        with local_tempdir() as tmp:
            _out, trim, _ff, rp = self._run_video_track(tmp, have=10.5, cached=True)
        trim.assert_not_called()
        rp.assert_not_called()

    def test_build_music_bed_uses_drone_filter(self):
        with local_tempdir() as tmp, patch.object(render_long_form, "_ffmpeg") as ff:
            out = render_long_form.build_music_bed(tmp / "bed.wav", 12.0)
        self.assertEqual(out, tmp / "bed.wav")
        filt = ff.call_args.args[0][1]
        # Filter uses sine at 82, 123, 164 Hz with aecho
        self.assertIn("82", filt)
        self.assertIn("123", filt)
        self.assertIn("aecho", filt)


# ---------------------------------------------------------------------------
# build_captions_srt, build_caption_pngs, build_caption_pngs_from_chunks,
# render_watermark_png, _render_caption_png, build_captions_ass
# ---------------------------------------------------------------------------

class CaptionRenderingTests(unittest.TestCase):
    def test_hms_format(self):
        self.assertEqual(render_long_form._hms(0.0), "00:00:00,000")
        self.assertEqual(render_long_form._hms(65.5), "00:01:05,500")
        self.assertEqual(render_long_form._hms(3661.234), "01:01:01,234")

    def test_build_captions_srt_sentence_groups_and_wraps(self):
        words = [
            FakeWord("Hello", 0, 0.2), FakeWord("world.", 0.2, 0.5),
            FakeWord("one", 1.0, 1.1), FakeWord("two", 1.1, 1.2),
            FakeWord("three", 1.2, 1.3), FakeWord("four", 1.3, 1.4),
            FakeWord("five.", 1.4, 2.0),
        ]
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words):
            out = render_long_form.build_captions_srt(
                tmp / "n.wav", tmp / "out.srt",
                max_chars_per_line=8, max_lines_per_cue=1
            )
            body = out.read_text()
            self.assertIn("Hello", body)
            self.assertGreater(body.count("-->"), 2)

    def test_build_captions_srt_no_words_raises(self):
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=[]):
            with self.assertRaises(RuntimeError):
                render_long_form.build_captions_srt(tmp / "n.wav", tmp / "out.srt")

    def test_build_captions_srt_trailing_unpunctuated_sentence(self):
        words = [FakeWord("Trailing", 0.0, 0.2), FakeWord("sentence", 0.2, 0.5)]
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words):
            out = render_long_form.build_captions_srt(tmp / "n.wav", tmp / "out.srt")
            self.assertIn("Trailing sentence", out.read_text())

    def test_audit_q221_build_captions_srt_threads_asr_provider(self):
        """Audit Q2.21 — pre-fix this called transcribe_words(narration_wav)
        without ``provider=``, so the channel YAML's asr_provider was
        silently ignored on the long-form caption path."""
        words = [FakeWord("Hi.", 0.0, 0.5)]
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words) as m:
            render_long_form.build_captions_srt(
                tmp / "n.wav", tmp / "out.srt",
                asr_provider="parakeet_mlx",
            )
            m.assert_called_once_with(tmp / "n.wav", provider="parakeet_mlx")

    def test_audit_q221_build_captions_ass_whisper_path_threads_asr_provider(self):
        words = [FakeWord("Hi.", 0.0, 0.5)]
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words) as m:
            render_long_form.build_captions_ass(
                tmp / "out.ass",
                narration_wav=tmp / "n.wav",
                asr_provider="parakeet_mlx",
            )
            m.assert_called_once_with(tmp / "n.wav", provider="parakeet_mlx")

    def test_build_caption_pngs_normal_font_fallback(self):
        words = [FakeWord("Hello", 0, 0.5), FakeWord("world.", 0.5, 1.0)]
        with local_tempdir() as tmp:
            with patch("pipeline.beats.transcribe_words", return_value=words), \
                 patch.object(render_long_form.Path, "exists", return_value=False):
                cues = render_long_form.build_caption_pngs(
                    tmp / "n.wav", tmp / "caps", max_chars_per_line=5
                )
            self.assertEqual(len(cues), 1)
            self.assertTrue(cues[0][0].exists())
            self.assertGreater(cues[0][0].stat().st_size, 0)

    def test_build_caption_pngs_no_words_raises(self):
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=[]):
            with self.assertRaises(RuntimeError):
                render_long_form.build_caption_pngs(tmp / "n.wav", tmp / "caps")

    def test_audit_q221_build_caption_pngs_threads_asr_provider(self):
        """Audit Q2.21 — same fix on the PNG-overlay caption path."""
        words = [FakeWord("Hi.", 0.0, 0.5)]
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words) as m:
            render_long_form.build_caption_pngs(
                tmp / "n.wav", tmp / "caps",
                asr_provider="parakeet_mlx",
            )
            m.assert_called_once_with(tmp / "n.wav", provider="parakeet_mlx")

    def test_build_caption_pngs_trailing_sentence_truetype_exception(self):
        words = [FakeWord("Trailing", 0.0, 0.2), FakeWord("caption", 0.2, 0.5)]
        default_font = ImageFont.load_default()
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words), \
             patch.object(render_long_form.Path, "exists", return_value=True), \
             patch("PIL.ImageFont.truetype", side_effect=OSError("bad font")), \
             patch("PIL.ImageFont.load_default", return_value=default_font):
            cues = render_long_form.build_caption_pngs(tmp / "n.wav", tmp / "caps")
        self.assertEqual(len(cues), 1)

    def test_build_caption_pngs_truetype_success_breaks_font_loop(self):
        words = [FakeWord("Hello.", 0.0, 0.5)]
        font = ImageFont.load_default()
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words), \
             patch.object(render_long_form.Path, "exists", return_value=True), \
             patch("PIL.ImageFont.truetype", return_value=font) as tt:
            cues = render_long_form.build_caption_pngs(tmp / "n.wav", tmp / "caps")
        self.assertEqual(len(cues), 1)
        tt.assert_called_once()

    def test_build_caption_pngs_from_chunks_mismatch_raises(self):
        with local_tempdir() as tmp:
            with self.assertRaises(RuntimeError):
                render_long_form.build_caption_pngs_from_chunks(
                    "One. Two.", [tmp / "c0.wav"], 0.1, tmp / "caps", chunk_target_chars=5
                )

    def test_build_caption_pngs_from_chunks_normal(self):
        with local_tempdir() as tmp:
            wavs = [write_wav(tmp / "c0.wav")]

            def run_jobs(jobs, **_kw):
                for job in jobs:
                    job()

            with patch.object(render_long_form, "_probe_duration", return_value=4.0), \
                 patch("pipeline.parallel.run_parallel", side_effect=run_jobs):
                cues = render_long_form.build_caption_pngs_from_chunks(
                    "First sentence. Second sentence.", wavs, 0.2, tmp / "caps",
                    max_chars_per_line=5, max_lines_per_cue=1
                )
            self.assertEqual(len(cues), 4)
            self.assertTrue(all(p.exists() for p, _s, _e in cues))

    def test_build_caption_pngs_from_chunks_empty_sentence_chunk_skips(self):
        with local_tempdir() as tmp:
            with patch.object(render_long_form, "_split_into_chunks", return_value=[""]), \
                 patch.object(render_long_form, "_probe_duration", return_value=1.0):
                cues = render_long_form.build_caption_pngs_from_chunks(
                    "ignored", [tmp / "c0.wav"], 0.1, tmp / "caps"
                )
        self.assertEqual(cues, [])

    def test_build_caption_pngs_from_chunks_truetype_exception_fallback(self):
        default_font = ImageFont.load_default()
        with local_tempdir() as tmp, \
             patch.object(render_long_form, "_probe_duration", return_value=1.0), \
             patch.object(render_long_form.Path, "exists", return_value=True), \
             patch("PIL.ImageFont.truetype", side_effect=OSError("bad font")), \
             patch("PIL.ImageFont.load_default", return_value=default_font):
            cues = render_long_form.build_caption_pngs_from_chunks(
                "One sentence.", [tmp / "c0.wav"], 0.1, tmp / "caps"
            )
        self.assertEqual(len(cues), 1)

    def test_render_watermark_normal_and_font_fallback(self):
        with local_tempdir() as tmp:
            a = render_long_form.render_watermark_png("CHANNEL", tmp / "a.png", italic=True)
            with patch.object(render_long_form.Path, "exists", return_value=False):
                b = render_long_form.render_watermark_png("CHANNEL", tmp / "b.png")
            default_font = ImageFont.load_default()
            with patch.object(render_long_form.Path, "exists", return_value=True), \
                 patch("PIL.ImageFont.truetype", side_effect=OSError("bad font")), \
                 patch("PIL.ImageFont.load_default", return_value=default_font):
                c = render_long_form.render_watermark_png("CHANNEL", tmp / "c.png")
            self.assertGreater(a.stat().st_size, 0)
            self.assertGreater(b.stat().st_size, 0)
            self.assertGreater(c.stat().st_size, 0)

    def test_render_caption_png_one_and_two_lines(self):
        with local_tempdir() as tmp:
            font = ImageFont.load_default()
            render_long_form._render_caption_png(["one line"], tmp / "one.png", 320, font)
            render_long_form._render_caption_png(
                ["line one", "line two"], tmp / "two.png", 320, font
            )
            self.assertGreater((tmp / "one.png").stat().st_size, 0)
            self.assertGreater((tmp / "two.png").stat().st_size, 0)

    def test_build_captions_ass_authored_empty_sentence_chunk(self):
        with local_tempdir() as tmp, \
             patch.object(render_long_form, "_split_into_chunks", return_value=[""]), \
             patch.object(render_long_form, "_probe_duration", return_value=1.0):
            _path, count = render_long_form.build_captions_ass(
                tmp / "out.ass", narration_text="ignored", chunk_wavs=[tmp / "c.wav"]
            )
        self.assertEqual(count, 0)

    def test_build_captions_ass_wraps_long_cue_into_multiple_events(self):
        with local_tempdir() as tmp, \
             patch.object(render_long_form, "_probe_duration", return_value=2.0):
            _path, count = render_long_form.build_captions_ass(
                tmp / "out.ass",
                narration_text="First sentence.",
                chunk_wavs=[tmp / "c.wav"],
                max_chars=5, max_lines=1
            )
            self.assertGreater(count, 1)
            self.assertIn("First", (tmp / "out.ass").read_text())

    def test_build_captions_ass_whisper_empty_words_raises(self):
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=[]):
            with self.assertRaises(RuntimeError):
                render_long_form.build_captions_ass(
                    tmp / "out.ass", narration_wav=tmp / "n.wav"
                )

    def test_build_captions_ass_whisper_path_groups_by_punctuation(self):
        words = [
            FakeWord("Hello", 0, 0.2), FakeWord("world.", 0.2, 0.5),
            FakeWord("How", 0.6, 0.7), FakeWord("are", 0.7, 0.8),
            FakeWord("you?", 0.8, 1.0),
            FakeWord("trailing", 1.1, 1.2),
        ]
        with local_tempdir() as tmp, \
             patch("pipeline.beats.transcribe_words", return_value=words):
            _, count = render_long_form.build_captions_ass(
                tmp / "out.ass", narration_wav=tmp / "n.wav"
            )
            self.assertEqual(count, 3)  # "Hello world.", "How are you?", "trailing"
            body = (tmp / "out.ass").read_text()
            self.assertIn("Hello", body)

    def test_build_captions_ass_no_source_raises(self):
        with local_tempdir() as tmp:
            with self.assertRaises(ValueError):
                render_long_form.build_captions_ass(tmp / "out.ass")


# ---------------------------------------------------------------------------
# final_mux, _ffmpeg_has_libass
# ---------------------------------------------------------------------------

class FinalMuxTests(unittest.TestCase):
    def _files(self, tmp: Path):
        v = write_big(tmp / "v.mp4")
        n = write_wav(tmp / "n.wav")
        m = write_wav(tmp / "m.wav")
        out = tmp / "out.mp4"
        return v, n, m, out

    def _mux_args(self, tmp: Path, **kwargs):
        v, n, m, out = self._files(tmp)
        with patch.object(render_long_form, "_ffmpeg") as ff:
            result = render_long_form.final_mux(v, n, m, out, **kwargs)
        self.assertEqual(result, out)
        return ff.call_args.args[0]

    def test_no_captions_no_overlays_simple_mux(self):
        with local_tempdir() as tmp:
            args = self._mux_args(tmp)
        self.assertIn("-c:v", args)
        self.assertIn("copy", args)
        self.assertIn("amix=inputs=2", " ".join(map(str, args)))

    def test_narration_leg_runs_through_loudnorm(self):
        # Regression — pre-fix, quiet TTS providers (Cloud Run
        # Chatterbox, Higgs Audio) produced renders the user reported
        # as "no audio" (-32 dB mean). Single-pass loudnorm at
        # I=-16 LUFS on the narration leg brings every TTS provider
        # to a consistent floor so the YAML's audio_narration_db
        # behaves predictably across providers.
        with local_tempdir() as tmp:
            args = self._mux_args(tmp)
        joined = " ".join(map(str, args))
        self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11", joined,
                      "narration leg must run through loudnorm before "
                      "the volume() trim — see 2026-05-12 audio post-mortem")
        # And it must apply to the narration input ([1:a]), not to
        # music ([2:a]) — auto-gaining the music bed is a known
        # antipattern that pumps up music volume in narration gaps.
        self.assertIn("[1:a]loudnorm=", joined)
        self.assertNotIn("[2:a]loudnorm=", joined)

    def test_watermark_only_overlay(self):
        with local_tempdir() as tmp:
            wm = write_big(tmp / "wm.png")
            args = self._mux_args(tmp, watermark_png=wm)
        self.assertIn("overlay=x=W-w-32:y=32", " ".join(map(str, args)))

    def test_watermark_custom_margin(self):
        with local_tempdir() as tmp:
            wm = write_big(tmp / "wm.png")
            args = self._mux_args(tmp, watermark_png=wm, watermark_margin=50)
        self.assertIn("x=W-w-50:y=50", " ".join(map(str, args)))

    def test_ass_subtitles_filter(self):
        with local_tempdir() as tmp:
            ass = write_big(tmp / "captions.ass")
            args = self._mux_args(tmp, captions_ass=ass)
        self.assertIn("subtitles=", " ".join(map(str, args)))

    def test_legacy_png_caption_overlay_chain(self):
        with local_tempdir() as tmp:
            png = write_big(tmp / "cap.png")
            args = self._mux_args(tmp, caption_cues=[(png, 0.0, 1.25)])
        joined = " ".join(map(str, args))
        self.assertIn("between(t,0.000,1.250)", joined)
        self.assertIn(str(png), joined)

    def test_both_captions_ass_and_cues_raises(self):
        with local_tempdir() as tmp:
            v, n, m, out = self._files(tmp)
            ass = write_big(tmp / "c.ass")
            png = write_big(tmp / "c.png")
            with self.assertRaises(ValueError):
                render_long_form.final_mux(
                    v, n, m, out,
                    captions_ass=ass,
                    caption_cues=[(png, 0.0, 1.0)]
                )

    def test_watermark_and_ass_combined(self):
        with local_tempdir() as tmp:
            wm = write_big(tmp / "wm.png")
            ass = write_big(tmp / "c.ass")
            args = self._mux_args(tmp, watermark_png=wm, captions_ass=ass)
        joined = " ".join(map(str, args))
        self.assertIn("[vwm]", joined)
        self.assertIn("[vass]", joined)

    def test_ffmpeg_has_libass_caches_result(self):
        # Clear any cached state
        if hasattr(render_long_form._ffmpeg_has_libass, "_cached"):
            del render_long_form._ffmpeg_has_libass._cached
        with patch("subprocess.check_output", return_value="Render text subtitles via libass"):
            result1 = render_long_form._ffmpeg_has_libass()
        result2 = render_long_form._ffmpeg_has_libass()
        self.assertIsInstance(result1, bool)
        self.assertEqual(result1, result2)
        # Clean up cache
        if hasattr(render_long_form._ffmpeg_has_libass, "_cached"):
            del render_long_form._ffmpeg_has_libass._cached

    def test_ffmpeg_has_libass_false_on_error(self):
        if hasattr(render_long_form._ffmpeg_has_libass, "_cached"):
            del render_long_form._ffmpeg_has_libass._cached
        with patch("subprocess.check_output", side_effect=FileNotFoundError("no ffmpeg")):
            result = render_long_form._ffmpeg_has_libass()
        self.assertFalse(result)
        if hasattr(render_long_form._ffmpeg_has_libass, "_cached"):
            del render_long_form._ffmpeg_has_libass._cached


# ---------------------------------------------------------------------------
# main() — comprehensive branch coverage
# ---------------------------------------------------------------------------

class MainBase(unittest.TestCase):
    """Base class providing _setup_channel and _run_main helpers."""

    def _setup_channel(
        self,
        tmp: Path,
        *,
        tts_provider: str = "cloudrun_f5",
        render_mode: str = "archival_footage",
        captions_enabled: bool = False,
        extra_lf: dict | None = None,
        narration: str | None = None,
        sections: list[dict] | None = None,
        panels: list[dict] | None = None,
        has_shotlist: bool = True,
        has_narration: bool = True,
        config_override: dict | None = None,
    ) -> tuple[Path, str]:
        slug = "test-slug"
        ch = "fakechan"
        channel_dir = tmp / ch
        for sub in [
            "narrations", "shotlist", "shorts", "music", "branding",
            f"cache/{slug}",
        ]:
            (channel_dir / sub).mkdir(parents=True, exist_ok=True)

        lf: dict = {
            "tts_provider": tts_provider,
            "tts_voice": "pipeline/voice_refs/sarah.wav",
            "tts_ref_text": "ref text",
            "tts_speed": 0.95,
            "tts_post_atempo": 1.0,
            "render_mode": render_mode,
            "captions_enabled": captions_enabled,
            "watermark": {"enabled": False},
        }
        if extra_lf:
            lf.update(extra_lf)

        if config_override is None:
            cfg = {"name": "Fake Channel", "long_form": lf}
        else:
            cfg = config_override

        # Config must live at <channel_dir>/config.yaml — this is where
        # RenderPaths.from_channel_dir(ch, project_root=tmp) resolves it.
        (channel_dir / "config.yaml").write_text(yaml.safe_dump(cfg))

        if has_narration:
            script: dict = {}
            if narration is not None:
                script["narration"] = narration
            elif sections is not None:
                script["sections"] = sections
            else:
                script["narration"] = "First sentence. Second sentence."
            if panels is not None:
                script["panels"] = panels
            (channel_dir / "narrations" / f"{slug}.json").write_text(json.dumps(script))

        if has_shotlist:
            shotlist = {"clips": [{"source": "test.mp4", "in_s": 0.0, "out_s": 10.0}]}
            (channel_dir / "shotlist" / f"{slug}.json").write_text(json.dumps(shotlist))

        return channel_dir, slug

    def _run_main(
        self,
        tmp: Path,
        channel_dir: Path,
        slug: str,
        extra_argv: list[str] | None = None,
        duration: float = 600.0,
        **overrides,
    ):
        fake_wav = channel_dir / "cache" / slug / "narration.wav"
        write_wav(fake_wav)
        fake_video = channel_dir / "cache" / slug / "video.mp4"
        write_big(fake_video)
        fake_music = channel_dir / "cache" / slug / "music_bed.wav"
        write_wav(fake_music)
        # Audit T1.14 — long-form output now lands in <channel>/long_form/,
        # not <channel>/shorts/.
        out_mp4 = channel_dir / "long_form" / f"{slug}.mp4"
        write_big(out_mp4)
        caption_png = channel_dir / "cache" / slug / "captions" / "x.png"
        write_big(caption_png)

        argv = ["render_long_form", "--channel", channel_dir.name, "--slug", slug]
        if extra_argv:
            argv += extra_argv

        mocks: dict = {
            "preflight": MagicMock(),
            "load_env": MagicMock(),
            "reset_cloud": MagicMock(),
            "synth_long_narration": MagicMock(return_value=(fake_wav, [fake_wav])),
            "probe_duration": MagicMock(return_value=duration),
            "_probe_duration": MagicMock(return_value=duration),
            "reset_mlx_state": MagicMock(),
            "build_video_track": MagicMock(return_value=fake_video),
            "build_image_panels_video": MagicMock(return_value=fake_video),
            "build_music_bed": MagicMock(return_value=fake_music),
            "final_mux": MagicMock(return_value=out_mp4),
            "build_captions_ass": MagicMock(
                return_value=(
                    channel_dir / "cache" / slug / "captions" / "captions.ass",
                    5,
                )
            ),
            "build_caption_pngs_from_chunks": MagicMock(
                return_value=[(caption_png, 0.0, 1.0)]
            ),
            "build_caption_pngs": MagicMock(return_value=[(caption_png, 0.0, 1.0)]),
            "render_watermark_png": MagicMock(
                side_effect=lambda text, path, **kw: write_big(Path(path))
            ),
            "ffmpeg_has_libass": MagicMock(return_value=True),
            "ffmpeg": MagicMock(),
            "check_output": MagicMock(return_value=b"600.0\n"),
        }
        mocks.update(overrides)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(render_long_form, "REPO_ROOT", tmp))
            # Existing test contract: the wrappers (build_video_track /
            # build_image_panels_video) are mocked, so the parallel
            # overlap path (which calls _trim_shotlist_clips /
            # _generate_panel_stills directly, bypassing the wrappers)
            # would skip every mock and explode on the first missing
            # source file. Disable overlap globally for the legacy
            # MainBase fixture; new dedicated tests in
            # tests/test_render_long_form_parallel.py exercise the
            # overlap path with the new granular mock surface.
            stack.enter_context(
                patch.dict(os.environ, {"YTFACTORY_DISABLE_STAGE_OVERLAP": "1"})
            )
            stack.enter_context(
                patch.object(render_long_form, "_preflight_power_check", mocks["preflight"])
            )
            stack.enter_context(
                patch.object(render_long_form, "_load_env", mocks["load_env"])
            )
            stack.enter_context(
                patch("pipeline.images.images_cloudrun.reset_circuit_breaker", mocks["reset_cloud"])
            )
            stack.enter_context(
                patch.object(
                    render_long_form, "synth_long_narration", mocks["synth_long_narration"]
                )
            )
            stack.enter_context(
                patch("pipeline.probe.probe_duration", mocks["probe_duration"])
            )
            stack.enter_context(
                patch.object(render_long_form, "_probe_duration", mocks["_probe_duration"])
            )
            stack.enter_context(
                patch("pipeline.preflight.reset_mlx_state", mocks["reset_mlx_state"])
            )
            stack.enter_context(
                patch.object(render_long_form, "build_video_track", mocks["build_video_track"])
            )
            stack.enter_context(
                patch.object(
                    render_long_form, "build_image_panels_video", mocks["build_image_panels_video"]
                )
            )
            stack.enter_context(
                patch.object(render_long_form, "build_music_bed", mocks["build_music_bed"])
            )
            stack.enter_context(
                patch.object(render_long_form, "final_mux", mocks["final_mux"])
            )
            stack.enter_context(
                patch.object(render_long_form, "build_captions_ass", mocks["build_captions_ass"])
            )
            stack.enter_context(
                patch.object(
                    render_long_form,
                    "build_caption_pngs_from_chunks",
                    mocks["build_caption_pngs_from_chunks"],
                )
            )
            stack.enter_context(
                patch.object(render_long_form, "build_caption_pngs", mocks["build_caption_pngs"])
            )
            stack.enter_context(
                patch.object(
                    render_long_form, "render_watermark_png", mocks["render_watermark_png"]
                )
            )
            stack.enter_context(
                patch.object(
                    render_long_form, "_ffmpeg_has_libass", mocks["ffmpeg_has_libass"]
                )
            )
            stack.enter_context(
                patch.object(render_long_form, "_ffmpeg", mocks["ffmpeg"])
            )
            stack.enter_context(
                patch("subprocess.check_output", mocks["check_output"])
            )
            result = render_long_form.main()
        return result, mocks


class MainFunctionTests(MainBase):
    def test_missing_narration_exits(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, has_narration=False)
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("missing narration", str(cm.exception))

    def test_no_long_form_block_exits(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, config_override={"name": "Fake"}
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("no `long_form:`", str(cm.exception))

    def test_unsupported_provider_exits(self):
        """kokoro is not supported by the historyrecapped long-form renderer."""
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, tts_provider="kokoro")
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("not supported", str(cm.exception))

    def test_empty_text_exits(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, narration="")
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("neither 'narration'", str(cm.exception))

    def test_tts_only_returns_before_video(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp)
            result, mocks = self._run_main(
                tmp, channel_dir, slug, extra_argv=["--tts-only"]
            )
        self.assertEqual(result, 0)
        mocks["build_video_track"].assert_not_called()
        mocks["reset_mlx_state"].assert_not_called()

    def test_sections_based_narration_joined(self):
        """Sections are joined with \\n\\n when no 'narration' key."""
        sections = [{"text": "Part one."}, {"text": "Part two."}]
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, sections=sections)
            _result, mocks = self._run_main(
                tmp, channel_dir, slug, extra_argv=["--tts-only"]
            )
        text = mocks["synth_long_narration"].call_args.kwargs["text"]
        self.assertIn("Part one.", text)
        self.assertIn("Part two.", text)

    def test_cloudrun_f5_provider_calls_synth(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, tts_provider="cloudrun_f5")
            _result, mocks = self._run_main(
                tmp, channel_dir, slug, extra_argv=["--tts-only"]
            )
        mocks["synth_long_narration"].assert_called_once()
        # Provider must be forwarded into synth_long_narration — pre-2026-05-10
        # the renderer parsed the YAML provider, validated it, then dropped
        # it on the floor (synth_long_narration kept its f5_tts default).
        self.assertEqual(
            mocks["synth_long_narration"].call_args.kwargs.get("provider"),
            "cloudrun_f5",
        )

    def test_cloudrun_chatterbox_provider_calls_synth(self):
        """2026-05-10: cosmosdecoded + historyrecapped long-form flipped
        from cloudrun_f5 → cloudrun_chatterbox because the f5 cloud
        service was never deployed to ytfactory-prod-v2. The renderer
        must accept the new provider AND forward it to
        synth_long_narration so the cloud path actually fires."""
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, tts_provider="cloudrun_chatterbox"
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug, extra_argv=["--tts-only"]
            )
        mocks["synth_long_narration"].assert_called_once()
        self.assertEqual(
            mocks["synth_long_narration"].call_args.kwargs.get("provider"),
            "cloudrun_chatterbox",
        )

    def test_f5_tts_provider_calls_synth(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, tts_provider="f5_tts")
            _result, mocks = self._run_main(
                tmp, channel_dir, slug, extra_argv=["--tts-only"]
            )
        mocks["synth_long_narration"].assert_called_once()
        self.assertEqual(
            mocks["synth_long_narration"].call_args.kwargs.get("provider"),
            "f5_tts",
        )

    def test_reset_mlx_called_before_video(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp)
            result, mocks = self._run_main(tmp, channel_dir, slug)
        self.assertEqual(result, 0)
        mocks["reset_mlx_state"].assert_called_once()

    def test_output_lands_in_long_form_dir_not_shorts(self):
        # Audit T1.14 — long-form mp4 must write to <channel>/long_form/,
        # NOT <channel>/shorts/. Pre-fix the renderer wrote into the
        # shorts dir which broke the canonical paths.long_form_for(slug)
        # lookup downstream + confused operators inspecting the channel
        # artifact tree.
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp)
            result, mocks = self._run_main(tmp, channel_dir, slug)
        self.assertEqual(result, 0)
        # final_mux is the last stage of the renderer; its `out_path`
        # kwarg pins the actual mp4 location.
        out_path = mocks["final_mux"].call_args.kwargs.get("out_path")
        if out_path is None:  # try positional
            out_path = mocks["final_mux"].call_args.args[-1]
        self.assertIn("long_form", str(out_path))
        self.assertNotIn(
            "shorts/", str(out_path),
            "long-form output must NOT land in shorts/ — pre-fix this "
            "regressed paths.long_form_for(slug) lookup downstream",
        )

    def test_archival_footage_calls_build_video_track(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, render_mode="archival_footage")
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["build_video_track"].assert_called_once()
        mocks["build_image_panels_video"].assert_not_called()

    def test_archival_footage_with_grade_filter(self):
        with local_tempdir() as tmp:
            lf = {"visual_grade": {"enabled": True, "filter": "eq=saturation=1.1"}}
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="archival_footage", extra_lf=lf
            )
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        self.assertEqual(
            mocks["build_video_track"].call_args.kwargs["grade_filter"],
            "eq=saturation=1.1",
        )

    def test_no_grade_flag_clears_grade_filter(self):
        with local_tempdir() as tmp:
            lf = {"visual_grade": {"enabled": True, "filter": "eq=saturation=1.1"}}
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="archival_footage", extra_lf=lf
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug, extra_argv=["--no-grade"]
            )
        self.assertIsNone(mocks["build_video_track"].call_args.kwargs["grade_filter"])

    def test_archival_footage_missing_shotlist_exits(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="archival_footage", has_shotlist=False
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("missing shotlist", str(cm.exception))

    def test_image_panels_mode_calls_build_image_panels_video(self):
        panels = [{"scene": "a temple at night", "hold_s": 20}]
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="image_panels", panels=panels
            )
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["build_image_panels_video"].assert_called_once()
        mocks["build_video_track"].assert_not_called()

    def test_image_panels_no_panels_exits(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="image_panels", panels=None
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("image_panels", str(cm.exception))

    def test_image_panels_too_many_panels_exits(self):
        panels = [{"scene": f"s{i}", "hold_s": 20} for i in range(25)]
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="image_panels", panels=panels
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main(tmp, channel_dir, slug)
        self.assertIn("PANEL_HARD_CAP=24", str(cm.exception))

    def test_image_panels_auto_pad_when_short(self):
        """Total panel duration < narration → hold_s extended."""
        panels = [{"scene": "a scene", "hold_s": 10}]  # total=10 < dur=600
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="image_panels", panels=panels
            )
            _result, mocks = self._run_main(tmp, channel_dir, slug, duration=600.0)
        call_panels = mocks["build_image_panels_video"].call_args.kwargs["panels"]
        self.assertGreater(call_panels[0]["hold_s"], 10)

    def test_image_panels_scale_down_when_long(self):
        """Total panel duration > narration + 1 → hold_s scaled down."""
        panels = [{"scene": "a scene", "hold_s": 1000}]  # total=1000 > dur=60+1
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="image_panels", panels=panels
            )
            _result, mocks = self._run_main(tmp, channel_dir, slug, duration=60.0)
        call_panels = mocks["build_image_panels_video"].call_args.kwargs["panels"]
        self.assertLess(call_panels[0]["hold_s"], 1000)

    def test_music_bed_curated_track_loops(self):
        """If curated track exists in music/, _ffmpeg loops it."""
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp)
            write_big(channel_dir / "music" / "aether-loop.wav")
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["build_music_bed"].assert_not_called()
        mocks["ffmpeg"].assert_called()

    def test_music_bed_synthetic_when_missing(self):
        """No curated track → build_music_bed synthesizes placeholder."""
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp)
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["build_music_bed"].assert_called_once()

    def test_captions_disabled_skips_captioning(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp, captions_enabled=False)
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["build_captions_ass"].assert_not_called()
        mocks["build_caption_pngs"].assert_not_called()

    def test_captions_ass_authored_mode(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, captions_enabled=True,
                extra_lf={"caption_align": "authored"},
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug,
                ffmpeg_has_libass=MagicMock(return_value=True),
            )
        mocks["build_captions_ass"].assert_called_once()
        call_kwargs = mocks["build_captions_ass"].call_args.kwargs
        self.assertIn("narration_text", call_kwargs)
        self.assertNotIn("narration_wav", call_kwargs)

    def test_captions_ass_whisper_mode(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, captions_enabled=True,
                extra_lf={"caption_align": "whisper"},
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug,
                ffmpeg_has_libass=MagicMock(return_value=True),
            )
        call_kwargs = mocks["build_captions_ass"].call_args.kwargs
        self.assertIn("narration_wav", call_kwargs)
        self.assertNotIn("narration_text", call_kwargs)

    def test_captions_png_authored_mode_no_libass(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, captions_enabled=True,
                extra_lf={"caption_align": "authored"},
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug,
                ffmpeg_has_libass=MagicMock(return_value=False),
            )
        mocks["build_caption_pngs_from_chunks"].assert_called_once()
        mocks["build_captions_ass"].assert_not_called()

    def test_captions_png_authored_cleans_old_cap_files(self):
        """Line 1834: old cap_*.png files in cap_dir are deleted before regen."""
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, captions_enabled=True,
                extra_lf={"caption_align": "authored"},
            )
            # Pre-create a stale cap file that should be cleaned up.
            cap_dir = channel_dir / "cache" / slug / "captions"
            cap_dir.mkdir(parents=True, exist_ok=True)
            stale = cap_dir / "cap_old.png"
            write_big(stale)
            self.assertTrue(stale.exists())
            _result, mocks = self._run_main(
                tmp, channel_dir, slug,
                ffmpeg_has_libass=MagicMock(return_value=False),
            )
        # The stale file should have been unlinked before build_caption_pngs_from_chunks ran.
        self.assertFalse(stale.exists())
        mocks["build_caption_pngs_from_chunks"].assert_called_once()

    def test_captions_png_whisper_mode_no_libass(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, captions_enabled=True,
                extra_lf={"caption_align": "whisper"},
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug,
                ffmpeg_has_libass=MagicMock(return_value=False),
            )
        mocks["build_caption_pngs"].assert_called_once()
        mocks["build_caption_pngs_from_chunks"].assert_not_called()

    def test_watermark_enabled_missing_file_renders_it(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, extra_lf={"watermark": {"enabled": True, "text": "HISTORY"}}
            )
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["render_watermark_png"].assert_called_once()
        wm_call = mocks["render_watermark_png"].call_args
        self.assertEqual(wm_call.args[0], "HISTORY")

    def test_watermark_enabled_file_exists_skips_render(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, extra_lf={"watermark": {"enabled": True}}
            )
            write_big(channel_dir / "branding" / "watermark_topright.png")
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["render_watermark_png"].assert_not_called()

    def test_watermark_disabled_skips_render(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, extra_lf={"watermark": {"enabled": False}}
            )
            _result, mocks = self._run_main(tmp, channel_dir, slug)
        mocks["render_watermark_png"].assert_not_called()

    def test_final_mux_called_with_all_args(self):
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(tmp)
            result, mocks = self._run_main(tmp, channel_dir, slug)
        self.assertEqual(result, 0)
        mocks["final_mux"].assert_called_once()

    def test_render_mode_cli_override(self):
        """--render-mode=image_panels overrides config render_mode."""
        panels = [{"scene": "a scene", "hold_s": 20}]
        with local_tempdir() as tmp:
            channel_dir, slug = self._setup_channel(
                tmp, render_mode="archival_footage", panels=panels
            )
            _result, mocks = self._run_main(
                tmp, channel_dir, slug,
                extra_argv=["--render-mode", "image_panels"],
            )
        mocks["build_image_panels_video"].assert_called_once()


class CliMainTests(unittest.TestCase):
    def test_cli_main_delegates_to_main(self):
        with patch.object(render_long_form, "main", return_value=42) as m:
            result = render_long_form.cli_main()
        self.assertEqual(result, 42)
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
