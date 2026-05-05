"""Parity tests for the pipeline.render package.

The 2026-05-05 refactor moved 4 renderer scripts from ``scripts/*`` to
``pipeline/render/*`` and replaced the originals with thin CLI shims.
These tests pin the public surface so an accidental break (e.g. a
renamed entry point or a missing re-export) surfaces in CI rather than
in a 5-minute-deep render.

What's NOT tested here: the actual render pipelines (those need torch
+ MLX + 11 GB of footage and live in ``test_audio_tts_providers``-style
``YTFACTORY_TTS_LIVE=1`` opt-in suites). What IS tested:

* All 4 renderer modules import cleanly without sys.path manipulation.
* Each exposes a callable ``cli_main`` (the entry point the matching
  ``scripts/*`` shim invokes).
* ``pipeline.render.shorts`` exposes ``make_short`` (programmatic API)
  and ``_resolve_channel_out_dir`` (per-channel layout resolver).
* ``pipeline.render.long_form`` exposes the helpers
  ``test_render_long_form_guards.py`` pins (so a refactor accidentally
  removing them is caught here too).
* The 4 ``scripts/*`` CLI shims are syntactically valid + their
  ``--help`` exits 0.
* The cloud worker's OUTPUT_MANIFEST parser picks up the shorts
  renderer's manifest line shape.

Skipped on the slim CI venv: ``pipeline.render.shorts`` transitively
pulls in torch / diffusers via ``pipeline.images``; ``pipeline.render.long_form``
+ ``footage_only`` + ``sports_doc`` pull in MLX / kokoro_onnx via
``pipeline.audio``. The OUTPUT_MANIFEST parser tests don't need
either, so they always run (CI included).
"""
from __future__ import annotations

import importlib.util as _u
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401


def _heavy_deps_available() -> bool:
    """Return True iff torch + kokoro_onnx + soundfile are importable.

    These are needed by ``pipeline.render.shorts`` (torch via diffusers)
    and ``pipeline.render.long_form`` / ``footage_only`` / ``sports_doc``
    (kokoro_onnx + soundfile via pipeline.audio). On the slim CI venv
    they aren't installed; the heavy-import tests self-skip.
    """
    return all(_u.find_spec(n) is not None for n in ("torch", "kokoro_onnx", "soundfile"))


_HEAVY = _heavy_deps_available()


@unittest.skipUnless(_HEAVY, "renderer modules need torch + kokoro_onnx (laptop venv only)")
class RendererImportsTest(unittest.TestCase):
    """All 4 renderer modules import + expose their CLI entry."""

    def test_pipeline_render_shorts_imports(self):
        from pipeline.render import shorts
        self.assertTrue(callable(shorts.cli_main))
        self.assertTrue(callable(shorts.make_short))
        self.assertTrue(callable(shorts._resolve_channel_out_dir))
        # Backward-compat alias preserved (older in-process callers).
        self.assertIs(shorts.main, shorts.cli_main)

    def test_pipeline_render_long_form_imports(self):
        from pipeline.render import long_form
        self.assertTrue(callable(long_form.cli_main))
        self.assertTrue(callable(long_form.main))
        # Helpers pinned by test_render_long_form_guards.
        self.assertTrue(callable(long_form._split_into_chunks))
        self.assertTrue(callable(long_form.synth_long_narration))

    def test_pipeline_render_footage_only_imports(self):
        from pipeline.render import footage_only
        self.assertTrue(callable(footage_only.cli_main))

    def test_pipeline_render_sports_doc_imports(self):
        from pipeline.render import sports_doc
        self.assertTrue(callable(sports_doc.cli_main))
        # Sports-doc reuses long-form's chunked-TTS + caption builder.
        # The import was rewritten from `from scripts.historyrecapped...`
        # to `from pipeline.render.long_form...` in the same refactor;
        # asserting the names are bound here catches a regression.
        for name in (
            "synth_long_narration", "build_caption_pngs_from_chunks",
            "render_watermark_png", "build_music_bed",
            "_ffmpeg", "_probe_duration", "_trim_clip_letterbox",
        ):
            self.assertTrue(
                hasattr(sports_doc, name),
                f"pipeline.render.sports_doc missing {name!r} "
                f"(should be re-imported from pipeline.render.long_form)",
            )


