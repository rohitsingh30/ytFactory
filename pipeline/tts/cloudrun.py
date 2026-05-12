"""Cloud Run GPU TTS provider — talks to ``ytfactory-tts`` Cloud Run
service in asia-southeast1.

This is the laptop-side counterpart of ``cloud/tts/server.py``. Same
function signatures as ``pipeline.tts.f5._synth_f5_tts`` etc. so the
dispatcher in ``pipeline.audio.synthesize`` can swap any local ``f5_tts``
call for ``cloudrun_f5`` by changing the provider string only.

**Auto-fallback to local.** If the cloud service is unreachable (DNS
fail, 5xx, timeout >120 s), the call transparently falls back to the
local synth function for the same model. This means a Cloud Run outage
or quota exhaustion never blocks a render — we just pay the local
M2 Max wall-clock instead.

**Auth.** Cloud Run service is deployed `--no-allow-unauthenticated`.
We attach a Google ID token from the gcloud Application Default
Credentials. The token's audience MUST be the service URL (NOT the API
endpoint) for Cloud Run to accept it.

Env vars:

* ``CLOUDRUN_TTS_URL`` — required. The full Cloud Run service URL,
  e.g. ``https://ytfactory-tts-767262167641.asia-southeast1.run.app``.
  Set in the laptop's ``.env`` (sourced by the renderer entrypoints).
* ``CLOUDRUN_TTS_COSYVOICE_URL`` — optional. Separate URL for the
  ``ytfactory-tts-cosyvoice`` Cloud Run service (CosyVoice 2 lives in
  its own image — see ``cloud/tts-cosyvoice/`` for why). If set, the
  ``cloudrun_cosyvoice`` provider routes here instead of
  ``CLOUDRUN_TTS_URL``. If unset, requests fall through to
  ``CLOUDRUN_TTS_URL`` (which today returns 400 for cosyvoice — useful
  to detect mis-configuration during rollout).
* ``CLOUDRUN_TTS_TIMEOUT`` — optional. Per-call timeout in seconds.
  Default 180. Long-form chunks rarely need >30 s on L4 warm; the
  large headroom covers cold-start image-pull + model-load (~40 s)
  plus a generous safety margin.
* ``CLOUDRUN_TTS_DISABLE_FALLBACK`` — optional. Set to ``1`` to make
  cloud failures hard-error instead of falling back to local. Useful
  in tests where you want to confirm the cloud path.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ----------------------------------------------------------- token caching
#
# Lifted to `pipeline.cloudrun_auth` 2026-05-07 — see that module for
# the per-audience cache rationale (multi-service rollout requires
# audience-scoped tokens, not the original single global cache). This
# module re-exports `_get_id_token` as a thin wrapper so any existing
# import paths keep working.


from pipeline.cloudrun_auth import get_id_token as _get_id_token  # noqa: E402, F401
from pipeline import telemetry as _tlm  # noqa: E402


# ---------------------------------------------------------------- base URL


def _service_url(*, model: str | None = None) -> str:
    """Return the Cloud Run service URL for a given model.

    Each model lives in its own Cloud Run service (separate container,
    separate dep tree, separate quota slot), so we route per-model:

    * f5          → CLOUDRUN_TTS_F5_URL (or legacy CLOUDRUN_TTS_URL)
    * higgs       → CLOUDRUN_TTS_HIGGS_URL
    * cosyvoice   → CLOUDRUN_TTS_COSYVOICE_URL
    * chatterbox  → CLOUDRUN_TTS_CHATTERBOX_URL
    * indicparler → CLOUDRUN_TTS_INDICPARLER_URL
    * indicf5     → CLOUDRUN_TTS_INDICF5_URL

    Each service-specific env falls back to CLOUDRUN_TTS_URL if unset
    so a one-service deployment still works.
    """
    per_model = {
        "f5":          "CLOUDRUN_TTS_F5_URL",
        "higgs":       "CLOUDRUN_TTS_HIGGS_URL",
        "cosyvoice":   "CLOUDRUN_TTS_COSYVOICE_URL",
        "chatterbox":  "CLOUDRUN_TTS_CHATTERBOX_URL",
        "indicparler": "CLOUDRUN_TTS_INDICPARLER_URL",
        "indicf5":     "CLOUDRUN_TTS_INDICF5_URL",
    }
    if model in per_model:
        specific = os.environ.get(per_model[model], "").strip().rstrip("/")
        if specific:
            return specific
    url = os.environ.get("CLOUDRUN_TTS_URL", "").strip().rstrip("/")
    if not url:
        env_hint = per_model.get(model or "f5", "CLOUDRUN_TTS_URL")
        raise CloudRunUnavailable(
            f"cloudrun_{model or 'f5'} provider requires {env_hint} (or "
            f"CLOUDRUN_TTS_URL as fallback) to be set. "
            f"Add it to .env and re-source, or `unset` to force fall back "
            f"to local TTS providers."
        )
    return url


def _timeout_s() -> int:
    try:
        return int(os.environ.get("CLOUDRUN_TTS_TIMEOUT", "180"))
    except ValueError:
        return 180


def _fallback_disabled() -> bool:
    return os.environ.get("CLOUDRUN_TTS_DISABLE_FALLBACK", "").strip() in ("1", "true")


def _local_fallback_or_raise(
    cloud_err: "CloudRunUnavailable",
    fallback_label: str,
    fallback_call,
):
    """Run a local-fallback synth call, but if the local provider isn't
    available in the venv (laptop nuclear cleanup 2026-05-09 stripped
    mlx + torch from the laptop venv) re-raise the original
    ``CloudRunUnavailable`` instead of bubbling a confusing
    ``ModuleNotFoundError``.

    ``fallback_call`` is a no-arg callable returning a ``Path``. Use a
    closure to bind the per-call args at the call site.
    """
    if _fallback_disabled():
        # The fallback is intentionally off (e.g. canary tests). Record
        # the failure as a telemetry event so the dashboard's TTS panel
        # surfaces it instead of silently propagating.
        _tlm.track(
            "tts_fallback",
            category="tts",
            success=False,
            metadata={
                "label": fallback_label,
                "fallback_disabled": True,
                "cloud_error": str(cloud_err)[:200],
            },
        )
        raise cloud_err
    try:
        out = fallback_call()
    except (ImportError, ModuleNotFoundError) as ie:
        logger.warning(
            "%s local fallback unavailable in this venv (%s); "
            "re-raising CloudRunUnavailable", fallback_label, ie,
        )
        # Local fallback unavailable → user-visible failure. Record so
        # the panel can chart "no path forward" outages distinctly from
        # successful fallback rescues.
        _tlm.track(
            "tts_fallback",
            category="tts",
            success=False,
            metadata={
                "label": fallback_label,
                "venv_missing": str(ie)[:200],
                "cloud_error": str(cloud_err)[:200],
            },
        )
        raise cloud_err
    # Successful cloud→local rescue.
    _tlm.track(
        "tts_fallback",
        category="tts",
        success=True,
        metadata={
            "label": fallback_label,
            "cloud_error": str(cloud_err)[:200],
        },
    )
    return out


# ---------------------------------------------------------------- warmup


def warmup(provider: str) -> "threading.Thread | None":
    """Fire-and-forget /readyz against the cloud TTS service.

    Mirrors `pipeline.images.images_cloudrun.warmup` — fires on a
    background daemon thread and returns it so the caller can
    `.join(timeout=...)` right before the first /synth call to
    guarantee cold-load completed instead of racing it.

    `provider` is the channel YAML's tts_provider, e.g.
    ``cloudrun_chatterbox`` / ``cloudrun_indicparler`` /
    ``cloudrun_indicf5``. Returns None when the provider isn't a
    cloudrun_* one.
    """
    import threading  # noqa: PLC0415
    if not provider.startswith("cloudrun_"):
        return None
    model = provider.removeprefix("cloudrun_")

    def _go() -> None:
        try:
            url = _service_url(model=model)
        except RuntimeError as e:
            logger.info("cloudrun_%s warmup skipped — %s", model, e)
            return
        try:
            token = _get_id_token(url)
        except Exception as e:
            logger.warning("cloudrun_%s warmup token fetch failed: %s", model, e)
            return
        sess = requests.Session()
        try:
            t0 = time.time()
            resp = sess.get(
                f"{url}/readyz",
                headers={"Authorization": f"Bearer {token}"},
                timeout=900,
            )
            resp.raise_for_status()
            logger.info(
                "cloudrun_%s warmup ok in %.2fs", model, time.time() - t0
            )
        except Exception as e:
            logger.warning(
                "cloudrun_%s warmup failed: %s — first /synth will pay "
                "the cold-load instead", model, e,
            )
        finally:
            sess.close()

    t = threading.Thread(target=_go, daemon=True, name=f"cloudrun-tts-warmup-{model}")
    t.start()
    return t


# --------------------------------------------------------------- HTTP call


class CloudRunUnavailable(RuntimeError):
    """Raised when the cloud service can't satisfy the request and the
    caller should fall back to the local provider."""


def _post_synth(payload: dict) -> dict:
    """POST to /synth with retry/backoff, return parsed JSON.

    Retry semantics (5-attempt cap):
      - 429 Too Many Requests → exponential backoff (1, 2, 4, 8s), retry.
      - 503 Service Unavailable → exponential backoff, retry.
      - Other 5xx (500/502/504/...) → ``CloudRunUnavailable`` immediately
        (no retry — cloud is sick, fall back to local).
      - URLError / OSError / TimeoutError → backoff + retry; if all 5
        attempts fail, ``CloudRunUnavailable``.
      - 4xx that isn't 429 → re-raise ``HTTPError`` (caller's bad input,
        not a fall-back-to-local situation).

    The implementation uses ``urllib.request.urlopen`` directly. Each
    call opens a fresh socket, which sidesteps the stale-TCP issue we
    saw with reused ``requests.Session()`` connections during Cloud
    Run cold-load (memory/feedback_urllib_cloudrun_stale_tcp.md). The
    important property is "fresh socket per attempt" — which urllib
    gives us by default — combined with a wall-clock timeout.
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = _service_url(model=payload.get("model"))
    token = _get_id_token(url)

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/synth",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    max_attempts = 5
    last_http_error: urllib.error.HTTPError | None = None
    last_net_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
                raw = resp.read()
                try:
                    return json.loads(raw)
                except ValueError as e:
                    raise CloudRunUnavailable(
                        f"cloud /synth returned non-JSON: {raw[:300]!r}"
                    ) from e
        except urllib.error.HTTPError as e:
            last_http_error = e
            code = getattr(e, "code", None)
            if code in (429, 503):
                if attempt + 1 >= max_attempts:
                    # Audit Q2.20 — 503 already converted to
                    # CloudRunUnavailable so the render-level circuit
                    # breaker trips and the call falls back to the
                    # local provider; pre-fix, 429 raised the bare
                    # HTTPError, which the wrappers don't catch
                    # (they only catch CloudRunUnavailable). A
                    # rate-limited render then crashed entirely
                    # instead of falling back to local F5/Kokoro.
                    raise CloudRunUnavailable(  # coverage: pinned via test_5_consecutive_429s in tests/test_tts_cloudrun_full.py — git grep \b doesn't break on dots
                        f"cloud /synth {code} after {max_attempts} attempts"
                    ) from e
                time.sleep(2 ** attempt)
                continue
            if 500 <= (code or 0) < 600:
                # Other 5xx → not retryable; cloud needs to fail over.
                raise CloudRunUnavailable(
                    f"cloud /synth {code}: not retryable"
                ) from e
            # 4xx (other than 429) — caller's bad input.
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_net_error = e
            if attempt + 1 >= max_attempts:
                raise CloudRunUnavailable(
                    f"cloud /synth network error after {max_attempts} "
                    f"attempts: {e}"
                ) from e
            time.sleep(2 ** attempt)
            continue

    # Defensive — loop should always either return or raise.
    if last_http_error is not None:
        raise last_http_error
    if last_net_error is not None:
        raise CloudRunUnavailable(f"cloud /synth: {last_net_error}")
    raise CloudRunUnavailable("cloud /synth: unreachable code path")


