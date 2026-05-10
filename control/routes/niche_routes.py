"""Public read-only endpoints used by the operator UI.

The legacy /api/niches, /api/voices, /api/voice_clones endpoints lived on
web/server.py. The cloud control plane re-exports a slim version of them
so the operator UI (web/static/index.html) renders against the prod URL.

For the cloud-driven product the niches are the **publishing channels**
the chat assistant can produce for. Voice picking is folded into chat
(natural language) so /api/voices + /api/voice_clones return empty
arrays for now — the UI's voice section will simply not render anything
and the chat panel handles voice selection if the user wants to override.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


# Curated reel of channels the chat assistant can produce for. Each entry
# becomes one 9:16 card on the landing reel. `prefill` is a short sentence
# the chat panel auto-types when the card is clicked, biased to the
# winning hook style for that channel.
NICHES: list[dict] = [
    {
        "key": "mystoriesanimated",
        "label": "MyStoriesAnimated",
        "tagline": "Reddit AITA / TIFU drama, animated 2D crayon",
        "emoji": "🔥",
        "color_from": "from-rose-500",
        "color_to": "to-pink-700",
        "ring": "ring-rose-400/40",
        "sample_hooks": [
            "AITA for ruining my SIL's birthday cake",
            "TIFU by texting my boss what I thought was a meme",
            "AITA for not letting my cousin sleep on our couch",
        ],
        "prefill": "Make me an AITA short about ",
        "channel_key_for_chat": "mystoriesanimated",
    },
    {
        "key": "sportsrecapped",
        "label": "SportsRecapped",
        "tagline": "Football moments, Tifo line-art + real broadcast cut-ins",
        "emoji": "⚽️",
        "color_from": "from-emerald-500",
        "color_to": "to-teal-800",
        "ring": "ring-emerald-300/40",
        "sample_hooks": [
            "Aguero's 93:20 title-winner vs QPR",
            "Iniesta's silencer at Stamford Bridge 2009",
            "Roberto Carlos free kick that defied physics",
        ],
        "prefill": "Make me a sports short about ",
        "channel_key_for_chat": "sportsrecapped",
    },
    {
        "key": "historyrecapped",
        "label": "History Recapped",
        "tagline": "100% archival war footage with documentary narration + word captions",
        "emoji": "⚔️",
        "color_from": "from-amber-700",
        "color_to": "to-stone-900",
        "ring": "ring-amber-300/40",
        "sample_hooks": [
            "The 12-second silence before the first Apollo 13 alarm",
            "Stalingrad's last airdrop — 18 January 1943",
            "How a single Polish cipher clerk broke Enigma in 1932",
        ],
        "prefill": "",
        "channel_key_for_chat": "historyrecapped",
    },
    {
        "key": "open",
        "label": "Open prompt",
        "tagline": "Skip the niches — just describe the Short",
        "emoji": "✨",
        "color_from": "from-indigo-500",
        "color_to": "to-violet-800",
        "ring": "ring-indigo-300/40",
        "sample_hooks": [
            "An animated explainer about why octopuses dream",
            "A 60s history micro-documentary on the Antikythera mechanism",
            "Today-in-history short about June 2 1953",
        ],
        "prefill": "",
        "channel_key_for_chat": "auto",
    },
]


@router.get("/api/niches")
async def list_niches() -> dict:
    return {"niches": NICHES}


@router.get("/api/voices")
async def list_voices() -> dict:
    """Voice picking happens in chat now — return empty so legacy code no-ops cleanly."""
    return {"voices": [], "languages": [], "default": None}


@router.get("/api/voice_clones")
async def list_voice_clones() -> dict:
    return {"clones": []}
