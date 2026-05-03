"""FastAPI wrapper for the ytFactory pipeline.

Runs locally on the M-series Mac because the pipeline depends on
local Flux/Whisper/Kokoro models. Exposed publicly via cloudflared
tunnel (matches the shofferai laptop-relay deployment pattern).

Endpoints:
    GET  /                          → niche picker HTML
    GET  /static/*                  → assets
    POST /api/jobs                  → start a job, returns {job_id}
    GET  /api/jobs/{id}             → job status snapshot
    GET  /api/jobs/{id}/events      → SSE stream of stage events
    GET  /api/jobs/{id}/short       → final mp4 (when done)
    GET  /api/jobs/{id}/thumb/{i}   → image thumbnail (img_NN.png)

Pipeline integration: every job spawns
``pull_stories.py`` and then ``make_shorts.py`` as subprocesses
(re-using the existing CLI rather than refactoring). We tail stdout,
parse the structured prefixes already emitted (``[1/4]``, ``[prompts]``,
``[3/4] mflux: generating N images``, ``[critic] score=...``), and
fan them out as SSE events to the browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from dataclasses import asdict as _dc_asdict

import yaml
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from pipeline.sources.base import RawStory, save_raw
from pipeline import niches as _niches
from pipeline import telemetry as tlm


# ---- Auth ---------------------------------------------------------------
#
# Match shofferai's RELAY_AUTH_TOKEN pattern. When YTFACTORY_TOKEN is set,
# every API and the home page require it (cookie OR ?token= query). Without
# it, the server is open (localhost-only safe; public tunnel NOT safe).
#
# Set it before exposing publicly:
#     export YTFACTORY_TOKEN=$(uuidgen)
#
AUTH_TOKEN = os.environ.get("YTFACTORY_TOKEN", "").strip() or None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = PROJECT_ROOT / ".venv" / "bin" / "python"
DATA_DIR = PROJECT_ROOT / "data"
SHORTS_DIR = DATA_DIR / "shorts"
CACHE_DIR = DATA_DIR / "cache"
JOBS_PERSIST_DIR = DATA_DIR / "_jobs"


# Languages we expose in the picker. ``code`` matches Kokoro's lang param.
LANGUAGES: list[dict[str, str]] = [
    {"code": "en-us", "label": "English (US)", "flag": "🇺🇸"},
    {"code": "en-gb", "label": "English (UK)", "flag": "🇬🇧"},
    {"code": "es",    "label": "Español",      "flag": "🇪🇸"},
    {"code": "fr-fr", "label": "Français",     "flag": "🇫🇷"},
    {"code": "it",    "label": "Italiano",     "flag": "🇮🇹"},
    {"code": "pt-br", "label": "Português",    "flag": "🇧🇷"},
    {"code": "hi",    "label": "हिन्दी / Hindi", "flag": "🇮🇳"},
    {"code": "ja",    "label": "日本語 / Japanese", "flag": "🇯🇵"},
    {"code": "cmn",   "label": "中文 / Mandarin", "flag": "🇨🇳"},
]

# AITA-style hook sample text per language (~3s spoken). The whole web
# UI shows a preview of the voice reading this line so you can pick the
# vibe before generating.
SAMPLE_TEXT_BY_LANG: dict[str, str] = {
    "en-us": "AITA for refusing to split the bill? My friends ordered four hundred dollars of steak and wine.",
    "en-gb": "AITA for refusing to split the bill? My mates ordered four hundred quid of steak and wine.",
    "es":    "¿Soy el imbécil por negarme a dividir la cuenta? Mis amigos pidieron cuatrocientos dólares de bistec y vino.",
    "fr-fr": "Suis-je le con d'avoir refusé de partager l'addition? Mes amis ont commandé quatre cents dollars de steak et de vin.",
    "it":    "Sono io lo stronzo per essermi rifiutato di dividere il conto? I miei amici hanno ordinato quattrocento dollari di bistecca e vino.",
    "pt-br": "Eu sou o babaca por me recusar a dividir a conta? Meus amigos pediram quatrocentos dólares em filé e vinho.",
    "hi":    "Kya main galat hoon ki maine bill split karne se mana kar diya? Mere doston ne char sau dollar ka steak aur wine order kiya.",
    "ja":    "AITA、会計を割り勘にするのを拒んだ私が悪いの? 友達は四百ドル分のステーキとワインを頼んだのよ。",
    "cmn":   "我是混蛋吗？因为我拒绝平摊账单。我的朋友们点了四百美元的牛排和红酒。",
}

# Curated Kokoro voices for the picker — broader set across 9 languages.
# IDs come from kokoro_onnx.Kokoro.get_voices(). Prefix scheme:
#   af/am — American F/M    bf/bm — British F/M
#   ef/em — Spanish F/M     ff — French F        if/im — Italian F/M
#   pf/pm — Portuguese F/M  hf/hm — Hindi F/M
#   jf/jm — Japanese F/M    zf/zm — Mandarin F/M
VOICES: list[dict[str, Any]] = [
    # English — American
    {"id": "af_bella",   "label": "Bella",   "lang": "en-us", "tone": "warm female narrator", "default": True},
    {"id": "af_heart",   "label": "Heart",   "lang": "en-us", "tone": "youthful, energetic"},
    {"id": "af_sarah",   "label": "Sarah",   "lang": "en-us", "tone": "confident female"},
    {"id": "af_nicole",  "label": "Nicole",  "lang": "en-us", "tone": "soft, conspiratorial"},
    {"id": "af_nova",    "label": "Nova",    "lang": "en-us", "tone": "modern female"},
    {"id": "af_aoede",   "label": "Aoede",   "lang": "en-us", "tone": "smooth female"},
    {"id": "am_michael", "label": "Michael", "lang": "en-us", "tone": "solid male narrator"},
    {"id": "am_adam",    "label": "Adam",    "lang": "en-us", "tone": "older, deeper"},
    {"id": "am_liam",    "label": "Liam",    "lang": "en-us", "tone": "casual male"},
    {"id": "am_eric",    "label": "Eric",    "lang": "en-us", "tone": "crisp male"},
    {"id": "am_onyx",    "label": "Onyx",    "lang": "en-us", "tone": "deep, dramatic"},
    {"id": "am_puck",    "label": "Puck",    "lang": "en-us", "tone": "playful male"},
    # English — British
    {"id": "bf_emma",    "label": "Emma",    "lang": "en-gb", "tone": "crisp British female"},
    {"id": "bf_alice",   "label": "Alice",   "lang": "en-gb", "tone": "elegant British female"},
    {"id": "bf_isabella","label": "Isabella","lang": "en-gb", "tone": "literary British female"},
    {"id": "bf_lily",    "label": "Lily",    "lang": "en-gb", "tone": "youthful British female"},
    {"id": "bm_george",  "label": "George",  "lang": "en-gb", "tone": "warm British male"},
    {"id": "bm_daniel",  "label": "Daniel",  "lang": "en-gb", "tone": "neutral British male"},
    {"id": "bm_lewis",   "label": "Lewis",   "lang": "en-gb", "tone": "smooth British male"},
    {"id": "bm_fable",   "label": "Fable",   "lang": "en-gb", "tone": "storyteller male"},
    # Spanish
    {"id": "ef_dora",    "label": "Dora",    "lang": "es",    "tone": "narradora cálida"},
    {"id": "em_alex",    "label": "Alex",    "lang": "es",    "tone": "narrador firme"},
    # French
    {"id": "ff_siwis",   "label": "Siwis",   "lang": "fr-fr", "tone": "narratrice douce"},
    # Italian
    {"id": "if_sara",    "label": "Sara",    "lang": "it",    "tone": "narratrice calda"},
    {"id": "im_nicola",  "label": "Nicola",  "lang": "it",    "tone": "narratore solido"},
    # Portuguese
    {"id": "pf_dora",    "label": "Dora",    "lang": "pt-br", "tone": "narradora calma"},
    {"id": "pm_alex",    "label": "Alex",    "lang": "pt-br", "tone": "narrador grave"},
    # Hindi
    {"id": "hf_alpha",   "label": "Alpha",   "lang": "hi",    "tone": "स्पष्ट कथावाचक"},
    {"id": "hf_beta",    "label": "Beta",    "lang": "hi",    "tone": "कोमल कथावाचक"},
    {"id": "hm_omega",   "label": "Omega",   "lang": "hi",    "tone": "गंभीर कथावाचक"},
    {"id": "hm_psi",     "label": "Psi",     "lang": "hi",    "tone": "मित्रवत् कथावाचक"},
    # Japanese
    {"id": "jf_alpha",   "label": "Alpha",   "lang": "ja",    "tone": "明るい女性ナレーター"},
    {"id": "jf_nezumi",  "label": "Nezumi",  "lang": "ja",    "tone": "落ち着いた女性"},
    {"id": "jm_kumo",    "label": "Kumo",    "lang": "ja",    "tone": "渋い男性"},
    # Mandarin
    {"id": "zf_xiaoxiao","label": "Xiaoxiao","lang": "cmn",   "tone": "温暖的女声"},
    {"id": "zf_xiaobei", "label": "Xiaobei", "lang": "cmn",   "tone": "清晰女声"},
    {"id": "zm_yunxi",   "label": "Yunxi",   "lang": "cmn",   "tone": "沉稳男声"},
    {"id": "zm_yunjian", "label": "Yunjian", "lang": "cmn",   "tone": "戏剧男声"},
]


def _accent_for_voice(v: dict) -> str:
    """Format the accent/flag string from a voice's lang field."""
    code = v.get("lang", "en-us")
    for L in LANGUAGES:
        if L["code"] == code:
            return f"{L['flag']} {L['label']}"
    return code


# Decorate VOICES with accent display string (used by /api/voices).
for _v in VOICES:
    _v["accent"] = _accent_for_voice(_v)

DEFAULT_VOICE = next((v["id"] for v in VOICES if v.get("default")), VOICES[0]["id"])

VOICE_SAMPLES_DIR = Path(__file__).resolve().parent / "static" / "voice_samples"


# UI presentation for each niche. Routing fields (``channel``,
# ``channel_dir``) are *not* listed here — they're injected from
# ``pipeline.niches.NICHE_CHANNEL`` at module load so the routing has a
# single source of truth (see _NICHE_UI loop below). Riff has no entry
# in NICHE_CHANNEL because its channel is resolved per-job from the
# imitation profile.
_NICHE_UI: dict[str, dict[str, Any]] = {
    "aita": {
        "label": "AITA — Am I the Asshole",
        "description": "r/AmItheAsshole top-of-day. Conflict, drama, vote-bait closer.",
        "subreddit": "AmItheAsshole",
        "color_from": "from-rose-500",
        "color_to": "to-orange-500",
    },
    "aita_cliffhanger": {
        "label": "AITA — Cliffhanger (Part 1)",
        "description": "r/AmItheAsshole, but cut at the highest-tension moment with a SUBSCRIBE-for-Part-2 closer. Defaults to animated; swap to text or cooking via --channel.",
        "subreddit": "AmItheAsshole",
        "color_from": "from-rose-600",
        "color_to": "to-fuchsia-500",
    },
    "tifu": {
        "label": "TIFU — Today I Fucked Up",
        "description": "r/tifu top-of-day. Embarrassment, regret, oh-no story arcs.",
        "subreddit": "tifu",
        "color_from": "from-amber-500",
        "color_to": "to-red-500",
    },
    "malicious": {
        "label": "Malicious Compliance",
        "description": "r/MaliciousCompliance. Following the rules to spite the rule-maker.",
        "subreddit": "MaliciousCompliance",
        "color_from": "from-violet-500",
        "color_to": "to-pink-500",
    },
    "prorevenge": {
        "label": "Pro Revenge",
        "description": "r/ProRevenge. Calculated, satisfying, slow-burn payback.",
        "subreddit": "ProRevenge",
        "color_from": "from-fuchsia-500",
        "color_to": "to-purple-500",
    },
    "oddities": {
        "label": "Wiki Oddities",
        "description": "Wikipedia 'List of unusual deaths (21st century)'. Bizarre history.",
        "wiki_page": "unusual_deaths_21c",
        "color_from": "from-emerald-500",
        "color_to": "to-cyan-500",
    },
    "tih": {
        "label": "Today in History",
        "description": "Wikipedia 'On this day' for today's date. One historical event per Short.",
        "color_from": "from-sky-500",
        "color_to": "to-indigo-500",
    },
    "sports_ranked": {
        "label": "Sports — Top-5 Countdown",
        "description": "Tier-list ranking format. 5 ranked moments × ~10s, footage cut-in per rank, judgment-bait closer (LIKE if you agree / COMMENT who you'd swap in). Authored via /make-ranking — pick the dimension, the skill picks 5 from your sports pool. ~58s target, the 2026 sweet spot.",
        "color_from": "from-emerald-600",
        "color_to": "to-teal-500",
    },
    "riff": {
        "label": "Riff on a YouTube Short",
        "description": "Paste a YouTube URL. AI analyses tone, hook, structure, and aesthetic, then writes N fresh Shorts in the same style with novel content.",
        "needs_url": True,
        "color_from": "from-yellow-400",
        "color_to": "to-pink-500",
    },
}


def _build_niches() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for k, ui in _NICHE_UI.items():
        merged: dict[str, Any] = dict(ui)
        if k in _niches.NICHE_CHANNEL:
            channel_dir, channel_yaml = _niches.NICHE_CHANNEL[k]
            merged["channel"] = channel_yaml
            merged["channel_dir"] = channel_dir
        out[k] = merged
    return out


NICHES: dict[str, dict[str, Any]] = _build_niches()


