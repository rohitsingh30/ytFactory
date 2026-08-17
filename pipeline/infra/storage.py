"""Storage — the only place artifact paths are built. Local-filesystem impl; GCS slots behind the same Protocol."""
from __future__ import annotations

import shutil
from pathlib import Path


class LocalStorage:
    """Storage backed by a local root dir. ``put`` copies a file to ``root/<key>``; ``get`` copies it back out.

    Keys are POSIX-style relative paths (e.g. ``jobs/<id>/short.mp4``). A GCS-backed Storage
    with the same ``put`` / ``get`` shape is the production swap."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # contain within root — reject keys that traverse out of it
        dest = (self._root / key).resolve()
        root = self._root.resolve()
        if dest != root and root not in dest.parents:
            raise ValueError(f"Storage: key '{key}' escapes storage root {root}")
        return dest

    def put(self, local: Path, key: str) -> str:
        src = Path(local)
        if not src.exists():
            raise FileNotFoundError(f"Storage.put: source {src} does not exist")
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return str(dest)

    def get(self, key: str, local: Path) -> Path:
        src = self._resolve(key)
        if not src.exists():
            raise FileNotFoundError(f"Storage.get: key '{key}' not in storage ({src})")
        dest = Path(local)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest
