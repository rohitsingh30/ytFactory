"""Voice catalog — name → (ref WAV path, transcript) resolver.

Skills declare a voice by NAME (e.g. ``tts_voice: hindi-female-storyteller``)
instead of duplicating WAV paths + transcripts in every channel config.

The catalog source-of-truth is ``pipeline/voice_refs/catalog.yaml``.
WAV + transcript files live next to it under ``pipeline/voice_refs/``
so they're committed to the repo and cloud-rendering containers get
them via the same base64 wire format as path-style refs.

Both name-style and path-style are accepted by callers. Detection:
a value containing "/" or ending in ".wav" is treated as a path; any
other non-empty string is looked up in the catalog. Empty strings
mean "no voice ref" (description-driven providers like indicparler).

Public API:
    resolve_voice(name_or_path, project_root) -> (Path, transcript)
    list_voices(project_root) -> list[VoiceEntry]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceEntry:
    name: str
    path: Path           # absolute path to ref.wav
    transcript: str      # spoken transcript of the wav
    language: str        # iso code (en, hi, ...)
    register: str        # free-text style hint
    duration_s: float
    use_cases: tuple[str, ...]
    notes: str = ""


@lru_cache(maxsize=1)
def _load_catalog(project_root_str: str) -> dict[str, VoiceEntry]:
    """Parse ``pipeline/voice_refs/catalog.yaml`` once per process."""
    import yaml  # noqa: PLC0415 — keep top-level imports light

    project_root = Path(project_root_str)
    cat_path = project_root / "pipeline" / "voice_refs" / "catalog.yaml"
    if not cat_path.exists():
        logger.warning(
            "voice catalog not found at %s — name-style lookups will all miss",
            cat_path,
        )
        return {}
    raw = yaml.safe_load(cat_path.read_text()) or {}
    entries: dict[str, VoiceEntry] = {}
    for name, body in (raw.get("voices") or {}).items():
        wav = (project_root / body["path"]).resolve()
        if not wav.exists():
            logger.warning(
                "voice %r catalog entry points at missing WAV %s — skipping",
                name, wav,
            )
            continue
        entries[name] = VoiceEntry(
            name=name,
            path=wav,
            transcript=body.get("transcript", ""),
            language=body.get("language", ""),
            register=body.get("register", ""),
            duration_s=float(body.get("duration_s", 0.0)),
            use_cases=tuple(body.get("use_cases") or ()),
            notes=body.get("notes", ""),
        )
    return entries


def _looks_like_path(value: str) -> bool:
    return "/" in value or value.endswith(".wav")


def resolve_voice(
    name_or_path: str,
    project_root: Path | str,
) -> tuple[Path | None, str]:
    """Resolve ``tts_voice`` (name or path) to (absolute path, transcript).

    Resolution order:

    1. Empty string → ``(None, "")`` — caller treats as no-ref-WAV (the
       description-driven providers like indicparler accept this).
    2. Path-style (contains "/" or ends ".wav"): returned as-is, plus a
       sibling ``<basename>.txt`` / ``ref.txt`` transcript if found.
    3. Catalog name (in ``pipeline/voice_refs/catalog.yaml``): full
       VoiceEntry — most reliable for tested production voices.
    4. **Bare-name fallback** (added 2026-05-15): the value is treated
       as a voice id and we probe two on-disk locations under
       ``pipeline/voice_refs/``:

         - ``<name>.wav``         (legacy single-file layout — sarah,
                                   michael, theo, sports_male_intense)
         - ``<name>/ref.wav``     (newer per-voice-folder layout)

       If found, returns the resolved WAV + the matching transcript
       sidecar (``<name>.txt`` next to the wav, or ``<name>/ref.txt``
       in the folder layout).

    5. Nothing matched → raises ``ValueError`` listing the catalog
       entries AND the on-disk voices we discovered, so the operator
       can see exactly which names are valid.

    The bare-name fallback unblocks the wizard form which sends voice
    ids like ``"sarah"`` (the basename only — discovered via filesystem
    listing of ``voice_refs/*.wav``). Pre-2026-05-15, the dispatcher
    never called this function so the bare name leaked through to
    ``Path("sarah").read_bytes()`` → FileNotFoundError on every prorevenge
    render. Surfaced by job 215e411b canary.
    """
    if not name_or_path:
        return None, ""
    project_root = Path(project_root)
    if _looks_like_path(name_or_path):
        wav = project_root / name_or_path
        # Look for transcript sidecar: prefer <stem>.txt, then ref.txt.
        candidates = [
            wav.with_suffix(".txt"),
            wav.parent / "ref.txt",
        ]
        transcript = ""
        for c in candidates:
            if c.exists():
                transcript = c.read_text().strip()
                break
        return wav.resolve(), transcript
    # Name-style — catalog lookup first.
    catalog = _load_catalog(str(project_root))
    if name_or_path in catalog:
        entry = catalog[name_or_path]
        return entry.path, entry.transcript
    # Bare-name fallback: probe voice_refs/<name>.wav and
    # voice_refs/<name>/ref.wav on disk.
    voice_refs_dir = project_root / "pipeline" / "voice_refs"
    flat_wav = voice_refs_dir / f"{name_or_path}.wav"
    nested_wav = voice_refs_dir / name_or_path / "ref.wav"
    for wav, tx_candidates in (
        (flat_wav, [flat_wav.with_suffix(".txt")]),
        (nested_wav, [nested_wav.with_suffix(".txt"), nested_wav.parent / "ref.txt"]),
    ):
        if wav.exists():
            transcript = ""
            for tx in tx_candidates:
                if tx.exists():
                    transcript = tx.read_text().strip()
                    break
            return wav.resolve(), transcript
    # Nothing matched — give the operator a useful error message.
    catalog_names = ", ".join(sorted(catalog)) or "(empty catalog)"
    on_disk_flat = sorted(p.stem for p in voice_refs_dir.glob("*.wav"))
    on_disk_nested = sorted(
        p.parent.name for p in voice_refs_dir.glob("*/ref.wav")
    )
    on_disk = ", ".join(on_disk_flat + on_disk_nested) or "(none)"
    raise ValueError(
        f"voice {name_or_path!r} not found. "
        f"Catalog entries: {catalog_names}. "
        f"On-disk voices (no catalog entry): {on_disk}. "
        f"Either add it to {voice_refs_dir / 'catalog.yaml'} or "
        f"drop a wav at {voice_refs_dir}/{name_or_path}.wav."
    )


def list_voices(project_root: Path | str) -> list[VoiceEntry]:
    return list(_load_catalog(str(Path(project_root))).values())