@dataclass
class StageEvent:
    """One progress event sent to the browser via SSE."""
    job_id: str
    ts: float
    stage: str           # "pull" | "rewrite" | "cast" | "tts" | "beats" | "prompts" | "image" | "compose" | "critic" | "done" | "error"
    status: str          # "start" | "progress" | "done" | "error"
    message: str = ""
    data: dict = field(default_factory=dict)

    def to_sse(self) -> dict:
        return {"event": self.stage, "data": json.dumps(asdict(self))}


@dataclass
class Job:
    job_id: str
    niche: str
    options: dict
    created_at: float
    slug: str | None = None
    state: str = "queued"            # queued | running | done | error
    error: str | None = None
    out_mp4: Path | None = None
    score: int | None = None
    critique: dict = field(default_factory=dict)
    # Live event log (also broadcast over SSE).
    events: list[StageEvent] = field(default_factory=list)
    # Per-stage timing for the UI.
    stage_started: dict[str, float] = field(default_factory=dict)
    stage_done: dict[str, float] = field(default_factory=dict)
    # Authored prompts (so the UI can show them while images render).
    beat_prompts: list[dict] = field(default_factory=list)
    # Riff-mode extras: imitation profile + per-seed render results.
    profile: dict = field(default_factory=dict)
    seeds: list[dict] = field(default_factory=list)
    seed_idx: int = 0
    seed_total: int = 1
    # One mp4 per rendered seed (key = seed_idx).
    out_mp4s: dict[int, str] = field(default_factory=dict)


JOBS: dict[str, Job] = {}
SUBSCRIBERS: dict[str, list[asyncio.Queue]] = {}


# ---- Runtime handles for cancellation -----------------------------------
#
# Heavy work (Flux image gen, ffmpeg compose, F5-TTS) runs in pipeline
# subprocesses spawned with ``start_new_session=True`` so they survive a
# uvicorn reload. That same isolation means a plain ``task.cancel()`` on
# the wrapping asyncio coroutine does NOT kill the subprocess — it just
# detaches us from its stdout. To actually free the GPU/CPU we have to
# signal the process group directly.
#
# JOB_RUNTIME holds the live handles (asyncio.Task + currently-running
# subprocess Process). Kept OUT of the Job dataclass so persistence
# (Job → JSON) doesn't choke on un-serialisable handles.
JOB_RUNTIME: dict[str, dict] = {}

# How long a running job is allowed to have zero SSE subscribers before
# the idle watchdog kills it. The browser auto-reconnects an EventSource
# within a few seconds, so 30s tolerates a network blip / brief tab
# switch but not "user closed the tab and walked away".
IDLE_GRACE_SEC = 30.0


def _runtime(job_id: str) -> dict:
    return JOB_RUNTIME.setdefault(job_id, {
        "task": None,
        "proc": None,
        "zero_subs_since": None,
    })


async def cancel_job(job_id: str, *, reason: str = "superseded") -> bool:
    """Kill the in-flight subprocess + cancel the wrapping asyncio task.

    Idempotent: calling on a finished or already-cancelled job is a no-op.
    Order matters here:
      1. Flip ``job.state`` to ``cancelled`` BEFORE emitting so a racing
         emit() from the still-alive subprocess doesn't override it.
      2. Emit a final stage=error event so SSE subscribers terminate.
      3. SIGTERM the process group (subprocess was started with
         ``start_new_session=True``, so pgid == pid). This unblocks
         ``proc.wait()`` inside ``run_subprocess``.
      4. Cancel the asyncio task — this raises CancelledError at whatever
         await point ``run_job`` is currently sitting on, short-circuiting
         the rest of the pipeline (e.g. skipping subsequent riff seeds).
      5. If the proc is still alive 3s later, escalate to SIGKILL.
    """
    job = JOBS.get(job_id)
    if not job or job.state in ("done", "error", "cancelled"):
        return False

    rt = _runtime(job_id)
    job.state = "cancelled"
    job.error = f"cancelled: {reason}"
    emit(job, StageEvent(
        job_id=job_id, ts=time.time(),
        stage="error", status="error",
        message=f"Cancelled — {reason}",
        data={"cancelled": True, "reason": reason},
    ))

    proc = rt.get("proc")
    if proc is not None and proc.returncode is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    task = rt.get("task")
    if task is not None and not task.done():
        task.cancel()

    if proc is not None:
        # Poll for graceful exit; we can't ``await proc.wait()`` here
        # because run_subprocess is already awaiting it from the task we
        # just cancelled.
        for _ in range(30):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.1)
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    print(f"[cancel] job {job_id} cancelled — {reason}")
    return True


async def _idle_watchdog() -> None:
    """Cancel running jobs that have no SSE subscriber for >IDLE_GRACE_SEC.

    Image gen alone can hold ~10GB of resident memory on Apple Silicon,
    so leaving a job running after the browser tab is gone is genuine
    waste. ``zero_subs_since`` is tracked per-job and only reset when a
    fresh subscriber attaches.
    """
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            for jid, job in list(JOBS.items()):
                if job.state != "running":
                    continue
                rt = _runtime(jid)
                if SUBSCRIBERS.get(jid):
                    rt["zero_subs_since"] = None
                    continue
                if rt.get("zero_subs_since") is None:
                    rt["zero_subs_since"] = now
                    continue
                if now - rt["zero_subs_since"] >= IDLE_GRACE_SEC:
                    try:
                        await cancel_job(jid, reason="no active subscriber")
                    except Exception as e:
                        print(f"[watchdog] cancel failed for {jid}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[watchdog] loop error: {e}")


# ---- Job persistence ----------------------------------------------------
#
# The in-memory JOBS dict is wiped on every uvicorn reload. The pipeline
# subprocess survives (start_new_session=True), but the browser sees 404
# on /api/jobs/{id} and shows the "Lost the live trail" banner.
#
# Snapshot every Job to data/_jobs/{id}.json on each emit() so a restart
# can rehydrate state. data/_jobs/ is outside web/, so persist writes
# don't themselves trigger --reload-dir web reloads.

def _persist_job(job: Job) -> None:
    try:
        JOBS_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        path = JOBS_PERSIST_DIR / f"{job.job_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), default=str))
        tmp.replace(path)
    except Exception as e:
        print(f"[persist] {job.job_id} failed: {e}")


def _rehydrate_jobs() -> None:
    if not JOBS_PERSIST_DIR.exists():
        return
    cutoff = time.time() - 24 * 3600
    loaded = 0
    for path in JOBS_PERSIST_DIR.glob("*.json"):
        try:
            d = json.loads(path.read_text())
        except Exception:
            continue
        if float(d.get("created_at", 0)) < cutoff:
            continue
        events_raw = d.pop("events", []) or []
        out_mp4 = d.pop("out_mp4", None)
        out_mp4s_raw = d.pop("out_mp4s", {}) or {}
        try:
            job = Job(**d)
        except TypeError:
            # Schema drifted since this snapshot was written — skip.
            continue
        job.events = [StageEvent(**e) for e in events_raw]
        if out_mp4 and out_mp4 != "None":
            job.out_mp4 = Path(out_mp4)
        job.out_mp4s = {int(k): v for k, v in out_mp4s_raw.items()}
        # Reconcile: if the snapshot says "running" but the mp4 already
        # landed (subprocess outlived a restart), promote to done.
        if job.state == "running" and job.slug:
            mp4 = SHORTS_DIR / f"{job.slug}.mp4"
            if mp4.exists():
                job.state = "done"
                job.out_mp4 = mp4
        JOBS[job.job_id] = job
        loaded += 1
    if loaded:
        print(f"[persist] rehydrated {loaded} job(s) from {JOBS_PERSIST_DIR}")


