"""TTS A/B spike — Cartesia Sonic vs ElevenLabs on a real AITA narration.

Renders the same narration through both providers so the user can listen
and pick the winner before we commit to a paid tier.

Usage:
    export CARTESIA_API_KEY=...      # https://play.cartesia.ai/keys
    export ELEVENLABS_API_KEY=...    # https://elevenlabs.io/app/settings/api-keys
    .venv/bin/python scripts/tts_ab_spike.py

Outputs:
    data/intermediate/spike/tts_ab/cartesia_<slug>.wav
    data/intermediate/spike/tts_ab/elevenlabs_<slug>.wav

Override voices/script via env:
    SPIKE_SCRIPT_PATH=<path/to/script.json>   # default: AITA birth-pool
    CARTESIA_VOICE_ID=...                      # default: female narrator
    ELEVENLABS_VOICE_ID=...                    # default: Rachel (female)

Both providers' free tiers cover this spike (~400 chars × 2 calls).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = REPO / "data/intermediate/aita_animated/scripts/aita-birth-pool.json"
OUT_DIR = REPO / "data/intermediate/spike/tts_ab"

# Default female narrators on each platform — comparable register for A/B.
CARTESIA_DEFAULT_VOICE = "79a125e8-cd45-4c13-8a67-188112f4dd22"  # British Lady
ELEVENLABS_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel


def _http_post(url: str, headers: dict, body: bytes) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} from {url}\n{detail}") from None


def cartesia_tts(text: str, out_path: Path, voice_id: str, api_key: str, language: str = "en") -> None:
    body = json.dumps({
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
        "language": language,
    }).encode("utf-8")
    headers = {
        "X-API-Key": api_key,
        "Cartesia-Version": "2024-11-13",
        "Content-Type": "application/json",
    }
    audio = _http_post("https://api.cartesia.ai/tts/bytes", headers, body)
    out_path.write_bytes(audio)


def elevenlabs_tts(text: str, out_path: Path, voice_id: str, api_key: str) -> None:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_44100"
    body = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/wav",
    }
    pcm = _http_post(url, headers, body)
    # ElevenLabs PCM endpoint returns raw PCM; wrap in a WAV header.
    import wave
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # s16
        w.setframerate(44100)
        w.writeframes(pcm)


def main() -> int:
    cart_key = os.environ.get("CARTESIA_API_KEY")
    eleven_key = os.environ.get("ELEVENLABS_API_KEY")
    if not cart_key and not eleven_key:
        print("No API keys set. Set at least one and rerun:")
        print("  - CARTESIA_API_KEY   → https://play.cartesia.ai/keys")
        print("  - ELEVENLABS_API_KEY → https://elevenlabs.io/app/settings/api-keys")
        return 2

    script_path = Path(os.environ.get("SPIKE_SCRIPT_PATH", DEFAULT_SCRIPT))
    if not script_path.exists():
        print(f"Script not found: {script_path}")
        return 1
    script = json.loads(script_path.read_text())
    slug = script.get("slug") or script_path.stem
    text = script["narration"]

    cart_voice = os.environ.get("CARTESIA_VOICE_ID", CARTESIA_DEFAULT_VOICE)
    eleven_voice = os.environ.get("ELEVENLABS_VOICE_ID", ELEVENLABS_DEFAULT_VOICE)
    language = os.environ.get("SPIKE_LANGUAGE", "en")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cart_out = OUT_DIR / f"cartesia_{slug}.wav"
    eleven_out = OUT_DIR / f"elevenlabs_{slug}.wav"

    print(f"slug:  {slug}")
    print(f"chars: {len(text)}")
    print(f"text:  {text[:120]}{'...' if len(text) > 120 else ''}")
    print()

    if cart_key:
        print(f"[cartesia] sonic-2, voice={cart_voice}, lang={language} → {cart_out.name}")
        t0 = time.time()
        cartesia_tts(text, cart_out, cart_voice, cart_key, language=language)
        print(f"[cartesia] {time.time() - t0:.1f}s, {cart_out.stat().st_size // 1024} KB")
    else:
        print("[cartesia] skipped (CARTESIA_API_KEY not set)")

    if eleven_key:
        print(f"[elevenlabs] eleven_multilingual_v2, voice={eleven_voice} → {eleven_out.name}")
        t0 = time.time()
        elevenlabs_tts(text, eleven_out, eleven_voice, eleven_key)
        print(f"[elevenlabs] {time.time() - t0:.1f}s, {eleven_out.stat().st_size // 1024} KB")
    else:
        print("[elevenlabs] skipped (ELEVENLABS_API_KEY not set)")

    print()
    print(f"Compare: open {OUT_DIR}/")
    if cart_key:
        print(f"  open {cart_out}")
    if eleven_key:
        print(f"  open {eleven_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
