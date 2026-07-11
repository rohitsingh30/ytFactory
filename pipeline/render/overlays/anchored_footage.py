"""Anchored foreground footage OverlayProducer (sports_doc shape).

Each footage_plan entry becomes a layer-10 OverlayElement carrying
the trimmed clip. The visualize plugin (typically
``footage_filler``) renders the b-roll background; this overlay
producer composites foreground match clips on top at their authored
anchors.

Plugin activation: ``spec.overlay_timeline = True`` in the wizard.
Pairs with ``spec.visual_mode = footage_filler``.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)
from pipeline.render.shared.trim_letterbox import trim_clip_letterbox
from pipeline.render.telemetry_helpers import track_event

_logger = logging.getLogger(__name__)


def _slug_from_url(url: str) -> str:
    """Stable file-safe slug for caching downloads from a YouTube URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _download_source(url: str, sources_dir: Path) -> Path:
    """yt-dlp the source video into sources_dir/<hash>.mp4. Cached."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    target = sources_dir / f"{_slug_from_url(url)}.mp4"
    if target.exists() and target.stat().st_size > 1024 * 100:
        return target
    print(f"[dl  ] yt-dlp {url} → {target.name}")
    subprocess.run([
        "yt-dlp",
        "-f", "best[ext=mp4][height<=1080]/best[ext=mp4]/best",
        "-o", str(target),
        "--no-progress", "--no-warnings",
        url,
    ], check=True)
    return target


def _prep_footage_clip(
    entry: dict[str, Any],
    sources_dir: Path,
    cache_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
    grade_filter: str | None,
) -> Path:
    """Download source, trim to [in_s, out_s], normalize to out_w x out_h fps.

    Reuses :func:`pipeline.render.shared.trim_letterbox.trim_clip_letterbox`
    for the blurred-letterbox path on aspect mismatches + the stream-
    copy short-circuit when src already matches.
    """
    cid = entry["id"]
    out = cache_dir / "clips" / f"{cid}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 1024 * 100:
        return out
    src = _download_source(entry["url"], sources_dir)
    trim_clip_letterbox(
        src, float(entry["in_s"]), float(entry["out_s"]), out,
        out_w=out_w, out_h=out_h, fps=fps, grade_filter=grade_filter,
    )
    return out


class AnchoredFootage:
    """Foreground match-clips composited at authored anchors.

    Reads ``footage_plan`` entries from ``spec.extra['footage_plan_path']``.
    Trims each clip via the inlined helper, returns one OverlayElement
    per clip.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        footage_plan_path = (spec.extra or {}).get("footage_plan_path")
        if not footage_plan_path or not Path(footage_plan_path).exists():
            _logger.info("anchored_footage: no footage_plan_path — no overlays")
            return []

        try:
            import json  # noqa: PLC0415
            plan = json.loads(Path(footage_plan_path).read_text())
        except Exception as exc:  # noqa: BLE001
            _logger.warning("anchored_footage: failed to parse footage_plan: %s", exc)
            return []

        out_dir = audio.narration_path.parent / "anchored_footage"
        out_dir.mkdir(parents=True, exist_ok=True)
        sources_dir = out_dir / "sources"

        elements: list[OverlayElement] = []
        # Walk match_footage + talking_heads + archival_footage blocks.
        for arr_name in ("match_footage", "talking_heads", "archival_footage"):
            for entry in plan.get(arr_name, []):
                start_s = float(entry.get("at_s", 0))
                end_s = start_s + float(entry.get("duration_s", 5))
                cid = entry.get("id", "anchored")
                try:
                    t0 = time.perf_counter()
                    clip_path = _prep_footage_clip(
                        entry, sources_dir, out_dir,
                        spec.output_resolution[0],
                        spec.output_resolution[1],
                        spec.output_fps,
                        None,  # no grade filter
                    )
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("anchored_footage: skip %s (%s)", cid, exc)
                    continue
                track_event(
                    "overlay.render",
                    category="pipeline",
                    duration_ms=duration_ms,
                    metadata={
                        "kind": "anchor",
                        "count": 1,
                        "total_chars": len(str(cid or "")),
                        "duration_ms": duration_ms,
                    },
                )
                elements.append(OverlayElement(
                    start_s=start_s,
                    end_s=end_s,
                    layer=10,
                    asset_path=clip_path,
                    extras={"id": cid, "kind": arr_name},
                ))
        return elements


register_plugin("overlays", "anchored_footage", AnchoredFootage())
assert isinstance(AnchoredFootage(), OverlayProducer)


__all__ = ["AnchoredFootage"]
