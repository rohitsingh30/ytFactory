"""Generate ~30s Kokoro samples in 4 candidate voices for the long-form sleep mode.

Goal: soft AND clearly audible. af_nicole was rejected as too whispery.
Outputs at historyrecapped/cache/_kokoro_samples/<voice>.wav.

Usage:
    .venv/bin/python historyrecapped/scripts/kokoro_voice_samples.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import soundfile as sf
from pipeline.audio import _kokoro

OUT = ROOT / "historyrecapped/cache/_kokoro_samples"
OUT.mkdir(parents=True, exist_ok=True)

# 4 candidate voices — soft + clear, NOT the af_nicole whisper profile.
VOICES = [
    ("bf_isabella", "British female, formal calm — BBC-doc cadence"),
    ("af_bella",    "US female, warm + clear"),
    ("af_sarah",    "US female, neutral + clean enunciation"),
    ("bm_lewis",    "British male, calm + low"),
]

SAMPLE = (
    "On the morning of December the seventh, nineteen forty-one, the "
    "United States Pacific Fleet rests at anchor in the still water of "
    "Pearl Harbor. Battleship Row is quiet. Ford Island is quiet. "
    "Beyond the harbor, the Pacific stretches west, into a sunrise that "
    "has not yet reached the islands."
)


def main() -> int:
    print(f"text: {len(SAMPLE)} chars\n")
    kk = _kokoro()
    for voice, desc in VOICES:
        out = OUT / f"{voice}.wav"
        samples, sr = kk.create(SAMPLE, voice=voice, speed=1.0, lang="en-us" if voice.startswith("a") else "en-gb")
        sf.write(str(out), samples, sr)
        dur = len(samples) / sr
        print(f"[{voice:14s}] {desc}")
        print(f"    {out.name}  {dur:.1f}s")
    print(f"\nopen {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
