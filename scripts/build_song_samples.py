"""One-shot helper to generate Female + Male Suno song samples for the
SongPicker preview tiles. Idempotent — skips files that already exist
(use --force to regenerate).

Usage:
    SUNOAPI_API_KEY=... python scripts/build_song_samples.py
    # or rely on .env via the renderer's normal env loading
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "song_samples"

# Short, generic, English lyrics that surface vocal timbre clearly
# (sustained vowels + a chorus) without being so long the API costs
# add up. ~25-30s per generation. We use the same lyrics for both
# genders so the only audible difference is the singer.
_LYRICS = """\
[Verse 1]
Morning light, the city wakes up slow
Coffee steam, watching it rise and go
Take my time, let the wind blow my way
Today's the kind of day

[Chorus]
Oh I'm alright, I'm alright
Sun on my face, world feels so bright
Oh I'm alright, I'm alright
Singing along to the rhythm tonight
"""

_STYLE = (
    "warm acoustic indie pop, gentle steel-string guitar, "
    "soft brushed drums, 92 BPM, intimate vocal, daytime"
)


def _load_dotenv() -> None:
    """Best-effort .env loader (no python-dotenv dep)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def synth_one(gender: str, *, force: bool = False) -> Path | None:
    if gender not in ("f", "m"):
        raise ValueError(f"gender must be 'f' or 'm', got {gender!r}")
    name = "female" if gender == "f" else "male"
    out_mp3 = OUT_DIR / f"{name}.mp3"
    if out_mp3.exists() and not force:
        print(f"[skip] {out_mp3.relative_to(REPO_ROOT)} exists "
              f"(use --force to regenerate)")
        return None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    from pipeline.tts.song import synth_via_sunoapi  # noqa: PLC0415

    # synth_via_sunoapi writes .wav unconditionally if out_path ends
    # in .wav (transcodes from the downloaded .mp3). For preview
    # bytes we want the small .mp3 the API returned directly. Pass
    # an .mp3 out_path so the helper just renames the download.
    print(f"[suno] generating {name} sample (vocal_gender={gender})…")
    result = synth_via_sunoapi(
        lyrics=_LYRICS,
        style=_STYLE,
        out_path=out_mp3,
        title=f"ytFactory preview — {name}",
        model="V4_5",
        vocal_gender=gender,
        poll_timeout_s=420,
    )
    print(f"[ok]   {result.relative_to(REPO_ROOT)} ({result.stat().st_size:,} bytes)")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="Re-synthesise even if files already exist.")
    ap.add_argument("--only", choices=("f", "m"), default=None,
                    help="Generate only one gender.")
    args = ap.parse_args()

    _load_dotenv()
    if not os.environ.get("SUNOAPI_API_KEY"):
        print("ERROR: SUNOAPI_API_KEY not set (in env or .env)", file=sys.stderr)
        return 2

    genders = [args.only] if args.only else ["f", "m"]
    for g in genders:
        synth_one(g, force=args.force)
    print(f"[done] samples at {OUT_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