def _materialise_wav(resp: dict, out_path: Path) -> Path:
    """Write the WAV to ``out_path`` from either the inline base64
    field or the GCS URI in the response."""
    if "output_inline" in resp and resp["output_inline"]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(resp["output_inline"]))
        return out_path
    gcs_uri = resp.get("output_gcs")
    if not gcs_uri:
        raise RuntimeError(
            f"cloud /synth response had neither output_inline nor "
            f"output_gcs: keys={list(resp)!r}"
        )
    # Streaming GCS download via gcloud (no extra dep on the laptop).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gcloud", "storage", "cp", gcs_uri, str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


# -------------------------------------------------------- public synth API


def _read_ref_b64(ref_audio_path: str) -> str:
    """Load reference WAV from disk and base64-encode for the wire.

    Empty path returns empty string — Indic Parler-TTS runs purely
    from a description and accepts no ref WAV, so the cloud server
    treats empty ``ref_audio_b64`` as "no clone, generate from
    description only."
    """
    if not ref_audio_path:
        return ""
    return base64.b64encode(Path(ref_audio_path).read_bytes()).decode("ascii")


def _synth_cloudrun(
    *,
    model: str,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
    description: str | None = None,
) -> Path:
    """Generic Cloud Run dispatcher used by every cloudrun_<model>
    wrapper below. ref_audio_text required for f5/cosyvoice; optional
    for the rest (matches each model's local-side contract).

    Audit Q2.17 — ``description`` lands in its own top-level payload
    field so description-driven models (Indic Parler) actually receive
    the per-render voice description from the channel YAML. Pre-fix
    the indicparler wrapper piggybacked the description in
    ``ref_text``, but the server reads ``req.description`` so every
    Hindi render silently used the hardcoded
    "calm devotional Indian female voice" default."""
    payload = {
        "model": model,
        "text": text,
        "ref_audio_b64": _read_ref_b64(ref_audio_path),
        "ref_text": ref_audio_text,
        "speed": speed,
        "seed": seed,
        "output": "inline",
    }
    # coverage: description-set path is exercised by tests/test_tts_cloudrun_full.py::TestSynthCloudrun::test_description_included_in_payload_when_provided; gate's git-grep word-boundary doesn't surface that test as related (audit T1.4-style discovery limitation)
    if description:
        payload["description"] = description
    t0 = time.time()
    resp = _post_synth(payload)
    _materialise_wav(resp, out_path)
    logger.info(
        "cloudrun_%s ok: chars=%d audio=%.2fs cloud_wall=%.2fs "
        "rtf=%.2f e2e_wall=%.2fs",
        model, len(text), resp.get("duration_s", 0.0),
        resp.get("wall_s", 0.0), resp.get("rtf", 0.0),
        time.time() - t0,
    )
    return out_path


