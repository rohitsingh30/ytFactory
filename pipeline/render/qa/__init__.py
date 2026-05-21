"""Render-time automated quality-assurance helpers.

Pure-CPU video QA that runs on every render at near-zero cost, in
contrast to the LLM-vision critic at :mod:`pipeline.llm.critic` which
runs ~$0.50/call and is Claude-CLI-only today.

Public entry point:

    from pipeline.render.qa.vbench_adapter import score_video_vbench

See :mod:`pipeline.render.qa.vbench_adapter` for the wrapped axes
(``subject_consistency``, ``temporal_flickering``, ``imaging_quality``)
and how the raw 0-100 VBench scores map onto the team's 1-10 critic
axes via ``vbench_score = mean(3 axes) / 10``.
"""

from __future__ import annotations

__all__ = ["vbench_adapter"]
