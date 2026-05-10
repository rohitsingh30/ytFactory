"""RenderJob — the single context bag that flows through every stage.

Why
---
Today every stage in ``cloud/render-worker-v2/entrypoint.py`` reaches
back into Firestore + the file system to pick up the artifacts the
previous stage wrote. That makes it hard to:

- track WHICH artifacts have been produced and which haven't,
- reuse cached artifacts (you have to know where they live on disk),
- pass metadata (model used, attempt count, judge verdicts) through
  the pipeline,
- run a stage in isolation for tests.

The orchestrator owns a single :class:`RenderJob` per render. Stages
read upstream artifacts from it; the pipeline runner appends new
artifacts to it as the DAG advances. A render is "the lifecycle of one
RenderJob".

Fields are populated on demand — at any point the RenderJob captures
"what we know so far" about this render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BeatArtifact:
    """One beat's per-stage artifacts (prompt, image, captions slice)."""
    index: int
    text: str
    prompt: str | None = None        # filled by prompts stage
    image_uri: str | None = None     # filled by images stage
    image_local_path: str | None = None
    judge_verdict: dict | None = None    # AI-judge result if any


@dataclass
class CriticVerdict:
    """Output of the post-render critic stage."""
    verdict: str           # "SHIP" | "FIX" | "BLOCK"
    weakest_param: str = ""
    fixes: list[Any] = field(default_factory=list)   # list[Fix] — typed at use site
    raw: dict | None = None


@dataclass
class RenderJob:
    """Single context object owned by the pipeline runner.

    Stages take a RenderJob in, mutate the slot they own, and pass it
    on. The runner enforces "stage X writes only to slots in
    ``X.artifact_paths``" — preventing accidental cross-stage writes.
    """

    # Identity
    job_id: str
    channel: str
    format: str = "animated"

    # Configuration
    channel_cfg: dict = field(default_factory=dict)
    format_cfg: dict = field(default_factory=dict)
    raw_input: dict = field(default_factory=dict)

    # Per-stage artifacts (filled in as DAG advances)
    script: dict | None = None              # rewrite stage output
    cast: dict | None = None                # cast stage output
    beats: list[BeatArtifact] = field(default_factory=list)
    audio_uri: str | None = None
    audio_local_path: str | None = None
    captions: dict | None = None
    mp4_uri: str | None = None
    thumb_uri: str | None = None
    critic_verdict: CriticVerdict | None = None

    # Cross-cutting / observability
    past_critiques: list[CriticVerdict] = field(default_factory=list)
    fixes_applied: list[Any] = field(default_factory=list)   # list[Fix]
    attempts_per_stage: dict[str, int] = field(default_factory=dict)
    cache_hits: dict[str, int] = field(default_factory=dict)

    def upstream_for(self, stage: str) -> dict:
        """Return the upstream artifacts this stage's contract should
        hash into its cache key.

        Encodes the explicit dependency graph between stages — keeping
        it here rather than scattered across contracts means changing
        an edge requires editing exactly one place.
        """
        if stage == "rewrite":
            return {"raw_input": self.raw_input, "channel_cfg": self.channel_cfg}
        if stage == "cast":
            return {"script": self.script, "channel_cfg": self.channel_cfg}
        if stage == "prompts":
            return {"script": self.script, "cast": self.cast,
                    "channel_cfg": self.channel_cfg}
        if stage == "images":
            return {"beats": [{"index": b.index, "prompt": b.prompt}
                              for b in self.beats]}
        if stage == "tts":
            return {"script": self.script, "channel_cfg": self.channel_cfg}
        if stage == "asr":
            return {"audio_uri": self.audio_uri}
        if stage == "compose":
            return {
                "beats": [{"index": b.index, "image_uri": b.image_uri}
                          for b in self.beats],
                "audio_uri": self.audio_uri,
                "captions": self.captions,
            }
        if stage == "critic":
            return {"mp4_uri": self.mp4_uri, "script": self.script}
        return {}