def emit(job: Job, event: StageEvent) -> None:
    """Append to the job log AND fan out to live SSE subscribers.

    Side effect: every stage start/done/error is mirrored into the
    persistent telemetry log so the /api/telemetry views can compute
    rollups (avg / p50 / p95 / max per stage, success rate per niche)
    across all historical jobs.
    """
    job.events.append(event)
    if event.stage and event.status == "start":
        # Latest start wins. Stages that fire multiple times in a job
        # (e.g. compose runs once initially and again after the critic
        # patches a beat) need each pass timed independently against
        # its own start; setdefault would have anchored every done to
        # the first start, inflating the second compose's measured
        # duration to "first start → second done".
        job.stage_started[event.stage] = event.ts
    if event.status == "done":
        job.stage_done[event.stage] = event.ts
        # Stage finished — record duration relative to its first start.
        started_at = job.stage_started.get(event.stage)
        dur_ms: int | None = None
        if started_at is not None:
            dur_ms = max(0, int((event.ts - started_at) * 1000))
        tlm.track(
            "stage_done",
            category="pipeline",
            success=True,
            duration_ms=dur_ms,
            job_id=job.job_id,
            metadata={
                "stage": event.stage,
                "niche": job.niche,
                "slug": job.slug,
                **{k: v for k, v in (event.data or {}).items()
                   if k in ("seed_idx", "n", "total", "n_beats", "total_s",
                            "score", "i")},
            },
        )
    if event.status == "error":
        # User-cancellations come through the same status="error" channel
        # but they aren't engineering failures — bucket them as
        # job_cancelled so the success-rate rollup doesn't conflate
        # "user closed the tab" with "the pipeline crashed."
        is_cancel = bool((event.data or {}).get("cancelled"))
        # Keep the TAIL of the message, not the head — Python tracebacks
        # put the exception type + message at the end, and the head is
        # mostly the same boilerplate ("Traceback (most recent call
        # last): ...") that doesn't help fingerprint root causes.
        msg = event.message or ""
        msg_tail = msg[-600:] if len(msg) > 600 else msg
        tlm.track(
            "job_cancelled" if is_cancel else "stage_error",
            category="pipeline",
            success=False,
            duration_ms=None,
            job_id=job.job_id,
            metadata={
                "stage": event.stage,
                "niche": job.niche,
                "slug": job.slug,
                "message": msg_tail,
                **({"reason": (event.data or {}).get("reason")} if is_cancel else {}),
            },
        )
    for q in SUBSCRIBERS.get(job.job_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    _persist_job(job)


# ---- stdout-line parser -------------------------------------------------

# These regexes match the structured prefixes the existing pipeline
# already prints. We translate them into typed StageEvents so the
# frontend can render rich progress without changing the pipeline.

_RE_TTS_START = re.compile(r"^\[1/4\] TTS(?:\s*\(([^)]+)\))?")  # captures provider e.g. "kokoro" / "f5_tts"
_RE_TTS_CACHED = re.compile(r"^\[1/4\] TTS cached")
_RE_BEATS_START = re.compile(r"^\[2/4\] (\S+).+timestamps")
_RE_BEATS_CACHED = re.compile(r"^\[2/4\] beats cached")
_RE_BEATS_DONE = re.compile(r"^\s+(\d+) beats, total ([\d.]+)s")
_RE_PROMPTS_START = re.compile(r"^\[prompts\] authoring (\d+) beat prompts")
_RE_PROMPTS_DONE = re.compile(r"^\[prompts\] wrote ")
_RE_IMG_START = re.compile(r"^\[3/4\] \S+: generating (\d+) images")
_RE_IMG_BEAT = re.compile(r"^\s+\[(\d+)/(\d+)\]")
# Per-image completion marker — printed by make_shorts.py AFTER the
# img_NN.png is written (and QC-checked) to disk. This is the safe
# moment for the frontend to fetch /api/jobs/{id}/thumb/{i}; the
# earlier _RE_IMG_BEAT line fires BEFORE generate() runs.
_RE_IMG_DONE = re.compile(r"^\[image-done\] beat (\d+) of (\d+)")
# Per-attempt diagnostic. Printed once per images.generate() call inside
# the QC retry loop (make_shorts.py). Surfaces silent retry doubling:
# a beat that retries N times costs N× wall time but used to look
# identical in telemetry to a clean beat — only the aggregate beat
# duration moved. Capturing per-attempt {dt_s, qc_pass, reason} lets
# /api/telemetry rollups split "model is slow" from "QC rejected and
# we re-rolled silently."
_RE_IMG_ATTEMPT = re.compile(
    r"^\[image-attempt\] beat (\d+) attempt (\d+)/(\d+) "
    r"dt=([\d.]+)s qc=(pass|fail)(?: reason=(.+))?$"
)
_RE_COMPOSE_START = re.compile(r"^\[4/4\] ffmpeg compose")
_RE_COMPOSE_RECOMPOSE = re.compile(r"^\[critic\] recomposing")
_RE_COMPOSE_DONE = re.compile(r"^\[compose\] wrote (.+\.mp4)")
_RE_CRITIC_START = re.compile(r"^\[critic\] sending to claude CLI")
_RE_CRITIC_SCORE = re.compile(r"^\[critic\] score=(\d+) — (.+)")
_RE_REWRITE = re.compile(r"^\[rewrite\] authoring narration")
_RE_REWRITE_DONE = re.compile(r"^\[rewrite\] done (.+)")
_RE_CAST = re.compile(r"^\[cast\] authoring narrator")
_RE_CAST_DONE = re.compile(r"^\[cast\] done (.+)")
_RE_PULL_RAW = re.compile(r"^\[pull\] raw written (.+)")
_RE_FILTER_DROP = re.compile(r"^\s+\[filter\] DROP")
_RE_VISUAL_OK = re.compile(r"^\s+wrote (\d+) raw, (\d+) scripts")


def parse_stdout_line(line: str) -> tuple[str, str, str, dict] | None:
    """Return (stage, status, message, data) or None if not a progress line."""
    line_r = line.rstrip()
    if not line_r:
        return None

    m = _RE_PULL_RAW.match(line_r)
    if m:
        return "pull", "done", f"Pulled story: {m.group(1)}", {"slug": m.group(1)}
    if _RE_REWRITE.match(line_r):
        return "rewrite", "start", "Rewriting narration (claude CLI)", {}
    m = _RE_REWRITE_DONE.match(line_r)
    if m:
        return "rewrite", "done", "Rewrite done", {"slug": m.group(1)}
    if _RE_CAST.match(line_r):
        return "cast", "start", "Casting narrator (claude CLI)", {}
    m = _RE_CAST_DONE.match(line_r)
    if m:
        return "cast", "done", "Cast done", {"slug": m.group(1)}
    if _RE_FILTER_DROP.match(line_r):
        return "pull", "progress", line_r.strip(), {}
    m = _RE_VISUAL_OK.match(line_r)
    if m:
        return None  # superseded by per-stage events above

    # TTS-cached must be checked BEFORE TTS-start: the start regex's
    # prefix match also accepts "[1/4] TTS cached:" so unless we test
    # cached first, the cached branch is unreachable and the UI never
    # sees the cache-hit done-event.
    if _RE_TTS_CACHED.match(line_r):
        return "tts", "done", "TTS cached", {}
    m = _RE_TTS_START.match(line_r)
    if m:
        # Reflect the actual provider make_shorts.py prints; falls back
        # to "TTS" if the provider tag is missing (older log format).
        # f5_tts here typically means a cloned voice was bound for this
        # slug — surface that distinction in the UI so picking a clone
        # vs a Kokoro voice doesn't both label as "Kokoro".
        provider = (m.group(1) or "").strip().lower()
        if provider == "f5_tts":
            label = "Synthesising voice (F5-TTS, cloned)"
        elif provider == "kokoro":
            label = "Synthesising voice (Kokoro)"
        elif provider:
            label = f"Synthesising voice ({provider})"
        else:
            label = "Synthesising voice"
        return "tts", "start", label, {"provider": provider or None}

    if _RE_BEATS_START.match(line_r):
        return "beats", "start", "Aligning words to audio (Whisper)", {}
    if _RE_BEATS_CACHED.match(line_r):
        return "beats", "done", "Beats cached", {}
    m = _RE_BEATS_DONE.match(line_r)
    if m:
        return "beats", "done", f"{m.group(1)} beats / {m.group(2)}s", {
            "n_beats": int(m.group(1)),
            "total_s": float(m.group(2)),
        }

    m = _RE_PROMPTS_START.match(line_r)
    if m:
        return "prompts", "start", f"Authoring {m.group(1)} scene prompts", {
            "n": int(m.group(1)),
        }
    if _RE_PROMPTS_DONE.match(line_r):
        return "prompts", "done", "Scene prompts ready", {}

    m = _RE_IMG_START.match(line_r)
    if m:
        return "image", "start", f"Generating {m.group(1)} images", {
            "total": int(m.group(1)),
        }
    m = _RE_IMG_BEAT.match(line_r)
    if m:
        return "image", "progress", f"Image {m.group(1)} of {m.group(2)}", {
            "i": int(m.group(1)),
            "total": int(m.group(2)),
        }
    m = _RE_IMG_DONE.match(line_r)
    if m:
        # make_shorts.py prints the 0-indexed beat (matches img_NN.png
        # filename); we expose both forms so the frontend can use `i`
        # for display (1-indexed, consistent with image/progress events
        # and the [1/N] convention) and `beat_index` for the thumb URL
        # which takes the 0-indexed integer (/api/jobs/{id}/thumb/{i0}).
        i0 = int(m.group(1))
        total = int(m.group(2))
        return "image", "done", f"Image {i0 + 1} of {total} ready", {
            "i": i0 + 1,
            "beat_index": i0,
            "total": total,
        }
    m = _RE_IMG_ATTEMPT.match(line_r)
    if m:
        beat_i0 = int(m.group(1))
        attempt = int(m.group(2))
        attempts_max = int(m.group(3))
        dt_s = float(m.group(4))
        qc_pass = m.group(5) == "pass"
        reason = (m.group(6) or "").strip() or None
        verdict = "ok" if qc_pass else "qc-fail"
        msg = (
            f"Beat {beat_i0} attempt {attempt}/{attempts_max} "
            f"{dt_s:.1f}s ({verdict})"
        )
        return "image", "progress", msg, {
            "kind": "attempt",
            "beat_index": beat_i0,
            "attempt": attempt,
            "attempts_max": attempts_max,
            "dt_s": dt_s,
            "qc_pass": qc_pass,
            "reason": reason,
        }

    if _RE_COMPOSE_START.match(line_r):
        return "compose", "start", "Composing video (ffmpeg)", {}
    if _RE_COMPOSE_RECOMPOSE.match(line_r):
        return "compose", "start", "Recomposing after critic patch", {"pass": "recompose"}
    m = _RE_COMPOSE_DONE.match(line_r)
    if m:
        return "compose", "done", f"Wrote {Path(m.group(1)).name}", {
            "path": m.group(1),
        }

    if _RE_CRITIC_START.match(line_r):
        return "critic", "start", "Auto-critiquing the Short", {}
    m = _RE_CRITIC_SCORE.match(line_r)
    if m:
        return "critic", "done", f"Score {m.group(1)} — {m.group(2)[:120]}", {
            "score": int(m.group(1)),
            "take": m.group(2),
        }

    return None


# ---- job runner ---------------------------------------------------------


async def run_subprocess(
    job: Job,
    args: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    seed_idx: int | None = None,
) -> tuple[int, str]:
    """Run a subprocess, stream stdout into job events.

    Returns ``(rc, tail)``. ``tail`` is a newline-joined string of the
    last ~120 raw stdout lines, surfaced into error events when the
    subprocess exits non-zero so the UI shows the actual cause (Python
    traceback, ``RuntimeError`` text) instead of a bare
    "make_shorts.py exited 1".

    ``start_new_session=True`` puts the child in its own process group so
    a uvicorn restart (or pkill on the parent) does NOT cascade into the
    pipeline subprocess. The mp4 still finishes writing to disk; the new
    server just can't stream live events for it. Combined with on-disk
    artifacts (data/cache/<slug>/, <slug>.mp4), the worst-case
    after a restart is "Short is on disk but my browser thinks it died" —
    not "I lost 5 minutes of Flux work".

    When ``seed_idx`` is set (riff mode), every emitted event carries
    ``data.seed_idx`` so the UI can route progress to the right per-seed
    card.
    """
    env = os.environ.copy()
    env["YTFACTORY_JOB_ID"] = job.job_id
    # Without this, Python buffers stdout when it isn't a tty, so all of
    # pull_stories.py's stage prints arrive at the parent in one burst at
    # exit — collapsing per-stage durations to 0ms in telemetry. Force
    # line-buffered output so emit() sees stages as they actually happen.
    env["PYTHONUNBUFFERED"] = "1"
    # If the server is hosting a warm in-process diffusion pipe (opt-in
    # via YTFACTORY_PERSIST_IMAGE_PIPE=1 at startup), tell make_shorts.py
    # to render images by HTTP-POSTing to the server instead of cold-
    # loading its own pipe. Saves the 15-30s cold load per job and any
    # IP-adapter image cache — see _IMAGE_WORKER below.
    if _IMAGE_WORKER_ENABLED:
        env["YTFACTORY_IMAGE_WORKER_URL"] = _IMAGE_WORKER_URL
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd),
        start_new_session=True,
        env=env,
    )

    # Publish the handle so cancel_job can SIGTERM the process group if
    # the user starts a new job, closes the tab, etc.
    rt = _runtime(job.job_id)
    rt["proc"] = proc

    assert proc.stdout is not None
    # Keep a rolling tail of stdout so we can attach it to the error
    # event if the subprocess exits non-zero. Without this, anything
    # not matching parse_stdout_line's regexes is silently dropped and
    # the UI just shows "exited 1".
    tail: deque[str] = deque(maxlen=120)
    async for raw_line in proc.stdout:
        try:
            line = raw_line.decode("utf-8", errors="replace")
        except Exception:
            continue
        tail.append(line.rstrip("\n"))
        # Mirror to the server's stdout too so uvicorn logs capture
        # the full pipeline output for post-mortem; otherwise only
        # regex-matching lines leave any trace anywhere.
        print(line, end="" if line.endswith("\n") else "\n", flush=True)
        parsed = parse_stdout_line(line)
        if parsed:
            stage, status, message, data = parsed
            if seed_idx is not None:
                data = {**data, "seed_idx": seed_idx}
            emit(job, StageEvent(
                job_id=job.job_id,
                ts=time.time(),
                stage=stage,
                status=status,
                message=message,
                data=data,
            ))

    rc = await proc.wait()
    if rt.get("proc") is proc:
        rt["proc"] = None
    return rc, "\n".join(tail)


def _format_subprocess_failure(script_name: str, rc: int, tail: str) -> str:
    """Build a human-readable error message that includes the last
    handful of subprocess stdout lines, with a bias toward Python
    traceback / ``Error:`` lines so the UI doesn't show a bare
    "exited 1" when the real cause is e.g. a missing dependency.
    """
    if not tail:
        return f"{script_name} exited {rc}"
    lines = tail.splitlines()
    error_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Traceback") or "Error:" in ln or "Error " in ln:
            error_idx = i
            break
    if error_idx is not None:
        snippet = "\n".join(lines[error_idx:][-40:])
    else:
        snippet = "\n".join(lines[-12:])
    return f"{script_name} exited {rc}\n{snippet}"


def _slug_from_intermediate(channel_dir: str) -> str | None:
    """Pick the most recently created script slug for this channel."""
    scripts_dir = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "scripts"
    if not scripts_dir.exists():
        return None
    scripts = sorted(
        scripts_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not scripts:
        return None
    return scripts[0].stem


async def run_job(job: Job) -> None:
    """The job lifecycle. Pull → render → done."""
    niche_cfg = NICHES[job.niche]
    job.state = "running"

    if job.niche == "riff":
        await run_riff_job(job)
        return

    emit(job, StageEvent(
        job_id=job.job_id, ts=time.time(),
        stage="pull", status="start",
        message=f"Pulling story from {niche_cfg['label']}",
    ))

    # ---- Step 1: pull a story (and rewrite + cast via claude CLI) ----
    pull_args: list[str]
    if "subreddit" in niche_cfg:
        # Pull a candidate POOL (top-of-week, 15) instead of one
        # top-of-day post. The pool is filtered for visualizability,
        # de-duplicated against already-rendered post_ids, then
        # drama-scored — only the spiciest candidate gets rewritten
        # and rendered (`--pick best`). Without the pool, every click
        # within a 24h window re-pulls the same top-of-day story; with
        # it the user sees a fresh, high-conflict story each click.
        pull_args = [
            str(PYTHON_BIN), "pull_stories.py",
            "reddit",
            "--subreddit", niche_cfg["subreddit"],
            "--listing", "top",
            "--timeframe", "week",
            "--limit", "15",
            "--pick", "best",
        ]
    elif "wiki_page" in niche_cfg:
        pull_args = [
            str(PYTHON_BIN), "pull_stories.py",
            "wiki",
            "--page", niche_cfg["wiki_page"],
            "--limit", "1",
        ]
    else:
        pull_args = [
            str(PYTHON_BIN), "pull_stories.py", "tih", "--limit", "1",
        ]

    # Always route raw/scripts/cast to the niche's declared channel_dir.
    # Without this, niches that share a subreddit with vanilla AITA
    # (e.g. aita_cliffhanger) silently fall back to pull_stories.py's
    # default of `reddit_<subreddit>`, write into the wrong dir, and
    # then `_slug_from_intermediate(channel_dir)` returns None below
    # — surfacing as "no script produced".
    pull_args.extend(["--channel", niche_cfg["channel_dir"]])

    rc, tail = await run_subprocess(job, pull_args)
    if job.state == "cancelled":
        return
    if rc != 0:
        job.state = "error"
        job.error = _format_subprocess_failure("pull_stories.py", rc, tail)
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="error", status="error",
            message=job.error,
        ))
        return

    # Find the slug we just wrote.
    slug = _slug_from_intermediate(niche_cfg["channel_dir"])
    if not slug:
        job.state = "error"
        job.error = "no script produced"
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="error", status="error", message=job.error,
        ))
        return
    job.slug = slug
    # Note: pull/done with the slug was already emitted by pull_stories.py
    # (`[pull] raw written <slug>`). We don't re-emit pull/progress here
    # because rewrite and cast events have since fired and a "progress"
    # event would flip the pull card back to active in the UI.

    # Synthetic-done emit for the cache-reuse path: when pull_stories.py's
    # skip-seen guard drops every candidate (the user clicked again on a
    # niche where the top-of-week pool is fully exhausted into prior
    # renders), `_emit` returns early and never prints `[rewrite] done` /
    # `[cast] done`. The UI then shows those stage cards stuck on `·`
    # (pending) even though the pipeline is happily reusing the cached
    # script + cast files from a prior render. Detect that case here and
    # surface ✓ done events so the UI reflects reality. Same idea as the
    # `pull/done` event being emitted by the subprocess — we just synthesise
    # it for the stages that pull_stories.py skipped.
    chan_dir = PROJECT_ROOT / "data" / "intermediate" / niche_cfg["channel_dir"]
    rewrite_done_already = any(
        e.stage == "rewrite" and e.status == "done" for e in job.events
    )
    cast_done_already = any(
        e.stage == "cast" and e.status == "done" for e in job.events
    )
    script_path = chan_dir / "scripts" / f"{slug}.json"
    cast_path = chan_dir / "cast" / f"{slug}.json"
    if not rewrite_done_already and script_path.exists():
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="rewrite", status="done",
            message="Rewrite (cached from prior render)",
            data={"slug": slug, "cached": True},
        ))
    if not cast_done_already and cast_path.exists():
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="cast", status="done",
            message="Cast (cached from prior render)",
            data={"slug": slug, "cached": True},
        ))

    # If the user picked a YouTube-cloned voice, bind it to this slug now
    # by copying the cached ref into the channel's voices/<slug>.{wav,json}.
    # make_shorts._find_voice_path will discover it and force f5_tts.
    # If the user picked a Kokoro voice instead (no clone), unlink any
    # stale binding from a prior render so the picker isn't silently
    # overridden by leftover state.
    clone_id = (job.options or {}).get("voice_clone_id")
    voices_dir = PROJECT_ROOT / "data" / "intermediate" / niche_cfg["channel_dir"] / "voices"
    if not clone_id:
        for ext in ("wav", "json"):
            stale = voices_dir / f"{slug}.{ext}"
            if stale.exists():
                stale.unlink()
    if clone_id:
        src_dir = WEB_CLONES_DIR / clone_id
        if not (src_dir / "ref.wav").exists() or not (src_dir / "ref.json").exists():
            job.state = "error"
            job.error = f"voice clone {clone_id} not found"
            emit(job, StageEvent(
                job_id=job.job_id, ts=time.time(),
                stage="error", status="error", message=job.error,
            ))
            return
        voices_dir.mkdir(parents=True, exist_ok=True)
        dst_wav = voices_dir / f"{slug}.wav"
        dst_json = voices_dir / f"{slug}.json"
        import shutil
        shutil.copyfile(src_dir / "ref.wav", dst_wav)
        meta = json.loads((src_dir / "ref.json").read_text())
        # Rewrite ref_wav in the json to the new absolute path so make_shorts
        # finds the file regardless of which CWD it runs from.
        meta["ref_wav"] = str(dst_wav)
        meta["bound_clone_id"] = clone_id
        dst_json.write_text(json.dumps(meta, indent=2))
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="pull", status="progress",
            message=f"Bound cloned voice {clone_id} to {slug}",
            data={"voice_clone_id": clone_id},
        ))

    # ---- Step 2: render ----
    script_path = PROJECT_ROOT / niche_cfg["channel_dir"] / "narrations" / f"{slug}.json"
    render_args = [
        str(PYTHON_BIN), "scripts/make_shorts.py",
        "--script", str(script_path),
        "--channel", niche_cfg["channel"],
        # Stage 8 — every website-initiated render auto-uploads to YouTube.
        # The channel YAML's upload: block (account, privacy, tags, etc.)
        # decides where it goes. CLI-only renders are unaffected (the
        # channel YAMLs default to auto_upload: false).
        "--upload",
        # Auto-critique fires inline (principle #23). The Stage 7.5
        # critic walks every frame through the L1–L15 lens framework
        # and auto-regenerates weak beats before the mp4 is reported
        # done. Costs ~2-3min extra but lifts every Short and feeds
        # the system_corrections feedback loop.
    ]
    # Cloned voice wins; the kokoro voice picker only applies if no clone.
    voice = (job.options or {}).get("voice")
    if not clone_id and voice and voice in {v["id"] for v in VOICES}:
        render_args += ["--tts-voice", voice]
    rc, tail = await run_subprocess(job, render_args)
    if job.state == "cancelled":
        return
    if rc != 0:
        job.state = "error"
        job.error = _format_subprocess_failure("make_shorts.py", rc, tail)
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="error", status="error",
            message=job.error,
        ))
        return

    out_mp4 = SHORTS_DIR / f"{slug}.mp4"
    if not out_mp4.exists():
        job.state = "error"
        job.error = f"{out_mp4} not produced"
        emit(job, StageEvent(
            job_id=job.job_id, ts=time.time(),
            stage="error", status="error", message=job.error,
        ))
        return

    job.out_mp4 = out_mp4
    job.state = "done"

    # Try to load the authored prompts so the UI can show them.
    prompts_path = CACHE_DIR / slug / "prompts.json"
    if prompts_path.exists():
        try:
            job.beat_prompts = json.loads(prompts_path.read_text())
        except Exception:
            pass

    emit(job, StageEvent(
        job_id=job.job_id, ts=time.time(),
        stage="done", status="done",
        message="Short ready",
        data={"slug": slug, "mp4": f"/api/jobs/{job.job_id}/short"},
    ))


