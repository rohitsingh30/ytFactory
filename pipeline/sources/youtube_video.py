"""YouTube long-form-video source adapter (use cases 2.1 + 2.2 in DESIGN.md).

Two transcript paths, in order of preference:

  1. **Captions** via ``youtube-transcript-api`` — no audio download, no
     Whisper model. Works for anything that has manual or auto-generated
     CC. Fastest, lowest disk footprint.

  2. **Local Whisper** via ``pipeline.transcribe`` — only if captions are
     missing. Downloads the audio with ``yt-dlp`` and transcribes with
     ``mlx-whisper``. Heavy: requires the whisper model on disk.

Output: one ``RawStory`` whose ``body`` is the full transcript text. The
``/make-script`` skill is then responsible for segmenting the transcript
into individual stories and rewriting each as a hook-first script.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import requests

from pipeline.observability.event_helpers import safe_track as _track

from .base import RawStory, save_raw, slugify


USER_AGENT = "ytFactory/0.1 (https://github.com/local; transcript fetcher)"
TIMEOUT = 30


def _source_attempt(kind: str, ref: str, backend: str) -> None:
    _track(
        "source.fetch_attempt",
        category="http",
        metadata={"kind": kind, "ref": ref, "backend": backend},
    )


def _source_ok(*, status_code: int = 200, body_chars: int = 0) -> None:
    _track(
        "source.fetch_ok",
        category="http",
        success=True,
        metadata={"status_code": status_code, "body_chars": body_chars},
    )


def _source_fallback(*, reason: str, original_status: int | None = None) -> None:
    _track(
        "source.fetch_fallback",
        category="http",
        success=False,
        metadata={"fallback_reason": reason, "original_status": original_status},
    )


def _extract_video_id(s: str) -> str:
    """Pull the 11-char YouTube video id out of a URL or id string."""
    m = re.search(r"(?:v=|/shorts/|youtu\.be/|/embed/|/v/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    raise ValueError(f"could not extract a YouTube video id from {s!r}")


def _fetch_oembed_meta(video_id: str) -> dict:
    """Title + author for the video, no auth required."""
    url = f"https://www.youtube.com/oembed?url=https://youtu.be/{video_id}&format=json"
    _source_attempt("youtube_oembed", video_id, "oembed")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
        return r.json()
    except requests.HTTPError as e:
        _source_fallback(
            reason=f"http_{getattr(e.response, 'status_code', 'unknown')}",
            original_status=getattr(e.response, "status_code", None),
        )
        print(f"[youtube_video] oembed lookup failed: {e}")
        return {}
    except Exception as e:
        _source_fallback(reason=type(e).__name__, original_status=None)
        print(f"[youtube_video] oembed lookup failed: {e}")
        return {}


def _captions_via_youtube_transcript_api(video_id: str, languages: list[str]) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        print("[youtube_video] youtube-transcript-api not installed; skipping caption path")
        return None

    # v1.x API: instance method `fetch()` returning a FetchedTranscript with
    # `.snippets` (list of objects with `.text`). v0.x had a class method
    # `get_transcript` returning list[dict]. Support both.
    _source_attempt("youtube_transcript", video_id, "youtube_transcript_api")
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            chunks = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            texts = [c["text"].replace("\n", " ").strip() for c in chunks if c.get("text")]
        else:
            api = YouTubeTranscriptApi()
            transcript = api.fetch(video_id, languages=languages)
            texts = [s.text.replace("\n", " ").strip() for s in transcript.snippets if s.text]
    except Exception as e:
        _source_fallback(reason=type(e).__name__, original_status=None)
        print(f"[youtube_video] caption fetch failed: {e}")
        return None
    body = " ".join(t for t in texts if t)
    _source_ok(status_code=200, body_chars=len(body))
    return body


def _audio_via_yt_dlp(video_id: str, dest: Path) -> Path | None:
    if not dest.exists():
        cmd = [
            "yt-dlp",
            "-q",
            "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3",
            "-o", str(dest.with_suffix(".%(ext)s")),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        print(f"[youtube_video] yt-dlp downloading audio for {video_id}…")
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            print("[youtube_video] yt-dlp not on PATH — install it (`brew install yt-dlp` or `pip install yt-dlp`)")
            return None
        except subprocess.CalledProcessError as e:
            print(f"[youtube_video] yt-dlp failed: {e}")
            return None
    return dest if dest.exists() else None


def _transcribe_with_whisper(audio_path: Path) -> str | None:
    try:
        from pipeline import transcribe  # heavy import — only when needed
    except Exception as e:
        print(f"[youtube_video] cannot import pipeline.transcribe: {e}")
        return None
    try:
        result = transcribe.transcribe(audio_path)
    except Exception as e:
        print(f"[youtube_video] whisper transcription failed: {e}")
        return None
    return " ".join(seg.get("text", "").strip() for seg in result.get("segments", []) if seg.get("text"))


def fetch(
    url_or_id: str,
    languages: tuple[str, ...] = ("en", "en-US", "en-GB"),
    use_whisper_fallback: bool = False,
    audio_cache_dir: Path = Path("data/sources/youtube"),
) -> list[RawStory]:
    """Return a one-element list with the video's full transcript as body."""
    video_id = _extract_video_id(url_or_id)
    meta = _fetch_oembed_meta(video_id)
    title = meta.get("title", f"YouTube video {video_id}")
    author = meta.get("author_name", "")
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    transcript = _captions_via_youtube_transcript_api(video_id, list(languages))

    if not transcript and use_whisper_fallback:
        print("[youtube_video] no captions — falling back to yt-dlp + whisper")
        _source_fallback(reason="captions_unavailable_whisper_fallback", original_status=None)
        audio_cache_dir.mkdir(parents=True, exist_ok=True)
        audio_path = _audio_via_yt_dlp(video_id, audio_cache_dir / f"{video_id}.mp3")
        if audio_path:
            transcript = _transcribe_with_whisper(audio_path)

    if not transcript:
        raise RuntimeError(
            f"no transcript available for {video_id}. "
            "install `youtube-transcript-api` for caption fetch, or pass "
            "--whisper-fallback (requires yt-dlp + the mlx-whisper model on disk)."
        )

    transcript = re.sub(r"\s+", " ", transcript).strip()
    return [
        RawStory(
            slug=slugify(f"yt-{video_id}-{title}"),
            title=title,
            body=transcript,
            source="youtube_video",
            url=canonical_url,
            metadata={
                "video_id": video_id,
                "author": author,
                "transcript_chars": len(transcript),
                "language_pref": list(languages),
            },
        )
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="YouTube URL or 11-char video id")
    ap.add_argument("--languages", default="en,en-US,en-GB")
    ap.add_argument(
        "--whisper-fallback",
        action="store_true",
        help="If captions unavailable, download audio with yt-dlp and transcribe with mlx-whisper.",
    )
    ap.add_argument("--out", default="data/intermediate")
    ap.add_argument("--channel", default="reddit_video")
    args = ap.parse_args()

    stories = fetch(
        url_or_id=args.url,
        languages=tuple(args.languages.split(",")),
        use_whisper_fallback=args.whisper_fallback,
    )
    dest = Path(args.out) / args.channel / "raw"
    for s in stories:
        path = save_raw(s, dest)
        print(f"  -> {path}  ({s.metadata['transcript_chars']} chars)")


if __name__ == "__main__":
    main()
