"""Cloud Run GPU image-gen provider — talks to the
``ytfactory-image-*`` Cloud Run services in asia-southeast1.

Counterpart to ``cloud/image-flux2-klein/server.py`` (and a future
``cloud/_bench/image-z-image-turbo/server.py``). Same function signature as
``pipeline.images.images._generate_z_image_turbo`` etc. so the dispatcher in
``pipeline.images.images.generate`` can swap any local provider for a
``cloudrun_<model>`` provider by changing the provider string only.

**Key divergence from the TTS client (`pipeline.tts.cloudrun`):
render-level circuit breaker.** Each Short renders ~30 images on the
critical path. If the cloud service is down and we per-image fall
back to local mflux, that's 30 × ~120 s timeout = 1 hour added to
the wall-clock for a single Short. Instead, the FIRST CloudRunUnavailable
in a render trips a module-level breaker → all subsequent calls in
the same process skip cloud entirely. Renderer entry points call
``reset_circuit_breaker()`` at the top of every render to clear the
flag.

**HTTP client: per-call ``requests.Session()`` (NOT a shared
``urllib`` socket pool).** Audit Q2.69 — the related docstring in
``pipeline/tts/cloudrun.py`` historically claimed urllib "sidesteps"
the stale-TCP bug while this file said the opposite. The actual
fact is that BOTH approaches avoid stale TCP by NOT reusing
connections — the bug only ever bit long-lived ``Session`` reuse.
The 2026-05-07 canary surfaced this when the Cloud Run LB severed
an idle TCP connection mid-request and the laptop's blocking read
didn't notice. Both files now use per-call sockets (urllib in tts,
fresh Session in images) for the same reason: the choice between
the two libs is about API ergonomics (streaming JSON / progress
events for images, simple POST→JSON for tts), NOT about TCP behaviour.

Env vars:

* ``CLOUDRUN_IMAGE_FLUX2_KLEIN_URL`` — the FLUX.2 [klein] 4B service
  URL (e.g. ``https://ytfactory-image-flux2-klein-...run.app``).
* ``CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL`` — the Z-Image-Turbo 6B service
  URL. Optional today (Z-Image cloud cold-load reliability still
  WIP, see memory/feedback_zimage_cloudrun_coldload_stall.md and
  P3.5 todo).
* ``CLOUDRUN_IMAGE_TIMEOUT`` — per-call HTTP timeout in seconds.
  Default 900 (15 min, generous because a cold instance pays
  ~5-7 min cold-load before responding to /generate).
* ``CLOUDRUN_IMAGE_DISABLE_FALLBACK`` — set to ``1`` to make cloud
  failures hard-error instead of falling back to local mflux. Use
  in canary to surface cloud bugs.
* ``CLOUDRUN_IMAGE_FALLBACK_MODE`` — ``once_per_render`` (default,
  trip the breaker on first failure) or ``per_image`` (legacy
  per-call fallback; only for debugging).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

from pipeline.cloud.cloudrun_auth import get_id_token
from pipeline import telemetry as _tlm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- base URL


def _service_url(model: str) -> str:
    """Return the Cloud Run service URL for `model`.

    Each image model lives in its own Cloud Run service (separate
    container, separate dep tree, separate quota slot), so we route
    per-model:

    * flux2_klein     → CLOUDRUN_IMAGE_FLUX2_KLEIN_URL
    * z_image_turbo   → CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL
    * (future) qwen_image, hidream
    """
    per_model = {
        "flux2_klein":   "CLOUDRUN_IMAGE_FLUX2_KLEIN_URL",
        "z_image_turbo": "CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL",
        # qwen_image's canonical env is the longer form which mirrors
        # the model name verbatim. The shorter ``CLOUDRUN_IMAGE_QWEN_URL``
        # is accepted as a fallback for compatibility with any old
        # .env still using it.
        "qwen_image":    "CLOUDRUN_IMAGE_QWEN_IMAGE_URL",
        "hidream":       "CLOUDRUN_IMAGE_HIDREAM_URL",
    }
    if model not in per_model:
        raise ValueError(
            f"unknown cloudrun image model: {model!r}. "
            f"Valid: {sorted(per_model)}"
        )
    env_var = per_model[model]
    url = os.environ.get(env_var, "").strip().rstrip("/")
    # Back-compat fallbacks: the older shorter env var names still work.
    if not url and model == "qwen_image":
        url = os.environ.get("CLOUDRUN_IMAGE_QWEN_URL", "").strip().rstrip("/")
    if not url:
        raise CloudRunUnavailable(
            f"cloudrun_{model} requires {env_var} to be set in .env. "
            f"Without it the cloud path is unavailable; the caller's "
            f"fallback path will activate."
        )
    return url


def _timeout_s() -> int:
    """Default 900s (15 min). The FLUX.2 klein cold-load measured
    7-8 min on Cloud Run L4 from GCS Fuse + an actual 4-step inference
    runs ~3 s, so 900s gives a comfortable 6-7 min margin even on cold
    instances. Render entry points should ALSO call
    `pipeline.images.images.warmup("cloudrun_flux2_klein")` early so the first
    real /generate hits a warm container — see canary 2026-05-07
    where the default 600s tripped exactly at the cold-load envelope."""
    try:
        return int(os.environ.get("CLOUDRUN_IMAGE_TIMEOUT", "900"))
    except ValueError:
        return 900


def _fallback_disabled() -> bool:
    return os.environ.get("CLOUDRUN_IMAGE_DISABLE_FALLBACK", "").strip() in ("1", "true")


def _fallback_mode() -> str:
    """`once_per_render` (default) or `per_image`."""
    mode = os.environ.get("CLOUDRUN_IMAGE_FALLBACK_MODE", "").strip().lower()
    if mode in ("once_per_render", "per_image"):
        return mode
    return "once_per_render"


# ----------------------------------------------------- circuit breaker

# `_CLOUD_DISABLED_THIS_RENDER` is set to True on the first
# CloudRunUnavailable we see in `once_per_render` mode. Renderer
# entry points call `reset_circuit_breaker()` at the top of every
# render to clear it, so it's per-render not per-process.
_BREAKER_LOCK = threading.Lock()
_CLOUD_DISABLED_THIS_RENDER: bool = False
_BREAKER_REASON: str | None = None


def reset_circuit_breaker() -> None:
    """Clear the per-render circuit breaker. Call at the top of
    every render entry point (make_shorts, render/long_form,
    render/footage_only, render/sports_doc) so a previous render's
    cloud failure doesn't carry over to the next one."""
    global _CLOUD_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if _CLOUD_DISABLED_THIS_RENDER:
            logger.info(
                "cloudrun image circuit breaker reset (was tripped: %s)",
                _BREAKER_REASON,
            )
        _CLOUD_DISABLED_THIS_RENDER = False
        _BREAKER_REASON = None