# ---- riff (YouTube imitation) job runner --------------------------------


def _emit_simple(job: Job, stage: str, status: str, message: str = "", **data) -> None:
    emit(job, StageEvent(
        job_id=job.job_id, ts=time.time(),
        stage=stage, status=status, message=message, data=data,
    ))


async def _riff_render_one_seed(
    job: Job,
    seed: RawStory,
    seed_idx: int,
    profile: dict,
) -> Path | None:
    """Run pull-equivalent (raw write + rewrite + cast) + make_shorts.py
    for one ideated seed. Returns the produced mp4 path, or None on
    failure (already emitted as an error event with seed_idx)."""
    channel_dir = profile.get("channel_dir") or "reddit_amitheasshole"
    channel_yaml = profile.get("channel_yaml") or "mystoriesanimated/variants/aita_animated.yaml"
    job.slug = seed.slug  # so /api/jobs/{id}/audio etc. work for the active seed

    inter_root = PROJECT_ROOT / "data" / "intermediate" / channel_dir
    raw_dest = inter_root / "raw"
    scripts_dest = inter_root / "scripts"
    cast_dest = inter_root / "cast"

    # 1. Save raw seed.
    _emit_simple(job, "pull", "start",
                 f"Seed {seed_idx+1}/{job.seed_total}: {seed.title[:60]}",
                 seed_idx=seed_idx, slug=seed.slug)
    save_raw(seed, raw_dest)
    _emit_simple(job, "pull", "done",
                 f"Wrote 1 raw, 1 scripts",
                 seed_idx=seed_idx, slug=seed.slug, raw=1, scripts=1)

    # 2. Rewrite + cast in-process so we don't have to re-pull from the source.
    cfg_path = PROJECT_ROOT / channel_yaml
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}

    from pipeline import cast as cast_mod, rewrite as rewrite_mod

    # Rewrite + cast are independent claude-CLI calls — run in parallel.
    _emit_simple(job, "rewrite", "start", "Rewriting narration",
                 seed_idx=seed_idx, slug=seed.slug)
    _emit_simple(job, "cast", "start", "Casting narrator",
                 seed_idx=seed_idx, slug=seed.slug)

    script_path = scripts_dest / f"{seed.slug}.json"
    cast_path = cast_dest / f"{seed.slug}.json"

    async def _rewrite_task():
        script = await asyncio.to_thread(
            rewrite_mod.rewrite, _dc_asdict(seed), cfg
        )
        rewrite_mod.save_script(script, script_path)

    async def _cast_task():
        await asyncio.to_thread(
            cast_mod.author_cast,
            raw_story=_dc_asdict(seed),
            channel_cfg=cfg,
            out_path=cast_path,
        )

    rewrite_res, cast_res = await asyncio.gather(
        _rewrite_task(), _cast_task(), return_exceptions=True,
    )
    if isinstance(rewrite_res, Exception):
        _emit_simple(job, "error", "error", f"rewrite failed: {rewrite_res}",
                     seed_idx=seed_idx)
        return None
    _emit_simple(job, "rewrite", "done", "Narration ready",
                 seed_idx=seed_idx, slug=seed.slug)
    if isinstance(cast_res, Exception):
        _emit_simple(job, "error", "error", f"cast failed: {cast_res}",
                     seed_idx=seed_idx)
        return None
    _emit_simple(job, "cast", "done", "Narrator cast",
                 seed_idx=seed_idx, slug=seed.slug)

    # 3. Render via make_shorts.py — same flow as the regular niches.
    # Auto-critique fires inline (principle #23 — never opt out).
    # Stage 8 auto-upload — every website render ships to YouTube.
    render_args = [
        str(PYTHON_BIN), "scripts/make_shorts.py",
        "--script", str(script_path),
        "--channel", channel_yaml,
        "--upload",
    ]
    voice = (job.options or {}).get("voice") or profile.get("suggested_voice")
    if voice and voice in {v["id"] for v in VOICES}:
        render_args += ["--tts-voice", voice]
    rc, tail = await run_subprocess(job, render_args, seed_idx=seed_idx)
    if job.state == "cancelled":
        return None
    if rc != 0:
        _emit_simple(job, "error", "error",
                     _format_subprocess_failure("make_shorts.py", rc, tail),
                     seed_idx=seed_idx)
        return None

    out_mp4 = SHORTS_DIR / f"{seed.slug}.mp4"
    if not out_mp4.exists():
        _emit_simple(job, "error", "error",
                     f"{out_mp4.name} not produced", seed_idx=seed_idx)
        return None

    # Hand the per-seed prompts to the UI (UI displays the latest active seed's).
    prompts_path = CACHE_DIR / seed.slug / "prompts.json"
    if prompts_path.exists():
        try:
            job.beat_prompts = json.loads(prompts_path.read_text())
        except Exception:
            pass

    return out_mp4


async def run_riff_job(job: Job) -> None:
    """Riff-on-a-YouTube-Short job lifecycle.

    Stages:
      - riff_analyze : transcript + frames → imitation profile
      - riff_ideate  : profile → N novel RawStory seeds
      - per-seed     : pull → rewrite → cast → tts → ... → compose
      - done         : list of N mp4 urls
    """
    from pipeline import imitate

    options = job.options or {}
    url = (options.get("url") or "").strip()
    n = int(options.get("n") or 3)
    n = max(1, min(5, n))
    job.seed_total = n

    if not url:
        job.state = "error"
        job.error = "missing url"
        _emit_simple(job, "error", "error", job.error)
        return

    # ---- 1. Analyze ------------------------------------------------
    _emit_simple(job, "riff_analyze", "start",
                 f"Analysing source video — transcript + 8 sample frames")
    try:
        profile = await asyncio.to_thread(imitate.analyze, url)
    except Exception as e:
        job.state = "error"
        job.error = f"analyze failed: {e}"
        _emit_simple(job, "error", "error", job.error)
        return
    job.profile = profile
    _emit_simple(job, "riff_analyze", "done",
                 f"Niche: {profile.get('niche_match')} · "
                 f"hook: {profile.get('hook_template','')[:60]}",
                 niche_match=profile.get("niche_match"),
                 hook_template=profile.get("hook_template"),
                 suggested_voice=profile.get("suggested_voice"),
                 length_target_s=profile.get("length_target_s"),
                 themes=profile.get("themes", []))

    # If the user pre-supplied a profile in options (UI confirm step), use it.
    user_profile = options.get("profile")
    if isinstance(user_profile, dict) and user_profile:
        # Carry over housekeeping fields the UI didn't set.
        for k in ("video_id", "title", "author", "url", "frames",
                  "transcript_chars", "channel_dir", "channel_yaml"):
            user_profile.setdefault(k, profile.get(k))
        # Re-derive channel from possibly-edited niche_match.
        nm = user_profile.get("niche_match")
        if nm in imitate.NICHE_CHANNEL:
            cd, cy = imitate.NICHE_CHANNEL[nm]
            user_profile["channel_dir"] = cd
            user_profile["channel_yaml"] = cy
        profile = user_profile
        job.profile = profile

    # ---- 2. Ideate -------------------------------------------------
    _emit_simple(job, "riff_ideate", "start",
                 f"Generating {n} fresh story seeds in this style")
    try:
        seeds = await asyncio.to_thread(imitate.ideate, profile, n=n)
    except Exception as e:
        job.state = "error"
        job.error = f"ideate failed: {e}"
        _emit_simple(job, "error", "error", job.error)
        return
    job.seeds = [_dc_asdict(s) for s in seeds]
    _emit_simple(job, "riff_ideate", "done",
                 f"Wrote {len(seeds)} seeds",
                 seeds=[{"idx": i, "slug": s.slug, "title": s.title,
                         "hook": s.body.split("\n\n", 1)[0][:200]}
                        for i, s in enumerate(seeds)])

    # ---- 3. Render each seed sequentially --------------------------
    for i, seed in enumerate(seeds):
        if job.state == "cancelled":
            return
        job.seed_idx = i
        out_mp4 = await _riff_render_one_seed(job, seed, i, profile)
        if job.state == "cancelled":
            return
        if out_mp4 is None:
            # Seed errored — note it but keep going (others may still render).
            continue
        job.out_mp4s[i] = str(out_mp4)
        job.out_mp4 = out_mp4  # last successful — keeps single-Short endpoints working
        _emit_simple(job, "compose", "done",
                     f"Short {i+1}/{n} ready",
                     seed_idx=i, slug=seed.slug,
                     mp4=f"/api/jobs/{job.job_id}/short/{i}")

    if not job.out_mp4s:
        job.state = "error"
        job.error = "no shorts produced (all seeds failed)"
        _emit_simple(job, "error", "error", job.error)
        return

    job.state = "done"
    _emit_simple(job, "done", "done",
                 f"{len(job.out_mp4s)} of {n} Shorts ready",
                 mp4s=[
                     {"seed_idx": i,
                      "slug": job.seeds[i]["slug"],
                      "title": job.seeds[i]["title"],
                      "mp4": f"/api/jobs/{job.job_id}/short/{i}"}
                     for i in sorted(job.out_mp4s.keys())
                 ])


# ---- in-process image worker (opt-in) ----------------------------------
#
# Default behavior: every job's make_shorts.py subprocess cold-loads its
# own diffusion pipe (~3-7 GB, 15-30 s on Apple Silicon). Once you go
# beyond a handful of jobs/week that adds up — and the IP-adapter
# reference cache I added in pipeline/images.py also resets per process.
#
# Opt-in via YTFACTORY_PERSIST_IMAGE_PIPE=1: the server warms a single
# pipe at startup and exposes /api/_internal/render. make_shorts.py then
# POSTs each image request instead of generating in-process. The GPU is
# single-tenant, so a threading.Lock-equivalent (asyncio.Lock here)
# serializes concurrent renders. Across-process this gives you exactly
# one cold load for the lifetime of the server.
#
# Why opt-in: the server now holds gigabytes of GPU state. If you don't
# want that (e.g. dev machines without enough VRAM, or you're iterating
# on the pipeline code itself), keep the default subprocess flow.

