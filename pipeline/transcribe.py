"""Stage 1 — transcribe a long-form source video.

Thin shim over ``pipeline.asr`` so the choice of engine
(whisper_mlx / parakeet_mlx) is a config knob, not a code change. The
``Word`` shape and the result dict shape are both stable across
backends — see ``pipeline.asr`` for the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import asr
from .beats import Word


DEFAULT_PROVIDER = asr.DEFAULT_PROVIDER


def transcribe(
    source: Path,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
) -> dict:
    """Transcribe a long-form source video. Returns a Whisper-shaped result."""
    print(f"[transcribe] {source.name}  (provider={provider})")
    return asr.transcribe(source, provider=provider, model=model)


def words_from_result(result: dict) -> list[Word]:
    words: list[Word] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            words.append(
                Word(
                    text=(w.get("word") or "").strip(),
                    start=float(w["start"]),
                    end=float(w["end"]),
                )
            )
    return words


def save_result(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(result, f, indent=2)


def load_result(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)
