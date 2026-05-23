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
        from pipeline.render.shared.long_form_lib import synth_long_narration  # noqa: PLC0415

        narration_text = self._narration_text(script)
        out_path = work_dir / "narration.wav"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        provider = spec.voice_provider or "cloudrun_chatterbox"
        raw_voice_id = spec.voice_id or ""
        voice_id, resolved_ref_text = self._resolve_voice_for_long_form(raw_voice_id)
        speed = self._effective_speed(spec)
        atempo = self._effective_atempo(spec)

        # ref_audio_text resolution order: explicit spec.tts.ref_text wins
        # (caller-provided is always intentional), then catalog sidecar
        # (when a catalog name was used), else None.
        explicit_ref = self._ref_audio_text(spec)
        ref_audio_text = explicit_ref or resolved_ref_text

        # synth_long_narration writes narration.wav AND chunk_*.wav
        # files alongside, returns (narration_wav, list_of_chunks).
        #
        # Kwarg name guard (2026-05-15) — synth_long_narration's signature
        # uses ``chunk_target_chars`` / ``join_silence_s`` / ``atempo``.
        # Pre-fix this caller used ``target_chars`` / ``post_atempo`` →
        # TypeError swallowed by the engine's outer except → every cloud
        # long-form render failed with::
        #
        #     synth_long_narration() got an unexpected keyword argument 'target_chars'
        #
        # Same kwarg-drift class as the 2026-05-15 ``forced_lines=`` fix
        # in ``asr_beats.py``. The Protocol-method test in
        # tests/render/audio/test_tts_chunked_kwarg_contract.py pins the
        # caller→callee binding with ``inspect.signature`` so future
        # signature renames hard-fail at unit-test time, not at the
        # cloud render boundary.
        narration_wav, chunks = synth_long_narration(
            text=narration_text,
            voice_id=voice_id,
            provider=provider,
            cache_dir=work_dir,
            chunk_target_chars=spec.tts.chunk_target_chars,
            join_silence_s=spec.tts.chunk_join_silence_s,
            speed=speed,
            atempo=atempo,
            ref_audio_text=ref_audio_text,
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

    def _ref_audio_text(self, spec: Any) -> str | None:
        """Return the spoken transcript of the ref WAV at ``voice_id``.

        Required by ``synth_long_narration`` when provider expects a
        voice-clone reference transcript (e.g. cloudrun_indicf5).
        Pulled from ``spec.tts.ref_text`` when present (the channel YAML's
        ``long_form.tts_ref_text`` flows through build_spec into
        the optional TtsConfig field). When the YAML doesn't declare
        one, return ``None`` and let the underlying function raise
        a clear RuntimeError if the provider actually needs it.
        """
        ref = getattr(spec.tts, "ref_text", None)
        if ref:
            return str(ref).strip() or None
        return None

    def _resolve_voice_for_long_form(self, voice_id: str) -> tuple[str, str | None]:
        """Resolve ``voice_id`` to (on-disk WAV path, optional transcript).

        Mirrors what :func:`pipeline.audio.synthesize` does for short-form
        — ``synth_long_narration`` is the legacy long-form TTS shim that
        opens ``voice_id`` directly as a file path (no dispatcher
        indirection), so when the wizard sends a bare catalog name like
        ``sarah`` the long-form path tries to read
        ``/workspace/pipeline/sarah`` and dies with::

            [Errno 2] No such file or directory: '/workspace/pipeline/sarah'

        Surfaced by job 2585f6ab on 2026-05-15 (CosmosDecoded
        "How We Knew Universe Expanding" long-form). The short-form
        engine had this fix wired into ``pipeline.audio.synthesize``
        on 2026-05-15 (job 215e411b) — same resolver, different
        call site. This helper extends it to long-form's tts_chunked
        plugin so EVERY TTS path resolves bare names + catalog names
        identically.

        Returns a tuple ``(resolved_voice_path, catalog_transcript)``
        — second element is the catalog's ``ref.txt`` content for
        the picked voice (so the caller can chain it into the F5
        ``ref_audio_text`` argument without a second catalog roundtrip).
        ``None`` for the transcript when no catalog hit (e.g. the
        caller passed a path-style ``voice_id`` directly).

        Failure mode: any exception in the resolver path is caught
        and returns the original ``voice_id`` unchanged — the
        downstream synth call will then raise its own (more specific)
        error. Best-effort, never blocks the render.
        """
        if not voice_id:
            return voice_id, None
        try:
            from pipeline.voice.voice_catalog import resolve_voice as _resolve  # noqa: PLC0415
            from pathlib import Path as _Path  # noqa: PLC0415
            project_root = _Path(__file__).resolve().parents[3]
            wav_path, catalog_transcript = _resolve(voice_id, project_root)
            if wav_path is not None:
                return str(wav_path), catalog_transcript
            return voice_id, catalog_transcript
        except Exception as exc:  # noqa: BLE001 — best-effort
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger(__name__).warning(
                "long-form voice resolution failed for %r (%s) — "
                "synth_long_narration will see the unresolved value",
                voice_id, exc,
            )
            return voice_id, None


register_plugin("audio", "tts_chunked", TtsChunked())
assert isinstance(TtsChunked(), AudioSynthesizer)


__all__ = ["TtsChunked"]
