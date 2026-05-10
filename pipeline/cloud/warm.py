"""Channel-aware pre-warm of cloud GPU containers.

Replaces the ``warm-cloud`` skill. Reads the channel YAML to figure out
which ``tts_provider`` and ``image_provider`` the next render will use,
then fires ``cloud/warm_tts_services.sh`` and ``cloud/warm_image_services.sh``
in parallel against the right targets.

Idempotent — hitting ``/readyz`` on an already-warm container returns
near-instantly.

This module is the canonical entry point used by:

* :mod:`pipeline.render.shorts` / :mod:`pipeline.render.long_form` /
  :mod:`pipeline.render.footage_only` / :mod:`pipeline.render.sports_doc`
  (called inline before stage 1, fire-and-forget on a thread)
* The daily snapshot cron (so nightly burn isn't all cold-load tax)
* The Cloud admin tab "Warm now" button
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
WARM_TTS = REPO_ROOT / "cloud" / "warm_tts_services.sh"
WARM_IMAGE = REPO_ROOT / "cloud" / "warm_image_services.sh"

# Map provider strings as they appear in channel YAMLs → warm script
# argument. Anything not in the table is silently skipped.
_TTS_PROVIDER_TO_TARGET: dict[str, str] = {
    "cloudrun_chatterbox": "chatterbox",
    "cloudrun_f5": "f5",
    "cloudrun_higgs": "higgs",
    "cloudrun_cosyvoice": "cosyvoice",
    "cloudrun_indicparler": "indicparler",
    "cloudrun_indicf5": "indicf5",
}
_IMAGE_PROVIDER_TO_TARGET: dict[str, str] = {
    "cloudrun_flux2_klein": "flux",
    "cloudrun_z_image_turbo": "zimage",
    "cloudrun_qwen_image": "qwen",
    "cloudrun_hidream": "hidream",
}


@dataclass
class WarmResult:
    target: str
    kind: str  # tts | image
    ok: bool
    duration_s: float
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class WarmReport:
    channel: Optional[str]
    fired: bool
    results: list[WarmResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    note: Optional[str] = None


def _read_yaml(path: Path) -> dict[str, Any]:
    """Tiny YAML loader — uses PyYAML if installed, falls back to a
    very dumb line scanner so warm.py stays import-cheap."""
    try:
        import yaml  # type: ignore  # noqa: PLC0415

        return yaml.safe_load(path.read_text()) or {}
    except ImportError:
        out: dict[str, Any] = {}
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            v = v.strip().strip('"').strip("'")
            if v:
                out[k.strip()] = v
        return out


def _resolve_channel_yaml(channel: str) -> Optional[Path]:
    """Accept either a slug (``mystoriesanimated``), a path to config.yaml,
    or a variant yaml under ``<channel>/variants/<v>.yaml``."""
    p = Path(channel)
    if p.is_file():
        return p
    # Try variant under repo first.
    if "/" in channel:
        candidate = REPO_ROOT / channel
        if candidate.is_file():
            return candidate
    # Channel slug → main config.
    candidate = REPO_ROOT / channel / "config.yaml"
    if candidate.is_file():
        return candidate
    return None


def _providers_for_channel(channel: Optional[str]) -> tuple[set[str], set[str]]:
    """Return (tts_targets, image_targets) for the warm scripts."""
    if not channel:
        return ({"chatterbox"}, {"flux"})  # default English Shorts pair
    yaml_path = _resolve_channel_yaml(channel)
    if not yaml_path:
        logger.info("warm: no yaml for channel=%s, using defaults", channel)
        return ({"chatterbox"}, {"flux"})

    cfg = _read_yaml(yaml_path)
    tts_targets: set[str] = set()
    image_targets: set[str] = set()

    tts_provider = str(cfg.get("tts_provider") or "").strip()
    image_provider = str(cfg.get("image_provider") or "").strip()
    if tts_provider in _TTS_PROVIDER_TO_TARGET:
        tts_targets.add(_TTS_PROVIDER_TO_TARGET[tts_provider])
    if image_provider in _IMAGE_PROVIDER_TO_TARGET:
        image_targets.add(_IMAGE_PROVIDER_TO_TARGET[image_provider])

    return (tts_targets, image_targets)


def _run_script(script: Path, args: Iterable[str], timeout_s: float) -> WarmResult:
    started = time.perf_counter()
    arglist = list(args)
    target = arglist[0] if arglist else "(none)"
    kind = "tts" if "tts" in script.name else "image"
    if not script.exists():
        return WarmResult(target=target, kind=kind, ok=False, duration_s=0.0,
                          stderr_tail=f"missing script {script}")
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script), *arglist],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=REPO_ROOT,
        )
        dur = time.perf_counter() - started
        return WarmResult(
            target=target,
            kind=kind,
            ok=proc.returncode == 0,
            duration_s=round(dur, 2),
            stdout_tail=(proc.stdout or "").strip()[-400:],
            stderr_tail=(proc.stderr or "").strip()[-400:],
        )
    except subprocess.TimeoutExpired:
        return WarmResult(target=target, kind=kind, ok=False,
                          duration_s=round(time.perf_counter() - started, 2),
                          stderr_tail=f"timeout after {timeout_s:.0f}s")


def warm_for_channel(
    channel: Optional[str] = None,
    *,
    timeout_s: float = 600.0,
    parallelism: int = 6,
) -> WarmReport:
    """Pre-warm the cloud services this channel's next render will use.

    Returns immediately with ``fired=False`` when no relevant
    ``CLOUDRUN_*_URL`` env is set so the renderer can call this
    unconditionally without taking on the cold-load tax of a probe.
    """
    report = WarmReport(channel=channel, fired=False)

    tts_targets, image_targets = _providers_for_channel(channel)

    # Skip TTS warm if no CLOUDRUN_TTS_*_URL is configured at all.
    has_any_tts_url = any(
        os.environ.get(f"CLOUDRUN_TTS_{t.upper()}_URL")
        for t in tts_targets
    )
    has_any_img_url = bool(
        (image_targets and "flux" in image_targets and os.environ.get("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"))
        or (image_targets and "zimage" in image_targets and os.environ.get("CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL"))
    )
    if not (has_any_tts_url or has_any_img_url):
        report.note = "no CLOUDRUN_*_URL configured; skipping (laptop fallback path)"
        return report

    jobs = []
    if tts_targets and has_any_tts_url:
        jobs.append((WARM_TTS, sorted(tts_targets)))
    else:
        report.skipped.extend(f"tts:{t}" for t in tts_targets)
    if image_targets and has_any_img_url:
        jobs.append((WARM_IMAGE, sorted(image_targets)))
    else:
        report.skipped.extend(f"image:{t}" for t in image_targets)

    if not jobs:
        return report

    report.fired = True
    with ThreadPoolExecutor(max_workers=min(parallelism, len(jobs))) as pool:
        futs = [pool.submit(_run_script, script, args, timeout_s) for script, args in jobs]
        for f in as_completed(futs):
            report.results.append(f.result())
    return report


def warm_async(channel: Optional[str] = None, **kwargs: Any) -> threading.Thread:
    """Fire :func:`warm_for_channel` on a background thread, return it.

    Use this from render entrypoints — the renderer keeps booting while
    the warm runs concurrently.
    """
    t = threading.Thread(
        target=warm_for_channel,
        kwargs={"channel": channel, **kwargs},
        name=f"cloud-warm-{channel or 'default'}",
        daemon=True,
    )
    t.start()
    return t


def to_dict(report: WarmReport) -> dict[str, Any]:
    return {
        "channel": report.channel,
        "fired": report.fired,
        "skipped": report.skipped,
        "note": report.note,
        "results": [asdict(r) for r in report.results],
    }
