"""Chunked cloud-TTS AudioSynthesizer for the long engine.

Wraps :func:`pipeline.render.long_form.synth_long_narration` (the
canonical chunked-TTS implementation that splits text into ≤
``spec.tts.chunk_target_chars`` chunks, synthesises each with the
configured provider, and concatenates with silence joiners).

This is a DELEGATING shim — the body still lives in long_form.py.
The bigbang PR moves it fully into this module so long_form.py can
be deleted.

Why the long engine needs chunking
----------------------------------

Cloud TTS providers (cloudrun_chatterbox / indicf5 / etc.) cap
single-request input length around ~2k chars / ~30 s of audio.
Long-form renders routinely exceed 30 minutes / ~6000 words.
Chunking + concat with silence joiners (T1.15 hardening — sample
rate / channel layout matched against the first chunk) is the
canonical pattern.
"""
from __future__ import annotations

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


class TtsChunked:
    """Chunked cloud-TTS synthesizer.

    Delegates to ``pipeline.render.long_form.synth_long_narration``
    for now. The bigbang PR moves the body inline.
    """

    def synth(
        self,
        spec: Any,  # RenderSpec
        script: dict[str, Any],
        work_dir: Path,
    ) -> AudioResult:
        from pipeline.render.long_form import synth_long_narration  # noqa: PLC0415

        narration_text = self._narration_text(script)
        out_path = work_dir / "narration.wav"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        provider = spec.voice_provider or "cloudrun_chatterbox"
        voice_id = spec.voice_id or ""
        speed = self._effective_speed(spec)
        atempo = self._effective_atempo(spec)

        # synth_long_narration writes narration.wav AND chunk_*.wav
        # files alongside, returns (narration_wav, list_of_chunks).
        narration_wav, chunks = synth_long_narration(
            text=narration_text,
            voice_id=voice_id,
            provider=provider,
            cache_dir=work_dir,
            target_chars=spec.tts.chunk_target_chars,
            join_silence_s=spec.tts.chunk_join_silence_s,
            speed=speed,
            post_atempo=atempo,
        )

        # Voice fingerprint sidecar — bind the wav to the cfg that
        # produced it so the engine's cache layer can invalidate
        # stale narration.wav when the user picks a different voice.
        fp = compute_fingerprint({
            "tts_provider": provider,
            "tts_voice": voice_id,
            "tts_speed": speed,
            "tts_post_atempo": atempo,
            "tts_chunk_target_chars": spec.tts.chunk_target_chars,
            "tts_chunk_join_silence_s": spec.tts.chunk_join_silence_s,
        })
        write_sidecar(narration_wav, fp)

        duration_s = probe_duration(narration_wav)

        # Per-chunk (start_s, end_s) for downstream timeline plugins
        # — saves the asr_anchors plugin from re-probing each chunk.
        chunk_timings: list[tuple[float, float]] = []
        cursor = 0.0
        for chunk_path in chunks:
            chunk_dur = probe_duration(chunk_path)
            chunk_timings.append((cursor, cursor + chunk_dur))
            cursor += chunk_dur + spec.tts.chunk_join_silence_s

        return AudioResult(
            narration_path=narration_wav,
            duration_s=duration_s,
            voice_fingerprint=fp,
            chunk_timings=chunk_timings,
        )

    def _narration_text(self, script: dict[str, Any]) -> str:
        # Long-form scripts have either a flat ``narration`` field or
        # a list of ``sections[]`` / ``chapters[]`` with per-section
        # narration. Concatenate in order.
        if isinstance(script.get("narration"), str):
            return script["narration"]
        sections = script.get("sections") or script.get("chapters") or []
        parts: list[str] = []
        for s in sections:
            text = s.get("narration") or s.get("body") or ""
            if text:
                parts.append(text.strip())
        return "\n\n".join(parts)

    def _effective_speed(self, spec: Any) -> float:
        if spec.tone and spec.tone in spec.tts.tone_overrides:
            return spec.tts.tone_overrides[spec.tone].get("speed", spec.tts.speed_default)
        return spec.tts.speed_default

    def _effective_atempo(self, spec: Any) -> float:
        if spec.tone and spec.tone in spec.tts.tone_overrides:
            return spec.tts.tone_overrides[spec.tone].get("atempo", spec.tts.post_atempo_default)
        return spec.tts.post_atempo_default


register_plugin("audio", "tts_chunked", TtsChunked())
assert isinstance(TtsChunked(), AudioSynthesizer)


__all__ = ["TtsChunked"]
