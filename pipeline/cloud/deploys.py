"""Per-service deploy snapshot — last build + 6-step playbook prep status.

Two data sources:

1. ``gcloud builds list --project <P> --limit 200 --format=json`` —
   recent Cloud Build runs. We derive the service from the first tag
   that matches one of our known service names, falling back to the
   ``substitutions._SERVICE`` field if we encoded it in
   ``cloudbuild.yaml``.
2. ``cloud/<service>/.deploy_prep/`` — the prep dir the
   ``deploy-cloud-service`` workflow writes (steps 1-6 of the
   ``docs/cloud_service_dep_playbook.md``). Each step that's been
   completed leaves a ``stepN.ok`` marker. Counts surface as e.g.
   ``5/6 prepared`` so the operator can see a half-done service.

Falls back gracefully when ``gcloud`` isn't on $PATH (returns rows with
``last_build_status='gcloud_unavailable'`` so the panel still renders
the prep column).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .services import Service, list_services

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_DIR = REPO_ROOT / "cloud"
PREP_DIRNAME = ".deploy_prep"
PREP_STEPS = (
    ("step1_upstream_reqs", "Read upstream requirements.txt + pyproject.toml in FULL"),
    ("step2_grep_imports", "Grep upstream source for ALL imports"),
    ("step3_resolve_conflicts", "Resolve transitive conflicts vs cross-cutting pins"),
    ("step4_dryrun", "pip install --dry-run -r requirements.txt LOCALLY"),
    ("step5_verify_dryrun", "Verify 'Would install' output has no surprises"),
    ("step6_built_once", "Built once on Cloud Build"),
)


@dataclass
class PrepStatus:
    """One row per playbook step."""

    key: str
    label: str
    done: bool
    artifact: Optional[str] = None  # relative path of the marker file


@dataclass
class DeployRow:
    """Last-build snapshot for one service."""

    short: str
    name: str
    kind: str
    last_build_id: Optional[str] = None
    last_build_at: Optional[str] = None
    last_build_status: Optional[str] = None  # SUCCESS / FAILURE / WORKING / TIMEOUT / ...
    image_digest: Optional[str] = None
    log_url: Optional[str] = None
    prep_steps: list[PrepStatus] = field(default_factory=list)
    prep_done: int = 0
    prep_total: int = len(PREP_STEPS)


def _list_recent_builds(project: str, limit: int = 200) -> list[dict[str, Any]]:
    """Call ``gcloud builds list`` and return parsed JSON."""
    if not shutil.which("gcloud"):
        return []
    try:
        out = subprocess.run(
            [
                "gcloud", "builds", "list",
                "--project", project,
                "--limit", str(limit),
                "--format=json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(out.stdout or "[]")
    except subprocess.TimeoutExpired:
        logger.warning("gcloud builds list timed out after 60s")
        return []
    except subprocess.CalledProcessError as e:
        logger.warning("gcloud builds list failed: %s", e.stderr.strip()[:300])
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("gcloud builds list errored: %s", e)
        return []


def _service_for_build(build: dict[str, Any], known_short: set[str]) -> Optional[str]:
    """Best-effort: derive which service a build was for."""
    subs = (build.get("substitutions") or {})
    for key in ("_SERVICE", "_SERVICE_NAME", "_NAME"):
        v = subs.get(key)
        if v and v in known_short:
            return v
    for tag in (build.get("tags") or []):
        if tag in known_short:
            return tag
    # Image name fallback: gcr.io/PROJECT/ytfactory-tts-chatterbox:tag
    images = build.get("images") or []
    for img in images:
        for short in known_short:
            full = f"ytfactory-{short}" if not short.startswith("ytfactory-") else short
            if short in img or full in img:
                return short
    return None


def _read_prep(short: str) -> list[PrepStatus]:
    """Read ``cloud/<bare>/.deploy_prep/`` for the playbook markers.

    ``bare`` is the directory name under ``cloud/`` — same as ``short``
    for image-* / tts-* services, and the bare ``web-server`` etc for
    infra. We try both.
    """
    candidates = [
        CLOUD_DIR / short,
        CLOUD_DIR / f"image-{short}",
        CLOUD_DIR / f"tts-{short}",
    ]
    prep_dir: Optional[Path] = None
    for c in candidates:
        if (c / PREP_DIRNAME).is_dir():
            prep_dir = c / PREP_DIRNAME
            break
    rows: list[PrepStatus] = []
    for key, label in PREP_STEPS:
        if prep_dir is None:
            rows.append(PrepStatus(key=key, label=label, done=False))
            continue
        marker = prep_dir / f"{key}.ok"
        if marker.exists():
            rel = marker.relative_to(REPO_ROOT)
            rows.append(PrepStatus(key=key, label=label, done=True, artifact=str(rel)))
        else:
            rows.append(PrepStatus(key=key, label=label, done=False))
    return rows


def collect(
    *,
    project: str = "ytfactory-prod-v3",
    services: Optional[list[Service]] = None,
) -> list[DeployRow]:
    """Snapshot last build + prep status for every service we own."""
    targets = services if services is not None else list_services()
    short_index = {s.short: s for s in targets}
    builds = _list_recent_builds(project=project)

    # Take the most recent build per service.
    latest: dict[str, dict[str, Any]] = {}
    for b in builds:
        svc = _service_for_build(b, set(short_index.keys()))
        if not svc:
            continue
        # Builds come back newest-first — keep the first one we see per svc.
        latest.setdefault(svc, b)

    rows: list[DeployRow] = []
    for svc in targets:
        b = latest.get(svc.short)
        prep = _read_prep(svc.short)
        prep_done = sum(1 for p in prep if p.done)
        row = DeployRow(
            short=svc.short,
            name=svc.name,
            kind=svc.kind.value,
            prep_steps=prep,
            prep_done=prep_done,
            prep_total=len(PREP_STEPS),
        )
        if b:
            row.last_build_id = b.get("id")
            row.last_build_status = b.get("status")
            row.last_build_at = b.get("createTime") or b.get("startTime")
            row.log_url = b.get("logUrl")
            results = (b.get("results") or {}).get("images") or []
            if results and isinstance(results[0], dict):
                row.image_digest = results[0].get("digest")
        elif not shutil.which("gcloud"):
            row.last_build_status = "gcloud_unavailable"
        else:
            row.last_build_status = "no_recent_build"
        rows.append(row)
    rows.sort(key=lambda r: (r.kind, r.short))
    return rows


def collect_dict(**kwargs: Any) -> list[dict[str, Any]]:
    return [asdict(r) for r in collect(**kwargs)]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
