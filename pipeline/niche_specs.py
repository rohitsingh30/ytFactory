"""Niche JSON documents — file-system store for per-channel "niche" specs.

A *niche* describes a Short format authoritatively as a single JSON
document with a fixed schema. Created either by the one-shot backfill
(generated from the existing variant YAMLs) or by the user via the
"Add new niche" flow on `/c/<channel>` (AI pre-fills, user reviews,
saves).

Storage layout: ``<channel_key>/niches/<key>.json``.

Two storage backends, picked transparently per call:

* **GCS** (production / Cloud Run) — when ``YTFACTORY_STATE_BUCKET``
  is set the canonical store is ``gs://<bucket>/<channel>/niches/<key>.json``.
  Reads try GCS first; on miss, fall back to local disk so a partially-
  synced bucket still serves whatever the laptop has on disk. **Writes
  go to GCS only by default** — the laptop's ``<channel>/`` folder is
  no longer silently re-materialized. Set
  ``YTFACTORY_NICHE_DISK_MIRROR=1`` to opt back in to the legacy
  write-through (e.g. when a dev wants to inspect / diff freshly-seeded
  JSONs locally).
* **Local disk only** (laptop dev without the bucket env) — pure
  ``PROJECT_ROOT/<channel>/niches/<key>.json`` semantics, identical
  to the pre-cutover behaviour. Disk IS canonical here, so writes
  always land on disk regardless of the mirror env. This keeps
  ``test_niche_specs.py``'s ``patch.object(niche_specs, "PROJECT_ROOT",
  …)`` pattern working.

If GCS is configured but unreachable (no ADC / network down / bucket
gone), each call logs once and falls through to the disk path so the
dev workflow degrades gracefully rather than 500-ing.

Why the mirror is opt-in (2026-05-11):
  Before this cutover, every ``save_niche`` call (seeder, dashboard
  POST/PUT, AI-draft endpoint) unconditionally re-created
  ``<channel>/niches/`` on the laptop, even when GCS was canonical.
  The user removed all seven channel folders for a clean laptop;
  re-running the seeder against prod GCS silently materialised them
  again. The opt-in mirror keeps a clean laptop clean while letting
  GCS be the one source of truth.

This is intentionally additive — the renderer still reads variant YAMLs
as the source of truth for now. Niche JSONs are a description /
discoverability layer that the create wizard + channel page surface.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SourceKind = Literal[
    "reddit", "wikipedia", "manual", "x_twitter", "youtube", "rss"
]
# Mirror as a runtime set so the schema-migration validator can check
# membership before assigning an unknown value to the strict
# ``source_kind`` Literal field (which would 422 the whole load).
_SOURCE_KIND_VALUES: frozenset[str] = frozenset(SourceKind.__args__)  # type: ignore[attr-defined]

NicheFormat = Literal[
    "animated", "text", "cooking", "footage", "split_screen", "rhyme",
    "footage_only", "long_form", "sports_doc",
]
LengthKind = Literal["short", "long"]
CreatedBy = Literal["backfill", "user", "ai_chat"]


class NicheSource(BaseModel):
    """Nested topic-source descriptor on every niche.

    The persisted JSONs (one per ``<channel>/niches/<key>.json`` blob in
    GCS) use this nested shape, e.g.::

        {"kind": "wikipedia", "ref": "List_of_ancient_civilizations"}
        {"kind": "reddit",    "ref": "AmItheAsshole"}
        {"kind": "manual",    "ref": null}

    ``kind`` is intentionally typed as ``str | None`` (not the strict
    :data:`SourceKind` Literal) so a forward-compat niche with an
    unknown source kind degrades to LLM-only routing rather than
    422-failing the whole document load. Discover-side routing checks
    membership in :data:`_SOURCE_KIND_VALUES` before dispatching.
    """

    kind: Optional[str] = Field(None, max_length=40)
    ref: Optional[str] = Field(None, max_length=200)

    model_config = {"extra": "ignore"}


class NicheDoc(BaseModel):
    """The fixed-schema niche JSON document.

    Persisted at ``<channel>/niches/<key>.json``. ``key`` must match the
    file stem and uniquely identifies the niche within the channel.

    **Source field shape (2026-05-12).** The canonical persisted shape
    nests source under ``source: {kind, ref}`` (see
    :class:`NicheSource`). The legacy flat ``source_kind`` /
    ``source_ref`` fields are kept on the model for back-compat (the
    test suite + a couple of older callers still construct NicheDoc
    with flat kwargs) and are kept in sync via
    :meth:`_migrate_source_shape` — nested wins on conflict, flat is
    only used to synthesise nested when nested is absent. Adding the
    ``source`` field as a structured object also fixes a silent schema
    drift: the GCS JSONs already shipped the nested shape, but the old
    flat-only model dropped it via ``extra: ignore``, so callers that
    read ``niche_doc.source_kind`` always saw the default ``"manual"``.
    """

    key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_]*$", max_length=64)
    label: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=400)
    prompt_style_guide: str = Field(
        "", max_length=1200,
        description="Voice/tone/style guide passed to the LLM during script gen.",
    )
    length_kind: LengthKind = Field(
        "short",
        description=(
            "Format-bucket the niche targets — `short` for ≤90s vertical "
            "Shorts, `long` for multi-minute horizontal long-form."
        ),
    )
    voice: str = Field("sarah", max_length=80)
    format: NicheFormat = "animated"

    # Canonical source descriptor (nested shape — what GCS persists).
    source: Optional[NicheSource] = None

    # Legacy flat aliases kept in sync with ``source`` via the
    # before-validator. Readers should prefer ``source.kind`` /
    # ``source.ref``; these stay around so older constructors and the
    # existing test fixtures keep working without churn.
    source_kind: SourceKind = "manual"
    source_ref: Optional[str] = Field(None, max_length=200)
    hook_template: str = Field("", max_length=240)
    closer_template: str = Field("", max_length=240)
    image_style: str = Field("", max_length=240)
    music_bed: Optional[str] = Field(None, max_length=80)

    # Provenance — never edited by the user, always set by the writer.
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    created_by: CreatedBy = "user"

    model_config = {
        # Tolerate target_length_s + other forward-compat keys left over
        # from older JSONs (e.g. routing/templates/aesthetic — those are
        # GCS-side metadata that the renderer doesn't yet read).
        "extra": "ignore",
    }

    @model_validator(mode="before")
    @classmethod
    def _migrate_source_shape(cls, data):
        """Normalise legacy flat source_kind/source_ref ↔ nested source.

        Precedence: nested ``source`` is canonical when present.
        - nested + flat both set → nested wins, flat is overwritten.
        - nested only → flat fields are populated from it (so back-compat
          readers don't see stale defaults).
        - flat only → nested ``source`` is synthesised from them so the
          new niche-source-driven discover routing has a uniform field
          to read regardless of how the doc was constructed.

        Unknown ``source.kind`` values that aren't in :data:`SourceKind`
        are deliberately NOT mirrored into the strict ``source_kind``
        Literal (it stays as the default ``"manual"``) — this lets a
        future kind degrade gracefully to LLM-only discover routing
        instead of 422-failing the whole niche load.
        """
        if not isinstance(data, dict):
            return data  # coverage: pydantic forwards non-dict only on model rewrap, hard to hit
        nested = data.get("source")
        flat_kind = data.get("source_kind")
        flat_ref = data.get("source_ref")

        if isinstance(nested, dict):
            kind = nested.get("kind")
            ref = nested.get("ref")
            # Mirror nested → flat (only when nested kind is a known
            # SourceKind so we don't poison the Literal-typed flat field).
            if isinstance(kind, str) and kind in _SOURCE_KIND_VALUES:
                data["source_kind"] = kind
            if ref is None or isinstance(ref, str):
                data["source_ref"] = ref
            return data

        # No nested source provided. If flat fields are present,
        # synthesise nested from them so downstream readers don't need
        # to know about the legacy shape.
        if flat_kind is not None or flat_ref is not None:
            data["source"] = {
                "kind": flat_kind if isinstance(flat_kind, str) else None,
                "ref": flat_ref if isinstance(flat_ref, str) else None,
            }

        return data


# ---------------------------------------------------------------------------
# File-system store
# ---------------------------------------------------------------------------


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def _channel_root(channel_key: str) -> Path:
    """Repo-relative channel root. Every channel is a top-level dir whose
    name matches the channel key (see ``CHANNEL_REGISTRY``)."""
    if not _KEY_RE.match(channel_key) and channel_key != channel_key.replace("-", "_"):
        # Defensive — never let a hostile key escape the project root.
        raise ValueError(f"invalid channel key: {channel_key!r}")
    return PROJECT_ROOT / channel_key


def _niches_dir(channel_key: str) -> Path:
    return _channel_root(channel_key) / "niches"


def _niche_path(channel_key: str, key: str) -> Path:
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid niche key: {key!r}")
    return _niches_dir(channel_key) / f"{key}.json"


# ---------------------------------------------------------------------------
# GCS backend (optional — gated on YTFACTORY_STATE_BUCKET)
# ---------------------------------------------------------------------------


_BUCKET_ENV = "YTFACTORY_STATE_BUCKET"
_DISK_MIRROR_ENV = "YTFACTORY_NICHE_DISK_MIRROR"


def _state_bucket() -> Optional[str]:
    """Bucket name from env, or ``None`` for laptop-only operation."""
    name = os.environ.get(_BUCKET_ENV, "").strip()
    return name or None


def _should_disk_mirror() -> bool:
    """Whether ``save_niche`` should write a copy to local disk.

    * No bucket configured → disk IS the canonical store, always write.
    * Bucket configured (cloud-canonical) → only mirror if
      ``YTFACTORY_NICHE_DISK_MIRROR`` is set to a truthy value
      (``1`` / ``true`` / ``yes`` / ``on``). This keeps a freshly
      cleaned laptop from being silently re-populated whenever the
      dashboard, seeder, or AI-draft endpoint runs against prod.
    """
    if _state_bucket() is None:
        return True
    val = os.environ.get(_DISK_MIRROR_ENV, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _gcs_blob_path(channel_key: str, key: str) -> str:
    """GCS object key matching the on-disk layout."""
    return f"{channel_key}/niches/{key}.json"


def _gcs_blob(bucket_name: str, channel_key: str, key: str):
    """Return a GCS Blob handle. Raises on auth / network problems."""
    from google.cloud import storage  # noqa: PLC0415 — lazy

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3")
    client = storage.Client(project=project)
    return client.bucket(bucket_name).blob(_gcs_blob_path(channel_key, key))


def _gcs_load(channel_key: str, key: str) -> Optional[NicheDoc]:
    """Read one niche from GCS. Returns ``None`` on miss (or any error)."""
    bucket = _state_bucket()
    if bucket is None:
        return None
    try:
        blob = _gcs_blob(bucket, channel_key, key)
        if not blob.exists():
            return None
        body = blob.download_as_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS get %s/%s failed; falling back to disk: %s", channel_key, key, exc)
        return None
    try:
        return NicheDoc.model_validate_json(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS niche %s/%s malformed: %s", channel_key, key, exc)
        return None


def _gcs_list(channel_key: str) -> list[NicheDoc]:
    """List niches under ``<channel>/niches/`` from GCS. ``[]`` on any failure."""
    bucket = _state_bucket()
    if bucket is None:
        return []
    try:
        from google.cloud import storage  # noqa: PLC0415 — lazy

        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3")
        client = storage.Client(project=project)
        prefix = f"{channel_key}/niches/"
        blobs = client.list_blobs(bucket, prefix=prefix)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS list %s/niches/ failed; falling back to disk: %s", channel_key, exc)
        return []
    out: list[NicheDoc] = []
    for blob in blobs:
        # Skip nested subdirs (we only enumerate flat <channel>/niches/<k>.json).
        rel = blob.name[len(prefix):]
        if not rel or "/" in rel or not rel.endswith(".json"):
            continue
        try:
            out.append(NicheDoc.model_validate_json(blob.download_as_bytes()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed GCS niche %s: %s", blob.name, exc)
    return out


def _gcs_save(channel_key: str, doc: NicheDoc) -> bool:
    """Write ``doc`` to GCS. Returns ``True`` on success, ``False`` if no bucket
    is configured or the upload failed (caller still writes to disk so a dev
    without ADC isn't blocked)."""
    bucket = _state_bucket()
    if bucket is None:
        return False
    try:
        blob = _gcs_blob(bucket, channel_key, doc.key)
        blob.upload_from_string(
            doc.model_dump_json(indent=2) + "\n",
            content_type="application/json",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS put %s/%s failed; will rely on disk: %s", channel_key, doc.key, exc)
        return False


def _gcs_delete(channel_key: str, key: str) -> bool:
    """Delete the GCS object. Returns ``True`` if a blob was removed."""
    bucket = _state_bucket()
    if bucket is None:
        return False
    try:
        blob = _gcs_blob(bucket, channel_key, key)
        if not blob.exists():
            return False
        blob.delete()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS delete %s/%s failed: %s", channel_key, key, exc)
        return False


# ---------------------------------------------------------------------------
# Public CRUD — GCS-first, disk-fallback
# ---------------------------------------------------------------------------


def list_niches(channel_key: str) -> list[NicheDoc]:
    """Return every niche JSON under the channel, sorted by label.

    When ``YTFACTORY_STATE_BUCKET`` is set, GCS is the source of truth
    and any disk-only entries are merged in on top (deduped by key,
    GCS wins) so a partially-synced bucket still surfaces local docs.
    """
    by_key: dict[str, NicheDoc] = {}
    for doc in _gcs_list(channel_key):
        by_key[doc.key] = doc

    nd = _niches_dir(channel_key)
    if nd.exists():
        for p in sorted(nd.glob("*.json")):
            try:
                doc = NicheDoc.model_validate_json(p.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("skipping malformed niche JSON %s: %s", p, exc)
                continue
            by_key.setdefault(doc.key, doc)

    return sorted(by_key.values(), key=lambda d: d.label.lower())


def get_niche(channel_key: str, key: str) -> Optional[NicheDoc]:
    """Read one niche; tries GCS first then disk."""
    doc = _gcs_load(channel_key, key)
    if doc is not None:
        return doc

    p = _niche_path(channel_key, key)
    if not p.exists():
        return None
    try:
        return NicheDoc.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("niche %s/%s malformed: %s", channel_key, key, exc)
        return None


def save_niche(channel_key: str, doc: NicheDoc) -> NicheDoc:
    """Persist ``doc``.

    Routing:

    * Always attempt the GCS write (no-op when no bucket is configured).
    * Mirror to local disk **only when** ``_should_disk_mirror()``
      returns True — i.e. either no bucket is set (disk IS canonical)
      or the explicit ``YTFACTORY_NICHE_DISK_MIRROR`` opt-in is on.
      Without the opt-in, a cloud-canonical save NEVER re-creates the
      laptop's ``<channel>/niches/`` folder, so a deliberately-cleaned
      laptop stays clean.

    Overwrites if present. Returns the doc as written (round-tripped).
    """
    _gcs_save(channel_key, doc)

    if not _should_disk_mirror():
        return doc

    nd = _niches_dir(channel_key)
    try:
        nd.mkdir(parents=True, exist_ok=True)
        target = _niche_path(channel_key, doc.key)
        tmp = target.with_suffix(".json.tmp")
        payload = doc.model_dump_json(indent=2)
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        # Read-only FS in cloud containers — GCS write was the canonical
        # one anyway; surface to logs but don't fail the call.
        if _state_bucket() is None:
            raise
        logger.warning("disk write-through %s/%s failed (cloud-only mode): %s",
                       channel_key, doc.key, exc)

    return doc


def delete_niche(channel_key: str, key: str) -> bool:
    """Delete the JSON. Returns ``True`` if a doc was removed from
    either backend (so a doc that only exists in GCS still reports
    deleted, and vice versa)."""
    removed_gcs = _gcs_delete(channel_key, key)

    removed_disk = False
    p = _niche_path(channel_key, key)
    if p.exists():
        p.unlink()
        removed_disk = True

    return removed_gcs or removed_disk


def slugify_label(label: str) -> str:
    """Generate a niche key from a free-form label.

    Lowercase, alpha-numeric + underscore only, leading non-alpha
    stripped. Falls back to ``"niche"`` if the label has no usable
    characters.
    """
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s or not s[0].isalnum():
        s = f"niche_{s}".strip("_") or "niche"
    return s[:64]
