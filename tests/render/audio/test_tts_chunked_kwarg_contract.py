"""Regression test for the 2026-05-15 kwarg-drift cloud failure.

Backstory: ``pipeline/render/audio/tts_chunked.py::TtsChunked.synth``
called ``synth_long_narration(target_chars=..., post_atempo=...)`` —
neither kwarg name exists on the function (the actual names are
``chunk_target_chars`` and ``atempo``). Every cloud long-form render
hit::

    RuntimeError: long-form engine render failed for slug=...:
    synth_long_narration() got an unexpected keyword argument 'target_chars'

Same class as the ``forced_lines=`` kwarg-drift bug fixed in
``asr_beats.py`` the same day. Different test file, identical
introspection pattern: walk the caller's bound kwargs and assert
every name resolves on the callee's signature. Either rename
(caller side, what we did this time) or rename callee (with a
back-compat alias) — but never let them silently diverge again.

Why introspection rather than calling: synth_long_narration spawns
real cloud TTS requests that we don't want to make at unit-test
time. The ``inspect.signature`` test catches the bug class without
spinning up a fake server.

Why ``ast.parse`` rather than regex: the caller has nested calls
like ``ref_audio_text=self._ref_audio_text(spec)``; a non-greedy
regex on parens stops at the first inner ``)`` and misses kwargs
after it. AST walk handles balanced subexpressions correctly.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from typing import Any

from pipeline.render.audio.tts_chunked import TtsChunked
from pipeline.render.shared.long_form_lib import synth_long_narration


def _kwargs_passed_to(*, caller, callee_name: str) -> set[str]:
    """AST-walk the caller's source. Return the set of keyword
    argument NAMES passed to any call whose function-name attribute
    matches ``callee_name`` (e.g. ``synth_long_narration``).

    Handles nested calls (a kwarg whose value is itself a function
    call) — the regex approach stopped at the first inner ``)`` and
    missed downstream kwargs.
    """
    source = textwrap.dedent(inspect.getsource(caller))
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match either bare-name (synth_long_narration(...)) or attribute
        # (module.synth_long_narration(...)).
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        else:
            continue
        if name != callee_name:
            continue
        for kw in node.keywords:
            if kw.arg is not None:  # ``**kwargs`` has arg=None
                found.add(kw.arg)
    return found


class TtsChunkedKwargContractTest(unittest.TestCase):
    """Pin the caller→callee kwarg binding."""

    def test_caller_passes_only_kwargs_that_exist_on_callee(self) -> None:
        """Walk the source of TtsChunked.synth, find every keyword-arg
        name it passes to ``synth_long_narration``, and assert each
        name is a valid parameter on ``synth_long_narration``'s
        signature.
        """
        callee_sig = inspect.signature(synth_long_narration)
        callee_kwargs = set(callee_sig.parameters.keys())

        passed = _kwargs_passed_to(
            caller=TtsChunked.synth, callee_name="synth_long_narration",
        )

        unknown = passed - callee_kwargs
        self.assertFalse(
            unknown,
            f"TtsChunked.synth passes kwargs that don't exist on "
            f"synth_long_narration's signature: {sorted(unknown)}. "
            f"Either rename the call site OR add alias on the callee. "
            f"Last time this happened (target_chars vs chunk_target_chars), "
            f"every cloud long-form render TypeError'd — caught at the "
            f"engine boundary, swallowed into a RuntimeError. Pin the "
            f"contract here so future renames are caught at unit-test "
            f"time, not in production.",
        )

    def test_caller_passes_required_args_of_callee(self) -> None:
        """Every parameter on ``synth_long_narration`` that has NO
        default MUST be passed by the caller — otherwise we'd hit a
        ``TypeError: missing 1 required positional argument`` instead
        of unknown-kwarg, but the failure mode is the same: silent
        regression at the engine boundary."""
        callee_sig = inspect.signature(synth_long_narration)
        required = {
            name for name, p in callee_sig.parameters.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }

        passed = _kwargs_passed_to(
            caller=TtsChunked.synth, callee_name="synth_long_narration",
        )

        missing = required - passed
        self.assertFalse(
            missing,
            f"TtsChunked.synth doesn't pass required kwargs of "
            f"synth_long_narration: {sorted(missing)}. Without these "
            f"the call raises TypeError before any TTS work happens.",
        )


class RefAudioTextResolutionTest(unittest.TestCase):
    """Pin _ref_audio_text fallback. F5 providers (f5_tts / cloudrun_f5)
    require the reference WAV's transcript; chatterbox / kokoro don't.
    The helper returns the value from ``spec.tts.ref_text`` when set,
    None otherwise — we let synth_long_narration's own runtime check
    raise a clear error if the picked provider needs it."""

    def _spec_with_ref(self, ref_text: Any) -> Any:
        from unittest.mock import MagicMock
        spec = MagicMock()
        spec.tts.ref_text = ref_text
        return spec

    def test_returns_str_when_set(self) -> None:
        s = self._spec_with_ref("This is the spoken transcript.")
        out = TtsChunked()._ref_audio_text(s)
        self.assertEqual(out, "This is the spoken transcript.")

    def test_returns_none_when_unset(self) -> None:
        from unittest.mock import MagicMock
        spec = MagicMock()
        # Force AttributeError-equivalent — getattr returns None.
        del spec.tts.ref_text
        spec.tts = type("Tts", (), {})()
        out = TtsChunked()._ref_audio_text(spec)
        self.assertIsNone(out)

    def test_returns_none_when_empty_string(self) -> None:
        s = self._spec_with_ref("   ")
        out = TtsChunked()._ref_audio_text(s)
        self.assertIsNone(out)


class ResolveVoiceForLongFormTest(unittest.TestCase):
    """Pin the 2026-05-15 long-form voice-resolver fix.

    Backstory: ``synth_long_narration`` opens ``voice_id`` as a file
    path directly — it doesn't go through ``pipeline.audio.synthesize``
    where the catalog resolver lives. So when the wizard sent a bare
    catalog name like ``sarah``, the long-form path tried to read
    ``/workspace/pipeline/sarah`` and died with::

        [Errno 2] No such file or directory: '/workspace/pipeline/sarah'

    Surfaced by job 2585f6ab on 2026-05-15 (CosmosDecoded
    "How We Knew Universe Expanding" long-form).

    The fix wires the same ``pipeline.voice.voice_catalog.resolve_voice``
    used by short-form into the long-form ``tts_chunked`` plugin's
    ``_resolve_voice_for_long_form`` helper. Bare names → on-disk
    WAV path + transcript; path-style → returned unchanged.
    """

    def test_empty_voice_id_returns_unchanged(self):
        v, t = TtsChunked()._resolve_voice_for_long_form("")
        self.assertEqual(v, "")
        self.assertIsNone(t)

    def test_bare_name_resolves_to_catalog_path(self):
        from unittest.mock import patch
        from pathlib import Path
        with patch(
            "pipeline.voice.voice_catalog.resolve_voice",
            return_value=(Path("/abs/voice_refs/sarah.wav"), "transcript here"),
        ):
            v, t = TtsChunked()._resolve_voice_for_long_form("sarah")
        self.assertEqual(v, "/abs/voice_refs/sarah.wav")
        self.assertEqual(t, "transcript here")

    def test_resolver_returns_none_path_passes_voice_through(self):
        from unittest.mock import patch
        with patch(
            "pipeline.voice.voice_catalog.resolve_voice",
            return_value=(None, "stale transcript"),
        ):
            v, t = TtsChunked()._resolve_voice_for_long_form("unknown-voice")
        # When resolver can't pick a wav (returns None), preserve the
        # caller's original voice_id but still surface any catalog
        # transcript hit.
        self.assertEqual(v, "unknown-voice")
        self.assertEqual(t, "stale transcript")

    def test_resolver_exception_falls_back_to_input(self):
        from unittest.mock import patch
        with patch(
            "pipeline.voice.voice_catalog.resolve_voice",
            side_effect=ValueError("catalog corrupt"),
        ):
            v, t = TtsChunked()._resolve_voice_for_long_form("any-name")
        # Best-effort: exception → original value, no transcript.
        self.assertEqual(v, "any-name")
        self.assertIsNone(t)


class SynthCallsResolverAndChainsTranscriptTest(unittest.TestCase):
    """End-to-end test of the synth() entry point: resolver fires
    before synth_long_narration, and the catalog transcript chains
    into ref_audio_text when the spec doesn't override it."""

    def test_synth_resolves_voice_and_chains_transcript(self):
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        import tempfile

        spec = MagicMock()
        spec.voice_provider = "cloudrun_chatterbox"
        spec.voice_id = "sarah"
        spec.tts.chunk_target_chars = 380
        spec.tts.chunk_join_silence_s = 0.4
        spec.tts.speed_default = 0.98
        spec.tts.post_atempo_default = 1.0
        spec.tts.tone_overrides = {}
        spec.tts.ref_text = None  # No spec override → catalog wins
        spec.tone = None

        captured = {}

        def fake_synth(**kwargs):
            captured.update(kwargs)
            wav = Path(kwargs["cache_dir"]) / "narration.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"RIFF" + b"\0" * 100)
            return wav, [wav]

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "pipeline.voice.voice_catalog.resolve_voice",
                return_value=(Path("/abs/sarah.wav"), "catalog ref text"),
            ), patch(
                "pipeline.render.shared.long_form_lib.synth_long_narration",
                side_effect=fake_synth,
            ), patch(
                "pipeline.render.audio.tts_chunked.probe_duration",
                return_value=10.0,
            ), patch(
                "pipeline.render.audio.tts_chunked.compute_fingerprint",
                return_value={"fp": "x"},
            ), patch(
                "pipeline.render.audio.tts_chunked.write_sidecar"
            ):
                TtsChunked().synth(spec, {"narration": "hi there"}, Path(tmp))

        self.assertEqual(captured["voice_id"], "/abs/sarah.wav",
            "Bare 'sarah' MUST be resolved to absolute path before synth")
        self.assertEqual(captured["ref_audio_text"], "catalog ref text",
            "Catalog transcript MUST chain to ref_audio_text when "
            "spec.tts.ref_text is unset")

    def test_synth_spec_ref_text_wins_over_catalog(self):
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        import tempfile

        spec = MagicMock()
        spec.voice_provider = "cloudrun_chatterbox"
        spec.voice_id = "sarah"
        spec.tts.chunk_target_chars = 380
        spec.tts.chunk_join_silence_s = 0.4
        spec.tts.speed_default = 0.98
        spec.tts.post_atempo_default = 1.0
        spec.tts.tone_overrides = {}
        spec.tts.ref_text = "spec-explicit ref text"  # Override
        spec.tone = None

        captured = {}

        def fake_synth(**kwargs):
            captured.update(kwargs)
            wav = Path(kwargs["cache_dir"]) / "narration.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"RIFF" + b"\0" * 100)
            return wav, [wav]

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "pipeline.voice.voice_catalog.resolve_voice",
                return_value=(Path("/abs/sarah.wav"), "catalog ref text"),
            ), patch(
                "pipeline.render.shared.long_form_lib.synth_long_narration",
                side_effect=fake_synth,
            ), patch(
                "pipeline.render.audio.tts_chunked.probe_duration",
                return_value=10.0,
            ), patch(
                "pipeline.render.audio.tts_chunked.compute_fingerprint",
                return_value={"fp": "x"},
            ), patch(
                "pipeline.render.audio.tts_chunked.write_sidecar"
            ):
                TtsChunked().synth(spec, {"narration": "hi"}, Path(tmp))

        self.assertEqual(captured["ref_audio_text"], "spec-explicit ref text",
            "spec.tts.ref_text MUST win over catalog when both present")


if __name__ == "__main__":
    unittest.main()
