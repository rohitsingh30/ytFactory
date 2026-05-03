"""Clone a YouTube speaker's voice for use as a per-story F5-TTS narrator.

Example:

    .venv/bin/python scripts/clone_voice.py \\
        --url 'https://www.youtube.com/watch?v=XXXX' \\
        --channel aita_animated \\
        --slug aita-birth-pool \\
        --start 12 --duration 10

Pick a window where the protagonist is talking solo — no music, no
overlapping speakers. 10 seconds of clean speech is ideal; 5–15 is the
F5-TTS-MLX sweet spot. After this finishes, ``make_shorts.py`` will
auto-pick up the per-story voice the next time you render that slug
(no flags needed — it's discovered the same way per-story cast is).
"""

from __future__ import annotations

import argparse

from pipeline import voice_clone


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="YouTube URL of source video")
    ap.add_argument("--channel", required=True, help="Channel name (e.g. aita_animated)")
    ap.add_argument("--slug", required=True, help="Story slug to attach this voice to")
    ap.add_argument("--start", type=float, default=0.0, help="Start time in seconds (default 0)")
    ap.add_argument("--duration", type=float, default=10.0, help="Clip length 5–15s (default 10)")
    ap.add_argument(
        "--asr-provider", default="whisper_mlx",
        choices=["whisper_mlx", "whisper_mlx_base", "parakeet_mlx"],
        help="ASR backend for the reference transcript",
    )
    args = ap.parse_args()

    cloned = voice_clone.clone_from_youtube(
        args.url,
        channel=args.channel,
        slug=args.slug,
        start=args.start,
        duration=args.duration,
        asr_provider=args.asr_provider,
    )
    print()
    print(f"ref_wav:  {cloned.ref_wav}")
    print(f"ref_text: {cloned.ref_text}")
    print()
    print("next:")
    print(f"  .venv/bin/python scripts/make_shorts.py --channel channels/{args.channel}.yaml \\")
    print(f"      --script data/intermediate/{args.channel}/scripts/{args.slug}.json")


if __name__ == "__main__":
    main()
