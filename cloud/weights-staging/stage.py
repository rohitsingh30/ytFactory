"""ytFactory weights staging — pull HF snapshots into a GCS bucket.

Architecture (after 2026-05-06 GCS-Fuse-write thrash):
  download → /tmp/hf-stage (in-memory tmpfs, fast, no Fuse latency)
  upload   → gs://ytfactory-model-weights via google-cloud-storage SDK
             (transfer_manager parallel workers, multipart-friendly)
  free /tmp between repos so Qwen-Image (20 GB) fits in 32 GiB Job memory

Why not GCS Fuse for writes:
  hf_transfer's parallel chunk writes saturated Fuse's upload buffer →
  "chunk upload failed after 4 attempts, context deadline exceeded".
  Disabling hf_transfer gave only 60 Mbps. Two-stage gets ~500 Mbps.

Re-runnable: SDK upload uses if-generation-match=0 to skip-if-exists
(idempotent). Re-invocation is a no-op once a repo is fully present.

Usage:
  REPOS=org/repo1,org/repo2 python stage.py
  python stage.py org/repo1 org/repo2
  python stage.py
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path

from huggingface_hub import snapshot_download
from google.cloud import storage
from google.cloud.storage import transfer_manager

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("ytfactory.weights-staging")

DEFAULT_REPOS = [
    "PierrunoYT/higgs-audio-v2-generation-3B-base",
    "PierrunoYT/higgs-audio-v2-tokenizer",
    "ai4bharat/IndicF5",
    "ai4bharat/indic-parler-tts",
    "SWivid/F5-TTS",
    "ResembleAI/chatterbox",
    "FunAudioLLM/CosyVoice2-0.5B",
    "Qwen/Qwen-Image",
]

STAGE_ROOT = Path(os.environ.get("STAGE_ROOT", "/tmp/hf-stage"))
BUCKET_NAME = os.environ.get("BUCKET_NAME", "ytfactory-model-weights")
UPLOAD_WORKERS = int(os.environ.get("UPLOAD_WORKERS", "16"))


def _resolve_repos() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    env_val = os.environ.get("REPOS")
    if env_val:
        return [r.strip() for r in env_val.split(",") if r.strip()]
    return DEFAULT_REPOS


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            out.append(p)
    return out


def _upload_dir(client: storage.Client, local_root: Path, gcs_prefix: str) -> tuple[int, int]:
    """Upload every file under local_root to gs://BUCKET/<gcs_prefix>/<relpath>.

    Returns (uploaded_files, total_bytes). Skips files that already exist
    (idempotent re-runs)."""
    bucket = client.bucket(BUCKET_NAME)
    files = _walk_files(local_root)
    if not files:
        return 0, 0

    rel_paths = [str(f.relative_to(local_root)) for f in files]
    blob_names = [f"{gcs_prefix.rstrip('/')}/{rp}" for rp in rel_paths]

    # Probe which blobs already exist; skip those.
    existing: set[str] = set()
    for name in blob_names:
        if bucket.blob(name).exists(client):
            existing.add(name)

    pending_files: list[Path] = []
    pending_blobs: list[str] = []
    for f, blob_name in zip(files, blob_names):
        if blob_name in existing:
            continue
        pending_files.append(f)
        pending_blobs.append(blob_name)

    if not pending_files:
        log.info("  → all %d files already in GCS, skipping upload", len(files))
        return 0, 0

    log.info("  → uploading %d/%d files (%d already present)",
             len(pending_files), len(files), len(existing))

    # transfer_manager.upload_many handles parallel uploads.
    blob_to_local = {
        blob_name: str(f) for blob_name, f in zip(pending_blobs, pending_files)
    }
    results = transfer_manager.upload_many_from_filenames(
        bucket,
        list(blob_to_local.keys()),
        source_directory="",
        max_workers=UPLOAD_WORKERS,
        worker_type=transfer_manager.PROCESS,
        upload_kwargs={"if_generation_match": None},
        # transfer_manager expects relative names; pass full paths via blob_name_prefix=""
        blob_name_prefix="",
    )
    # Note: upload_many_from_filenames ties (blob_name, source_path) by index
    # via the source_directory + filenames mapping. Re-do with explicit pairs.
    # The simpler path: use upload_many with (blob, source_path) tuples.

    bytes_total = 0
    for f in pending_files:
        bytes_total += f.stat().st_size
    return len(pending_files), bytes_total


def _upload_dir_simple(client: storage.Client, local_root: Path, gcs_prefix: str) -> tuple[int, int]:
    """Simpler explicit upload — avoids transfer_manager API quirks.
    Parallel via concurrent.futures, idempotent via blob.exists() probe."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    bucket = client.bucket(BUCKET_NAME)
    files = _walk_files(local_root)
    if not files:
        return 0, 0

    def _upload_one(local_path: Path) -> tuple[bool, int]:
        rel = local_path.relative_to(local_root)
        blob_name = f"{gcs_prefix.rstrip('/')}/{rel}"
        blob = bucket.blob(blob_name)
        # Skip if exists with same size — fast resume after partial run
        if blob.exists(client):
            blob.reload()
            if blob.size == local_path.stat().st_size:
                return False, 0  # already done
        blob.upload_from_filename(str(local_path), timeout=600)
        return True, local_path.stat().st_size

    uploaded = 0
    bytes_total = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
        futures = [ex.submit(_upload_one, f) for f in files]
        for fut in as_completed(futures):
            did, n = fut.result()
            if did:
                uploaded += 1
                bytes_total += n
            else:
                skipped += 1
    log.info("  → %d uploaded, %d already present, %.2f GB transferred",
             uploaded, skipped, bytes_total / 1e9)
    return uploaded, bytes_total


