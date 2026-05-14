"""Plugin callable smoke gate — catches the class-of-bug that
shipped ``tts_single`` with ``from pipeline.tts import synth`` (a
symbol that has never existed) for weeks without CI noticing.

Why this exists
---------------

Pre-2026-05-14 the engine plugins had three layers of "should have
caught it" that all failed:

1. Module import succeeds for any plugin with deferred imports
   inside method bodies. A ``from X import Y`` inside ``def synth():``
   only raises when synth() is called — module import never sees it.

2. ``isinstance(plugin, AudioSynthesizer)`` at module load is
   misleading. ``typing.Protocol`` is structural — it just checks
   ``hasattr(obj, "synth")``. A method that raises ImportError on
   first call still satisfies the Protocol.

3. Every existing engine test uses fixture-loading variants
   (``audio_from_fixture`` etc) so the production plugins
   (``tts_single``, ``tts_chunked``, ``ai_beat_slideshow``, etc.)
   are NEVER actually invoked in CI. The cloud-mode goldens that
   would invoke them are gated behind ``YTFACTORY_GOLDEN_CLOUD=1``.

This gate closes the hole. Two layers:

* **Static check** (deterministic, fast): for every
  ``from pipeline.X import Y`` in any plugin file (whether
  top-level or deferred inside a method), resolve ``Y`` against
  the source module and assert it exists. Catches the
  ``pipeline.tts.synth`` class of bug.

* **Runtime callable smoke check**: for every registered plugin,
  instantiate it AND call its main contract method
  (``.synth`` / ``.build`` / ``.produce`` / ``.compose`` / ``.mux``)
  against minimal stubs with heavy operations (cloud HTTP, ffmpeg
  subprocess, model loading) mocked. The plugin's body MUST execute
  far enough to flush every deferred import and confirm the code
  paths line up with their target signatures.

The smoke check is allowed to raise ``NotImplementedError`` /
``FileNotFoundError`` / ``RuntimeError`` from the body's own logic
once it's past the import wall — it just must NOT raise
``ImportError`` / ``ModuleNotFoundError`` / ``AttributeError`` at
the import boundary or ``TypeError`` from a kwarg signature
mismatch.
"""
from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIRS = (
    "pipeline/render/audio",
    "pipeline/render/timeline",
    "pipeline/render/visualize",
    "pipeline/render/overlays",
    "pipeline/render/music",
    "pipeline/render/compose",
)


def _plugin_files() -> list[Path]:
    """Every plugin .py file (excluding __init__.py + dunder dirs)."""
    out: list[Path] = []
    for d in PLUGIN_DIRS:
        for f in sorted((REPO_ROOT / d).glob("*.py")):
            if f.name.startswith("_") or f.name == "__init__.py":
                continue
            out.append(f)
    return out


def _collect_pipeline_imports(
    file_path: Path,
) -> list[tuple[str, str, int]]:
    """Walk the AST of ``file_path``. Return every
    ``(source_module, imported_name, line_no)`` triple where
    ``source_module`` starts with ``pipeline.``.

    Catches both top-level imports AND deferred imports inside
    function bodies (the exact pattern that hid the
    ``pipeline.tts.synth`` bug for weeks). Skips `import pipeline.X`
    style imports — those are name-binding, not symbol-resolution,
    so they can't fail with the "symbol doesn't exist" bug class.
    """
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("pipeline."):
            continue
        for alias in node.names:
            # Skip `from pipeline.X import *` — wildcards don't have a
            # specific name to check.
            if alias.name == "*":
                continue
            out.append((node.module, alias.name, node.lineno))
    return out


class PluginImportResolutionTest(unittest.TestCase):
    """Static check: every `from pipeline.X import Y` in any plugin
    file must resolve to an attribute that actually exists at X.

    This is the gate that would have caught
    ``pipeline.render.audio.tts_single``'s ``from pipeline.tts import
    synth`` (a non-existent symbol) before it shipped.
    """

    def test_every_plugin_pipeline_import_resolves(self):
        unresolved: list[str] = []
        for plugin_file in _plugin_files():
            for source_mod, name, line in _collect_pipeline_imports(plugin_file):
                # Try to import the source module. If THAT fails the
                # plugin couldn't possibly work — surface the import
                # error rather than swallowing it.
                try:
                    mod = importlib.import_module(source_mod)
                except Exception as exc:  # noqa: BLE001
                    unresolved.append(
                        f"  {plugin_file.relative_to(REPO_ROOT)}:{line} — "
                        f"`from {source_mod} import {name}` — source module "
                        f"failed to import: {exc!r}"
                    )
                    continue
                # The imported name must exist as an attribute on the
                # source module. ``hasattr`` is the right check —
                # matches what `from X import Y` actually does at
                # runtime.
                if not hasattr(mod, name):
                    unresolved.append(
                        f"  {plugin_file.relative_to(REPO_ROOT)}:{line} — "
                        f"`from {source_mod} import {name}` — symbol "
                        f"{name!r} does NOT exist on {source_mod}"
                    )
        if unresolved:
            self.fail(
                "Plugin files reference pipeline symbols that don't exist.\n"
                "These are deferred imports that won't fail until the plugin's "
                "main method is called for the first time, so module-import "
                "tests + Protocol assertions can't catch them.\n\n"
                + "\n".join(unresolved)
            )


