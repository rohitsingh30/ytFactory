"""Judge the voice matrix — score every (cell × model) on objective metrics
and pick a winner per cell.

Metrics scored per render:
  * **transcription_accuracy** — Whisper WER vs source script (lower = better, 0=perfect)
  * **speaker_similarity** — resemblyzer cosine to reference WAV (higher = better)
  * **gender_match** — F0 detected gender matches expected (1=match, 0=mismatch)
  * **audio_health** — checks for: not constant DC, not silent, sample variety, peak amplitude
  * **rtf_score** — render-time / audio-time (lower = faster; not quality-related but
    flagged if >3.0 = unusable for production)
  * **speaking_rate** — chars-per-second; sanity check (8-15 = natural for English/Hindi)

Composite score (0-100):
  * 35% transcription_accuracy
  * 30% speaker_similarity
  * 20% gender_match (binary, 0 or 20)
  * 10% audio_health (binary, 0 or 10)
  * 5% RTF penalty (subtract up to 5 for >3.0 RTF)

Winner = highest composite score per cell. Ties broken by speaker_similarity.

Output:
  data/_bench/voice-matrix/<run>/<cell>/_winners.md  (auto-fills the rating table + picked winner)
  data/_bench/voice-matrix/<run>/_judge_summary.md  (per-channel winner picks + cross-cell stats)

Run:
  .venv/bin/python scripts/judge_voice_matrix.py --run-id 20260506-210506
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Suppress librosa/whisper warnings
import warnings
warnings.filterwarnings("ignore")


_WHISPER_MODEL_BASE = None
_WHISPER_MODEL_LARGE = None
_RESEMBLYZER_ENCODER = None


def _whisper(lang: str = "en"):
    """Lazy-load Whisper. Use base for English (fast, accurate enough)
    and large-v3 for Hindi (base has very poor Hindi WER, making
    quality scores misleading)."""
    global _WHISPER_MODEL_BASE, _WHISPER_MODEL_LARGE
    import whisper
    if lang == "hi":
        if "_WHISPER_MODEL_LARGE" not in globals() or _WHISPER_MODEL_LARGE is None:
            globals()["_WHISPER_MODEL_LARGE"] = whisper.load_model("large-v3")
        return globals()["_WHISPER_MODEL_LARGE"]
    if "_WHISPER_MODEL_BASE" not in globals() or _WHISPER_MODEL_BASE is None:
        globals()["_WHISPER_MODEL_BASE"] = whisper.load_model("base")
    return globals()["_WHISPER_MODEL_BASE"]


def _encoder():
    global _RESEMBLYZER_ENCODER
    if _RESEMBLYZER_ENCODER is None:
        from resemblyzer import VoiceEncoder
        _RESEMBLYZER_ENCODER = VoiceEncoder()
    return _RESEMBLYZER_ENCODER


def _wer(reference: str, hypothesis: str) -> float:
    """Word Error Rate. Tokenizes by whitespace + lowercases.
    Accepts garbage transcripts → returns 1.0 (100% error)."""
    ref_words = re.findall(r"\w+", reference.lower())
    hyp_words = re.findall(r"\w+", hypothesis.lower())
    if not ref_words:
        return 1.0
    # Levenshtein on word lists
    n, m = len(ref_words), len(hyp_words)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[n][m] / n


def _f0(wav_path: Path) -> float:
    """Median F0 in Hz; <165 = male, >165 = female (rough)."""
    import librosa
    import numpy as np
    y, sr = librosa.load(str(wav_path), sr=None)
    f0 = librosa.yin(y, fmin=70, fmax=400, sr=sr)
    fc = f0[~np.isnan(f0) & (f0 > 50)]
    return float(np.median(fc)) if len(fc) else 0.0


def _audio_health(wav_path: Path) -> tuple[bool, str]:
    """Returns (is_healthy, reason_if_not)."""
    import soundfile as sf
    import numpy as np
    try:
        y, sr = sf.read(str(wav_path))
    except Exception as e:
        return False, f"read_err:{type(e).__name__}"
    if len(y) == 0:
        return False, "empty"
    duration = len(y) / sr
    if duration < 1.0:
        return False, f"too_short:{duration:.1f}s"
    if y.std() < 0.01:
        return False, f"flat_signal_std={y.std():.4f}"
    if len(np.unique(y)) < 100:
        return False, f"low_variety:{len(np.unique(y))}"
    peak = np.abs(y).max()
    if peak > 0.999:
        return False, f"clipped:{peak:.3f}"
    if peak < 0.01:
        return False, f"too_quiet:{peak:.3f}"
    return True, ""


def _speaker_sim(ref_wav: Path, gen_wav: Path) -> float:
    """Cosine similarity of resemblyzer speaker embeddings (0-1)."""
    import numpy as np
    from resemblyzer import preprocess_wav
    encoder = _encoder()
    try:
        ref_emb = encoder.embed_utterance(preprocess_wav(str(ref_wav)))
        gen_emb = encoder.embed_utterance(preprocess_wav(str(gen_wav)))
        # cosine
        return float(np.dot(ref_emb, gen_emb) /
                     (np.linalg.norm(ref_emb) * np.linalg.norm(gen_emb)))
    except Exception:
        return 0.0


def _is_male_archetype(cell_name: str) -> bool:
    return "_male_" in cell_name and "_female_" not in cell_name


def score_cell_render(ref_wav: Path, gen_wav: Path, expected_text: str,
                      cell_name: str, lang: str) -> dict:
    """Score one (cell, model) render and return all sub-scores + composite."""
    is_male = _is_male_archetype(cell_name)

    # 1. transcription accuracy via WER
    try:
        whisper_lang = lang  # 'en' or 'hi'
        result = _whisper(whisper_lang).transcribe(
            str(gen_wav), language=whisper_lang,
            verbose=False, fp16=False,
        )
        transcript = (result.get("text") or "").strip()
        wer = _wer(expected_text, transcript)
    except Exception as e:
        transcript = ""
        wer = 1.0
    transcription_score = max(0.0, 1.0 - wer)  # 1.0 = perfect

    # 2. speaker similarity
    try:
        sim = _speaker_sim(ref_wav, gen_wav)
    except Exception:
        sim = 0.0

    # 3. gender match
    f0 = _f0(gen_wav)
    detected_male = f0 < 165 and f0 > 0
    gender_match = (detected_male == is_male)

    # 4. audio health
    healthy, health_reason = _audio_health(gen_wav)

    # 5. duration + speaking rate (chars per second)
    import wave
    try:
        with wave.open(str(gen_wav)) as w:
            duration = w.getnframes() / w.getframerate()
    except Exception:
        duration = 0.0
    chars_per_s = len(expected_text) / max(duration, 0.1)
    # Natural English: ~12-18 cps; Hindi (Devanagari): ~10-16 cps
    cps_natural = 8.0 <= chars_per_s <= 20.0

    # ---- composite ----
    composite = (
        35.0 * transcription_score +
        30.0 * sim +
        (20.0 if gender_match else 0.0) +
        (10.0 if healthy else 0.0) +
        (5.0 if cps_natural else 0.0)
    )

    return {
        "wer": round(wer, 3),
        "transcription_score": round(transcription_score, 3),
        "transcript_excerpt": transcript[:80],
        "speaker_sim": round(sim, 3),
        "f0": round(f0, 0),
        "expected_male": is_male,
        "gender_match": gender_match,
        "healthy": healthy,
        "health_reason": health_reason,
        "duration_s": round(duration, 2),
        "chars_per_s": round(chars_per_s, 1),
        "composite": round(composite, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    run_dir = ROOT / "data" / "_bench" / "voice-matrix" / args.run_id
    if not run_dir.exists():
        print(f"ERROR: {run_dir} not found")
        sys.exit(1)

    print(f"=== judging matrix at {run_dir.relative_to(ROOT)} ===")
    print()

    cells = sorted(d for d in run_dir.iterdir() if d.is_dir())
    all_scores: dict[str, dict[str, dict]] = {}  # cell -> model -> scores

    for cell_dir in cells:
        cell_name = cell_dir.name
        ref_wav = cell_dir / "_ref.wav"
        script_path = cell_dir / "_script.txt"
        if not (ref_wav.exists() and script_path.exists()):
            continue
        expected_text = script_path.read_text(encoding="utf-8").strip()
        lang = "hi" if "hindutavaanimated" in cell_name else "en"

        models_present = sorted(
            f.stem for f in cell_dir.glob("*.wav")
            if not f.name.startswith("_")
        )
        print(f"[cell] {cell_name}  (lang={lang}, {len(models_present)} models)")
        cell_scores: dict[str, dict] = {}
        for model in models_present:
            gen_wav = cell_dir / f"{model}.wav"
            scores = score_cell_render(
                ref_wav, gen_wav, expected_text, cell_name, lang,
            )
            cell_scores[model] = scores
            print(f"  {model:18s}  composite={scores['composite']:5.1f}  "
                  f"wer={scores['wer']:.2f}  sim={scores['speaker_sim']:.2f}  "
                  f"gender={'✓' if scores['gender_match'] else '❌'}  "
                  f"health={'✓' if scores['healthy'] else '❌(' + scores['health_reason'] + ')'}")

        all_scores[cell_name] = cell_scores

        # ---- write per-cell _winners.md ----
        winner = max(cell_scores.items(),
                     key=lambda kv: (kv[1]["composite"], kv[1]["speaker_sim"]))
        wmd = cell_dir / "_winners.md"
        lines = [
            f"# {cell_name}",
            "",
            "_Reference voice:_ `_ref.wav`",
            "_Script (synthesised below):_ `_script.txt`",
            "",
            "## Auto-judge scores",
            "",
            "| Model | Composite | WER↓ | Speaker Sim↑ | Gender | Health | Duration | Chars/s |",
            "|---|---:|---:|---:|---|---|---:|---:|",
        ]
        for model, s in sorted(cell_scores.items(),
                                key=lambda kv: -kv[1]["composite"]):
            mark = " 🏆" if model == winner[0] else ""
            lines.append(
                f"| {model}{mark} | **{s['composite']:.1f}** | "
                f"{s['wer']:.2f} | {s['speaker_sim']:.2f} | "
                f"{'✓' if s['gender_match'] else '❌'} F0={s['f0']:.0f} | "
                f"{'✓' if s['healthy'] else '❌ ' + s['health_reason']} | "
                f"{s['duration_s']:.1f}s | {s['chars_per_s']:.1f} |"
            )
        lines += [
            "",
            "## Auto-pick: 🏆 **{}** (composite {:.1f})".format(
                winner[0], winner[1]["composite"]),
            "",
            f"- Transcript heard: \"{winner[1]['transcript_excerpt']}\"",
            "- Speaker similarity to ref: **{:.2f}** (0=different person, 1=identical)".format(
                winner[1]["speaker_sim"]),
            "- Gender match: {}".format(
                "✓ correct" if winner[1]["gender_match"] else "❌ wrong gender"),
            "",
            "## Manual override (after listening)",
            "",
            "**Picked model:** _ _",
            "**Why:** _ _",
            "",
        ]
        wmd.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- run-level judge summary ----
    summary = run_dir / "_judge_summary.md"
    lines = [
        f"# Voice matrix run {args.run_id} — auto-judge picks",
        "",
        "_Auto-judged using: Whisper WER (35%), resemblyzer speaker sim (30%), "
        "gender match (20%), audio health (10%), speaking rate (5%)._",
        "",
        "## Winner per channel × format × archetype",
        "",
        "| Cell | Winner | Composite | WER | Speaker Sim |",
        "|---|---|---:|---:|---:|",
    ]
    by_channel: dict[str, list[tuple[str, str, dict]]] = {}
    for cell_name, scores in all_scores.items():
        if not scores:
            continue
        winner_model, winner_scores = max(
            scores.items(),
            key=lambda kv: (kv[1]["composite"], kv[1]["speaker_sim"]),
        )
        channel = cell_name.split("__")[0]
        by_channel.setdefault(channel, []).append(
            (cell_name, winner_model, winner_scores),
        )
        lines.append(
            f"| {cell_name} | **{winner_model}** | "
            f"{winner_scores['composite']:.1f} | {winner_scores['wer']:.2f} | "
            f"{winner_scores['speaker_sim']:.2f} |"
        )

    lines += [
        "",
        "## Model-level scoreboard (across all cells)",
        "",
        "| Model | Wins | Avg composite | Avg WER | Avg speaker sim |",
        "|---|---:|---:|---:|---:|",
    ]
    model_wins: dict[str, int] = {}
    model_composites: dict[str, list[float]] = {}
    model_wers: dict[str, list[float]] = {}
    model_sims: dict[str, list[float]] = {}
    for cell_name, scores in all_scores.items():
        if not scores:
            continue
        winner_model, _ = max(
            scores.items(),
            key=lambda kv: (kv[1]["composite"], kv[1]["speaker_sim"]),
        )
        model_wins[winner_model] = model_wins.get(winner_model, 0) + 1
        for m, s in scores.items():
            model_composites.setdefault(m, []).append(s["composite"])
            model_wers.setdefault(m, []).append(s["wer"])
            model_sims.setdefault(m, []).append(s["speaker_sim"])

    all_models = sorted(model_composites.keys(),
                        key=lambda m: -sum(model_composites[m]) / len(model_composites[m]))
    for m in all_models:
        composites = model_composites[m]
        wers = model_wers[m]
        sims = model_sims[m]
        lines.append(
            f"| {m} | {model_wins.get(m, 0)} | "
            f"{sum(composites)/len(composites):.1f} | "
            f"{sum(wers)/len(wers):.2f} | "
            f"{sum(sims)/len(sims):.2f} |"
        )

    lines += [
        "",
        "## Per-channel notes",
        "",
    ]
    for channel, picks in sorted(by_channel.items()):
        lines.append(f"### {channel}")
        lines.append("")
        for cell_name, model, s in picks:
            archetype = "/".join(cell_name.split("__")[1:])
            lines.append(
                f"- `{archetype}` → **{model}** (composite {s['composite']:.1f}, "
                f"sim {s['speaker_sim']:.2f}, gender {'✓' if s['gender_match'] else '❌'})"
            )
        lines.append("")

    lines += [
        "## Caveats",
        "",
        "- This is **objective scoring only** — does not capture taste, emotional",
        "  delivery, naturalness of pacing, or production polish.",
        "- Pickings should be validated by listening to the WAVs.",
        "- WER assumes Whisper-base transcription is accurate (it usually is for ",
        "  English; Hindi WER may be inflated due to Whisper's weaker Hindi).",
        "- Speaker similarity uses resemblyzer; >0.7 = strong clone, 0.5-0.7 = OK,",
        "  <0.5 = weak clone (different speaker character).",
        "",
    ]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Save raw scores JSON
    (run_dir / "_judge_scores.json").write_text(
        json.dumps(all_scores, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"=== judging done ===")
    print(f"Per-cell _winners.md written for {len(all_scores)} cells")
    print(f"Summary: {summary.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
