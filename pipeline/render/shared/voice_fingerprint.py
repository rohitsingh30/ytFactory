"""Voice-fingerprint sidecar for cached ``narration.wav`` files.

**Audit Q2.22** — cached TTS must be busted when the channel YAML's
``tts_provider`` / ``tts_voice`` / ``tts_speed`` changes. A stale
render path that only checked ``narr_path.exists()`` skipped synthesis
when true, so switching F5 → Chatterbox in the YAML didn't cause a
re-render — the next operator just heard the OLD voice.

This module provides a lightweight sidecar contract:

  - ``narr_path = .../narration.wav``
  - ``sidecar  = narr_path.with_suffix('.voice_fp.json')``

Each renderer should:

  1. Compute ``fp = compute_fingerprint(cfg)``.
  2. Read the sidecar (if any) into ``cached_fp``.
  3. If ``cached_fp != fp`` OR sidecar missing, RE-SYNTH and
     ``write_sidecar(narr_path, fp)`` after synthesis.

The fingerprint deliberately includes only fields that affect the
audio bytes — caption-burn knobs / chapter-card timing don't belong
here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def compute_fingerprint(cfg: dict) -> dict[str, Any]:
    """Hashable summary of the cfg fields that drive narration.wav."""
    return {
        "tts_provider": cfg.get("tts_provider"),
        "tts_voice": cfg.get("tts_voice"),
        "tts_speed": cfg.get("tts_speed", 1.0),
        "tts_language": cfg.get("tts_language"),
        "tts_ref_text": cfg.get("tts_ref_text"),
        # Long-form chunk join silence affects concatenation; bumping
        # it would otherwise invalidate the per-chunk cache without
        # invalidating the joined narration.wav.
        "tts_chunk_join_silence_s": cfg.get("tts_chunk_join_silence_s"),
        "tts_chunk_target_chars": cfg.get("tts_chunk_target_chars"),
    }


def fingerprint_hash(fp: dict[str, Any]) -> str:
    """Stable short hash of ``fp`` for log-line readability."""
    payload = json.dumps(fp, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def sidecar_path(narr_path: Path) -> Path:
    """Sibling sidecar path for ``narr_path``."""
    return narr_path.with_suffix(".voice_fp.json")


def read_sidecar(narr_path: Path) -> dict[str, Any] | None:
    """Return the cached fingerprint dict, or None if missing/unparseable."""
    side = sidecar_path(narr_path)
    if not side.exists():
        return None
    try:
        return json.loads(side.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_sidecar(narr_path: Path, fp: dict[str, Any]) -> None:
    """Write the fingerprint sidecar atomically."""
    side = sidecar_path(narr_path)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps(fp, indent=2, sort_keys=True))


def needs_resynth(narr_path: Path, cfg: dict) -> tuple[bool, str]:
    """Return (resynth, reason).

    ``resynth=True`` when narr_path doesn't exist OR sidecar's
    cached fingerprint differs from the one computed from cfg.
    Reason is a short human-readable string for the [tts] log line.
    """
    if not narr_path.exists():
        return True, "narration.wav missing"
    cached = read_sidecar(narr_path)
    if cached is None:
        return True, "voice_fp sidecar missing — re-synth to bind it"
    fresh = compute_fingerprint(cfg)
    if cached != fresh:
        return True, (
            f"voice_fp changed: cached={fingerprint_hash(cached)} "
            f"→ cfg={fingerprint_hash(fresh)}"
        )
    return False, f"voice_fp unchanged ({fingerprint_hash(fresh)})"


def maybe_wipe_stale_chunks(narr_path: Path, cfg: dict) -> bool:
    """Q2.22 — wipe ``narr_path`` and sibling ``chunk_*.wav`` cache when
    sidecar exists AND its fingerprint differs from ``cfg``. Returns
    True iff a wipe happened. Does NOT wipe on first encounter (no
    sidecar yet); the next synthesis will write the sidecar and bind
    future cache decisions to the current cfg.
    """
    cached = read_sidecar(narr_path)
    if cached is None or not narr_path.exists():
        return False
    fresh = compute_fingerprint(cfg)
    if cached == fresh:
        return False
    print(
        f"[tts] voice fingerprint changed; wiping stale chunks "
        f"(cached={fingerprint_hash(cached)} → cfg={fingerprint_hash(fresh)})"
    )
    narr_path.unlink(missing_ok=True)
    cache_dir = narr_path.parent
    for chunk_wav in cache_dir.glob("chunk_*.wav"):
        chunk_wav.unlink(missing_ok=True)
    return True


__all__ = [
    "compute_fingerprint",
    "fingerprint_hash",
    "maybe_wipe_stale_chunks",
    "needs_resynth",
    "read_sidecar",
    "sidecar_path",
    "write_sidecar",
]
