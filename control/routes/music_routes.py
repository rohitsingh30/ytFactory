"""Music bed catalogue + sample audio playback for the studio create flow.

GET /api/music/catalog
GET /api/music/sample/{channel}/{filename}

The customize step lets the user audition the music bed inline before
queueing a render. Catalogue enumerates ``*.{mp3,wav,m4a}`` under
each channel's curated music location:

  - ``<channel>/music/``                (the canonical layout)
  - ``<channel>/branding/music/``       (sportsrecapped-style)
  - ``<channel>/songs/``                (rhymetimejunction-style sung beds)

A "shared" pseudo-channel surfaces anything under ``data/music/`` (e.g.,
synth placeholders shared across channels). Channels with no curated
beds simply don't appear — the FE renders a "no beds yet" hint.

Path-traversal is rejected: ``filename`` must match a discovered file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from pipeline.paths import PROJECT_ROOT, DATA_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/music")

_AUDIO_EXTS = (".mp3", ".wav", ".m4a")
_MEDIA_TYPE = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}

# Per-channel candidate dirs to scan, in priority order. The first
# directory found per channel wins (we don't merge across them — keeps
# the catalogue clean of the same bed appearing twice).
_PER_CHANNEL_DIRS = ("music", "branding/music", "songs")


@dataclass(frozen=True)
class _Bed:
    """One discovered music bed."""

    channel: str
    filename: str
    abs_path: Path

    @property
    def key(self) -> str:
        # Filename without extension — same shape as voice keys.
        return self.abs_path.stem

    @property
    def label(self) -> str:
        # Pretty label: "tifo_bed" → "Tifo Bed"
        return self.key.replace("-", " ").replace("_", " ").title()

    @property
    def sample_url(self) -> str:
        return f"/api/music/sample/{self.channel}/{self.filename}"


def _iter_channels() -> list[str]:
    """Channel slugs (one dir per top-level <slug>/config.yaml)."""
    out: list[str] = []
    for d in sorted(PROJECT_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / "config.yaml").exists():
            out.append(d.name)
    return out


def _scan_channel(channel: str) -> list[_Bed]:
    base = PROJECT_ROOT / channel
    for sub in _PER_CHANNEL_DIRS:
        d = base / sub
        if not d.exists():
            continue
        beds = sorted(
            (
                _Bed(channel=channel, filename=p.name, abs_path=p)
                for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
            ),
            key=lambda b: b.key,
        )
        if beds:
            return beds
    return []


def _scan_shared() -> list[_Bed]:
    """Cross-channel beds under ``data/music/``."""
    d = DATA_ROOT / "music"
    if not d.exists():
        return []
    return sorted(
        (
            _Bed(channel="shared", filename=p.name, abs_path=p)
            for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
        ),
        key=lambda b: b.key,
    )


def _list_beds() -> list[_Bed]:
    out: list[_Bed] = list(_scan_shared())
    for ch in _iter_channels():
        out.extend(_scan_channel(ch))
    return out


@router.get("/catalog")
async def catalog() -> dict:
    beds = _list_beds()
    return {
        "music": [
            {
                "key": b.key,
                "label": b.label,
                "channel": b.channel,
                "filename": b.filename,
                "sample_url": b.sample_url,
            }
            for b in beds
        ],
    }


@router.get("/sample/{channel}/{filename}")
async def sample(channel: str, filename: str) -> FileResponse:
    # Reject path traversal — both segments must be plain identifiers.
    if "/" in channel or ".." in channel or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid path segment")

    target: Path | None = None
    if channel == "shared":
        candidate = DATA_ROOT / "music" / filename
        if candidate.exists() and candidate.is_file():
            target = candidate
    else:
        for sub in _PER_CHANNEL_DIRS:
            candidate = PROJECT_ROOT / channel / sub / filename
            if candidate.exists() and candidate.is_file():
                target = candidate
                break

    if target is None or target.suffix.lower() not in _AUDIO_EXTS:
        raise HTTPException(status_code=404, detail="bed not found")

    return FileResponse(
        str(target),
        media_type=_MEDIA_TYPE[target.suffix.lower()],
        headers={"Cache-Control": "public, max-age=3600"},
    )
