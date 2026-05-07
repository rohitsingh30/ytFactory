"""Phase-0 validator for cloud/weights-staging/stage.py.

Runs both `_stage_one` (HF cache layout) and `_stage_one_flat`
(flat layout for diffusers `from_pretrained(local_files_only=True)`)
against a TINY HF repo, mocking the GCS client so no bucket is
touched, then attempts to load the result via diffusers.

Used to catch the class of bug "stage.py looks fine but the bucket
layout it produces is unloadable by the consumer" — discovered
2026-05-07 when the original `_walk_files` skipped every symlink in
the HF cache, leaving `snapshots/<sha>/file` entries as empty in the
bucket and breaking `from_pretrained(local_files_only=True)`.

Run before any change to stage.py / the staging Job + after any
diffusers / huggingface_hub upgrade. Takes ~30 s (downloads ~17 MB).

  python cloud/weights-staging/validate_layout.py

Exit 0 = both layouts load. Non-zero = something regressed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

WORKDIR = Path(tempfile.mkdtemp(prefix="ytfactory-stage-validate-"))
print(f"workdir: {WORKDIR}")

sys.path.insert(0, str(Path(__file__).parent))
os.environ["STAGE_ROOT"] = str(WORKDIR / "stage-root")
os.environ["BUCKET_NAME"] = "validate-bucket"
os.environ["UPLOAD_WORKERS"] = "4"

import stage  # noqa: E402

SIMULATED_BUCKET = WORKDIR / "simulated-bucket"
SIMULATED_BUCKET.mkdir()


class _FakeBlob:
    def __init__(self, name: str):
        self._name = name
        self.size = 0

    def exists(self, client) -> bool:
        return (SIMULATED_BUCKET / self._name).exists()

    def reload(self) -> None:
        out = SIMULATED_BUCKET / self._name
        self.size = out.stat().st_size if out.exists() else 0

    def upload_from_filename(self, src: str, *, timeout: int = 600) -> None:
        out = SIMULATED_BUCKET / self._name
        out.parent.mkdir(parents=True, exist_ok=True)
        # `shutil.copyfile` follows symlinks — same behaviour as
        # `google.cloud.storage`'s `blob.upload_from_filename` which
        # reads through the symlink to upload target bytes.
        shutil.copyfile(src, out)


class _FakeBucket:
    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name)


class _FakeClient:
    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket()


fake_client = _FakeClient()

TINY_REPO = "hf-internal-testing/tiny-stable-diffusion-pipe"

# ---- FLAT layout ---------------------------------------------------------
print(f"\n--- _stage_one_flat({TINY_REPO!r}) ---")
stage.FLAT_LAYOUT_REPOS = {TINY_REPO}
ok = stage._stage_one_flat(TINY_REPO, token=None, client=fake_client)
flat_dir = SIMULATED_BUCKET / f"flat/{TINY_REPO}"
flat_n = sum(1 for p in flat_dir.rglob("*") if p.is_file())
flat_bytes = sum(p.stat().st_size for p in flat_dir.rglob("*") if p.is_file())
print(f"  uploaded {flat_n} files / {flat_bytes:,} bytes")

import torch  # noqa: E402
from diffusers import DiffusionPipeline  # noqa: E402

flat_load_ok = False
try:
    pipe = DiffusionPipeline.from_pretrained(
        str(flat_dir), torch_dtype=torch.float16, local_files_only=True,
    )
    print(f"  ✅ FLAT loads — pipe={type(pipe).__name__}")
    flat_load_ok = True
except Exception as e:
    print(f"  ❌ FLAT load failed — {type(e).__name__}: {str(e)[:200]}")

# ---- CACHE layout (with symlink-follow patch) ---------------------------
print(f"\n--- _stage_one({TINY_REPO!r}) (cache layout, symlink-follow) ---")
stage.FLAT_LAYOUT_REPOS = set()
ok = stage._stage_one(TINY_REPO, token=None, client=fake_client)
repo_dir_name = "models--" + TINY_REPO.replace("/", "--")
hub_dir = SIMULATED_BUCKET / "hub" / repo_dir_name
snap_root = hub_dir / "snapshots"
snap_dirs = [d for d in snap_root.iterdir() if d.is_dir()] if snap_root.exists() else []
snap_files = [p for p in snap_root.rglob("*") if p.is_file()] if snap_root.exists() else []
print(f"  cache layout: {sum(1 for p in hub_dir.rglob('*') if p.is_file())} files, "
      f"{len(snap_files)} in snapshots/<sha>/")

cache_load_ok = False
if snap_dirs:
    try:
        pipe = DiffusionPipeline.from_pretrained(
            str(snap_dirs[0]), torch_dtype=torch.float16, local_files_only=True,
        )
        print(f"  ✅ CACHE loads — pipe={type(pipe).__name__}")
        cache_load_ok = True
    except Exception as e:
        print(f"  ❌ CACHE load failed — {type(e).__name__}: {str(e)[:200]}")

print("\n=== SUMMARY ===")
print(f"  {'✅' if flat_load_ok else '❌'} FLAT  layout via _stage_one_flat → from_pretrained(local_files_only=True)")
print(f"  {'✅' if cache_load_ok else '❌'} CACHE layout via _stage_one (symlink-follow fix) → from_pretrained(local_files_only=True)")
shutil.rmtree(WORKDIR, ignore_errors=True)
sys.exit(0 if (flat_load_ok and cache_load_ok) else 1)
