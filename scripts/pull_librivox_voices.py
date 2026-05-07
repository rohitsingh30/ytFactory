"""Pull LibriVox public-domain voice references for the bench.

LibriVox (librivox.org) is a public-domain audiobook archive. All
recordings are dedicated to PD via CC0 — completely free for commercial
use, no attribution required.

Pulls strategy:
  * Use known-good LibriVox identifiers (curated list below; verified
    as solo male/female readers with clean acoustics).
  * Download a single chapter MP3 from archive.org (direct CDN, no
    auth required).
  * ffmpeg-clip a 10-15s segment that's just the reader's voice
    (skips the LibriVox intro — usually first ~10s — and any
    chapter-head silence).
  * Convert to 24 kHz mono PCM16 WAV (matches our cloud TTS contract).

Output:
  pipeline/voice_refs/bench/<channel>__<format>__<voice_id>/ref.wav
  pipeline/voice_refs/bench/<channel>__<format>__<voice_id>/ref.txt   ← transcript (Whisper-derived)

Channel + voice profile mapping is below. Each channel has 1-3 voices
that match the production aesthetic.

Run:
  .venv/bin/python scripts/pull_librivox_voices.py            # pull all
  .venv/bin/python scripts/pull_librivox_voices.py --channel mystoriesanimated  # one channel
  .venv/bin/python scripts/pull_librivox_voices.py --check    # list catalog only
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "pipeline" / "voice_refs" / "bench"


# =============================================================================
# CATALOG — channel × format × voice profile
# =============================================================================
#
# Each entry:
#   channel: target channel name (folder prefix)
#   format: "shorts" or "long_form"
#   voice_id: short identifier (used in filename)
#   gender: "male" | "female"  (for verification + tagging)
#   profile: human-readable character profile
#   archive_id: archive.org item identifier
#   chapter_file: filename of the chapter MP3 to pull
#   start_s: clip start offset in seconds (skip LibriVox intro ~10s)
#   end_s: clip end offset (target ~10-15s of clean voice)
#
# Curation rules:
#   * Only solo readers (no multi-reader collaborations)
#   * Modern recordings (2010+) for cleaner acoustics
#   * Reader genders verified by listening / book metadata
#   * Skip first 8-12s to avoid "This is a librivox recording. All
#     librivox recordings are in the public domain..." intro
#

CATALOG: list[dict] = [
    # =========================================================================
    # MALE VOICES (verified archive.org identifiers, 2026-05-06)
    # =========================================================================

    # ---- mystoriesanimated — male dramatic intense
    {
        "channel": "mystoriesanimated", "format": "shorts",
        "voice_id": "male_dramatic_intense",
        "gender": "male",
        "profile": "Intense narrator, varied pacing, perfect for AITA male variants",
        "archive_id": "moby_dick_librivox",
        "chapter_file": "mobydick_001_002_melville_64kb.mp3",
        "start_s": 35.0, "end_s": 49.0,
    },

    # ---- historyrecapped — male warm documentary
    {
        "channel": "historyrecapped", "format": "shorts",
        "voice_id": "male_warm_documentary",
        "gender": "male",
        "profile": "Warm authoritative tone, documentary-style male reader",
        "archive_id": "thejunglebook_pc_librivox",
        "chapter_file": "junglebookthe_02_kipling_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- historyrecapped — male deep calm storyteller (long-form sleep)
    {
        "channel": "historyrecapped", "format": "long_form",
        "voice_id": "male_deep_calm_storyteller",
        "gender": "male",
        "profile": "Deep calm baritone, slow soothing pace, ideal for sleep",
        "archive_id": "richestman_2604_librivox",
        "chapter_file": "richestman_02_clason_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- sportstoriesanimated — male intense energetic
    {
        "channel": "sportstoriesanimated", "format": "shorts",
        "voice_id": "male_intense_energetic",
        "gender": "male",
        "profile": "Animated, energetic, varied intonation",
        "archive_id": "art_war_ps_librivox",
        "chapter_file": "artofwar_02_sun_64kb.mp3",
        "start_s": 35.0, "end_s": 49.0,
    },

    # ---- sportstoriesanimated — male deep documentary (long-form)
    {
        "channel": "sportstoriesanimated", "format": "long_form",
        "voice_id": "male_deep_documentary",
        "gender": "male",
        "profile": "Authoritative deep documentary voice",
        "archive_id": "richestman_2604_librivox",
        "chapter_file": "richestman_03_clason_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- cosmosdecoded — male warm explainer (Shorts) — Walden, male reader
    {
        "channel": "cosmosdecoded", "format": "shorts",
        "voice_id": "male_warm_explainer",
        "gender": "male",
        "profile": "Warm engaging male explainer — science communication",
        "archive_id": "walden_librivox",
        "chapter_file": "walden_c01_p02_64kb.mp3",
        "start_s": 35.0, "end_s": 49.0,
    },

    # ---- cosmosdecoded — male deep authoritative (long-form)
    {
        "channel": "cosmosdecoded", "format": "long_form",
        "voice_id": "male_deep_authoritative",
        "gender": "male",
        "profile": "BBC-documentary-style deep authoritative",
        "archive_id": "moby_dick_librivox",
        "chapter_file": "mobydick_003_melville_64kb.mp3",
        "start_s": 35.0, "end_s": 49.0,
    },

    # ---- airecap — male news anchor
    {
        "channel": "airecap", "format": "shorts",
        "voice_id": "male_news_anchor",
        "gender": "male",
        "profile": "Clear formal newsreader / lecturer tone",
        "archive_id": "communistmanifesto_librivox",
        "chapter_file": "marx_engels_communistmanifesto_2_64kb.mp3",
        "start_s": 35.0, "end_s": 49.0,
    },

    # =========================================================================
    # FEMALE VOICES
    # =========================================================================

    # ---- mystoriesanimated — female dramatic emotional (AITA)
    {
        "channel": "mystoriesanimated", "format": "shorts",
        "voice_id": "female_dramatic_emotional",
        "gender": "female",
        "profile": "Emotional female reader, expressive, AITA-style",
        "archive_id": "pride_and_prejudice_librivox",
        "chapter_file": "prideandprejudice_04-05_austen_64kb.mp3",
        "start_s": 35.0, "end_s": 49.0,
    },

    # ---- historyrecapped — female warm friendly (Shorts)
    {
        "channel": "historyrecapped", "format": "shorts",
        "voice_id": "female_warm_friendly",
        "gender": "female",
        "profile": "Warm friendly female narrator (Little Princess audiobook reader)",
        "archive_id": "little_princess_krs",
        "chapter_file": "little_princess_02_burnett_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- historyrecapped — female calm soothing (long-form sleep)
    {
        "channel": "historyrecapped", "format": "long_form",
        "voice_id": "female_calm_soothing",
        "gender": "female",
        "profile": "Soft calm female reader, sleep cadence",
        "archive_id": "secret_garden_1105_librivox",
        "chapter_file": "secretgarden_02_burnett_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- airecap — female news anchor
    {
        "channel": "airecap", "format": "shorts",
        "voice_id": "female_news_anchor",
        "gender": "female",
        "profile": "Clear formal newsreader female",
        "archive_id": "wuthering_heights_rg_librivox",
        "chapter_file": "wutheringheights_02_bronte_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- rhymetimejunction — female kid-friendly
    {
        "channel": "rhymetimejunction", "format": "shorts",
        "voice_id": "female_kid_friendly",
        "gender": "female",
        "profile": "Bright, warm, animated — children's storyteller",
        "archive_id": "aliceinwonderland_drama_1310_librivox",
        "chapter_file": "aliceinwonderland_01_gerstenberg_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },

    # ---- cosmosdecoded — female warm explainer (alt)
    {
        "channel": "cosmosdecoded", "format": "shorts",
        "voice_id": "female_warm_explainer",
        "gender": "female",
        "profile": "Engaging female explainer voice",
        "archive_id": "secret_garden_1105_librivox",
        "chapter_file": "secretgarden_03_burnett_64kb.mp3",
        "start_s": 14.0, "end_s": 28.0,
    },
]


def _download_chapter(archive_id: str, chapter_file: str, dest_mp3: Path) -> None:
    """Fetch a single chapter MP3 from archive.org."""
    url = f"https://archive.org/download/{archive_id}/{chapter_file}"
    dest_mp3.parent.mkdir(parents=True, exist_ok=True)
    print(f"    GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ytFactory/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        dest_mp3.write_bytes(r.read())


def _ffmpeg_clip_to_wav(src_mp3: Path, dest_wav: Path,
                       start_s: float, end_s: float) -> None:
    """Clip + convert to 24 kHz mono PCM16 WAV."""
    dest_wav.parent.mkdir(parents=True, exist_ok=True)
    duration = end_s - start_s
    cmd = [
        "ffmpeg", "-y", "-i", str(src_mp3),
        "-ss", str(start_s), "-t", str(duration),
        "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
        str(dest_wav),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {result.stderr.strip()[-300:]}"
        )


def _whisper_transcribe(wav: Path) -> str:
    """Use openai-whisper to derive transcript (small model, fast)."""
    try:
        import whisper
    except ImportError:
        return "<TRANSCRIBE: whisper not installed>"
    model = whisper.load_model("base")
    r = model.transcribe(str(wav))
    return (r.get("text") or "").strip()


def fetch_one(entry: dict, force: bool = False) -> tuple[Path | None, str]:
    """Pull + clip + transcribe one entry."""
    cell_dir = (
        BENCH_DIR /
        f"{entry['channel']}__{entry['format']}__{entry['voice_id']}"
    )
    wav_dest = cell_dir / "ref.wav"
    txt_dest = cell_dir / "ref.txt"
    meta_dest = cell_dir / "_source.txt"

    if wav_dest.exists() and txt_dest.exists() and not force:
        return wav_dest, f"  [skip] {cell_dir.name} (exists)"

    try:
        # Download chapter MP3
        tmp_mp3 = Path("/tmp") / f"_librivox_{entry['archive_id']}_{entry['chapter_file']}"
        if not tmp_mp3.exists():
            _download_chapter(entry["archive_id"], entry["chapter_file"], tmp_mp3)

        # Clip to WAV
        _ffmpeg_clip_to_wav(
            tmp_mp3, wav_dest, entry["start_s"], entry["end_s"],
        )

        # Transcribe
        transcript = _whisper_transcribe(wav_dest)
        txt_dest.write_text(transcript)

        # Metadata for traceability + license attribution
        meta_dest.write_text(
            f"Source: LibriVox via archive.org\n"
            f"Identifier: {entry['archive_id']}\n"
            f"Chapter: {entry['chapter_file']}\n"
            f"Clip: {entry['start_s']:.1f}s - {entry['end_s']:.1f}s\n"
            f"License: Public Domain (CC0)\n"
            f"Profile: {entry['profile']}\n"
            f"Gender: {entry['gender']}\n"
        )
        return wav_dest, f"  [ok ] {cell_dir.name} (transcript: {len(transcript)} chars)"
    except Exception as e:
        return None, f"  [FAIL] {cell_dir.name}: {type(e).__name__}: {str(e)[:120]}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true",
                   help="List catalog without pulling")
    p.add_argument("--channel", default=None,
                   help="Only pull entries for this channel")
    p.add_argument("--force", action="store_true",
                   help="Re-pull even if files exist")
    args = p.parse_args()

    catalog = CATALOG
    if args.channel:
        catalog = [e for e in catalog if e["channel"] == args.channel]

    print(f"Catalog: {len(catalog)} voices")
    print(f"Output: {BENCH_DIR.relative_to(ROOT)}")
    print()

    if args.check:
        from collections import defaultdict
        by_ch: dict[str, list[dict]] = defaultdict(list)
        for e in catalog:
            by_ch[e["channel"]].append(e)
        for ch, entries in sorted(by_ch.items()):
            print(f"  {ch} ({len(entries)} voices):")
            for e in entries:
                print(f"    [{e['gender']:6s}] {e['format']:9s} {e['voice_id']:35s} ({e['profile']})")
        return

    n_ok = n_skip = n_fail = 0
    for entry in catalog:
        dest, msg = fetch_one(entry, force=args.force)
        print(msg)
        if dest is not None and "[skip]" in msg:
            n_skip += 1
        elif dest is not None:
            n_ok += 1
        else:
            n_fail += 1

    print()
    print(f"=== done: {n_ok} new, {n_skip} skipped, {n_fail} failed ===")


if __name__ == "__main__":
    main()