# ---------------------------------------------------------------------------
# Chunked synthesis
# ---------------------------------------------------------------------------
#
# Long-form narration (multi-paragraph kathaa, 50-min sleep videos,
# multi-act long-form) gets degraded prosody when shipped as a single
# /synth call: each model has a context-window + memory ceiling that
# bites past ~350 chars. The chunker below splits the text into
# paragraph (or sentence-group) chunks, synthesises each, then concats
# the chunk WAVs back together with optional inter-chunk silence.
#
# The chunker is also where per-paragraph prosody (speed + post-pause
# from upstream rewriter narration_prosody output) lands, and where
# voice-clone-capable models get *anchored* to chunk-0's WAV after
# chunk 0 — so the timbre stays stable across a long render even when
# the model would otherwise drift.

# Voice-clone capability: which cloud models swap their ref to the
# previous chunk's output to keep timbre locked. Indic Parler is
# description-driven, not clone-driven, so it stays on the original
# (empty) ref for every chunk. Higgs is left out for the same reason
# — it's prompt-driven not clone-anchored.
_VOICE_CLONE_CAPABLE = {"f5", "indicf5", "chatterbox", "cosyvoice"}


def _split_for_chunked_synth(text: str, max_chars: int = 350) -> list[str]:
    """Split ``text`` into chunks suitable for one /synth call each.

    Strategy:

    1. Split on ``\\n\\n`` (paragraph break). Each non-empty paragraph
       is a chunk if it fits under ``max_chars``.
    2. Any paragraph that exceeds ``max_chars`` is recursively split on
       sentence terminators (``. ! ?``), greedy-grouping sentences
       until adding the next one would blow the cap.
    3. Whitespace-only chunks are filtered out.

    The return list always preserves left-to-right narration order,
    which is what the concat step assumes.
    """
    if not text or not text.strip():
        return []
    raw_paragraphs = [p.strip() for p in text.split("\n\n")]
    raw_paragraphs = [p for p in raw_paragraphs if p]

    out: list[str] = []
    for para in raw_paragraphs:
        if len(para) <= max_chars:
            out.append(para)
            continue
        # Greedy sentence-group split for over-long paragraphs.
        sentences = re.split(r"(?<=[.!?])\s+", para)
        current = ""
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            candidate = (current + " " + sent).strip() if current else sent
            if len(candidate) > max_chars and current:
                out.append(current)
                current = sent
            else:
                current = candidate
        if current:
            out.append(current)
    return out