def _stage_one(repo: str, token: str | None, client: storage.Client) -> bool:
    log.info("=== %s ===", repo)
    t0 = time.time()
    try:
        # snapshot_download writes to STAGE_ROOT/hub/models--<org>--<name>/...
        local_path = snapshot_download(
            repo_id=repo,
            cache_dir=str(STAGE_ROOT / "hub"),
            token=token,
        )
    except Exception as e:
        log.exception("snapshot_download FAILED %s: %s", repo, e)
        return False
    dl_s = time.time() - t0
    log.info("  downloaded %s → %s (%.1fs)", repo, local_path, dl_s)

    # Resolve the cache root for this repo (e.g. /tmp/hf-stage/hub/models--<org>--<name>)
    repo_dir_name = "models--" + repo.replace("/", "--")
    repo_cache = STAGE_ROOT / "hub" / repo_dir_name
    if not repo_cache.exists():
        log.error("expected cache dir not found: %s", repo_cache)
        return False

    # Upload preserving the hub-cache layout under gs://BUCKET/hub/<repo_dir>/
    t1 = time.time()
    _upload_dir_simple(client, repo_cache, f"hub/{repo_dir_name}")
    up_s = time.time() - t1
    log.info("  uploaded %s in %.1fs (download %.1fs, upload %.1fs, total %.1fs)",
             repo, up_s, dl_s, up_s, time.time() - t0)

    # Free tmpfs before next repo
    shutil.rmtree(repo_cache, ignore_errors=True)
    return True


def main() -> int:
    repos = _resolve_repos()
    token = os.environ.get("HF_TOKEN") or None
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    log.info("STAGE_ROOT=%s BUCKET=%s repos=%d workers=%d",
             STAGE_ROOT, BUCKET_NAME, len(repos), UPLOAD_WORKERS)

    client = storage.Client()
    failures: list[str] = []
    t0 = time.time()
    for i, repo in enumerate(repos, 1):
        log.info("[%d/%d] %s", i, len(repos), repo)
        ok = _stage_one(repo, token, client)
        if not ok:
            failures.append(repo)

    log.info("DONE in %.1fs — %d ok, %d failed", time.time() - t0,
             len(repos) - len(failures), len(failures))
    if failures:
        for f in failures:
            log.error("FAILED: %s", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
