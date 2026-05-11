"""Reproducible procedural music beds for the studio Customize step.

Run once after a fresh checkout (or any time you want to bump the
catalogue):

    python scripts/build_music_beds.py

Outputs MP3 loops (12-15s, mono, 128k) under ``data/music/<key>.mp3``.
The /api/music/catalog FastAPI route picks them up automatically — no
schema sync needed.

Why ffmpeg-synthesised? The control plane treats ``data/music/`` as a
shared procedural catalogue (see control/routes/music_routes.py
``_scan_shared``). The five originals (off / ambient_low / ambient_med
/ cinematic / upbeat) were authored the same way; this script just
extends the palette to ≥10 so the create page has more variety.

Each bed is ≤15s and loops cleanly enough for short-form background
use. The renderer treats the file as a tile; long renders extend by
``aloop=loop=-1:size=...`` at compose time.

Idempotent: skips any bed file that already exists. Pass --force to
re-synthesise the entire catalogue (the originals included).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MUSIC_DIR = REPO_ROOT / "data" / "music"


def _ffmpeg(filter_complex: str, out_path: Path, *, bitrate_k: int = 128) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-filter_complex", filter_complex,
        "-map", "[a]",
        "-ac", "1", "-ar", "44100",
        "-c:a", "libmp3lame", "-b:a", f"{bitrate_k}k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Catalogue. Each entry is (key, label-for-humans, ffmpeg filter_complex).
#
# All five new beds are intentionally simple (a few sines + one or two
# fx). The point is *vibe variety*, not production polish — at music_bed_db
# = -28 dB under narration the listener perceives mood, not melody.
# ---------------------------------------------------------------------------

_DUR = 12.0


def _filter(parts: str, *, dur: float = _DUR) -> str:
    return parts.format(d=dur)


_NEW_BEDS: dict[str, tuple[str, str]] = {
    # Lo-fi: warm bass + soft hat texture (filtered noise) — chill,
    # study-vibes mood. Lower frequencies emphasised so it sits well
    # under spoken narration.
    "lo_fi": (
        "Lo-Fi · chill",
        _filter(
            "sine=frequency=110:duration={d}[bass];"
            "sine=frequency=165:duration={d}[harm];"
            "anoisesrc=color=brown:duration={d}:amplitude=0.15[noise];"
            "[bass][harm][noise]amix=inputs=3:duration=longest:weights=1.0 0.5 0.4,"
            "lowpass=f=600,aecho=0.6:0.4:280:0.3,volume=-20dB[a]"
        ),
    ),
    # Mysterious: low drone + sparse bell harmonic. Detective / mystery
    # / unresolved-thriller mood for AITA reveal-style stories.
    "mysterious": (
        "Mysterious · sparse",
        _filter(
            "sine=frequency=90:duration={d}[drone];"
            "sine=frequency=523:duration={d}[bell1];"
            "sine=frequency=659:duration={d}[bell2];"
            "[bell1][bell2]amix=inputs=2:duration=longest:weights=0.5 0.4,"
            "atempo=0.7,aecho=0.7:0.6:1100:0.5[bells];"
            "[drone][bells]amix=inputs=2:duration=longest:weights=1.0 0.6,"
            "lowpass=f=900,volume=-22dB[a]"
        ),
    ),
    # Tense: heart-beat low pulse + sub bass + slow tremor. Builds
    # dread without going full horror — works under crime / true-crime.
    "tense": (
        "Tense · pulse",
        _filter(
            "sine=frequency=55:duration={d}[sub];"
            "sine=frequency=82:duration={d}[low];"
            "[sub][low]amix=inputs=2:duration=longest:weights=1.0 0.6,"
            "tremolo=f=1.6:d=0.7,volume=-21dB[a]"
        ),
    ),
    # Dreamy: high airy pad + chimes. Romance / nostalgia / wholesome
    # reveal mood. Detuned major thirds for a shimmer.
    "dreamy": (
        "Dreamy · airy",
        _filter(
            "sine=frequency=440:duration={d}[a1];"
            "sine=frequency=554:duration={d}[a2];"
            "sine=frequency=659:duration={d}[a3];"
            "sine=frequency=880:duration={d}[a4];"
            "[a1][a2][a3][a4]amix=inputs=4:duration=longest:weights=1.0 0.6 0.5 0.3,"
            "aecho=0.8:0.7:600:0.5,lowpass=f=4000,volume=-24dB[a]"
        ),
    ),
    # Dark: sub-bass + low minor strings (sine fifth a tritone away).
    # Threatening / horror / unresolved-mystery mood.
    "dark": (
        "Dark · ominous",
        _filter(
            "sine=frequency=49:duration={d}[sub];"
            "sine=frequency=73:duration={d}[s1];"
            "sine=frequency=104:duration={d}[s2];"
            "[sub][s1][s2]amix=inputs=3:duration=longest:weights=1.2 0.7 0.5,"
            "lowpass=f=400,aecho=0.7:0.5:1500:0.5,volume=-20dB[a]"
        ),
    ),
}


def synth_one(key: str, *, force: bool = False) -> Path | None:
    if key not in _NEW_BEDS:
        raise KeyError(f"unknown bed: {key!r}")
    label, flt = _NEW_BEDS[key]
    out = MUSIC_DIR / f"{key}.mp3"
    if out.exists() and not force:
        print(f"[skip] {key}.mp3 exists (use --force to regenerate)")
        return None
    _ffmpeg(flt, out)
    print(f"[ok]   {key}.mp3 — {label}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="Re-synthesise even if the file already exists.")
    ap.add_argument("--only", nargs="*", default=None,
                    help="Synthesise only these keys (default: all in the catalogue).")
    args = ap.parse_args()

    keys = args.only or list(_NEW_BEDS.keys())
    for k in keys:
        synth_one(k, force=args.force)
    print(f"[done] catalogue at {MUSIC_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
