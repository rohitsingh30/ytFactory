"""Voice catalog + sample audio + clone-upload for the studio create flow.

GET  /api/voices/catalog            — list known voices (built-in + user clones)
GET  /api/voices/sample/{key}.wav   — stream the per-voice reference clip
POST /api/voices/clone              — upload an audio file → become a new voice

Voice storage layout (one voice = one directory):

    pipeline/voice_refs/<name>/ref.wav    — 24 kHz mono reference clip
    pipeline/voice_refs/<name>/ref.txt    — spoken transcript (optional)

User clones land under ``pipeline/voice_refs/clones/<slug>/`` so they
don't collide with curated voices in ``catalog.yaml``. The endpoint
ffmpeg-converts arbitrary upload audio (mp3/m4a/wav/ogg) to the
required 24 kHz mono WAV format the rendering pipeline expects.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/voices")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VOICE_REFS_DIR = PROJECT_ROOT / "pipeline" / "voice_refs"
CLONES_DIR = VOICE_REFS_DIR / "clones"
CATALOG_PATH = VOICE_REFS_DIR / "catalog.yaml"
# Pre-generated Kokoro preset samples — 21 voices the legacy UI shipped with.
KOKORO_SAMPLES_DIR = PROJECT_ROOT / "web" / "static" / "voice_samples"

# 25 MB upload cap — enough for ~5 minutes of WAV. Real refs are 5-15s.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm"}


class VoiceInfo(BaseModel):
    key: str
    label: str
    language: str
    gender: str
    style: str
    sample_url: str | None = None
    notes: str | None = None
    is_clone: bool = False
    duration_s: float | None = None
    # Optional — drives the tabbed picker UI on the create flow.
    category: str = "library"  # library | famous | your-clones
    # Use-case tags for filtering (e.g. ["documentary", "narrator"]).
    use_cases: list[str] = []
    # Provider hint: "human" | "kokoro" | "edge" | "openai" | "elevenlabs".
    # Drives the "Show TTS presets" toggle so the default view is human-only.
    provider: str = "human"
    # Tone tags derived from the style/label string (deep / warm / intense / …)
    # — populated server-side; the picker shows them as filter pills.
    tones: list[str] = []


class FamousVoiceTemplate(BaseModel):
    """A celebrity / famous-voice template the user can clone with one click."""

    key: str
    label: str
    language: str
    description: str
    hint: str
    initial_letters: str  # 2-letter monogram for the avatar
    # The closest in-catalog voice we'd recommend as a stand-in. The famous
    # voice card plays *this* voice's preview when the user hits ▶, with a
    # clear "(closest match)" label, since we can't legally ship the real
    # celebrity audio. Click "Clone" to upload a clip of the actual voice.
    closest_voice_key: str | None = None


def _closest_voice_url(famous_key: str) -> str | None:
    """Find the recommended stand-in's playable preview/sample URL."""
    # Walk the FAMOUS_VOICES list once and look up the closest_voice_key
    for f in FAMOUS_VOICES:
        if f.key == famous_key and f.closest_voice_key:
            return _voice_url(f.closest_voice_key)
    return None