@unittest.skipUnless(_HEAVY, "shim subprocess loads torch (laptop venv only)")
class CliShimsTest(unittest.TestCase):
    """The 4 ``scripts/*`` thin shims still parse + run --help."""

    def _shim_help(self, shim: str) -> tuple[int, str]:
        # Run the shim with --help; should exit 0.
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / shim), "--help"],
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode, (proc.stdout + proc.stderr)

    def test_make_shorts_shim_help(self):
        rc, out = self._shim_help("scripts/make_shorts.py")
        self.assertEqual(rc, 0, f"make_shorts.py --help exited {rc}: {out[-300:]}")
        self.assertIn("--channel", out)
        self.assertIn("--script", out)

    def test_render_long_form_shim_help(self):
        rc, out = self._shim_help("historyrecapped/scripts/render_long_form.py")
        self.assertEqual(rc, 0, f"render_long_form.py --help exited {rc}: {out[-300:]}")

    def test_render_footage_only_shim_help(self):
        rc, out = self._shim_help("historyrecapped/scripts/render_footage_only.py")
        self.assertEqual(rc, 0, f"render_footage_only.py --help exited {rc}: {out[-300:]}")

    def test_render_long_form_doc_shim_help(self):
        rc, out = self._shim_help("sportstoriesanimated/scripts/render_long_form_doc.py")
        self.assertEqual(rc, 0, f"render_long_form_doc.py --help exited {rc}: {out[-300:]}")


# OUTPUT_MANIFEST parser + _find_outputs are pure-Python (no torch);
# they always run, including on slim CI.

class OutputManifestParseTest(unittest.TestCase):
    """The cloud worker parses ``OUTPUT_MANIFEST: {…}`` lines from the render log."""

    def test_parse_manifest_picks_last_line(self):
        from workers.heavy.render_short import _parse_output_manifest

        log = (
            "[some] earlier render log line\n"
            "[some] another line\n"
            f"OUTPUT_MANIFEST: {json.dumps({'mp4': '/x/y.mp4', 'thumb': None, 'slug': 's', 'channel_dir': 'historyrecapped'})}\n"
        )
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(log)
            p = Path(f.name)
        try:
            result = _parse_output_manifest(p)
            self.assertIsNotNone(result)
            self.assertEqual(result["mp4"], "/x/y.mp4")
            self.assertEqual(result["slug"], "s")
            self.assertEqual(result["channel_dir"], "historyrecapped")
        finally:
            p.unlink()

    def test_parse_manifest_returns_none_when_absent(self):
        from workers.heavy.render_short import _parse_output_manifest
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("regular log lines, no manifest line\n[done] /tmp/whatever.mp4\n")
            p = Path(f.name)
        try:
            self.assertIsNone(_parse_output_manifest(p))
        finally:
            p.unlink()

    def test_parse_manifest_returns_none_for_missing_file(self):
        from workers.heavy.render_short import _parse_output_manifest
        self.assertIsNone(_parse_output_manifest(Path("/does/not/exist.log")))


class FindOutputsTest(unittest.TestCase):
    """``_find_outputs`` prefers the manifest, falls back to per-channel scan."""

    def test_prefers_manifest_when_present(self):
        from workers.heavy.render_short import _find_outputs
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mp4 = Path(td) / "real.mp4"
            mp4.write_bytes(b"fake mp4")
            log = Path(td) / "render.log"
            log.write_text(
                f'OUTPUT_MANIFEST: {json.dumps({"mp4": str(mp4), "thumb": None, "slug": "s", "channel_dir": td})}\n'
            )
            mp4_p, thumb_p = _find_outputs("s", td, log_path=log)
            self.assertEqual(mp4_p, mp4)
            self.assertIsNone(thumb_p)


if __name__ == "__main__":
    unittest.main()