def _trip_breaker(reason: str) -> None:
    global _CLOUD_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if not _CLOUD_DISABLED_THIS_RENDER:
            logger.warning(
                "cloudrun image circuit breaker TRIPPED — falling back "
                "to local mflux for the rest of this render (reason: %s)",
                reason,
            )
            _CLOUD_DISABLED_THIS_RENDER = True
            _BREAKER_REASON = reason
            # Telemetry event so the dashboard can chart per-render
            # breaker trips alongside per-image latency. ``success=False``
            # reflects the loss-of-cloud event (the render itself may
            # still succeed via local fallback).
            _tlm.track(
                "image_circuit_breaker_tripped",
                category="image",
                success=False,
                metadata={"reason": str(reason)[:300]},
            )


def _breaker_open() -> bool:
    with _BREAKER_LOCK:
        return _CLOUD_DISABLED_THIS_RENDER


# --------------------------------------------------------------- HTTP call


class CloudRunUnavailable(RuntimeError):
    """Raised when the cloud service can't satisfy the request and
    the caller should fall back to the local provider (or trip the
    breaker, if `once_per_render` mode)."""


def _post_generate(url: str, payload: dict) -> dict:
    """POST to /generate, return parsed JSON. Raises
    CloudRunUnavailable on connection error / timeout / 5xx so the
    caller can fall back.

    Retries ONCE on any connection error (ConnectionError /
    ChunkedEncodingError / ReadTimeout) — the most common failure
    mode is a stale TCP connection severed by Cloud Run's LB during
    a long cold-load wait, and a fresh connection then succeeds.
    Each call uses a fresh `requests.Session` so we never re-use a
    pooled connection across stage boundaries.
    """
    token = get_id_token(url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = _timeout_s()
    last_err: Exception | None = None
    # 5 attempts with exponential backoff for 503/429 (Cloud Run "Rate
    # exceeded" — happens when the renderer fires N parallel image-gen
    # requests against a max-instances=3 service — bumped from 2 on
    # 2026-05-11 alongside the per-render fan-out in
    # ``pipeline.render.shorts._render_one_beat``; see
    # ``docs/parallel_per_beat_fanout.md`` for the dispatcher recipe).
    # The whole retry window is bounded at ~30s so we don't masquerade
    # a real outage as a long timeout.
    backoff_s = [1, 2, 4, 8]
    for attempt in (1, 2, 3, 4, 5):
        sess = requests.Session()
        try:
            resp = sess.post(
                f"{url}/generate", data=json.dumps(payload),
                headers=headers, timeout=timeout,
            )
            if resp.status_code in (429, 503) and attempt <= 4:
                wait_s = backoff_s[attempt - 1]
                logger.warning(
                    "cloudrun /generate %d (Rate exceeded) attempt %d/5; "
                    "sleeping %ds before retry",
                    resp.status_code, attempt, wait_s,
                )
                import time as _time  # noqa: PLC0415
                _time.sleep(wait_s)
                continue
            if 500 <= resp.status_code < 600:
                raise CloudRunUnavailable(
                    f"cloud /generate {resp.status_code}: "
                    f"{resp.text[:300]!r}"
                )
            # 401 = ID token expired mid-render. Long renders
            # (image_steps=8 × 30 images = ~5 min just on this
            # service) push past gcloud token TTL. Clear the per-
            # audience cache and retry once with a freshly-minted
            # token. Discovered 2026-05-07 mid-flight crash on dentist
            # v3 with image_steps=8.
            if resp.status_code == 401 and attempt <= 4:
                logger.warning(
                    "cloudrun /generate 401 unauthorized — refreshing "
                    "ID token and retrying"
                )
                from pipeline.cloud.cloudrun_auth import _TOKENS
                _TOKENS.pop(url, None)
                token = get_id_token(url)
                headers["Authorization"] = f"Bearer {token}"
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            # 4xx (caller bug — bad input) → re-raise without fallback.
            raise
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
        ) as e:
            last_err = e
            logger.warning(
                "cloudrun /generate attempt %d/5 failed (%s); %s",
                attempt, type(e).__name__,
                "retrying with fresh connection" if attempt <= 4
                else "giving up → CloudRunUnavailable",
            )
        except CloudRunUnavailable:
            raise  # already wrapped, no retry
        finally:
            sess.close()
    raise CloudRunUnavailable(
        f"cloud /generate network error after 5 attempts: {last_err}"
    ) from last_err