# 16 well-known voices grouped roughly by region. The dialog opens with
# `name` pre-filled and the user supplies the audio. Hand-curated; safe
# to grow over time. Order is intentional (most-asked first).
FAMOUS_VOICES: list[FamousVoiceTemplate] = [
    # English-speaking — closest_voice_key picks the most-similar Library voice
    # so the ▶ button on each famous card plays a "in this ballpark" preview
    # rendered through our pipeline. Click "Clone" to upload the actual voice.
    FamousVoiceTemplate(
        key="morgan-freeman",
        label="Morgan Freeman",
        language="en",
        description="Calm, gravelly, omniscient narrator",
        hint="Search YouTube for 'Morgan Freeman March of the Penguins narration' — extract a 6-10s clean line.",
        initial_letters="MF",
        closest_voice_key="english-male-history-sleep-narrator",
    ),
    FamousVoiceTemplate(
        key="david-attenborough",
        label="David Attenborough",
        language="en",
        description="Documentary, measured, reverent",
        hint="BBC Planet Earth has many isolated narration segments on YouTube — pick a 6-10s line.",
        initial_letters="DA",
        closest_voice_key="tifo_devine",
    ),
    FamousVoiceTemplate(
        key="james-earl-jones",
        label="James Earl Jones",
        language="en",
        description="Deep baritone, regal authority",
        hint="Try the original Lion King teaser narration ('Everything the light touches…') for a clean 8s clip.",
        initial_letters="JJ",
        closest_voice_key="english-male-history-sleep-narrator",
    ),
    FamousVoiceTemplate(
        key="oprah-winfrey",
        label="Oprah Winfrey",
        language="en",
        description="Warm, conversational interviewer",
        hint="The 'Oprah's Master Class' YouTube series has 6-10s monologues that work well.",
        initial_letters="OW",
        closest_voice_key="sarah",
    ),
    FamousVoiceTemplate(
        key="barack-obama",
        label="Barack Obama",
        language="en",
        description="Measured cadence, oratorical pauses",
        hint="The C-SPAN archive has clean PD speech audio — 8-12s of his 2008 victory speech is ideal.",
        initial_letters="BO",
        closest_voice_key="web-mlk-dream",
    ),
    FamousVoiceTemplate(
        key="winston-churchill",
        label="Winston Churchill",
        language="en",
        description="Wartime broadcast, gravelly authority",
        hint="The 'We shall fight on the beaches' (1940) recording is public domain in many jurisdictions.",
        initial_letters="WC",
        closest_voice_key="web-mlk-dream",
    ),
    FamousVoiceTemplate(
        key="don-lafontaine",
        label="Don LaFontaine",
        language="en",
        description="\"In a world…\" trailer baritone",
        hint="The official 'Five Guys in a Limo' supercut on YouTube is a great 8s sample.",
        initial_letters="DL",
        closest_voice_key="sports_male_intense",
    ),
    FamousVoiceTemplate(
        key="emma-watson",
        label="Emma Watson",
        language="en",
        description="UN speech cadence, articulate, warm",
        hint="The 2014 HeForShe UN speech has many 6-10s clean monologue moments.",
        initial_letters="EW",
        closest_voice_key="sarah",
    ),
    # Hindi-speaking
    FamousVoiceTemplate(
        key="amitabh-bachchan",
        label="Amitabh Bachchan",
        language="hi",
        description="Bollywood baritone, theatrical authority",
        hint="His KBC ('Computer-ji') opening monologue clips are 6-10s and dialogue-clean.",
        initial_letters="AB",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="harish-bhimani",
        label="Harish Bhimani",
        language="hi",
        description="Mahabharat narrator — 'main samay hoon'",
        hint="The opening line of the 1988 BR Chopra Mahabharat is the canonical 8s clip.",
        initial_letters="HB",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="lata-mangeshkar",
        label="Lata Mangeshkar",
        language="hi",
        description="Iconic playback singer",
        hint="An interview snippet works better than a song — search for a 1990s interview clip.",
        initial_letters="LM",
        closest_voice_key="hindi-female-storyteller-calm",
    ),
    FamousVoiceTemplate(
        key="naseeruddin-shah",
        label="Naseeruddin Shah",
        language="hi",
        description="Theatrical, measured, baritone",
        hint="His audiobook narrations on Audible India have 6-10s passages that work well.",
        initial_letters="NS",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="ratan-tata",
        label="Ratan Tata",
        language="en",
        description="Indian-English elder statesman cadence",
        hint="Any IIT / IIM convocation speech archive on YouTube has clean 8-12s passages.",
        initial_letters="RT",
        closest_voice_key="english-male-history-sleep-narrator",
    ),
    FamousVoiceTemplate(
        key="jawaharlal-nehru",
        label="Jawaharlal Nehru",
        language="en",
        description="\"Tryst with Destiny\" oratory",
        hint="The 1947 'Tryst with Destiny' AIR recording is in the public domain.",
        initial_letters="JN",
        closest_voice_key="web-mlk-dream",
    ),
    FamousVoiceTemplate(
        key="mahatma-gandhi",
        label="Mahatma Gandhi",
        language="en",
        description="Soft, measured, sermon-like",
        hint="The 1931 'Spiritual Message' recording (BBC archive) is public domain.",
        initial_letters="MG",
        closest_voice_key="english-male-history-sleep-narrator",
    ),
    FamousVoiceTemplate(
        key="apj-abdul-kalam",
        label="A. P. J. Abdul Kalam",
        language="en",
        description="Inspirational, scientist-poet",
        hint="The Anna University 'You are Born to Blossom' lecture on YouTube is gold.",
        initial_letters="AK",
        closest_voice_key="english-male-history-sleep-narrator",
    ),
    # More Hindi-speaking
    FamousVoiceTemplate(
        key="shahrukh-khan",
        label="Shahrukh Khan",
        language="hi",
        description="Romantic Bollywood baritone",
        hint="His TED India 2017 talk has 6-10s clean monologue passages.",
        initial_letters="SK",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="aamir-khan",
        label="Aamir Khan",
        language="hi",
        description="Earnest, articulate Hindi diction",
        hint="The Satyamev Jayate intro monologues are great 8-12s clips.",
        initial_letters="AmK",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="pankaj-tripathi",
        label="Pankaj Tripathi",
        language="hi",
        description="Slow Bihari cadence, philosophical",
        hint="His Mirzapur 'Kaaleen Bhaiya' monologues are widely available.",
        initial_letters="PT",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="kishore-kumar",
        label="Kishore Kumar",
        language="hi",
        description="Playful playback, vintage Bollywood",
        hint="Use a non-singing interview clip for prosody — radio archives have these.",
        initial_letters="KK",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="anupam-kher",
        label="Anupam Kher",
        language="hi",
        description="Booming, theatrical, declamatory",
        hint="His motivational 'My Soul, My Self' YouTube series has clean 6-10s lines.",
        initial_letters="AnK",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="manoj-bajpayee",
        label="Manoj Bajpayee",
        language="hi",
        description="Intense, gravelly, internalised",
        hint="The Family Man monologues on Prime work — extract a 6-10s isolated line.",
        initial_letters="MB",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
    FamousVoiceTemplate(
        key="ravish-kumar",
        label="Ravish Kumar",
        language="hi",
        description="Calm news-anchor cadence, slight lilt",
        hint="His NDTV / Magsaysay-acceptance speeches have measured 8-12s passages.",
        initial_letters="RK",
        closest_voice_key="hindi-female-iitm-anchor",
    ),
    FamousVoiceTemplate(
        key="amrish-puri",
        label="Amrish Puri",
        language="hi",
        description="Iconic villain baritone, 'Mogambo'",
        hint="Mr. India ('Mogambo khush hua') — the line itself is too short, find an interview.",
        initial_letters="AP",
        closest_voice_key="hindi-male-anurag-vardaan",
    ),
]


@router.get("/famous")
async def list_famous_voices() -> dict:
    """Templates the UI shows in the 'Famous voices' tab.

    Each card includes a ``closest_voice_url`` that the picker plays as a
    "voice in this ballpark" preview (rendered through our pipeline using
    the closest in-catalog reference). For the actual celebrity voice the
    user clones with their own clip via the dialog.
    """
    out = []
    for v in FAMOUS_VOICES:
        d = v.model_dump()
        d["closest_voice_url"] = _voice_url(v.closest_voice_key) if v.closest_voice_key else None
        out.append(d)
    return {"templates": out}


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "voice"


def _gender_from_register(register: str) -> str:
    r = register.lower()
    if "female" in r:
        return "female"
    if "male" in r:
        return "male"
    return ""


# Tone heuristics — derive a small set of mood tags from the free-text style
# string so the picker can offer "Deep · Warm · Intense · Calm" filter chips.
_TONE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "deep":      ("deep", "baritone", "low", "bass", "gravelly"),
    "warm":      ("warm", "soft", "calm", "intimate", "friendly", "gentle", "warming"),
    "intense":   ("intense", "dramatic", "declamatory", "aggressive", "passionate", "epic"),
    "bright":    ("bright", "energetic", "cheerful", "young", "playful", "lively", "modern"),
    "measured":  ("measured", "documentary", "neutral", "narrator", "audiobook", "calm", "podcast"),
    "expressive":("expressive", "emotional", "theatrical", "melodic", "operatic"),
    "raspy":     ("raspy", "gravelly", "husky", "rough"),
    "british":   ("british", "bbc", "english"),
    "indian":    ("indian", "hindi", "iit"),
}


