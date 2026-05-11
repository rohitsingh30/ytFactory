"""Song-sample preview audio for the SongPicker tiles.

GET /api/songs/sample/{filename}

Serves short MP3 clips from ``data/song_samples/`` so the create-page
SongPicker can render an inline audition button next to each Female /
Male vocal-gender option. Generated once via
``scripts/build_song_samples.py`` (Suno via the sunoapi.org wrapper);
served as static files thereafter.

Kept as a separate route from /api/music/sample/* on purpose — the
music-bed catalogue scans ``data/music/`` and would otherwise pull
these in as if they were beds.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from pipeline.paths import DATA_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/songs")

_SAMPLES_DIR = DATA_ROOT / "song_samples"
_AUDIO_EXTS = (".mp3", ".wav", ".m4a")
_MEDIA_TYPE = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


@router.get("/sample/{filename}")
async def sample(filename: str) -> FileResponse:
    # Reject path traversal — filename must be a plain identifier.
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")

    candidate = _SAMPLES_DIR / filename
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="sample not found")
    if candidate.suffix.lower() not in _AUDIO_EXTS:
        raise HTTPException(status_code=404, detail="not an audio file")

    return FileResponse(
        str(candidate),
        media_type=_MEDIA_TYPE[candidate.suffix.lower()],
        headers={"Cache-Control": "public, max-age=86400"},
    )
