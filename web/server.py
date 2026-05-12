"""FastAPI wrapper for the ytFactory pipeline (also the source for ytfactory-web).

**2026-05-09 — laptop nuclear cleanup.** The canonical instance of
this app runs as the ``ytfactory-web`` Cloud Run SERVICE in
asia-southeast1 (project ``ytfactory-prod-v2``). Running locally on
:8765 is for **dev iteration only**. Skills now POST to the cloud
service by default (see ``pipeline.cloud.skill_dispatch.WEBSITE_URL``); set
``YTFACTORY_WEBSITE_URL=http://localhost:8765`` to redirect to a
local dev instance.

Most heavy ML deps (torch, diffusers, mflux, mlx-whisper, kokoro)
were removed from the laptop venv 2026-05-09. The local web server
can still load + serve the UI but actual rendering must go through
the cloud render-worker JOB (``YTFACTORY_RENDER_BACKEND=cloudrun``).
Local mode (subprocess of make_shorts.py) will fail at the first GPU
import.

Endpoints (mirrored cloud-side):
    GET  /                          → niche picker HTML
    GET  /static/*                  → assets
    POST /api/jobs                  → start a job, returns {job_id}
    GET  /api/jobs/{id}             → job status snapshot
    GET  /api/jobs/{id}/events      → SSE stream of stage events
    GET  /api/jobs/{id}/short       → final mp4 (when done)
    GET  /api/jobs/{id}/thumb/{i}   → image thumbnail (img_NN.png)

Pipeline integration: every job spawns ``pull_stories.py`` and then
``make_shorts.py`` as subprocesses (re-using the existing CLI rather
than refactoring). We tail stdout, parse the structured prefixes
already emitted (``[1/4]``, ``[prompts]``,
``[3/4] mflux: generating N images``, ``[critic] score=...``), and
fan them out as SSE events to the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import time

# Load .env on startup so CLOUDRUN_*_URL, GOOGLE_CLOUD_PROJECT, OAuth
# config etc. are available without remembering to `set -a; source .env`
# every uvicorn invocation. Best-effort — silent if python-dotenv isn't
# installed (the website still runs, just without the env vars).
try:
    from dotenv import load_dotenv as _load_dotenv  # type: ignore
    from pathlib import Path as _PathBootstrap
    _ENV_FILE = _PathBootstrap(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.exists():
        _load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass
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

logger = logging.getLogger(__name__)


# ---- Auth ---------------------------------------------------------------
#
# Two-tier auth (2026-05-10):
#
# 1. **Browser users** — Sign in with Google via ``pipeline.auth``.
#    Email-keyed Firestore allowlist (auth_users/<email>) gates access:
#    approved → through, pending → /access-pending, denied → /login.
#    Implicit admin = email domain in YTFACTORY_ADMIN_DOMAINS (default
#    docx.co.in). Admins can approve/deny pending requests.
#
# 2. **M2M callers** (Cloud Scheduler, laptop agent, skills) —
#    ``Authorization: Bearer <YTFACTORY_AGENT_TOKEN>`` shared secret on
#    M2M-only path prefixes (/agent/*, /api/scheduler/*, /api/cron/*).
#
# Legacy single-token mode survives behind ``YTFACTORY_TOKEN``: when
# set with no AGENT_TOKEN/OAuth client configured, the old cookie/query
# token flow gates everything (used by laptop dev). Production sets
# YTFACTORY_AGENT_TOKEN + YTFACTORY_WEB_OAUTH_CLIENT and AUTH_TOKEN
# stays unset.
#
AUTH_TOKEN = os.environ.get("YTFACTORY_TOKEN", "").strip() or None
AGENT_TOKEN = os.environ.get("YTFACTORY_AGENT_TOKEN", "").strip() or None

# When set (Cloud Run env), `/` and `/dashboard` redirect here so the
# polished Next.js studio (web-next) is the canonical face. Locally
# unset → keep serving the legacy single-page web/static/index.html so
# dev / direct-API operators still have a landing page.
_PUBLIC_FRONTEND_URL = os.environ.get("YTFACTORY_PUBLIC_FRONTEND_URL", "").rstrip("/")

# Path prefixes that carry their own M2M auth — only checked against
# AGENT_TOKEN, never against the browser session cookie.
_M2M_PATH_PREFIXES: tuple[str, ...] = (
    "/agent/",
    "/api/scheduler/",
    "/api/cron/",
)

# Path prefixes that bypass auth entirely (sign-in flow + static assets
# the login page references).
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
    "/static/",
    "/_static_unauth/",
)

_PUBLIC_EXACT_PATHS: frozenset[str] = frozenset({
    # `/` + `/dashboard` redirect to web-next on Cloud Run via
    # YTFACTORY_PUBLIC_FRONTEND_URL — bypass auth so anonymous browsers
    # land on the polished login page (handled by web-next middleware)
    # instead of being 401'd by FastAPI.
    "/",
    "/dashboard",
    "/login",
    "/access-pending",
    "/healthz",
})


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

# ---- Website-native render-from-script jobs (2026-05-09) -----------------
#
# The original /api/jobs flow is niche-driven: pick a niche, the website
# runs pull_stories.py + make_shorts.py end-to-end. Skills (the AI
# authoring layer) need a different entry point: they hand-author the
# script JSON themselves and just want the website to drive the
# renderer in its already-correct env (PYTHONPATH, gcloud account, cwd).
# /api/jobs/from_script is that entry point. SCRIPT_JOBS tracks them
# separately from the niche-driven JOBS so their schemas don't collide.
#
# **Persistence (2026-05-10):** SCRIPT_JOBS is a ``ScriptJobsStore`` that
# behaves like a plain ``dict`` for tests / local dev (default
# ``YTFACTORY_QUEUE_BACKEND=memory``) and mirror-writes through to a
# Firestore ``script_jobs`` collection in prod
# (``YTFACTORY_QUEUE_BACKEND=firestore``). Without it, a Cloud Run
# revision rotation mid-render leaves the UI 404'ing on a job ID the
# running renderer is still chugging through. See
# ``web/script_jobs_store.py`` for the persistence policy.
from web.script_jobs_store import ScriptJobsStore

SCRIPT_JOBS: ScriptJobsStore = ScriptJobsStore(collection="script_jobs")
SCRIPT_JOB_LOG_DIR = Path("/tmp/ytfactory-script-jobs")

# Server-side critique state. Mirrors SCRIPT_JOBS shape.
# `mode` ∈ {"video", "audio"}; `verdict` is the parsed critique JSON.
CRITIQUE_JOBS: dict[str, dict[str, Any]] = {}
CRITIQUE_JOB_LOG_DIR = Path("/tmp/ytfactory-critique-jobs")

# Per-render Publish jobs. Mirrors SCRIPT_JOBS shape; one entry per
# upload attempt. Auto-falls-back-marker on YouTube API quotaExceeded.
UPLOAD_JOBS: dict[str, dict[str, Any]] = {}

# Channel cron-drain jobs. Wraps the existing per-channel upload-rotation
# scripts (scripts/upload_next.py for mystoriesanimated etc.) as
# website-driven endpoints so the schedule + status are visible alongside
# everything else.
CRON_JOBS: dict[str, dict[str, Any]] = {}
CRON_JOB_LOG_DIR = Path("/tmp/ytfactory-cron-jobs")


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


# ---- Periodic queue reaper -----------------------------------------------
#
# Returns stuck-LEASED tasks back to QUEUED so a crashed/orphaned agent
# can't pin a task forever. On 2026-05-12 we discovered 158 zombie LEASED
# burner_engage tasks accumulated over several days because the laptop
# agent's _ack used to send status="failed" instead of the schema's "error",
# silently 422-ing every failure ack and leaving the lease dangling. The
# ack bug is fixed in pipeline/laptop_agent.py, but agents can still crash
# mid-task — this reaper is the belt-and-braces guard.

_QUEUE_REAPER_INTERVAL_S = int(os.environ.get("YTFACTORY_QUEUE_REAPER_INTERVAL_S", "300"))


async def _periodic_queue_reaper() -> None:
    """Call ``q.reap_expired()`` every ``_QUEUE_REAPER_INTERVAL_S`` seconds.

    The reaper is idempotent — a no-op when no leases have expired. We run
    it from the FastAPI app (min-instances=1 in prod) instead of Cloud
    Scheduler so it doesn't depend on extra infrastructure.
    """
    # Lazy import — keeps tests that swap in the in-memory queue backend
    # from paying the firestore-client startup cost.
    from control.core.queue import get_queue  # noqa: PLC0415

    while True:
        try:
            await asyncio.sleep(_QUEUE_REAPER_INTERVAL_S)
            try:
                # reap_expired is sync and may hit Firestore — push it off
                # the event loop so a slow scan doesn't stall request
                # handling.
                n = await asyncio.to_thread(get_queue().reap_expired)
                if n:
                    print(f"[queue-reaper] reset {n} stuck-LEASED tasks back to QUEUED")
            except Exception as e:  # noqa: BLE001
                print(f"[queue-reaper] reap_expired failed: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            print(f"[queue-reaper] loop error: {e}")


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

    from pipeline.llm import cast as cast_mod, rewrite as rewrite_mod

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
    from pipeline.llm import imitate

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
        # Lazy-import — pipeline.images.images pulls in torch+diffusers, which
        # is heavy. We don't want to pay that cost when the worker is
        # disabled (the default).
        from pipeline.images import images as _images
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
    # Hydrate the persistent SCRIPT_JOBS store from Firestore (no-op when
    # YTFACTORY_QUEUE_BACKEND=memory). Done before any /api/jobs/*
    # endpoint can serve traffic so a freshly-rolled container immediately
    # surfaces in-flight renders submitted to the previous revision.
    n = await SCRIPT_JOBS.hydrate()
    if n:
        print(f"[script_jobs] hydrated {n} records from firestore")
    SCRIPT_JOBS.start_flush_task()
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
    # Periodic queue reaper. The laptop agent's /agent/ack used to send
    # status="failed" instead of the schema-required "error", which
    # silently 422-d and left tasks LEASED forever (158 zombies built up
    # by 2026-05-12). Even with that bug fixed, agents can crash mid-task
    # and leave a lease orphaned. Run reap_expired() every 5 min so
    # stuck-LEASED tasks return to QUEUED automatically.
    queue_reaper = asyncio.create_task(_periodic_queue_reaper())
    try:
        yield
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
        queue_reaper.cancel()
        try:
            await queue_reaper
        except asyncio.CancelledError:
            pass
        await SCRIPT_JOBS.stop_flush_task()


app = FastAPI(title="ytFactory", lifespan=lifespan)

# OTel — auto-instrument every route → an HTTP server span (named
# after the route template, e.g. ``POST /api/chat/confirm``). The
# OTel ASGI middleware injected here also reads incoming
# ``traceparent`` headers from the Next.js proxy so the chat →
# render-worker → cloud-TTS span tree links into one trace. Idempotent.
from pipeline import observability as _obs  # noqa: E402

_obs.instrument_fastapi(app)
_obs.instrument_outbound_http()
_obs.install_http_identity_middleware(app)


# ---------------------------------------------------------------------------
# Performance middleware (2026-05-10)
# ---------------------------------------------------------------------------
#
# Two responsibilities:
#
# 1. Add ``Server-Timing: total;dur=<ms>`` to every API response so we
#    can read per-endpoint backend cost straight out of the browser
#    DevTools "Network" → "Timing" tab without needing GCP logging.
#    Cheap (one timestamp diff) and unblocks ad-hoc perf debugging.
#
# 2. Set ``Cache-Control: public, max-age=N, stale-while-revalidate=M``
#    on the dashboard's polled read endpoints so a browser back/forward
#    or rapid tab-switch doesn't re-pay the round-trip. The dashboard
#    already polls for freshness; SWR makes the *paint* instant while
#    revalidation happens in the background.
#
#    POST/PUT/PATCH/DELETE always get ``no-store`` (they're mutations).
#    Auth endpoints + cookie-bearing redirects also get ``no-store``
#    so we don't accidentally cache a session-bound response.
#
# Order matters: this middleware sits OUTSIDE the route handlers so the
# Server-Timing total reflects everything, including any inner middleware.
import time as _perf_time  # noqa: PLC0415


# Endpoints that return idempotent dashboard data and are safe to cache
# briefly in the browser. The dashboard polls these every 10–30 s — SWR
# means the user sees their cached payload immediately while a fresh
# fetch runs in the background.
_CACHEABLE_GET_PREFIXES: tuple[str, ...] = (
    "/api/dashboard",
    "/api/channels",  # both /api/channels and /api/channels/{key}
    "/api/cloud/health",
    "/api/cloud/cost",
    "/api/cloud/deploys",
    "/api/cloud/services",
    "/api/queue",
    "/api/voices",
    "/api/niches",
)


@app.middleware("http")
async def _perf_headers_middleware(request, call_next):
    started = _perf_time.perf_counter()
    response = await call_next(request)
    dur_ms = (_perf_time.perf_counter() - started) * 1000.0
    # Append rather than replace so handlers can pre-populate sub-stage
    # timings (e.g. "gcs;dur=42, render;dur=18") and we tack the total on.
    existing = response.headers.get("Server-Timing")
    total = f"total;dur={dur_ms:.1f}"
    response.headers["Server-Timing"] = f"{existing}, {total}" if existing else total

    # Audit S1.23 — security headers.
    # Defence-in-depth headers on every response. CSP turns any
    # surviving XSS surface into a non-takeover (script-src 'self'
    # blocks the inline-script sink); X-Frame-Options blocks
    # clickjacking; HSTS pins HTTPS for the cookie-bearing window;
    # Referrer-Policy stops leaking dashboard URLs to off-host
    # resources; X-Content-Type-Options blocks MIME-sniff XSS.
    # Each header sets only when not already present so per-route
    # overrides win (e.g. a specific page that needs a stricter CSP
    # can set its own).
    response.headers.setdefault(
        "Content-Security-Policy",
        # 'unsafe-inline' on style is needed for the legacy
        # inline-styled dashboards; tighten when those move to a
        # stylesheet. 'self' for img + media covers our own GCS
        # signed URLs because they're proxied through this host;
        # blob: covers <video src=URL.createObjectURL(...)> in the
        # dashboard preview chip.
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "connect-src 'self' https://*.googleapis.com https://*.run.app; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self' https://accounts.google.com; "
        "object-src 'none'",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=()",
    )

    # Browser caching for dashboard-class GETs only.
    # Audit Q2.39 — guard with status_code < 400 so transient
    # auth blips don't get cached for 10s + stale-while-revalidate=60s
    # in the browser (the pre-fix mode left users staring at stuck
    # 401s after a re-login).
    if (
        request.method == "GET"
        and response.status_code < 400
        and any(request.url.path.startswith(p) for p in _CACHEABLE_GET_PREFIXES)
        and "Cache-Control" not in response.headers
    ):
        # 10 s fresh + 60 s stale-while-revalidate. Matches the dashboard
        # poll cadence so the SWR window soaks up tab-flip + back-button.
        # ``private`` (not ``public``): these endpoints are auth-gated.
        # Even with ``Vary: Cookie`` we don't want any intermediate
        # proxy / CDN holding a copy that another user could see by
        # accident. The browser HTTP cache is ours alone — that's where
        # SWR pays off without the cross-user risk.
        response.headers["Cache-Control"] = "private, max-age=10, stale-while-revalidate=60"
        # Vary on cookie so per-user dashboards never cross-pollinate
        # the browser cache (defensive — these endpoints don't currently
        # personalize but auth gating + future per-account scoping might).
        existing_vary = response.headers.get("Vary")
        response.headers["Vary"] = f"{existing_vary}, Cookie" if existing_vary else "Cookie"
    return response


# ---------------------------------------------------------------------------
# ytfactory-control absorption (Phase 4 — 2026-05-10)
# ---------------------------------------------------------------------------
#
# The legacy split between web/server.py (orchestrator/UI) and
# control/server_dev.py (scheduler/agent/state-API) is gone. Every
# control router is now mounted onto this app — ytfactory-web is the
# canonical Cloud Run service. The control/* package stays as a Python
# library; only its routers are imported here. control/server_dev.py
# remains usable for local-dev iteration of the control plane in
# isolation.
#
# Auth note: control routers carry their own auth (K_SERVICE trust on
# Cloud Run + Bearer YTFACTORY_AGENT_TOKEN off-cloud). web/server.py's
# AUTH_TOKEN middleware below is exempted on these route prefixes so
# the two auth models don't double up.
#
from control.routes.agent_routes import router as _control_agent_router
from control.routes.auth_pin import router as _control_auth_pin_router
from control.routes.burner_routes import router as _control_burner_router
from control.routes.channels_routes import router as _control_channels_router
from control.routes.cloud_routes import router as _control_cloud_router
from control.routes.clone_video_routes import router as _control_clone_video_router
from control.routes.critique_routes import router as _control_critique_router
from control.routes.dashboard_routes import router as _control_dashboard_router
from control.routes.discover_routes import router as _control_discover_router
from control.routes.music_routes import router as _control_music_router
from control.routes.niche_routes import router as _control_niche_router
from control.routes.niche_specs_routes import router as _control_niche_specs_router
from control.routes.oauth_web_routes import router as _control_oauth_web_router
from control.routes.render_routes import router as _control_render_router
from control.routes.scheduler_routes import router as _control_scheduler_router
from control.routes.script_jobs_routes import router as _control_script_jobs_router
from control.routes.state_routes import router as _control_state_router
from control.routes.telemetry_routes import router as _control_telemetry_router
from control.routes.voices_routes import router as _control_voices_router

# NOTE: app.include_router() calls for control routers happen at the
# BOTTOM of this file (search for "control router include block").
# That ordering means web's own @app.* decorators register FIRST, so
# any route paths that overlap (e.g. /api/jobs/from_script,
# /api/research/*, /api/dashboard/*, /api/voices/*) resolve to web's
# established handler — control's duplicate registrations become no-ops.
# Endpoints that are unique to control (/agent/*, /api/scheduler/*,
# /api/state/*, /api/channels, /api/niches/*, /api/burner*, etc.)
# attach cleanly with no collision.


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
        from pipeline.images import images as _images
        t0 = time.time()
        result = await asyncio.to_thread(_images.generate, **kwargs)
        dt = time.time() - t0
    return {"path": str(result), "duration_s": round(dt, 3)}


SESSION_COOKIE = "yt_session"
OAUTH_STATE_COOKIE = "yt_oauth_state"


def _bearer_matches(request: Request, expected: str | None) -> bool:
    if not expected:
        return False
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(auth.split(" ", 1)[1].strip(), expected)


def _legacy_token_ok(request: Request) -> bool:
    """Backwards-compat token mode (laptop dev). Active only when
    ``YTFACTORY_TOKEN`` is set."""
    if AUTH_TOKEN is None:
        return False
    cookie = request.cookies.get("yt_tok")
    if cookie and secrets.compare_digest(cookie, AUTH_TOKEN):
        return True
    qtok = request.query_params.get("token")
    if qtok and secrets.compare_digest(qtok, AUTH_TOKEN):
        return True
    return _bearer_matches(request, AUTH_TOKEN)


def _is_browser_request(request: Request) -> bool:
    """Heuristic: HTML accept header or no Accept = browser; JSON/* = API."""
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return True
    if not accept or accept == "*/*":
        # Treat unspecified as browser only for GET — POST without Accept is API.
        return request.method == "GET"
    return False