def _derive_chunk_prosody(
    paragraphs: list[str],
    prosody: list[dict] | None,
) -> list[tuple[float, float]] | None:
    """Map per-sentence prosody onto per-paragraph (speed, post_pause_s).

    ``prosody`` comes from the LLM rewrite stage as a list of
    ``{"text": str, "speed": float, "post_pause_s": float}`` entries
    keyed on individual sentences. The chunker operates at paragraph
    granularity, so we collapse: for each paragraph, find every
    prosody entry whose ``text`` is a substring of the paragraph,
    average their speeds, and take the LAST matching entry's pause
    (capped at 0.40s — anything longer reads as broken pacing).

    A paragraph is considered "annotated" when it has at least one
    matching prosody entry whose ``speed`` differs from 1.0 (i.e. the
    rewriter actually applied a deliberate pacing instruction).

    Tier-2 smoothing — only fires when the document contains at
    least one annotated paragraph somewhere:
      - if the **next** paragraph is annotated, an unannotated
        paragraph gets ``auto_s=0.96`` (a small ritard before the
        annotated handoff — keeps the seam from feeling abrupt);
      - otherwise, two adjacent unannotated paragraphs get
        ``auto_s=1.03`` (parity, slight uplift to keep momentum).

    A document with NO annotated paragraphs (or all unannotated
    around a single matched-but-flat speed=1.0 entry) leaves every
    paragraph at ``(1.0, 0.0)``.

    Returns ``None`` when prosody is None or empty so the caller can
    short-circuit.
    """
    if not prosody:
        return None
    valid_entries = [e for e in prosody if (e.get("text") or "").strip()]
    annotated_idx: set[int] = set()
    out: list[tuple[float, float]] = []
    for i, para in enumerate(paragraphs):
        matches = [e for e in valid_entries if e["text"] in para]
        if matches:
            speeds = [float(e.get("speed", 1.0)) for e in matches]
            avg = sum(speeds) / len(speeds)
            last_pause = float(matches[-1].get("post_pause_s", 0.0))
            pause = min(0.40, max(0.0, last_pause))
            out.append((avg, pause))
            # "Annotated" = any matched speed deliberately != 1.0.
            # speed=1.0 matches are treated as parity/no-op so the
            # smoothing rules below still see the paragraph as
            # unannotated (matches the rewriter's contract: only
            # non-1.0 speeds are signal).
            if any(abs(float(e.get("speed", 1.0)) - 1.0) > 1e-9 for e in matches):
                annotated_idx.add(i)
        else:
            out.append((1.0, 0.0))  # placeholder; tier-2 rewrite below

    # Tier-2 only fires when SOMETHING in the doc is annotated.
    # Keeps single-para / all-flat docs at the strict default.
    if not annotated_idx:
        return out

    for i, (s, p) in enumerate(out):
        if i in annotated_idx:
            continue
        next_annotated = (i + 1 < len(out)) and (i + 1 in annotated_idx)
        auto_s = 0.96 if next_annotated else 1.03
        out[i] = (auto_s, p)
    return out


