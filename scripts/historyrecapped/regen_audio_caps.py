#!/usr/bin/env python3
"""Regenerate narration.wav + beats.json + word_*.png for historyrecapped's
battle-of-britain-few slug. Skips image-gen entirely — we're shipping
100%-footage so we don't need beat images."""
import json, os, sys, time
from pathlib import Path

ROOT = Path("/Users/rohit/ytFactory")
sys.path.insert(0, str(ROOT))

import yaml
from pipeline.audio import audio, beats as beats_mod, align
from pipeline.footage import compose

CACHE = ROOT / "historyrecapped/cache/battle-of-britain-few"
CACHE.mkdir(parents=True, exist_ok=True)
SCRIPT = ROOT / "historyrecapped/narrations/battle-of-britain-few.json"
CHANNEL = ROOT / "historyrecapped/config.yaml"

# Load .env for any provider env vars that pipeline.audio.audio expects.
env = (ROOT / ".env").read_text()
for line in env.splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

cfg = yaml.safe_load(CHANNEL.read_text())
script = json.loads(SCRIPT.read_text())
text = script["narration"]
text = audio.normalize_for_tts(text)
print(f"[narration] {len(text.split())} words after normalisation")

narr_path = CACHE / "narration.wav"
beats_path = CACHE / "beats.json"

# 1. TTS
t0 = time.time()
print(f"[1/3] TTS via {cfg['tts_provider']} voice={cfg['tts_voice']}…")
audio.synthesize(
    text,
    voice=cfg["tts_voice"],
    out_path=narr_path,
    speed=cfg.get("tts_speed", 1.0),
    provider=cfg["tts_provider"],
    language=cfg.get("tts_language", "en"),
)
print(f"     wrote {narr_path} in {time.time()-t0:.1f}s")

# 2. ASR + beats
t0 = time.time()
asr_provider = cfg.get("asr_provider", "whisper_mlx")
print(f"[2/3] {asr_provider} word timestamps + beat split…")
whisper_words = beats_mod.transcribe_words(narr_path, provider=asr_provider)
source_aligned = align.align_source_to_whisper(text, whisper_words)
beat_list = beats_mod.split_into_beats(
    source_aligned,
    target_s=cfg.get("beat_target_s", 2.0),
    max_s=cfg.get("beat_max_s", 3.0),
)
beats_mod.save_beats(beat_list, beats_path)
total_dur = sum(b.duration for b in beat_list)
print(f"     {len(beat_list)} beats, total {total_dur:.2f}s in {time.time()-t0:.1f}s")
for i, b in enumerate(beat_list):
    print(f"       beat {i}: {b.duration:.2f}s — {b.text[:60]}")

# 3. Word captions (yellow-on-black PIL render — exactly the same code path
# that compose() uses, so styling matches the previous render).
t0 = time.time()
print(f"[3/3] prerendering word PNGs…")
n = compose.prerender_word_captions(beat_list, CACHE)
print(f"     wrote {n} word PNGs in {time.time()-t0:.1f}s")
print(f"\nNARRATION DURATION: {total_dur:.2f}s")