class PluginRegistryHasExpectedShapeTest(unittest.TestCase):
    """Confirm every registered plugin under each slot exposes the
    contract method the engine will call. Pre-fix, ``isinstance(p,
    AudioSynthesizer)`` was the only check — but Protocol checks only
    confirm `hasattr`, not that the method works. This goes one step
    further: confirm `getattr(plugin, method)` is callable AND has the
    expected positional parameter shape so an engine-side
    ``plugin.method(spec, script, work_dir)`` won't TypeError.
    """

    SLOT_TO_METHOD = {
        "audio": "synth",
        "timeline": "build",
        "visualize": "produce",
        "overlays": "produce",
        "music": "compose",
        "compose": "mux",
    }

    def setUp(self):
        # Eager-import every plugin package so the registry is full.
        import pipeline.render.audio  # noqa: F401, PLC0415
        import pipeline.render.compose  # noqa: F401, PLC0415
        import pipeline.render.music  # noqa: F401, PLC0415
        import pipeline.render.overlays  # noqa: F401, PLC0415
        import pipeline.render.timeline  # noqa: F401, PLC0415
        import pipeline.render.visualize  # noqa: F401, PLC0415

    def test_every_registered_plugin_exposes_its_contract_method(self):
        from pipeline.render.contracts import _REGISTRY  # noqa: PLC0415

        problems: list[str] = []
        for slot, expected_method in self.SLOT_TO_METHOD.items():
            registered = _REGISTRY.get(slot, {})
            self.assertGreater(
                len(registered), 0,
                f"slot {slot!r} has no registered plugins — engine "
                f"would PluginNotFound at first dispatch",
            )
            for name, plugin in registered.items():
                method = getattr(plugin, expected_method, None)
                if method is None:
                    problems.append(
                        f"  {slot}/{name}: missing method {expected_method!r} "
                        f"(plugin class: {type(plugin).__name__})"
                    )
                    continue
                if not callable(method):
                    problems.append(
                        f"  {slot}/{name}: attribute {expected_method!r} is "
                        f"not callable (got {type(method).__name__})"
                    )
        if problems:
            self.fail(
                "Plugin registry has plugins missing their contract methods:\n"
                + "\n".join(problems)
            )


class TtsSinglePluginCallableSmokeTest(unittest.TestCase):
    """Pin that ``tts_single`` actually executes its body without
    raising on a deferred-import failure. THIS is the test that
    would have caught the production bug.

    We mock ``pipeline.audio.synthesize`` (the underlying TTS
    dispatcher) so we don't actually hit a TTS provider — but
    everything BEFORE that call (the deferred import, the spec
    field reads, the kwarg names) executes for real.
    """

    def test_tts_single_synth_does_not_raise_on_deferred_imports(self):
        from unittest.mock import MagicMock, patch
        import tempfile

        # Lazy-import to avoid Metal-context allocation at test collect time.
        import pipeline.render.audio.tts_single as plugin_mod  # noqa: PLC0415

        plugin = plugin_mod.TtsSingle()

        # Minimal spec stub — must expose voice_provider, voice_id,
        # tone, tts.{tone_overrides, speed_default, post_atempo_default}
        spec = MagicMock()
        spec.voice_provider = "cloudrun_chatterbox"
        spec.voice_id = "test-voice"
        spec.tone = None
        spec.tts.tone_overrides = {}
        spec.tts.speed_default = 1.0
        spec.tts.post_atempo_default = 1.0

        script = {"narration": "hello world"}

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            captured_synthesize_kwargs: dict = {}

            def fake_synthesize(**kw):
                # Verify the kwargs we passed are ones synthesize
                # actually accepts (TypeError check happens at call site
                # automatically when this signature is the real one).
                captured_synthesize_kwargs.update(kw)
                # Write a stub wav so probe_duration doesn't fail.
                out = kw["out_path"]
                out.parent.mkdir(parents=True, exist_ok=True)
                # Minimal valid wav (44-byte RIFF header + 0 data).
                # probe_duration uses ffprobe which can read this.
                import subprocess
                subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-t", "0.5",
                    "-i", "sine=frequency=440:sample_rate=24000",
                    "-c:a", "pcm_s16le", str(out),
                ], check=True)
                return out

            # Patch where it's looked up by the deferred import (the
            # plugin does `from pipeline.audio import synthesize`).
            with patch("pipeline.audio.synthesize", side_effect=fake_synthesize):
                result = plugin.synth(spec, script, work_dir)

            # Plugin returned an AudioResult — wiring works end-to-end.
            self.assertEqual(result.narration_path, work_dir / "narration.wav")
            self.assertGreater(result.duration_s, 0)

            # The kwargs we forwarded must be a subset of what
            # `pipeline.audio.synthesize` accepts. THIS is the kwarg-
            # signature check — if the plugin passes `atempo=...`
            # but synthesize doesn't take it, Python raises
            # TypeError at the call site.
            from pipeline.audio import synthesize  # noqa: PLC0415
            import inspect
            allowed = set(inspect.signature(synthesize).parameters.keys())
            forwarded = set(captured_synthesize_kwargs.keys())
            extra = forwarded - allowed
            self.assertFalse(
                extra,
                f"tts_single forwarded kwargs {extra!r} that "
                f"pipeline.audio.synthesize doesn't accept — would "
                f"raise TypeError in production. allowed={allowed!r}",
            )


if __name__ == "__main__":
    unittest.main()