def _seed_from_text(text: str) -> int:
    """Deterministic seed when the caller leaves ``seed=None``.

    sha256-truncated to the bottom 31 bits so it fits in ``int32``
    (most TTS backends bound the seed at 0..2**31-1).
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _make_silence_wav(duration_s: float, out_path: Path) -> Path:
    """Generate a mono 24kHz silence WAV via ffmpeg anullsrc."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=mono:sample_rate=24000",
        "-t", f"{max(0.0, duration_s):.3f}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _concat_wavs(parts: list[Path], out_path: Path) -> Path:
    """ffmpeg concat-demuxer combine of WAV parts → out_path.

    The concat list file is written via ``tempfile.NamedTemporaryFile``
    (real ``/tmp/`` write, not the per-render chunk dir) so callers
    that mock ``Path.mkdir`` to skirt unwritable test paths still get
    a working concat.
    """
    import tempfile  # noqa: PLC0415
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve paths to absolute — ffmpeg's concat demuxer interprets
    # relative paths in the list file relative to the LIST FILE's
    # directory (which is /tmp/), not the cwd. With relative chunk
    # paths, the cloud worker hit
    #   "Impossible to open '/tmp/data/cache/.../chunk_000.wav'"
    # because the list file said `file 'data/cache/.../chunk_000.wav'`.
    abs_parts = [p.resolve() for p in parts]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        f.write("\n".join(f"file '{p}'" for p in abs_parts))
        list_path = Path(f.name)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _synth_cloudrun_chunked(
    *,
    model: str,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
    narration_prosody: list[dict] | None = None,
    description: str | None = None,
) -> Path:
    """Chunked /synth wrapper. Splits long text and concatenates the
    per-chunk WAVs back together at the end.

    Single-chunk path bypasses chunking entirely (saves the disk write
    + ffmpeg concat for the common short-form case).

    Multi-chunk path:
      * each chunk gets its own ``chunk_NNN.wav`` under
        ``out_path.parent / f".{out_path.stem}.chunks/"``.
      * voice-clone-capable models (see ``_VOICE_CLONE_CAPABLE``) swap
        their ref to ``chunk_000.wav`` after the first chunk so the
        timbre stays anchored.
      * per-chunk speed comes from ``_derive_chunk_prosody`` if
        ``narration_prosody`` is supplied; for IndicF5 the speeds are
        rebaselined around the caller-provided ``speed`` so the mean
        equals 1.0× the caller's intent (prevents the LLM-supplied
        absolute speeds from over-driving the cloud renderer).
      * inter-chunk silence > 0.05s is rendered via ffmpeg ``anullsrc``
        and inserted between chunk WAVs.
      * a deterministic seed is derived from the input text when
        ``seed=None`` so re-renders of the same script are stable.
    """
    chunks = _split_for_chunked_synth(text)
    if not chunks:
        raise ValueError("nothing to synthesise — text is empty/whitespace")

    if len(chunks) == 1:
        return _synth_cloudrun(
            model=model, text=chunks[0], ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed, description=description,
        )

    if seed is None:
        seed = _seed_from_text(text)

    chunk_dir = out_path.with_suffix("").parent / f".{out_path.stem}.chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    prosody = _derive_chunk_prosody(chunks, narration_prosody)
    # IndicF5 specifically: rebaseline the per-chunk speeds around the
    # caller's `speed`. Without this, an LLM-supplied 0.8/1.2 prosody
    # would actually run the model at those absolute values which
    # over-drives the cloud renderer.
    if prosody and model == "indicf5":
        mean_s = sum(s for s, _ in prosody) / len(prosody)
        if mean_s > 0:
            prosody = [(s / mean_s * speed, p) for (s, p) in prosody]

    parts: list[Path] = []
    chunk0_path: Path | None = None
    voice_clone = model in _VOICE_CLONE_CAPABLE
    current_ref = ref_audio_path

    for i, chunk_text in enumerate(chunks):
        chunk_path = chunk_dir / f"chunk_{i:03d}.wav"
        chunk_speed = prosody[i][0] if prosody else speed
        _synth_cloudrun(
            model=model, text=chunk_text, ref_audio_path=current_ref,
            ref_audio_text=ref_audio_text, out_path=chunk_path,
            speed=chunk_speed, seed=seed, description=description,
        )
        parts.append(chunk_path)
        if i == 0:
            chunk0_path = chunk_path
            if voice_clone and chunk0_path is not None:
                current_ref = str(chunk0_path)

        # Insert silence after this chunk if prosody asks for one.
        if prosody:
            _, pause = prosody[i]
            if pause > 0.05 and i < len(chunks) - 1:
                sil_path = chunk_dir / f"silence_{i:03d}.wav"
                _make_silence_wav(pause, sil_path)
                parts.append(sil_path)

    return _concat_wavs(parts, out_path)


