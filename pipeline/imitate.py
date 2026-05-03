"""Riff-on-a-YouTube-video pipeline (use case: imitate a viral Short).

Two new stages plug into the existing pull → rewrite → cast → render flow:

  1. ``analyze(url)`` — pull the transcript, download a low-res copy with
     yt-dlp, sample evenly-spaced frames with ffmpeg, then ask Claude
     (Sonnet, vision via the Read tool) to extract an *imitation profile*:
     the niche, hook template, structure, tone, pacing, themes, suggested
     voice, and visual aesthetic of the source.

  2. ``ideate(profile, n)`` — Claude writes N novel ``RawStory`` seeds
     that match the profile but have completely different content. They
     drop straight into the standard ``rewrite → cast → make_shorts``
     pipeline because the output shape is identical to a ``RawStory``
     coming out of any other source adapter.

The profile's ``niche_match`` field maps to one of the existing channels
(``aita``, ``tifu``, ``malicious``, ``prorevenge``, ``oddities``, ``tih``)
or ``"novel"``. v1 is intentionally tight: novel profiles fall back to
the AITA channel YAML, and we never invent a new channel YAML on the fly.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from sources.base import RawStory, slugify
from sources.youtube_video import (
    _captions_via_youtube_transcript_api,
    _extract_video_id,
    _fetch_oembed_meta,
)

from . import llm


# Niche → (channel_dir, channel_yaml) routing lives in pipeline.niches
# so web.server and this module read from one canonical source. Re-
# exported here for any caller that imports ``imitate.NICHE_CHANNEL``.
from .niches import NICHE_CHANNEL, NICHE_FALLBACK  # noqa: F401


# Voice ids the profile is allowed to suggest. Mirrors web/server.py VOICES.
ALLOWED_VOICES: list[str] = [
    "af_bella", "af_heart", "af_sarah", "af_nicole", "af_nova", "af_aoede",
    "am_michael", "am_adam", "am_liam", "am_eric", "am_onyx", "am_puck",
    "bf_emma", "bf_alice", "bf_isabella", "bf_lily",
    "bm_george", "bm_daniel", "bm_lewis", "bm_fable",
]


PROFILE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "niche_match", "hook_template", "structure", "tone", "pacing",
        "length_target_s", "themes", "viral_hooks",
        "voice_profile_summary", "suggested_voice", "visual_aesthetic",
    ],
    "properties": {
        "niche_match": {
            "type": "string",
            "enum": ["aita", "tifu", "malicious", "prorevenge",
                     "oddities", "tih", "novel"],
        },
        "hook_template": {"type": "string"},
        "structure": {"type": "string"},
        "tone": {"type": "string"},
        "pacing": {"type": "string"},
        "length_target_s": {"type": "number"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "viral_hooks": {"type": "array", "items": {"type": "string"}},
        "voice_profile_summary": {"type": "string"},
        "suggested_voice": {"type": "string", "enum": ALLOWED_VOICES},
        "visual_aesthetic": {"type": "string"},
    },
}


def _find_yt_dlp() -> str | None:
    """Resolve yt-dlp path even if the parent shell's PATH doesn't have it.

    yt-dlp is installed in the venv (requirements.txt lists it) but the
    user's shell PATH may not include ``.venv/bin``. We check the same
    directory as the running interpreter first, then fall back to PATH.
    """
    # Don't .resolve() — it follows symlinks out of the venv.
    venv_bin = Path(sys.executable).parent / "yt-dlp"
    if venv_bin.exists():
        return str(venv_bin)
    return shutil.which("yt-dlp")


def _ydl_download_video(video_id: str, dest_dir: Path) -> Path | None:
    """Download a small mp4 of the source so we can sample frames.

    We deliberately pick the smallest reasonable format — frame analysis
    doesn't need 1080p and a Short is usually ≤60s so the download is
    quick. Returns None if yt-dlp isn't installed or the fetch fails.
    """
    yt_dlp = _find_yt_dlp()
    if not yt_dlp:
        print("[imitate] yt-dlp not available — skipping frame sample")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_template = dest_dir / f"{video_id}.%(ext)s"
    cmd = [
        yt_dlp,
        "-q",
        "-f", "mp4[height<=480]/best[height<=480]/best",
        "-o", str(out_template),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    print(f"[imitate] yt-dlp downloading video {video_id}…")
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"[imitate] yt-dlp failed: {e.stderr.decode('utf-8', 'replace')[:300] if e.stderr else e}")
        return None
    for p in dest_dir.iterdir():
        if p.stem == video_id and p.suffix.lower() in {".mp4", ".webm", ".mkv", ".m4v"}:
            return p
    return None


def _probe_duration(video_path: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, check=True,
        )
        return float(proc.stdout.strip())
    except Exception as e:
        print(f"[imitate] ffprobe failed: {e}")
        return None


def _sample_frames(video_path: Path, n: int, out_dir: Path) -> list[Path]:
    """Grab N evenly-spaced JPEG frames, scaled to 640px wide."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration(video_path)
    if not duration or duration <= 0:
        return []
    paths: list[Path] = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        out = out_dir / f"frame_{i:02d}.jpg"
        if not out.exists():
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-ss", f"{t:.2f}", "-i", str(video_path),
                     "-frames:v", "1", "-q:v", "4",
                     "-vf", "scale=640:-1", str(out)],
                    check=True,
                )
            except Exception as e:
                print(f"[imitate] ffmpeg frame {i} failed: {e}")
                continue
        paths.append(out)
    return paths


