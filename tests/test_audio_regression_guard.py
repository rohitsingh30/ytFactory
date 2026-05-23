"""P5.4 — audio-mix regression guard (Q75).

Per the cofounder onboarding Q75: audio mix is "fine, just make sure
nothing is worsening." This file is the tripwire — it pins every
channel's audio quality knobs (tts_speed, post_atempo, chunk size) so
any unintentional change has to update the test explicitly.

Update path: when the user agrees to a deliberate audio change, edit
the expected values below + this test passes again. Surprise edits to
the YAMLs will fail this test.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml


_REPO = Path(__file__).resolve().parents[1]
_CHANNELS_DIR = _REPO / "pipeline" / "channels"


def _load_channel(slug: str) -> dict:
    return yaml.safe_load((_CHANNELS_DIR / f"{slug}.yaml").read_text())


# Pinned 2026-05-23 — derived by reading every shipped channel YAML's
# audio knobs. The TUPLE shape is (shorts_speed, long_speed,
# long_post_atempo, long_chunk_target_chars). ``None`` slots mean the
# YAML didn't set that knob; the renderer falls back to the global
# default in ``pipeline.audio.audio``.
_PINNED_AUDIO = {
    "mystoriesanimated": (0.90, 0.95, 0.92, 380),
    "cosmosdecoded":     (1.00, 1.00, 1.00, 380),
    "historyrecapped":   (1.00, 0.95, 0.85, 380),
    "hindutavaanimated": (1.00, 0.92, 0.95, 320),
    "sportsrecapped":    None,    # not yet pinned — fill once channel goes through smoke
    "rhymetimejunction": (1.00, 0.95, 1.00, 240),  # song-bridge channel; long_form does still TTS the verse
    "scrollpulse":       (0.95, None, None, None),  # P6.1 — short-only for now
}


class AudioRegressionGuardTest(unittest.TestCase):
    """Per-channel: pin audio knobs so nothing silently regresses."""

    def _shorts_speed(self, slug: str) -> float | None:
        cfg = _load_channel(slug)
        v = cfg.get("tts_speed")
        return None if v is None else float(v)

    def _long(self, slug: str) -> tuple[float | None, float | None, int | None]:
        cfg = _load_channel(slug)
        lf = cfg.get("long_form") or {}
        return (
            None if lf.get("tts_speed") is None else float(lf["tts_speed"]),
            None if lf.get("tts_post_atempo") is None else float(lf["tts_post_atempo"]),
            None if lf.get("tts_chunk_target_chars") is None else int(lf["tts_chunk_target_chars"]),
        )

    def test_audio_knobs_match_pinned_values(self) -> None:
        for slug, expected in _PINNED_AUDIO.items():
            if expected is None:
                # Channel knobs not yet pinned (e.g. sportsrecapped) —
                # surface as a sub-skip rather than a failure so the
                # test stays informative.
                continue
            shorts_speed, long_speed, long_atempo, long_chunk = expected
            actual_shorts = self._shorts_speed(slug)
            actual_long_speed, actual_long_atempo, actual_long_chunk = self._long(slug)
            with self.subTest(slug=slug):
                self.assertEqual(
                    actual_shorts, shorts_speed,
                    f"{slug}: tts_speed changed from pinned {shorts_speed} → {actual_shorts}. "
                    "If intentional, update _PINNED_AUDIO; otherwise revert the YAML.",
                )
                self.assertEqual(
                    actual_long_speed, long_speed,
                    f"{slug}: long_form.tts_speed changed from pinned {long_speed} → {actual_long_speed}.",
                )
                self.assertEqual(
                    actual_long_atempo, long_atempo,
                    f"{slug}: long_form.tts_post_atempo changed from pinned {long_atempo} → {actual_long_atempo}.",
                )
                self.assertEqual(
                    actual_long_chunk, long_chunk,
                    f"{slug}: long_form.tts_chunk_target_chars changed from pinned {long_chunk} → {actual_long_chunk}.",
                )


if __name__ == "__main__":
    unittest.main()