_IMAGE_WORKER_ENABLED = os.environ.get("YTFACTORY_PERSIST_IMAGE_PIPE", "").strip() in ("1", "true", "yes")
_IMAGE_WORKER_URL = os.environ.get(
    "YTFACTORY_IMAGE_WORKER_URL_OVERRIDE",
    "http://127.0.0.1:8765/api/_internal/render",
)
_IMAGE_WORKER_PROVIDER = os.environ.get("YTFACTORY_IMAGE_WORKER_PROVIDER", "sdxl_lightning")
_IMAGE_GPU_LOCK: asyncio.Lock | None = None  # set in lifespan when enabled


async def _warm_image_pipe() -> None:
    """Call once at startup to lazy-load the diffusion model into GPU.

    Runs in a thread because diffusers' weight load + MPS transfer is
    sync and would block the asyncio event loop for tens of seconds.
    """
    if not _IMAGE_WORKER_ENABLED:
        return
    print(f"[image-worker] warming pipe (provider={_IMAGE_WORKER_PROVIDER})…")
    t0 = time.time()
    try:
        # Lazy-import — pipeline.images pulls in torch+diffusers, which
        # is heavy. We don't want to pay that cost when the worker is
        # disabled (the default).
        from pipeline import images as _images
        await asyncio.to_thread(_images.warmup, _IMAGE_WORKER_PROVIDER)
        print(f"[image-worker] warm in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[image-worker] warmup failed: {e!r} — falling back to per-subprocess loading")


# ---- FastAPI ------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _rehydrate_jobs()
    if AUTH_TOKEN:
        print(f"[auth] YTFACTORY_TOKEN is set — protected mode (cookie/?token=)")
    else:
        print(
            f"[auth] YTFACTORY_TOKEN is NOT set — server is OPEN. "
            f"Localhost-only is safe; do NOT expose publicly without setting it."
        )
    if _IMAGE_WORKER_ENABLED:
        global _IMAGE_GPU_LOCK
        _IMAGE_GPU_LOCK = asyncio.Lock()
        # Run warmup in a fire-and-forget background task so the server
        # comes up immediately. Renders that arrive before warmup
        # finishes will block on the lock then go straight through.
        asyncio.create_task(_warm_image_pipe())
    watchdog = asyncio.create_task(_idle_watchdog())
    try:
        yield
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ytFactory", lifespan=lifespan)


@app.post("/api/_internal/render")
async def _internal_render(req: Request) -> dict:
    """Render one image inside the long-lived server process.

    Single-tenant GPU lock — concurrent jobs queue up. Loopback only
    by virtue of /api/_internal/* not being exposed beyond the
    existing AUTH_TOKEN gate; the env var that activates the worker
    is opt-in.
    """
    if not _IMAGE_WORKER_ENABLED:
        raise HTTPException(503, "image worker not enabled (set YTFACTORY_PERSIST_IMAGE_PIPE=1)")
    body = await req.json()
    kwargs = body.get("kwargs") or {}
    # `out_path` arrives as a string; cast back to Path for images.generate.
    if "out_path" in kwargs:
        kwargs["out_path"] = Path(kwargs["out_path"])
    if "ip_adapter_image" in kwargs and kwargs["ip_adapter_image"]:
        kwargs["ip_adapter_image"] = Path(kwargs["ip_adapter_image"])
    elif "ip_adapter_image" in kwargs:
        kwargs["ip_adapter_image"] = None

    assert _IMAGE_GPU_LOCK is not None
    async with _IMAGE_GPU_LOCK:
        from pipeline import images as _images
        t0 = time.time()
        result = await asyncio.to_thread(_images.generate, **kwargs)
        dt = time.time() - t0
    return {"path": str(result), "duration_s": round(dt, 3)}


def _check_auth(request: Request) -> None:
    """Reject if YTFACTORY_TOKEN is set and the request doesn't carry it.

    Accepts either:
      - cookie ``yt_tok=<token>`` (set by /auth?token=...)
      - query string ``?token=<token>``
      - header ``Authorization: Bearer <token>``
    """
    if AUTH_TOKEN is None:
        return
    cookie = request.cookies.get("yt_tok")
    if cookie and secrets.compare_digest(cookie, AUTH_TOKEN):
        return
    qtok = request.query_params.get("token")
    if qtok and secrets.compare_digest(qtok, AUTH_TOKEN):
        return
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        if secrets.compare_digest(auth.split(" ", 1)[1], AUTH_TOKEN):
            return
    raise HTTPException(401, "auth required")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Gate all routes except /auth and /healthz."""
    path = request.url.path
    if path in ("/auth", "/healthz") or path.startswith("/_static_unauth"):
        return await call_next(request)
    try:
        _check_auth(request)
    except HTTPException as e:
        # Friendly redirect for the home page.
        if path == "/":
            return RedirectResponse(url="/auth", status_code=302)
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    return await call_next(request)


app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "auth_required": AUTH_TOKEN is not None}


@app.get("/auth")
async def auth_page(token: str | None = None) -> Response:
    """Token entry. ``GET /auth?token=...`` sets the cookie and redirects /."""
    if AUTH_TOKEN is None:
        return RedirectResponse("/", status_code=302)
    if token and secrets.compare_digest(token, AUTH_TOKEN):
        resp = RedirectResponse("/", status_code=302)
        # 7 days; cookie scoped to root.
        resp.set_cookie(
            "yt_tok",
            AUTH_TOKEN,
            max_age=7 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=False,  # tunnel terminates TLS at cloudflared edge
        )
        return resp
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>ytFactory · auth</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 min-h-screen grid place-items-center font-sans">
<div class="max-w-md w-full p-6 bg-slate-900/60 border border-slate-800 rounded-xl">
<h1 class="text-xl font-bold mb-2">ytFactory</h1>
<p class="text-slate-400 text-sm mb-4">Paste your access token to enter.</p>
<form method="get" action="/auth" class="flex gap-2">
<input type="password" name="token" placeholder="Token" autofocus
  class="flex-1 bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm" />
<button class="px-4 py-2 bg-violet-600 hover:bg-violet-500 rounded text-sm font-medium">Enter</button>
</form></div></body></html>"""
    return Response(html, media_type="text/html")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")


@app.get("/dashboard")
async def dashboard() -> FileResponse:
    """Live YouTube analytics dashboard for every uploaded Short."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "dashboard.html")


@app.get("/api/dashboard/videos")
async def dashboard_videos(refresh: bool = False) -> dict:
    """Aggregate every uploaded video with cached YouTube analytics.

    Set ``?refresh=true`` to hit the YouTube API and refresh stats before
    returning. Without it, returns whatever's cached at
    ``data/research/analytics/<slug>.json``.
    """
    from pipeline import youtube_stats as _yt
    if refresh:
        try:
            _yt.fetch_all(quiet=True)
        except Exception as e:
            return {"error": f"refresh failed: {e}", "channels": []}

    by_channel: dict[str, list[dict]] = {}
    totals = {"videos": 0, "views": 0, "likes": 0, "comments": 0}
    latest_fetch: str | None = None

    for account, slug, vid, rec_path in _yt._enumerate_uploads():
        try:
            rec = json.loads(rec_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stats = _yt.load_for_slug(slug) or {}
        view = stats.get("view_count")
        like = stats.get("like_count")
        comment = stats.get("comment_count")
        fetched_at = stats.get("fetched_at")
        if fetched_at and (latest_fetch is None or fetched_at > latest_fetch):
            latest_fetch = fetched_at

        is_short = bool(rec.get("mp4_path", "").endswith(".mp4"))
        watch_url = (
            f"https://youtube.com/shorts/{vid}" if is_short else rec.get("url") or f"https://youtu.be/{vid}"
        )

        by_channel.setdefault(account, []).append({
            "slug": slug,
            "video_id": vid,
            "title": rec.get("title") or slug,
            "uploaded_at": rec.get("uploaded_at"),
            "privacy": rec.get("privacy"),
            "watch_url": watch_url,
            "studio_url": rec.get("studio_url") or f"https://studio.youtube.com/video/{vid}/edit",
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "stats": {
                "views": view,
                "likes": like,
                "comments": comment,
                "fetched_at": fetched_at,
            },
        })
        totals["videos"] += 1
        if view is not None: totals["views"] += view
        if like is not None: totals["likes"] += like
        if comment is not None: totals["comments"] += comment

    # Sort each channel by upload date desc.
    channels = []
    for account, vids in sorted(by_channel.items()):
        vids.sort(key=lambda v: v.get("uploaded_at") or "", reverse=True)
        c_views = sum(v["stats"]["views"] or 0 for v in vids)
        c_likes = sum(v["stats"]["likes"] or 0 for v in vids)
        c_comments = sum(v["stats"]["comments"] or 0 for v in vids)
        channels.append({
            "account": account,
            "video_count": len(vids),
            "totals": {"views": c_views, "likes": c_likes, "comments": c_comments},
            "videos": vids,
        })

    return {
        "channels": channels,
        "totals": totals,
        "latest_fetch": latest_fetch,
    }


@app.get("/api/niches")
async def list_niches() -> dict:
    """Niche options for the home page."""
    return {
        "niches": [
            {"key": k, **{kk: vv for kk, vv in v.items()
                          if kk not in {"channel", "channel_dir"}}}
            for k, v in NICHES.items()
        ]
    }


@app.get("/api/voices")
async def list_voices() -> dict:
    """Voice options + URL to a pre-recorded sample for each, plus the
    list of languages available for the language-tab UI.
    """
    return {
        "languages": LANGUAGES,
        "voices": [
            {**v, "sample_url": f"/api/voices/{v['id']}/sample.wav"}
            for v in VOICES
        ],
        "default": DEFAULT_VOICE,
    }


@app.get("/api/voices/{voice_id}/sample.wav")
async def voice_sample(voice_id: str) -> FileResponse:
    """Serve a 3-second preview clip for the chosen voice. Cached on disk.

    Sample text is matched to the voice's language so non-English voices
    don't read English with a thick TTS accent.
    """
    voice = next((v for v in VOICES if v["id"] == voice_id), None)
    if not voice:
        raise HTTPException(404, "unknown voice")
    VOICE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    sample_path = VOICE_SAMPLES_DIR / f"{voice_id}.wav"
    if not sample_path.exists():
        from pipeline import audio as audio_mod
        text = SAMPLE_TEXT_BY_LANG.get(voice["lang"], SAMPLE_TEXT_BY_LANG["en-us"])
        try:
            await asyncio.to_thread(
                audio_mod.synthesize,
                text,
                voice=voice_id,
                out_path=sample_path,
                speed=1.0,
                provider="kokoro",
            )
        except Exception as e:
            # If a particular lang isn't supported by this Kokoro build,
            # fall back to English text so the user still hears the voice.
            print(f"[voices] {voice_id} sample with lang={voice['lang']} failed: {e} — retrying with en-us text")
            await asyncio.to_thread(
                audio_mod.synthesize,
                SAMPLE_TEXT_BY_LANG["en-us"],
                voice=voice_id,
                out_path=sample_path,
                speed=1.0,
                provider="kokoro",
            )
    return FileResponse(str(sample_path), media_type="audio/wav")


# ---- Voice clones (YouTube → F5-TTS ref clip) ---------------------------
#
# Each clone lives at data/cache/voice_clones/web/<id>/{ref.wav, ref.json}
# and surfaces in the UI as another voice card. When a job is submitted
# with options.voice_clone_id, run_job copies the ref into
# data/intermediate/<channel>/voices/<slug>.{wav,json} so make_shorts
# auto-picks the f5_tts override (same path the per-story cast uses).

WEB_CLONES_DIR = PROJECT_ROOT / "data" / "cache" / "voice_clones" / "web"


def _list_web_clones() -> list[dict]:
    if not WEB_CLONES_DIR.exists():
        return []
    out: list[dict] = []
    for d in sorted(WEB_CLONES_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = d / "ref.json"
        wav = d / "ref.wav"
        if not (meta.exists() and wav.exists()):
            continue
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        out.append({
            "clone_id": d.name,
            "ref_text": m.get("ref_text", ""),
            "source_url": m.get("source_url", ""),
            "start": m.get("start"),
            "duration": m.get("duration"),
            "created_at": m.get("created_at"),
            "sample_url": f"/api/voice_clones/{d.name}/sample.wav",
        })
    return out


@app.get("/api/voice_clones")
async def list_voice_clones() -> dict:
    return {"clones": _list_web_clones()}


@app.post("/api/voice_clones")
async def create_voice_clone(payload: dict) -> dict:
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        start = float(payload.get("start", 0.0))
        duration = float(payload.get("duration", 10.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "start/duration must be numbers")
    if not (5.0 <= duration <= 15.0):
        raise HTTPException(400, "duration must be between 5 and 15 seconds")

    clone_id = uuid.uuid4().hex[:10]
    clone_dir = WEB_CLONES_DIR / clone_id
    clone_dir.mkdir(parents=True, exist_ok=True)
    ref_wav = clone_dir / "ref.wav"
    ref_json = clone_dir / "ref.json"

    from pipeline import voice_clone as vc_mod
    try:
        cloned = await asyncio.to_thread(
            vc_mod.clone_from_youtube,
            url,
            start=start,
            duration=duration,
            out_wav=ref_wav,
            out_json=ref_json,
        )
    except Exception as e:
        for p in (ref_wav, ref_json):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        raise HTTPException(400, f"clone failed: {e}")

    meta = json.loads(ref_json.read_text())
    meta["created_at"] = time.time()
    ref_json.write_text(json.dumps(meta, indent=2))

    return {
        "clone_id": clone_id,
        "ref_text": cloned.ref_text,
        "sample_url": f"/api/voice_clones/{clone_id}/sample.wav",
    }


@app.get("/api/voice_clones/{clone_id}/sample.wav")
async def voice_clone_sample(clone_id: str) -> FileResponse:
    p = WEB_CLONES_DIR / clone_id / "ref.wav"
    if not p.exists():
        raise HTTPException(404, "clone not found")
    return FileResponse(str(p), media_type="audio/wav")


@app.delete("/api/voice_clones/{clone_id}")
async def delete_voice_clone(clone_id: str) -> dict:
    d = WEB_CLONES_DIR / clone_id
    if not d.exists():
        raise HTTPException(404, "clone not found")
    for p in d.iterdir():
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass
    return {"ok": True}


async def _run_job_with_telemetry(job: Job) -> None:
    """Wrap run_job to record job-level lifecycle telemetry (start/end,
    end-to-end duration, final state). Stage-level rows are emitted by
    ``emit()``; this records ONE row per job so per-niche rollups stay
    accurate even when individual stages are cached/skipped.
    """
    t0 = time.time()
    tlm.track(
        "job_started",
        category="job",
        job_id=job.job_id,
        metadata={
            "niche": job.niche,
            "voice": (job.options or {}).get("voice"),
            "voice_clone_id": (job.options or {}).get("voice_clone_id"),
            "n": (job.options or {}).get("n"),
        },
    )
    try:
        await run_job(job)
    finally:
        tlm.track(
            "job_finished",
            category="job",
            success=(job.state == "done"),
            duration_ms=int((time.time() - t0) * 1000),
            job_id=job.job_id,
            metadata={
                "niche": job.niche,
                "state": job.state,
                "slug": job.slug,
                "error": (job.error or "")[:300] or None,
                "n_seeds_done": len(job.out_mp4s),
                "n_seeds_total": job.seed_total,
            },
        )


@app.post("/api/jobs")
async def create_job(payload: dict) -> dict:
    niche = payload.get("niche")
    if niche not in NICHES:
        raise HTTPException(400, f"unknown niche: {niche!r}")

    # Starting a new job means the user has moved on from anything that's
    # currently in-flight — kill it before we spin up another GPU/Flux
    # consumer. Without this, hitting "generate" twice in a row leaves
    # both pipelines fighting for ~10GB of resident memory.
    for jid, j in list(JOBS.items()):
        if j.state in ("queued", "running"):
            try:
                await cancel_job(jid, reason="superseded by new job")
            except Exception as e:
                print(f"[create_job] failed to cancel {jid}: {e}")

    job = Job(
        job_id=uuid.uuid4().hex[:10],
        niche=niche,
        options=payload.get("options") or {},
        created_at=time.time(),
    )
    JOBS[job.job_id] = job
    SUBSCRIBERS[job.job_id] = []
    task = asyncio.create_task(_run_job_with_telemetry(job))
    _runtime(job.job_id)["task"] = task
    return {"job_id": job.job_id}


@app.get("/api/jobs/{job_id}")
async def job_snapshot(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "job_id": job.job_id,
        "niche": job.niche,
        "state": job.state,
        "slug": job.slug,
        "error": job.error,
        "stage_started": job.stage_started,
        "stage_done": job.stage_done,
        "events": [asdict(e) for e in job.events[-200:]],
        "beat_prompts": job.beat_prompts,
        "mp4_url": (
            f"/api/jobs/{job.job_id}/short" if job.out_mp4 else None
        ),
        # Riff-mode extras (empty/zero for the regular niches).
        "profile": job.profile,
        "seeds": job.seeds,
        "seed_idx": job.seed_idx,
        "seed_total": job.seed_total,
        "mp4s": [
            {"seed_idx": i,
             "slug": (job.seeds[i].get("slug") if i < len(job.seeds) else None),
             "title": (job.seeds[i].get("title") if i < len(job.seeds) else None),
             "mp4": f"/api/jobs/{job.job_id}/short/{i}"}
            for i in sorted(job.out_mp4s.keys())
        ],
    }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> EventSourceResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    async def stream() -> AsyncIterator[dict]:
        # Replay the existing log first so a late subscriber catches up.
        for e in job.events:
            yield e.to_sse()

        if job.state in ("done", "error", "cancelled"):
            return

        # Then live-tail.
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        SUBSCRIBERS.setdefault(job_id, []).append(q)
        # New subscriber attached — clear any pending "idle" timer so the
        # watchdog gives this job the full grace period if/when this
        # subscriber later disappears.
        _runtime(job_id)["zero_subs_since"] = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # heartbeat
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield event.to_sse()
                if event.stage in ("done", "error"):
                    break
        finally:
            try:
                SUBSCRIBERS[job_id].remove(q)
            except ValueError:
                pass
            # If this was the last subscriber, start the idle countdown
            # — the watchdog will SIGTERM the pipeline if nobody re-attaches
            # within IDLE_GRACE_SEC.
            if not SUBSCRIBERS.get(job_id):
                _runtime(job_id)["zero_subs_since"] = time.time()

    return EventSourceResponse(stream())


@app.get("/api/jobs/{job_id}/short")
async def job_short(job_id: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job or not job.out_mp4 or not job.out_mp4.exists():
        raise HTTPException(404, "mp4 not ready")
    return FileResponse(
        str(job.out_mp4),
        media_type="video/mp4",
        filename=f"{job.slug}.mp4",
    )


@app.get("/api/jobs/{job_id}/short/{seed_idx}")
async def job_short_seed(job_id: str, seed_idx: int) -> FileResponse:
    """Riff-mode: per-seed mp4 (one of N rendered Shorts)."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    path_str = job.out_mp4s.get(seed_idx)
    if not path_str:
        raise HTTPException(404, "seed mp4 not ready")
    p = Path(path_str)
    if not p.exists():
        raise HTTPException(404, "seed mp4 file missing")
    return FileResponse(str(p), media_type="video/mp4", filename=p.name)


