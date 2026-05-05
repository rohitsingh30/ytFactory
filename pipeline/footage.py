"""Stage 6 (alt) — real broadcast footage clip resolver for sportstoriesanimated.

For beats with ``kind: footage``, download the source video with yt-dlp
(cached by video id), trim to a frame-accurate ``[in_s, out_s]`` window
with ffmpeg, and scale-crop to portrait 1080x1920.

The original audio is preserved so compose.py can mix it under the
narrator at a configurable level — this module does NOT duck audio
itself, because the duck factor depends on the surrounding narration
and is decided at compose time.

CLI smoke test:

    python -m pipeline.footage <url> --in 41.2 --out 44.5 \\
        --out-path data/scratch/footage_smoke.mp4
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import telemetry as _tlm


_VIDEO_ID_RE = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/embed/|/v/)([A-Za-z0-9_-]{11})"
)

DEFAULT_CACHE_DIR = Path("sportstoriesanimated/footage/sources")


def _extract_video_id(url: str) -> str:
    m = _VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    raise ValueError(f"could not extract a YouTube video id from {url!r}")


@dataclass
class FootageClip:
    path: Path
    duration_s: float
    width: int
    height: int
    has_audio: bool


def _ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def _has_audio_stream(path: Path) -> bool:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return bool(result.stdout.strip())


def _download_source(url: str, cache_dir: Path) -> Path:
    """Download the source video. Cached by video id — re-runs no-op."""
    video_id = _extract_video_id(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{video_id}.mp4"
    if dest.exists():
        return dest

    # Invoke as a Python module so the venv's yt-dlp is found without
    # requiring the venv's bin to be on PATH. Falls back to the binary
    # only if the module form fails (e.g. yt-dlp installed system-wide
    # via Homebrew but not pip).
    cmd_base = [
        # Prefer mp4 + m4a so ffmpeg doesn't have to remux exotic codecs.
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", str(dest),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    # Cookie strategy (added 2026-05-05): YouTube blocks anonymous
    # yt-dlp on a sizeable share of videos with "Sign in to confirm
    # you're not a bot". yt-dlp is the canonical footage download path
    # (no Playwright mixing — see memory feedback_footage_yt_dlp_canonical
    # for the principle). Auth precedence:
    #
    #   1. YTFACTORY_YTDLP_COOKIES=/path/to/cookies.txt  (Netscape format)
    #      — preferred. One-time export, survives Chrome restarts, no
    #      Keychain prompts, works in headless renders. Use a browser
    #      extension like "Get cookies.txt LOCALLY" → save to e.g.
    #      ~/.config/ytdlp/youtube_cookies.txt → export the env var.
    #
    #   2. YTFACTORY_YTDLP_BROWSER=chrome  (or firefox / chrome:Default)
    #      — live-extraction fallback. On macOS the first run prompts
    #      for Keychain access and Chrome must be quit completely.
    #      Brittle, but no manual export.
    #
    #   3. Anonymous yt-dlp — last resort. Still works for videos that
    #      aren't bot-flagged.
    cookies_file = os.environ.get("YTFACTORY_YTDLP_COOKIES", "")
    cookies_file_args: list[str] = []
    if cookies_file:
        if Path(cookies_file).exists():
            cookies_file_args = ["--cookies", cookies_file]
        else:
            print(
                f"[footage] WARN: YTFACTORY_YTDLP_COOKIES={cookies_file!r} "
                f"does not exist; falling through to browser extraction"
            )

    browser = os.environ.get("YTFACTORY_YTDLP_BROWSER", "chrome")
    cookie_args = ["--cookies-from-browser", browser] if browser else []
    print(f"[footage] yt-dlp downloading {video_id}…")

    def _try(args: list[str]) -> bool:
        try:
            subprocess.run(
                [sys.executable, "-m", "yt_dlp", *args],
                check=True, capture_output=True, text=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "")[-500:]
            if "No module named" in stderr:
                try:
                    subprocess.run(
                        ["yt-dlp", *args],
                        check=True, capture_output=True, text=True,
                    )
                    return True
                except FileNotFoundError as fnf:
                    raise RuntimeError(
                        "yt-dlp not available — install with `pip install "
                        "yt-dlp` (into the venv) or `brew install yt-dlp`"
                    ) from fnf
                except subprocess.CalledProcessError as e2:
                    print(f"[footage] yt-dlp stderr: {(e2.stderr or '')[-500:]}")
                    return False
            print(f"[footage] yt-dlp stderr: {stderr}")
            return False

    ok = False
    if cookies_file_args:
        ok = _try([*cookies_file_args, *cmd_base])
        if not ok:
            print(
                f"[footage] cookies file ({cookies_file}) auth failed; "
                f"trying browser extraction"
            )
    if not ok and cookie_args:
        ok = _try([*cookie_args, *cmd_base])
        if not ok:
            print(
                f"[footage] cookie auth ({browser}) failed; retrying "
                f"without cookies"
            )
    if not ok:
        ok = _try(cmd_base)
    if not ok:
        raise RuntimeError(
            f"yt-dlp could not download {video_id} — set "
            f"YTFACTORY_YTDLP_COOKIES=/path/to/cookies.txt (preferred; "
            f"one-time Netscape-format export from a logged-in YouTube "
            f"session), or quit Chrome and grant Keychain access on the "
            f"YTFACTORY_YTDLP_BROWSER live-extraction fallback"
        )
    if not dest.exists():
        raise RuntimeError(f"yt-dlp did not produce {dest}")
    return dest


def fetch_clip(
    url: str,
    in_s: float,
    out_s: float,
    out_path: Path,
    *,
    target_resolution: tuple[int, int] = (1080, 1920),
    fade_in_s: float = 0.10,
    fade_out_s: float = 0.15,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    pre_pad_s: float = 0.0,
) -> FootageClip:
    """Download (or reuse) the source video and trim to ``[in_s, out_s]``.

    Frame-accurate seek (``-ss`` after ``-i``) — slower than fast seek
    but lands on the requested frame, which matters because narration
    timing is locked to the cut.

    ``pre_pad_s``: when > 0, prepend N seconds of pure-black video +
    silence to the trimmed clip via a second ffmpeg pass. Used by the
    sports channel's ``black_intro`` flag to give the narrator's setup
    line ("Last kick of the season") a black-screen suspense beat
    before the broadcast cuts in. The narration plays over the silent
    black intro; the broadcast plays over the inserted-silence period
    that follows. Compose's duck-narration window must start at
    ``video_start[i] + pre_pad_s`` (= the beat's audio end) when this
    is set, so the narration during the black intro stays audible.
    """
    if out_s <= in_s:
        raise ValueError(f"out_s ({out_s}) must be > in_s ({in_s})")

    duration = out_s - in_s
    width, height = target_resolution

    src = _download_source(url, cache_dir)
    src_has_audio = _has_audio_stream(src)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 9:16 letterbox with blurred fill (Instagram / TikTok sports edit
    # convention). Source 16:9 broadcast is rendered AT FULL WIDTH in
    # the middle band of the 9:16 frame; the top/bottom strips are
    # filled with a heavily-blurred + dimmed copy of the same broadcast.
    #
    # Why not center-crop:
    #   1080x1920 portrait from 1920x1080 landscape forces ~1.78x
    #   vertical upscale + center-crop, which (a) destroys quality on
    #   broadcast-grade source and (b) chops the sides — i.e. exactly
    #   where the ball trajectory, the passer, and the receiving runner
    #   usually live. Caught on v9 Aguero render: the live broadcast
    #   showed Balotelli on the floor + Aguero running onto the ball
    #   on the right edge of the source frame; center-crop hid both.
    #
    # The blurred-letterbox filtergraph splits the input video into
    # ``[bg]`` (cover-scale + crop + heavy gblur, dimmed -15%) and
    # ``[fg]`` (fit-width scale, full broadcast aspect preserved),
    # then overlays fg on bg vertically centred. Ball + players stay
    # fully visible. Source quality preserved (no >1x upscale).
    fc_video = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=24,eq=brightness=-0.15[bg_blur];"
        f"[fg]scale={width}:-2[fg_scaled];"
        f"[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,"
        f"fade=in:st=0:d={fade_in_s:.3f},"
        f"fade=out:st={duration - fade_out_s:.3f}:d={fade_out_s:.3f},"
        f"setsar=1[vout]"
    )
    fc_audio = ""
    if src_has_audio:
        fc_audio = (
            f";[0:a]afade=in:st=0:d={fade_in_s:.3f},"
            f"afade=out:st={duration - fade_out_s:.3f}:d={fade_out_s:.3f}[aout]"
        )

    # ``-ss`` BEFORE ``-i`` (input seek). This makes ffmpeg reset the
    # output PTS to 0, so the ``fade`` filter's ``st=`` offsets land
    # inside the trim window. With ``-ss`` AFTER ``-i`` (output seek),
    # the fade timestamps stay on the source timeline — which means a
    # fade-out at ``duration - fade_out_s`` falls BEFORE the trim window
    # starts and every output frame renders full-black. (Bug found
    # 2026-05-02 on the Aguero v4 render — entire footage cut was
    # solid black.) Modern ffmpeg makes input seek frame-accurate when
    # combined with re-encoding, so we don't lose precision.
    cmd: list[str] = [
        "ffmpeg", "-y",
        "-ss", f"{in_s:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
        "-filter_complex", fc_video + fc_audio,
        "-map", "[vout]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        # Force 30fps on the broadcast trim so it matches the
        # 30fps black-intro (lavfi color=...:r=30) at concat time.
        # Without this, sources at 25fps (FIFA TV / European broadcasts)
        # mismatched the 30fps intro and the concat re-encoder dropped
        # ~2.7s of frames at the end — caught on v1 Iniesta render where
        # footage_09.mp4 had 16.3s audio but only 13.6s video, leaving
        # the last 2.7s black on screen while Tyler's payoff played.
        "-r", "30",
    ]
    if src_has_audio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(out_path)]

    print(
        f"[footage] trimming {in_s:.2f}-{out_s:.2f}s ({duration:.2f}s) "
        f"-> {out_path.name}"
    )
    with _tlm.timed(
        "footage_trim",
        category="pipeline",
        metadata={
            "src_video_id": _extract_video_id(url),
            "in_s": round(in_s, 3),
            "out_s": round(out_s, 3),
            "duration_s": round(duration, 3),
            "has_audio": src_has_audio,
        },
    ):
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[footage] ffmpeg stderr (last 3000 chars):")
        print(result.stderr[-3000:])
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode})")

    # Optional pre-pad: prepend pure-black video + silence to the
    # trimmed broadcast. Done as a second ffmpeg pass via concat
    # demuxer — keeps the trim filter graph simple, avoids breaking
    # the blurred-letterbox composition. Concat with -c copy requires
    # the intro and broadcast to share codec params, which they do
    # because we render both with libx264 + AAC at the same w/h/fps.
    if pre_pad_s > 0.05:
        intro_path = out_path.parent / f"{out_path.stem}_intro.mp4"
        broadcast_path = out_path.parent / f"{out_path.stem}_bcast.mp4"
        # Move the just-trimmed file to broadcast_path so we can rebuild
        # at out_path with concat.
        if broadcast_path.exists():
            broadcast_path.unlink()
        out_path.rename(broadcast_path)

        intro_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d={pre_pad_s:.3f}:r=30",
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:d={pre_pad_s:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(intro_path),
        ]
        result = subprocess.run(intro_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("[footage] black-intro ffmpeg stderr:")
            print(result.stderr[-2000:])
            raise RuntimeError(f"black intro generation failed (exit {result.returncode})")

        concat_list = out_path.parent / f"{out_path.stem}_concat.txt"
        concat_list.write_text(
            f"file '{intro_path.name}'\nfile '{broadcast_path.name}'\n"
        )
        # Re-encode on concat (not -c copy) because the intro PTS
        # baseline can desync with the broadcast otherwise — small
        # cost, big robustness win.
        concat_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", "30",  # uniform fps — both intro + broadcast are 30fps now
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        result = subprocess.run(concat_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("[footage] concat ffmpeg stderr:")
            print(result.stderr[-2000:])
            raise RuntimeError(f"black-intro concat failed (exit {result.returncode})")

        # Cleanup intermediates
        intro_path.unlink()
        broadcast_path.unlink()
        concat_list.unlink()
        print(f"[footage] prepended {pre_pad_s:.2f}s black intro")

    actual = _ffprobe_duration(out_path)
    print(f"[footage] wrote {out_path}  ({actual:.3f}s)")
    return FootageClip(
        path=out_path,
        duration_s=actual,
        width=width,
        height=height,
        has_audio=src_has_audio,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Trim a YouTube clip to a frame-accurate 9:16 window."
    )
    ap.add_argument("url", help="YouTube URL or 11-char video id")
    ap.add_argument("--in", dest="in_s", type=float, required=True,
                    help="start seconds (float, ms precision)")
    ap.add_argument("--out", dest="out_s", type=float, required=True,
                    help="end seconds (float, ms precision)")
    ap.add_argument("--out-path", required=True, help="destination mp4 path")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fade-in", type=float, default=0.10)
    ap.add_argument("--fade-out", type=float, default=0.15)
    args = ap.parse_args()

    fetch_clip(
        url=args.url,
        in_s=args.in_s,
        out_s=args.out_s,
        out_path=Path(args.out_path),
        target_resolution=(args.width, args.height),
        fade_in_s=args.fade_in,
        fade_out_s=args.fade_out,
    )


if __name__ == "__main__":
    main()
