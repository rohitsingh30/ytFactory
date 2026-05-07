"""Pull Hindi voice references from AI4Bharat IndicVoices-R (CC-BY-4.0).

Strategy: scan multiple Hindi parquet shards, filter for clean clips
(high SNR ≥40dB, age 25-60, duration 8-15s), pick voices that match
hindutavaanimated archetypes:

  hindi_male_storyteller_warm    — male, 30-50, mid-pitch, calm
  hindi_male_grandfather         — male, 45-60, low-pitch, slow
  hindi_female_storyteller       — female, 30-50, expressive
  hindi_female_meditation_calm   — female, 35-55, low-pitch (calm), slow

Each saves to:
  pipeline/voice_refs/bench/hindutavaanimated__<format>__<voice_id>/ref.wav
  pipeline/voice_refs/bench/hindutavaanimated__<format>__<voice_id>/ref.txt
  pipeline/voice_refs/bench/hindutavaanimated__<format>__<voice_id>/_source.txt

Run:
  .venv/bin/python scripts/pull_hindi_voices.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "pipeline" / "voice_refs" / "bench"


# Filter criteria for picking clean voice samples
MIN_SNR_DB = 40.0
MIN_DURATION_S = 8.0
MAX_DURATION_S = 18.0


# Targets: (channel, format, voice_id, profile, gender, pitch_min, pitch_max, age_groups)
# Pitch ranges (Hindi avg): male 90-160 Hz, female 180-280 Hz
TARGETS = [
    {
        "channel": "hindutavaanimated", "format": "shorts",
        "voice_id": "hindi_male_storyteller_warm",
        "profile": "Hindi male announcer-quality voice (SNR 70+dB, low pitch var, measured pace)",
        "gender": "Male", "pitch_min": 120, "pitch_max": 165,
        "pitch_std_max": 25, "min_snr": 65,
        "speaking_rate_min": 9.0, "speaking_rate_max": 11.5,
        "age_groups": ["30-45", "18-30"], "preferred_age": "30-45",
    },
    {
        "channel": "hindutavaanimated", "format": "long_form",
        "voice_id": "hindi_male_grandfather_calm",
        "profile": "Hindi male elder, deep voice (announcer-grade, SNR 65+, low std)",
        "gender": "Male", "pitch_min": 90, "pitch_max": 130,
        "pitch_std_max": 25, "min_snr": 65,
        "speaking_rate_min": 9.0, "speaking_rate_max": 11.0,
        "age_groups": ["45-60", "60+", "30-45"], "preferred_age": "45-60",
    },
    {
        "channel": "hindutavaanimated", "format": "shorts",
        "voice_id": "hindi_female_storyteller",
        "profile": "Hindi female narrator, expressive announcer-quality",
        "gender": "Female", "pitch_min": 200, "pitch_max": 245,
        "pitch_std_max": 35, "min_snr": 65,
        "speaking_rate_min": 9.5, "speaking_rate_max": 11.5,
        "age_groups": ["30-45", "18-30"], "preferred_age": "30-45",
    },
    {
        "channel": "hindutavaanimated", "format": "long_form",
        "voice_id": "hindi_female_meditation_calm",
        "profile": "Hindi female calm voice for sleep/meditation (announcer-grade)",
        "gender": "Female", "pitch_min": 180, "pitch_max": 215,
        "pitch_std_max": 30, "min_snr": 65,
        "speaking_rate_min": 9.0, "speaking_rate_max": 11.0,
        "age_groups": ["45-60", "30-45"], "preferred_age": "45-60",
    },
]


def _ffmpeg_save_wav(audio_bytes: bytes, dest: Path) -> None:
    """Convert raw audio bytes (parquet stores wav-encoded bytes) → 24kHz mono WAV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("/tmp") / f"_iv_raw_{dest.parent.name}.wav"
    tmp.write_bytes(audio_bytes)
    cmd = [
        "ffmpeg", "-y", "-i", str(tmp),
        "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg fail: {res.stderr[-300:]}")
    tmp.unlink(missing_ok=True)


def _whisper_transcribe(wav: Path) -> str:
    try:
        import whisper
        model = whisper.load_model("base")
        r = model.transcribe(str(wav), language="hi")
        return (r.get("text") or "").strip()
    except Exception as e:
        return f"<TRANSCRIBE_ERR: {e}>"


