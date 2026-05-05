"""Tests for the engineering-level guard rails in
``scripts/historyrecapped/render_long_form.py``.

What this guards:
- TTS provider guard: long-form must hard-reject any non-F5 provider.
  A silent fall-through here would re-introduce the Cartesia / Kokoro
  paths the user explicitly retired (see
  ``docs/long_form_model_inventory.md``).
- F5-TTS missing-ref-text precondition: the renderer raises before
  loading the 1.35 GB checkpoint, so a missing config doesn't burn 30s
  on a model load that's about to fail anyway.
- image_panels panel-count cap: pure-image timelines beyond the
  configured cap (default 24) cause Metal command-buffer timeouts.
  The renderer must `SystemExit` *before* z_image_turbo cold-loads.
- ``_split_into_chunks`` invariants: the captioner relies on this
  function producing the same chunks the TTS path used. A change in
  splitting behaviour would silently desync caption timing from audio.

All tests are in-process and avoid loading any ML model (F5, whisper,
z_image_turbo). The two tests that exercise ``main()`` mock
``synth_long_narration`` and ``_probe_duration`` so no real TTS or
ffprobe runs.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests._helpers import PROJECT_ROOT  # noqa: F401


# Import the script as a module. It lives under scripts/historyrecapped/
# which is not on sys.path by default, so we add it.
_RENDER_SCRIPTS_DIR = PROJECT_ROOT / "scripts" / "historyrecapped"
if str(_RENDER_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_RENDER_SCRIPTS_DIR))
render_long_form = importlib.import_module("render_long_form")


# ---------- _split_into_chunks --------------------------------------------


class SplitIntoChunksTests(unittest.TestCase):
    """The captioner aligns authored sentences against TTS chunk durations,
    so the chunk boundaries it computes MUST match what the TTS pass produced.
    Both call sites use ``_split_into_chunks`` with the same target. These
    tests pin the invariants so a future change can't silently desync them.
    """

    def test_packs_short_sentences_into_one_chunk(self):
        text = "First. Second. Third."
        chunks = render_long_form._split_into_chunks(text, target_chars=200)
        self.assertEqual(chunks, ["First. Second. Third."])

    def test_breaks_at_sentence_boundary_not_mid_word(self):
        text = "Sentence one is here. Sentence two follows. Sentence three closes."
        # Set target to force a break after the first sentence.
        chunks = render_long_form._split_into_chunks(text, target_chars=25)
        self.assertEqual(len(chunks), 3)
        for ch in chunks:
            self.assertTrue(ch.endswith("."), f"chunk does not end at sentence: {ch!r}")

    def test_paragraph_break_starts_fresh_chunk(self):
        # Empty-line paragraph break should always start a new chunk even
        # when the next sentence would otherwise pack with the previous.
        text = "Para A.\n\nPara B."
        chunks = render_long_form._split_into_chunks(text, target_chars=500)
        self.assertEqual(chunks, ["Para A.", "Para B."])

    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(render_long_form._split_into_chunks("", target_chars=380), [])

    def test_idempotent_on_repeated_call(self):
        # Same text + same target → same chunks. Caption alignment depends
        # on this — TTS calls split, captioner calls split, results must match.
        text = "One. Two. Three. Four. Five. Six."
        a = render_long_form._split_into_chunks(text, target_chars=15)
        b = render_long_form._split_into_chunks(text, target_chars=15)
        self.assertEqual(a, b)


# ---------- synth_long_narration ref-text precondition --------------------


class SynthLongNarrationPreconditionTests(unittest.TestCase):
    def test_raises_when_ref_audio_text_missing(self):
        # F5 ref-text is required up-front — without it the function would
        # silently load the 1.35 GB model just to crash later.
        with self.assertRaises(RuntimeError) as cm:
            render_long_form.synth_long_narration(
                text="hello world.",
                voice_id="pipeline/voice_refs/sarah.wav",
                cache_dir=Path("/tmp/never-created-test-dir"),
                atempo=1.0,
                ref_audio_text=None,
            )
        self.assertIn("tts_ref_text", str(cm.exception))

    def test_raises_when_ref_audio_text_empty_string(self):
        with self.assertRaises(RuntimeError):
            render_long_form.synth_long_narration(
                text="hello world.",
                voice_id="pipeline/voice_refs/sarah.wav",
                cache_dir=Path("/tmp/never-created-test-dir"),
                atempo=1.0,
                ref_audio_text="",
            )


# ---------- main() guard rails --------------------------------------------


def _write_minimal_long_form_setup(
    tmp: Path,
    *,
    tts_provider: str = "f5_tts",
    render_mode: str = "archival_footage",
    panel_max_count: int | None = None,
    panels: list | None = None,
    extra_lf: dict | None = None,
) -> tuple[Path, str]:
    """Create a minimal channel dir with config.yaml + narration JSON.

    Returns ``(channel_dir, slug)``. The narration text is short so even
    if a guard fails to fire and TTS does run (which it shouldn't), the
    test fails noisily rather than burning model-load time.
    """
    slug = "guard-test"
    channel_dir = tmp / "fakechan"
    (channel_dir / "narrations").mkdir(parents=True)
    (channel_dir / "shotlist").mkdir(parents=True)

    long_form: dict = {
        "tts_provider": tts_provider,
        "tts_voice": "pipeline/voice_refs/sarah.wav",
        "tts_ref_text": "ref text",
        "tts_speed": 0.95,
        "tts_post_atempo": 1.0,
        "render_mode": render_mode,
        "captions_enabled": False,
    }
    if panel_max_count is not None:
        long_form["panel_max_count"] = panel_max_count
    if extra_lf:
        long_form.update(extra_lf)

    cfg = {"long_form": long_form}
    (channel_dir / "config.yaml").write_text(yaml.safe_dump(cfg))

    narration: dict = {"narration": "Short. Test. Text."}
    if panels is not None:
        narration["panels"] = panels
    (channel_dir / "narrations" / f"{slug}.json").write_text(json.dumps(narration))

    return channel_dir, slug


class MainProviderGuardTests(unittest.TestCase):
    """The provider guard fires BEFORE any TTS runs, so no mocking needed."""

    def setUp(self):
        # See MainImagePanelsGuardTests.setUp for rationale.
        self._prev_skip = os.environ.get("YTFACTORY_SKIP_POWER_CHECK")
        os.environ["YTFACTORY_SKIP_POWER_CHECK"] = "1"

    def tearDown(self):
        if self._prev_skip is None:
            os.environ.pop("YTFACTORY_SKIP_POWER_CHECK", None)
        else:
            os.environ["YTFACTORY_SKIP_POWER_CHECK"] = self._prev_skip

    def _run_main_with_args(self, channel_dir: Path, slug: str):
        # render_long_form.main() reads --channel as a relative dir under
        # REPO_ROOT. Patch REPO_ROOT to the test tmp dir so config.yaml
        # resolves to our fake channel.
        argv = ["render_long_form", "--channel", channel_dir.name, "--slug", slug]
        with patch.object(render_long_form, "REPO_ROOT", channel_dir.parent), \
             patch.object(sys, "argv", argv):
            render_long_form.main()

    def test_kokoro_provider_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, tts_provider="kokoro",
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main_with_args(channel_dir, slug)
            self.assertIn("kokoro", str(cm.exception))
            self.assertIn("F5", str(cm.exception))

    def test_cartesia_provider_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, tts_provider="cartesia",
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main_with_args(channel_dir, slug)
            self.assertIn("cartesia", str(cm.exception))

    def test_unknown_provider_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, tts_provider="elevenlabs",
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main_with_args(channel_dir, slug)
            self.assertIn("elevenlabs", str(cm.exception))


class MainImagePanelsGuardTests(unittest.TestCase):
    """image_panels guards fire AFTER the TTS stage, so we mock
    ``synth_long_narration`` + ``_probe_duration`` so no real audio is
    synthesised. The whole point is to exercise the cap *without* loading
    F5 or ffprobing a fake wav.
    """

    # Sentinel so the "under cap" tests can prove the guard PASSED them
    # through to image gen without actually firing z_image_turbo (which
    # would cold-load the diffusion model and likely crash Metal).
    _SENTINEL = RuntimeError("image-gen-reached")

    def setUp(self):
        # The renderer's preflight power check shells out to `pmset` with
        # text=True, but our subprocess.check_output mocks return bytes.
        # We're not testing the preflight here — bypass it via env var.
        self._prev_skip = os.environ.get("YTFACTORY_SKIP_POWER_CHECK")
        os.environ["YTFACTORY_SKIP_POWER_CHECK"] = "1"

    def tearDown(self):
        if self._prev_skip is None:
            os.environ.pop("YTFACTORY_SKIP_POWER_CHECK", None)
        else:
            os.environ["YTFACTORY_SKIP_POWER_CHECK"] = self._prev_skip

    def _run_main_with_mocks(self, channel_dir: Path, slug: str,
                             *, image_gen_sentinel: bool = False):
        # Provide a fake narration.wav path; it never gets read by the test
        # because the panel guard runs before any code touches the file.
        fake_wav = channel_dir / "cache" / slug / "narration.wav"
        fake_wav.parent.mkdir(parents=True, exist_ok=True)
        fake_wav.write_bytes(b"")

        # When the cap should pass, mock build_image_panels_video so it
        # raises our sentinel instead of cold-loading z_image_turbo. The
        # test asserts the sentinel propagated, proving the guard didn't
        # block this run.
        side_effect = self._SENTINEL if image_gen_sentinel else None

        argv = ["render_long_form", "--channel", channel_dir.name, "--slug", slug]
        with patch.object(render_long_form, "REPO_ROOT", channel_dir.parent), \
             patch.object(sys, "argv", argv), \
             patch.object(render_long_form, "synth_long_narration",
                          return_value=(fake_wav, [fake_wav])), \
             patch.object(render_long_form, "_probe_duration",
                          return_value=600.0), \
             patch.object(render_long_form, "build_image_panels_video",
                          side_effect=side_effect), \
             patch("subprocess.check_output",
                   return_value=b"600.0\n"):
            render_long_form.main()

    def test_image_panels_with_no_panels_field(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, render_mode="image_panels", panels=None,
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main_with_mocks(channel_dir, slug)
            msg = str(cm.exception)
            self.assertIn("image_panels", msg)
            self.assertIn("panels", msg)

    def test_image_panels_over_default_cap(self):
        # Default cap is 24; build 25 dummy panels to trip it.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            panels = [{"scene": f"s{i}", "hold_s": 20} for i in range(25)]
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, render_mode="image_panels", panels=panels,
            )
            with self.assertRaises(SystemExit) as cm:
                self._run_main_with_mocks(channel_dir, slug)
            msg = str(cm.exception)
            self.assertIn("25 panels", msg)
            self.assertIn("PANEL_HARD_CAP=24", msg)

    def test_image_panels_under_cap_passes_guard(self):
        # 5 panels is well under the cap. The guard must NOT raise; we
        # prove this by mocking build_image_panels_video to a sentinel
        # exception and asserting THAT propagated (not the cap message).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            panels = [{"scene": f"s{i}", "hold_s": 20} for i in range(5)]
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, render_mode="image_panels", panels=panels,
            )
            with self.assertRaises(RuntimeError) as cm:
                self._run_main_with_mocks(
                    channel_dir, slug, image_gen_sentinel=True,
                )
            # The sentinel — proves we got past the cap into image gen.
            self.assertEqual(str(cm.exception), "image-gen-reached")

    def test_panel_max_count_override_lifts_cap(self):
        # If the channel YAML raises panel_max_count, the cap moves with it.
        # 40 panels with cap=50 should pass; we again use the sentinel to
        # prove the guard didn't block.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            panels = [{"scene": f"s{i}", "hold_s": 20} for i in range(40)]
            channel_dir, slug = _write_minimal_long_form_setup(
                tmp, render_mode="image_panels",
                panels=panels, panel_max_count=50,
            )
            with self.assertRaises(RuntimeError) as cm:
                self._run_main_with_mocks(
                    channel_dir, slug, image_gen_sentinel=True,
                )
            self.assertEqual(str(cm.exception), "image-gen-reached")


class PreflightPowerCheckTests(unittest.TestCase):
    """Guards against the macOS WindowServer watchdog crash class
    (incident F743A4C5, 2026-05-04). When Low Power Mode is on, the
    GPU is clocked down and long Metal command buffers stretch past
    the 40s WindowServer checkin window — the kernel kills WindowServer
    and the system logs out / panics. The preflight refuses to start
    in that state."""

    def setUp(self):
        self._prev_skip = os.environ.get("YTFACTORY_SKIP_POWER_CHECK")
        os.environ.pop("YTFACTORY_SKIP_POWER_CHECK", None)

    def tearDown(self):
        if self._prev_skip is None:
            os.environ.pop("YTFACTORY_SKIP_POWER_CHECK", None)
        else:
            os.environ["YTFACTORY_SKIP_POWER_CHECK"] = self._prev_skip

    def test_low_power_mode_is_rejected(self):
        # Simulate `pmset -g` reporting Low Power Mode on.
        fake_pmset = (
            "Currently drawing from 'AC Power'\n"
            " sleep                   10800     \n"
            " lowpowermode            1\n"
        )
        with patch("subprocess.check_output", return_value=fake_pmset), \
             patch.object(sys, "platform", "darwin"):
            with self.assertRaises(SystemExit) as cm:
                render_long_form._preflight_power_check()
        self.assertIn("Low Power Mode", str(cm.exception))

    def test_normal_power_state_passes(self):
        fake_pmset = (
            "Currently drawing from 'AC Power'\n"
            " lowpowermode            0\n"
        )
        with patch("subprocess.check_output", return_value=fake_pmset), \
             patch.object(sys, "platform", "darwin"):
            # Should not raise.
            render_long_form._preflight_power_check()

    def test_skip_env_var_bypasses_check(self):
        # If user set the override, the function returns immediately even
        # if Low Power Mode is on.
        os.environ["YTFACTORY_SKIP_POWER_CHECK"] = "1"
        fake_pmset = " lowpowermode            1\n"
        with patch("subprocess.check_output", return_value=fake_pmset), \
             patch.object(sys, "platform", "darwin"):
            render_long_form._preflight_power_check()  # no raise

    def test_pmset_unavailable_does_not_block(self):
        # On a system without pmset (or where it errors), preflight
        # should fail open, not block the render.
        with patch("subprocess.check_output",
                   side_effect=FileNotFoundError("pmset")), \
             patch.object(sys, "platform", "darwin"):
            render_long_form._preflight_power_check()  # no raise

    def test_non_darwin_platform_skips_check(self):
        # On Linux / Windows we don't have pmset; skip the check entirely.
        with patch.object(sys, "platform", "linux"):
            render_long_form._preflight_power_check()  # no raise


if __name__ == "__main__":
    unittest.main()
