"""Cloud Run yt-dlp client — drop-in replacement for laptop yt-dlp.

The cloud worker (`cloud/clone-video-worker/`) exposes a `/download`
endpoint that runs yt-dlp inside a container with the full bot-evasion
stack (bgutil PO-token provider, yt-jsc-youtubei JS challenge solver,
incognito-frozen cookies in Secret Manager). Every laptop call site
that previously shelled out to `yt-dlp` should now go through here so
the laptop never has to fight YouTube's bot detection again.

Two compatibility surfaces:

  1. ``download(url, output_path, ...)`` — the canonical helper. Posts
     to /download, streams the file to disk, returns the final Path.
  2. ``YoutubeDLCloud(opts).download([urls])`` — a thin facade matching
     the subset of `yt_dlp.YoutubeDL` semantics the codebase actually
     uses (`outtmpl`, `format`, `quiet`, `extract_audio`,
     `download_sections`). For ``pipeline/voice_clone.py`` which
     imports the Python API directly.

Both honour ``CLOUDRUN_YT_DLP_DISABLE_FALLBACK=1`` for canary tests
where you want a hard error instead of falling back to local yt-dlp.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# 5 minutes is plenty for the kind of clips ytFactory pulls (most are
# <2 min sports highlights / 5-15 s voice samples). Long-form movies
# are out of scope for this client.
_DEFAULT_TIMEOUT_S = 300


class CloudRunYtDlpUnavailable(RuntimeError):
    """Raised when the Cloud Run service is unreachable / unhealthy.

    Callers can catch this and fall back to local yt-dlp, OR re-raise
    if they want hard-cloud-only behaviour.
    """


class CloudRunYtDlpFailed(RuntimeError):
    """Raised when the Cloud Run service returns a 4xx error (the URL
    is genuinely undownloadable — bot challenge, deleted video, region
    block, etc). Callers should NOT auto-retry against local yt-dlp
    because the laptop is unlikely to do better and just costs latency.
    """


# ---------------------------------------------------------------------------
# Auth + URL resolution
# ---------------------------------------------------------------------------


def _service_url() -> str:
    url = os.environ.get("CLOUDRUN_YT_DLP_URL", "").strip()
    if url:
        return url.rstrip("/")
    # Reuse the clone-video-worker URL — same container, same /download
    # endpoint. Lets callers set just one env var.
    url = os.environ.get("CLOUDRUN_CLONE_VIDEO_URL", "").strip()
    if url:
        return url.rstrip("/")
    raise CloudRunYtDlpUnavailable(
        "CLOUDRUN_YT_DLP_URL (or CLOUDRUN_CLONE_VIDEO_URL) not set"
    )


def _id_token(audience: str) -> Optional[str]:
    try:
        from pipeline.cloud.cloudrun_auth import get_id_token  # noqa: PLC0415

        return get_id_token(audience)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloudrun yt-dlp: could not mint ID token: %s", exc)
        return None



def _inject_trace_headers(headers: dict[str, str]) -> None:
    try:
        from pipeline.observability.propagation import inject_into_dict  # noqa: PLC0415

        carrier: dict[str, str] = {}
        inject_into_dict(carrier)
        for key in ("traceparent", "tracestate"):
            if carrier.get(key):
                headers[key] = carrier[key]
    except Exception:  # noqa: BLE001
        pass



def _track_http_call(
    *,
    method: str,
    url: str,
    status_code: int | None,
    response_bytes: int,
    duration_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    try:
        from pipeline import observability as obs  # noqa: PLC0415

        meta: dict[str, Any] = {
            "service": "yt_dlp_cloudrun",
            "method": method,
            "url": url,
            "status_code": status_code,
            "response_bytes": response_bytes,
        }
        if error:
            meta["error"] = error[:500]
        obs.track(
            "http.call",
            category="http",
            success=success,
            duration_ms=duration_ms,
            metadata=meta,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def download(
    url: str,
    output_path: Path | str,
    *,
    format_string: Optional[str] = None,
    audio_only: bool = False,
    audio_ext: str = "m4a",
    sections: Optional[list[str]] = None,
    extra_args: Optional[list[str]] = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    fallback_to_local: bool = True,
) -> Path:
    """Download a single URL via Cloud Run yt-dlp, write to ``output_path``.

    ``output_path`` may include a yt-dlp-style ``%(ext)s`` placeholder
    (e.g. ``/tmp/clip.%(ext)s``) — when present the actual extension is
    inferred from the cloud response and substituted before the write.

    Returns the final on-disk Path.
    """
    import requests  # noqa: PLC0415 — lazy

    out_path = Path(output_path)
    base_url = None
    try:
        base_url = _service_url()
    except CloudRunYtDlpUnavailable as exc:
        if fallback_to_local and _local_fallback_enabled():
            logger.info("cloudrun yt-dlp: %s → falling back to local", exc)
            return _local_fallback_download(
                url, out_path,
                format_string=format_string,
                audio_only=audio_only, audio_ext=audio_ext,
                sections=sections, extra_args=extra_args,
                timeout_s=timeout_s,
            )
        raise

    body: dict[str, Any] = {"url": url, "timeout_s": timeout_s}
    if format_string is not None:
        body["format"] = format_string
    if audio_only:
        body["audio_only"] = True
        body["audio_ext"] = audio_ext
    if sections:
        body["sections"] = sections
    if extra_args:
        body["extra_args"] = extra_args

    headers = {"Content-Type": "application/json"}
    token = _id_token(base_url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _inject_trace_headers(headers)

    request_url = f"{base_url}/download"
    request_t0 = time.time()
    try:
        resp = requests.post(
            request_url,
            json=body,
            headers=headers,
            timeout=timeout_s + 30,
            stream=True,
        )
    except requests.RequestException as exc:
        _track_http_call(
            method="POST",
            url=request_url,
            status_code=None,
            response_bytes=0,
            duration_ms=int((time.time() - request_t0) * 1000),
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        if fallback_to_local and _local_fallback_enabled():
            logger.warning("cloudrun yt-dlp: HTTP failed (%s) → falling back to local", exc)
            return _local_fallback_download(
                url, out_path,
                format_string=format_string,
                audio_only=audio_only, audio_ext=audio_ext,
                sections=sections, extra_args=extra_args,
                timeout_s=timeout_s,
            )
        raise CloudRunYtDlpUnavailable(f"cloud yt-dlp HTTP failed: {exc}") from exc

    if resp.status_code == 400:
        _track_http_call(
            method="POST",
            url=request_url,
            status_code=resp.status_code,
            response_bytes=len(resp.content or b""),
            duration_ms=int((time.time() - request_t0) * 1000),
            success=False,
            error=resp.text[:300],
        )
        # Genuine yt-dlp failure — don't fall back to laptop, the URL
        # itself is the problem.
        raise CloudRunYtDlpFailed(
            f"cloud yt-dlp 400: {resp.text[:300]}"
        )
    if resp.status_code == 504:
        _track_http_call(
            method="POST",
            url=request_url,
            status_code=resp.status_code,
            response_bytes=len(resp.content or b""),
            duration_ms=int((time.time() - request_t0) * 1000),
            success=False,
            error=resp.text[:300],
        )
        # Audit D3.59 — pre-fix this raised CloudRunYtDlpFailed (no
        # local retry), but a 504 is a SERVICE-side timeout: the same
        # URL might succeed on the laptop's faster network or under
        # the laptop's longer timeout budget. Reclassify as
        # CloudRunYtDlpUnavailable so the fallback path is taken.
        if fallback_to_local and _local_fallback_enabled():
            logger.warning(
                "cloudrun yt-dlp: HTTP 504 timeout (%s) → falling back to local",
                resp.text[:200],
            )
            return _local_fallback_download(
                url, out_path,
                format_string=format_string,
                audio_only=audio_only, audio_ext=audio_ext,
                sections=sections, extra_args=extra_args,
                timeout_s=timeout_s,
            )
        raise CloudRunYtDlpUnavailable(
            f"cloud yt-dlp timeout: {resp.text[:200]}"
        )
    if resp.status_code != 200:
        _track_http_call(
            method="POST",
            url=request_url,
            status_code=resp.status_code,
            response_bytes=len(resp.content or b""),
            duration_ms=int((time.time() - request_t0) * 1000),
            success=False,
            error=resp.text[:300],
        )
        # 401/403/5xx → service issue; fall back to laptop.
        if fallback_to_local and _local_fallback_enabled():
            logger.warning(
                "cloudrun yt-dlp: HTTP %d (%s) → falling back to local",
                resp.status_code, resp.text[:200],
            )
            return _local_fallback_download(
                url, out_path,
                format_string=format_string,
                audio_only=audio_only, audio_ext=audio_ext,
                sections=sections, extra_args=extra_args,
                timeout_s=timeout_s,
            )
        raise CloudRunYtDlpUnavailable(
            f"cloud yt-dlp HTTP {resp.status_code}: {resp.text[:300]}"
        )

    # Stream to disk. If output_path has %(ext)s, replace with the
    # actual extension from the response filename.
    cloud_filename = resp.headers.get("X-Ytdlp-Filename", "out.bin")
    cloud_ext = Path(cloud_filename).suffix.lstrip(".") or "bin"

    out_str = str(out_path)
    if "%(ext)s" in out_str:
        out_str = out_str.replace("%(ext)s", cloud_ext)
        out_path = Path(out_str)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    # Audit Q2.32 — per-chunk read timeout. Pre-fix
    # ``resp.iter_content(...)`` had NO read deadline. A slow-trickle
    # server (or a stalled connection mid-body) could keep us reading
    # one byte at a time for the full timeout_s + 30 envelope. Now we
    # bound per-chunk wait to ``YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S``
    # (default 60 s) by tracking elapsed time between chunks; on
    # stall, raise CloudRunYtDlpFailed so the wrapper falls back to
    # local yt-dlp (or surfaces to the operator).
    chunk_timeout_s = float(
        os.environ.get("YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S", "60")
    )
    try:
        with out_path.open("wb") as fh:
            last_chunk_at = time.time()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                now = time.time()
                if (now - last_chunk_at) > chunk_timeout_s:
                    out_path.unlink(missing_ok=True)
                    raise CloudRunYtDlpFailed(
                        f"cloud yt-dlp body stalled: no chunk in "
                        f"{chunk_timeout_s:.0f}s ({bytes_written} bytes received)"
                    )
                if chunk:
                    fh.write(chunk)
                    bytes_written += len(chunk)
                    last_chunk_at = now
    except Exception as exc:  # noqa: BLE001
        _track_http_call(
            method="POST",
            url=request_url,
            status_code=resp.status_code,
            response_bytes=bytes_written,
            duration_ms=int((time.time() - request_t0) * 1000),
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    _track_http_call(
        method="POST",
        url=request_url,
        status_code=resp.status_code,
        response_bytes=bytes_written,
        duration_ms=int((time.time() - request_t0) * 1000),
        success=bytes_written > 0,
        error="0-byte body" if bytes_written == 0 else None,
    )
    if bytes_written == 0:
        out_path.unlink(missing_ok=True)
        raise CloudRunYtDlpFailed("cloud yt-dlp returned 0-byte body")

    logger.info(
        "cloudrun yt-dlp: %s → %s (%d bytes)", url, out_path.name, bytes_written
    )
    return out_path


# ---------------------------------------------------------------------------
# YoutubeDL-shaped facade — for callers that import yt_dlp directly
# ---------------------------------------------------------------------------


class YoutubeDLCloud:
    """Subset of ``yt_dlp.YoutubeDL``'s API that ytFactory actually
    uses, routed through the cloud helper.

    Supported opts (everything else is ignored with a warning):
      * ``outtmpl`` — string, may contain ``%(ext)s``
      * ``format`` — yt-dlp format selector
      * ``quiet`` — suppress logger.info (default False)
      * ``extractaudio`` / ``postprocessors`` — if a FFmpegExtractAudio
        post-processor is present (the voice_clone.py pattern), we
        flip ``audio_only=True`` and pull ``preferredcodec``.
      * ``download_sections`` — list of sections strings
    """

    def __init__(self, opts: Optional[dict[str, Any]] = None) -> None:
        self.opts = opts or {}
        self._last_path: Optional[Path] = None

    def __enter__(self) -> "YoutubeDLCloud":
        return self

    def __exit__(self, *args: Any) -> None:  # noqa: D401
        return None

    def download(self, urls: Iterable[str]) -> int:
        urls = list(urls)
        if not urls:
            return 0

        outtmpl = self.opts.get("outtmpl")
        if isinstance(outtmpl, dict):
            # yt-dlp lets outtmpl be a {"default": "...", "audio": "..."}
            # mapping. Use whichever the call wanted.
            outtmpl = outtmpl.get("default") or next(iter(outtmpl.values()), None)
        if not outtmpl:
            raise ValueError("YoutubeDLCloud requires opts['outtmpl']")

        format_string = self.opts.get("format")

        audio_only = False
        audio_ext = "m4a"
        # Detect FFmpegExtractAudio postprocessor pattern.
        for pp in self.opts.get("postprocessors") or []:
            if isinstance(pp, dict) and pp.get("key") in (
                "FFmpegExtractAudio", "FFmpegAudioConvertor",
            ):
                audio_only = True
                audio_ext = pp.get("preferredcodec") or audio_ext
        if self.opts.get("extractaudio"):
            audio_only = True

        sections = self.opts.get("download_sections") or None

        # We download each URL serially — matches yt-dlp's default
        # behaviour for our usage.
        for url in urls:
            self._last_path = download(
                url,
                outtmpl,
                format_string=format_string,
                audio_only=audio_only,
                audio_ext=audio_ext,
                sections=sections,
            )
        return 0  # yt-dlp returns 0 on success

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        """Subset matching the way ``voice_clone.py`` uses it.

        We don't have a free metadata-only path through Cloud Run yet
        (the `/download` endpoint always downloads), so when
        ``download=False`` we raise — the laptop callers using this
        path should be migrated to the canonical ``download()``
        helper instead.
        """
        if not download:
            raise NotImplementedError(
                "YoutubeDLCloud.extract_info(download=False) is unsupported. "
                "Use cloudrun_yt_dlp.download() directly with --skip-download "
                "via extra_args, or fall back to local yt_dlp.YoutubeDL."
            )
        out = self.download([url])
        return {
            "_filename": str(self._last_path) if self._last_path else None,
            "ext": (self._last_path.suffix.lstrip(".") if self._last_path else None),
        }


# ---------------------------------------------------------------------------
# Local fallback — kept tiny + last-resort
# ---------------------------------------------------------------------------


def _local_fallback_enabled() -> bool:
    return os.environ.get("CLOUDRUN_YT_DLP_DISABLE_FALLBACK", "").strip() not in ("1", "true", "yes")


def _local_fallback_download(
    url: str,
    output_path: Path,
    *,
    format_string: Optional[str],
    audio_only: bool,
    audio_ext: str,
    sections: Optional[list[str]],
    extra_args: Optional[list[str]],
    timeout_s: int,
) -> Path:
    """Last-resort local yt-dlp invocation (used only when the cloud
    service is unreachable). Mirrors the signature of ``download()``."""
    out_str = str(output_path)
    cmd: list[str] = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings",
        "-o", out_str,
    ]
    if format_string:
        cmd += ["-f", format_string]
    if audio_only:
        cmd += ["-x", "--audio-format", audio_ext]
    if sections:
        for s in sections:
            cmd += ["--download-sections", s]
    if extra_args:
        cmd += list(extra_args)
    cmd += [url]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(cmd, check=True, timeout=timeout_s + 30)
    except subprocess.CalledProcessError as exc:
        raise CloudRunYtDlpFailed(
            f"local yt-dlp fallback failed: returncode={exc.returncode}"
        ) from exc
    except FileNotFoundError as exc:
        raise CloudRunYtDlpUnavailable(
            "local yt-dlp not installed (pip install yt-dlp)"
        ) from exc

    # Substitute %(ext)s if present + verify file exists.
    if "%(ext)s" in out_str:
        # yt-dlp wrote with whatever extension it picked. Find the file.
        # Audit Q2.31 — pre-fix this was ``Path("/").glob(stem_glob.lstrip("/"))``
        # which globs the ENTIRE filesystem starting at root. If the
        # output path was passed as a non-absolute string, lstrip("/")
        # left a relative-looking glob that walked from root. Now we
        # restrict the glob to the parent dir of the configured
        # output_path — yt-dlp always writes there per ``-o`` flag.
        stem = Path(out_str.replace("%(ext)s", "*"))
        if stem.is_absolute():
            search_root = stem.parent
            pattern = stem.name
        else:
            search_root = output_path.parent
            pattern = stem.name
        candidates = sorted(search_root.glob(pattern))
        if not candidates:
            raise CloudRunYtDlpFailed(
                f"local yt-dlp produced no file matching {stem} in {search_root}"
            )
        return candidates[0]
    if not output_path.exists():
        raise CloudRunYtDlpFailed(f"local yt-dlp did not produce {output_path}")
    return output_path
