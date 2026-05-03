"""Shared test helpers: fake Beat objects, project-root sys.path fix."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Make the project importable when tests are run from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class FakeWord:
    text: str
    start: float
    end: float


@dataclass
class FakeBeat:
    """Stand-in for `pipeline.beats.Beat` that tests can build without
    pulling in Whisper or audio. Same .start/.end/.duration API."""

    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def fake_beat_list(spans: list[tuple[float, float]], text: str = "x") -> list[FakeBeat]:
    """Build a list of FakeBeat from (start, end) tuples."""
    return [FakeBeat(text=text, start=s, end=e) for s, e in spans]
