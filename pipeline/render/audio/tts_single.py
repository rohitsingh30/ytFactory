"""Single-pass cloud-TTS AudioSynthesizer for the short engine.

Wraps the existing single-pass TTS path baked into ``shorts.py`` so
the new engine can call a Protocol method instead of import-and-
orchestrate the renderer-internal helpers.

This is a DELEGATING shim — the body still lives in shorts.py and
the plugin just calls into it. The bigbang PR moves the body fully
into this module (along with the rest of the per-beat orchestration)
so shorts.py can be deleted.

Provider routing
----------------

The short engine sets ``spec.voice_provider``. Today's values:

- ``cloudrun_chatterbox``  (English, default for shorts on every
  channel except mystoriesanimated/tifu)
- ``cloudrun_indicparler`` (Hindi, descriptive voices)
- ``cloudrun_indicf5``     (Hindi, AI4Bharat F5-tuned, voice clone)
- ``f5_tts``               (laptop fallback)
- ``kokoro``               (laptop fallback for Hindi)

This module's :class:`TtsSingle` doesn't reach into the providers
itself — it delegates to ``pipeline.tts`` which already routes by
provider name with cloud→laptop fallback wired through
``pipeline/tts/cloudrun.py`` (per ``docs/cloudrun_tts.md``).
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
        from pipeline.tts import synth as _provider_synth  # noqa: PLC0415

        narration_text = self._narration_text(script)
        out_path = work_dir / "narration.wav"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        provider = spec.voice_provider or "cloudrun_chatterbox"
        voice_id = spec.voice_id or ""
        speed = self._effective_speed(spec)
        atempo = self._effective_atempo(spec)

        # _provider_synth is the channel-agnostic TTS dispatcher in
        # pipeline.tts.__init__. It accepts provider name + voice ref +
        # text and writes the wav to out_path. Cloud→laptop fallback
        # is wired inside (CloudRunUnavailable → local F5/kokoro).
        _provider_synth(
            text=narration_text,
            provider=provider,
            voice=voice_id,
            speed=speed,
            atempo=atempo,
            out_path=out_path,
        )

        # Bind the wav to the cfg that produced it via the voice
        # fingerprint sidecar, so cache-invalidation downstream works
        # the same way it does for shorts.py + long_form.py today.
        fp = compute_fingerprint({
            "tts_provider": provider,
            "tts_voice": voice_id,
            "tts_speed": speed,
            "tts_post_atempo": atempo,
        })
        write_sidecar(out_path, fp)

        duration_s = probe_duration(out_path)
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


# Module-import side-effect: register the plugin under
# ``audio:tts_single`` so the engine can look it up.
register_plugin("audio", "tts_single", TtsSingle())

# Protocol conformance is structural — but we assert it here for
# documentation + a fail-fast safety net if a future edit breaks the
# signature. ``isinstance`` works because AudioSynthesizer is
# @runtime_checkable.
assert isinstance(TtsSingle(), AudioSynthesizer)


__all__ = ["TtsSingle"]