@app.get("/api/jobs/{job_id}/audio")
async def job_audio(job_id: str) -> FileResponse:
    """The TTS narration.wav for this job (available as soon as TTS done)."""
    job = JOBS.get(job_id)
    if not job or not job.slug:
        raise HTTPException(404, "no slug yet")
    p = CACHE_DIR / job.slug / "narration.wav"
    if not p.exists():
        raise HTTPException(404, "audio not ready")
    return FileResponse(str(p), media_type="audio/wav")


@app.get("/api/jobs/{job_id}/thumb/{i}")
async def job_thumb(job_id: str, i: int) -> FileResponse:
    job = JOBS.get(job_id)
    if not job or not job.slug:
        raise HTTPException(404, "no slug")
    img_path = CACHE_DIR / job.slug / f"img_{i:02d}.png"
    if not img_path.exists():
        raise HTTPException(404, "image not ready")
    return FileResponse(str(img_path), media_type="image/png")


# ---- telemetry --------------------------------------------------------
#
# Reads pipeline/telemetry's JSONL log on demand and computes the rollups
# the dashboard wants. No DB, no background aggregation: the file is
# small (a few MB at most for months of single-user usage) and pandas-
# free Python beats both the complexity and the latency.


@app.get("/api/telemetry/overview")
async def telemetry_overview(hours: int = 24) -> dict:
    """High-level totals + success rate + per-niche job counts."""
    since = time.time() - max(1, hours) * 3600
    events = tlm.read_events(since_ts=since)
    jobs_done = [e for e in events if e["event"] == "job_finished"]
    jobs_started = [e for e in events if e["event"] == "job_started"]
    llm_calls = [e for e in events if e["event"] == "llm_call"]
    stage_errors = [e for e in events if e["event"] == "stage_error"]

    success = sum(1 for e in jobs_done if e.get("success"))
    failure = len(jobs_done) - success
    durs = [e["duration_ms"] for e in jobs_done if e.get("duration_ms")]
    llm_in = sum((e.get("metadata") or {}).get("input_tokens") or 0 for e in llm_calls)
    llm_out = sum((e.get("metadata") or {}).get("output_tokens") or 0 for e in llm_calls)
    llm_cost = sum((e.get("metadata") or {}).get("cost_usd") or 0.0 for e in llm_calls)

    # Per-niche tallies.
    by_niche: dict[str, dict[str, int]] = {}
    for e in jobs_done:
        n = (e.get("metadata") or {}).get("niche") or "?"
        d = by_niche.setdefault(n, {"total": 0, "success": 0, "failed": 0,
                                    "avg_ms": 0, "_dur_sum": 0, "_dur_n": 0})
        d["total"] += 1
        if e.get("success"):
            d["success"] += 1
        else:
            d["failed"] += 1
        if e.get("duration_ms"):
            d["_dur_sum"] += e["duration_ms"]
            d["_dur_n"] += 1
    for d in by_niche.values():
        d["avg_ms"] = round(d["_dur_sum"] / d["_dur_n"]) if d["_dur_n"] else 0
        del d["_dur_sum"]
        del d["_dur_n"]

    return {
        "hours": hours,
        "jobs_started": len(jobs_started),
        "jobs_finished": len(jobs_done),
        "jobs_success": success,
        "jobs_failed": failure,
        "success_rate": round(success / len(jobs_done) * 100, 1) if jobs_done else None,
        "avg_job_ms": round(sum(durs) / len(durs)) if durs else 0,
        "p50_job_ms": round(tlm.percentile(durs, 0.5)),
        "p95_job_ms": round(tlm.percentile(durs, 0.95)),
        "max_job_ms": max(durs) if durs else 0,
        "stage_errors": len(stage_errors),
        "llm_calls": len(llm_calls),
        "llm_input_tokens": llm_in,
        "llm_output_tokens": llm_out,
        "llm_cost_usd": round(llm_cost, 4),
        "by_niche": by_niche,
    }


@app.get("/api/telemetry/stages")
async def telemetry_stages(hours: int = 24) -> dict:
    """Per-stage counts + avg/p50/p95/max duration. The headline view
    that answers 'how long does each pipeline step actually take'."""
    since = time.time() - max(1, hours) * 3600
    events = [e for e in tlm.read_events(since_ts=since)
              if e["event"] == "stage_done" and e.get("duration_ms") is not None]
    by_stage: dict[str, list[int]] = {}
    for e in events:
        stage = (e.get("metadata") or {}).get("stage") or "?"
        by_stage.setdefault(stage, []).append(e["duration_ms"])

    rows = []
    for stage, durs in sorted(by_stage.items(), key=lambda kv: -sum(kv[1])):
        rows.append({
            "stage": stage,
            "count": len(durs),
            "avg_ms": round(sum(durs) / len(durs)),
            "p50_ms": round(tlm.percentile(durs, 0.5)),
            "p95_ms": round(tlm.percentile(durs, 0.95)),
            "max_ms": max(durs),
            "total_ms": sum(durs),
        })
    return {"hours": hours, "stages": rows}