def _server_is_open() -> bool:
    """Fully-unconfigured = open mode (laptop dev + unit tests).

    Matches the original ``AUTH_TOKEN is None`` semantics: open when
    neither the legacy single-token nor the OAuth flow is configured.
    AGENT_TOKEN being set (test fixtures, M2M-only deploys) does NOT
    lock the door — that's M2M-flavor auth, distinct from user auth.

    Always locked on Cloud Run (``K_SERVICE`` is set by the runtime),
    so a production env without explicit auth config still blocks
    public traffic.
    """
    if os.environ.get("K_SERVICE"):
        return False
    return (
        not os.environ.get("YTFACTORY_TOKEN")
        and not os.environ.get("YTFACTORY_WEB_OAUTH_CLIENT")
        and not os.environ.get("YTFACTORY_WEB_OAUTH_CLIENT_PATH")
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Two-tier auth gate.

    Order:
      1. Public paths (sign-in flow, static, /healthz) — always pass.
      2. M2M paths (/agent/*, /api/scheduler/*, /api/cron/*) — require
         ``Authorization: Bearer <AGENT_TOKEN>`` if AGENT_TOKEN is set.
         When unset, fall through to legacy/session check (laptop dev).
      3. Legacy ``YTFACTORY_TOKEN`` mode — single shared token via
         cookie / query / Bearer. Skips OAuth entirely. Used by laptop dev.
      4. Default — Sign in with Google session cookie. Approved → through.
         Pending → /access-pending (HTML) or 403 (API). Anonymous → /login
         (HTML) or 401 (API).
    """
    path = request.url.path

    # 0. Open mode (dev / tests / fully-unconfigured)
    if _server_is_open():
        return await call_next(request)

    # 1. Public paths
    if path in _PUBLIC_EXACT_PATHS:
        return await call_next(request)
    if any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    # 2. M2M paths
    if any(path.startswith(p) for p in _M2M_PATH_PREFIXES):
        # Cloud Run runtime sets K_SERVICE. Google Frontend has already
        # validated the caller's OIDC token against the run.invoker
        # binding before the request reached us; the OIDC consumed the
        # Authorization header so we can't ALSO require an app-level
        # bearer here. Trust IAM. Mirrors the same K_SERVICE bypass in
        # control/core/auth.py:require_agent — without this the laptop
        # agent (which sends `gcloud print-identity-token`, not the
        # shared YTFACTORY_AGENT_TOKEN) is permanently 401'd against
        # cloud, and `/app/burner-channels` cross-engage never starts.
        if os.environ.get("K_SERVICE"):
            return await call_next(request)
        if AGENT_TOKEN is None:
            # No AGENT_TOKEN configured → fall through to (3)/(4) so laptop
            # dev still works.
            pass
        else:
            if _bearer_matches(request, AGENT_TOKEN):
                return await call_next(request)
            return JSONResponse({"error": "missing or invalid bearer"}, status_code=401)

    # 3. Legacy single-token mode (laptop dev convenience).
    if AUTH_TOKEN is not None:
        if _legacy_token_ok(request):
            return await call_next(request)
        if _is_browser_request(request) and path == "/":
            return RedirectResponse(url="/auth", status_code=302)
        return JSONResponse({"error": "auth required"}, status_code=401)

    # 4. Session-cookie path — Sign in with Google.
    from pipeline.auth import (  # noqa: PLC0415
        USER_STATUS_APPROVED,
        USER_STATUS_PENDING,
        get_user,
        verify_session,
    )

    # M2M Bearer is also accepted on browser endpoints (skills hitting
    # /api/jobs/* etc. with a token).
    if _bearer_matches(request, AGENT_TOKEN):
        return await call_next(request)

    email = verify_session(request.cookies.get(SESSION_COOKIE))
    if not email:
        if _is_browser_request(request):
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse({"error": "auth required"}, status_code=401)

    try:
        user = get_user(email)
    except Exception as e:  # Firestore offline / network blip
        logger.warning(f"auth: Firestore lookup failed for {email!r}: {e}")
        return JSONResponse({"error": "auth backend unavailable"}, status_code=503)

    status = (user or {}).get("status")
    if status == USER_STATUS_APPROVED:
        request.state.user_email = email
        request.state.user_is_admin = bool((user or {}).get("is_admin"))
        return await call_next(request)
    if status == USER_STATUS_PENDING:
        if _is_browser_request(request):
            return RedirectResponse(url="/access-pending", status_code=302)
        return JSONResponse(
            {"error": "access pending", "email": email}, status_code=403
        )
    # No record OR denied — bounce to /login (clears any stale cookie path).
    if _is_browser_request(request):
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE)
        return resp
    return JSONResponse({"error": "not authorized"}, status_code=403)


class _CachedStaticFiles(StaticFiles):
    """StaticFiles subclass that adds Cache-Control to every response.

    FastAPI's stock StaticFiles ships no Cache-Control header, so the
    browser revalidates every asset on every page load — measurable on
    the dashboard which references ~6 .css/.js + ~10 .png assets per
    pageview. We send the assets with a short max-age (5 min) so a deploy
    is reflected within a poll cycle but the FE doesn't pay a full
    revalidation round-trip on every navigation.
    """

    def __init__(self, *args, max_age: int = 300, **kwargs):
        super().__init__(*args, **kwargs)
        self._max_age = max_age

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if 200 <= resp.status_code < 400:
            # Don't override an explicit Cache-Control if one's already set
            # (some files we serve via FileResponse already set no-cache).
            resp.headers.setdefault(
                "Cache-Control", f"public, max-age={self._max_age}"
            )
        return resp


app.mount(
    "/static",
    _CachedStaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "session_auth": AGENT_TOKEN is not None or AUTH_TOKEN is None,
        "legacy_token": AUTH_TOKEN is not None,
    }


# ---------------------------------------------------------------------------
# Sign-in with Google + per-email allowlist (2026-05-10).
# ---------------------------------------------------------------------------


def _set_session_cookie(resp: Response, email: str) -> None:
    from pipeline.auth import sign_session  # noqa: PLC0415

    resp.set_cookie(
        SESSION_COOKIE,
        sign_session(email),
        max_age=int(os.environ.get("YTFACTORY_SESSION_TTL_S", str(7 * 24 * 3600))),
        httponly=True,
        samesite="lax",
        secure=os.environ.get("YTFACTORY_COOKIE_SECURE", "1") == "1",
        path="/",
    )


@app.get("/login")
async def login_page(request: Request) -> Response:
    """Sign-in page. Anonymous → renders the page. Already-authed approved
    user → 302 to /. Pending → 302 to /access-pending."""
    from pipeline.auth import (  # noqa: PLC0415
        USER_STATUS_APPROVED,
        USER_STATUS_PENDING,
        get_user,
        verify_session,
    )

    email = verify_session(request.cookies.get(SESSION_COOKIE))
    if email:
        try:
            user = get_user(email)
        except Exception:
            user = None
        status = (user or {}).get("status")
        if status == USER_STATUS_APPROVED:
            return RedirectResponse("/", status_code=302)
        if status == USER_STATUS_PENDING:
            return RedirectResponse("/access-pending", status_code=302)
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "auth_login.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/access-pending")
async def access_pending_page() -> Response:
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "auth_pending.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/admin")
async def admin_page(request: Request) -> Response:
    """Admin UI — list pending requests + approve/deny. Admin-only.

    Anonymous browser hits middleware first → redirected to /login.
    Approved-non-admin → 403 here (we don't expose the page).
    """
    if not getattr(request.state, "user_is_admin", False):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "auth_admin.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/auth/google/login")
async def google_login() -> Response:
    """Start the Google OAuth web flow."""
    from pipeline.auth import oauth_url  # noqa: PLC0415

    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(url=oauth_url(state), status_code=302)
    resp.set_cookie(
        OAUTH_STATE_COOKIE, state,
        max_age=600, httponly=True, samesite="lax",
        secure=os.environ.get("YTFACTORY_COOKIE_SECURE", "1") == "1",
        path="/api/auth/",
    )
    return resp


@app.get("/api/auth/google/callback")
async def google_callback(
    request: Request, code: str | None = None, state: str | None = None
) -> Response:
    """Receive Google's callback. Verify state, exchange code, upsert user,
    set session cookie, route based on status."""
    from pipeline.auth import (  # noqa: PLC0415
        USER_STATUS_APPROVED,
        USER_STATUS_PENDING,
        exchange_code,
        upsert_user,
    )

    if not code:
        return JSONResponse({"error": "missing code"}, status_code=400)
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not expected_state or not state or not secrets.compare_digest(state, expected_state):
        return JSONResponse({"error": "state mismatch"}, status_code=400)

    try:
        info = exchange_code(code)
    except Exception as e:
        logger.exception("OAuth callback failed")
        return JSONResponse({"error": f"OAuth failed: {e}"}, status_code=400)

    user = upsert_user(
        email=info["email"],
        name=info.get("name", ""),
        picture=info.get("picture", ""),
    )

    status = user.get("status")
    if status == USER_STATUS_APPROVED:
        target = "/"
    elif status == USER_STATUS_PENDING:
        target = "/access-pending"
    else:
        target = "/login?error=denied"

    resp = RedirectResponse(target, status_code=302)
    if status in (USER_STATUS_APPROVED, USER_STATUS_PENDING):
        _set_session_cookie(resp, info["email"])
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/")
    return resp


@app.get("/api/auth/whoami")
async def whoami(request: Request) -> dict:
    """Cheap session probe — used by the admin UI + frontend to render
    the right state."""
    from pipeline.auth import get_user, verify_session  # noqa: PLC0415

    email = verify_session(request.cookies.get(SESSION_COOKIE))
    if not email:
        return {"signed_in": False}
    try:
        user = get_user(email) or {}
    except Exception:
        user = {}
    return {
        "signed_in": True,
        "email": email,
        "status": user.get("status"),
        "is_admin": bool(user.get("is_admin")),
        "name": user.get("name") or "",
        "picture": user.get("picture") or "",
    }


@app.post("/api/auth/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# Admin endpoints — gated by middleware (must be approved + is_admin via
# request.state). We re-check is_admin here in case middleware ever changes.


def _require_admin(request: Request) -> None:
    if not getattr(request.state, "user_is_admin", False):
        raise HTTPException(status_code=403, detail="admin only")


@app.get("/api/admin/requests")
async def admin_list_requests(request: Request) -> dict:
    """List pending access requests (admin-only)."""
    _require_admin(request)
    from pipeline.auth import list_pending  # noqa: PLC0415

    return {"pending": list_pending()}


@app.get("/api/admin/users")
async def admin_list_users(request: Request) -> dict:
    """List ALL users (admin-only) — for the audit log on /admin."""
    _require_admin(request)
    from pipeline.auth import list_users  # noqa: PLC0415

    return {"users": list_users()}


@app.post("/api/admin/requests/{email}/approve")
async def admin_approve(request: Request, email: str) -> dict:
    _require_admin(request)
    from pipeline.auth import approve  # noqa: PLC0415

    actor = getattr(request.state, "user_email", "system")
    try:
        return {"ok": True, "user": approve(email, by=actor)}
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.post("/api/admin/requests/{email}/deny")
async def admin_deny(request: Request, email: str) -> dict:
    _require_admin(request)
    from pipeline.auth import deny  # noqa: PLC0415

    actor = getattr(request.state, "user_email", "system")
    try:
        return {"ok": True, "user": deny(email, by=actor)}
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/admin/token-health")
async def admin_token_health(request: Request) -> dict:
    """Per-account OAuth-token state for every YouTube channel (admin UI).

    Browser-facing variant of the same data the cron endpoint returns —
    gated by the existing admin middleware. See ``_compute_token_health``
    for the actual computation; ``/api/cron/token-health`` is the M2M
    sibling used by the daily Cloud Scheduler health probe (plan-D4).

    Born from the 2026-05-10 token-issue permanent fix (plan-D3).
    """
    _require_admin(request)
    return _compute_token_health()


@app.get("/api/cron/token-health")
async def cron_token_health() -> dict:
    """Same payload as ``/api/admin/token-health``, but auth-bypassed
    via the existing M2M middleware (``K_SERVICE``-trusted on Cloud Run,
    ``YTFACTORY_AGENT_TOKEN``-bearer on laptop). Called daily by the
    ``ytfactory-token-health-cron`` Cloud Scheduler (plan-D4); the cron
    posts to a Slack webhook / operator email when ``summary.broken > 0``."""
    return _compute_token_health()


def _compute_token_health() -> dict:
    """Aggregate OAuth-token state for every channel.

    Walks ``pipeline/channels/*.yaml`` for the canonical channel set,
    then asks ``pipeline.upload.upload.inspect_token_status`` where the
    active token sits (Cloud Run Secret Manager mount in cloud,
    ``~/.config/ytfactory/`` on laptop) and what state it's in
    (``ok`` / ``missing`` / ``no_refresh_token`` / ``missing_scopes`` /
    ``unreadable``).

    Response shape::

        {
          "fetched_at": "<iso>",
          "summary":    { "total": int, "ok": int, "broken": int },
          "accounts":   [
            { "account": str,
              "state":   "ok"|"missing"|"no_refresh_token"|"missing_scopes"|"unreadable",
              "expiry":  iso or null,
              "path":    str (token path or secret mount path),
              "source":  "secret_mount" | "config_dir" | "missing",
              "missing_scopes": [str, ...] | null,
              "error":   str | null
            }, ...
          ]
        }
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from pipeline.research.youtube import iter_channel_configs  # noqa: PLC0415
    from pipeline.upload.upload import (  # noqa: PLC0415
        inspect_token_status,
        _secret_mount_path,
    )

    accounts = sorted({a for a, _ in iter_channel_configs()})
    rows: list[dict] = []
    for acct in accounts:
        status = inspect_token_status(acct)
        path_str = status.get("path") or ""
        if path_str.startswith("/secrets/"):
            source = "secret_mount"
        elif status.get("state") == "missing":
            sp = _secret_mount_path(acct)
            source = "secret_mount" if str(sp) == path_str else "missing"
        else:
            source = "config_dir"
        rows.append({
            "account": acct,
            "state": status.get("state"),
            "expiry": status.get("expiry"),
            "path": path_str,
            "source": source,
            "missing_scopes": status.get("missing"),
            "error": status.get("error"),
        })

    ok_count = sum(1 for r in rows if r["state"] == "ok")
    return {
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "summary": {
            "total": len(rows),
            "ok": ok_count,
            "broken": len(rows) - ok_count,
        },
        "accounts": rows,
    }


# ---------------------------------------------------------------------------
# A4 — refresh-via-job: trigger the cloud stats-refresh JOB asynchronously
# ---------------------------------------------------------------------------
#
# Pre-A4, the legacy /api/dashboard/videos?refresh=true called
# pipeline.research.youtube.fetch_all() inside the request thread —
# 30-60s of synchronous YouTube API calls per channel, easily timed
# out by the FE. The FastAPI request also pinned an event-loop
# coroutine for the whole duration, blocking other dashboard polls.
#
# Now: this endpoint kicks off the ytfactory-stats-refresh Cloud Run
# JOB asynchronously and returns immediately with the execution name.
# The FE polls /api/dashboard/videos until ``latest_fetch`` advances
# past the refresh-trigger timestamp.

STATS_REFRESH_JOB_NAME = os.environ.get(
    "YTFACTORY_STATS_REFRESH_JOB", "ytfactory-stats-refresh"
)


@app.post("/api/dashboard/refresh-research-cache")
async def dashboard_refresh_research_cache(request: Request) -> dict:
    """Kick off the stats-refresh Cloud Run JOB asynchronously.

    Triggers ``ytfactory-stats-refresh`` (see ``cloud/stats-refresh/``)
    which writes a fresh per-account YT cache to
    ``gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json``.
    The dashboard's ``/api/dashboard/videos`` reader picks the new
    cache up automatically on its next poll.

    Returns immediately (``execution_name`` for log-trail; FE polls
    ``/api/dashboard/videos`` for ``latest_fetch`` to advance).
    Admin-only on the laptop / approved-domain on cloud — uses the
    existing ``_require_admin`` gate.
    """
    _require_admin(request)
    from datetime import datetime, timezone  # noqa: PLC0415

    triggered_at = datetime.now(tz=timezone.utc).isoformat()

    # On laptop dev (no gcloud / no project), short-circuit to the
    # in-process fetch. The cloud cron handles the recurring case;
    # this manual trigger is mostly for "I just uploaded a video,
    # show its stats now" workflows.
    if not os.environ.get("K_SERVICE"):
        try:
            from pipeline.research import youtube as _yt  # noqa: PLC0415

            summary = await asyncio.to_thread(_yt.fetch_all, quiet=True)
            return {
                "ok": True,
                "mode": "laptop_inline",
                "triggered_at": triggered_at,
                "summary": summary,
            }
        except Exception as exc:
            return {
                "ok": False,
                "mode": "laptop_inline",
                "triggered_at": triggered_at,
                "error": str(exc),
            }

    # Cloud path — trigger the JOB via gcloud (same SA already has
    # run.developer / run.invoker on its own project's JOBs).
    execute_cmd = [
        "gcloud", "run", "jobs", "execute", STATS_REFRESH_JOB_NAME,
        "--project", CLOUDRUN_JOB_PROJECT,
        "--region", CLOUDRUN_JOB_REGION,
        "--async",
        "--format", "value(metadata.name)",
    ]
    proc = await asyncio.create_subprocess_exec(
        *execute_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(
            500,
            f"stats-refresh JOB execute failed: "
            f"{stderr.decode(errors='replace')[:500]}",
        )
    return {
        "ok": True,
        "mode": "cloud_job",
        "triggered_at": triggered_at,
        "execution_name": stdout.decode().strip(),
        "job_name": STATS_REFRESH_JOB_NAME,
    }


@app.get("/")
async def home(request: Request):
    """The new product home (creator-studio feel). The old niche-picker
    POST-/api/jobs flow lives at /legacy for power users.

    On Cloud Run the web-next service (Next.js) is the canonical public
    face. If `YTFACTORY_PUBLIC_FRONTEND_URL` is set, redirect there so
    operators who bookmarked the FastAPI URL still land on the polished
    UI. Locally (env unset), keep serving the legacy `web/static/index.html`
    so dev / tests / direct-API operators still have a landing page.
    """
    if _PUBLIC_FRONTEND_URL:
        return RedirectResponse(url=_PUBLIC_FRONTEND_URL + "/", status_code=302)
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/legacy")
async def legacy_home() -> FileResponse:
    """The original niche-picker UI — POSTs to /api/jobs (the niche-driven
    flow). Kept for power users who want direct access to the niche
    pipeline without going through the new product UI."""
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "legacy.html",
        headers={"Cache-Control": "no-cache"},
    )


# Audit S1.19 — there used to be a duplicate ``@app.get("/admin")``
# here that served renders.html (the operator/engineer dashboard)
# WITH NO AUTH CHECK. Starlette resolves duplicate routes in
# registration order — the first one (line ~2007 above, admin-only)
# always won, but that meant the duplicate was dead code one
# refactor away from accidentally exposing the page unauthenticated.
# The duplicate has been removed; operators reach the renders
# dashboard via the canonical ``/renders`` route (line ~2580 below)
# which is itself fenced via the auth_middleware allow-list.


# ---- /renders dashboard data (P1.1 — 2026-05-09) -------------------------
#
# Single endpoint that powers the redesigned /renders page. Aggregates
# channel rosters, cron-drain channel set, in-flight + recent jobs across
# render/critique/publish/cron, and a best-effort service-health probe.
# One JSON round-trip → fast first paint.


_RENDERS_TRACKED_CHANNELS: tuple[str, ...] = (
    "mystoriesanimated", "cosmosdecoded", "historyrecapped", "hindutavaanimated",
    "sportsrecapped", "scrollpulse", "rhymetimejunction",
)


def _read_job_file(jf: Path) -> dict | None:
    """Cached parse of one job JSON file. Mtime-keyed, no TTL —
    historical job records never change once finished, and active jobs
    move their mtime on every state update so the cache self-busts.
    """
    try:
        st = jf.stat()
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    cached = _JOB_FILE_CACHE.get(jf)
    if cached is not None and (cached[0], cached[1]) == key:
        return cached[2]
    try:
        blob = json.loads(jf.read_text())
    except Exception:
        return None
    _JOB_FILE_CACHE[jf] = (st.st_mtime, st.st_size, blob)
    return blob


_JOB_FILE_CACHE: dict[Path, tuple[float, int, dict]] = {}


def _count_files(p: Path, glob: str = "*.json") -> int:
    if not p.is_dir():
        return 0
    return sum(1 for _ in p.rglob(glob))


# ---- channel-scan cache ------------------------------------------------
#
# `/api/dashboard` and `/api/overview` both rglob every channel folder
# for `uploads/*.json` + `_holds.json` on every poll (FE polls every
# ~10 s × N tabs). On a host with 7 channels × ~50 uploads each that's
# ~350 small JSON parses per poll, sync, on the asyncio event loop.
#
# Cache strategy: 30 s TTL with an mtime fast-path. If the channel
# folder's mtime hasn't moved AND the cached entry is younger than
# TTL, return cached. The mtime check lets the cache *self-invalidate*
# the moment a new upload lands — no manual bust needed.
_CHAN_SCAN_TTL_S = 30.0
_CHAN_SCAN_CACHE: dict[Path, tuple[float, float, dict]] = {}
# (cached_at_ts, watched_mtime, payload)
_CHAN_SCAN_LOCK = __import__("threading").Lock()


def _channel_scan(chan_root: Path) -> dict:
    """Return {uploads: [(path, parsed)], rendered: [Path], holds: dict}.

    Mtime-checked + 30 s TTL. Safe to call from multiple async routes
    on every request — collapses to a directory stat + cache hit when
    nothing has changed.
    """
    try:
        chan_mtime = chan_root.stat().st_mtime
    except OSError:
        return {"uploads": [], "rendered": [], "holds": {}}
    now = time.time()
    with _CHAN_SCAN_LOCK:
        cached = _CHAN_SCAN_CACHE.get(chan_root)
    if cached is not None:
        cached_at, cached_mtime, payload = cached
        if cached_mtime == chan_mtime and (now - cached_at) < _CHAN_SCAN_TTL_S:
            return payload
    # Cold path: walk the channel.
    uploads: list[tuple[Path, dict]] = []
    for u in chan_root.rglob("uploads/*.json"):
        if u.name.endswith(".x.json"):
            continue
        try:
            uploads.append((u, json.loads(u.read_text())))
        except Exception:
            continue
    rendered: list[Path] = (
        list(chan_root.rglob("shorts/*.mp4"))
        + list(chan_root.rglob("long_form/*.mp4"))
    )
    holds: dict = {}
    holds_file = chan_root / "_holds.json"
    if holds_file.exists():
        try:
            holds = json.loads(holds_file.read_text()) or {}
        except Exception:
            holds = {}
    payload = {"uploads": uploads, "rendered": rendered, "holds": holds}
    with _CHAN_SCAN_LOCK:
        _CHAN_SCAN_CACHE[chan_root] = (now, chan_mtime, payload)
    return payload


def _channel_summary(channel: str) -> dict:
    chan_root = PROJECT_ROOT / channel
    if not chan_root.is_dir() or not (chan_root / "config.yaml").exists():
        return {"channel": channel, "exists": False}
    scan = _channel_scan(chan_root)
    uploads = scan["uploads"]
    rendered = scan["rendered"]
    last_upload_iso: str | None = None
    for _, d in uploads:
        ts = d.get("uploaded_at") or d.get("publish_at")
        if ts and (last_upload_iso is None or ts > last_upload_iso):
            last_upload_iso = ts
    queue_depth = max(0, len(rendered) - len(uploads))
    return {
        "channel": channel,
        "exists": True,
        "uploaded_count": len(uploads),
        "rendered_count": len(rendered),
        "queue_depth": queue_depth,
        "last_upload_iso": last_upload_iso,
        "cron_drain_supported": channel in _CRON_DRAIN_SCRIPTS,
    }


@app.get("/api/overview")
async def overview() -> dict:
    """Aggregate dashboard payload for /renders. Single round-trip."""
    channels = [_channel_summary(c) for c in _RENDERS_TRACKED_CHANNELS]
    totals = {
        "uploaded": sum(c.get("uploaded_count", 0) for c in channels),
        "rendered": sum(c.get("rendered_count", 0) for c in channels),
        "queue_depth": sum(c.get("queue_depth", 0) for c in channels),
        "channels_count": sum(1 for c in channels if c.get("exists")),
    }
    # Recent in-flight + completed render jobs
    script_jobs = sorted(
        SCRIPT_JOBS.values(), key=lambda r: r.get("started_at", 0), reverse=True
    )[:8]
    in_flight = sum(1 for r in SCRIPT_JOBS.values() if r.get("state") == "running")
    upload_jobs = sorted(
        UPLOAD_JOBS.values(), key=lambda r: r.get("started_at", 0), reverse=True
    )[:5]
    critique_jobs = sorted(
        CRITIQUE_JOBS.values(), key=lambda r: r.get("started_at", 0), reverse=True
    )[:5]
    cron_jobs = sorted(
        CRON_JOBS.values(), key=lambda r: r.get("started_at", 0), reverse=True
    )[:5]
    # Cloud Run service health — best-effort (no Cloud Run access from website
    # process required if env vars set; otherwise skip).
    services = []
    for env_key, label in (
        ("CLOUDRUN_TTS_CHATTERBOX_URL", "chatterbox"),
        ("CLOUDRUN_TTS_INDICF5_URL", "indicf5"),
        ("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL", "flux2-klein"),
    ):
        url = os.environ.get(env_key)
        services.append({
            "label": label,
            "configured": bool(url),
            "url": url or "",
        })
    return {
        "channels": channels,
        "totals": totals,
        "in_flight": in_flight,
        "recent_renders": [
            _script_job_view(r) for r in script_jobs
        ],
        "recent_uploads": upload_jobs,
        "recent_critiques": [
            {k: v for k, v in r.items() if k not in ("log_path",)}
            for r in critique_jobs
        ],
        "recent_crons": [
            {k: v for k, v in r.items() if k not in ("log_path",)}
            for r in cron_jobs
        ],
        "services": services,
    }


@app.get("/renders")
async def renders_page() -> FileResponse:
    """Render-list + preview UI. Lists JOBS (niche-driven) and SCRIPT_JOBS
    (skill-driven, the website-native dispatch path). Auto-refreshes every 5s."""
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "renders.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/dashboard")
async def dashboard():
    """Live YouTube analytics dashboard for every uploaded Short.

    Cloud: redirect to the polished `/app` route on the Next.js service
    when YTFACTORY_PUBLIC_FRONTEND_URL is set. Local dev: serve the
    legacy single-file dashboard.html so operators without web-next can
    still see numbers.
    """
    if _PUBLIC_FRONTEND_URL:
        return RedirectResponse(url=_PUBLIC_FRONTEND_URL + "/app", status_code=302)
    # no-cache forces the browser to revalidate via ETag/304 every load,
    # so dashboard.html updates take effect on a normal reload instead of
    # requiring ⌘⇧R after every deploy. The 304 path stays bandwidth-cheap.
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "dashboard.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/dashboard")
async def dashboard_summary() -> dict:
    """Aggregate read for the studio dashboard tile/card layer.

    Schema mirrors `web-next/lib/types.ts::DashboardData`:

    - ``renders_7d`` / ``uploads_7d`` — counts of jobs / uploads completed
      in the trailing 7 days (from the persisted job + upload registries).
    - ``queued`` — currently active jobs (state in {pending, running}).
    - ``held`` — total slugs across all per-channel `_holds.json` files.
    - ``recent_jobs`` — last 6 jobs (any state), newest first.
    - ``top_performer`` — highest-views uploaded video across all channels
      (read from the cached YouTube analytics, no live API hit).

    Cheap to compute (file reads + a single in-memory scan); meant to be
    polled every ~10s by the dashboard page. No `?refresh` mode here —
    the FE refreshes YouTube stats via /api/dashboard/videos?refresh=true
    explicitly.

    The disk-walking work runs in `asyncio.to_thread` so a slow scan
    doesn't block other in-flight HTTP requests on the event loop. The
    underlying `_channel_scan` + `_read_job_file` helpers are mtime-
    cached so re-polls hit memory instead of disk.
    """
    base = await asyncio.to_thread(_dashboard_summary_sync)

    # --- Top performer ----------------------------------------------------
    # Compute directly from the cached uploads + cached stats instead of
    # calling the full `/api/dashboard/videos` handler. The full handler
    # builds a per-channel grouped payload (titles, totals, subscribers,
    # latest_fetch, ...) just so we can pull one (title, thumb, views)
    # tuple. Cuts ~30 lines of per-video work out of the dashboard hot
    # path; both helpers it now uses are O(1) cached after the first call.
    top_performer: dict | None = None
    try:
        from control.routes.dashboard_routes import (  # noqa: PLC0415
            _enumerate_uploads as _enum_uploads,
            _ensure_fresh as _ensure_fresh_stats,
            _STATS_CACHE as _stats_cache,
        )

        uploads = _enum_uploads()  # cached list[(account, slug, vid, rec)]
        if uploads:
            # Refresh any stats older than the 10-min TTL — cheap when warm.
            _ensure_fresh_stats([vid for _, _, vid, _ in uploads])
            best: tuple[int, dict, str, str] | None = None  # (views, rec, account, vid)
            for account, slug, vid, rec in uploads:
                views = (_stats_cache.get(vid) or {}).get("viewCount") or 0
                if not isinstance(views, int):
                    try:
                        views = int(views)
                    except (TypeError, ValueError):
                        continue
                if views <= 0:
                    continue
                if best is None or views > best[0]:
                    best = (views, rec, account, vid)
            if best:
                views, rec, account, vid = best
                is_short = bool(str(rec.get("mp4_path") or "").endswith(".mp4"))
                watch_url = (
                    f"https://youtube.com/shorts/{vid}" if is_short
                    else rec.get("url") or f"https://youtu.be/{vid}"
                )
                top_performer = {
                    "youtube_url": watch_url,
                    "title": rec.get("title") or vid,
                    "thumb_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "views": views,
                    "channel": account,
                }
    except Exception as e:
        logger.warning(f"dashboard: top_performer aggregation failed: {e}")

    base["top_performer"] = top_performer
    return base


def _dashboard_state_bucket() -> str | None:
    """GCS state bucket holding per-channel uploads/analytics in cloud.

    Mirrors ``control/core/scheduler.py::_state_bucket``. When unset
    (laptop dev), every dashboard reader walks the local FS.
    """
    return os.environ.get("YTFACTORY_STATE_BUCKET") or None


def _dashboard_gcs_client():
    """Lazy google-cloud-storage client for the cloud dashboard reads."""
    from google.cloud import storage  # noqa: PLC0415
    return storage.Client(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
    )


def _iter_channel_slugs() -> list[str]:
    """All channel slugs known to the registry (cloud-safe).

    Reads ``pipeline/channels/*.yaml`` (the canonical post-2026-05-10
    registry) and merges in any legacy per-channel root dirs that still
    exist on disk. Used by the dashboard summary endpoints to know which
    channel namespaces to walk in GCS.
    """
    seen: set[str] = set()
    out: list[str] = []
    registry = PROJECT_ROOT / "pipeline" / "channels"
    if registry.is_dir():
        for cfg_path in sorted(registry.glob("*.yaml")):
            slug = cfg_path.stem
            if slug not in seen:
                seen.add(slug)
                out.append(slug)
    if PROJECT_ROOT.is_dir():
        for d in sorted(PROJECT_ROOT.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            if not (d / "config.yaml").exists():
                continue
            out.append(d.name)
            seen.add(d.name)
    return out


def _list_uploads_gcs(bucket: str, channel: str) -> list[tuple[str, dict]]:
    """All upload records for ``channel`` in the GCS state bucket.

    Returns list of (slug, record_dict). Skips ``*.x.json`` X-platform
    sidecars and unparseable bodies.

    Performance: the per-blob ``download_as_bytes`` calls fan out across
    a thread pool — sequential they took ~50 ms × N records each tick,
    which is the dominant cost of /api/dashboard on cloud. Wrapped by
    :func:`_iter_all_uploads`'s 60 s in-process TTL cache so most polls
    don't even reach this function.
    """
    out: list[tuple[str, dict]] = []
    try:
        cli = _dashboard_gcs_client()
        # First pass: collect blob references — cheap (one list_blobs call).
        targets: list = []
        for blob in cli.list_blobs(bucket, prefix=f"{channel}/"):
            parts = blob.name.split("/")
            if len(parts) < 3 or parts[-2] != "uploads" or not blob.name.endswith(".json"):
                continue
            if blob.name.endswith(".x.json"):
                continue
            targets.append((Path(parts[-1]).stem, blob))

        if not targets:
            return out

        def _fetch(target):
            slug, blob = target
            try:
                rec = json.loads(blob.download_as_bytes())
            except Exception:
                return None
            return (slug, rec)

        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        with ThreadPoolExecutor(
            max_workers=min(16, len(targets)),
            thread_name_prefix="dashboard-uploads-gcs",
        ) as pool:
            for result in pool.map(_fetch, targets):
                if result is not None:
                    out.append(result)
    except Exception:
        logger.warning("dashboard: GCS uploads list failed for %s", channel, exc_info=True)
    return out


# Module-level TTL cache for the cloud GCS upload enumeration. Keyed by
# bucket name so cross-test bucket monkey-patching doesn't cross-pollinate.
# Value: (cached_at_epoch, list[(channel, account, slug, vid, rec)]).
#
# 60 s TTL — comfortably shorter than any plausible publish cadence and
# long enough to collapse a 10 s dashboard poll into a single GCS scan
# every 6 ticks. Writers (the laptop's `pipeline.upload.upload`) don't
# share this process so we can't bust on write — but the dashboard
# tile is best-effort eventually-consistent anyway.
_DASHBOARD_UPLOADS_TTL_S = 60.0
_DASHBOARD_UPLOADS_CACHE: dict[str, tuple[float, list[tuple[str, str, str, str, dict]]]] = {}
_DASHBOARD_UPLOADS_LOCK = __import__("threading").Lock()


def _bust_dashboard_uploads_cache() -> None:
    """Drop the cached dashboard uploads enumeration."""
    with _DASHBOARD_UPLOADS_LOCK:
        _DASHBOARD_UPLOADS_CACHE.clear()


def _iter_all_uploads() -> list[tuple[str, str, str, str, dict]]:
    """All upload records across every channel.

    Returns ``(channel_slug, account, slug, video_id, record)`` tuples
    where ``video_id`` may be empty if the record is an X-platform
    sidecar (already filtered out above) or a malformed entry. In cloud
    (``YTFACTORY_STATE_BUCKET`` set), reads from GCS; on laptop falls
    back to walking ``PROJECT_ROOT/<channel>/**/uploads/*.json``.

    Cloud path is cached in-process for ``_DASHBOARD_UPLOADS_TTL_S``
    so the dashboard's 10 s poll cadence doesn't re-walk GCS on every
    tick; the per-channel listing inside that walk runs in parallel
    threads. Together these collapse what was a 3–8 s sequential
    waterfall (one HTTP round-trip per record per channel) to ~0 ms
    on warm polls and a few hundred ms on a cold scan.
    """
    bucket = _dashboard_state_bucket()
    out: list[tuple[str, str, str, str, dict]] = []

    if bucket:
        # Warm-cache fast path.
        now = time.time()
        with _DASHBOARD_UPLOADS_LOCK:
            cached = _DASHBOARD_UPLOADS_CACHE.get(bucket)
        if cached is not None and (now - cached[0]) < _DASHBOARD_UPLOADS_TTL_S:
            return cached[1]

        # Cold path: walk every channel in parallel — each call is one
        # GCS list + N parallel downloads, so doing them sequentially
        # would still serialize the per-channel list operations.
        channels = _iter_channel_slugs()
        if channels:
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
            with ThreadPoolExecutor(
                max_workers=min(8, len(channels)),
                thread_name_prefix="dashboard-uploads-channels",
            ) as pool:
                results = list(pool.map(
                    lambda c: (c, _list_uploads_gcs(bucket, c)),
                    channels,
                ))
            for channel, recs in results:
                for slug, rec in recs:
                    vid = rec.get("video_id") or ""
                    acct = rec.get("account") or channel
                    if vid:
                        out.append((channel, acct, slug, vid, rec))
        with _DASHBOARD_UPLOADS_LOCK:
            _DASHBOARD_UPLOADS_CACHE[bucket] = (now, out)
        return out

    # Laptop FS fallback.
    if not PROJECT_ROOT.is_dir():
        return out
    for chan_dir in PROJECT_ROOT.iterdir():
        if not chan_dir.is_dir() or not (chan_dir / "config.yaml").exists():
            continue
        for rec_path in chan_dir.rglob("uploads/*.json"):
            if rec_path.name.endswith(".x.json"):
                continue
            try:
                rec = json.loads(rec_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            vid = rec.get("video_id") or ""
            acct = rec.get("account") or chan_dir.name
            if vid:
                out.append((chan_dir.name, acct, rec_path.stem, vid, rec))
    return out


# ---------------------------------------------------------------------------
# (Removed in C2 — 2026-05-10 token-issue permanent fix)
# ---------------------------------------------------------------------------
#
# The legacy ``_load_analytics_for_slug`` helper read per-video analytics
# from ``data/research/analytics/<slug>.json``. That JSON was written by
# a refresh path tied to per-account OAuth tokens — exactly the fragility
# we're killing. The canonical dashboard now lives at
# ``control/routes/dashboard_routes.py::dashboard_videos`` and pulls
# stats live from YouTube Data API v3 with a single ``YOUTUBE_API_KEY``
# (in-memory 10-min TTL cache, no per-channel OAuth, no on-disk cache).
# Avatar/banner mirrors at ``data/research/channel_assets/`` are still
# served via ``channel_assets.asset_path`` (FS only) — they're baked
# into the prod image and rarely change, so GCS-ifying them is
# deferred until rebrands become more frequent.


def _dashboard_summary_sync() -> dict:
    import time
    from datetime import datetime, timezone

    now = time.time()
    week_ago = now - 7 * 24 * 3600

    # --- Jobs (renders) ---------------------------------------------------
    jobs_dir = JOBS_PERSIST_DIR
    recent_jobs: list[dict] = []
    renders_7d = 0
    queued = 0
    if jobs_dir.exists():
        for jf in jobs_dir.glob("*.json"):
            blob = _read_job_file(jf)
            if blob is None:
                continue
            state = (blob.get("state") or blob.get("status") or "").lower()
            ts = blob.get("updated_at") or blob.get("created_at") or jf.stat().st_mtime
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = jf.stat().st_mtime
            if state in {"pending", "running", "queued"}:
                queued += 1
            if state in {"done", "completed", "succeeded", "ok"} and ts >= week_ago:
                renders_7d += 1
            recent_jobs.append({
                "id": blob.get("id") or jf.stem,
                "channel": blob.get("channel") or blob.get("channel_yaml") or "—",
                "slug": blob.get("slug") or blob.get("script") or "—",
                "state": state or "unknown",
                "updated_at": (
                    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    if isinstance(ts, (int, float))
                    else None
                ),
            })
    recent_jobs.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    recent_jobs = recent_jobs[:6]

    # --- Uploads (last 7 days) -------------------------------------------
    #
    # Provider-aware: in cloud (YTFACTORY_STATE_BUCKET set) reads from
    # GCS via _iter_all_uploads(); on laptop walks the per-channel
    # filesystem layout via _channel_scan. The ``held`` count remains
    # FS-only because per-channel _holds.json files aren't mirrored to
    # GCS yet — that's a separate plan item.
    uploads_7d = 0
    held = 0
    if _dashboard_state_bucket():
        for _channel, _account, _slug, _vid, rec in _iter_all_uploads():
            if rec.get("platform") and rec.get("platform") != "youtube":
                continue
            ts = rec.get("uploaded_at") or rec.get("published_at")
            epoch: float | None = None
            if isinstance(ts, str):
                try:
                    epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    epoch = None
            if epoch is not None and epoch >= week_ago:
                uploads_7d += 1
    else:
        for chan_dir in PROJECT_ROOT.iterdir():
            if not chan_dir.is_dir() or not (chan_dir / "config.yaml").exists():
                continue
            scan = _channel_scan(chan_dir)
            for _upf, rec in scan["uploads"]:
                if rec.get("platform") and rec.get("platform") != "youtube":
                    continue
                ts = rec.get("uploaded_at") or rec.get("published_at")
                epoch: float
                if isinstance(ts, str):
                    try:
                        epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        try:
                            epoch = _upf.stat().st_mtime
                        except OSError:
                            continue
                else:
                    try:
                        epoch = _upf.stat().st_mtime
                    except OSError:
                        continue
                if epoch >= week_ago:
                    uploads_7d += 1
            held += len(scan["holds"])

    return {
        "renders_7d": renders_7d,
        "uploads_7d": uploads_7d,
        "queued": queued,
        "held": held,
        "recent_jobs": recent_jobs,
    }


# /api/dashboard/videos is intentionally NOT defined here — see C1 of the
# permanent-fix-for-token-issue plan (2026-05-10). The handler at
# control/routes/dashboard_routes.py::dashboard_videos is the canonical
# implementation: GCS-aware uploads listing via control.core.storage.
# list_upload_records(), public-API stats via YOUTUBE_API_KEY (no per-
# channel OAuth tokens needed), in-memory cache + refresh logic. Mounted
# at the bottom of this file via app.include_router(_control_dashboard_router).
# Setting YOUTUBE_API_KEY in the prod env lights up the channel cards.


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
        from pipeline.audio import audio as audio_mod
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

    from pipeline.voice import voice_clone as vc_mod
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


# ---- Website-native render-from-script (2026-05-09) ---------------------
#
# Skills (the AI authoring layer) hand-author script JSON locally, then
# POST it here. The website spawns the right renderer in its own
# already-correct env, captures stdout/stderr to a per-job log file,
# and exposes status + log_tail + final mp4 path via the GET sibling.
# This replaces the older "skills bash-exec scripts/make_shorts.py
# directly" pattern (`feedback_skills_kick_render_directly.md`),
# which was env-fragile (PYTHONPATH, cwd, gcloud account).
# Allowed entry-points the skill is permitted to invoke. Whitelist
# rather than free-form to prevent the endpoint from becoming a
# remote-shell. Add a new path here to expose a new renderer.
_ALLOWED_RENDER_CMDS: set[str] = {
    "scripts/make_shorts.py",
    "historyrecapped/scripts/render_long_form.py",
    "historyrecapped/scripts/render_footage_only.py",
    "sportsrecapped/scripts/render_long_form_doc.py",
    "sportsrecapped/scripts/render_tweet_reaction.py",
    "sportsrecapped/scripts/render_rivalry_compilation.py",
    "scrollpulse/scripts/render_split_screen.py",
}


def _resolve_mp4_for_script_job(
    hint_paths: list[str],
    *,
    min_mtime: float | None = None,
    mtime_tolerance_s: float = 30.0,
) -> str | None:
    """Best-effort mp4-locator. Walk the channel-rooted hints (e.g. a
    --channel YAML and a --script JSON or --slug-derived path) and look
    for an mp4 with the same stem under {shorts,long_form}/.

    Two layouts seen in the wild as of 2026-05-09:
      <chan>/<niche>/shorts/<slug>.mp4           ← mystoriesanimated
      <chan>/<niche>/shorts/<slug>/<slug>.mp4    ← some others
    Try both.

    ``min_mtime`` (2026-05-09 fix): when set, an mp4 only matches if its
    mtime is ≥ ``min_mtime - mtime_tolerance_s``. Without this, a brand-new
    job whose subprocess exited 0 quickly (silent failure, or an
    everything-cached fast path) would resolve a STALE mp4 from the
    previous render of the same slug and be marked "done" instantly,
    pointing the UI at the OLD video. Pass `started_at` here.
    """
    candidates: list[Path] = []
    slugs: set[str] = set()
    for hint in hint_paths:
        if not hint:
            continue
        p = Path(hint)
        if p.suffix in (".json", ".yaml") or p.is_file():
            slugs.add(p.stem)
        # Walk upward looking for a likely channel root
        for parent in [p, p.parent, p.parent.parent, p.parent.parent.parent]:
            if not parent or not parent.is_dir():
                continue
            for kind in ("shorts", "long_form"):
                candidates.append(parent / kind)
    threshold = (min_mtime - mtime_tolerance_s) if min_mtime is not None else None
    for kind_dir in candidates:
        if not kind_dir.is_dir():
            continue
        for slug in slugs:
            for mp4 in (kind_dir / f"{slug}.mp4", kind_dir / slug / f"{slug}.mp4"):
                if not mp4.exists():
                    continue
                if threshold is not None and mp4.stat().st_mtime < threshold:
                    # Stale — pre-dates this job's start. Skip.
                    continue
                return str(mp4)
    return None


# ---- Render-progress parser (2026-05-09) --------------------------------
#
# Skill-driven /api/jobs/from_script flows used to expose only state ∈
# {running, done, done_no_mp4_found, failed} — the user saw "running"
# flip to "done" with nothing in between, and on cache-hit fast paths
# the first poll often caught it already at "done". The renderer
# already prints high-fidelity stage markers to stdout (mirrored to
# the per-job log file); we just weren't parsing them. Now we do.
#
# Contract — each renderer prints, in order:
#   [1/4] TTS …              ← TTS phase
#   [2/4] … beat split …     ← ASR/beats phase
#   [3/4] <provider>: generating N images …
#   [image-done] beat I of N ← per-image completion
#   [4/4] ffmpeg compose …   ← final compose
# Plus optional: [warmup] …, [opening_image] WARNING: …, traceback lines.

_PHASE_TOTAL = 4
_PHASE_LABELS = {
    0: "starting",
    1: "tts",
    2: "beats",
    3: "images",
    4: "compose",
}
_PHASE_HUMAN = {
    "starting": "Starting render",
    "tts": "Synthesising narration",
    "beats": "Aligning beats",
    "images": "Generating images",
    "compose": "Composing video",
}

_RE_PHASE_HEADER = re.compile(r"^\[(\d)/4\]\s*(.*)$")
_RE_IMAGES_TOTAL = re.compile(r"generating\s+(\d+)\s+(?:images|clips)\b")
_RE_IMAGE_DONE = re.compile(r"^\[image-done\]\s+beat\s+(\d+)\s+of\s+(\d+)\s*$")


def _parse_script_job_progress(
    log_path: str | None,
    *,
    state: str | None = None,
    max_bytes: int = 64 * 1024,
) -> dict:
    """Parse the latest stage + per-image progress out of the renderer
    log tail. Returns a JSON-safe dict the UI can render directly.

    Only reads the tail (≤64KB) so it's cheap to call on every poll —
    the markers we care about are line-anchored and the latest one wins.

    Output schema:
        {
            "phase":         str   (one of _PHASE_LABELS values),
            "phase_index":   int   (0-4),
            "phase_total":   int   (4),
            "phase_label":   str   (human-readable),
            "images_done":   int | None,
            "images_total":  int | None,
            "percent":       int   (0-100, rough overall completion),
            "text":          str   (single-line status for the UI),
        }
    """
    phase_index = 0
    images_done: int | None = None
    images_total: int | None = None

    if log_path:
        p = Path(log_path)
        if p.exists():
            try:
                with open(p, "rb") as f:
                    try:
                        f.seek(-max_bytes, os.SEEK_END)
                    except OSError:
                        f.seek(0)
                    data = f.read().decode("utf-8", errors="replace")
                for raw in data.splitlines():
                    line = raw.strip()
                    if not line:
                        continue
                    m = _RE_PHASE_HEADER.match(line)
                    if m:
                        idx = int(m.group(1))
                        if idx > phase_index:
                            phase_index = idx
                        if idx == 3:
                            mt = _RE_IMAGES_TOTAL.search(m.group(2))
                            if mt:
                                images_total = int(mt.group(1))
                                # New image phase started — reset counter
                                # (the critic patch path can re-emit [3/4]).
                                images_done = 0
                        continue
                    m = _RE_IMAGE_DONE.match(line)
                    if m:
                        # `beat I of N` is 0-indexed at I; treat I as
                        # "completed-through-this-beat" (so I=0 means
                        # 1 image done).
                        i = int(m.group(1))
                        n = int(m.group(2))
                        images_total = n
                        images_done = max(images_done or 0, i + 1)
            except OSError:
                pass

    # Terminal-state overrides — once the worker reports done/failed,
    # the parser's last-marker view is already stale.
    if state in ("done", "done_no_mp4_found"):
        phase_index = _PHASE_TOTAL
        if images_total is not None:
            images_done = images_total
    elif state == "failed":
        # Keep whichever phase was last seen so the UI can show
        # "Failed at: Generating images". Do nothing.
        pass

    phase = _PHASE_LABELS.get(phase_index, "starting")
    label = _PHASE_HUMAN.get(phase, phase)

    if state in ("done", "done_no_mp4_found"):
        text = "Done"
        percent = 100
    elif state == "failed":
        text = f"Failed at: {label}"
        # Estimate percent as fraction of phases completed at failure.
        percent = int(round(100 * phase_index / _PHASE_TOTAL))
    else:
        if phase == "images" and images_total:
            done = images_done or 0
            text = f"{label} ({done}/{images_total})"
            # Smoother percent for the longest phase: blend phase
            # midpoint with image fraction.
            phase_floor = (phase_index - 1) / _PHASE_TOTAL
            phase_ceil = phase_index / _PHASE_TOTAL
            sub = done / max(images_total, 1)
            percent = int(round(100 * (phase_floor + sub * (phase_ceil - phase_floor))))
        else:
            text = label
            # Conservative: report having entered this phase, not finished it.
            percent = int(round(100 * max(phase_index - 1, 0) / _PHASE_TOTAL))

    return {
        "phase": phase,
        "phase_index": phase_index,
        "phase_total": _PHASE_TOTAL,
        "phase_label": label,
        "images_done": images_done,
        "images_total": images_total,
        "percent": max(0, min(100, percent)),
        "text": text,
    }


@app.post("/api/jobs/from_script")
async def create_script_job(payload: dict) -> dict:
    """Run a pre-built renderer command via the website (the AI-layer
    entry). The skill assembles the exact CLI it wants; the website
    just executes it in its own already-correct process env
    (PYTHONPATH, gcloud account, cwd) and tracks status.

    Payload (preferred — generic):
        cmd:    list[str], e.g.
                ["scripts/make_shorts.py",
                 "--channel", "mystoriesanimated/variants/aita_animated.yaml",
                 "--script", "mystoriesanimated/.../scripts/<slug>.json"]
                The first element MUST be in the allowed-entry-point
                whitelist (`_ALLOWED_RENDER_CMDS`).

    Payload (legacy convenience for shorts):
        channel_yaml + script_path  →  rewrites to cmd=
            [scripts/make_shorts.py, --channel, ..., --script, ...]
    """
    cmd_in: list[str] = list(payload.get("cmd") or [])

    # Convenience-form rewrite (keeps the shorts-only callers simple).
    if not cmd_in and (payload.get("channel_yaml") and payload.get("script_path")):
        cmd_in = [
            "scripts/make_shorts.py",
            "--channel", str(payload["channel_yaml"]),
            "--script", str(payload["script_path"]),
        ]
        cmd_in.extend(list(payload.get("extra_args") or []))

    if not cmd_in:
        raise HTTPException(
            400,
            "either `cmd: [...]` or `{channel_yaml, script_path}` is required",
        )
    entry = cmd_in[0]
    if entry not in _ALLOWED_RENDER_CMDS:
        raise HTTPException(
            400,
            f"entry-point {entry!r} not in allowed whitelist: "
            f"{sorted(_ALLOWED_RENDER_CMDS)}",
        )

    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / entry).exists():
        raise HTTPException(400, f"renderer script not found at: {entry}")

    # Validate any --channel / --script / --slug paths that exist on
    # disk. Best-effort — unknown flags pass through.
    flag_paths: list[str] = []
    it = iter(cmd_in[1:])
    for tok in it:
        if tok in ("--channel", "--script"):
            try:
                v = next(it)
            except StopIteration:
                break
            flag_paths.append(v)
            if not (repo_root / v).exists() and not Path(v).exists():
                raise HTTPException(400, f"{tok} path not found: {v}")

    SCRIPT_JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:10]
    log_path = SCRIPT_JOB_LOG_DIR / f"{job_id}.log"
    label = payload.get("label") or " ".join(cmd_in[:6])

    SCRIPT_JOBS[job_id] = {
        "job_id": job_id,
        "label": label,
        "cmd": list(cmd_in),
        "state": "running",
        "started_at": time.time(),
        "completed_at": None,
        "exit_code": None,
        "mp4_path": None,
        "error": None,
        "log_path": str(log_path),
        "backend": os.environ.get("YTFACTORY_RENDER_BACKEND", "local"),
    }

    backend = SCRIPT_JOBS[job_id]["backend"]
    if backend == "cloudrun":
        # Cloud Run JOB path: upload spec to GCS, trigger the worker, poll
        # state.json. See docs/full_cloud_cutover_2026_05_09.md.
        asyncio.create_task(_run_cloudrun(job_id, cmd_in, flag_paths))
        return {"job_id": job_id, "state": "running", "backend": "cloudrun"}

    # Local subprocess path (default).
    cmd = [
        os.fspath(repo_root / ".venv" / "bin" / "python"),
        *cmd_in,
    ]

    async def _run() -> None:
        rec = SCRIPT_JOBS[job_id]
        try:
            with open(log_path, "wb") as logfh:
                env = os.environ.copy()
                # Make sure pipeline.* imports resolve regardless of how
                # the website itself was started.
                env["PYTHONPATH"] = str(repo_root) + (
                    os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
                )
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(repo_root),
                    env=env,
                    stdout=logfh,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                rec["pid"] = proc.pid
                await proc.wait()
            rec["exit_code"] = proc.returncode
            rec["completed_at"] = time.time()
            if proc.returncode == 0:
                # Fresh-mp4 gate: only accept an mp4 produced by THIS
                # job (mtime ≥ started_at). Without the gate, a
                # subprocess that exited 0 silently — or an
                # everything-cached fast path that didn't actually
                # rewrite the mp4 — would resolve a stale prior render
                # and the UI would flip straight to "done" pointing
                # at the OLD video. (2026-05-09 instant-done bug.)
                mp4 = _resolve_mp4_for_script_job(
                    flag_paths, min_mtime=rec["started_at"]
                )
                rec["mp4_path"] = mp4
                if mp4:
                    rec["state"] = "done"
                else:
                    # Distinguish a true no-mp4-on-disk case from a
                    # fast silent failure: if the subprocess exited
                    # near-instantly AND wrote no log, surface as
                    # failed so the UI doesn't claim success.
                    elapsed = rec["completed_at"] - rec["started_at"]
                    log_size = (
                        Path(log_path).stat().st_size
                        if Path(log_path).exists() else 0
                    )
                    if elapsed < 2.0 and log_size < 64:
                        rec["state"] = "failed"
                        rec["error"] = (
                            f"renderer exited 0 in {elapsed:.2f}s with "
                            f"{log_size}B of log output and no fresh mp4 "
                            "(silent failure — check entry-point and env)"
                        )
                    else:
                        rec["state"] = "done_no_mp4_found"
            else:
                rec["state"] = "failed"
                rec["error"] = f"renderer exit_code={proc.returncode}"
        except Exception as e:
            rec["state"] = "failed"
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["completed_at"] = time.time()
        finally:
            # Terminal state — flush instantly so a Cloud Run revision
            # rotation in the next millisecond doesn't lose the verdict.
            SCRIPT_JOBS.flush(job_id)

    asyncio.create_task(_run())
    return {"job_id": job_id, "state": "running", "backend": "local"}


# ---- Cloud Run JOB backend (P-cloud — 2026-05-09) ------------------------
#
# When YTFACTORY_RENDER_BACKEND=cloudrun, /api/jobs/from_script writes a
# spec.json to GCS, triggers the ytfactory-render-worker Cloud Run JOB
# with JOB_SPEC_GCS_URI, and polls state.json until the worker completes.
# The same SCRIPT_JOBS dict surfaces both backends transparently to the
# /renders UI. Design: docs/full_cloud_cutover_2026_05_09.md.

CLOUDRUN_JOB_NAME = os.environ.get("YTFACTORY_CLOUDRUN_JOB", "ytfactory-render-worker-v2")
CLOUDRUN_JOB_REGION = os.environ.get("YTFACTORY_CLOUDRUN_REGION", "asia-southeast1")
CLOUDRUN_JOB_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
CLOUDRUN_ARTIFACTS_BUCKET = os.environ.get(
    "YTFACTORY_BUCKET", "ytfactory-prod-v2-artifacts"
)


def _b64_file(p: Path) -> str:
    import base64
    return base64.b64encode(p.read_bytes()).decode("ascii")


async def _run_cloudrun(
    job_id: str, cmd_in: list[str], flag_paths: list[str]
) -> None:
    """Cloud Run backend for /api/jobs/from_script."""
    rec = SCRIPT_JOBS[job_id]
    repo_root = Path(__file__).resolve().parent.parent

    try:
        # 1. Resolve --channel / --script / optional --raw paths.
        channel_yaml: Path | None = None
        script_path: Path | None = None
        it = iter(cmd_in)
        next(it)  # skip the entry script
        for tok in it:
            if tok == "--channel":
                channel_yaml = repo_root / next(it)
            elif tok == "--script":
                script_path = repo_root / next(it)
        if not (channel_yaml and script_path):
            raise ValueError("cmd missing --channel or --script")
        # Optional raw file (the renderer can use it for upload metadata).
        raw_rel: str | None = None
        raw_b64: str | None = None
        if script_path.parent.name == "scripts":
            raw_candidate = (
                script_path.parent.parent / "raw" / f"{script_path.stem}.json"
            )
            if raw_candidate.exists():
                raw_rel = str(raw_candidate.relative_to(repo_root))
                raw_b64 = _b64_file(raw_candidate)

        # 2. Build spec.
        cy_rel = str(channel_yaml.relative_to(repo_root))
        sp_rel = str(script_path.relative_to(repo_root))
        spec = {
            "job_id": job_id,
            "cmd": list(cmd_in),
            "channel_yaml_path": cy_rel,
            "channel_yaml_b64": _b64_file(channel_yaml),
            "script_path": sp_rel,
            "script_json_b64": _b64_file(script_path),
        }
        if raw_rel and raw_b64:
            spec["raw_path"] = raw_rel
            spec["raw_b64"] = raw_b64

        # 3. Upload spec.json to GCS.
        spec_uri = (
            f"gs://{CLOUDRUN_ARTIFACTS_BUCKET}/jobs/{job_id}/spec.json"
        )
        await asyncio.to_thread(_gcs_upload_text, json.dumps(spec), spec_uri)
        rec["spec_uri"] = spec_uri

        # 4. Trigger Cloud Run JOB execution. We use `gcloud run jobs
        #    execute` rather than the REST API because the gcloud CLI
        #    handles auth automatically; orchestrator runs as
        #    tts-runner SA which has the run.invoker / run.developer roles.
        # Inject ``YTFACTORY_TRACEPARENT`` so the JOB's root span links
        # back to this chat-request span — see
        # ``cloud/_shared/otel_init.py::attach_traceparent_from_env``.
        from pipeline.observability import propagation as _trace_prop  # noqa: PLC0415
        env_pairs = [f"JOB_SPEC_GCS_URI={spec_uri}"]
        for k, v in _trace_prop.inject_into_env({}).items():
            env_pairs.append(f"{k}={v}")
        env_arg = "^|^" + "|".join(env_pairs)
        execute_cmd = [
            "gcloud", "run", "jobs", "execute", CLOUDRUN_JOB_NAME,
            "--project", CLOUDRUN_JOB_PROJECT,
            "--region", CLOUDRUN_JOB_REGION,
            "--update-env-vars", env_arg,
            "--async",
            "--format", "value(metadata.name)",
        ]
        proc = await asyncio.create_subprocess_exec(
            *execute_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"gcloud run jobs execute failed: {stderr.decode(errors='replace')}"
            )
        execution_name = stdout.decode().strip()
        rec["cloudrun_execution"] = execution_name

        # 5. Poll state.json from GCS until terminal.
        state_uri = (
            f"gs://{CLOUDRUN_ARTIFACTS_BUCKET}/jobs/{job_id}/state.json"
        )
        terminal = ("done", "done_no_mp4_found", "failed")
        for _ in range(720):  # ~1 hr at 5s poll
            await asyncio.sleep(5)
            try:
                state = await asyncio.to_thread(_gcs_read_json, state_uri)
            except Exception:
                continue
            if state.get("state") in terminal:
                rec["state"] = state["state"]
                rec["exit_code"] = state.get("exit_code")
                rec["error"] = state.get("error")
                rec["mp4_path"] = state.get("mp4_uri")
                rec["completed_at"] = state.get("completed_at") or time.time()
                rec["log_uri"] = state.get("log_uri")
                SCRIPT_JOBS.flush(job_id)
                return
        # Timed out
        rec["state"] = "failed"
        rec["error"] = "polling timed out after 1 hour"
        rec["completed_at"] = time.time()
        SCRIPT_JOBS.flush(job_id)
    except Exception as e:
        rec["state"] = "failed"
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["completed_at"] = time.time()
        SCRIPT_JOBS.flush(job_id)


def _gcs_upload_text(text: str, uri: str, content_type: str = "application/json") -> None:
    """Sync GCS text upload (called via asyncio.to_thread)."""
    from google.cloud import storage
    from urllib.parse import urlparse
    p = urlparse(uri)
    client = storage.Client(project=CLOUDRUN_JOB_PROJECT)
    blob = client.bucket(p.netloc).blob(p.path.lstrip("/"))
    # Pass content_type to upload_from_string directly — setting
    # blob.content_type beforehand triggers a metadata-vs-upload
    # content-type mismatch (HTTP 400) on the multipart upload.
    blob.upload_from_string(text, content_type=content_type)


def _gcs_read_json(uri: str) -> dict:
    """Sync GCS JSON read (called via asyncio.to_thread)."""
    from google.cloud import storage
    from urllib.parse import urlparse
    p = urlparse(uri)
    client = storage.Client(project=CLOUDRUN_JOB_PROJECT)
    blob = client.bucket(p.netloc).blob(p.path.lstrip("/"))
    return json.loads(blob.download_as_bytes())


@app.get("/api/jobs/from_script/{job_id}")
async def get_script_job(job_id: str, log_tail_kb: int = 8) -> dict:
    rec = SCRIPT_JOBS.get(job_id)
    if not rec:
        raise HTTPException(404, "script job not found")
    out = dict(rec)
    out["elapsed_s"] = round(
        (rec["completed_at"] or time.time()) - rec["started_at"], 1
    )
    log_path = Path(rec["log_path"])
    if log_path.exists():
        nbytes = max(1, log_tail_kb) * 1024
        with open(log_path, "rb") as f:
            try:
                f.seek(-nbytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace")
        out["log_tail"] = tail
    else:
        out["log_tail"] = ""
    out["progress"] = _parse_script_job_progress(
        rec.get("log_path"), state=rec.get("state")
    )
    return out


@app.get("/api/jobs/from_script/{job_id}/mp4")
async def get_script_job_mp4(job_id: str):
    """Serve the rendered mp4 for a finished script-driven job. Handles
    both local filesystem mp4s (`mp4_path` is a real path) and GCS-hosted
    mp4s (`mp4_path` is a `gs://` URI from the cloudrun backend).
    """
    rec = SCRIPT_JOBS.get(job_id)
    if not rec:
        raise HTTPException(404, "script job not found")
    return _serve_script_job_mp4(rec)


@app.get("/api/jobs/from_script")
async def list_script_jobs(limit: int = 20) -> dict:
    items = sorted(
        SCRIPT_JOBS.values(),
        key=lambda r: r.get("started_at", 0),
        reverse=True,
    )[:limit]
    return {"jobs": [_script_job_view(r) for r in items]}


def _script_job_view(rec: dict) -> dict:
    """Strip the on-disk log_path and attach a parsed `progress` block.
    Used by both /api/overview's recent_renders and the list endpoint so
    the UI can show a live stage strip without an extra round-trip."""
    out = {k: v for k, v in rec.items() if k != "log_path"}
    out["progress"] = _parse_script_job_progress(
        rec.get("log_path"), state=rec.get("state")
    )
    return out


# ---- /api/jobs/{id} fall-through adapter (2026-05-10) -------------------
#
# A skill-submitted render lives in SCRIPT_JOBS, but the new web-next
# render-detail page (`/app/render/<id>`) polls `/api/jobs/{id}`, which
# resolves to the niche-driven `JOBS` dict. Without an adapter, every
# skill-rendered job 404s the moment the user opens its page. The
# adapter maps a SCRIPT_JOBS record into BOTH shapes:
#
#   * legacy `JOBS`-snapshot keys (state/niche/mp4_url/events/…) for the
#     older UIs that still exist on the same /api/jobs/{id} surface;
#   * new `JobView` keys (status/channel/topic/preview_url/created_at/
#     updated_at/log_tail/proposal/…) for the web-next UI.
#
# Extra keys are harmless to clients that ignore them. See
# /Users/rohit/.copilot/session-state/.../plan.md for the full mapping.

_SCRIPT_STATE_TO_STATUS = {
    "running": "rendering",
    "done": "done",
    "done_no_mp4_found": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _isoformat(epoch: float | None) -> str | None:
    if not epoch:
        return None
    import datetime
    return (
        datetime.datetime.fromtimestamp(float(epoch), tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _isoformat_value(value: Any) -> str | None:
    """Normalize a wider set of timestamp inputs to the same ``…Z`` shape
    ``_isoformat`` produces for epoch floats.

    Handles:
    - ``datetime`` (naive → assumed UTC; aware → converted to UTC)
    - epoch ``int`` / ``float``
    - already-formatted ``str`` (passed through; ``+00:00`` → ``Z``)
    - ``None`` / falsy → ``None``

    Used by the control-plane fall-through where ``created_at`` /
    ``updated_at`` arrive as ``datetime`` objects (``control.core.jobs``
    writes ``_utcnow()``) — passing those through ``_isoformat`` would
    raise because it does ``float(epoch)``.
    """
    if value is None or value == "":
        return None
    import datetime as _dt
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        else:
            value = value.astimezone(_dt.timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return _isoformat(value)
    if isinstance(value, str):
        return value.replace("+00:00", "Z")
    # Best-effort for unfamiliar types (e.g. google.cloud.firestore Timestamp).
    try:
        return _isoformat_value(value.isoformat())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _channel_topic_from_cmd(cmd: list[str] | None) -> tuple[str | None, str | None]:
    """Best-effort extraction of channel + topic from the renderer cmd.

    `--channel <yaml>` → channel slug from the first path component
    (`mystoriesanimated/variants/aita_animated.yaml` → `mystoriesanimated`).
    `--script <json>` → topic from the file stem (`my_aita_short.json` →
    `my_aita_short`). Returns (None, None) on parse failure rather than
    raising so the snapshot still renders.
    """
    if not cmd:
        return (None, None)
    channel: str | None = None
    topic: str | None = None
    it = iter(cmd[1:])  # skip the entry script
    for tok in it:
        if tok == "--channel":
            try:
                yaml_path = next(it)
                parts = Path(yaml_path).parts
                channel = parts[0] if parts else None
            except StopIteration:
                break
        elif tok == "--script":
            try:
                script_path = next(it)
                topic = Path(script_path).stem
            except StopIteration:
                break
    return (channel, topic)


def _script_job_to_snapshot(rec: dict) -> dict:
    """Adapt a SCRIPT_JOBS record to the same JSON shape /api/jobs/{id}
    returns for niche-driven jobs.

    Includes both legacy keys and new JobView keys so the response is
    consumable by every UI that hits this endpoint.
    """
    job_id = rec.get("job_id", "")
    state = rec.get("state") or "running"
    status = _SCRIPT_STATE_TO_STATUS.get(state, state)
    channel, topic = _channel_topic_from_cmd(rec.get("cmd"))
    progress = _parse_script_job_progress(
        rec.get("log_path"), state=state
    )

    mp4_path = rec.get("mp4_path")
    short_uri: str | None = None
    preview_url: str | None = None
    if mp4_path:
        if isinstance(mp4_path, str) and mp4_path.startswith("gs://"):
            short_uri = mp4_path
        # The mp4 endpoint at /api/jobs/{id}/short already falls through
        # to SCRIPT_JOBS (see job_short() below), so a single URL covers
        # both local and gs:// paths.
        preview_url = f"/api/jobs/{job_id}/short"

    # Read the log tail the same way the from_script endpoint does, so
    # the new UI's "live trail" surface works without an extra request.
    log_tail = ""
    log_path = rec.get("log_path")
    if log_path:
        p = Path(log_path)
        if p.exists():
            try:
                with open(p, "rb") as f:
                    try:
                        f.seek(-8 * 1024, os.SEEK_END)
                    except OSError:
                        f.seek(0)
                    log_tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                pass

    proposal = {
        "from_script": True,
        "backend": rec.get("backend"),
        "cmd": rec.get("cmd"),
        "label": rec.get("label"),
        "spec_uri": rec.get("spec_uri"),
        "log_uri": rec.get("log_uri"),
        "cloudrun_execution": rec.get("cloudrun_execution"),
        "exit_code": rec.get("exit_code"),
        "progress": progress,
    }
    # Drop None values from proposal to keep the payload tight.
    proposal = {k: v for k, v in proposal.items() if v is not None}

    return {
        # ---- Legacy job_snapshot keys (web/static UIs) ----
        "job_id": job_id,
        "niche": channel,
        "state": state,
        "slug": topic,
        "error": rec.get("error"),
        "stage_started": {},
        "stage_done": {},
        "events": [],
        "beat_prompts": [],
        "mp4_url": preview_url,
        "profile": {},
        "seeds": [],
        "seed_idx": 0,
        "seed_total": 1,
        "mp4s": [],
        # ---- New JobView keys (web-next UI) ----
        "status": status,
        "stage": progress.get("phase"),
        "channel": channel,
        "topic": topic,
        "short_uri": short_uri,
        "preview_url": preview_url,
        "created_at": _isoformat(rec.get("started_at")),
        "updated_at": _isoformat(rec.get("completed_at") or rec.get("started_at")),
        "log_tail": log_tail,
        "proposal": proposal,
        "timeline": [],
        "critique": None,
        "youtube_url": None,
        "thumb_uri": None,
    }


# Status names from `control.core.jobs` are already in the JobView
# vocabulary; no remap needed for `status`. We mirror them into the legacy
# `state` slot so old UIs that still consume that key get a sensible value.
_CONTROL_STATUS_TO_LEGACY_STATE = {
    "pending": "queued",
    "rendering": "running",
    "uploading": "running",
    "researching": "running",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _control_job_to_snapshot(job_id: str, doc: dict) -> dict:
    """Adapt a ``control.core.jobs`` Firestore doc to the same JSON shape
    ``/api/jobs/{id}`` returns for legacy ``JOBS`` and ``SCRIPT_JOBS``.

    The control plane (``/api/render`` + chat → confirm) is the third
    submission origin for the same endpoint — without this adapter, jobs
    born there 404 because ``job_snapshot`` only checks the two legacy
    stores. See tests/test_jobs_id_fallthrough.py for the contract.

    Mirrors the ``preview_url`` gating from
    ``control.routes.render_routes._doc_to_view`` (only set once a render
    is past ``rendering`` / ``researching``, so the UI's ``<video>``
    doesn't bind to a URL that's guaranteed to 404).
    """
    status = doc.get("status") or "pending"
    legacy_state = _CONTROL_STATUS_TO_LEGACY_STATE.get(status, status)

    short_uri = doc.get("short_uri")
    thumb_uri = doc.get("thumb_uri")
    preview_url: str | None = None
    # Gate on the artifact ACTUALLY existing, not on the wider status
    # window. Pre-2026-05-12 this was gated on
    # ``status in (done, uploading)`` — but the worker flips
    # ``status="uploading"`` BEFORE the actual GCS upload completes,
    # so ``short_uri`` isn't populated in that window. The dashboard
    # then mounted a ``<video src="/api/jobs/<id>/preview.mp4">``
    # which 404'd. Mirrors the gate in
    # ``control.routes.render_routes._doc_to_view`` — the two MUST
    # stay in sync (web/server.py is the legacy fall-through; the
    # control-plane fall-through is the canonical one). Memory:
    # feedback_preview_url_artifact_gate.md.
    # coverage: this branch fires inside _job_snapshot which the legacy fall-through reaches; pinned by control-side test_status_uploading_without_short_uri_no_preview that mirrors the same gate logic
    if doc.get("preview_local_path") or short_uri:
        # Served by control.routes.render_routes.preview_mp4 — that route
        # handles both sim:// (file response) and gs:// (302 to a signed
        # URL), so a single URL covers every backend.
        preview_url = f"/api/jobs/{job_id}/preview.mp4"

    proposal = doc.get("proposal") or {}

    return {
        # ---- Legacy job_snapshot keys (web/static UIs) ----
        "job_id": job_id,
        "niche": doc.get("channel"),
        "state": legacy_state,
        "slug": doc.get("topic"),
        "error": doc.get("error"),
        "stage_started": {},
        "stage_done": {},
        "events": [],
        "beat_prompts": [],
        "mp4_url": preview_url,
        "profile": {},
        "seeds": [],
        "seed_idx": 0,
        "seed_total": 1,
        "mp4s": [],
        # ---- New JobView keys (web-next UI) ----
        "status": status,
        "stage": doc.get("stage"),
        "channel": doc.get("channel"),
        "topic": doc.get("topic"),
        "short_uri": short_uri,
        "preview_url": preview_url,
        "created_at": _isoformat_value(doc.get("created_at")),
        "updated_at": _isoformat_value(doc.get("updated_at")),
        "log_tail": doc.get("log_tail"),
        "proposal": proposal,
        "timeline": doc.get("timeline") or [],
        "critique": doc.get("critique"),
        "youtube_url": doc.get("youtube_url"),
        "thumb_uri": thumb_uri,
    }


# Job IDs born in `control.core.jobs._enqueue_render_job` are full
# `uuid.uuid4().hex` (32 lowercase hex chars). Legacy IDs from
# `web/server.py` and `script_jobs_routes.py` are always `…hex[:10]`
# (10 chars). We use this shape to decide whether a control-plane lookup
# failure should surface as 502 (the ID *should* have resolved but the
# backing store hiccuped) vs the unconditional 404 we keep for shorter,
# unknown IDs.
_CONTROL_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---- Server-side critique (P2 — 2026-05-09) ------------------------------
#
# /critique-video used to be a Claude-side skill that bash-sampled frames
# locally and called `claude -p` with a critique prompt. Same pattern,
# but the website now owns the execution: it has the mp4 (from a
# SCRIPT_JOBS record), it has the cache_dir (derivable from --channel +
# --script paths), and it has Claude CLI in-process via
# pipeline.llm.call_claude_cli. Skill becomes a thin POST.


def _resolve_critique_inputs_from_job(job_id: str) -> dict:
    """Extract slug + mp4_path + cache_dir from a finished SCRIPT_JOB."""
    rec = SCRIPT_JOBS.get(job_id)
    if not rec:
        raise HTTPException(404, f"script job {job_id} not found")
    if rec.get("state") not in ("done", "done_no_mp4_found"):
        raise HTTPException(
            400, f"job {job_id} is in state {rec.get('state')}; "
            f"critique requires a completed render"
        )
    mp4 = rec.get("mp4_path")
    if not mp4:
        raise HTTPException(400, f"job {job_id} has no mp4_path")
    # Slug: stem of --script path
    cmd = rec.get("cmd") or []
    script_path = None
    channel_yaml = None
    it = iter(cmd)
    for tok in it:
        if tok == "--script":
            try: script_path = next(it)
            except StopIteration: pass
        elif tok == "--channel":
            try: channel_yaml = next(it)
            except StopIteration: pass
    if not script_path:
        raise HTTPException(400, f"job {job_id} has no --script in cmd")
    slug = Path(script_path).stem
    # cache_dir: <niche_root>/cache/<slug>/ — matches make_shorts.py layout
    sp = Path(script_path)
    cache_dir = (
        sp.parent.parent / "cache" / slug
        if sp.parent.name == "scripts"
        else Path(mp4).parent / "cache" / slug
    )
    return {
        "slug": slug,
        "mp4_path": mp4,
        "cache_dir": str(cache_dir),
        "channel_yaml": channel_yaml,
        "script_path": script_path,
    }


@app.post("/api/critique")
async def create_critique_job(payload: dict) -> dict:
    """Server-side video critique. Calls pipeline.llm.critic.critique_short
    with the website's process env.

    Payload (any one of):
        job_id:   SCRIPT_JOBS id of a finished render. Auto-derives slug,
                  mp4_path, cache_dir from the job's cmd.
        mp4_path + cache_dir + slug:   direct override.

    Returns: {critique_id, state}.
    """
    mode = payload.get("mode", "video")
    if mode not in ("video", "audio"):
        raise HTTPException(400, f"unknown mode {mode!r}; expected video|audio")
    if mode == "audio":
        raise HTTPException(501, "audio critique not yet implemented in this endpoint")

    if payload.get("job_id"):
        inputs = _resolve_critique_inputs_from_job(payload["job_id"])
        slug = inputs["slug"]
        mp4_path = inputs["mp4_path"]
        cache_dir = inputs["cache_dir"]
    else:
        slug = payload.get("slug")
        mp4_path = payload.get("mp4_path")
        cache_dir = payload.get("cache_dir")
        if not (slug and mp4_path and cache_dir):
            raise HTTPException(
                400,
                "either job_id OR (slug + mp4_path + cache_dir) is required",
            )
    if not Path(mp4_path).exists():
        raise HTTPException(400, f"mp4 not found: {mp4_path}")

    CRITIQUE_JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    critique_id = uuid.uuid4().hex[:10]
    out_dir = CRITIQUE_JOB_LOG_DIR / critique_id
    log_path = CRITIQUE_JOB_LOG_DIR / f"{critique_id}.log"

    CRITIQUE_JOBS[critique_id] = {
        "critique_id": critique_id,
        "mode": mode,
        "slug": slug,
        "mp4_path": str(mp4_path),
        "cache_dir": str(cache_dir),
        "source_job_id": payload.get("job_id"),
        "state": "running",
        "started_at": time.time(),
        "completed_at": None,
        "verdict": None,
        "score": None,
        "error": None,
        "log_path": str(log_path),
    }

    async def _run() -> None:
        from pipeline.llm import critic as _critic
        rec = CRITIQUE_JOBS[critique_id]
        try:
            with open(log_path, "w") as logfh:
                # Best-effort log mirror — critique_short prints to stdout;
                # we don't redirect FDs because pipeline.llm.cli expects
                # to manage its own subprocess pipes. Lightweight log only.
                logfh.write(f"[critique] starting {mode} critique for slug={slug}\n")
                logfh.write(f"[critique] mp4={mp4_path}\n")
                logfh.write(f"[critique] cache={cache_dir}\n")
                logfh.flush()
            verdict = await asyncio.to_thread(
                _critic.critique_short,
                slug=slug,
                mp4_path=Path(mp4_path),
                cache_dir=Path(cache_dir),
                out_dir=out_dir,
            )
            rec["verdict"] = verdict
            rec["score"] = (
                verdict.get("score") if isinstance(verdict, dict) else None
            )
            rec["state"] = "done"
        except Exception as e:
            rec["state"] = "failed"
            rec["error"] = f"{type(e).__name__}: {e}"
        finally:
            rec["completed_at"] = time.time()

    asyncio.create_task(_run())
    return {"critique_id": critique_id, "state": "running"}


@app.get("/api/critique/{critique_id}")
async def get_critique_job(critique_id: str) -> dict:
    rec = CRITIQUE_JOBS.get(critique_id)
    if not rec:
        raise HTTPException(404, "critique job not found")
    out = dict(rec)
    out["elapsed_s"] = round(
        (rec["completed_at"] or time.time()) - rec["started_at"], 1
    )
    return out


@app.get("/api/critique")
async def list_critique_jobs(limit: int = 20) -> dict:
    items = sorted(
        CRITIQUE_JOBS.values(),
        key=lambda r: r.get("started_at", 0),
        reverse=True,
    )[:limit]
    return {
        "jobs": [
            {k: v for k, v in r.items() if k not in ("log_path",)}
            for r in items
        ]
    }


# ---- Per-render Publish (P3 — 2026-05-09) --------------------------------
#
# /api/uploads/from_job is the Publish-button entry. Takes a SCRIPT_JOBS id,
# resolves channel YAML + slug + mp4 + script JSON from its cmd, calls
# pipeline.upload.upload_short() in the website's process. On API
# quotaExceeded (HTTP 403 across all owned channels' shared 10K-unit
# pool), sets state='quota_exhausted' with a hint to run
# /upload-via-playwright. Future P3.5: drive Playwright in-process from
# pipeline/cross_engage_via_playwright.py shape.


def _is_quota_exhausted_error(err: BaseException) -> bool:
    s = str(err).lower()
    return "quotaexceeded" in s or "uploadlimitexceeded" in s or (
        "403" in s and "quota" in s
    )


@app.post("/api/uploads/from_job")
async def create_upload_from_job(payload: dict) -> dict:
    """Publish a finished script-driven render to YouTube.

    Payload:
        job_id:    SCRIPT_JOBS id (required)
        privacy:   "public" | "unlisted" | "private" (default per channel YAML)
        force:     bool — re-upload even if existing record exists
        skip_critic: bool — skip pre-upload critic gate
    """
    job_id = payload.get("job_id")
    if not job_id:
        raise HTTPException(400, "job_id required")
    rec = SCRIPT_JOBS.get(job_id)
    if not rec:
        raise HTTPException(404, f"script job {job_id} not found")
    if rec.get("state") != "done" or not rec.get("mp4_path"):
        raise HTTPException(
            400,
            f"job {job_id} state={rec.get('state')!r} mp4_path={rec.get('mp4_path')!r}; "
            f"upload requires a completed render with an mp4",
        )

    # Resolve --channel and --script from the job's cmd.
    cmd = rec.get("cmd") or []
    channel_yaml_path = None
    script_path = None
    it = iter(cmd)
    for tok in it:
        if tok == "--channel":
            try: channel_yaml_path = next(it)
            except StopIteration: pass
        elif tok == "--script":
            try: script_path = next(it)
            except StopIteration: pass
    if not (channel_yaml_path and script_path):
        raise HTTPException(
            400, f"job {job_id} cmd missing --channel or --script flags"
        )

    repo_root = Path(__file__).resolve().parent.parent
    mp4_abs = Path(rec["mp4_path"])
    cyp = (repo_root / channel_yaml_path).resolve() if not Path(channel_yaml_path).is_absolute() else Path(channel_yaml_path)
    spp = (repo_root / script_path).resolve() if not Path(script_path).is_absolute() else Path(script_path)
    if not cyp.exists() or not spp.exists() or not mp4_abs.exists():
        raise HTTPException(
            400,
            f"missing inputs: channel_yaml={cyp.exists()} "
            f"script={spp.exists()} mp4={mp4_abs.exists()}",
        )

    upload_id = uuid.uuid4().hex[:10]
    slug = spp.stem
    # channel_dir = parent of variants/ if variant yaml, else parent
    channel_dir = (
        cyp.parent.parent.name if cyp.parent.name == "variants" else cyp.parent.name
    )

    UPLOAD_JOBS[upload_id] = {
        "upload_id": upload_id,
        "source_job_id": job_id,
        "slug": slug,
        "channel_yaml": str(cyp),
        "channel_dir": channel_dir,
        "mp4_path": str(mp4_abs),
        "script_path": str(spp),
        "state": "running",
        "started_at": time.time(),
        "completed_at": None,
        "video_id": None,
        "video_url": None,
        "error": None,
        "quota_exhausted": False,
        "playwright_fallback_hint": None,
    }

    async def _run() -> None:
        from pipeline.upload import upload as _upload
        rec_u = UPLOAD_JOBS[upload_id]
        try:
            channel_yaml = yaml.safe_load(cyp.read_text())
            script = json.loads(spp.read_text())
            # Best-effort raw lookup (matches pipeline.upload.upload_short shape)
            raw_path = spp.parent.parent / "raw" / f"{slug}.json"
            raw = json.loads(raw_path.read_text()) if raw_path.exists() else None
            result = await asyncio.to_thread(
                _upload.upload_short,
                project_root=repo_root,
                channel_yaml=channel_yaml,
                channel_dir=channel_dir,
                slug=slug,
                mp4_path=mp4_abs,
                script=script,
                raw=raw,
                privacy_override=payload.get("privacy"),
                force=bool(payload.get("force", False)),
                skip_critic=bool(payload.get("skip_critic", False)),
            )
            rec_u["video_id"] = (result or {}).get("video_id")
            rec_u["video_url"] = (result or {}).get("url") or (
                f"https://www.youtube.com/watch?v={rec_u['video_id']}"
                if rec_u.get("video_id") else None
            )
            rec_u["state"] = "done"
        except Exception as e:
            if _is_quota_exhausted_error(e):
                rec_u["state"] = "quota_exhausted"
                rec_u["quota_exhausted"] = True
                rec_u["error"] = (
                    "YouTube API quota exhausted (project-wide 10K/day pool). "
                    "Fall back to Studio UI: run /upload-via-playwright "
                    f"with mp4 = {mp4_abs}"
                )
                rec_u["playwright_fallback_hint"] = (
                    f"PYTHONPATH=. .venv/bin/python -m pipeline.cloud.skill_dispatch render "
                    f"--cmd <playwright-upload> -- --mp4 {mp4_abs} --slug {slug} "
                    f"# (Playwright server-side fallback is P3.5 follow-up)"
                )
            else:
                rec_u["state"] = "failed"
                rec_u["error"] = f"{type(e).__name__}: {e}"
        finally:
            rec_u["completed_at"] = time.time()

    asyncio.create_task(_run())
    return {"upload_id": upload_id, "state": "running"}


@app.get("/api/uploads/from_job/{upload_id}")
async def get_upload_job(upload_id: str) -> dict:
    rec = UPLOAD_JOBS.get(upload_id)
    if not rec:
        raise HTTPException(404, "upload job not found")
    out = dict(rec)
    out["elapsed_s"] = round(
        (rec["completed_at"] or time.time()) - rec["started_at"], 1
    )
    return out


@app.get("/api/uploads/from_job")
async def list_upload_jobs(limit: int = 20) -> dict:
    items = sorted(
        UPLOAD_JOBS.values(), key=lambda r: r.get("started_at", 0), reverse=True,
    )[:limit]
    return {"jobs": items}


# ---- Channel cron-drain endpoints (P4 — 2026-05-09) ----------------------
#
# Three filesystem-driven cron scripts (one per channel that has a
# rotating upload backlog) became website-driven endpoints. The schedule
# now lives in the website (control/scheduler.py); the dashboard surfaces
# next-up + last-run per channel; manual "drain now" triggers via the UI.
_CRON_DRAIN_SCRIPTS: dict[str, str] = {
    "mystoriesanimated": "scripts/upload_next.py",
    "cosmosdecoded":     "cosmosdecoded/scripts/cron_upload_daily.py",
    "historyrecapped":   "historyrecapped/scripts/cron_upload_one.py",
}


@app.post("/api/cron/drain")
async def trigger_cron_drain(payload: dict) -> dict:
    """Trigger one of the channel-specific upload-rotation scripts.

    Payload:
        channel: "mystoriesanimated" | "cosmosdecoded" | "historyrecapped"
        count:   int (default 1) — forwarded to upload_next.py as --count
                 where supported; ignored by single-shot scripts
        dry_run: bool (default false)
    """
    channel = (payload.get("channel") or "").strip()
    if channel not in _CRON_DRAIN_SCRIPTS:
        raise HTTPException(
            400,
            f"unknown channel {channel!r}; expected one of "
            f"{sorted(_CRON_DRAIN_SCRIPTS)}",
        )
    repo_root = Path(__file__).resolve().parent.parent
    script_rel = _CRON_DRAIN_SCRIPTS[channel]
    if not (repo_root / script_rel).exists():
        raise HTTPException(500, f"cron script missing: {script_rel}")

    CRON_JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    cron_id = uuid.uuid4().hex[:10]
    log_path = CRON_JOB_LOG_DIR / f"{cron_id}.log"
    cmd: list[str] = [
        os.fspath(repo_root / ".venv" / "bin" / "python"),
        script_rel,
    ]
    # upload_next.py supports --count + --dry-run; the others don't (per
    # cron_upload_daily / cron_upload_one). Pass them only when known-safe.
    if channel == "mystoriesanimated":
        if payload.get("count"):
            cmd += ["--count", str(int(payload["count"]))]
        if payload.get("dry_run"):
            cmd += ["--dry-run"]

    CRON_JOBS[cron_id] = {
        "cron_id": cron_id,
        "channel": channel,
        "cmd": cmd,
        "state": "running",
        "started_at": time.time(),
        "completed_at": None,
        "exit_code": None,
        "error": None,
        "log_path": str(log_path),
    }

    async def _run() -> None:
        rec = CRON_JOBS[cron_id]
        try:
            with open(log_path, "wb") as logfh:
                env = os.environ.copy()
                env["PYTHONPATH"] = str(repo_root) + (
                    os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
                )
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(repo_root),
                    env=env,
                    stdout=logfh,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                rec["pid"] = proc.pid
                await proc.wait()
            rec["exit_code"] = proc.returncode
            rec["state"] = "done" if proc.returncode == 0 else "failed"
        except Exception as e:
            rec["state"] = "failed"
            rec["error"] = f"{type(e).__name__}: {e}"
        finally:
            rec["completed_at"] = time.time()

    asyncio.create_task(_run())
    return {"cron_id": cron_id, "state": "running"}


@app.get("/api/cron/drain/{cron_id}")
async def get_cron_drain(cron_id: str, log_tail_kb: int = 8) -> dict:
    rec = CRON_JOBS.get(cron_id)
    if not rec:
        raise HTTPException(404, "cron job not found")
    out = dict(rec)
    out["elapsed_s"] = round(
        (rec["completed_at"] or time.time()) - rec["started_at"], 1
    )
    log_path = Path(rec["log_path"])
    if log_path.exists():
        nbytes = max(1, log_tail_kb) * 1024
        with open(log_path, "rb") as f:
            try: f.seek(-nbytes, os.SEEK_END)
            except OSError: f.seek(0)
            out["log_tail"] = f.read().decode("utf-8", errors="replace")
    else:
        out["log_tail"] = ""
    return out


@app.get("/api/cron/drain")
async def list_cron_drain(channel: str | None = None, limit: int = 20) -> dict:
    items = list(CRON_JOBS.values())
    if channel:
        items = [r for r in items if r.get("channel") == channel]
    items = sorted(items, key=lambda r: r.get("started_at", 0), reverse=True)[:limit]
    return {
        "channels": list(_CRON_DRAIN_SCRIPTS.keys()),
        "jobs": [
            {k: v for k, v in r.items() if k not in ("log_path",)}
            for r in items
        ],
    }


@app.get("/api/jobs/{job_id}")
async def job_snapshot(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        # Fall through to skill-submitted render jobs (SCRIPT_JOBS) so
        # the new web-next render-detail page (`/app/render/<id>`) can
        # poll a single endpoint regardless of submission origin.
        rec = SCRIPT_JOBS.get(job_id)
        if rec:
            return _script_job_to_snapshot(rec)
        # Final fall through: control-plane jobs (`/api/render` and chat
        # → confirm enqueue here). Without this tier, a chat-confirmed
        # render is unreachable via GET /api/jobs/{id} even though the
        # control router would have found it — the @app.get registration
        # at this line shadows the control router for the same path.
        # Wrap in try/except so a Firestore outage doesn't turn legacy
        # 404s into 500s; only surface 502 for IDs that LOOK control-
        # plane (32-hex), so a typo'd legacy ID still 404s cleanly.
        try:
            from control.core import jobs as control_jobs  # noqa: PLC0415
            doc = control_jobs.get_job(job_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "control-plane job lookup failed for %s", job_id, exc_info=True
            )
            if _CONTROL_JOB_ID_RE.fullmatch(job_id):
                raise HTTPException(
                    status_code=502,
                    detail=f"control-plane job lookup failed: {e}",
                )
            doc = None
        if doc:
            return _control_job_to_snapshot(job_id, doc)
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
async def job_short(job_id: str):
    job = JOBS.get(job_id)
    if job and job.out_mp4 and job.out_mp4.exists():
        return FileResponse(
            str(job.out_mp4),
            media_type="video/mp4",
            filename=f"{job.slug}.mp4",
        )
    # Fall through to skill-submitted render jobs (SCRIPT_JOBS) so the
    # new web-next render-detail page can use a single mp4 URL
    # regardless of submission origin. mp4_path is either a local
    # filesystem path (local backend) or a `gs://...` URI (cloudrun
    # backend); we serve the former directly and 302 to a v4-signed
    # URL for the latter — same shape as /api/jobs/from_script/{id}/mp4.
    rec = SCRIPT_JOBS.get(job_id)
    if rec is None:
        raise HTTPException(404, "mp4 not ready")
    return _serve_script_job_mp4(rec)


def _serve_script_job_mp4(rec: dict):
    mp4 = rec.get("mp4_path")
    if not mp4:
        raise HTTPException(404, "mp4 not yet available for this job")
    if mp4.startswith("gs://"):
        try:
            from urllib.parse import urlparse  # noqa: PLC0415
            from google.cloud import storage  # noqa: PLC0415
            p = urlparse(mp4)
            client = storage.Client(project=CLOUDRUN_JOB_PROJECT)
            blob = client.bucket(p.netloc).blob(p.path.lstrip("/"))
            url = blob.generate_signed_url(
                version="v4",
                expiration=900,  # 15 min
                method="GET",
            )
            return RedirectResponse(url, status_code=302)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"failed to sign GCS URL: {e}")
    if not Path(mp4).exists():
        raise HTTPException(404, "mp4 not yet available for this job")
    return FileResponse(mp4, media_type="video/mp4", filename=Path(mp4).name)


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
    from pipeline.upload import upload as up

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
    from pipeline.upload import upload as up

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
        from pipeline.utils import thumbnails as thumb_mod
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
    from pipeline.utils import thumbnails as thumb_mod

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
        from pipeline.upload import upload as up
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
    from pipeline.upload import upload as up
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


# ---- YouTube OAuth re-auth (cross-engagement support) -------------------
#
# pipeline/cross_engage.py likes + subscribes from every sibling channel
# on every upload. That requires each channel's OAuth token to (a) carry
# the full `youtube` scope and (b) include a refresh_token so the laptop
# agent can run non-interactively. These routes surface auth health on
# the dashboard and let the operator kick off a re-auth flow per account
# without dropping to a terminal.

# In-memory map of account → in-flight reauth subprocess + captured URL.
_REAUTH_JOBS: dict[str, dict[str, Any]] = {}


@app.get("/api/youtube/auth/status")
async def youtube_auth_status() -> dict:
    """Per-account OAuth health: token present? has refresh_token? has all SCOPES?"""
    from pipeline.upload import upload as _upload
    from pipeline.research import cross_engage as _ce

    accounts = _ce.list_sibling_accounts()
    # Audit S1.9 — use public load_registry alias rather than the
    # underscore-prefixed _load_registry, which is the in-module
    # entry point.
    registry = _ce.load_registry()
    out = []
    for a in accounts:
        s = _upload.inspect_token_status(a)
        entry = registry.get(a) or {}
        s["channel_id"] = entry.get("channel_id")
        s["channel_title"] = entry.get("title")
        job = _REAUTH_JOBS.get(a)
        if job:
            s["reauth_in_flight"] = True
            s["reauth_url"] = job.get("url")
            s["reauth_started_at"] = job.get("started_at")
        out.append(s)
    return {"accounts": out, "scopes": _upload.SCOPES}


@app.post("/api/youtube/auth/start/{account}")
async def youtube_auth_start(account: str) -> dict:
    """Kick off the browser OAuth flow for ``account`` in a subprocess.

    The subprocess binds localhost:8089 and prints the URL to stdout; we
    parse it and return it so the dashboard can show the operator a
    clickable link. The caller then opens it, signs into the right
    Google account, clicks Allow, and the localhost callback completes
    the flow inside the subprocess.

    Idempotent on a per-account basis — calling twice while a flow is
    in flight returns the existing URL instead of starting a duplicate
    (which would just hit "Address already in use" anyway).

    **Audit T1.20 — Cloud Run hard-stop.** This endpoint uses a
    blocking subprocess.Popen + threading.Thread to spawn a
    local-server OAuth flow on localhost:8089. That has THREE
    independent failures on Cloud Run:

      - no display, so the browser-based OAuth dance can't complete;
      - no localhost callback reachable from the operator's browser;
      - the Cloud Run revision is per-request (default scaling), so
        the subprocess spawned by request N is killed before request
        N+1 (the poll) lands.

    On Cloud Run we now refuse with a clear 501 pointing to the
    proper web-OAuth endpoint at ``/api/oauth/start?account=<account>``
    (the public-callback flow added in :mod:`control.routes.oauth_web_routes`)
    so the operator gets routed to the working path instead of
    hanging on a process that's about to die.
    """
    import re as _re
    import subprocess
    import threading
    from datetime import datetime, timezone

    from pipeline.research import cross_engage as _ce

    if os.environ.get("K_SERVICE"):
        raise HTTPException(
            status_code=501,
            detail=(
                f"localhost-OAuth flow does not work on Cloud Run "
                f"(no display, no callback host). Use the web OAuth "
                f"flow instead: GET /api/oauth/start?account={account} "
                f"(see control/routes/oauth_web_routes.py)."
            ),
        )

    if account not in _ce.list_sibling_accounts() and account not in (
        "default",
    ):
        raise HTTPException(404, f"unknown account {account!r}")

    job = _REAUTH_JOBS.get(account)
    if job and job.get("proc") and job["proc"].poll() is None:
        return {
            "account": account,
            "status": "in_flight",
            "url": job.get("url"),
            "started_at": job.get("started_at"),
        }

    py = str(PYTHON_BIN if PYTHON_BIN.exists() else "python3")
    proc = subprocess.Popen(
        [
            py,
            "-c",
            "from pipeline.upload.upload import authenticate; "
            f"authenticate({account!r}, interactive=True)",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    rec: dict[str, Any] = {
        "proc": proc,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "url": None,
        "log": [],
        "completed": False,
        "ok": None,
    }
    _REAUTH_JOBS[account] = rec

    url_re = _re.compile(r"https://accounts\.google\.com/o/oauth2/auth\?\S+")

    def _drain() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                rec["log"].append(line.rstrip())
                if rec["url"] is None:
                    m = url_re.search(line)
                    if m:
                        rec["url"] = m.group(0)
        finally:
            proc.wait()
            rec["completed"] = True
            rec["ok"] = proc.returncode == 0

    threading.Thread(target=_drain, daemon=True).start()

    # Wait briefly for the URL to appear in stdout (max 8s).
    for _ in range(80):
        if rec.get("url") or rec.get("completed"):
            break
        await asyncio.sleep(0.1)

    return {
        "account": account,
        "status": "started" if rec.get("url") else ("done" if rec.get("completed") else "starting"),
        "url": rec.get("url"),
        "started_at": rec["started_at"],
        "ok": rec.get("ok"),
    }


@app.get("/api/youtube/auth/poll/{account}")
async def youtube_auth_poll(account: str) -> dict:
    """Check whether the in-flight OAuth flow for ``account`` has finished."""
    job = _REAUTH_JOBS.get(account)
    if not job:
        return {"account": account, "status": "no_job"}
    proc = job.get("proc")
    if proc is None:
        return {"account": account, "status": "no_proc"}
    if proc.poll() is None:
        return {
            "account": account,
            "status": "in_flight",
            "url": job.get("url"),
            "started_at": job["started_at"],
        }
    return {
        "account": account,
        "status": "completed" if job.get("ok") else "failed",
        "url": job.get("url"),
        "started_at": job["started_at"],
        "tail": job.get("log", [])[-20:],
    }


@app.post("/api/youtube/cross_engage/subscribe_all")
async def youtube_cross_engage_subscribe_all() -> dict:
    """Run the one-time cross-subscribe pass across all sibling pairs.

    Each successful pair is idempotent at the YouTube end (already-subscribed
    is treated as success), so this is safe to re-run.
    """
    from pipeline.research import cross_engage as _ce

    results = await asyncio.to_thread(_ce.subscribe_all_pairs)
    return {"results": results}


# ---------------------------------------------------------------------------
# Control router include block (Phase 4 — 2026-05-10)
# ---------------------------------------------------------------------------
#
# Mounted AFTER all of web's own @app.* decorators so that overlapping
# paths (/api/jobs/from_script, /api/research/*, /api/dashboard/*,
# /api/voices/*, etc.) resolve to web's established handler. Control's
# routes that DON'T overlap (/agent/*, /api/scheduler/*, /api/state/*,
# channels, niches v2, burners, ...) attach cleanly. The chat router is
# intentionally absent — retired in Phase 1.
#
app.include_router(_control_agent_router)
app.include_router(_control_auth_pin_router)
app.include_router(_control_burner_router)
app.include_router(_control_channels_router)
app.include_router(_control_cloud_router)
app.include_router(_control_clone_video_router)
app.include_router(_control_critique_router)
app.include_router(_control_dashboard_router)
app.include_router(_control_discover_router)
app.include_router(_control_music_router)
app.include_router(_control_niche_router)
app.include_router(_control_niche_specs_router)
app.include_router(_control_oauth_web_router)
app.include_router(_control_render_router)
app.include_router(_control_scheduler_router)
app.include_router(_control_script_jobs_router)
app.include_router(_control_state_router)
app.include_router(_control_telemetry_router)
app.include_router(_control_voices_router)
