"""A/B bench — F5-TTS-MLX vs Chatterbox on the same sentences.

Renders representative scripts from each ytFactory channel through
both providers, using the same `sarah.wav` reference clip, and writes
both WAVs side-by-side under `data/_bench/tts/<run_id>/` so a human
can listen and decide which provider wins per channel.

Why per-channel: F5 is calmer / book-narration; Chatterbox has the
emotion knob and tends to sound more conversational. The right
choice depends on whether the channel is AITA-drama, sleep-history,
sports-doc, or kids' rhymes — different tradeoffs each time.

Usage:
    .venv/bin/python scripts/bench_tts_f5_vs_chatterbox.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 4 representative voice samples — one per stylistic bucket on the
# channel grid. Keep each under ~25 s of speech so the bench finishes
# in minutes, not hours, on M2 Max.
SAMPLES: list[dict[str, str]] = [
    {
        "id": "01_aita_drama",
        "channel": "mystoriesanimated",
        "note": "Reddit AITA — conversational, emotionally charged",
        "text": (
            "AITA for telling my sister she can't bring her boyfriend to "
            "Thanksgiving? She's been dating him for three weeks. Three. "
            "Weeks. And she wants him at the table next to grandma like "
            "he's already family. I told her no. Now she's not speaking "
            "to me and my mom is calling me cruel."
        ),
    },
    {
        "id": "02_sleep_history",
        "channel": "historyrecapped",
        "note": "Long-form sleep-history — slow, soothing, archival",
        "text": (
            "By the autumn of nineteen forty, the skies above southern "
            "England had grown quiet again. The hum of Merlin engines no "
            "longer filled every afternoon. The few young men who had "
            "stood between an island and an empire were beginning, slowly, "
            "to be remembered as something more than pilots."
        ),
    },
    {
        "id": "03_sports_excited",
        "channel": "sportstoriesanimated",
        "note": "Sports doc — high-energy, crowd-noise narration",
        "text": (
            "Ninety three minutes and twenty seconds. Aguero takes it on "
            "his chest, swivels, and absolutely smashes it past Paddy "
            "Kenny. The Etihad erupts. Forty four years of waiting — gone. "
            "In a single touch. City are champions of England."
        ),
    },
    {
        "id": "04_cosmos_decoder",
        "channel": "cosmosdecoded",
        "note": "Physics decoder — measured, curious, mid-pace",
        "text": (
            "In nineteen nineteen, on a small island off the coast of West "
            "Africa, Arthur Eddington pointed his telescope at the eclipsed "
            "sun and waited. If Einstein was right, starlight grazing the "
            "sun would bend by one point seven five arcseconds. If Newton "
            "was right, by half that. The plates Eddington developed that "
            "night would settle the question."
        ),
    },
]

REF_WAV = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.wav"
REF_TXT = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.txt"


def _ffprobe_duration_s(path: Path) -> float:
    import subprocess

    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _render_f5(text: str, out_path: Path) -> tuple[float, float]:
    """Render via F5-TTS-MLX. Returns (wall_time_s, audio_duration_s)."""
    from pipeline.tts.f5 import _synth_f5_tts

    t0 = time.time()
    _synth_f5_tts(
        text=text,
        ref_audio_path=str(REF_WAV),
        ref_audio_text=REF_TXT.read_text().strip(),
        out_path=out_path,
        speed=1.0,
    )
    return time.time() - t0, _ffprobe_duration_s(out_path)


def _render_chatterbox(text: str, out_path: Path, exaggeration: float) -> tuple[float, float]:
    from pipeline.tts.chatterbox import _synth_chatterbox

    t0 = time.time()
    _synth_chatterbox(
        text=text,
        ref_audio_path=str(REF_WAV),
        out_path=out_path,
        speed=1.0,
        exaggeration=exaggeration,
        cfg_weight=0.5,
    )
    return time.time() - t0, _ffprobe_duration_s(out_path)


def main() -> None:
    if not REF_WAV.exists():
        raise SystemExit(f"missing reference wav: {REF_WAV}")
    if not REF_TXT.exists():
        raise SystemExit(f"missing reference txt: {REF_TXT}")

    run_id = time.strftime("%Y%m%d-%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "_bench" / "tts" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str | float]] = []
    for sample in SAMPLES:
        sid = sample["id"]
        text = sample["text"]
        print(f"\n=== {sid} ({sample['channel']}) — {sample['note']} ===")
        print(f"  text: {text[:80]}…")

        # F5 first (TTS heap is cleaner before Chatterbox loads its torch graph)
        f5_path = out_dir / f"{sid}__f5.wav"
        f5_wall, f5_dur = _render_f5(text, f5_path)
        f5_rtf = f5_wall / max(f5_dur, 0.01)
        print(f"  [f5_tts]    wall={f5_wall:5.1f}s  audio={f5_dur:5.1f}s  RTF={f5_rtf:.2f}x")

        # Chatterbox — exaggeration scales by channel mood: AITA + sports louder, sleep + decoder calmer
        if sample["channel"] in ("mystoriesanimated", "sportstoriesanimated"):
            exag = 0.65
        else:
            exag = 0.45
        cb_path = out_dir / f"{sid}__chatterbox.wav"
        cb_wall, cb_dur = _render_chatterbox(text, cb_path, exaggeration=exag)
        cb_rtf = cb_wall / max(cb_dur, 0.01)
        print(f"  [chatterbox] wall={cb_wall:5.1f}s  audio={cb_dur:5.1f}s  RTF={cb_rtf:.2f}x  (exag={exag})")

        rows.append({
            "id": sid,
            "channel": sample["channel"],
            "f5_wall_s": round(f5_wall, 2),
            "f5_audio_s": round(f5_dur, 2),
            "f5_rtf": round(f5_rtf, 3),
            "cb_wall_s": round(cb_wall, 2),
            "cb_audio_s": round(cb_dur, 2),
            "cb_rtf": round(cb_rtf, 3),
            "cb_exaggeration": exag,
        })

    # Summary table
    print("\n" + "=" * 78)
    print(f"{'sample':<24} {'channel':<22} {'f5 RTF':>8} {'cb RTF':>8}")
    print("-" * 78)
    for r in rows:
        print(f"{r['id']:<24} {r['channel']:<22} {r['f5_rtf']:>8.2f} {r['cb_rtf']:>8.2f}")
    print("=" * 78)
    print(f"\nSamples written to: {out_dir}")
    print("Listen to each pair side-by-side and pick the winner per channel.")


if __name__ == "__main__":
    main()
