"""Single-pass cloud-TTS AudioSynthesizer for the short engine.

Wraps ``pipeline.audio.synthesize`` (the channel-agnostic TTS dispatcher
that every legacy renderer already uses) so the new short engine can
call a Protocol method instead of import-and-orchestrate the
provider-specific helpers itself.

Provider routing
----------------

The short engine sets ``spec.voice_provider``. Today's values:

- ``cloudrun_chatterbox``  (English, default for shorts on every
  channel except mystoriesanimated/tifu)
- ``cloudrun_indicf5``     (Hindi, AI4Bharat F5-tuned, voice clone)
- ``kokoro``               (laptop fallback)

This module's :class:`TtsSingle` doesn't reach into the providers
itself — it delegates to :func:`pipeline.audio.synthesize` which
already routes by provider name with cloud→laptop fallback wired
through ``pipeline/tts/cloudrun.py`` (per ``docs/cloudrun_tts.md``).

History
-------

Pre-2026-05-14 this file imported ``from pipeline.tts import synth``
— a symbol that has NEVER existed. The bug went unnoticed because
every engine test uses ``audio_plugin: audio_from_fixture`` instead
of ``tts_single``, so the broken import (deferred inside the
``synth()`` method body) was never executed in CI. The
``isinstance(TtsSingle(), AudioSynthesizer)`` assertion at module
load was misleading — Protocol checks are structural (just
``hasattr``), so a syntactically-correct method satisfies the
contract even if it raises on first call. Fix: use the actual
dispatcher + add a smoke-test gate that calls every plugin's main
method against a stub. See
``tests/render/test_plugin_callable_smoke.py``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    AudioSynthesizer,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration
from pipeline.render.shared.voice_fingerprint import (
    compute_fingerprint,
    write_sidecar,
)


class TtsSingle:
    """Single-pass cloud-TTS synthesizer.

    Calls the provider once with the full narration text. Suitable for
    short-form (≤ 120 s) where TTS providers don't need chunking.

    For long-form (chunked TTS), use :class:`pipeline.render.audio.tts_chunked.TtsChunked`.
    """

    def synth(
        self,
        spec: Any,  # RenderSpec — Any here to avoid the spec→audio circular import
        script: dict[str, Any],
        work_dir: Path,
    ) -> AudioResult:
        # Deferred import to avoid loading the heavy TTS provider
        # graph at engine-import time. ``pipeline.audio`` pulls in
        # kokoro / chatterbox model loaders transitively and
        # those allocate Metal contexts on import.
        from pipeline.audio import synthesize  # noqa: PLC0415

        narration_text = self._narration_text(script)
        out_path = work_dir / "narration.wav"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        provider = spec.voice_provider or "cloudrun_chatterbox"
        voice_id = spec.voice_id or ""
        speed = self._effective_speed(spec)

        # synthesize is the channel-agnostic TTS dispatcher in
        # pipeline.audio.__init__. It accepts (text, voice, out_path,
        # speed, provider, …) and writes the wav to out_path. Cloud→
        # laptop fallback is wired inside pipeline.tts.cloudrun
        # (CloudRunUnavailable → local F5/kokoro). Returns the wav path.
        synthesize(
            text=narration_text,
            voice=voice_id,
            out_path=out_path,
            speed=speed,
            provider=provider,
        )

        # Bind the wav to the cfg that produced it via the voice
        # fingerprint sidecar, so cache-invalidation downstream works
        # the same way it does for the legacy renderers today. We
        # include post_atempo in the fingerprint even though
        # synthesize() doesn't apply it directly — the engine layer
        # may apply atempo as a post-pass; the fingerprint must
        # match the cfg the user picked.
        atempo = self._effective_atempo(spec)
        fp = compute_fingerprint({
            "tts_provider": provider,
            "tts_voice": voice_id,
            "tts_speed": speed,
            "tts_post_atempo": atempo,
        })
        write_sidecar(out_path, fp)

        duration_s = probe_duration(out_path)
        _emit_tts_chunk_telemetry(
            chunk_index=0,
            chunk_text=narration_text,
            voice=voice_id,
            provider=provider,
            audio_path=out_path,
            audio_seconds=duration_s,
        )
        return AudioResult(
            narration_path=out_path,
            duration_s=duration_s,
            voice_fingerprint=fp,
            chunk_timings=None,
        )

    def _narration_text(self, script: dict[str, Any]) -> str:
        # The short engine accepts either a flat narration text or a
        # beats-style script. For now we just concatenate beats[].text;
        # the bigbang PR will define a stable script schema for this.
        if isinstance(script.get("narration"), str):
            return script["narration"]
        beats = script.get("beats") or []
        return " ".join(b.get("text", "") for b in beats if b.get("text"))

    def _effective_speed(self, spec: Any) -> float:
        # tone overrides win when set; else the spec.tts.speed_default.
        if spec.tone and spec.tone in spec.tts.tone_overrides:
            return spec.tts.tone_overrides[spec.tone].get("speed", spec.tts.speed_default)
        return spec.tts.speed_default

    def _effective_atempo(self, spec: Any) -> float:
        if spec.tone and spec.tone in spec.tts.tone_overrides:
            return spec.tts.tone_overrides[spec.tone].get("atempo", spec.tts.post_atempo_default)
        return spec.tts.post_atempo_default


def _emit_tts_chunk_telemetry(
    *,
    chunk_index: int,
    chunk_text: str,
    voice: str,
    provider: str,
    audio_path: Path,
    audio_seconds: float,
) -> None:
    try:
        from pipeline.observability import current_context, track_io  # noqa: PLC0415

        audio_bytes = audio_path.stat().st_size if audio_path.exists() else 0
        job_id = current_context().job_id or os.environ.get("YTFACTORY_JOB_ID")
        track_io(
            "tts.chunk",
            category="tts",
            job_id=job_id,
            input_text=chunk_text,
            output_text=None,
            input_meta={
                "chunk_index": chunk_index,
                "voice": voice,
                "provider": provider,
            },
            output_meta={
                "audio_seconds": audio_seconds,
                "audio_bytes": audio_bytes,
            },
        )
        if job_id:
            from pipeline.render.artifacts import emit_artifact_json  # noqa: PLC0415

            emit_artifact_json(
                job_id=job_id,
                kind="tts_chunks",
                data={
                    "chunk_index": chunk_index,
                    "text": chunk_text,
                    "voice": voice,
                    "audio_seconds": audio_seconds,
                },
                filename=f"{chunk_index:05d}.json",
                index=chunk_index,
            )
    except Exception:  # noqa: BLE001
        pass


# Module-import side-effect: register the plugin under
# ``audio:tts_single`` so the engine can look it up.
register_plugin("audio", "tts_single", TtsSingle())

# Protocol conformance is structural — but we assert it here for
# documentation + a fail-fast safety net if a future edit breaks the
# signature. ``isinstance`` works because AudioSynthesizer is
# @runtime_checkable. NOTE: Protocol assertions only check attribute
# existence — they cannot detect that a method calls a non-existent
# import. The real "is this plugin actually callable?" gate lives in
# tests/render/test_plugin_callable_smoke.py.
assert isinstance(TtsSingle(), AudioSynthesizer)


__all__ = ["TtsSingle"]