def _derive_tones(style: str, label: str) -> list[str]:
    """Surface mood tags from a free-text style string for the filter chips."""
    text = (style + " " + label).lower()
    return [tone for tone, kws in _TONE_KEYWORDS.items() if any(k in text for k in kws)]


def _voice_path(key: str) -> Path | None:
    """Resolve a voice key to its ref.wav, checking every layout."""
    candidates = [
        # Built-in catalog layout
        VOICE_REFS_DIR / key / "ref.wav",
        # User clone layout
        CLONES_DIR / key / "ref.wav",
        # Web-fetched human voices
        VOICE_REFS_DIR / "web" / key / "ref.wav",
        # Legacy flat layout (sarah.wav at the root)
        VOICE_REFS_DIR / f"{key}.wav",
        # Kokoro pre-baked preview samples (21 voices)
        KOKORO_SAMPLES_DIR / f"{key}.wav",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _preview_path(key: str) -> Path | None:
    """Resolve a voice key to its renderer-synthesized preview WAV.

    Preview WAVs ("how this voice will sound when our pipeline uses it") sit
    next to the reference under the same dir layout. For flat WAVs we
    write previews to ``pipeline/voice_refs/_previews/<key>.wav``.
    """
    candidates = [
        VOICE_REFS_DIR / key / "preview.wav",
        CLONES_DIR / key / "preview.wav",
        VOICE_REFS_DIR / "web" / key / "preview.wav",
        VOICE_REFS_DIR / "_previews" / f"{key}.wav",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _voice_url(key: str) -> str | None:
    """Sample URL — the renderer-rendered preview if present, else the raw ref."""
    if _preview_path(key) is not None:
        # Both preview and ref get served by the same /sample endpoint with
        # ?p=1 to disambiguate which one to read.
        return f"/api/voices/sample/{key}.wav?preview=1"
    return f"/api/voices/sample/{key}.wav" if _voice_path(key) else None


def _ref_url(key: str) -> str | None:
    """Always returns the raw reference URL (used by the clone-detail UI)."""
    return f"/api/voices/sample/{key}.wav" if _voice_path(key) else None


def _load_catalog_voices() -> list[VoiceInfo]:
    """Voices declared in pipeline/voice_refs/catalog.yaml."""
    out: list[VoiceInfo] = []
    if not CATALOG_PATH.exists():
        return out
    try:
        raw = yaml.safe_load(CATALOG_PATH.read_text()) or {}
    except Exception:  # noqa: BLE001
        logger.warning("failed to parse %s", CATALOG_PATH, exc_info=True)
        return out
    for key, body in (raw.get("voices") or {}).items():
        register = body.get("register", "")
        out.append(VoiceInfo(
            key=key,
            label=key.replace("-", " ").title().replace("Iitm", "IITM"),
            language=body.get("language", ""),
            gender=_gender_from_register(register),
            style=register or body.get("notes", ""),
            sample_url=_voice_url(key),
            notes=body.get("notes") or None,
            duration_s=body.get("duration_s"),
            provider="human",
            use_cases=list(body.get("use_cases") or []),
        ))
    return out


def _load_clone_voices() -> list[VoiceInfo]:
    """Voices uploaded via /api/voices/clone."""
    out: list[VoiceInfo] = []
    if not CLONES_DIR.exists():
        return out
    for child in sorted(CLONES_DIR.iterdir()):
        ref = child / "ref.wav"
        meta = child / "meta.yaml"
        if not ref.exists() or not child.is_dir():
            continue
        body = {}
        if meta.exists():
            try:
                body = yaml.safe_load(meta.read_text()) or {}
            except Exception:  # noqa: BLE001
                pass
        out.append(VoiceInfo(
            key=child.name,
            label=body.get("label") or child.name.replace("-", " ").title(),
            language=body.get("language", "en"),
            gender=body.get("gender", ""),
            style=body.get("style") or "User clone",
            sample_url=_voice_url(child.name),
            notes=body.get("notes") or "Cloned from upload.",
            is_clone=True,
            duration_s=body.get("duration_s"),
            provider="human",
            use_cases=["clone"],
        ))
    return out


def _load_web_voices() -> list[VoiceInfo]:
    """Voices fetched from public-domain web sources (LibriVox, archive.org).

    Each entry lives under ``pipeline/voice_refs/web/<key>/{ref.wav, meta.yaml}``
    and is downloaded once by ``scripts/fetch_web_voices.py``.
    """
    web_dir = VOICE_REFS_DIR / "web"
    if not web_dir.exists():
        return []
    out: list[VoiceInfo] = []
    for child in sorted(web_dir.iterdir()):
        ref = child / "ref.wav"
        meta = child / "meta.yaml"
        if not ref.exists() or not child.is_dir():
            continue
        body = {}
        if meta.exists():
            try:
                body = yaml.safe_load(meta.read_text()) or {}
            except Exception:  # noqa: BLE001
                pass
        out.append(VoiceInfo(
            key=child.name,
            label=body.get("label") or child.name.replace("-", " ").title(),
            language=body.get("language", "en"),
            gender=body.get("gender", ""),
            style=body.get("style") or "Web-sourced human",
            sample_url=_voice_url(child.name),
            notes=body.get("notes") or body.get("source") or None,
            duration_s=body.get("duration_s"),
            provider="human",
            use_cases=list(body.get("use_cases") or []),
        ))
    return out


# ---- Kokoro preset metadata (21 pre-baked sample WAVs in web/static/voice_samples/)
# Naming: {region}{gender}_{name}  →  am=American Male, af=American Female,
# bm=British Male, bf=British Female, hm=Hindi Male, hf=Hindi Female,
# im=Italian Male
_KOKORO_META: dict[str, tuple[str, str, str, str]] = {
    # key            label                          language gender style
    "af_aoede":   ("Aoede · American female",        "en", "female", "calm, melodic"),
    "af_bella":   ("Bella · American female",        "en", "female", "warm, conversational"),
    "af_heart":   ("Heart · American female",        "en", "female", "expressive, emotional"),
    "af_nicole":  ("Nicole · American female",       "en", "female", "narrator, measured"),
    "af_nova":    ("Nova · American female",         "en", "female", "bright, modern"),
    "af_sarah":   ("Sarah · American female",        "en", "female", "documentary, soft"),
    "am_adam":    ("Adam · American male",           "en", "male",   "neutral newsreader"),
    "am_eric":    ("Eric · American male",           "en", "male",   "podcast host, friendly"),
    "am_liam":    ("Liam · American male",           "en", "male",   "young, conversational"),
    "am_michael": ("Michael · American male",        "en", "male",   "Reddit storyteller cadence"),
    "am_onyx":    ("Onyx · American male",           "en", "male",   "deep, gravelly trailer"),
    "am_puck":    ("Puck · American male",           "en", "male",   "playful, animated"),
    "bf_alice":   ("Alice · British female",         "en", "female", "BBC documentary"),
    "bf_emma":    ("Emma · British female",          "en", "female", "audiobook narrator"),
    "bm_george":  ("George · British male",          "en", "male",   "wartime baritone"),
    "bm_lewis":   ("Lewis · British male",           "en", "male",   "documentary, soft"),
    "hf_alpha":   ("Alpha · Hindi female",           "hi", "female", "warm storyteller"),
    "hf_beta":    ("Beta · Hindi female",            "hi", "female", "news anchor, crisp"),
    "hm_omega":   ("Omega · Hindi male",             "hi", "male",   "audiobook reader"),
    "hm_psi":     ("Psi · Hindi male",               "hi", "male",   "deep, calm"),
    "im_nicola":  ("Nicola · Italian male",          "it", "male",   "expressive, theatrical"),
}

# Curated extras that aren't in catalog.yaml — flat WAV refs in pipeline/voice_refs/
# AND cloud-TTS preset names (Edge / OpenAI / ElevenLabs) that surface in the
# picker but get TTS-synthesised on first render.
_EXTRA_VOICES: list[VoiceInfo] = [
    # --- Real WAV refs at pipeline/voice_refs/<key>.wav (HUMAN voices) -------
    # Curated for CLEAN audio only — anything with crowd noise / music bed /
    # broadcast-mix has been removed (peter_drury + sports_male_intense were
    # pulled 2026-05-09 because they're broadcast feeds with stadium crowd).
    VoiceInfo(
        key="sarah",
        label="Sarah · documentary",
        language="en",
        gender="female",
        style="warm, measured, narrator",
        notes="Default English voice for MyStoriesAnimated and most channels.",
        provider="human",
        use_cases=["documentary", "narrator", "storyteller"],
    ),
    VoiceInfo(
        key="michael",
        label="Michael · AITA narrator",
        language="en",
        gender="male",
        style="Reddit storyteller, slightly aggrieved",
        provider="human",
        use_cases=["storyteller", "conversational"],
    ),
    VoiceInfo(
        key="tifo_devine",
        label="Joe Devine · Tifo Football",
        language="en",
        gender="male",
        style="tactical analyst, measured British",
        notes="Tifo Football's signature explainer voice — clean studio recording.",
        provider="human",
        use_cases=["sports", "documentary", "explainer"],
    ),
    # --- Edge-TTS cloud presets (TTS — no WAV; synthesised on first render) --
    VoiceInfo(key="edge:en-US-AriaNeural",     label="Aria · Microsoft Edge",      language="en", gender="female", style="cheerful, customer-service", provider="edge", use_cases=["conversational", "customer-service"]),
    VoiceInfo(key="edge:en-US-GuyNeural",      label="Guy · Microsoft Edge",       language="en", gender="male",   style="friendly, conversational",   provider="edge", use_cases=["conversational"]),
    VoiceInfo(key="edge:en-US-JennyNeural",    label="Jenny · Microsoft Edge",     language="en", gender="female", style="versatile assistant",         provider="edge", use_cases=["conversational"]),
    VoiceInfo(key="edge:en-US-DavisNeural",    label="Davis · Microsoft Edge",     language="en", gender="male",   style="news anchor",                  provider="edge", use_cases=["news"]),
    VoiceInfo(key="edge:en-US-AndrewNeural",   label="Andrew · Microsoft Edge",    language="en", gender="male",   style="podcast host",                 provider="edge", use_cases=["podcast", "conversational"]),
    VoiceInfo(key="edge:en-US-EmmaNeural",     label="Emma · Microsoft Edge",      language="en", gender="female", style="energetic announcer",          provider="edge", use_cases=["news", "announcer"]),
    VoiceInfo(key="edge:en-US-BrianNeural",    label="Brian · Microsoft Edge",     language="en", gender="male",   style="warm narrator",                provider="edge", use_cases=["narrator"]),
    VoiceInfo(key="edge:en-GB-RyanNeural",     label="Ryan · British male",        language="en", gender="male",   style="British, formal",              provider="edge", use_cases=["news", "documentary"]),
    VoiceInfo(key="edge:en-GB-SoniaNeural",    label="Sonia · British female",     language="en", gender="female", style="British, measured",            provider="edge", use_cases=["documentary", "narrator"]),
    VoiceInfo(key="edge:en-GB-LibbyNeural",    label="Libby · British female",     language="en", gender="female", style="British, warm",                provider="edge", use_cases=["narrator"]),
    VoiceInfo(key="edge:en-AU-NatashaNeural",  label="Natasha · Australian",       language="en", gender="female", style="Australian, friendly",         provider="edge", use_cases=["conversational"]),
    VoiceInfo(key="edge:en-AU-WilliamNeural",  label="William · Australian",       language="en", gender="male",   style="Australian, casual",           provider="edge", use_cases=["conversational"]),
    VoiceInfo(key="edge:en-IE-EmilyNeural",    label="Emily · Irish",              language="en", gender="female", style="Irish lilt",                   provider="edge", use_cases=["narrator"]),
    VoiceInfo(key="edge:en-IN-NeerjaNeural",   label="Neerja · Indian English",    language="en", gender="female", style="Indian English, warm",         provider="edge", use_cases=["narrator", "news"]),
    VoiceInfo(key="edge:en-IN-PrabhatNeural",  label="Prabhat · Indian English",   language="en", gender="male",   style="Indian English, formal",       provider="edge", use_cases=["news", "documentary"]),
    VoiceInfo(key="edge:hi-IN-SwaraNeural",    label="Swara · Microsoft Edge",     language="hi", gender="female", style="versatile assistant",          provider="edge", use_cases=["conversational"]),
    VoiceInfo(key="edge:hi-IN-MadhurNeural",   label="Madhur · Microsoft Edge",    language="hi", gender="male",   style="news anchor",                  provider="edge", use_cases=["news"]),
    VoiceInfo(key="openai:alloy",   label="Alloy · OpenAI TTS",      language="en", gender="female", style="neutral, balanced",  provider="openai", use_cases=["narrator"]),
    VoiceInfo(key="openai:echo",    label="Echo · OpenAI TTS",       language="en", gender="male",   style="conversational",      provider="openai", use_cases=["conversational"]),
    VoiceInfo(key="openai:fable",   label="Fable · OpenAI TTS",      language="en", gender="male",   style="British storyteller", provider="openai", use_cases=["storyteller", "narrator"]),
    VoiceInfo(key="openai:onyx",    label="Onyx · OpenAI TTS",       language="en", gender="male",   style="deep, authoritative", provider="openai", use_cases=["trailer", "narrator"]),
    VoiceInfo(key="openai:nova",    label="Nova · OpenAI TTS",       language="en", gender="female", style="bright, energetic",   provider="openai", use_cases=["conversational"]),
    VoiceInfo(key="openai:shimmer", label="Shimmer · OpenAI TTS",    language="en", gender="female", style="warm, friendly",      provider="openai", use_cases=["conversational"]),
    VoiceInfo(key="elevenlabs:rachel",  label="Rachel · ElevenLabs",  language="en", gender="female", style="calm, narration",     provider="elevenlabs", use_cases=["narrator"]),
    VoiceInfo(key="elevenlabs:adam",    label="Adam · ElevenLabs",    language="en", gender="male",   style="deep, narrator",      provider="elevenlabs", use_cases=["narrator", "trailer"]),
    VoiceInfo(key="elevenlabs:antoni",  label="Antoni · ElevenLabs",  language="en", gender="male",   style="well-rounded",        provider="elevenlabs", use_cases=["conversational", "narrator"]),
    VoiceInfo(key="elevenlabs:bella",   label="Bella · ElevenLabs",   language="en", gender="female", style="soft, narration",     provider="elevenlabs", use_cases=["narrator", "asmr"]),
    VoiceInfo(key="elevenlabs:domi",    label="Domi · ElevenLabs",    language="en", gender="female", style="strong, confident",   provider="elevenlabs", use_cases=["narrator"]),
    VoiceInfo(key="elevenlabs:elli",    label="Elli · ElevenLabs",    language="en", gender="female", style="emotional, young",    provider="elevenlabs", use_cases=["conversational"]),
    VoiceInfo(key="elevenlabs:josh",    label="Josh · ElevenLabs",    language="en", gender="male",   style="deep, narrative",     provider="elevenlabs", use_cases=["narrator", "trailer"]),
    VoiceInfo(key="elevenlabs:arnold",  label="Arnold · ElevenLabs",  language="en", gender="male",   style="crisp, news",         provider="elevenlabs", use_cases=["news"]),
    VoiceInfo(key="elevenlabs:sam",     label="Sam · ElevenLabs",     language="en", gender="male",   style="raspy, casual",       provider="elevenlabs", use_cases=["conversational"]),
]


def _load_kokoro_voices() -> list[VoiceInfo]:
    """Pre-baked Kokoro preset samples in web/static/voice_samples/."""
    if not KOKORO_SAMPLES_DIR.exists():
        return []
    out: list[VoiceInfo] = []
    for wav in sorted(KOKORO_SAMPLES_DIR.glob("*.wav")):
        key = wav.stem
        meta = _KOKORO_META.get(key)
        if meta:
            label, lang, gender, style = meta
        else:
            label = key.replace("_", " ").title()
            lang = "en"
            gender = ""
            style = "Kokoro preset"
        out.append(VoiceInfo(
            key=key,
            label=label,
            language=lang,
            gender=gender,
            style=style,
            notes="Kokoro TTS preset · synthesised, not a recorded human.",
            provider="kokoro",
            use_cases=["narrator"],
        ))
    return out


def _all_voices() -> list[VoiceInfo]:
    """De-dup by key with priority: clones > catalog > web > kokoro > extras."""
    seen: dict[str, VoiceInfo] = {}
    sources = (
        _EXTRA_VOICES
        + _load_kokoro_voices()
        + _load_web_voices()
        + _load_catalog_voices()
        + _load_clone_voices()
    )
    for v in sources:
        # Re-resolve sample url every call so just-cloned voices show up;
        # auto-fill tones from the style/label so the filter chips work
        # on every entry without each one having to declare them.
        d = v.model_dump()
        d["sample_url"] = _voice_url(v.key)
        if not d.get("tones"):
            d["tones"] = _derive_tones(v.style or "", v.label or "")
        seen[v.key] = VoiceInfo(**d)
    return list(seen.values())


@router.get("/catalog")
async def list_voices() -> dict:
    return {"voices": [v.model_dump() for v in _all_voices()]}


@router.get("/sample/{key}.wav")
async def sample(key: str) -> FileResponse:
    if "/" in key or ".." in key:  # pragma: no cover — Starlette rejects path-traversal before handler
        raise HTTPException(status_code=400, detail="invalid voice key")
    wav = _voice_path(key)
    if wav is None:
        raise HTTPException(status_code=404, detail="no sample for this voice")
    return FileResponse(
        str(wav),
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=300"},
    )


# ---------------------------------------------------------------------------
# Clone upload
# ---------------------------------------------------------------------------


class CloneResult(BaseModel):
    voice: VoiceInfo
    duration_s: float
    note: str = ""


def _ffprobe_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe; 0.0 on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return float(out) if out else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _ffmpeg_convert(src: Path, dst: Path, *, max_seconds: float = 15.0) -> None:
    """Convert any audio to 24 kHz mono 16-bit WAV, capped to max_seconds.

    The render pipeline expects 5-15s reference clips; longer clips don't
    improve cloning quality and just bloat the request payload. We also
    normalise loudness so quiet recordings don't get inaudible TTS output.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-t", str(max_seconds),
        "-ac", "1",
        "-ar", "24000",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-sample_fmt", "s16",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


@router.post("/clone", response_model=CloneResult)
async def clone_voice(
    name: str = Form(..., description="Display name. Will be slugified for the key."),
    language: str = Form("en", description="ISO code: en | hi | ..."),
    transcript: str = Form("", description="Spoken transcript. Helps F5/IndicF5 prosody."),
    audio: UploadFile = File(..., description="Reference clip (5-15s recommended)"),
) -> CloneResult:
    """Accept an audio upload, ffmpeg-convert to 24 kHz mono WAV, save as a new voice."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise HTTPException(status_code=500, detail="ffmpeg/ffprobe not on PATH")

    if not audio.filename:
        raise HTTPException(status_code=422, detail="no audio file uploaded")

    ext = Path(audio.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported audio format {ext!r} — try one of: {sorted(ALLOWED_EXTS)}",
        )

    key = _slugify(name)
    if not key:
        raise HTTPException(status_code=422, detail="name must contain at least one letter or digit")

    voice_dir = CLONES_DIR / key
    if voice_dir.exists():
        raise HTTPException(
            status_code=409,
            detail=f"a voice named '{key}' already exists — pick a different name",
        )
    voice_dir.mkdir(parents=True, exist_ok=True)

    # Buffer the upload to a temp file (with size cap) before letting ffmpeg
    # touch it, so a malicious upload can't blow up disk.
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while True:
            chunk = await audio.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                tmp_path.unlink(missing_ok=True)
                shutil.rmtree(voice_dir, ignore_errors=True)
                raise HTTPException(status_code=413, detail="upload too large (>25 MB)")
            tmp.write(chunk)

    out_wav = voice_dir / "ref.wav"
    try:
        _ffmpeg_convert(tmp_path, out_wav)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(voice_dir, ignore_errors=True)
        stderr = (e.stderr or b"").decode("utf-8", "ignore")[:300]
        raise HTTPException(status_code=422, detail=f"audio conversion failed: {stderr}")
    finally:
        tmp_path.unlink(missing_ok=True)

    duration = _ffprobe_duration(out_wav)

    # Save transcript + meta sidecars
    if transcript.strip():
        (voice_dir / "ref.txt").write_text(transcript.strip())

    meta = {
        "label": name.strip(),
        "language": language.strip() or "en",
        "style": "User clone",
        "duration_s": round(duration, 2),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "transcript": transcript.strip() or None,
    }
    (voice_dir / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))

    # Build the VoiceInfo to return — UI uses this to refresh the catalog.
    voice = VoiceInfo(
        key=key,
        label=name.strip(),
        language=meta["language"],
        gender="",
        style="User clone",
        sample_url=_voice_url(key),
        notes=f"Cloned from upload · {duration:.1f}s",
        is_clone=True,
        duration_s=round(duration, 2),
    )

    note = ""
    if duration < 4.0:
        note = "Sample is shorter than 4s — consider re-uploading a 5-15s clip for best cloning quality."
    elif duration > 14.0:
        note = "Sample was trimmed to 15s (the renderer's optimal length)."

    return CloneResult(voice=voice, duration_s=round(duration, 2), note=note)


@router.delete("/clone/{key}")
async def delete_clone(key: str) -> dict:
    if "/" in key or ".." in key:  # pragma: no cover — Starlette rejects path-traversal before handler
        raise HTTPException(status_code=400, detail="invalid key")
    voice_dir = CLONES_DIR / key
    if not voice_dir.exists() or not voice_dir.is_dir():
        raise HTTPException(status_code=404, detail="clone not found")
    shutil.rmtree(voice_dir, ignore_errors=True)
    return {"ok": True, "key": key}

