"""Common types + helpers for source adapters."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RawStory:
    """One mined story before any rewriting.

    ``body`` is the long-form text the rewriter will condense into a 50-80
    word narration. ``source`` is a short tag like ``reddit:AmItheAsshole``
    used to disambiguate cached items across niches.
    """

    slug: str
    title: str
    body: str
    source: str
    url: str
    metadata: dict = field(default_factory=dict)


def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe slug. Strips accents, lowercases, hyphenates."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len] or "untitled"


def save_raw(story: RawStory, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{story.slug}.json"
    with path.open("w") as f:
        json.dump(asdict(story), f, indent=2, ensure_ascii=False)
    return path


def load_raw(path: Path) -> RawStory:
    with path.open() as f:
        return RawStory(**json.load(f))
