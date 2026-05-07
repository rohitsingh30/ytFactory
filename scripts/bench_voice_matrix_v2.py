"""Voice decision matrix — render every (voice_ref × eligible model) cell.

For each voice cell in pipeline/voice_refs/bench/, render a sample
script using EVERY eligible cloud TTS model (cloning from that voice's
ref.wav). Output side-by-side WAVs for audition.

Output:
  data/_bench/voice-matrix/<run_id>/<voice_cell>/
    _ref.wav            (copy of the source reference voice)
    _ref.txt            (the reference transcript)
    _script.txt         (the channel-appropriate sample text rendered)
    f5.wav              (F5 cloud render — English only)
    higgs.wav           (Higgs cloud render — both English & Hindi)
    chatterbox.wav      (Chatterbox cloud render — English only)
    cosyvoice.wav       (CosyVoice 2 cloud render — English only)
    indicf5.wav         (IndicF5 cloud render — Hindi only)
    indicparler.wav     (Indic Parler cloud render — Hindi only)
    kokoro_hf_alpha.wav (local Kokoro — Hindi only fallback)
    _winners.md         (template for picking winner)

Run:
  source /tmp/cloudrun_envs.sh
  .venv/bin/python scripts/bench_voice_matrix_v2.py --parallel=6
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.tts.cloudrun import _get_id_token  # noqa: E402

REF_DIR = ROOT / "pipeline" / "voice_refs" / "bench"
BENCH_BASE = ROOT / "data" / "_bench" / "voice-matrix"


# ----------------------------------------------------------- channel scripts
# 30s for shorts, 60s for long-form. ~150 wpm → 75 / 150 words.

SCRIPTS_EN: dict[tuple[str, str], str] = {
    ("mystoriesanimated", "shorts"): (
        "AITA for telling my sister she can't bring her boyfriend to "
        "Thanksgiving? She's been dating him for three weeks. Three. "
        "Weeks. And she wants him at the table next to grandma, like "
        "he's already family. I told her no. AITA?"
    ),
    ("historyrecapped", "shorts"): (
        "It's June sixth, nineteen forty four. The largest amphibious "
        "assault in human history is about to begin. One hundred and "
        "fifty thousand Allied troops will land on five beaches in "
        "Normandy. This is the day that changed the war."
    ),
    ("historyrecapped", "long_form"): (
        "Tonight we travel back in time to the western front of the "
        "Great War. The year is nineteen seventeen. The trenches "
        "stretch from the English Channel to the Swiss border, a four "
        "hundred mile scar across Europe. Inside those trenches, "
        "millions of young men are living in conditions no human "
        "being should ever endure. Mud, rats, lice, the constant threat "
        "of shellfire. Some are barely more than boys. Tonight, we walk "
        "with them. Speak softly."
    ),
    ("sportstoriesanimated", "shorts"): (
        "Manchester City versus Liverpool. Anfield. Ninety third "
        "minute. The score is two to two. The ball comes off the "
        "crossbar and falls right at his feet. He doesn't even think. "
        "He just hits it."
    ),
    ("sportstoriesanimated", "long_form"): (
        "On the morning of October twenty seventh, nineteen ninety "
        "one, no one in the stadium could have predicted what was "
        "about to happen. The Atlanta Braves were five outs away from "
        "their first World Series in nearly four decades. The Minnesota "
        "Twins were down to their last hope. Kirby Puckett had carried "
        "this team for two months. Now, in the bottom of the eleventh "
        "inning, with the season on the line, he stepped up to the "
        "plate one more time."
    ),
    ("cosmosdecoded", "shorts"): (
        "A black hole isn't actually a hole. It's a region of space "
        "where gravity is so strong that not even light can escape. "
        "And at the centre of every galaxy, there's one of these "
        "monsters."
    ),
    ("cosmosdecoded", "long_form"): (
        "In nineteen nineteen, a British astronomer named Arthur "
        "Eddington led an expedition to the island of Principe off "
        "the west coast of Africa. He was there to test a prediction "
        "made by a then little known patent clerk in Switzerland. The "
        "prediction was that gravity would bend the path of starlight. "
        "If Eddington was right, it would prove that Einstein's theory "
        "of general relativity was correct. The world was about to "
        "find out."
    ),
    ("airecap", "shorts"): (
        "OpenAI just announced a new model that scores at the level "
        "of expert human researchers on the most challenging math "
        "problems. The cost of intelligence keeps falling."
    ),
    ("rhymetimejunction", "shorts"): (
        "Today we're going to learn about the colours of the rainbow. "
        "Red, orange, yellow, green, blue, indigo, violet. Can you "
        "say them with me? Let's clap our hands and sing along."
    ),
}

SCRIPTS_HI: dict[tuple[str, str], str] = {
    ("hindutavaanimated", "shorts"): (
        "नमस्ते मित्रों। आज हम सुनेंगे एक प्रेरणादायक कहानी। "
        "बहुत समय पहले एक छोटे से गाँव में एक बुद्धिमान बूढ़े साधु रहते थे। "
        "उनकी कहानी आज भी हमें बहुत कुछ सिखाती है।"
    ),
    ("hindutavaanimated", "long_form"): (
        "नमस्ते भक्तजनों। आज हम सुनेंगे महाभारत के एक अद्भुत प्रसंग की कथा। "
        "कुरुक्षेत्र की रणभूमि पर खड़े अर्जुन का मन उदास था। "
        "अपने सामने अपने ही गुरुजनों, बंधुओं और मित्रों को देखकर वे विचलित हो गए। "
        "उन्होंने अपना धनुष नीचे रख दिया और श्री कृष्ण से बोले, हे माधव, मैं युद्ध नहीं कर सकता। "
        "तब भगवान कृष्ण ने उन्हें जो उपदेश दिया, वही आज भगवद् गीता के नाम से जाना जाता है।"
    ),
}


# ----------------------------------------------------- model URLs (cloud)

SERVICE_ENVS: dict[str, str] = {
    "f5":          "CLOUDRUN_TTS_F5_URL",
    "higgs":       "CLOUDRUN_TTS_HIGGS_URL",
    "chatterbox":  "CLOUDRUN_TTS_CHATTERBOX_URL",
    "cosyvoice":   "CLOUDRUN_TTS_COSYVOICE_URL",
    "indicf5":     "CLOUDRUN_TTS_INDICF5_URL",
    "indicparler": "CLOUDRUN_TTS_INDICPARLER_URL",
}

MODELS_EN = ["f5", "higgs", "chatterbox", "cosyvoice"]
MODELS_HI = ["indicf5", "indicparler", "higgs", "kokoro_hf_alpha"]


def _post_synth(svc_url: str, payload: dict, timeout: int = 600) -> dict:
    tok = _get_id_token(svc_url + "/synth")
    req = urllib.request.Request(
        svc_url + "/synth",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _save_inline_wav(resp: dict, dest: Path) -> Path:
    if "output_inline" not in resp:
        if "output_gcs" in resp:
            raise RuntimeError("got GCS output, expected inline (chunks too big)")
        raise RuntimeError(f"no inline output: {list(resp)[:5]}")
    raw = base64.b64decode(resp["output_inline"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


def _wav_duration_s(path: Path) -> float:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def _render_one(model: str, text: str, ref_wav: Path,
                ref_txt: str, dest: Path) -> tuple[float, float]:
    """Render one matrix cell. Returns (wall_s, audio_s)."""
    if model == "kokoro_hf_alpha":
        from pipeline.tts.kokoro import _synth_kokoro
        t0 = time.time()
        _synth_kokoro(text=text, voice="hf_alpha", out_path=dest, speed=1.0)
        return time.time() - t0, _wav_duration_s(dest)

    env = SERVICE_ENVS[model]
    url = os.environ.get(env, "").rstrip("/")
    if not url:
        raise RuntimeError(f"{env} not set")

    if model == "indicparler":
        # Description-driven. Indic Parler has a HARD female bias for
        # Hindi — confirmed empirically 2026-05-06 across multiple
        # description variants. We render Indic Parler ONLY for female
        # archetype cells (skip male cells with NotImplementedError so
        # they're cleanly absent from the matrix rather than mislabeled).
        cell_name = dest.parent.name
        # Use word-boundary check (not substring): "_female_" contains
        # "male", so simple "_male_" in name matches BOTH genders.
        is_male = "_male_" in cell_name and "_female_" not in cell_name
        if is_male:
            raise NotImplementedError(
                "Indic Parler cannot reliably generate male Hindi voice "
                "(bias toward female; confirmed 2026-05-06). Skip this cell."
            )
        speaker_desc = (
            "Divya speaks Hindi with a clear expressive female voice, "
            "moderate pace, suitable for storytelling. "
            "Studio quality recording."
        )
        payload = {
            "model": "indicparler",
            "text": text,
            "ref_audio_b64": "",
            "ref_text": speaker_desc,
            "speed": 1.0,
            "output": "inline",
        }
    else:
        ref_b64 = base64.b64encode(ref_wav.read_bytes()).decode()
        payload = {
            "model": model,
            "text": text,
            "ref_audio_b64": ref_b64,
            "ref_text": ref_txt,
            "speed": 1.0,
            "output": "inline",
        }
    t0 = time.time()
    resp = _post_synth(url, payload, timeout=600)
    wall = time.time() - t0
    _save_inline_wav(resp, dest)
    return wall, _wav_duration_s(dest)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default=None,
                   help="Output run id (default: timestamp)")
    p.add_argument("--parallel", type=int, default=6)
    p.add_argument("--cell", default=None,
                   help="Only render this voice cell (substring match)")
    args = p.parse_args()

    run_id = args.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = BENCH_BASE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Discover voice cells
    cells = sorted(d for d in REF_DIR.iterdir()
                   if d.is_dir() and (d / "ref.wav").exists())
    if args.cell:
        cells = [c for c in cells if args.cell in c.name]
    print(f"=== voice matrix → {run_dir.relative_to(ROOT)} ===")
    print(f"Voice cells: {len(cells)}")
    print(f"Parallel workers: {args.parallel}")
    print()

    # Build job list
    jobs: list[dict] = []
    for cell_src in cells:
        cell_name = cell_src.name  # e.g. mystoriesanimated__shorts__male_dramatic_intense
        parts = cell_name.split("__", 2)
        if len(parts) != 3:
            print(f"  [SKIP] {cell_name}: malformed name")
            continue
        channel, fmt, _voice_id = parts

        # Pick script (Hindi for hindutava, English for the rest)
        is_hindi = channel == "hindutavaanimated"
        scripts = SCRIPTS_HI if is_hindi else SCRIPTS_EN
        script = scripts.get((channel, fmt))
        if not script:
            print(f"  [SKIP] {cell_name}: no script for ({channel},{fmt})")
            continue

        models = MODELS_HI if is_hindi else MODELS_EN
        ref_wav = cell_src / "ref.wav"
        ref_txt = (cell_src / "ref.txt").read_text(encoding="utf-8").strip()

        cell_dst = run_dir / cell_name
        cell_dst.mkdir(parents=True, exist_ok=True)

        # Copy context files once
        if not (cell_dst / "_ref.wav").exists():
            shutil.copy(ref_wav, cell_dst / "_ref.wav")
        (cell_dst / "_ref.txt").write_text(ref_txt, encoding="utf-8")
        (cell_dst / "_script.txt").write_text(script, encoding="utf-8")
        if (cell_src / "_source.txt").exists():
            shutil.copy(cell_src / "_source.txt", cell_dst / "_source.txt")

        for model in models:
            out = cell_dst / f"{model}.wav"
            if out.exists():
                continue
            jobs.append({
                "cell_name": cell_name,
                "model": model,
                "ref_wav": ref_wav,
                "ref_txt": ref_txt,
                "text": script,
                "dest": out,
            })

    print(f"Total cells to render: {len(jobs)}")
    print()
    if not jobs:
        return

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {
            ex.submit(_render_one, j["model"], j["text"], j["ref_wav"],
                      j["ref_txt"], j["dest"]): j
            for j in jobs
        }
        done = 0
        for fut in cf.as_completed(futs):
            j = futs[fut]
            done += 1
            label = f"{j['cell_name']:65s} {j['model']:18s}"
            try:
                wall, dur = fut.result()
                rtf = wall / dur if dur else 0
                print(f"  [{done:3d}/{len(jobs):3d}] OK   {label}  audio={dur:5.2f}s wall={wall:5.2f}s rtf={rtf:5.2f}",
                      flush=True)
                results.append({**j, "ok": True, "wall_s": wall,
                                "audio_s": dur, "rtf": rtf})
            except Exception as e:
                print(f"  [{done:3d}/{len(jobs):3d}] FAIL {label}  {type(e).__name__}: {str(e)[:140]}",
                      flush=True)
                results.append({**j, "ok": False, "err": str(e)[:200]})

    # ---- Per-cell _winners.md template ----
    by_cell: dict[Path, list[dict]] = {}
    for r in results:
        cell = r["dest"].parent
        by_cell.setdefault(cell, []).append(r)
    for cell_dir, rs in by_cell.items():
        wmd = cell_dir / "_winners.md"
        if wmd.exists():
            continue
        lines = [
            f"# {cell_dir.name}",
            "",
            "_Reference voice:_ `_ref.wav`",
            "_Script (synthesised below):_ `_script.txt`",
            "",
            "## Listening notes",
            "",
            "| Model | RTF | Quality / 5 | Production-ready? | Notes |",
            "|---|---:|---|---|---|",
        ]
        for r in sorted(rs, key=lambda r: r["model"]):
            rtf = f"{r['rtf']:.2f}" if r["ok"] else "FAIL"
            lines.append(f"| {r['model']} | {rtf} | _ /5_ | _ y/n_ | _ _ |")
        lines += ["", "## Winner", "", "**Picked model:** _ _", "**Why:** _ _", ""]
        wmd.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- Run-level summary ----
    n_ok = sum(1 for r in results if r["ok"])
    summary = run_dir / "_summary.md"
    lines = [
        f"# Voice matrix run {run_id}",
        "",
        f"- **Cells rendered:** {n_ok} ok, {len(results) - n_ok} failed (of {len(results)} attempted)",
        f"- **Voice references:** {len(cells)}",
        "",
        "## How to audition",
        "",
        "Each `<voice_cell>/` dir has:",
        "- `_ref.wav` — reference voice cloned by every model",
        "- `_script.txt` — text synthesised",
        "- `_source.txt` — provenance / license info",
        "- `<model>.wav` — one per model",
        "- `_winners.md` — fill in your ratings + winner",
        "",
        "## Failures",
        "",
    ]
    for r in results:
        if not r["ok"]:
            lines.append(f"- `{r['cell_name']}/{r['model']}.wav` — {r['err'][:120]}")
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"=== done: {n_ok}/{len(results)} ok ===")
    print(f"Summary: {summary.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
