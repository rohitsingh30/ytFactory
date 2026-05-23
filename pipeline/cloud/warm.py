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

from .. import observability as _obs

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
WARM_TTS = REPO_ROOT / "cloud" / "warm_tts_services.sh"
WARM_IMAGE = REPO_ROOT / "cloud" / "warm_image_services.sh"

# Map provider strings as they appear in channel YAMLs → warm script
# argument. Anything not in the table is silently skipped.
_TTS_PROVIDER_TO_TARGET: dict[str, str] = {
    "cloudrun_chatterbox": "chatterbox",
    "cloudrun_indicf5": "indicf5",
}
_IMAGE_PROVIDER_TO_TARGET: dict[str, str] = {
    "cloudrun_z_image_turbo": "zimage",
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
        candidate = REPO_ROOT / "data" / channel
        if candidate.is_file():
            return candidate
    # Channel slug → main config.
    candidate = REPO_ROOT / "data" / channel / "config.yaml"
    if candidate.is_file():
        return candidate
    return None


def _providers_for_channel(channel: Optional[str]) -> tuple[set[str], set[str]]:
    """Return (tts_targets, image_targets) for the warm scripts."""
    if not channel:
        return ({"chatterbox"}, {"zimage"})  # default English Shorts pair
    yaml_path = _resolve_channel_yaml(channel)
    if not yaml_path:
        logger.info("warm: no yaml for channel=%s, using defaults", channel)
        return ({"chatterbox"}, {"zimage"})

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


@_obs.traced("cloud.warm.warm_for_channel", category="cloud",
             capture=["channel", "timeout_s", "parallelism", "include_editing"])
def warm_for_channel(
    channel: Optional[str] = None,
    *,
    timeout_s: float = 600.0,
    parallelism: int = 6,
    include_editing: bool = False,
) -> WarmReport:
    """Pre-warm the cloud services this channel's next render will use.

    Returns immediately with ``fired=False`` when no relevant
    ``CLOUDRUN_*_URL`` env is set so the renderer can call this
    unconditionally without taking on the cold-load tax of a probe.

    Args:
        channel: Channel slug or path to channel.yaml. ``None`` uses
            the default English Shorts pair (chatterbox + zimage).
        timeout_s: Per-script timeout (each warm script is a /readyz
            probe with retries; usually completes in < 30s if warm,
            up to ~7 min if cold).
        parallelism: Max concurrent warm jobs in the thread pool.
        include_editing: Also probe ``ytfactory-editing-agent`` /readyz.
            Set to True from render entrypoints when the proposal opted
            into the optional 8th orchestrator stage
            (``proposal.editing.enabled``) so the editing-agent
            cold-load happens behind TTS+image rather than serially
            after compose.
    """
    report = WarmReport(channel=channel, fired=False)

    tts_targets, image_targets = _providers_for_channel(channel)

    # Skip TTS warm if no CLOUDRUN_TTS_*_URL is configured at all.
    has_any_tts_url = any(
        os.environ.get(f"CLOUDRUN_TTS_{t.upper()}_URL")
        for t in tts_targets
    )
    has_any_img_url = bool(
        image_targets and "zimage" in image_targets
        and os.environ.get("CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL")
    )
    has_editing_url = bool(
        include_editing and os.environ.get("CLOUDRUN_EDITING_AGENT_URL")
    )
    if not (has_any_tts_url or has_any_img_url or has_editing_url):
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

    if not jobs and not has_editing_url:
        return report

    report.fired = True

    # Editing-agent uses a Python probe (no warm.sh shell script — its
    # shape doesn't match the GPU-targeted warm_*_services.sh helpers).
    # We run it on the same thread pool so the timing budget stays
    # bounded by ``timeout_s``.
    futures = []
    with ThreadPoolExecutor(max_workers=min(parallelism, max(1, len(jobs) + (1 if has_editing_url else 0)))) as pool:
        for script, args in jobs:
            futures.append(pool.submit(_run_script, script, args, timeout_s))
        if has_editing_url:
            futures.append(pool.submit(_warm_editing_agent, timeout_s))
        for f in as_completed(futures):
            report.results.append(f.result())
    return report


def _warm_editing_agent(timeout_s: float) -> WarmResult:
    """Probe the editing-agent's /readyz directly via its laptop client.

    Kept inline (no separate warm script) because the editing-agent is
    a CPU service whose only "warm" step is a /readyz probe — there's
    no GPU model to load in advance. If editing-agent ever grows a
    cold-load step heavier than module import, port to a
    cloud/warm_editing_agent.sh script alongside the GPU ones."""
    started = time.perf_counter()
    try:
        from pipeline.editing.cloudrun import warmup  # noqa: PLC0415

        ok = warmup()
        return WarmResult(
            target="editing-agent",
            kind="video",
            ok=ok,
            duration_s=round(time.perf_counter() - started, 2),
            stdout_tail="readyz=200" if ok else "readyz!=200",
        )
    except Exception as e:  # noqa: BLE001
        return WarmResult(
            target="editing-agent",
            kind="video",
            ok=False,
            duration_s=round(time.perf_counter() - started, 2),
            stderr_tail=str(e)[:400],
        )


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


def warm_async_http(channel: Optional[str] = None) -> threading.Thread:
    """Fire-and-forget pure-HTTP warm for a channel's GPU services.

    Designed for the QUEUE-TIME warm path (POST /api/render →
    _enqueue_render_job → kick this off in background as soon as the
    job doc lands in Firestore). By the time the worker picks the
    job up + finishes the rewrite stage (~3-5 min), the TTS + image
    services are warm and the first /synth + /generate calls hit
    sub-second latency instead of the cold-start 30-90s + GPU-quota
    502 window.

    Differs from :func:`warm_async` in that it does NOT depend on the
    bundled shell scripts (``cloud/warm_*_services.sh``) — those
    scripts aren't shipped in the ytfactory-web Cloud Run container
    image. Instead it calls each provider's existing fire-and-forget
    HTTP warmup (``pipeline.tts.cloudrun.warmup`` /
    ``pipeline.images.images_cloudrun.warmup``), which only needs
    the CLOUDRUN_*_URL env vars + ID-token minting.

    Returns the daemon thread immediately so the caller doesn't block
    the HTTP response. The caller does NOT need to .join() —
    individual provider warmups self-log on success / failure and
    ignored failures are not fatal (the render's own warm-on-stage
    path remains as a fallback).
    """
    def _go() -> None:
        try:
            tts_targets, image_targets = _providers_for_channel(channel)
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm_async_http: provider resolve failed: %s", exc)
            return

        # TTS warmups
        try:
            from pipeline.tts.cloudrun import warmup as _tts_warmup  # noqa: PLC0415
            for target in tts_targets:
                # _TTS_PROVIDER_TO_TARGET maps "cloudrun_chatterbox" → "chatterbox";
                # tts.cloudrun.warmup expects the FULL provider string.
                provider = f"cloudrun_{target}"
                _tts_warmup(provider)  # itself returns a daemon thread, fire-and-forget
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm_async_http: TTS warm batch failed: %s", exc)

        # Image warmups
        try:
            from pipeline.images.images import warmup as _img_warmup  # noqa: PLC0415
            for target in image_targets:
                # _IMAGE_PROVIDER_TO_TARGET maps a provider key (e.g.
                # "cloudrun_z_image_turbo") to a short label (e.g. "zimage").
                # images.warmup expects the full provider key. Reverse-map.
                provider = next(
                    (k for k, v in _IMAGE_PROVIDER_TO_TARGET.items() if v == target),
                    None,
                )
                if provider:
                    _img_warmup(provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm_async_http: image warm batch failed: %s", exc)

        logger.info(
            "warm_async_http: kicked off warmups for channel=%s tts=%s image=%s",
            channel, sorted(tts_targets), sorted(image_targets),
        )

    t = threading.Thread(
        target=_go,
        name=f"cloud-warm-http-{channel or 'default'}",
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
