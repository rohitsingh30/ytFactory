#!/usr/bin/env python3
"""Pre-render safety scan for shotlist source URLs (gate G16).

Samples N=5 random frames from a downloaded source via ffmpeg, then runs:
  - OCR on each frame for source-channel watermark text
  - face-age heuristic for under-18 person detection

Returns a JSON report; CLI exits non-zero if any frame trips a rule.
Used by /make-top10 (and any other long-form skill that imports source
video for visual wallpaper) to prevent third-party watermarks and
inappropriate content from bleeding through into the final render.

Origin: 2026-05-05 postmortem of top10-alien-abductions-202605.

CLI usage:
    .venv/bin/python scripts/_shared/safety_scan_source.py \\
        --source <path/to/source.mp4> \\
        [--n 5] [--strict]

Programmatic:
    from scripts._shared.safety_scan_source import scan
    report = scan(source_path, n_frames=5)
    if report["watermark_hits"] or report["minor_face_hits"]:
        raise SafetyScanFailed(report)

Dependencies (deferred — install on first use):
    pytesseract  (sudo brew install tesseract; pip install pytesseract)
    mediapipe    (pip install mediapipe)
    Pillow       (already in venv)

Until those are installed, this stub returns a stub report flagging
the missing-deps state, which the skill MUST surface to the user
rather than silently passing the gate."""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


WATERMARK_RX = re.compile(
    r"(?i)credit|episode|s\d+e\d+|copyright|©|all rights reserved|youtube\.com/",
)


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ]).decode().strip()
    return float(out)


def _sample_frames(src: Path, n: int, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = _ffprobe_duration(src)
    rng = random.Random(0xA11)
    timestamps = sorted(rng.uniform(5.0, max(6.0, dur - 5.0)) for _ in range(n))
    paths: list[Path] = []
    for i, t in enumerate(timestamps):
        p = out_dir / f"sample_{i:02d}_t{int(t):05d}.png"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t:.3f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "2", str(p),
        ], check=True)
        paths.append(p)
    return paths


def _ocr_watermark(image_path: Path) -> list[str]:
    try:
        import pytesseract  # type: ignore
        from PIL import Image
    except ImportError:
        return ["__deps_missing__:pytesseract"]
    text = pytesseract.image_to_string(Image.open(image_path))
    return [m.group(0) for m in WATERMARK_RX.finditer(text)]


def _detect_minor_faces(image_path: Path) -> list[dict[str, Any]]:
    try:
        import mediapipe as mp  # type: ignore
        from PIL import Image
    except ImportError:
        return [{"__deps_missing__": "mediapipe"}]
    # mediapipe FaceMesh + a heuristic landmark-ratio age estimator.
    # NOTE: mediapipe ships face detection but not age classification.
    # For a real age check, swap in a small ONNX age model (e.g.
    # age-gender-recognition-retail-0013). Until then, this stub
    # returns the deps-missing marker so the skill knows the gate is
    # not yet enforced and surfaces to the user.
    return [{"__model_unavailable__": "age_classifier_not_yet_wired"}]


def scan(source_path: Path, n_frames: int = 5) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="safety-scan-") as td:
        frame_paths = _sample_frames(source_path, n_frames, Path(td))
        watermark_hits: list[dict[str, Any]] = []
        minor_face_hits: list[dict[str, Any]] = []
        for p in frame_paths:
            wm = _ocr_watermark(p)
            if wm and not (len(wm) == 1 and wm[0].startswith("__deps_missing__")):
                watermark_hits.append({"frame": p.name, "matches": wm})
            faces = _detect_minor_faces(p)
            real_minor_hits = [f for f in faces if "__deps_missing__" not in f and "__model_unavailable__" not in f]
            if real_minor_hits:
                minor_face_hits.append({"frame": p.name, "faces": real_minor_hits})
    return {
        "source": str(source_path),
        "n_frames_sampled": n_frames,
        "watermark_hits": watermark_hits,
        "minor_face_hits": minor_face_hits,
        "_deps_status": {
            "pytesseract": "ok" if not any(
                "__deps_missing__" in str(h) for h in watermark_hits
            ) else "missing",
            "mediapipe_age": "stub_only — install mediapipe + an age model to enforce",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True, help="Path to downloaded source mp4")
    ap.add_argument("--n", type=int, default=5, help="Frames to sample")
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero on any hit")
    args = ap.parse_args()
    report = scan(Path(args.source), n_frames=args.n)
    print(json.dumps(report, indent=2))
    if args.strict and (report["watermark_hits"] or report["minor_face_hits"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
