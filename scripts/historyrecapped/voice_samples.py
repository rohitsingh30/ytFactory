"""Generate ~60s sleep-narration samples in 3 candidate Cartesia voices.

Each voice is rendered at Cartesia 'slow' AND post-stretched with
ffmpeg atempo=0.85 for true sleep-pace cadence.

Outputs:
    historyrecapped/cache/_voice_samples/{british_lady,sarah,sneha}.wav (raw slow)
    historyrecapped/cache/_voice_samples/{british_lady,sarah,sneha}_stretched.wav (slowed further)

Usage:
    .venv/bin/python scripts/historyrecapped/voice_samples.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "historyrecapped/cache/_voice_samples"
OUT.mkdir(parents=True, exist_ok=True)

# Load .env so CARTESIA_API_KEY is available.
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

API_KEY = os.environ["CARTESIA_API_KEY"]

VOICES = [
    ("british_lady", "79a125e8-cd45-4c13-8a67-188112f4dd22"),
    ("sarah",        "694f9389-aac1-45b6-b726-9d9369183238"),
    ("sneha",        "6b02ffe5-e3cb-48c0-a023-c72f85953375"),
]

SAMPLE = (
    "On the morning of December the seventh, nineteen forty-one, the "
    "United States Pacific Fleet rests at anchor in the still water of "
    "Pearl Harbor. Battleship Row is quiet. Ford Island is quiet. "
    "Beyond the harbor, the Pacific stretches west, into a sunrise that "
    "has not yet reached the islands. The first wave of Japanese aircraft "
    "is already in the air. They have flown for nearly two hours over "
    "open ocean, guided in part by a Honolulu radio station playing "
    "softly through the predawn dark. They cross the north shore of "
    "Oahu at six minutes to eight. The men on the ships do not yet know. "
    "The men on the islands do not yet know. The morning is calm."
)


def cartesia(text: str, voice_id: str, out: Path) -> None:
    body = json.dumps({
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
        "language": "en",
        "__experimental_controls": {"speed": "slow"},
    }).encode("utf-8")
    headers = {
        "X-API-Key": API_KEY,
        "Cartesia-Version": "2024-11-13",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        "https://api.cartesia.ai/tts/bytes", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code}\n{detail}")


def stretch(in_wav: Path, out_wav: Path, factor: float = 0.85) -> None:
    """ffmpeg atempo to stretch further without pitch shift."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(in_wav),
            "-filter:a", f"atempo={factor}",
            str(out_wav),
        ],
        check=True,
    )


def duration_s(wav: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(wav)],
        text=True,
    ).strip()
    return float(out)


def main() -> int:
    print(f"text: {len(SAMPLE)} chars\n")
    for name, vid in VOICES:
        raw = OUT / f"{name}.wav"
        slow = OUT / f"{name}_stretched.wav"
        t0 = time.time()
        cartesia(SAMPLE, vid, raw)
        d_raw = duration_s(raw)
        print(f"[{name:12s}] cartesia slow → {raw.name} {d_raw:.1f}s ({time.time()-t0:.1f}s gen)")
        stretch(raw, slow, 0.85)
        d_slow = duration_s(slow)
        print(f"[{name:12s}] +atempo 0.85  → {slow.name} {d_slow:.1f}s")
        print()
    print(f"open {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
