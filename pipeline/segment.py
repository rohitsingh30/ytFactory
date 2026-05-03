"""Stage 2 — split a long transcript into individual stories.

For v0 phase 2: this is done interactively by Claude Code in the dev
session — the developer pastes the transcript, Claude returns
`[{title, start, end, summary}]`, and that JSON gets dropped into
`data/intermediate/<slug>/stories.json`.

For v1: replace this with an Anthropic API call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class Story:
    title: str
    start: float       # seconds into the source video
    end: float
    summary: str


def load_stories(path: Path) -> list[Story]:
    with path.open() as f:
        raw = json.load(f)
    return [Story(**s) for s in raw]


def save_stories(stories: list[Story], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump([asdict(s) for s in stories], f, indent=2)


def transcript_text_for_segmenting(result: dict) -> str:
    """Format a Whisper result into a text suitable for handing to Claude.

    Format: one line per segment with [HH:MM:SS] timestamp, so Claude can
    return story boundaries in seconds and we can verify alignment.
    """
    lines: list[str] = []
    for seg in result.get("segments", []):
        t = float(seg.get("start", 0.0))
        h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
        ts = f"[{h:02d}:{m:02d}:{s:02d}]"
        lines.append(f"{ts} {seg.get('text', '').strip()}")
    return "\n".join(lines)