def _materialise_png(resp: dict, out_path: Path) -> Path:
    """Write the PNG to `out_path` from either the inline base64
    field or the GCS URI in the response."""
    if "output_inline" in resp and resp["output_inline"]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(resp["output_inline"]))
        return out_path
    gcs_uri = resp.get("output_gcs")
    if not gcs_uri:
        raise RuntimeError(
            f"cloud /generate response had neither output_inline nor "
            f"output_gcs: keys={list(resp)!r}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gcloud", "storage", "cp", gcs_uri, str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


# -------------------------------------------------------- public generate API


def _generate_cloudrun(
    *,
    model: str,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Generic Cloud Run dispatcher used by every cloudrun_<model>
    wrapper below. Same signature as the local `_generate_z_image_turbo`
    / `_generate_mflux` so the orchestrator can pass through unchanged."""
    url = _service_url(model)
    # Append the anti-text suffix to suppress diffusion's natural
    # tendency to render gibberish text inside panels — see
    # ``ANTI_TEXT_SUFFIX`` for the full rationale and per-distilled-model
    # justification (FLUX.2 klein is guidance-distilled, so a true
    # negative_prompt has no effect; in-prompt negation does).
    payload = {
        "prompt": _append_anti_text_suffix(prompt, model=model),
        "width": width,
        "height": height,
        "steps": steps,
        "seed": seed,
        "output": "inline",
    }
    t0 = time.time()
    resp = _post_generate(url, payload)
    _materialise_png(resp, out_path)
    logger.info(
        "cloudrun_%s ok: prompt_len=%d %sx%s steps=%d "
        "cloud_wall=%.2fs e2e_wall=%.2fs%s",
        model, len(prompt), resp.get("width", "?"), resp.get("height", "?"),
        resp.get("steps", steps),
        resp.get("wall_s", 0.0), time.time() - t0,
        " (cold)" if resp.get("cold_loaded") else "",
    )
    return out_path


# ---------------------------------------------------------------- anti-text
#
# Diffusion image models LOVE to render text. Even without prompted to,
# they'll splatter gibberish letters across jerseys, signs, scoreboards,
# and panel margins. The 2026-05-13 audit of 27 rendered Shorts found:
#
#   - Cake-AITA panels: top-text "Flate 2w HOOuR / to Fro TVEIRINE",
#     "frgoggst i ate it!", "BIG -to Momene", "LUQAR APE", "PIatenn
#     Predium!" — pure AI-hallucinated nonsense baked into the panel.
#   - Baghdad-Mongols panels: "VIGEE TI. TWO OIL MONGK BREEAI MILURAK
#     HISROGLY", "RSOLD MONGCOLAN ENTRH BAGOAI TINVALE HISTORY."
#   - Ronaldinho jerseys: fake "FCO" Barcelona crest, "CHCASYQUEB"
#     across the chest.
#
# FLUX.2 klein is GUIDANCE-DISTILLED, so the standard
# ``negative_prompt`` parameter is a no-op (the server explicitly
# documents this — see cloud/image-flux2-klein/server.py:173-177).
# But distilled models DO parse in-prompt instructions, so we append
# an anti-text suffix to every positive prompt. Other cloud providers
# (z_image_turbo, qwen_image, hidream) accept the same suffix without
# harm even when their server-side ``negative_prompt`` does work.
#
# Per-channel override: a channel YAML can set
# ``image.anti_text_suffix: ""`` to disable (e.g. for a "screenshot
# of a tweet" channel where text IS the content).

ANTI_TEXT_SUFFIX = (
    "(no readable text in image, no signs, no captions, no banners, "
    "no inscribed words, no jersey lettering, no sponsor logos, "
    "no street signs, no readable book covers, no name tags, "
    "plain backgrounds, no watermark)"
)


def _append_anti_text_suffix(prompt: str, *, model: str) -> str:
    """Append the anti-text suffix to a prompt unless already present.

    Idempotent: if the prompt already contains the suffix substring,
    returns unchanged (so callers that pre-attach it for testing or
    customization don't get a double-suffix).

    Per-model carve-outs:

    - ``flux2_klein`` / ``z_image_turbo`` / ``qwen_image`` / ``hidream``:
      append the suffix in parentheses at the end. Distilled models
      respect parenthesized in-prompt negation; non-distilled models
      treat it as a soft hint that aligns with their own negative-prompt
      behaviour.
    - Any future model: same suffix unless an opt-out is added here.

    The model parameter is currently unused for branching but kept in
    the signature so per-model overrides can land without changing
    callers (e.g. an "image_legible" model variant might want to
    suppress the suffix entirely).
    """
    if not prompt:
        return prompt
    if "no readable text in image" in prompt.lower():
        return prompt
    return f"{prompt.rstrip(' .,;')}. {ANTI_TEXT_SUFFIX}"


# ------------------------------------------------------- per-model wrappers


def _generate_cloudrun_flux2_klein(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """FLUX.2 [klein] 4B via Cloud Run.

    Local fallback removed 2026-05-09 (laptop nuclear cleanup). On
    cloud failure this raises ``CloudRunUnavailable`` and the renderer
    must surface the failure (the render-level circuit breaker also
    no longer trips since there's no second path to switch to).
    """
    return _generate_cloudrun(
        model="flux2_klein", prompt=prompt, seed=seed,
        out_path=out_path, width=width, height=height,
        steps=_clamp_steps(steps, lo=2, hi=8, default=4),
    )


def _generate_cloudrun_z_image_turbo(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Z-Image-Turbo 6B via Cloud Run.

    Local fallback removed 2026-05-09 (laptop nuclear cleanup).
    """
    return _generate_cloudrun(
        model="z_image_turbo", prompt=prompt, seed=seed,
        out_path=out_path, width=width, height=height,
        steps=_clamp_steps(steps, lo=4, hi=12, default=9),
    )


def _generate_cloudrun_qwen_image(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Qwen VL image model via Cloud Run (future / bench lane)."""
    return _generate_cloudrun(
        model="qwen_image", prompt=prompt, seed=seed,
        out_path=out_path, width=width, height=height,
        steps=_clamp_steps(steps, lo=1, hi=50, default=20),
    )


def _generate_cloudrun_hidream(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """HiDream image model via Cloud Run (future / bench lane)."""
    return _generate_cloudrun(
        model="hidream", prompt=prompt, seed=seed,
        out_path=out_path, width=width, height=height,
        steps=_clamp_steps(steps, lo=1, hi=50, default=25),
    )


def _clamp_steps(steps: int, *, lo: int, hi: int, default: int) -> int:
    """Clamp step count to the model's valid range; fall back to
    `default` if outside. Keeps the wire payload safe even if the
    caller passes weird numbers (e.g. legacy 4-step value when we
    deploy a model that needs 8+ steps)."""
    if steps < lo or steps > hi:
        logger.info(
            "cloudrun image: clamping steps=%d into [%d, %d] → using %d",
            steps, lo, hi, default,
        )
        return default
    return steps


def _local_fallback(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Stub — local fallback removed 2026-05-09 (laptop nuclear cleanup).

    Kept as a function so any code still calling it (e.g. an azure→
    cloudrun→local cascade in the deleted-but-still-imported
    pipeline.images.images_azure path) gets a clear hard error instead of
    importing a now-missing local image module.
    """
    raise CloudRunUnavailable(
        "local image fallback was removed 2026-05-09 (laptop nuclear "
        "cleanup); cloud is the only image-gen path. Set "
        "CLOUDRUN_IMAGE_FLUX2_KLEIN_URL to a healthy service or wait "
        "for the cloud outage to clear."
    )


# ----------------------------------------------------------------- warmup


def _readyz(model: str) -> dict:
    """GET /readyz on a service. Used to pay the cold-load tax
    BEFORE a render starts. Returns the server's JSON response.

    Uses requests (not urllib) so a Cloud Run LB severance during
    the long cold-load doesn't leave us hanging on a dead socket
    (see _post_generate docstring for the urllib post-mortem)."""
    url = _service_url(model)
    token = get_id_token(url)
    sess = requests.Session()
    try:
        resp = sess.get(
            f"{url}/readyz",
            headers={"Authorization": f"Bearer {token}"},
            timeout=900,  # cold-load can be 5-7 min on FLUX, longer on Z-Image
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        sess.close()


def warmup(model: str) -> threading.Thread:
    """Fire-and-forget background-thread warmup of `model`. Returns
    the thread so the caller can `.join(timeout=...)` if it wants
    to. Failures are logged but don't propagate — warmup is best-
    effort; the real /generate call surfaces any actual problem."""
    def _go() -> None:
        try:
            t0 = time.time()
            r = _readyz(model)
            logger.info(
                "cloudrun_%s warmup ok: cold=%s warm_s=%s "
                "boot_uptime_s=%s e2e_wall=%.2fs",
                model, r.get("cold_loaded"), r.get("warm_s"),
                r.get("boot_uptime_s"), time.time() - t0,
            )
        except CloudRunUnavailable as e:
            logger.info(
                "cloudrun_%s warmup skipped — service URL unset (%s)",
                model, e,
            )
        except Exception as e:
            logger.warning(
                "cloudrun_%s warmup failed: %s — actual /generate "
                "call will surface the real error or fall back",
                model, e,
            )

    t = threading.Thread(target=_go, daemon=True, name=f"cloudrun-warmup-{model}")
    t.start()
    return t