def find_voice_for_target(rows: list, target: dict) -> dict | None:
    """Filter rows down to clean clips matching the archetype.

    Updated 2026-05-07: now uses announcer-quality filters (min SNR,
    max pitch std, speaking-rate range) so the picked voices have a
    professional measured cadence — closer to a public-broadcast
    narrator than a casual conversational sample.
    """
    candidates = []
    min_snr = target.get("min_snr", MIN_SNR_DB)
    pitch_std_max = target.get("pitch_std_max", 999)
    sr_min = target.get("speaking_rate_min", 0)
    sr_max = target.get("speaking_rate_max", 999)
    for r in rows:
        if r["gender"] != target["gender"]:
            continue
        if r["age_group"] not in target["age_groups"]:
            continue
        if r["snr"] < min_snr:
            continue
        if not (MIN_DURATION_S <= r["duration"] <= MAX_DURATION_S):
            continue
        pitch = r.get("utterance_pitch_mean", 0)
        if not (target["pitch_min"] <= pitch <= target["pitch_max"]):
            continue
        if r.get("utterance_pitch_std", 999) > pitch_std_max:
            continue
        sr = r.get("speaking_rate", 0)
        if not (sr_min <= sr <= sr_max):
            continue
        candidates.append(r)

    if not candidates:
        return None

    # Sort: prefer matching age + higher SNR + lower pitch std
    def score(r):
        age_match = 1 if r["age_group"] == target["preferred_age"] else 0
        return (age_match, r["snr"], -r.get("utterance_pitch_std", 0))

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def main():
    HF_TOKEN = os.environ.get("HF_TOKEN") or (
        Path.home() / ".cache" / "huggingface" / "token"
    ).read_text().strip()

    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    print("=== AI4Bharat IndicVoices-R Hindi voice extraction ===")
    print()

    # Pull more train shards (test is too small for filtering with strict announcer criteria)
    shards_to_scan = [
        "Hindi/test-00000-of-00002.parquet",
        *[f"Hindi/train-{i:05d}-of-00099.parquet" for i in range(0, 8)],
    ]

    all_rows: list[dict] = []
    for shard in shards_to_scan:
        print(f"Loading {shard}...")
        try:
            p = hf_hub_download(
                "ai4bharat/indicvoices_r", shard,
                repo_type="dataset", token=HF_TOKEN,
            )
            t = pq.read_table(p)
            df = t.to_pandas()
            for _, row in df.iterrows():
                d = row.to_dict()
                # Audio is dict {bytes, path, sampling_rate}
                all_rows.append({
                    "speaker_id": d["speaker_id"],
                    "gender": d["gender"],
                    "age_group": d["age_group"],
                    "duration": float(d["duration"]),
                    "snr": float(d["snr"]),
                    "utterance_pitch_mean": float(d.get("utterance_pitch_mean", 0)),
                    "utterance_pitch_std": float(d.get("utterance_pitch_std", 0)),
                    "speaking_rate": float(d.get("speaking_rate", 0)),
                    "verbatim": d["verbatim"],
                    "audio_bytes": d["audio"]["bytes"],
                    "shard": shard,
                })
            print(f"  loaded {len(df)} rows; running total: {len(all_rows)}")
        except Exception as e:
            print(f"  FAIL {shard}: {type(e).__name__}: {str(e)[:120]}")

    print()
    print(f"Total rows scanned: {len(all_rows)}")
    print()

    # Pick 1 voice per target
    used_speakers = set()
    n_ok = n_fail = 0
    for target in TARGETS:
        # Filter, but exclude already-picked speakers
        # (so the 4 voices come from 4 different people, not all from one chatty subject)
        eligible = [r for r in all_rows if r["speaker_id"] not in used_speakers]
        pick = find_voice_for_target(eligible, target)
        if pick is None:
            print(f"  [FAIL] {target['voice_id']:35s} no clip matches "
                  f"(gender={target['gender']}, age={target['age_groups']}, "
                  f"pitch={target['pitch_min']}-{target['pitch_max']})")
            n_fail += 1
            continue

        used_speakers.add(pick["speaker_id"])
        cell_dir = BENCH_DIR / f"{target['channel']}__{target['format']}__{target['voice_id']}"
        wav_dest = cell_dir / "ref.wav"
        try:
            _ffmpeg_save_wav(pick["audio_bytes"], wav_dest)
            (cell_dir / "ref.txt").write_text(pick["verbatim"])
            (cell_dir / "_source.txt").write_text(
                f"Source: AI4Bharat IndicVoices-R\n"
                f"License: CC-BY-4.0\n"
                f"Speaker ID: {pick['speaker_id']}\n"
                f"Gender: {pick['gender']}\n"
                f"Age group: {pick['age_group']}\n"
                f"Duration: {pick['duration']:.2f}s\n"
                f"SNR: {pick['snr']:.1f} dB\n"
                f"Pitch: {pick['utterance_pitch_mean']:.1f} Hz\n"
                f"Speaking rate: {pick['speaking_rate']:.2f}\n"
                f"Shard: {pick['shard']}\n"
                f"Profile: {target['profile']}\n"
            )
            print(f"  [ok ] {target['voice_id']:35s} "
                  f"speaker={pick['speaker_id'][:14]} pitch={pick['utterance_pitch_mean']:.0f}Hz "
                  f"snr={pick['snr']:.0f}dB age={pick['age_group']}")
            n_ok += 1
        except Exception as e:
            print(f"  [FAIL] {target['voice_id']:35s} {type(e).__name__}: {str(e)[:120]}")
            n_fail += 1

    print()
    print(f"=== done: {n_ok} ok, {n_fail} failed ===")


if __name__ == "__main__":
    main()