def _analyze_prompt(*, title: str, author: str, video_id: str,
                    transcript: str, frame_paths: list[Path]) -> str:
    # Cap transcript size — 4k chars is plenty to read tone/structure.
    transcript_block = (transcript[:4000] + "…") if len(transcript) > 4000 else transcript
    if not transcript_block:
        transcript_block = "(no captions available — judge from frames + title alone)"

    lines: list[str] = [
        "You are analyzing a viral YouTube Short to extract its IMITATION PROFILE — the topic, tone, structure, pacing, and aesthetic that make it work — so we can generate fresh Shorts in the same style with novel content.",
        "",
        f"VIDEO TITLE: {title}",
        f"VIDEO AUTHOR: {author}",
        f"VIDEO ID: {video_id}",
        "",
        "TRANSCRIPT (caption text in order):",
        transcript_block,
    ]
    if frame_paths:
        lines += [
            "",
            "FRAME FILES — read each as an image with the Read tool to see what the source LOOKS like (illustration style, palette, on-screen text, character framing, pacing changes):",
        ]
        lines += [f"- {p}" for p in frame_paths]

    lines += [
        "",
        "Return STRICT JSON matching the imitation profile schema:",
        "- niche_match: pick the closest existing niche [aita, tifu, malicious, prorevenge, oddities, tih]; use 'novel' ONLY if none fit.",
        "- hook_template: a fill-in-the-blank pattern like 'AITA for [X]?' that captures the opening line shape.",
        "- structure: short '→' chain (e.g. 'setup → conflict → twist → vote-bait closer').",
        "- tone: 1-2 adjectives.",
        "- pacing: short description (e.g. 'fast, ~4 beats per 30s').",
        "- length_target_s: number 15–90.",
        "- themes: 3-6 substantive themes the source riffs on.",
        "- viral_hooks: 3-6 phrases that drive watch-through.",
        "- voice_profile_summary: short narrator description.",
        f"- suggested_voice: ONE Kokoro voice id from {ALLOWED_VOICES}.",
        "- visual_aesthetic: 1-2 sentence description of how the source looks.",
        "",
        "Return ONLY the JSON object, no prose, no markdown fences.",
    ]
    return "\n".join(lines)


def analyze(
    url: str,
    *,
    work_root: Path = Path("data/cache/riff"),
    n_frames: int = 8,
) -> dict:
    """Pull transcript + frames, ask Claude for an imitation profile.

    Returns a dict with the profile fields plus housekeeping:
    ``video_id``, ``title``, ``author``, ``url``, ``transcript_chars``,
    ``frames`` (list of file paths), and ``channel_dir`` /
    ``channel_yaml`` derived from ``niche_match``.
    """
    video_id = _extract_video_id(url)
    work_dir = work_root / video_id
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"[imitate] [analyze] {video_id} — fetching captions…")
    transcript = _captions_via_youtube_transcript_api(
        video_id, ["en", "en-US", "en-GB"]
    ) or ""
    transcript = re.sub(r"\s+", " ", transcript).strip()

    meta = _fetch_oembed_meta(video_id)
    title = meta.get("title", f"YouTube video {video_id}")
    author = meta.get("author_name", "")

    print(f"[imitate] [analyze] {video_id} — sampling frames…")
    frames: list[Path] = []
    video_path = _ydl_download_video(video_id, work_dir / "video")
    if video_path is not None:
        frames = _sample_frames(video_path, n_frames, work_dir / "frames")

    if not transcript and not frames:
        raise RuntimeError(
            f"could not analyze {video_id}: no captions and no frames "
            "(install yt-dlp + ffmpeg, or pick a video with captions)."
        )

    analyze_model = llm.model_for("imitate_analyze")
    print(f"[imitate] [analyze] {video_id} — calling claude ({analyze_model} vision)…")
    prompt = _analyze_prompt(
        title=title, author=author, video_id=video_id,
        transcript=transcript, frame_paths=frames,
    )
    profile = llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=PROFILE_SCHEMA,
        model=analyze_model,
        add_dirs=[work_dir / "frames"] if frames else None,
        allowed_tools=["Read"] if frames else None,
        timeout_s=240,
        budget_usd=1.0,
    )
    if not isinstance(profile, dict):
        raise llm.ClaudeCLIError(f"analyze: expected object, got {type(profile).__name__}")

    # Normalise voice if model picked something off-list.
    if profile.get("suggested_voice") not in ALLOWED_VOICES:
        profile["suggested_voice"] = "af_bella"

    niche = profile.get("niche_match", "novel")
    channel_dir, channel_yaml = NICHE_CHANNEL.get(niche, NICHE_FALLBACK)
    profile.update({
        "video_id": video_id,
        "title": title,
        "author": author,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "transcript_chars": len(transcript),
        "frames": [str(p) for p in frames],
        "channel_dir": channel_dir,
        "channel_yaml": channel_yaml,
    })

    profile_path = work_dir / "profile.json"
    profile_path.write_text(json.dumps(profile, indent=2))
    print(f"[imitate] [analyze] {video_id} — wrote {profile_path}")
    return profile


