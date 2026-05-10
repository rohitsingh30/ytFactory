"""ytFactory TTS — Chatterbox server (Cloud Run GPU L4).
Single-model. Same /synth contract as F5 + Higgs services.

E2E DEBUG INSTRUMENTATION (2026-05-06): wraps torch.load,
safetensors.load_file, and hf_hub_download with timing logs so we can
see exactly where the chatterbox cold-start spends its 8-10 minutes.
Triggered by env DEBUG_TIMING=1.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("ytfactory.tts.chatterbox")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")
# DEBUG_TIMING gates only the verbose [PHASE]/[IO]/[HF]/[GPU]/[STK] logs.
# The assign=True monkey-patch on T3 + S3Token2Wav runs unconditionally
# (it's the production fix for the 14 min → 4.4 min cold-start).
DEBUG_TIMING = os.environ.get("DEBUG_TIMING", "0") == "1"

# Phase boundary tracking. Container start to first /synth.
_BOOT_T0 = time.monotonic()


def _phase(label: str) -> None:
    if DEBUG_TIMING:
        logger.info(f"[PHASE +{time.monotonic() - _BOOT_T0:7.2f}s] {label}")


_phase("server.py module imported")


def _install_assign_patch() -> None:
    """Force assign=True on chatterbox's T3 + S3Token2Wav load_state_dict.
    Drops cold-start from ~14 min → ~4.4 min. Always-on (independent
    of DEBUG_TIMING). See docs/cloudrun_persistent_weights.md."""
    import torch.nn as nn
    _ASSIGN_TARGETS = {"T3", "S3Token2Wav"}
    _orig_lsd = nn.Module.load_state_dict
    def _patched_lsd(self, *a, **kw):
        cls = type(self).__name__
        if cls in _ASSIGN_TARGETS and "assign" not in kw:
            kw["assign"] = True
        return _orig_lsd(self, *a, **kw)
    nn.Module.load_state_dict = _patched_lsd


_install_assign_patch()


def _install_timing_patches() -> None:
    """Wrap torch.load, safetensors.torch.load_file, hf_hub_download
    with timing logs. Idempotent. Only when DEBUG_TIMING=1."""
    if not DEBUG_TIMING:
        return
    import torch
    import huggingface_hub.file_download as hf_dl
    from safetensors import torch as st_torch

    _orig_torch_load = torch.load
    def _patched_torch_load(*args, **kwargs):
        f = args[0] if args else kwargs.get("f", "?")
        size_b = -1
        try:
            size_b = os.path.getsize(f) if isinstance(f, (str, os.PathLike)) else -1
        except Exception:
            pass
        t0 = time.monotonic()
        r = _orig_torch_load(*args, **kwargs)
        dt = time.monotonic() - t0
        mb = size_b / (1024 * 1024) if size_b > 0 else 0
        logger.info(f"[IO  +{time.monotonic()-_BOOT_T0:7.2f}s] torch.load   "
                    f"{dt:6.2f}s  {mb:7.1f}MB  {f}")
        return r
    torch.load = _patched_torch_load

    _orig_st = st_torch.load_file
    def _patched_st(filename, *a, **kw):
        size_b = -1
        try:
            size_b = os.path.getsize(filename)
        except Exception:
            pass
        t0 = time.monotonic()
        r = _orig_st(filename, *a, **kw)
        dt = time.monotonic() - t0
        mb = size_b / (1024 * 1024) if size_b > 0 else 0
        logger.info(f"[IO  +{time.monotonic()-_BOOT_T0:7.2f}s] safetensors  "
                    f"{dt:6.2f}s  {mb:7.1f}MB  {filename}")
        return r
    st_torch.load_file = _patched_st

    _orig_hf = hf_dl.hf_hub_download
    def _patched_hf(*args, **kwargs):
        repo = kwargs.get("repo_id", args[0] if args else "?")
        fn = kwargs.get("filename", "?")
        t0 = time.monotonic()
        r = _orig_hf(*args, **kwargs)
        dt = time.monotonic() - t0
        logger.info(f"[HF  +{time.monotonic()-_BOOT_T0:7.2f}s] hf_hub_dl    "
                    f"{dt:6.2f}s  {repo}/{fn} -> {r}")
        return r
    hf_dl.hf_hub_download = _patched_hf

    _orig_snap = __import__("huggingface_hub").snapshot_download
    def _patched_snap(*args, **kwargs):
        repo = kwargs.get("repo_id", args[0] if args else "?")
        t0 = time.monotonic()
        r = _orig_snap(*args, **kwargs)
        logger.info(f"[HF  +{time.monotonic()-_BOOT_T0:7.2f}s] snapshot_dl  "
                    f"{time.monotonic()-t0:6.2f}s  {repo} -> {r}")
        return r
    import huggingface_hub
    huggingface_hub.snapshot_download = _patched_snap

    # nn.Module.cuda / .to / load_state_dict — see GPU upload + state assign cost
    import torch.nn as nn
    _orig_cuda = nn.Module.cuda
    def _patched_cuda(self, *a, **kw):
        cls = type(self).__name__
        np = sum(p.numel() for p in self.parameters() if p is not None)
        size_mb = (np * 4) / (1024 * 1024) if np else 0
        t0 = time.monotonic()
        r = _orig_cuda(self, *a, **kw)
        dt = time.monotonic() - t0
        if dt >= 0.05 or size_mb > 1:
            logger.info(f"[GPU +{time.monotonic()-_BOOT_T0:7.2f}s] Module.cuda  "
                        f"{dt:6.2f}s  {size_mb:7.1f}MB  {cls}")
        return r
    nn.Module.cuda = _patched_cuda

    _orig_to = nn.Module.to
    def _patched_to(self, *a, **kw):
        cls = type(self).__name__
        np = sum(p.numel() for p in self.parameters() if p is not None)
        size_mb = (np * 4) / (1024 * 1024) if np else 0
        t0 = time.monotonic()
        r = _orig_to(self, *a, **kw)
        dt = time.monotonic() - t0
        if dt >= 0.1 or size_mb > 10:
            target = a[0] if a else kw.get("device", "?")
            logger.info(f"[GPU +{time.monotonic()-_BOOT_T0:7.2f}s] Module.to    "
                        f"{dt:6.2f}s  {size_mb:7.1f}MB  {cls} -> {target}")
        return r
    nn.Module.to = _patched_to

    # Timing wrapper around (already assign-patched) load_state_dict.
    _orig_lsd = nn.Module.load_state_dict
    def _patched_lsd(self, *a, **kw):
        cls = type(self).__name__
        t0 = time.monotonic()
        r = _orig_lsd(self, *a, **kw)
        dt = time.monotonic() - t0
        if dt >= 0.1:
            logger.info(f"[SD  +{time.monotonic()-_BOOT_T0:7.2f}s] load_state   "
                        f"{dt:6.2f}s  {cls}")
        return r
    nn.Module.load_state_dict = _patched_lsd

    # Stack-sampler: every 5s while model is loading, dump the main thread's stack
    import threading, traceback, sys as _sys
    _stop_sampler = threading.Event()
    def _sampler():
        main_id = threading.main_thread().ident
        while not _stop_sampler.is_set():
            time.sleep(5)
            frames = _sys._current_frames()
            f = frames.get(main_id)
            if f is None:
                continue
            stk = traceback.extract_stack(f, limit=5)
            top = stk[-1] if stk else None
            if top:
                logger.info(f"[STK +{time.monotonic()-_BOOT_T0:7.2f}s] in {top.filename}:{top.lineno} "
                            f"{top.name}() :: {(top.line or '')[:120]}")
    threading.Thread(target=_sampler, daemon=True, name="cold-stack-sampler").start()

    _phase("timing patches installed (torch.load, safetensors, hf_hub_download, Module.cuda/to/load_state_dict, stack-sampler)")


_install_timing_patches()


app = FastAPI(title="ytfactory-tts-chatterbox", version="1")
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        _phase("BEGIN _model() → from chatterbox.tts import ChatterboxTTS")
        t_imp = time.monotonic()
        from chatterbox.tts import ChatterboxTTS
        _phase(f"import done ({time.monotonic()-t_imp:.2f}s) → from_pretrained(device=cuda)")
        t_load = time.monotonic()
        _MODEL = ChatterboxTTS.from_pretrained(device="cuda")
        _phase(f"from_pretrained done ({time.monotonic()-t_load:.2f}s) → MODEL ready")
    return _MODEL


class SynthIn(BaseModel):
    model: str = Field("chatterbox")
    text: str
    ref_audio_b64: str
    ref_text: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None
    exaggeration: float = 0.5
    cfg_weight: float = 0.5


_REF_DIR = Path(tempfile.gettempdir()) / "ytfactory-tts-refs"
_REF_DIR.mkdir(parents=True, exist_ok=True)


def _ref_audio_to_path(ref_b64: str) -> Path:
    if not ref_b64:
        raise ValueError(
            "empty ref_audio_b64 — Chatterbox is a voice-cloning model and "
            "requires a base64-encoded reference WAV. Pass ref_audio_path on "
            "the client side or hand-craft the payload with a real reference."
        )
    raw = base64.b64decode(ref_b64)
    if len(raw) < 44:  # WAV header is 44 bytes minimum
        raise ValueError(
            f"ref_audio_b64 decoded to {len(raw)} bytes — too small to be a "
            "valid WAV (header is 44 bytes). Likely the client sent empty or "
            "corrupted base64."
        )
    sha = hashlib.sha256(raw).hexdigest()[:16]
    path = _REF_DIR / f"{sha}.wav"
    if not path.exists():
        path.write_bytes(raw)
    return path


@app.get("/readyz")
def readyz() -> dict:
    t0 = time.time()
    _model()
    return {"status": "ready", "warm_s": round(time.time() - t0, 2)}


@app.post("/synth")
def synth(req: SynthIn) -> JSONResponse:
    _phase(f"=== /synth begin (text_chars={len(req.text)}) ===")
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    t_ref = time.monotonic()
    try:
        ref_path = _ref_audio_to_path(req.ref_audio_b64)
    except Exception as e:
        raise HTTPException(400, f"bad ref_audio_b64: {e}")
    _phase(f"  ref-decode {time.monotonic()-t_ref:.3f}s -> {ref_path}")

    out_path = Path(tempfile.gettempdir()) / f"out-{uuid.uuid4().hex[:12]}.wav"
    t0 = time.time()
    try:
        import soundfile as sf
        t_m = time.monotonic()
        model = _model()
        _phase(f"  _model() returned ({time.monotonic()-t_m:.3f}s; cold load shows above)")
        t_gen = time.monotonic()
        wav = model.generate(
            req.text, audio_prompt_path=str(ref_path),
            exaggeration=req.exaggeration, cfg_weight=req.cfg_weight,
        )
        _phase(f"  model.generate() {time.monotonic()-t_gen:.3f}s")
        sr = int(model.sr)
        t_np = time.monotonic()
        audio_np = wav.detach().cpu().numpy().squeeze()
        _phase(f"  detach+cpu+numpy {time.monotonic()-t_np:.3f}s shape={audio_np.shape} sr={sr}")
        if abs(req.speed - 1.0) > 0.01:
            raw = out_path.with_suffix(".raw.wav")
            sf.write(str(raw), audio_np, sr)
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw), "-filter:a",
                 f"atempo={req.speed:.4f}", "-ar", str(sr), "-ac", "1",
                 str(out_path)],
                check=True, capture_output=True,
            )
            raw.unlink(missing_ok=True)
        else:
            t_sf = time.monotonic()
            sf.write(str(out_path), audio_np, sr)
            _phase(f"  sf.write {time.monotonic()-t_sf:.3f}s -> {out_path}")
    except Exception as e:
        logger.exception("chatterbox synth failed")
        raise HTTPException(500, f"synth error: {e}")
    wall_s = time.time() - t0
    _phase(f"=== /synth done in {wall_s:.3f}s ===")

    wav_bytes = out_path.read_bytes()
    sha256 = hashlib.sha256(wav_bytes).hexdigest()
    duration_s = _wav_duration_s(wav_bytes)
    payload = {
        "model": "chatterbox",
        "duration_s": round(duration_s, 3),
        "wall_s": round(wall_s, 3),
        "rtf": round(wall_s / max(duration_s, 1e-3), 3),
        "sha256": sha256,
    }
    if req.output == "gcs" or len(wav_bytes) > INLINE_LIMIT_BYTES:
        payload["output_gcs"] = _upload_to_gcs(
            wav_bytes,
            object_name=f"{req.gcs_object_prefix or ''}{uuid.uuid4().hex}.wav",
        )
    else:
        payload["output_inline"] = base64.b64encode(wav_bytes).decode("ascii")
    out_path.unlink(missing_ok=True)
    return JSONResponse(payload)


def _wav_duration_s(b: bytes) -> float:
    if len(b) < 44 or b[:4] != b"RIFF":
        return 0.0
    sr = int.from_bytes(b[24:28], "little")
    bits = int.from_bytes(b[34:36], "little")
    chans = int.from_bytes(b[22:24], "little")
    data_size = int.from_bytes(b[40:44], "little")
    if sr == 0 or bits == 0 or chans == 0:
        return 0.0
    return data_size / (sr * (bits // 8) * chans)


_GCS_CLIENT = None


def _upload_to_gcs(wav_bytes: bytes, *, object_name: str) -> str:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage
        _GCS_CLIENT = storage.Client()
    bucket = _GCS_CLIENT.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(wav_bytes, content_type="audio/wav")
    return f"gs://{GCS_BUCKET}/{object_name}"