@app.get("/api/telemetry/timeline")
async def telemetry_timeline(hours: int = 24) -> dict:
    """Events bucketed by hour for a sparkline-style view."""
    since = time.time() - max(1, hours) * 3600
    events = tlm.read_events(since_ts=since)
    buckets: dict[int, dict[str, int]] = {}
    for e in events:
        # Bucket on the wall-clock hour.
        hr = int(e["ts"] // 3600 * 3600)
        b = buckets.setdefault(hr, {"jobs": 0, "errors": 0, "llm": 0})
        if e["event"] == "job_finished":
            b["jobs"] += 1
            if not e.get("success"):
                b["errors"] += 1
        elif e["event"] == "stage_error":
            b["errors"] += 1
        elif e["event"] == "llm_call":
            b["llm"] += 1
    return {
        "hours": hours,
        "buckets": [
            {"hour_ts": hr, **vals}
            for hr, vals in sorted(buckets.items())
        ],
    }


@app.get("/api/telemetry/llm")
async def telemetry_llm(hours: int = 24) -> dict:
    """Per-model LLM token + latency rollup."""
    since = time.time() - max(1, hours) * 3600
    events = [e for e in tlm.read_events(since_ts=since) if e["event"] == "llm_call"]
    by_model: dict[str, dict] = {}
    for e in events:
        meta = e.get("metadata") or {}
        m = meta.get("model") or "?"
        d = by_model.setdefault(m, {"calls": 0, "errors": 0,
                                    "input_tokens": 0, "output_tokens": 0,
                                    "cost_usd": 0.0, "_durs": []})
        d["calls"] += 1
        if not e.get("success"):
            d["errors"] += 1
        d["input_tokens"] += meta.get("input_tokens") or 0
        d["output_tokens"] += meta.get("output_tokens") or 0
        d["cost_usd"] += meta.get("cost_usd") or 0.0
        if e.get("duration_ms") is not None:
            d["_durs"].append(e["duration_ms"])
    rows = []
    for m, d in sorted(by_model.items(), key=lambda kv: -kv[1]["calls"]):
        durs = d.pop("_durs")
        rows.append({
            "model": m,
            **d,
            "cost_usd": round(d["cost_usd"], 4),
            "avg_ms": round(sum(durs) / len(durs)) if durs else 0,
            "p95_ms": round(tlm.percentile(durs, 0.95)),
        })
    return {"hours": hours, "models": rows, "total_calls": len(events)}


@app.get("/api/telemetry/errors")
async def telemetry_errors(hours: int = 24, limit: int = 50) -> dict:
    """Most recent failures across stages, jobs, and LLM calls."""
    since = time.time() - max(1, hours) * 3600
    events = tlm.read_events(since_ts=since)
    failed = [e for e in events
              if e["event"] in ("stage_error",) or e.get("success") is False]
    failed.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return {
        "hours": hours,
        "errors": [
            {
                "ts": e["ts"],
                "event": e["event"],
                "category": e.get("category"),
                "job_id": e.get("job_id"),
                "duration_ms": e.get("duration_ms"),
                "metadata": e.get("metadata") or {},
            }
            for e in failed[:limit]
        ],
    }


def _traceback_fingerprint(message: str) -> tuple[str, str]:
    """Reduce a Python traceback string to (fingerprint, headline).

    The fingerprint is the last non-empty line of the traceback (the
    actual exception type + message), trimmed. Two failures with the
    same fingerprint share a root cause even if their stacks differ in
    line numbers. ``headline`` is a longer human-readable version.
    """
    if not message:
        return ("(no message)", "(no message)")
    lines = [ln.rstrip() for ln in str(message).splitlines() if ln.strip()]
    if not lines:
        return ("(no message)", "(no message)")
    last = lines[-1].strip()
    # Strip line/file numbers off the fingerprint so eg.
    # `make_shorts.py:836` and `:824` collapse to the same group.
    fp = last
    for noise in ("0x[0-9a-fA-F]+", r"line \d+", r":\d+:"):
        import re
        fp = re.sub(noise, "", fp)
    fp = " ".join(fp.split())[:200]
    return (fp, last[:300])


@app.get("/api/telemetry/latency")
async def telemetry_latency(hours: int = 168) -> dict:
    """Engineering-grade latency rollup. Surfaces:

    - **hotspots**: stages ranked by cumulative wall time, with share %.
      The whole point: see at a glance which stage is the bottleneck.
    - **job_health**: per-niche success rate + p50/p90 wall time.
    - **image_retries**: % of image_attempt events that were retries
      (silent QC churn that doubles wall time per beat). Empty until
      Task #2 ships the image_attempt event.
    - **error_groups**: stage_errors bucketed by traceback fingerprint
      so 17 raw failures collapse into 2-3 root causes.
    - **slowest_recent_job**: timeline of the longest finished job —
      first place to look when something feels slow.

    Defaults to a 7-day window because successful jobs are sparse on
    a single-host setup and the 24h view is often empty.
    """
    since = time.time() - max(1, hours) * 3600
    events = tlm.read_events(since_ts=since)

    # ---- hotspots: stages by cumulative wall time ---------------------
    by_stage: dict[str, list[int]] = {}
    for e in events:
        if e["event"] != "stage_done" or e.get("duration_ms") is None:
            continue
        stage = (e.get("metadata") or {}).get("stage") or "?"
        by_stage.setdefault(stage, []).append(e["duration_ms"])
    total_stage_ms = sum(sum(v) for v in by_stage.values()) or 1
    hotspots = []
    for stage, durs in sorted(by_stage.items(), key=lambda kv: -sum(kv[1])):
        tot = sum(durs)
        hotspots.append({
            "stage": stage,
            "count": len(durs),
            "total_ms": tot,
            "share_pct": round(100 * tot / total_stage_ms, 1),
            "p50_ms": round(tlm.percentile(durs, 0.5)),
            "p90_ms": round(tlm.percentile(durs, 0.9)),
            "max_ms": max(durs),
        })

    # ---- job health: per-niche success + p50/p90 ----------------------
    jobs_started = [e for e in events if e["event"] == "job_started"]
    jobs_finished = [e for e in events if e["event"] == "job_finished"]
    by_niche_jobs: dict[str, dict] = {}
    for e in jobs_started:
        n = (e.get("metadata") or {}).get("niche") or "?"
        by_niche_jobs.setdefault(n, {"started": 0, "ok": 0, "failed": 0,
                                     "_durs": []})["started"] += 1
    for e in jobs_finished:
        n = (e.get("metadata") or {}).get("niche") or "?"
        d = by_niche_jobs.setdefault(n, {"started": 0, "ok": 0, "failed": 0,
                                         "_durs": []})
        if e.get("success"):
            d["ok"] += 1
        else:
            d["failed"] += 1
        if e.get("duration_ms"):
            d["_durs"].append(e["duration_ms"])
    job_health = []
    for n, d in sorted(by_niche_jobs.items(), key=lambda kv: -kv[1]["started"]):
        durs = d.pop("_durs")
        success_rate = (
            round(100 * d["ok"] / (d["ok"] + d["failed"]), 1)
            if (d["ok"] + d["failed"]) else None
        )
        job_health.append({
            "niche": n,
            **d,
            "success_rate_pct": success_rate,
            "p50_ms": round(tlm.percentile(durs, 0.5)) if durs else 0,
            "p90_ms": round(tlm.percentile(durs, 0.9)) if durs else 0,
            "max_ms": max(durs) if durs else 0,
        })

    # ---- image retry %: depends on image_attempt events ---------------
    attempts = [e for e in events if e["event"] == "image_attempt"]
    by_niche_attempts: dict[str, dict] = {}
    for e in attempts:
        md = e.get("metadata") or {}
        n = md.get("niche") or "?"
        d = by_niche_attempts.setdefault(n, {"attempts": 0, "retries": 0,
                                             "qc_fails": 0})
        d["attempts"] += 1
        if (md.get("attempt") or 1) > 1:
            d["retries"] += 1
        if md.get("qc") == "fail":
            d["qc_fails"] += 1
    image_retries = []
    for n, d in sorted(by_niche_attempts.items(), key=lambda kv: -kv[1]["attempts"]):
        image_retries.append({
            "niche": n,
            **d,
            "retry_pct": round(100 * d["retries"] / d["attempts"], 1) if d["attempts"] else 0,
        })

    # ---- error groups: bucket stage_errors by traceback fingerprint ---
    err_groups: dict[str, dict] = {}
    for e in events:
        if e["event"] != "stage_error":
            continue
        md = e.get("metadata") or {}
        msg = md.get("message") or md.get("error") or ""
        fp, headline = _traceback_fingerprint(msg)
        g = err_groups.setdefault(fp, {
            "fingerprint": fp,
            "count": 0,
            "headline": headline,
            "niches": set(),
            "stages": set(),
            "last_ts": 0,
            "last_job_id": None,
        })
        g["count"] += 1
        if md.get("niche"):
            g["niches"].add(md["niche"])
        if md.get("stage"):
            g["stages"].add(md["stage"])
        if e.get("ts", 0) > g["last_ts"]:
            g["last_ts"] = e["ts"]
            g["last_job_id"] = e.get("job_id")
    error_groups = sorted(
        ({**g, "niches": sorted(g["niches"]), "stages": sorted(g["stages"])}
         for g in err_groups.values()),
        key=lambda g: -g["count"],
    )

    # ---- slowest_recent_job: critical path of one bad apple -----------
    # Find the longest finished job and rebuild its stage timeline so
    # the user can see which beat / which call ate the budget.
    slowest = None
    if jobs_finished:
        ranked = [e for e in jobs_finished if e.get("duration_ms")]
        ranked.sort(key=lambda e: -e["duration_ms"])
        if ranked:
            target = ranked[0]
            jid = target.get("job_id")
            timeline = [
                {
                    "event": e["event"],
                    "ts": e["ts"],
                    "duration_ms": e.get("duration_ms"),
                    "metadata": e.get("metadata") or {},
                }
                for e in events
                if e.get("job_id") == jid and e["event"] in (
                    "stage_done", "stage_error", "llm_call",
                    "ffmpeg_compose", "footage_trim", "image_attempt",
                )
            ]
            timeline.sort(key=lambda x: x["ts"])
            slowest = {
                "job_id": jid,
                "niche": (target.get("metadata") or {}).get("niche"),
                "duration_ms": target["duration_ms"],
                "success": target.get("success"),
                "ts": target["ts"],
                "timeline": timeline,
            }

    return {
        "hours": hours,
        "total_stage_ms": total_stage_ms,
        "hotspots": hotspots,
        "job_health": job_health,
        "image_retries": image_retries,
        "error_groups": error_groups,
        "slowest_recent_job": slowest,
    }


# Convenience: serve the live raw cache too (read-only).
@app.get("/api/jobs/{job_id}/closer")
async def job_closer(job_id: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job or not job.slug:
        raise HTTPException(404, "no slug")
    p = CACHE_DIR / job.slug / "closer_panel.png"
    if not p.exists():
        raise HTTPException(404, "closer not ready")
    return FileResponse(str(p), media_type="image/png")


# ---- uploads (Stage 8) ------------------------------------------------
#
# Thin web wrapper around pipeline/upload.py. State is per-slug so an
# upload survives a job scrolling out of the in-memory JOBS dict.
# Long-running work (the actual HTTP POST to YouTube) runs in a thread
# so the FastAPI event loop stays responsive.

UPLOADS: dict[str, dict] = {}  # slug → {state, progress, video_url, error, started_at, finished_at}

# Custom thumbnails uploaded via the web form land here, keyed by slug.
# Cleared after a successful upload so subsequent re-renders can pick a
# fresh thumbnail.
CUSTOM_THUMBS: dict[str, Path] = {}


def _channel_yaml_for_slug(slug: str) -> tuple[Path, str] | None:
    """Find the channel YAML + channel_dir for a slug.

    Searches data/intermediate/<channel_dir>/scripts/<slug>.json, then
    matches the directory name back to channels/*.yaml via
    pipeline.niches.NICHE_CHANNEL.
    """
    base = PROJECT_ROOT / "data" / "intermediate"
    if not base.exists():
        return None
    found_dir: str | None = None
    for chan_dir in base.iterdir():
        if (chan_dir / "scripts" / f"{slug}.json").exists():
            found_dir = chan_dir.name
            break
    if not found_dir:
        return None
    # Reverse lookup channel_dir → channel YAML.
    for _niche, (cdir, yaml_path) in _niches.NICHE_CHANNEL.items():
        if cdir == found_dir:
            return PROJECT_ROOT / yaml_path, found_dir
    # NICHE_CHANNEL only covers niches with a UI card (sports_ranked,
    # aita, tih, …). Channels rendered via /make-script or /make-movie-short
    # land in data/intermediate/<channel_dir>/ without a niche entry —
    # so try channels/<channel_dir>.yaml directly before falling back.
    direct = PROJECT_ROOT / "channels" / f"{found_dir}.yaml"
    if direct.exists():
        return direct, found_dir
    chans = sorted((PROJECT_ROOT / "channels").glob("*.yaml"))
    if chans:
        return chans[0], found_dir
    return None


def _do_upload_blocking(
    slug: str,
    channel_yaml_path: Path,
    channel_dir: str,
    privacy: str | None,
    publish_at: str | None,
    title_override: str | None,
    description_override: str | None,
    tags_override: list[str] | None,
    thumbnail_path: Path | None,
    force: bool,
    skip_critic: bool = False,
    auto_thumbnail: bool | None = None,
    headline_override: str | None = None,
) -> None:
    """Runs in a worker thread; mutates UPLOADS[slug] as it progresses."""
    from pipeline import upload as up

    state = UPLOADS[slug]
    try:
        chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
        if "upload" not in chan_yaml:
            raise RuntimeError(
                f"{channel_yaml_path.name} has no `upload:` block. "
                f"Add one — see mystoriesanimated/config.yaml."
            )

        script_path = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "scripts" / f"{slug}.json"
        raw_path = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "raw" / f"{slug}.json"
        mp4_path = PROJECT_ROOT / "data" / "shorts" / f"{slug}.mp4"
        if not mp4_path.exists():
            raise RuntimeError(f"mp4 not found at {mp4_path}")
        script = json.loads(script_path.read_text()) if script_path.exists() else {"slug": slug}
        raw = None
        if raw_path.exists():
            try:
                raw = json.loads(raw_path.read_text())
            except json.JSONDecodeError:
                raw = None

        def cb(pct: float) -> None:
            state["progress"] = round(pct, 1)

        state["state"] = "uploading"
        record = up.upload_short(
            project_root=PROJECT_ROOT,
            channel_yaml=chan_yaml,
            channel_dir=channel_dir,
            slug=slug,
            mp4_path=mp4_path,
            script=script,
            raw=raw,
            privacy_override=privacy,
            publish_at=publish_at,
            title_override=title_override,
            description_override=description_override,
            tags_override=tags_override,
            thumbnail_path=thumbnail_path,
            auto_thumbnail=auto_thumbnail,
            headline_override=headline_override,
            force=force,
            skip_critic=skip_critic,
            progress_cb=cb,
        )
        state["state"] = "done"
        state["progress"] = 100.0
        state["video_id"] = record.get("video_id")
        state["video_url"] = record.get("url")
        state["studio_url"] = record.get("studio_url")
        state["finished_at"] = time.time()
    except Exception as e:
        state["state"] = "error"
        state["error"] = str(e)
        state["finished_at"] = time.time()


def _scene_thumbs_for(slug: str) -> list[dict]:
    """List the cached scene PNGs (img_NN.png) for a slug, in order.

    These are the per-beat images make_shorts.py wrote to data/cache/<slug>/.
    The user can pick one in the upload form to use as the YouTube thumbnail.
    """
    cache = CACHE_DIR / slug
    if not cache.exists():
        return []
    out: list[dict] = []
    for p in sorted(cache.glob("img_*.png")):
        # img_NN.png — extract the index for display.
        try:
            idx = int(p.stem.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        out.append({
            "index": idx,
            "filename": p.name,
            "url": f"/api/uploads/scene-thumb/{slug}/{idx}",
            "size": p.stat().st_size,
        })
    return out


@app.get("/api/uploads/preflight/{slug}")
async def upload_preflight(slug: str) -> dict:
    """Return everything the upload form needs to prefill itself.

    Title options come from script.title_options. The description is the
    rendered template (so the user sees the final string and can edit).
    Tags + privacy come from the channel YAML's upload: block.
    """
    from pipeline import upload as up

    mp4 = SHORTS_DIR / f"{slug}.mp4"
    if not mp4.exists():
        raise HTTPException(404, f"no rendered mp4 at {mp4}")

    info = _channel_yaml_for_slug(slug)
    if not info:
        raise HTTPException(
            400,
            f"could not auto-resolve channel for slug={slug!r}; ensure "
            f"data/intermediate/<channel_dir>/scripts/{slug}.json exists",
        )
    channel_yaml_path, channel_dir = info
    chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
    upload_cfg = dict(chan_yaml.get("upload") or {})
    if not upload_cfg:
        raise HTTPException(400, f"{channel_yaml_path.name} has no upload: block")

    script_path = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "scripts" / f"{slug}.json"
    raw_path = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "raw" / f"{slug}.json"
    script = json.loads(script_path.read_text()) if script_path.exists() else {"slug": slug}
    raw = None
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text())
        except json.JSONDecodeError:
            raw = None

    meta = up.derive_metadata(script=script, raw=raw, upload_cfg=upload_cfg)
    title_options = list(script.get("title_options") or [])
    if meta["title"] not in title_options:
        title_options.insert(0, meta["title"])

    existing = up.existing_upload(PROJECT_ROOT, channel_dir, slug)
    custom = CUSTOM_THUMBS.get(slug)

    # Auto-thumbnail preview: compose (or reuse) so the form can show it
    # and the user can pick "Auto-generated" without an extra round-trip.
    auto_thumb_url = None
    auto_headline = None
    try:
        from pipeline import thumbnails as thumb_mod
        cache_dir = CACHE_DIR / slug
        out_path = cache_dir / "auto_thumb.jpg"
        scene_paths = thumb_mod.list_scene_frames(cache_dir)
        scene_mtime = max((p.stat().st_mtime for p in scene_paths), default=0.0)
        need_compose = (
            scene_paths
            and (not out_path.exists() or out_path.stat().st_mtime < scene_mtime)
        )
        if need_compose:
            thumb_mod.auto_thumbnail(
                slug=slug, cache_dir=cache_dir, script=script,
                channel_yaml=chan_yaml, channel_dir=channel_dir,
                out_path=out_path,
            )
        if out_path.exists():
            auto_thumb_url = f"/api/uploads/auto-thumb/{slug}?t={int(out_path.stat().st_mtime)}"
        style = thumb_mod.style_from_channel(chan_yaml, channel_dir=channel_dir)
        auto_headline = thumb_mod.pick_headline(script=script, style=style)
    except Exception as e:
        print(f"[preflight] auto-thumbnail preview skipped: {e}")

    return {
        "slug": slug,
        "channel_yaml": str(channel_yaml_path.relative_to(PROJECT_ROOT)),
        "channel_dir": channel_dir,
        "channel_label": chan_yaml.get("name") or channel_yaml_path.stem,
        "account": upload_cfg.get("account") or "default",
        "title": meta["title"],
        "title_options": title_options[:10],
        "description": meta["description"],
        "tags": meta["tags"],
        "privacy": meta["privacy"],
        "category_id": meta["category_id"],
        "made_for_kids": meta["made_for_kids"],
        "scene_thumbs": _scene_thumbs_for(slug),
        "custom_thumb_present": custom is not None and custom.exists(),
        "auto_thumb_url": auto_thumb_url,
        "auto_headline": auto_headline,
        "existing_upload": existing,
    }


@app.get("/api/uploads/auto-thumb/{slug}")
async def upload_auto_thumb(slug: str) -> FileResponse:
    p = CACHE_DIR / slug / "auto_thumb.jpg"
    if not p.exists():
        raise HTTPException(404, "auto thumbnail not generated yet")
    return FileResponse(str(p), media_type="image/jpeg")


@app.post("/api/uploads/auto-thumb/{slug}/regenerate")
async def upload_auto_thumb_regenerate(slug: str, payload: dict | None = None) -> dict:
    """Recompose the auto-thumbnail with optional headline / scene / style overrides."""
    from pipeline import thumbnails as thumb_mod

    info = _channel_yaml_for_slug(slug)
    if not info:
        raise HTTPException(400, f"could not resolve channel for slug={slug!r}")
    channel_yaml_path, channel_dir = info
    chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
    script_path = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "scripts" / f"{slug}.json"
    script = json.loads(script_path.read_text()) if script_path.exists() else {"slug": slug}

    body = payload or {}
    cache_dir = CACHE_DIR / slug
    out_path = cache_dir / "auto_thumb.jpg"
    p = thumb_mod.auto_thumbnail(
        slug=slug, cache_dir=cache_dir, script=script,
        channel_yaml=chan_yaml, channel_dir=channel_dir,
        out_path=out_path,
        headline_override=body.get("headline"),
        scene_index_override=int(body["scene_index"]) if body.get("scene_index") is not None else None,
        style_override=body.get("style"),
    )
    if p is None:
        raise HTTPException(404, "no scene frames available for this slug")
    return {
        "slug": slug,
        "auto_thumb_url": f"/api/uploads/auto-thumb/{slug}?t={int(p.stat().st_mtime)}",
        "size": p.stat().st_size,
    }


@app.get("/api/uploads/scene-thumb/{slug}/{idx}")
async def upload_scene_thumb(slug: str, idx: int) -> FileResponse:
    p = CACHE_DIR / slug / f"img_{idx:02d}.png"
    if not p.exists():
        raise HTTPException(404, "scene image not found")
    return FileResponse(str(p), media_type="image/png")


@app.post("/api/uploads/custom-thumb/{slug}")
async def upload_custom_thumb(slug: str, request: Request) -> dict:
    """Receive a custom thumbnail file from the upload form.

    Stored under data/cache/<slug>/custom_thumb.<ext> and used by the
    next POST /api/uploads call for the same slug.
    """
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if len(body) > 2 * 1024 * 1024:
        raise HTTPException(413, "thumbnail must be ≤ 2 MB (YouTube limit)")
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg"}.get(ctype)
    if not ext:
        raise HTTPException(415, f"unsupported content-type {ctype!r}; use image/png or image/jpeg")
    cache = CACHE_DIR / slug
    cache.mkdir(parents=True, exist_ok=True)
    # Wipe any prior custom thumb for this slug so we don't leak stale files.
    for old in cache.glob("custom_thumb.*"):
        try:
            old.unlink()
        except OSError:
            pass
    p = cache / f"custom_thumb{ext}"
    p.write_bytes(body)
    CUSTOM_THUMBS[slug] = p
    return {"slug": slug, "size": len(body), "path": str(p.relative_to(PROJECT_ROOT))}


@app.post("/api/uploads")
async def create_upload(payload: dict) -> dict:
    slug = (payload.get("slug") or "").strip()
    if not slug:
        raise HTTPException(400, "slug required")

    mp4 = SHORTS_DIR / f"{slug}.mp4"
    if not mp4.exists():
        raise HTTPException(404, f"no rendered mp4 at {mp4}")

    explicit_channel = (payload.get("channel") or "").strip()
    if explicit_channel:
        channel_yaml_path = (PROJECT_ROOT / explicit_channel).resolve()
        if not channel_yaml_path.exists():
            raise HTTPException(400, f"channel YAML not found: {explicit_channel}")
        # Find channel_dir for record-keeping.
        info = _channel_yaml_for_slug(slug)
        channel_dir = info[1] if info else "_adhoc"
    else:
        info = _channel_yaml_for_slug(slug)
        if not info:
            raise HTTPException(
                400,
                f"could not auto-resolve channel for slug={slug!r}; "
                f"pass `channel: channels/<name>.yaml` explicitly",
            )
        channel_yaml_path, channel_dir = info

    privacy = payload.get("privacy")
    publish_at = payload.get("publish_at")
    title = payload.get("title")
    description = payload.get("description")
    force = bool(payload.get("force", False))

    # Tags can come as a list, or a comma-separated string from a plain input.
    raw_tags = payload.get("tags")
    tags_override: list[str] | None = None
    if isinstance(raw_tags, list):
        tags_override = [str(t).strip() for t in raw_tags if str(t).strip()]
    elif isinstance(raw_tags, str) and raw_tags.strip():
        tags_override = [t.strip() for t in raw_tags.split(",") if t.strip()]

    # Thumbnail choice:
    #   'auto'          → let upload_short compose one (default)
    #   'youtube'       → don't set any custom thumb; YouTube auto-picks
    #   'scene:<idx>'   → cached img_NN.png frame
    #   'custom'        → user-uploaded file via POST /api/uploads/custom-thumb
    thumb_choice = (payload.get("thumbnail") or "auto").strip()
    thumbnail_path: Path | None = None
    auto_thumbnail_flag: bool | None = None
    if thumb_choice.startswith("scene:"):
        try:
            idx = int(thumb_choice.split(":", 1)[1])
        except ValueError:
            raise HTTPException(400, f"invalid thumbnail choice {thumb_choice!r}")
        p = CACHE_DIR / slug / f"img_{idx:02d}.png"
        if not p.exists():
            raise HTTPException(404, f"scene image {p.name} not found")
        thumbnail_path = p
        auto_thumbnail_flag = False
    elif thumb_choice == "custom":
        p = CUSTOM_THUMBS.get(slug)
        if not p or not p.exists():
            raise HTTPException(
                400,
                "custom thumbnail not on the server. POST it to "
                "/api/uploads/custom-thumb/<slug> first.",
            )
        thumbnail_path = p
        auto_thumbnail_flag = False
    elif thumb_choice == "auto":
        # Compose-on-upload — let upload_short do it.
        auto_thumbnail_flag = True
    elif thumb_choice in ("youtube", "none"):
        # Skip our custom thumb path; YouTube will auto-derive a frame.
        auto_thumbnail_flag = False
    elif thumb_choice not in ("", None):
        raise HTTPException(400, f"unknown thumbnail choice {thumb_choice!r}")

    # Idempotency short-circuit, unless force.
    if not force:
        from pipeline import upload as up
        existing = up.existing_upload(PROJECT_ROOT, channel_dir, slug)
        if existing:
            UPLOADS[slug] = {
                "state": "done",
                "progress": 100.0,
                "video_id": existing.get("video_id"),
                "video_url": existing.get("url"),
                "studio_url": existing.get("studio_url"),
                "started_at": time.time(),
                "finished_at": time.time(),
                "cached": True,
            }
            return {"slug": slug, **UPLOADS[slug]}

    UPLOADS[slug] = {
        "state": "starting",
        "progress": 0.0,
        "started_at": time.time(),
        "finished_at": None,
    }
    skip_critic = bool(payload.get("skip_critic", False))
    headline_override = payload.get("headline") or None
    asyncio.create_task(asyncio.to_thread(
        _do_upload_blocking,
        slug, channel_yaml_path, channel_dir,
        privacy, publish_at, title, description,
        tags_override, thumbnail_path, force, skip_critic,
        auto_thumbnail_flag, headline_override,
    ))
    return {"slug": slug, "state": "starting"}


@app.get("/api/uploads/{slug}")
async def get_upload(slug: str) -> dict:
    # In-memory state takes precedence (fresher progress %).
    state = UPLOADS.get(slug)
    if state:
        return {"slug": slug, **state}
    # Fallback: read the on-disk record so a reload after upload still works.
    from pipeline import upload as up
    info = _channel_yaml_for_slug(slug)
    if info:
        _, channel_dir = info
        rec = up.existing_upload(PROJECT_ROOT, channel_dir, slug)
        if rec:
            return {
                "slug": slug,
                "state": "done",
                "progress": 100.0,
                "video_id": rec.get("video_id"),
                "video_url": rec.get("url"),
                "studio_url": rec.get("studio_url"),
                "cached": True,
            }
    return {"slug": slug, "state": "none"}


@app.get("/api/uploads")
async def list_uploads() -> dict:
    base = PROJECT_ROOT / "data" / "uploads"
    out: list[dict] = []
    if base.exists():
        for chan_dir in sorted(base.iterdir()):
            if not chan_dir.is_dir():
                continue
            for f in sorted(chan_dir.glob("*.json")):
                try:
                    rec = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({
                    "slug": rec.get("slug", f.stem),
                    "channel_dir": chan_dir.name,
                    "video_id": rec.get("video_id"),
                    "video_url": rec.get("url"),
                    "privacy": rec.get("privacy"),
                    "uploaded_at": rec.get("uploaded_at"),
                    "title": rec.get("title"),
                })
    return {"uploads": out}


# ---- Research index -----------------------------------------------------
#
# Cross-channel view of every rendered short, every channel YAML, and every
# durable learning (memory feedback files + critique class-of-bug findings).
# Backed by data/research/*.jsonl which pipeline.research rebuilds from
# filesystem state — see pipeline/research.py.

from pipeline import research as _research

RESEARCH_DIR = PROJECT_ROOT / "data" / "research"


def _ensure_research_built() -> None:
    """Auto-build on first hit so the UI works on a fresh checkout."""
    needed = ["videos.jsonl", "channels.jsonl", "learnings.jsonl"]
    if not all((RESEARCH_DIR / n).exists() for n in needed):
        _research.rebuild(quiet=True)


@app.get("/api/research/videos")
async def research_videos() -> dict:
    _ensure_research_built()
    return {"videos": _research.load_jsonl(RESEARCH_DIR / "videos.jsonl")}


@app.get("/api/research/channels")
async def research_channels() -> dict:
    _ensure_research_built()
    return {"channels": _research.load_jsonl(RESEARCH_DIR / "channels.jsonl")}


@app.get("/api/research/learnings")
async def research_learnings() -> dict:
    _ensure_research_built()
    return {"learnings": _research.load_jsonl(RESEARCH_DIR / "learnings.jsonl")}


@app.post("/api/research/rebuild")
async def research_rebuild(refresh_analytics: bool = Query(False)) -> dict:
    """Rebuild the index. ?refresh_analytics=1 also pulls fresh YouTube
    view/like/comment counts (slower, network-dependent)."""
    return _research.rebuild(quiet=True, refresh_analytics=refresh_analytics)