def _ideate_prompt(profile: dict, n: int) -> str:
    return (
        f"Given the following IMITATION PROFILE of a viral YouTube Short, write {n} fresh, novel story seeds in the SAME niche and tone but with completely different content.\n\n"
        f"Each seed MUST:\n"
        f"- match the profile's hook_template shape\n"
        f"- fit the profile's structure arc\n"
        f"- be substantive enough to support length_target_s ≈ {profile.get('length_target_s', 45)}s of narration (≥180 words of body content with concrete specifics: numbers, names, props, settings)\n"
        f"- pull from the profile's themes list — use a DIFFERENT theme for each seed\n"
        f"- invent fresh specifics — do not retell the source video\n"
        f"- be first-person where the source is first-person\n"
        f"\n"
        f"PROFILE:\n{json.dumps(profile, indent=2)}\n\n"
        f"Return STRICT JSON: an array of EXACTLY {n} objects, each:\n"
        f"  {{\n"
        f'    "title": "<10-15 word click-bait headline>",\n'
        f'    "hook": "<1-sentence opener matching hook_template>",\n'
        f'    "body": "<200-400 word first-person account, conversational, with concrete details>"\n'
        f"  }}\n"
        f"\nReturn ONLY the JSON array, no prose, no markdown fences."
    )


def ideate(profile: dict, *, n: int = 3) -> list[RawStory]:
    """Generate N novel story seeds matching the profile."""
    print(f"[imitate] [ideate] generating {n} seeds for niche={profile.get('niche_match')}")
    prompt = _ideate_prompt(profile, n)
    seeds_raw = llm.call_claude_cli(
        prompt,
        output_json=True,
        model=llm.model_for("imitate_apply"),
        timeout_s=240,
        budget_usd=1.0,
    )
    if not isinstance(seeds_raw, list):
        raise llm.ClaudeCLIError(f"ideate: expected array, got {type(seeds_raw).__name__}")

    video_id = profile.get("video_id", "")
    seeds: list[RawStory] = []
    for i, s in enumerate(seeds_raw[:n]):
        if not isinstance(s, dict):
            continue
        title = (s.get("title") or "").strip() or f"Riff {i+1}"
        hook = (s.get("hook") or "").strip()
        body = (s.get("body") or "").strip()
        if not body:
            continue
        full_body = f"{hook}\n\n{body}" if hook else body
        slug = slugify(f"riff-{video_id}-{i+1}-{title}")[:60]
        seeds.append(RawStory(
            slug=slug,
            title=title,
            body=full_body,
            source=f"riff:youtube:{video_id}",
            url=profile.get("url", ""),
            metadata={
                "ideated_from_profile": True,
                "source_video_id": video_id,
                "source_title": profile.get("title", ""),
                "niche_match": profile.get("niche_match"),
            },
        ))
    if not seeds:
        raise llm.ClaudeCLIError("ideate produced 0 valid seeds")
    print(f"[imitate] [ideate] wrote {len(seeds)} seeds")
    return seeds


def riff(url: str, *, n: int = 3) -> tuple[dict, list[RawStory]]:
    """Convenience: analyze + ideate in one call."""
    profile = analyze(url)
    seeds = ideate(profile, n=n)
    return profile, seeds
