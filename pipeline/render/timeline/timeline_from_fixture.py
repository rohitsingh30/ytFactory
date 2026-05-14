"""Test-only TimelineBuilder that loads a pre-recorded JSON.

Used by engine integration tests to isolate engine wiring from the
cloud-whisper dependency. Same Protocol as :class:`AsrBeats` /
:class:`AsrAnchors` — engine doesn't see the difference.

Fixture format
--------------

Plain JSON list of segments:

.. code-block:: json

    [
      {"start_s": 0.0, "end_s": 1.5, "text": "Hello world",
       "anchor_id": "beat_000", "kind": "beat"},
      {"start_s": 1.5, "end_s": 3.0, "text": "Second beat",
       "anchor_id": "beat_001", "kind": "beat"}
    ]

The fixture path is read from ``spec.extra["timeline_fixture_path"]``
so tests can swap fixtures per render without changing the registered
plugin.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    Segment,
    Timeline,
    TimelineBuilder,
    register_plugin,
)


class TimelineFromFixture:
    """Loads a Timeline from a JSON file pointed at by
    ``spec.extra['timeline_fixture_path']``.

    Raises ``FileNotFoundError`` if the path is missing or absent —
    the test is misconfigured if it picked this plugin without
    setting the fixture path.
    """

    def build(
        self,
        spec: Any,  # RenderSpec
        script: dict[str, Any],
        audio: AudioResult,
    ) -> Timeline:
        fixture_path = (spec.extra or {}).get("timeline_fixture_path")
        if not fixture_path:
            raise ValueError(
                "TimelineFromFixture: spec.extra['timeline_fixture_path'] is "
                "required when using this plugin (set it in the test fixture "
                "or pick a different timeline plugin)."
            )
        path = Path(fixture_path)
        if not path.exists():
            raise FileNotFoundError(
                f"TimelineFromFixture: fixture not found at {path}"
            )
        raw = json.loads(path.read_text())
        return [
            Segment(
                start_s=float(s["start_s"]),
                end_s=float(s["end_s"]),
                text=s.get("text", ""),
                anchor_id=str(s.get("anchor_id", f"seg_{i:03d}")),
                kind=s.get("kind", "beat"),
            )
            for i, s in enumerate(raw)
        ]


register_plugin("timeline", "timeline_from_fixture", TimelineFromFixture())
assert isinstance(TimelineFromFixture(), TimelineBuilder)


__all__ = ["TimelineFromFixture"]