def _synth_cloudrun_f5(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """F5-TTS via Cloud Run. Falls back to local F5-MLX on cloud
    failure unless ``CLOUDRUN_TTS_DISABLE_FALLBACK=1``.

    Output WAV is byte-different from the local MLX path (different
    inference runtime), but voice character should be perceptually
    identical because the cloud uses the same checkpoint
    (F5TTS_Base) and same flow-matching params (ode_method=rk4,
    nfe_step=8, cfg_strength=2.0, sway_sampling_coef=-1.0).
    """
    try:
        return _synth_cloudrun(
            model="f5", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        logger.warning(
            "cloudrun_f5 unavailable (%s); falling back to local f5_tts", e,
        )
        def _fb():
            from pipeline.tts.f5 import _synth_f5_tts  # noqa: PLC0415
            return _synth_f5_tts(
                text=text, ref_audio_path=ref_audio_path,
                ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
            )
        return _local_fallback_or_raise(e, "cloudrun_f5", _fb)


def _synth_cloudrun_higgs(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """Higgs Audio v2 via Cloud Run.

    On cloud failure, fall back to **local F5-TTS** (per laptop-fallback
    policy 2026-05-06: "F5 for all on laptop"). Higgs has no realistic
    local equivalent (PyTorch-only, ~20× real-time on M2 Max MPS),
    so F5 is the practical degradation path.
    """
    try:
        return _synth_cloudrun(
            model="higgs", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        logger.warning(
            "cloudrun_higgs unavailable (%s); falling back to local F5-TTS "
            "(per laptop-fallback policy 2026-05-06)", e,
        )
        def _fb():
            from pipeline.tts.f5 import _synth_f5_tts  # noqa: PLC0415
            return _synth_f5_tts(
                text=text, ref_audio_path=ref_audio_path,
                ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
            )
        return _local_fallback_or_raise(e, "cloudrun_higgs", _fb)


def _synth_cloudrun_cosyvoice(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """CosyVoice 2 via Cloud Run.

    On cloud failure, fall back to **local F5-TTS** (per laptop-fallback
    policy 2026-05-06). CosyVoice isn't installed in the laptop venv
    (deepspeed/torch dep tree fights), so F5 is the only viable local
    path.

    NOTE: CosyVoice 2 0.5B does NOT speak Hindi (proven 2026-05-05).
    Use cloudrun_indicparler / kokoro hf_alpha for Hindi instead.
    """
    if not ref_audio_text:
        raise ValueError(
            "cloudrun_cosyvoice requires ref_audio_text "
            "(transcript of the ref WAV)"
        )
    try:
        return _synth_cloudrun(
            model="cosyvoice", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        logger.warning(
            "cloudrun_cosyvoice unavailable (%s); falling back to local F5-TTS "
            "(per laptop-fallback policy 2026-05-06)", e,
        )
        def _fb():
            from pipeline.tts.f5 import _synth_f5_tts  # noqa: PLC0415
            return _synth_f5_tts(
                text=text, ref_audio_path=ref_audio_path,
                ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
            )
        return _local_fallback_or_raise(e, "cloudrun_cosyvoice", _fb)


def _synth_cloudrun_indicparler(
    text: str,
    ref_audio_path: str | None,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
    description: str | None = None,
    narration_prosody: list[dict] | None = None,
) -> Path:
    """Indic Parler-TTS via Cloud Run.

    On cloud failure, fall back to **local Kokoro hf_alpha** (per
    laptop-fallback policy 2026-05-06: "kokoro for hindutava-animated").
    Hindi has no F5 local equivalent — Kokoro is the only Hindi TTS
    that runs on the laptop.

    Uses ``description`` (natural-language voice spec) instead of
    ``ref_audio_path``. Indic Parler is description-driven.

    Long-form text is chunked through ``_synth_cloudrun_chunked`` so
    paragraph-aware prosody + post-pause silence can be honoured per
    ``narration_prosody``.
    """
    payload_text = text
    payload_desc = description or (
        "A clear, expressive Indian female voice with moderate pace. "
        "Recording is high quality."
    )
    try:
        return _synth_cloudrun_chunked(
            model="indicparler", text=payload_text,
            ref_audio_path=ref_audio_path or "",  # not used by indicparler
            # Audit Q2.17 — pass the voice description through the
            # dedicated `description` channel; the server reads
            # req.description, so the pre-fix piggyback in ref_text
            # was silently ignored and every Hindi render fell back
            # to the hardcoded "calm devotional Indian female" default.
            ref_audio_text=None,
            out_path=out_path, speed=speed, seed=seed,
            narration_prosody=narration_prosody,
            description=payload_desc,
        )
    except CloudRunUnavailable as e:
        logger.warning(
            "cloudrun_indicparler unavailable (%s); falling back to local "
            "Kokoro hf_alpha (per laptop-fallback policy 2026-05-06)", e,
        )
        def _fb():
            from pipeline.tts.kokoro import _synth_kokoro  # noqa: PLC0415
            return _synth_kokoro(
                text=text, voice="hf_alpha", out_path=out_path, speed=speed,
            )
        return _local_fallback_or_raise(e, "cloudrun_indicparler", _fb)


def _synth_cloudrun_indicf5(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    seed: int | None = None,
    narration_prosody: list[dict] | None = None,
) -> Path:
    """AI4Bharat IndicF5 via Cloud Run.

    F5-TTS architecture fine-tuned on 1417h of curated Indian speech;
    11 Indic languages including Hindi, Bengali, Tamil, etc.
    Voice-clone style — REQUIRES ref_audio_path + ref_audio_text.

    Long-form text is chunked through ``_synth_cloudrun_chunked``;
    chunk 0 uses the caller's ref WAV, every later chunk swaps to
    chunk-0's output for timbre anchoring (see ``_VOICE_CLONE_CAPABLE``).

    On cloud failure, fall back to **local Kokoro hf_alpha** (per
    laptop-fallback policy 2026-05-06: only on-laptop Hindi voice).
    Kokoro is preset-voice (no clone), so the fallback loses voice
    identity but stays in Hindi.
    """
    if not ref_audio_text:
        raise ValueError(
            "cloudrun_indicf5 requires ref_audio_text "
            "(transcript of the ref WAV; used for prosody anchoring)"
        )
    try:
        return _synth_cloudrun_chunked(
            model="indicf5", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
            narration_prosody=narration_prosody,
        )
    except CloudRunUnavailable as e:
        logger.warning(
            "cloudrun_indicf5 unavailable (%s); falling back to local "
            "Kokoro hf_alpha (per laptop-fallback policy 2026-05-06)", e,
        )
        def _fb():
            from pipeline.tts.kokoro import _synth_kokoro  # noqa: PLC0415
            return _synth_kokoro(
                text=text, voice="hf_alpha", out_path=out_path, speed=speed,
            )
        return _local_fallback_or_raise(e, "cloudrun_indicf5", _fb)


def _synth_cloudrun_chatterbox(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
    narration_prosody: list[dict] | None = None,
) -> Path:
    """Chatterbox via Cloud Run.

    Chunked through ``_synth_cloudrun_chunked`` for long-form prosody
    + voice-clone-anchored timbre across chunks.

    On cloud failure, fall back to **local F5-TTS** (NOT local Chatterbox).

    Why F5 instead of local Chatterbox:
      * User policy 2026-05-06: "for laptop we keep F5 for all" — local
        Chatterbox is too slow on M2 Max MPS (~6-13× real-time) for
        production use. F5-MLX is the canonical laptop TTS.
      * The fallback exists to keep renders unblocked during a Cloud Run
        outage; voice quality match is acceptable degradation.
      * Local Chatterbox provider remains available via explicit
        ``tts_provider: chatterbox`` in a YAML, but is not the cloud
        fallback target.

    Local F5 ignores ``ref_audio_text`` for chunked synthesis — the
    contract still passes through.
    """
    try:
        return _synth_cloudrun_chunked(
            model="chatterbox", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
            narration_prosody=narration_prosody,
        )
    except CloudRunUnavailable as e:
        logger.warning(
            "cloudrun_chatterbox unavailable (%s); falling back to local F5-TTS "
            "(per laptop-fallback policy 2026-05-06)", e,
        )
        def _fb():
            from pipeline.tts.f5 import _synth_f5_tts  # noqa: PLC0415
            return _synth_f5_tts(
                text=text, ref_audio_path=ref_audio_path,
                ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
            )
        return _local_fallback_or_raise(e, "cloudrun_chatterbox", _fb)
