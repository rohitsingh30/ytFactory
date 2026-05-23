"""Guard test: every *.py file under cloud/<svc>/ must appear in a
COPY line in the matching cloud/<svc>/Dockerfile.

Background: see docs/cloud_service_dockerfile_discipline.md.

Why this test exists: 2026-05-23 incident — a new file added to
cloud/render-worker-v2/ (telemetry's _stage_envelope.py) wasn't
copied into the image, every render died at startup. Worse, the
same omission of _tel_track_io.py in 3 other services was silent
(try/except softener) and went undetected. ~3 hours of operator
time lost to a "deploy is complete" claim that was structurally
false.

The test enumerates every cloud/<svc>/*.py and asserts each is
named in a COPY directive in the matching Dockerfile. If the
Dockerfile uses a wholesale `COPY cloud/<svc>/ ./` or `COPY . .`
the check for that service is skipped (the wholesale form is
self-explanatory; the explicit form is what drifts).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_DIR = REPO_ROOT / "cloud"

# Services to check. Anything in cloud/ with both a Dockerfile and
# at least one *.py file is in scope.
_EXCLUDE_DIRS = {"_shared", "iam"}


def _service_dirs() -> list[Path]:
    out: list[Path] = []
    for child in sorted(CLOUD_DIR.iterdir()):
        if not child.is_dir() or child.name in _EXCLUDE_DIRS:
            continue
        if not (child / "Dockerfile").exists():
            continue
        if not any(child.glob("*.py")):
            continue
        out.append(child)
    return out


def _dockerfile_uses_wholesale_copy(dockerfile_text: str, svc: str) -> bool:
    """True iff the Dockerfile copies the service dir wholesale.

    Recognises:
        COPY . .
        COPY cloud/<svc>/ ./
        COPY cloud/<svc> ./
        COPY ./cloud/<svc>/ ./
    Any of these means we don't need to spot-check individual files.
    """
    patterns = (
        rf"^COPY\s+\.\s+\.\s*$",
        rf"^COPY\s+\./?cloud/{re.escape(svc)}/?\s+\./?\s*$",
        rf"^COPY\s+cloud/{re.escape(svc)}/?\s+\./?\s*$",
    )
    for line in dockerfile_text.splitlines():
        line = line.strip()
        for pat in patterns:
            if re.match(pat, line):
                return True
    return False


def _files_copied_explicitly(dockerfile_text: str) -> set[str]:
    """Set of basenames the Dockerfile explicitly COPYs.

    Catches both shapes used in this repo:
        COPY server.py ./
        COPY cloud/render-worker-v2/entrypoint.py /workspace/...
    """
    out: set[str] = set()
    copy_re = re.compile(r"^\s*COPY(?:\s+--\S+)*\s+(\S+)\s+(\S+)\s*$")
    for line in dockerfile_text.splitlines():
        m = copy_re.match(line)
        if not m:
            continue
        src = m.group(1)
        # Strip any directory prefix; we only care about the basename
        # (the test asserts presence-by-filename, not by full path).
        out.add(Path(src).name)
    return out


@pytest.mark.parametrize("svc_dir", _service_dirs(), ids=lambda p: p.name)
def test_every_py_file_is_copied(svc_dir: Path) -> None:
    dockerfile = svc_dir / "Dockerfile"
    text = dockerfile.read_text()
    svc = svc_dir.name

    if _dockerfile_uses_wholesale_copy(text, svc):
        # Service uses `COPY . .` or `COPY cloud/<svc>/ ./` — no
        # per-file drift possible. Skip the spot-check.
        return

    copied = _files_copied_explicitly(text)
    expected = {p.name for p in svc_dir.glob("*.py")}
    # Test files are not deployed; allow them to be uncopied.
    expected = {n for n in expected if not n.startswith("test_")}

    missing = expected - copied
    assert not missing, (
        f"cloud/{svc}/Dockerfile is missing COPY lines for: {sorted(missing)}.\n"
        f"  Source files in cloud/{svc}/: {sorted(expected)}\n"
        f"  Files COPY'd by Dockerfile:  {sorted(copied & expected)}\n"
        f"\n"
        f"Either add explicit COPY lines for each missing file, OR\n"
        f"refactor to a wholesale `COPY cloud/{svc}/ ./` (with a\n"
        f".dockerignore). See docs/cloud_service_dockerfile_discipline.md."
    )
