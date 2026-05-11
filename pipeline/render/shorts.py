"""End-to-end orchestrator for stages 4-7 (creation).

Reads providers from the channel YAML so swapping models is a config
change, not a code edit:

    asr_provider:    whisper_mlx | parakeet_mlx
    tts_provider:    kokoro      | f5_tts
    image_provider:  sd_turbo    | sdxl_lightning | mflux | z_image_turbo  (slideshow path)
    motion_provider: <unset>     | animatediff_toonyou | animatediff_lcm

If ``motion_provider`` is set, each beat becomes a continuous animated
clip (via ``pipeline.animation``) and the final compose uses
``compose.compose_clips`` instead of the slideshow ``compose.compose``.

Two ways to feed in narration:

    --text "..."                  hardcoded narration
    --script <path/to/script.json>  load a Script produced by /make-script
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import yaml

from pipeline import align, audio, beats, compose, images
from pipeline.llm import prompts as prompts_mod, script_check
from pipeline import telemetry as tlm


# Hardcoded sample (a short AITA-style story for v0 testing)
SAMPLE_TEXT = (
    "AITA for refusing to bake a cake for my sister's wedding? "
    "She told me the cake had to be vegan, gluten-free, and sugar-free. "
    "I'm a professional baker. I said no. "
    "Now my whole family is calling me selfish."
)


# Last-beat YouTube-style like + subscribe iconography, drawn INTO the
# 2D cartoon image (not overlaid as a separate UI surface). User
# feedback 2026-05-02: drop the closer panel/captions concept entirely;
# the buttons must look like part of the scene in the same crayon style.
#
# Phrasing avoids text-bait words ("text", "letters", "writing", etc.)
# so images.strip_text_bait doesn't shred this on the way to diffusion.
# The buttons are described by SHAPE/COLOR/ICON only — no English
# inscriptions on the rectangle, since SDXL/Z-Image-Turbo render any
# requested text as gibberish (the "asshlash" / "夫倭吧?" failure mode).
_LAST_BEAT_ICONS = (
    " In the visual center of the frame, side by side, two compact"
    " cartoon icons: a yellow thumbs-up icon with a thick black"
    " outline on the left, and a red rounded-rectangle button with a"
    " tiny white play-triangle and a tiny bell silhouette on the"
    " right. The two icons sit in the middle of the frame, touching"
    " each other, both compact in size. Both icons are drawn in the"
    " same flat 2D crayon style as the rest of the scene."
)


def _safe_prerender_word_captions(beat_list, cache_dir):
    """Background-thread wrapper around compose.prerender_word_captions.

    Swallows any exception so a caption-render bug can't crash the
    pipeline — compose() will re-render anything we miss. Logged via
    print so it surfaces in stdout/telemetry but doesn't fail the job.
    """
    try:
        t0 = time.time()
        n = compose.prerender_word_captions(beat_list, cache_dir)
        if n > 0:
            print(
                f"[caption-prerender] wrote {n} word PNGs in "
                f"{time.time()-t0:.1f}s (in parallel with image gen)"
            )
    except Exception as e:
        print(f"[caption-prerender] non-fatal failure: {e!r}; compose will render fresh")


_CLOSER_KEYWORDS = ("like", "comment", "subscribe", "agree", "swap")


def _append_last_beat_icons(
    scene: str, beat_idx: int, n_beats: int,
    beat_text: str | None = None,
) -> str:
    """Append cartoon-style like + subscribe iconography to closer beats.

    Programmatic append (not LLM-author-driven) — guarantees the icons
    are present regardless of LLM compliance, on the initial render AND
    on the critic-regen path. Mirrors the kit_lock pattern used by the
    sports channel: server-side enforced visual tokens that survive
    LLM paraphrasing.

    Fires on the LAST beat unconditionally, AND on any preceding beat
    whose narration contains closer-keyword tokens (LIKE / COMMENT /
    SUBSCRIBE / AGREE / SWAP). Critic 2026-05-03 (top3-stoppage-goals
    v3): the closer "like if you agree with number one, and comment
    your swap below" was split into 2 beats by the splitter — only
    the LAST beat got icons, leaving an empty-stadium frame under the
    LIKE-IF-YOU-AGREE caption. Treating ALL closer-keyword beats as
    icon-bearing covers the multi-beat closer.

    No-op for non-closer beats (returns scene unchanged).
    """
    if not scene:
        return scene
    is_last = beat_idx == n_beats - 1
    is_closer_keyword = bool(
        beat_text
        and any(kw in beat_text.lower() for kw in _CLOSER_KEYWORDS)
    )
    if not (is_last or is_closer_keyword):
        return scene
    return scene.rstrip(" .") + "." + _LAST_BEAT_ICONS


# Repo root = parent.parent.parent of this file (pipeline/render/shorts.py
# → pipeline/render/ → pipeline/ → repo). Used for channel discovery.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _record_stage_done(
    stage: str,
    t0: float,
    *,
    slug: str,
    channel: str,
    success: bool = True,
    extra: dict | None = None,
) -> None:
    """Emit a `stage_done` telemetry event with `metadata.stage`.

    Lightweight wrapper so the renderer's existing `t0 = time.time()`
    + `print("done in ...s")` pattern can also feed the latency
    dashboard's hotspots view (which buckets on `metadata.stage`).
    Pre-fix: `stage_done` was never emitted by any renderer, so the
    headline `/api/telemetry/latency` panel was empty. Post-fix: each
    of the 4 short-render stages (tts / asr+beats / image_gen /
    compose) plus the optional critic stage logs a row.

    Cheap (one append to the JSONL log); never raises.
    """
    md = {
        "stage": stage,
        "slug": slug,
        "niche": channel,
    }
    if extra:
        md.update(extra)
    tlm.track_stage_done(
        "stage_done",
        category="pipeline",
        success=success,
        duration_ms=int((time.time() - t0) * 1000),
        job_id=os.environ.get("YTFACTORY_JOB_ID") or None,
        metadata=md,
    )


def _channel_folders() -> list[Path]:
    """Top-level dirs that look like a channel (have config.yaml).

    Discovery basis for every per-slug lookup since the 2026-05-03 reorg:
    each YouTube channel is a top-level repo folder (`historyrecapped/`,
    `mystoriesanimated/`, etc.) holding its own config.yaml + state dirs.
    """
    out: list[Path] = []
    for p in _REPO_ROOT.iterdir():
        if p.is_dir() and (p / "config.yaml").exists():
            out.append(p)
    return out


def _scan_intermediate(slug: str, subdir: str) -> Path | None:
    """Find ``<channel>/.../<subdir>/<slug>.json`` — recursive so nested
    layouts (parent/variant/{cast,narrations,...}) work the same as flat.
    Returns the first match. ``subdir`` is the leaf bucket name like
    "narrations", "cast", "voices", "dossier", "shotlist".
    """
    for ch in _channel_folders():
        for p in ch.rglob(f"{subdir}/{slug}.json"):
            if p.is_file():
                return p
    return None


def _channel_dir_for(slug: str, subdir: str = "narrations") -> str:
    """Return the channel_dir relative path (e.g. "mystoriesanimated" for
    flat layouts, "mystoriesanimated/reddit_amitheasshole" for nested) by
    locating <subdir>/<slug>.json under any channel folder. Empty if not found.
    """
    p = _scan_intermediate(slug, subdir)
    if p is None:
        return ""
    rel = p.resolve().relative_to(_REPO_ROOT)
    # rel is "<channel_dir>/<subdir>/<slug>.json" — strip last 2 parts.
    return str(rel.parent.parent)


def _find_cast_path(slug: str) -> Path | None:
    """Locate <channel>/cast/<slug>.json if it exists."""
    return _scan_intermediate(slug, "cast")


def _load_forced_narration_lines(slug: str) -> list[str] | None:
    """Locate ``data/intermediate/<channel>/shotlist/<slug>.json`` and
    extract the per-shot narration_lines (plus closer.narration_line) in
    order. Returns None if no shotlist exists for this slug.

    These lines drive the beat splitter so each shot becomes one beat
    by construction — Phase-2 sync fix (principle #26).
    """
    sp = _scan_intermediate(slug, "shotlist")
    if sp is None:
        return None
    try:
        data = json.loads(sp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    lines: list[str] = []
    for shot in data.get("shots") or []:
        line = (shot.get("narration_line") or "").strip()
        if line:
            lines.append(line)
    closer = data.get("closer") or {}
    closer_line = (closer.get("narration_line") or "").strip()
    if closer_line:
        lines.append(closer_line)
    return lines or None


def _find_voice_path(slug: str) -> Path | None:
    """Locate data/intermediate/<channel>/voices/<slug>.json if it exists.

    Produced by clone_voice.py from a YouTube URL; presence forces
    tts_provider=f5_tts for this slug.
    """
    return _scan_intermediate(slug, "voices")


def _voice_fingerprint(
    cfg: dict,
    pronunciation_dict: dict | None = None,
    *,
    out_dir: Path | None = None,
    slug: str | None = None,
) -> dict:
    """Hashable summary of the cfg fields that drive narration.wav.

    A change in any of these invalidates the synthesized audio AND every
    artifact derived from word timestamps (beats, prompts, images/clips,
    captions). For f5_tts the ``voice`` value is a path to a ref clip
    that the web bind step writes to a slug-keyed location, so the path
    string can stay constant across re-binds — include mtime+size so a
    different clone bound to the same slug still busts the cache.

    The dossier-driven ``pronunciation_dict`` is also part of the
    fingerprint — re-authoring the dossier with new respellings must
    bust the cached audio.
    """
    audio_provider = cfg.get("audio_provider", "tts")
    fp: dict = {
        "audio_provider": audio_provider,
    }
    if audio_provider == "external_song":
        # The narration.wav comes from a user-provided song file (Suno
        # for the rhyme channel). Cache key is the source file's
        # mtime+size — overwriting the song WAV (e.g. user re-generates
        # on Suno after a critique) busts the audio cache and the
        # downstream beats/captions re-align to the new audio. Also
        # includes the trim knobs so toggling duration_max_s or
        # audio_drop_leading_silence re-runs the trim from source.
        if out_dir is None or slug is None:
            fp["external_unknown"] = True
            return fp
        ext = _external_song_path(cfg, out_dir, slug)
        try:
            st = ext.stat()
            fp["external_path"] = str(ext)
            fp["external_mtime"] = st.st_mtime
            fp["external_size"] = st.st_size
        except OSError:
            fp["external_path"] = str(ext)
            fp["external_missing"] = True
        fp["duration_max_s"] = cfg.get("duration_max_s")
        fp["audio_trim_start_s"] = cfg.get("audio_trim_start_s", 0.0)
        fp["drop_leading_silence"] = cfg.get("audio_drop_leading_silence", False)
        fp["drop_vocal_pickup"] = cfg.get("audio_drop_vocal_pickup", False)
        fp["vocal_pickup_threshold_db"] = cfg.get("audio_vocal_pickup_threshold_db", -22.0)
        fp["fade_out_s"] = cfg.get("audio_fade_out_s", 0.0)
        return fp

    if audio_provider == "sunoapi":
        # narration.wav is fetched from sunoapi.org's Suno wrapper.
        # Cache key is the SUNO PROMPT (lyrics + style + model + vocal),
        # so editing the lyrics/style/model in script.json re-generates
        # the song on the next render. The fingerprint is the prompt
        # content itself, not the rendered audio — Suno is non-
        # deterministic, so the same prompt produces a different song
        # each call; we trust the FIRST result and cache.
        suno_prompt: dict = {}
        if cfg is not None:
            suno_prompt = (cfg.get("_suno_prompt_override") or {})
        # The suno_prompt isn't in cfg by default — make_short threads
        # it in via the script.json. We accept it via cfg here for
        # fingerprint purposes; if absent, skip.
        fp["sunoapi_model"] = cfg.get("sunoapi_model", "V4_5")
        fp["sunoapi_vocal"] = cfg.get("sunoapi_vocal_gender", "f")
        fp["sunoapi_style"] = (suno_prompt.get("style") or "")[:500]
        fp["sunoapi_lyrics"] = (suno_prompt.get("lyrics") or "")[:2000]
        return fp

    # tts mode (kokoro / f5_tts / chatterbox / styletts2 / indic_parler)
    fp.update({
        "provider": cfg.get("tts_provider", "kokoro"),
        "voice": cfg.get("tts_voice"),
        "ref_text": cfg.get("tts_ref_text"),
        "speed": cfg.get("tts_speed", 1.0),
        # Language tag — switching from en→hi must bust the cache so the
        # new language re-synths instead of replaying the English audio.
        "language": cfg.get("tts_language", "en"),
        # Modulation block: tweaking a factor in the channel YAML
        # invalidates the cached narration.wav so the new tempo curve
        # is heard on the next render. Sorted for stable hashing.
        "modulation": dict(sorted((cfg.get("tts_modulation") or {}).items())),
        "pronunciation": dict(sorted((pronunciation_dict or {}).items())),
    })
    if fp["provider"] == "f5_tts" and fp["voice"]:
        try:
            st = Path(fp["voice"]).stat()
            fp["ref_mtime"] = st.st_mtime
            fp["ref_size"] = st.st_size
        except OSError:
            pass
    return fp


def _external_song_path(cfg: dict, out_dir: Path, slug: str) -> Path:
    """Resolve the user-provided audio file for ``audio_provider: external_song``.

    Convention: ``<out_dir>/songs/<slug>.wav``. The user generates the
    song externally (e.g. on suno.com), downloads the WAV, and drops it
    at this path. The render gates on the file existing — render fails
    with a helpful error if it's missing instead of producing a silent
    short.

    The channel YAML's optional ``audio_external_filename`` overrides
    the default ``<slug>.wav`` filename (rare — useful if the user
    keeps multiple takes named ``<slug>-take2.wav``).
    """
    fname = cfg.get("audio_external_filename") or f"{slug}.wav"
    return out_dir / "songs" / fname


def _load_footage_overrides(script_path: Path) -> list[dict]:
    """Read the optional ``footage`` array from a script JSON.

    Schema (per entry):
      {
        "match_text": "Aguero scores",      # substring of a beat's text
        "url": "https://www.youtube.com/...",
        "in_s": 84.2,
        "out_s": 87.6,
        "audio_mix": 0.35                    # optional, v2 (compose drops audio)
      }

    Returns ``[]`` if the script has no ``footage`` array. Validation is
    permissive — bad entries are skipped with a warning rather than
    failing the whole render.
    """
    try:
        data = json.loads(script_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("footage")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            print(f"[footage] script.footage[{i}] is not an object — skipping")
            continue
        url = entry.get("url")
        match_text = entry.get("match_text")
        in_s = entry.get("in_s")
        out_s = entry.get("out_s")
        if not (url and match_text is not None and in_s is not None and out_s is not None):
            print(f"[footage] script.footage[{i}] missing url/match_text/in_s/out_s — skipping")
            continue
        # Pass-through unknown fields so newer per-cut flags
        # (e.g. black_intro, pre_pad_s) reach _attach_footage_to_beats
        # → b.footage without the loader filtering them out.
        loaded = dict(entry)
        loaded["match_text"] = str(match_text)
        loaded["url"] = str(url)
        loaded["in_s"] = float(in_s)
        loaded["out_s"] = float(out_s)
        loaded["audio_mix"] = float(entry.get("audio_mix", 0.0))
        out.append(loaded)
    return out


def _load_ranks(script_path: Path) -> list[dict]:
    """Read the optional ``ranks`` array from a tier-list script JSON.

    Each entry: ``{rank, subject, match_text, image_prompt_hint, kit?}``.
    Used by:
      - ``prompts.py:_build_user_prompt`` to inject curator hints into
        the LLM prompt author
      - ``make_shorts.py`` post-author kit_lock injection (when an entry
        has a ``kit`` block, append kit tokens to the matching beat's
        scene)

    Returns ``[]`` when the script has no ``ranks`` (non-tier-list channels).
    Permissive — bad entries are skipped silently rather than failing.
    """
    try:
        data = json.loads(script_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("ranks")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _inject_kit_lock(prompts_path: Path, ranks: list[dict]) -> int:
    """Append per-rank kit tokens to every prompt that matches a rank.

    Mirrors the existing sports cast-locked-tokens pattern
    (feedback_sports_cast_locked_tokens.md) — LLM authoring is paraphrased
    after the fact, so the kit / shirt-number / era tokens are bolted on
    server-side. This guarantees the right kit lands on screen even when
    the LLM drops or rephrases them.

    Each rank entry may carry::

        "kit": {
          "team": "Manchester City",
          "era": "2011-12",
          "primary_color": "sky blue",
          "secondary_color": "white",
          "shirt_number": "16",
          "badge": "Manchester City crest"
        },
        "face": "David Beckham, sandy-blond hair, sharp cheekbones, tattoo on right arm"

    The optional ``face`` field locks the player's likeness — it gets
    appended to the matching beat's scene the same way ``kit`` does.
    Critic 2026-05-03: even with kit_lock right, generic player faces
    drift; a 1-line face descriptor disambiguates Solskjaer / Ramos /
    Iniesta when the LLM authoring is vague.

    Returns count of beats patched. Silent no-op if no ranks have ``kit``.
    """
    if not ranks or not prompts_path.exists():
        return 0
    has_any_kit = any(isinstance(r.get("kit"), dict) for r in ranks)
    if not has_any_kit:
        return 0
    try:
        prompts_data = json.loads(prompts_path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(prompts_data, list):
        return 0

    # Each rank's match_text ("Number one") may appear in MORE THAN ONE
    # beat — e.g. the closer "like if you agree with number one" also
    # contains "number one". Without claim-tracking we'd inject the same
    # kit into the closer beat, which renders the wrong character.
    # Critic 2026-05-02 (top5-stoppage-goals v1): closer rendered as
    # another Aguero celebration instead of the thumbs-up closer cartoon.
    # First-match-wins per rank — rank announcements appear before
    # closer references in countdown narration.
    #
    # Each rank can ALSO carry an optional ``scorer_match_text`` —
    # the narrator's outro line that follows the footage cut
    # ("Solskjaer pokes it in"). Critic 2026-05-03: muted viewers
    # never learn who scored because broadcast audio doesn't reach
    # them. The scorer-outro beat shows the player illustration with
    # the same kit locked in. Track scorer-slot independently so a
    # rank can claim both: primary announcement + scorer outro.
    claimed_ranks: set[int] = set()
    claimed_scorer_slots: set[int] = set()
    n_patched = 0
    for prompt_obj in prompts_data:
        if not isinstance(prompt_obj, dict):
            continue
        beat_text = (prompt_obj.get("narration_line") or "").lower()
        if not beat_text:
            continue
        for r in ranks:
            kit = r.get("kit")
            if not isinstance(kit, dict):
                continue
            rank_id = r.get("rank")
            # Two slots per rank: (1) primary match_text (announcement),
            # (2) scorer_match_text (post-footage scorer-outro). Each
            # slot independently claimable, so the same rank's kit fires
            # on both the announcement beat AND the scorer-outro beat.
            primary_claimed = rank_id in claimed_ranks
            scorer_claimed = rank_id in claimed_scorer_slots
            mt = (r.get("match_text") or "").lower().strip()
            scorer_mt = (r.get("scorer_match_text") or "").lower().strip()
            slot = None
            if not primary_claimed and mt and mt in beat_text:
                slot = "primary"
            elif not scorer_claimed and scorer_mt and scorer_mt in beat_text:
                slot = "scorer"
            if slot is None:
                continue
            # Build a kit clause from the structured fields. Order matters
            # for diffusion — primary_color first, then era, then number.
            parts: list[str] = []
            team = (kit.get("team") or "").strip()
            era = (kit.get("era") or "").strip()
            primary = (kit.get("primary_color") or "").strip()
            secondary = (kit.get("secondary_color") or "").strip()
            num = (kit.get("shirt_number") or "").strip()
            badge = (kit.get("badge") or "").strip()
            if primary and team:
                kit_phrase = f"in {team} {era} {primary} kit".strip()
                if secondary:
                    kit_phrase += f" with {secondary} trim"
                parts.append(kit_phrase)
            elif primary:
                parts.append(f"in {primary} kit")
            if num:
                parts.append(f"shirt number {num}")
            if badge:
                parts.append(f"{badge} on chest")
            face = (r.get("face") or "").strip()
            if face:
                parts.append(face)
            if not parts:
                continue
            kit_clause = ", ".join(parts)
            old_scene = prompt_obj.get("scene", "")
            # Idempotent: don't double-append on a re-run. CRITICAL: still
            # claim the rank when we detect the prior injection — otherwise
            # the rank slot is treated as unclaimed and a later beat that
            # also matches match_text (e.g. closer "if you agree with
            # number one") gets the kit appended. Critic 2026-05-03
            # (top3-stoppage-goals v1): closer beat 15 ended up with
            # Ramos kit + #4 because the 1st-run injection on beat 11
            # didn't carry over its claim into the 2nd run's set.
            def _claim_slot():
                if slot == "scorer":
                    claimed_scorer_slots.add(rank_id)
                else:
                    claimed_ranks.add(rank_id)
            if kit_clause in old_scene:
                _claim_slot()
                break
            prompt_obj["scene"] = (
                f"{old_scene.rstrip(' .')}, {kit_clause}.".lstrip(", ")
            )
            n_patched += 1
            _claim_slot()
            break  # one rank match per beat

    if n_patched:
        with prompts_path.open("w") as f:
            json.dump(prompts_data, f, indent=2)
        print(f"[ranks] injected kit tokens into {n_patched} beat prompt(s)")
    return n_patched


def _attach_footage_to_beats(beat_list: list, footage_overrides: list[dict]) -> int:
    """Tag matching beats with ``kind="footage"`` + footage payload.

    Match strategy: case-insensitive substring of ``match_text`` against
    the beat's ``text``. First-match-wins per override. Logs a warning
    for any override that didn't match a beat (likely typo in the script).

    When an override has ``covers_full_rank: true`` (the
    /make-rivalry-recap footage-only contract), the attach SPANS from
    the matching anchor beat through (but excluding) the next override's
    anchor beat — or to end-of-beat-list for the last anchor. Each beat
    in the span gets its OWN sliced ``in_s``/``out_s``, proportional to
    its audio duration vs the span's total duration. ``black_intro``
    only applies to the first beat in the span; later beats reset it to
    avoid prepending black silence per fragment.
    """
    # First pass: locate the primary anchor beat for each override and
    # remember its order. We need positions before applying spans because
    # `covers_full_rank` reads NEXT-anchor's index.
    anchors: list[tuple[int, dict]] = []  # (beat_index, override)
    used_idx: set[int] = set()
    for ov in footage_overrides:
        needle = (ov.get("match_text") or "").lower().strip()
        if not needle:
            continue
        hit = False
        for i, b in enumerate(beat_list):
            if i in used_idx:
                continue
            if needle in (b.text or "").lower():
                anchors.append((i, ov))
                used_idx.add(i)
                hit = True
                break
        if not hit:
            print(f"[footage] WARN: no beat matched match_text={ov['match_text']!r}")
    anchors.sort(key=lambda t: t[0])

    n_matched = 0
    for k, (anchor_idx, ov) in enumerate(anchors):
        if not ov.get("covers_full_rank"):
            # Single-beat attach (legacy /make-ranking + /make-script paths).
            b = beat_list[anchor_idx]
            b.kind = "footage"
            b.footage = dict(ov)
            n_matched += 1
            continue

        # Span [anchor_idx, next_anchor_idx) — or to end of beat_list
        # for the last anchor.
        next_idx = anchors[k + 1][0] if k + 1 < len(anchors) else len(beat_list)
        span = list(range(anchor_idx, next_idx))
        src_in = float(ov.get("in_s") or 0.0)
        src_out = float(ov.get("out_s") or 0.0)

        # Slice 1:1 with audio: each fragment is exactly the beat's
        # audio duration, walked forward from src_in. The author's
        # `out_s` is the upper bound (where to stop reading source) but
        # not a stretch target — if total audio is shorter than
        # `out_s - in_s`, the trailing source is unused. Stretching
        # (proportional slicing) blew up the timeline because
        # compose_hybrid pads each audio slot with silence to fit a
        # longer footage fragment, and a 12s source window for 5s of
        # rank audio added 7s of silence per rank — pushed a 58s Short
        # to 75s. Class-of-bug fix 2026-05-05.
        src_cursor = src_in
        for j, i in enumerate(span):
            b = beat_list[i]
            # Tiny / zero-duration beats (silence between sentences,
            # punctuation-only sub-beats from the splitter) get a 0.20s
            # floor — small enough that compose's silence-pad doesn't
            # bloat the Short, big enough for ffmpeg trim + fade pair.
            frag_dur = max(b.duration, 0.20)
            frag_in = src_cursor
            frag_out = src_cursor + frag_dur
            # Hard-clamp to the source window. If we run past it, the
            # last fragment shrinks to whatever's left; subsequent
            # fragments freeze on the tail (`compose_hybrid` extends
            # the audio slot with silence — preferable to ffmpeg
            # erroring on an out-of-bounds trim).
            if frag_out > src_out:
                frag_out = src_out
                if frag_out - frag_in < 0.20:
                    frag_in = max(src_out - 0.20, src_in)
            src_cursor = frag_out
            b.kind = "footage"
            b.footage = dict(ov)
            b.footage["in_s"] = round(frag_in, 3)
            b.footage["out_s"] = round(frag_out, 3)
            # Only the leading beat of a span keeps the black-intro pad;
            # otherwise we'd prepend N black intros (one per fragment).
            if j > 0:
                b.footage.pop("black_intro", None)
                b.footage.pop("black_intro_buffer_s", None)
                b.footage.pop("pre_pad_s", None)
            n_matched += 1
        used_src_dur = src_cursor - src_in
        total_beat_dur = sum(max(beat_list[i].duration, 0.20) for i in span)
        print(
            f"[footage] covers_full_rank: anchor beat {anchor_idx} → "
            f"spanned {len(span)} beat(s) {span}; src window "
            f"{src_in:.2f}-{src_out:.2f}s, used {used_src_dur:.2f}s "
            f"(audio sum {total_beat_dur:.2f}s)"
        )
    return n_matched


def _seed_for_beat(
    scene: str,
    key_visual: str,
    supporting_chars: list[dict],
    default_seed: int,
) -> tuple[int, str | None]:
    """Pick the image seed for one beat.

    Scans ``scene`` + ``key_visual`` for any supporting character's name
    or alias. If a match is found, returns that character's seed (so all
    beats featuring this character render at the same seed → faces hold
    across the Short). Falls back to ``default_seed`` for character-free
    or multi-character beats.

    Returns ``(seed, matched_name | None)``.
    """
    if not supporting_chars:
        return default_seed, None
    haystack = f"{scene} {key_visual}".lower()
    if not haystack.strip():
        return default_seed, None
    for entry in supporting_chars:
        for name in entry["names"]:
            n = name.lower().strip()
            if not n:
                continue
            # word-boundary-ish: check the name appears as a substring
            # surrounded by non-letter chars. Cheap; good enough for the
            # short scene strings the LLM produces.
            if n in haystack:
                # Verify it's not embedded inside a larger word.
                idx = haystack.find(n)
                left_ok = idx == 0 or not haystack[idx - 1].isalpha()
                right_ok = (
                    idx + len(n) == len(haystack)
                    or not haystack[idx + len(n)].isalpha()
                )
                if left_ok and right_ok:
                    return entry["seed"], name
    return default_seed, None


def _find_script_path(slug: str) -> Path | None:
    """Locate <channel>/narrations/<slug>.json if it exists.

    Renamed from "scripts" to "narrations" in the 2026-05-03 reorg so the
    bucket doesn't collide with each channel's `scripts/` python tools dir.
    """
    return _scan_intermediate(slug, "narrations")


def _load_pronunciation_dict(slug: str) -> dict:
    """Read pronunciation_dict from the per-story dossier if present.

    The dossier is authored by ``pipeline/wiki_research.py`` and lives
    at ``data/intermediate/<channel>/dossier/<slug>.json``. Channel
    dir is unknown at make_short time (same problem as ``_find_cast_path``),
    so we scan all channel dirs.
    """
    dp = _scan_intermediate(slug, "dossier")
    if dp is None:
        return {}
    try:
        d = json.loads(dp.read_text())
        pd = d.get("pronunciation_dict") or {}
        if isinstance(pd, dict):
            return {k: v for k, v in pd.items()
                    if isinstance(k, str) and isinstance(v, str)}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _load_script_text(script_path: Path) -> tuple[str, str, str]:
    """Read a /make-script Script JSON and return (narration, slug, source_story).

    source_story is the raw long-form text from
    data/intermediate/<channel>/raw/<slug>.json if found, else the
    narration itself (so LLM-prompt-authoring still has SOMETHING to
    work with even if the raw is missing).
    """
    raw = json.loads(script_path.read_text())
    narration = raw["narration"]
    slug = raw["slug"]

    # The script is at data/intermediate/<channel>/scripts/<slug>.json.
    # The raw story is at data/intermediate/<channel>/raw/<slug>.json.
    raw_path = script_path.parent.parent / "raw" / f"{slug}.json"
    source_story = narration
    if raw_path.exists():
        try:
            raw_obj = json.loads(raw_path.read_text())
            body = (raw_obj.get("body") or "").strip()
            title = (raw_obj.get("title") or "").strip()
            if body:
                source_story = f"{title}\n\n{body}" if title else body
        except (json.JSONDecodeError, KeyError):
            pass

    return narration, slug, source_story


def _looks_like_concrete_token(token: str, prompt: str) -> bool:
    """A concrete token is considered present if its lowercase form (or
    a substantive word from it) appears in the prompt. We don't require
    exact match — any of the multi-word phrase's content words counts.
    """
    parts = [p for p in token.lower().split() if len(p) > 2]
    p = prompt.lower()
    return any(part in p for part in parts) if parts else token.lower() in p


def _validate_opening_image(prompt: str, directives: dict | None) -> list[str]:
    """Return a list of warnings for the beat-0 prompt. Empty = passes."""
    if not directives:
        return []
    required = int(directives.get("required_concrete_tokens", 2))
    example_tokens = directives.get("example_tokens") or []
    matches = [t for t in example_tokens if _looks_like_concrete_token(t, prompt)]
    if len(matches) < required:
        return [
            f"opening image prompt has only {len(matches)}/{required} "
            f"concrete tokens from the channel's example set "
            f"(matched: {matches}). Add specific room/prop/posture detail."
        ]
    return []


# Note: prompt composition now lives in images.build_full_prompt()
# (Principles #2, #11, #13). The orchestrator just provides the
# character_description and per-beat scene/key_visual.


def _apply_form_overrides(cfg: dict, overrides: dict) -> None:
    """Translate website-form `channel_overrides` keys → channel YAML cfg keys.

    The Customize step on the create page emits a flat dict; this is
    where each entry lands in the actual cfg the renderer reads. Kept
    as a separate function so the override→cfg-key map is reviewable in
    one place — adding a new form knob is one entry here plus the
    matching schema field in pipeline/schemas/customization.py.

    Quietly skips empty / None values so a YAML default keeps winning
    when the user didn't touch a field. Unknown keys are ignored on
    purpose — the form is expected to send a wider superset over time
    and we don't want a stale form value silently mis-configuring the
    render.
    """
    audio_mode = overrides.get("audio_mode")
    if audio_mode == "song":
        # Force Suno even if the channel's YAML defaults to TTS. Will raise
        # later (with a clear message) if SUNOAPI_API_KEY is missing — that
        # error is surfacable to the user, which is correct here.
        cfg["audio_provider"] = "sunoapi"
    elif audio_mode == "voice":
        # Force TTS even on a song channel. Channel's tts_voice / tts_provider
        # is used as-is — for rhymetimejunction this is cloudrun_chatterbox
        # with sarah.wav, which is set up for the future Dadi-style spoken
        # bridge. Won't sound like a *song*, but that's the user's choice.
        cfg["audio_provider"] = "tts"

    song_style = overrides.get("song_style")
    if isinstance(song_style, str) and song_style.strip():
        # _audio_fingerprint_for_cache reads cfg["_suno_prompt_override"]["style"]
        # and the synth path reads it from script.json's suno_prompt block.
        # Stashing it on cfg here threads it through both consumers
        # without touching the script authoring flow.
        suno_override = dict(cfg.get("_suno_prompt_override") or {})
        suno_override["style"] = song_style.strip()
        cfg["_suno_prompt_override"] = suno_override

    song_vocal_gender = overrides.get("song_vocal_gender")
    if song_vocal_gender in ("f", "m"):
        cfg["sunoapi_vocal_gender"] = song_vocal_gender

    song_model = overrides.get("song_model")
    if isinstance(song_model, str) and song_model.strip():
        cfg["sunoapi_model"] = song_model.strip()

    visual_source = overrides.get("visual_source")
    if visual_source in ("ai", "footage", "both"):
        # Advisory cfg key consumed at the per-beat decision points
        # downstream. "ai" leaves the channel's image_provider alone
        # (default behaviour). "footage"/"both" require the script to
        # carry footage data per beat — see _attach_footage_to_beats
        # and the visual_source warning emitted further down in
        # make_short().
        cfg["visual_source"] = visual_source


def make_short(
    text: str,
    channel_path: Path,
    out_dir: Path,
    slug: str,
    source_story: str | None = None,
    run_critic: bool = True,
    tts_voice_override: str | None = None,
    upload_override: bool | None = None,
    require_critic: bool = False,
    cfg_overrides: dict | None = None,
) -> Path:
    from pipeline.preflight import power_check  # noqa: PLC0415

    _render_t0 = time.time()

    # Refuse to start in Low Power Mode (the 2026-05-04 / 2026-05-05
    # SIGABRT-on-Metal class of bug). Override with
    # ``YTFACTORY_SKIP_POWER_CHECK=1`` if you understand the risk.
    power_check(label="Shorts")

    # Reset the cloud-image circuit breaker per-render. Without this,
    # a previous render's CloudRunUnavailable would still be tripping
    # the breaker on this render → all images would skip cloud and go
    # to local mflux even when the cloud service has recovered. See
    # pipeline/images_cloudrun.py for breaker semantics.
    from pipeline.images_cloudrun import reset_circuit_breaker  # noqa: PLC0415
    reset_circuit_breaker()

    cfg = yaml.safe_load(channel_path.read_text())

    # Apply website-form overrides BEFORE any cfg-driven branching.
    # `cfg_overrides` arrives as a flat dict from the create-page form
    # (channel_overrides → CLI --override key=value → here). The keys we
    # honour map cleanly to existing cfg knobs:
    #
    #   audio_mode=song        → cfg.audio_provider = sunoapi (forces Suno
    #                            even on a TTS channel, raises clearly if
    #                            SUNOAPI_API_KEY is missing)
    #   audio_mode=voice       → cfg.audio_provider = tts (force TTS even
    #                            on a song channel, e.g. for a Dadi-style
    #                            spoken bridge; uses cfg.tts_voice as-is)
    #   song_style             → cfg._suno_prompt_override.style (consumed
    #                            by _audio_fingerprint_for_cache + the
    #                            sunoapi synth call below)
    #   song_vocal_gender      → cfg.sunoapi_vocal_gender
    #   song_model             → cfg.sunoapi_model
    #   visual_source          → cfg.visual_source (advisory; honoured at
    #                            the per-beat decision points below)
    #
    # Anything not in this map is ignored — pass-through of arbitrary
    # YAML-style overrides would let the form silently mis-configure
    # the renderer. New knobs need an entry here.
    if cfg_overrides:
        _apply_form_overrides(cfg, cfg_overrides)

    # An override that contains no path separator is a Kokoro voice id
    # (e.g. "am_eric"). When the channel default is F5-TTS but the user
    # explicitly picks a Kokoro voice in the web UI, we have to flip
    # cfg["tts_provider"] back to kokoro AND clear tts_ref_text — otherwise
    # the synth call would dispatch to f5_tts with a Kokoro voice name and
    # crash. (Path-style overrides — pointing at a ref clip — are F5-TTS
    # references and should keep cfg["tts_provider"]=f5_tts.)
    explicit_kokoro = bool(tts_voice_override) and "/" not in tts_voice_override
    if tts_voice_override:
        cfg["tts_voice"] = tts_voice_override
        if explicit_kokoro:
            cfg["tts_provider"] = "kokoro"
            cfg.pop("tts_ref_text", None)
    cache = out_dir / "cache" / slug
    cache.mkdir(parents=True, exist_ok=True)

    # Per-story cloned voice (from clone_voice.py) overrides channel TTS.
    # Mirrors the cast-override pattern: presence of a voices/<slug>.json
    # forces F5-TTS zero-shot cloning for this slug. Skipped when the
    # caller passed an explicit Kokoro voice ID via --tts-voice — explicit
    # user input wins over stale clone bindings left from a prior render.
    voice_path = None if explicit_kokoro else _find_voice_path(slug)
    if voice_path is not None:
        voice_meta = json.loads(voice_path.read_text())
        cfg["tts_provider"] = "f5_tts"
        cfg["tts_voice"] = voice_meta["ref_wav"]
        cfg["tts_ref_text"] = voice_meta["ref_text"]
        print(
            f"[voice] using cloned voice from {voice_path.name} "
            f"(src={voice_meta.get('source_url','?')}, "
            f"{voice_meta.get('duration','?')}s @ {voice_meta.get('start','?')}s)"
        )

    # Provider knobs — sane defaults preserve current behaviour.
    asr_provider = cfg.get("asr_provider", "whisper_mlx")
    tts_provider = cfg.get("tts_provider", "kokoro")
    image_provider = cfg.get("image_provider", "sd_turbo")
    motion_provider = cfg.get("motion_provider")  # None → slideshow path

    # Cloud image-gen cold-load is 5-7 min through GCS Fuse. If we wait
    # to discover that on the first /generate call, the whole Short
    # blocks for those 5-7 min on the critical path. Instead: kick off
    # /readyz on a background thread NOW (image_provider is resolved
    # but we still have ~60-90s of TTS+ASR ahead). Provider-aware:
    # no-ops for local providers, fires for cloudrun_*.
    image_warmup_thread = images.warmup(image_provider)

    # 2026-05-10: also prewarm the cloud TTS service. Pre-fix the
    # Shorts path had no TTS prewarm at all (long_form did at
    # long_form.py:266); a cold render's worst-case telemetry showed
    # image_attempt taking 33 min — almost certainly because the
    # TTS+image cold-loads serialised on the critical path before
    # the warmup background thread had time to actually warm the
    # image service. Hitting both /readyz upfront kills that
    # serialisation. Provider-aware: no-op for local TTS providers
    # like kokoro/f5_tts.
    tts_warmup_thread = None
    if tts_provider.startswith("cloudrun_"):
        try:
            from pipeline.tts.cloudrun import warmup as _tts_warmup  # noqa: PLC0415
            tts_warmup_thread = _tts_warmup(tts_provider)
            if tts_warmup_thread is not None:
                print(f"[warmup] {tts_provider} /readyz fired on background thread")
        except Exception as e:
            # Fail open — warmup is best-effort. Real /synth will
            # surface any actual problem with the cloud service.
            print(f"[warmup] {tts_provider} prewarm failed (non-fatal): {e!r}")

    # Class-of-bug guard (see images.validate_provider_config docstring).
    # Channels can pick provider, dims, and steps independently — we
    # catch combos that produce smudgy garbage (e.g. sd_turbo @ 768x1344)
    # at job start instead of after the user has watched a broken Short.
    # Skipped on the motion_provider path because animation has its own
    # capability story (animatediff_lcm etc) — handled in pipeline.animation.
    if motion_provider is None:
        provider_errors = images.validate_provider_config(
            image_provider,
            width=cfg.get("image_width", 768),
            height=cfg.get("image_height", 1344),
            steps=cfg.get("image_steps", 4),
        )
        if provider_errors:
            msg = (
                f"channel {channel_path.name} has a broken image config:\n  - "
                + "\n  - ".join(provider_errors)
            )
            raise SystemExit(msg)

    # Per-story cast overrides the channel-locked character if a
    # cast.json exists. Channel YAML's character_description is the
    # backwards-compat fallback for slugs that haven't been re-pulled
    # under the new flow.
    character_description = cfg.get("character_description")
    cast_default_emotion: str | None = None
    cast_path = _find_cast_path(slug)
    from pipeline.llm import cast as cast_mod
    if cast_path is None and source_story:
        # Principle #24 — auto-author per-story narrator instead of
        # silently falling back to the channel default. The
        # age-ambiguous channel character ("round-headed kid") was
        # winning when scripts implied parent / grandparent / spouse
        # narrators, producing boy-cartoons-voice-grandmother
        # mismatches (see critique aita-birth-pool). Run cast.author_cast
        # against the raw story when no cast file exists.
        # Resolve the channel data root from the channel YAML's location
        # under the per-channel-folder reorg. `<channel>/config.yaml` →
        # `<channel>/`. `<channel>/variants/<variant>.yaml` →
        # `<channel>/<variant>/` (the niche-specific data dir).
        if channel_path.name == "config.yaml":
            channel_data_root = channel_path.parent
        elif channel_path.parent.name == "variants":
            channel_data_root = channel_path.parent.parent / channel_path.stem
        else:
            channel_data_root = channel_path.parent
        autopath = channel_data_root / "cast" / f"{slug}.json"
        try:
            print(f"[cast] no cast.json found — auto-authoring per-story narrator…")
            cast_mod.author_cast(
                raw_story={
                    "slug": slug,
                    "title": "",  # source_story already concatenates title+body
                    "body": source_story,
                },
                channel_cfg=cfg,
                out_path=autopath,
            )
            cast_path = autopath
        except Exception as e:
            print(f"[cast] auto-author failed (falling back to channel default): {e}")
    # Per-character seed table — populated below from cast.supporting[].
    # When a beat's scene names a character, the image-gen loop uses that
    # character's seed instead of the channel-wide image_seed. Keeps the
    # cartoon Aguero looking like the cartoon Aguero across beats.
    supporting_chars: list[dict] = []
    cast_supporting_full: list[dict] = []  # raw {name, aliases, description, seed}
    if cast_path is not None:
        cast = cast_mod.load_cast(cast_path)
        if cast and cast["narrator"].get("description"):
            character_description = cast["narrator"]["description"]
            cast_default_emotion = cast["narrator"].get("default_emotion")
            cast_supporting_full = cast.get("supporting") or []
            print(
                f"[cast] using per-story narrator from {cast_path.name} "
                f"({cast['narrator'].get('age_band', '?')} "
                f"{cast['narrator'].get('gender', '?')}, "
                f"emotion={cast_default_emotion!r})"
            )
            for sup in cast.get("supporting") or []:
                if not isinstance(sup, dict) or not sup.get("name"):
                    continue
                names = [sup["name"], *(sup.get("aliases") or [])]
                seed = sup.get("seed")
                if seed is None:
                    continue  # legacy cast entry — fall through to channel seed
                supporting_chars.append({
                    "names": [n for n in names if isinstance(n, str) and n],
                    "seed": int(seed),
                })
            if supporting_chars:
                print(
                    f"[cast] {len(supporting_chars)} supporting characters "
                    f"have per-character seeds — beats featuring them will "
                    f"lock to those seeds for cross-beat consistency"
                )

    # Sports-style channels: narrator is voice-over only. Suppress the
    # narrator description from per-beat image prompts so the renderer
    # doesn't paint an "analyst persona" alongside (or instead of) the
    # actual people in the story. The prompt-author still receives the
    # mode flag and the supporting[] list so it can name real people.
    if cfg.get("narrator_visual_mode") == "voice_only":
        if character_description:
            print(
                f"[cast] narrator_visual_mode=voice_only — clearing "
                f"channel-wide character_description so no analyst "
                f"persona is painted into per-beat image prompts"
            )
        character_description = ""
    opening_directives = cfg.get("opening_image_directives")

    print(f"\n=== make_short: {slug} ===")
    print(f"text:      {text[:80]}{'...' if len(text) > 80 else ''}")
    print(
        f"providers: asr={asr_provider} tts={tts_provider} "
        f"{'motion=' + motion_provider if motion_provider else 'image=' + image_provider}"
    )

    # Pre-flight script check (Principles #4 + #5). For channels with
    # `closer_format` set (AITA-class), errors block the render so weak
    # hooks / missing wedges / vague closers don't ship.
    issues = script_check.check_script_text(text, channel_cfg=cfg)
    script_check.report(issues, fail_on_error=True)

    # Stage 4 — TTS
    #
    # Cache invalidation: narration.wav is keyed by slug, so changing
    # voice config (e.g. binding a cloned voice after a Kokoro run, or
    # swapping Kokoro voices) used to silently reuse the prior audio
    # because the file already existed. Same hazard for everything
    # downstream (beats are word-timestamp-derived; prompts/images
    # cascade off beats). Fingerprint the voice cfg into a sidecar; if
    # it differs from the cached one — or is missing entirely (legacy
    # cache from before this code) — wipe the per-slug cache so the
    # next render is fully consistent with the current voice.
    # Kick off diffusion-pipeline warm-up on a background thread BEFORE
    # TTS so the ~15–30s cold load (SDXL: 7 GB + Lightning LoRA + optional
    # IP-Adapter) hides behind the next ~60-90s of TTS + ASR + beat-prompt
    # authoring (all mostly-CPU/network work where the GPU is idle).
    #
    # mflux is INTENTIONALLY excluded: MLX binds device streams (and the
    # internal weight buffers that reference them) to the thread that
    # creates them. Loading Flux1 on a background thread then calling
    # generate_image() from the main thread crashes with "There is no
    # Stream(gpu, 1) in current thread". For mflux we fall back to the
    # original behaviour — lazy-load on first generate() from the main
    # thread. (Confirmed regression from earlier threaded-warmup attempt.)
    import threading
    can_threaded_warmup = image_provider in ("sdxl_lightning", "sd_turbo")
    use_ip_for_warmup = bool(cfg.get("ip_adapter_image")) and can_threaded_warmup
    warmup_thread: threading.Thread | None = None
    if can_threaded_warmup:
        warmup_thread = threading.Thread(
            target=images.warmup,
            kwargs={"provider": image_provider, "want_ip_adapter": use_ip_for_warmup},
            name="images.warmup",
            daemon=True,
        )
        warmup_thread.start()

    t0 = time.time()
    audio_path = cache / "narration.wav"
    fp_path = cache / "narration.voice.json"
    # Pronunciation dict from the per-story dossier (sports channel) —
    # respellings flow into TTS only; captions and ASR-source alignment
    # continue to see the original spelling.
    pronunciation_dict = _load_pronunciation_dict(slug)
    fp_now = _voice_fingerprint(
        cfg,
        pronunciation_dict=pronunciation_dict,
        out_dir=out_dir,
        slug=slug,
    )
    if audio_path.exists():
        fp_prev: dict | None = None
        if fp_path.exists():
            try:
                fp_prev = json.loads(fp_path.read_text())
            except (OSError, json.JSONDecodeError):
                fp_prev = None
        if fp_prev != fp_now:
            reason = (
                "no voice fingerprint sidecar (legacy cache)"
                if fp_prev is None
                else f"voice config changed: {fp_prev} → {fp_now}"
            )
            print(f"[1/4] {reason}; wiping per-slug cache")
            for child in list(cache.iterdir()):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
    # Normalise numerals before TTS *and* alignment. Kokoro reads "$2000"
    # as "two zero zero zero" without this; we expand currency and bare
    # integers to spelled-out words. The same normalised string flows into
    # source-text alignment so beat captions match what was actually spoken
    # (without this, alignment would try to match "$2000" against ASR
    # words "two thousand dollars" and fall apart).
    text = audio.normalize_for_tts(text)

    audio_provider = cfg.get("audio_provider", "tts")
    if audio_provider == "external_song" and not audio_path.exists():
        # No TTS — the user supplies the audio externally (e.g. a Suno-
        # generated song WAV for the rhyme channel). Two things happen
        # here before the rest of the pipeline (ASR, beats, captions,
        # compose) sees the audio:
        #   1. Drop the leading instrumental intro (Suno songs have ~3s
        #      of quiet guitar that Whisper hallucinates as words at
        #      wrong timestamps — class-of-bug from
        #      feedback_whisper_hallucinates_on_instrumental).
        #   2. Cap to channel YAML's `duration_max_s` so the final
        #      Short stays in the algorithmic retention window. Without
        #      this, a 156s Suno song produces a 156s mp4 — way over
        #      the 60s Shorts retention sweet spot.
        ext = _external_song_path(cfg, out_dir, slug)
        if not ext.exists():
            raise SystemExit(
                f"channel uses audio_provider: external_song but the "
                f"source audio file does not exist:\n  {ext}\n\n"
                f"Generate the song externally (e.g. on suno.com), "
                f"download the WAV, and drop it at that path. Then "
                f"re-run make_shorts.py."
            )
        max_duration_s = float(cfg.get("duration_max_s") or 9999.0)
        trim_start_s = float(cfg.get("audio_trim_start_s") or 0.0)
        drop_leading = bool(cfg.get("audio_drop_leading_silence", False))
        drop_pickup = bool(cfg.get("audio_drop_vocal_pickup", False))
        pickup_threshold_db = float(cfg.get("audio_vocal_pickup_threshold_db") or -22.0)
        fade_out_s = float(cfg.get("audio_fade_out_s") or 0.0)
        needs_trim = (
            max_duration_s < 9999.0
            or trim_start_s > 0.0
            or drop_leading
            or drop_pickup
            or fade_out_s > 0.0
        )
        if needs_trim:
            print(
                f"[1/4] external_song: trimming {ext.name} "
                f"(start={trim_start_s:.1f}s, max={max_duration_s:.1f}s, "
                f"drop_pickup={drop_pickup}, fade_out={fade_out_s:.1f}s) "
                f"→ narration.wav"
            )
            trim_a, trim_b = audio.trim_song_for_short(
                in_path=ext,
                out_path=audio_path,
                max_duration_s=max_duration_s,
                trim_start_s=trim_start_s,
                drop_leading_silence=drop_leading,
                drop_vocal_pickup=drop_pickup,
                vocal_pickup_threshold_db=pickup_threshold_db,
                fade_out_s=fade_out_s,
            )
            print(
                f"     trimmed source [{trim_a:.2f}s – {trim_b:.2f}s] "
                f"→ {audio_path.name}"
            )
        else:
            print(f"[1/4] external_song: copying {ext.name} → narration.wav")
            ext.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ext, audio_path)
        fp_path.write_text(json.dumps(fp_now, indent=2))
    elif audio_provider == "sunoapi" and not audio_path.exists():
        # narration.wav is GENERATED by sunoapi.org's Suno wrapper.
        # Fully automated — POST lyrics+style, poll for completion,
        # download the WAV. The lyrics + style live in the script.json
        # under suno_prompt.{lyrics, style} (authored by /make-rhyme).
        script_json_path = out_dir / "scripts" / f"{slug}.json"
        if not script_json_path.exists():
            raise SystemExit(
                f"audio_provider=sunoapi requires {script_json_path} "
                f"to exist with a suno_prompt block. Author it via "
                f"/make-rhyme or switch the channel YAML back."
            )
        try:
            script_dict = json.loads(script_json_path.read_text())
        except json.JSONDecodeError as e:
            raise SystemExit(f"could not parse {script_json_path}: {e}")
        suno_prompt = script_dict.get("suno_prompt") or {}
        lyrics = (suno_prompt.get("lyrics") or "").strip()
        style = (suno_prompt.get("style") or "").strip()
        if not lyrics or not style:
            raise SystemExit(
                f"audio_provider=sunoapi requires {script_json_path} "
                f"to contain suno_prompt.{{lyrics, style}}. Run "
                f"/make-rhyme to author a script with these fields. "
                f"Got: {list(suno_prompt)}"
            )
        # Re-fingerprint with the prompt content so re-edits bust cache.
        cfg["_suno_prompt_override"] = suno_prompt
        fp_now = _voice_fingerprint(
            cfg, pronunciation_dict=pronunciation_dict,
            out_dir=out_dir, slug=slug,
        )
        title = script_dict.get("hook") or slug
        print(f"[1/4] sunoapi: generating sung song (model="
              f"{cfg.get('sunoapi_model', 'V4_5')})…")
        audio.synth_via_sunoapi(
            lyrics=lyrics,
            style=style,
            out_path=audio_path,
            title=str(title)[:80],
            model=cfg.get("sunoapi_model", "V4_5"),
            vocal_gender=cfg.get("sunoapi_vocal_gender", "f"),
        )
        fp_path.write_text(json.dumps(fp_now, indent=2))
    elif not audio_path.exists():
        if pronunciation_dict:
            print(f"[1/4] TTS ({tts_provider}) with {len(pronunciation_dict)} "
                  f"pronunciation overrides from dossier…")
        else:
            print(f"[1/4] TTS ({tts_provider})…")
        audio.synthesize(
            text,
            voice=cfg["tts_voice"],
            out_path=audio_path,
            speed=cfg.get("tts_speed", 1.0),
            provider=tts_provider,
            ref_audio_text=cfg.get("tts_ref_text"),
            # Per-paragraph speed modulation: channel YAML can override
            # individual factors via the `tts_modulation` block. Without
            # a config block, sensible defaults from
            # ``audio._DEFAULT_MODULATION`` apply (hook + closer slower,
            # exclamation faster, ellipsis-trail slower). Kokoro-only;
            # f5_tts ignores this.
            modulation=cfg.get("tts_modulation"),
            # Phonetic respellings from the per-story dossier (e.g.
            # "Aguero" → "Ah-GWAIR-oh"). Original spelling stays in
            # ``text`` for downstream alignment and caption rendering.
            pronunciation_dict=pronunciation_dict,
            # Language tag — channel YAML's `tts_language: hi` cues Hindi
            # for hindutavaanimated; defaults to English. Kokoro reads its
            # own lang_for_voice() — this field is a no-op for kokoro/f5_tts.
            language=cfg.get("tts_language", "en"),
        )
        fp_path.write_text(json.dumps(fp_now, indent=2))
    else:
        print(f"[1/4] TTS cached: {audio_path}")
    print(f"     done in {time.time() - t0:.1f}s")
    _record_stage_done(
        "tts", t0, slug=slug, channel=channel_path.stem,
        extra={"provider": tts_provider},
    )

    # 2026-05-05: drop F5-TTS-MLX (~1.35 GB) at the renderer-stage boundary
    # if F5 was the active provider. Image gen, beats, captions, mux all
    # run after this and don't need F5; previously it leaked through to
    # those stages and contributed to Metal aborts on long renders.
    # No-op when tts_provider != 'f5_tts'.
    if tts_provider == "f5_tts":
        from pipeline.preflight import reset_mlx_state  # noqa: PLC0415
        reset_mlx_state(drop_f5=True, label="Shorts stage-1 TTS")

    # Stage 5 — beats (word timestamps + source-text alignment)
    t0 = time.time()
    beats_path = cache / "beats.json"
    if not beats_path.exists():
        print(f"[2/4] {asr_provider} word timestamps + source-text alignment + beat split…")
        whisper_words = beats.transcribe_words(audio_path, provider=asr_provider)
        # Align the source narration to the ASR timestamps (fixes
        # "AITA"->"Ada" class misrecognitions). `text` is already
        # normalised above, so e.g. "two thousand dollars" matches what
        # Whisper transcribed instead of failing on "$2000".
        source_aligned = align.align_source_to_whisper(text, whisper_words)

        # Phase-2 sync fix (principle #26 / shotlist-driven beats):
        # if a shotlist exists for this slug, force one beat per shot.
        # The shotlist's narration_lines become the cut boundaries —
        # no merging, no duration heuristic. This makes shot-to-beat
        # alignment deterministic for /make-movie-short runs and
        # eliminates the off-by-one slides we saw on aita-birth-pool.
        forced_lines = _load_forced_narration_lines(slug)
        if forced_lines:
            print(
                f"[beats] shotlist found — forcing {len(forced_lines)} beat "
                f"boundaries from shotlist narration_lines"
            )

        beat_list = beats.split_into_beats(
            source_aligned,
            target_s=cfg.get("beat_target_s", 2.0),
            max_s=cfg.get("beat_max_s", 3.0),
            forced_narration_lines=forced_lines,
        )
        beats.save_beats(beat_list, beats_path)
    else:
        print(f"[2/4] beats cached: {beats_path}")
        beat_list = beats.load_beats(beats_path)
    print(f"     {len(beat_list)} beats, total {sum(b.duration for b in beat_list):.1f}s")
    for i, b in enumerate(beat_list):
        print(f"       beat {i}: {b.duration:.2f}s — {b.text[:60]}")
    _record_stage_done(
        "asr_beats", t0, slug=slug, channel=channel_path.stem,
        extra={"provider": asr_provider, "n_beats": len(beat_list)},
    )

    # Stage 5.5 — author per-beat prompts via claude CLI if not already
    # cached. The orchestrator-level cache means re-running on the same
    # slug skips this; deleting prompts.json is the way to force regen.
    prompts_path = cache / "prompts.json"
    # Tier-list ranks (from script.json) — used both as authoritative
    # hints for the LLM prompt author AND as the source of post-author
    # kit_lock token injection. Empty list for non-tier-list channels.
    script_path_for_ranks = _find_script_path(slug)
    ranks_for_prompts = (
        _load_ranks(script_path_for_ranks)
        if script_path_for_ranks else []
    )
    if not prompts_path.exists():
        try:
            prompts_mod.author_beat_prompts(
                narration=text,
                beats=beat_list,
                source_story=source_story or text,
                cast_narrator_desc=character_description,
                cast_default_emotion=cast_default_emotion,
                style_prefix=cfg.get("image_style_prefix", ""),
                opening_directives=opening_directives,
                out_path=prompts_path,
                narrator_visual_mode=cfg.get("narrator_visual_mode", "on_screen"),
                # cast.json supporting[] entries with full {name, aliases,
                # description, seed}. Prompt-author needs these in
                # voice_only mode so it can name the actual people from
                # the dossier in scene strings (no fictional narrator).
                supporting=cast_supporting_full or None,
                # Tier-list curator hints — image_prompt_hint per rank
                # is treated as authoritative ground truth by the LLM
                # (see prompts._SYSTEM rule #12). Empty for non-ranked.
                ranks=ranks_for_prompts or None,
            )
        except Exception as e:
            # If LLM authoring fails for any reason, fall through to the
            # heuristic. Better a degraded render than a hard failure.
            print(f"[prompts] WARNING: failed to author prompts: {e}")
            print(f"[prompts] continuing with heuristic prompts")
    # Server-side kit_lock injection — runs whether prompts were just
    # authored or were already cached. Idempotent (won't double-append).
    # Mirrors the sports cast-locked-tokens pattern that fixed Aguero v5.
    if ranks_for_prompts:
        _inject_kit_lock(prompts_path, ranks_for_prompts)

    # Stage 6 — visuals: either animated clips OR static images.
    t0 = time.time()

    # Memory hygiene: free the ASR model + any cached MPS allocations
    # before we load the multi-GB AnimateDiff/SD pipeline. M-series
    # Macs share unified memory, so holding both pipelines at once is
    # the OOM that bit us before.
    import gc
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available() and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
    except Exception:
        pass

    if motion_provider:
        # ---- animation path ------------------------------------------
        from pipeline import animation  # lazy: heavy diffusers chain

        custom_prompts = images.load_prompts(
            prompts_path,
            n_beats=len(beat_list),
            beat_texts=[b.text for b in beat_list],
        )
        if custom_prompts:
            print(f"[3/4] {motion_provider}: animating {len(beat_list)} clips (custom prompts)…")
        else:
            print(f"[3/4] {motion_provider}: animating {len(beat_list)} clips (heuristic prompts)…")

        # Opening directives validation (Principle #7).
        if custom_prompts:
            opening_scene = custom_prompts[0].get("scene", "")
            opening_warnings = _validate_opening_image(opening_scene, opening_directives)
            for w in opening_warnings:
                print(f"[opening_image] WARNING: {w}")

        clip_paths: list[Path] = []
        for i, b in enumerate(beat_list):
            p = cache / f"clip_{i:02d}.mp4"
            if not p.exists():
                if custom_prompts:
                    key_visual = custom_prompts[i]["key_visual"]
                    scene = custom_prompts[i]["scene"]
                else:
                    key_visual = ""
                    scene = animation.beat_to_prompt(b.text)

                # Last-beat icon embed (animation path mirrors slideshow).
                scene = _append_last_beat_icons(scene, i, len(beat_list), beat_text=b.text)

                # Channel-locked character + key-visual weighting baked in
                # (Principles #2, #11, #13). Cast routing per beat — see
                # build_full_prompt call further down for details.
                from pipeline.llm.cast_router import (
                    route_character_description, is_object_only_beat,
                )
                routed_desc, matched = route_character_description(
                    beat_text=b.text,
                    scene=scene,
                    key_visual=key_visual,
                    narrator_desc=character_description,
                    supporting_full=cast_supporting_full,
                )
                if matched:
                    print(f"[cast-router] beat {i} → {matched!r}")
                elif is_object_only_beat(scene, key_visual, b.text):
                    print(f"[cast-router] beat {i} → object-only (no character)")
                    routed_desc = None
                full_prompt = images.build_full_prompt(
                    style_prefix=cfg["image_style_prefix"],
                    character_description=routed_desc,
                    key_visual=key_visual,
                    scene=scene,
                )
                print(f"     [{i+1}/{len(beat_list)}] {b.duration:.2f}s — {scene[:60]}")
                animation.generate_clip(
                    # NOTE: seed is locked across beats on the motion path
                    # so the same character + scene base recurs throughout
                    # the Short. The text prompt drives the per-beat
                    # difference. Per DESIGN.md §14 #2/#14 character
                    # consistency: locked seed + identical character
                    # description; IP-Adapter on the still-image path
                    # gives stronger lock when used.
                    prompt=full_prompt,
                    style_prefix="",  # baked in
                    seed=cfg["image_seed"],
                    duration_s=b.duration,
                    out_path=p,
                    provider=motion_provider,
                    base_model=cfg.get("motion_base_model"),
                    motion_model=cfg.get("motion_adapter_model"),
                )
            else:
                print(f"     [{i+1}/{len(beat_list)}] cached")
            clip_paths.append(p)
        print(f"     done in {time.time() - t0:.1f}s")
        _record_stage_done(
            "motion_gen", t0, slug=slug, channel=channel_path.stem,
            extra={"provider": motion_provider, "n_clips": len(clip_paths)},
        )

        # Stage 7 — compose from clips
        t0 = time.time()
        print("[4/4] ffmpeg compose (clips)…")
        out_path = out_dir / "shorts" / f"{slug}.mp4"
        compose.compose_clips(
            clip_paths=clip_paths,
            beats=beat_list,
            audio_path=audio_path,
            out_path=out_path,
            cache_dir=cache,
            tail_hold_s=float(cfg.get("closer_hold_s", 0.0)),
        )
        _record_stage_done(
            "compose", t0, slug=slug, channel=channel_path.stem,
            extra={"phase": "compose_clips"},
        )
    else:
        # ---- slideshow path (current default) -------------------------
        # Footage attach moved up — must happen BEFORE image gen so
        # footage beats can skip the GPU. Pre-2026-05-05 the attach ran
        # in Stage 7 (compose), which meant a 25-beat rivalry recap
        # tagged 5 beats as footage but still ran 25 image gens, timing
        # out the Metal GPU around beat 15. Class-of-bug fix paired with
        # `covers_full_rank` span attach in `_attach_footage_to_beats`.
        # The yt-dlp/ffmpeg fetch stays in Stage 7 — only the tagging
        # moved.
        footage_overrides = []
        script_path_lookup = _find_script_path(slug)
        if script_path_lookup is not None:
            footage_overrides = _load_footage_overrides(script_path_lookup)
        n_footage_matched = 0
        if footage_overrides:
            n_footage_matched = _attach_footage_to_beats(beat_list, footage_overrides)
            print(f"[footage] tagged {n_footage_matched} beat(s) for footage cut-in (pre-image-gen)")

        # Form-level visual_source override (set by the create-page
        # Background-visuals card via `cfg_overrides`). v1 contract:
        #   - "ai"      → no-op; channel renders with its current image_provider.
        #   - "footage" → real-footage-only path. The Shorts pipeline
        #                 doesn't have a stock-footage fetcher, so this
        #                 only "works" on channels whose script.json
        #                 already carries footage data per beat (e.g.
        #                 sportsrecapped). On other channels we WARN and
        #                 fall back to AI rather than silently produce
        #                 a blank video. The dedicated footage-only
        #                 entrypoint (pipeline/render/footage_only.py)
        #                 is the right home for true footage-only
        #                 history/cosmos renders.
        #   - "both"    → the existing `compose_hybrid` path already
        #                 honours per-beat `kind: footage` tags, so this
        #                 is a no-op WHEN the script carries footage
        #                 data. On scripts without footage data we WARN.
        _visual_source = cfg.get("visual_source")
        if _visual_source in ("footage", "both") and n_footage_matched == 0:
            print(
                f"[visual_source] WARNING: requested visual_source="
                f"{_visual_source!r} but the script for slug={slug!r} carries no "
                f"per-beat footage data (script.footage[] is empty). "
                f"Falling back to AI image-gen. To use real footage on this "
                f"channel, author footage URLs in the script first, or use "
                f"the dedicated footage_only render path on history/cosmos "
                f"channels."
            )

        custom_prompts = images.load_prompts(
            prompts_path,
            n_beats=len(beat_list),
            beat_texts=[b.text for b in beat_list],
        )
        if custom_prompts:
            print(f"[3/4] {image_provider}: generating {len(beat_list)} images (custom prompts)…")
        else:
            print(f"[3/4] {image_provider}: generating {len(beat_list)} images (heuristic prompts)…")

        # b2-prewarm-on-render-start (2026-05-10): guarantee the cloud
        # image service finished its cold-load BEFORE the per-beat
        # /generate loop starts. Without this, the first /generate
        # raced the warmup thread and could pay 5-7 min of cold-load
        # tax on the critical path — the 33-min worst-case
        # image_attempt event in production was the symptom. Join with
        # 30s timeout: if /readyz hasn't returned by then, the cold-
        # load is still in flight and we'll just pay the rest inside
        # the first /generate (no worse than pre-fix). image_warmup_thread
        # is None for local providers — the if-guard makes this a no-op
        # there.
        if image_warmup_thread is not None and image_warmup_thread.is_alive():
            print("[warmup] waiting (≤30s) for image-gen /readyz to finish before stage 6…")
            _t = time.time()
            image_warmup_thread.join(timeout=30.0)
            print(f"[warmup] image-gen warmup wait: {time.time()-_t:.1f}s "
                  f"(still alive={image_warmup_thread.is_alive()})")

        # Validate the opening prompt against channel's opening directives
        # (Principle #7). Warn only — don't block the render.
        if custom_prompts:
            opening_scene = custom_prompts[0].get("scene", "")
            opening_warnings = _validate_opening_image(opening_scene, opening_directives)
            for w in opening_warnings:
                print(f"[opening_image] WARNING: {w}")

        # IP-Adapter setup (Principle #14). When enabled, condition every
        # generation on a reference image: the channel's locked
        # `character_reference_image` if set, else auto-bootstrap from
        # `img_00.png` (which is generated WITHOUT IP-Adapter so we have
        # something to anchor to).
        use_ip = bool(cfg.get("use_ip_adapter", False))
        ip_scale = float(cfg.get("ip_adapter_scale", 0.6))
        char_ref_cfg = cfg.get("character_reference_image")
        char_ref_path: Path | None = None
        if char_ref_cfg:
            p = Path(char_ref_cfg)
            if not p.is_absolute():
                p = channel_path.parent.parent / char_ref_cfg
            if p.exists():
                char_ref_path = p
            else:
                print(
                    f"[ip_adapter] WARNING: character_reference_image "
                    f"{char_ref_cfg} not found; will auto-bootstrap from "
                    f"img_00.png"
                )

        # Join the warm-up thread before the first generate() so any
        # load-time errors print before image-gen logs and the global
        # _PIPE is observably ready. If warmup is already done this is
        # instant; if it's still loading, the join cost was unavoidable
        # anyway. mflux skips the threaded warmup (see comment near the
        # spawn site) so warmup_thread is None on that path.
        if warmup_thread is not None:
            if warmup_thread.is_alive():
                print("[warmup] waiting for diffusion pipeline to finish loading…")
            warmup_thread.join()

        # Streaming-compose precursor: word_NNNN.png caption rendering is
        # pure CPU, depends only on beat alignment, and currently runs
        # serially inside compose() after the entire image stage. Pre-
        # wipe stale per-beat artefacts NOW (so compose's safety wipe
        # doesn't erase what we render in the next step) and kick off
        # the prerender on a background thread that runs alongside GPU
        # image gen. Compose then no-ops the render for files already
        # on disk. Saves ~7-15s per render (150 word PNGs × ~50ms) and
        # lays the groundwork for true streaming compose later.
        compose.wipe_stale_per_beat_artefacts(cache, len(beat_list))
        caption_prerender_thread = threading.Thread(
            target=_safe_prerender_word_captions,
            args=(beat_list, cache),
            name="captions.prerender",
            daemon=True,
        )
        caption_prerender_thread.start()

        image_paths: list[Path] = []
        import hashlib as _hashlib

        def _prompt_hash(kv: str, scene: str, char_desc: str, seed: int) -> str:
            """Content hash for an image's source prompt. Used as a sidecar
            cache key so img_NN.png is regenerated when the prompt content
            changes (e.g. ASR re-aligns the beat split between renders and
            prompts.json gets re-authored)."""
            blob = "|".join([str(kv or ""), str(scene or ""),
                             str(char_desc or ""), str(seed)])
            return _hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

        # b4-image-cold-load-budget: track first-image cold-load tax
        # per render. The metric we care about isn't "how long does
        # /generate take" (we already have that per-attempt) but "how
        # much wall-clock did the first image cost relative to the
        # render's start". A first-image dt of 8 s on a warm container
        # is healthy; a first-image dt of 33 min on a cold one is the
        # elephant. Recording it as `cold_load_s` on the FIRST
        # image_attempt makes the dashboard's image-retry rollup
        # surface cold-load tax explicitly per render.
        #
        # 2026-05-11: mutable single-element container + lock so the
        # nested per-beat function can atomically check-and-clear from
        # multiple ThreadPool workers when image fan-out is enabled
        # (cloud providers, see dispatcher below). Pre-fix the bare
        # ``_first_image_pending = True`` rebind worked fine for the
        # serial loop but broke under fan-out — multiple beat threads
        # could each see ``True`` before any of them set it ``False``,
        # causing N parallel renders to all stamp ``cold_load_s`` and
        # poison the dashboard's per-render cold-load tax view.
        import threading as _threading  # noqa: PLC0415
        from concurrent.futures import (  # noqa: PLC0415
            ThreadPoolExecutor, as_completed,
        )
        _first_image_pending = [True]
        _first_image_lock = _threading.Lock()

        def _render_one_beat(i: int, b) -> Path:
            """Render the image for beat ``i``. Pure-ish closure: reads
            from the enclosing render scope (cfg, image_provider,
            char_ref_path, supporting_chars, custom_prompts, …),
            writes to disk (img_NN.png + img_NN.prompt.sha256
            sidecar) and to the telemetry stream. Returns the image
            path on disk.

            Designed to be called either sequentially (beat 0 — IP-
            Adapter bootstrap + cold-load priming, see dispatcher
            below) OR from a ThreadPoolExecutor worker (beats 1..N
            when ``image_provider`` is ``cloudrun_*``). Per-beat state
            (key_visual, scene, routed_desc, base_seed, hash) is
            fully local; the only mutable shared state read/written
            is ``_first_image_pending`` (atomic via
            ``_first_image_lock``) and the cloud circuit breaker
            inside ``pipeline.images.images_cloudrun`` (already
            ``_BREAKER_LOCK``-protected).
            """
            p = cache / f"img_{i:02d}.png"
            hash_path = cache / f"img_{i:02d}.prompt.sha256"

            # Footage beats use the broadcast clip in compose; generating
            # an image is wasted GPU. The placeholder path keeps
            # `image_paths` index-aligned with `beat_list` for compose's
            # downstream `image_paths[i]` reads. The missing-file pre-flight
            # below filters footage indices so the placeholder doesn't
            # trip the existence check.
            if b.kind == "footage":
                print(f"     [{i+1}/{len(beat_list)}] footage beat — skip image gen")
                print(f"[image-done] beat {i} of {len(beat_list)}")
                return p

            # Decide IP-Adapter reference for THIS beat.
            if not use_ip:
                ip_ref = None
            elif char_ref_path is not None:
                ip_ref = char_ref_path
            elif i == 0:
                # Bootstrap: first beat has no reference yet.
                ip_ref = None
            else:
                # Reuse the first generated image to lock the character.
                # This is why beat 0 must run sequentially BEFORE any
                # beat with i > 0 is dispatched (see dispatcher below).
                bootstrap = cache / "img_00.png"
                ip_ref = bootstrap if bootstrap.exists() else None

            # Pre-compute the content hash for this beat's prompt. The
            # cache hit check below requires both img_NN.png AND a
            # matching img_NN.prompt.sha256 sidecar. Without this,
            # changing prompts.json (e.g. after a TTS rerun shifts the
            # beat split) silently reuses stale images keyed by index —
            # the AV-sync bug caught on Iniesta v2.
            if custom_prompts:
                _kv_for_hash = custom_prompts[i].get("key_visual", "")
                _scene_for_hash = custom_prompts[i].get("scene", "")
            else:
                _kv_for_hash = ""
                _scene_for_hash = images.beat_to_prompt(b.text)
            _seed_for_hash, _ = _seed_for_beat(
                scene=_scene_for_hash,
                key_visual=_kv_for_hash,
                supporting_chars=supporting_chars,
                default_seed=cfg["image_seed"],
            )
            current_hash = _prompt_hash(
                _kv_for_hash, _scene_for_hash, character_description or "",
                _seed_for_hash,
            )
            cache_valid = p.exists()
            if cache_valid and hash_path.exists():
                try:
                    cache_valid = hash_path.read_text().strip() == current_hash
                except OSError:
                    cache_valid = False
            elif cache_valid and not hash_path.exists():
                # Image exists but no hash sidecar (legacy or never
                # written) — treat as stale to be safe. Forces regen
                # once; subsequent runs hit the hash-matched cache.
                cache_valid = False

            if not cache_valid:
                # Drop the stale image so the existing `not p.exists()`
                # branch below kicks in and regenerates.
                if p.exists():
                    p.unlink()

            if not p.exists():
                if custom_prompts:
                    key_visual = custom_prompts[i]["key_visual"]
                    scene = custom_prompts[i]["scene"]
                else:
                    key_visual = ""
                    scene = images.beat_to_prompt(b.text)

                # Embed YouTube-style like + subscribe iconography INTO
                # the LAST beat's image (no overlay, no separate panel).
                # See _append_last_beat_icons docstring for rationale.
                scene = _append_last_beat_icons(scene, i, len(beat_list), beat_text=b.text)

                # Lint the user-authored scene only — character description
                # and style prefix are channel-level and always present.
                for w in images.lint_prompt(scene):
                    print(f"[images] WARNING (beat {i}): {w}")

                # Cast routing (Stage 6.4): if this beat names a supporting
                # character, prepend THAT character's description instead of
                # the narrator's. Without this, the narrator is the only
                # body that ever appears on screen even when the narration
                # is about someone else.
                from pipeline.llm.cast_router import (  # noqa: PLC0415
                    route_character_description, is_object_only_beat,
                )
                routed_desc, matched = route_character_description(
                    beat_text=b.text,
                    scene=scene,
                    key_visual=key_visual,
                    narrator_desc=character_description,
                    supporting_full=cast_supporting_full,
                )
                if matched:
                    print(f"[cast-router] beat {i} → {matched!r}")
                elif is_object_only_beat(scene, key_visual, b.text):
                    # Object-only scene (a calendar, three wine bottles…) —
                    # don't force a person into the frame.
                    print(f"[cast-router] beat {i} → object-only (no character)")
                    routed_desc = None

                # Flux doesn't parse compel-style (text:weight) syntax,
                # so emit unweighted prompts on the mflux path.
                full_prompt = images.build_full_prompt(
                    style_prefix=cfg["image_style_prefix"],
                    character_description=routed_desc,
                    key_visual=key_visual,
                    scene=scene,
                    weighted=(image_provider != "mflux"),
                )

                tag = f" KV={key_visual[:40]!r}" if key_visual else ""
                ip_tag = f" ip={ip_ref.name}" if ip_ref else ""
                print(f"     [{i+1}/{len(beat_list)}] {scene[:60]}{tag}{ip_tag}")
                # Seed is locked across beats (DESIGN.md §6 — character
                # consistency strategy). character_description prepend +
                # identical seed is the v0 lock; per-beat seed variation
                # was fighting it.
                #
                # Quality-gate retries: if Flux/SDXL produces an obviously
                # broken image (all-black, low edge density), bump the
                # seed and regenerate up to MAX_QC_RETRIES times.
                from pipeline.llm import quality_gate  # noqa: PLC0415
                # Bumped 2 → 3 in 2026-05 alongside the tightened
                # luminance gate in quality_gate.py. The new mean/P75
                # luminance checks reject more images (the dark-frame
                # regression that previously slipped through), so the
                # extra retry budget is what keeps the gate from
                # giving up on a beat that would have rendered cleanly
                # on a different seed.
                MAX_QC_RETRIES = 3
                # Per-character seed lock — when this beat's scene names
                # a supporting character (Aguero, Dzeko, etc.), use that
                # character's deterministic seed so every beat featuring
                # them renders the SAME face. Beats with no character
                # match (or multi-character beats) use the channel seed.
                base_seed, _matched_char = _seed_for_beat(
                    scene=scene,
                    key_visual=key_visual,
                    supporting_chars=supporting_chars,
                    default_seed=cfg["image_seed"],
                )
                if _matched_char and base_seed != cfg["image_seed"]:
                    print(f"     [seed-lock] beat {i} → "
                          f"{_matched_char!r} seed={base_seed}")
                # Track per-beat wall time + retry count so the
                # post-image-stage summary surfaces which beats burned
                # the budget. Without this, the only visible signal was
                # "stage 3 took 30 minutes" with no per-beat attribution.
                beat_t0 = time.time()
                attempts_used = 0
                for attempt in range(MAX_QC_RETRIES + 1):
                    attempts_used = attempt + 1
                    seed = base_seed + (attempt * 1000) + (i if attempt else 0)
                    attempt_t0 = time.time()
                    images.generate(
                        prompt=full_prompt,
                        style_prefix="",  # already baked into full_prompt
                        seed=seed,
                        out_path=p,
                        width=cfg.get("image_width", 768),
                        height=cfg.get("image_height", 1344),
                        steps=cfg.get("image_steps", 4),
                        provider=image_provider,
                        ip_adapter_image=ip_ref,
                        ip_adapter_scale=ip_scale,
                        extra_negative=cfg.get("negative_prompt"),
                        force_positive=cfg.get("force_positive"),
                    )
                    qc_kwargs: dict = {}
                    if "image_min_mean_luminance" in cfg:
                        qc_kwargs["min_mean_luminance"] = float(cfg["image_min_mean_luminance"])
                    if "image_min_p75_luminance" in cfg:
                        qc_kwargs["min_p75_luminance"] = float(cfg["image_min_p75_luminance"])
                    ok, reason = quality_gate.check_image(
                        p,
                        expected_w=cfg.get("image_width", 768),
                        expected_h=cfg.get("image_height", 1344),
                        reject_text_artefacts=bool(cfg.get("quality_gate_ocr", False)),
                        **qc_kwargs,
                    )
                    attempt_dt = time.time() - attempt_t0
                    # Per-attempt structured line. Without this, a beat that
                    # silently retries N times under the QC gate looks
                    # identical in telemetry to a beat that ran clean — and
                    # N retries means N× wall time. The web server parses
                    # this into an image/progress event so the rollups show
                    # silent retry doubling instead of hiding it inside a
                    # single beat duration.
                    qc_tag = "pass" if ok else "fail"
                    qc_reason = "" if ok else f" reason={reason!r}"
                    print(
                        f"[image-attempt] beat {i} attempt {attempt+1}/"
                        f"{MAX_QC_RETRIES + 1} dt={attempt_dt:.1f}s "
                        f"qc={qc_tag}{qc_reason}"
                    )
                    # Atomic check-and-clear under lock so exactly ONE
                    # image_attempt event records cold_load_s when image
                    # fan-out runs across multiple beat workers. The
                    # captured `_was_first` is then used inline below to
                    # decide whether to inject the cold_load_s field —
                    # check + emit happen as one atomic step from the
                    # caller's perspective.
                    with _first_image_lock:
                        _was_first = _first_image_pending[0]
                        if _was_first:
                            _first_image_pending[0] = False
                    # Structured per-attempt event for the latency dashboard.
                    # The stdout line above is for humans tailing logs; this
                    # one feeds /api/telemetry/latency's image-retry rollup.
                    # Belt-and-braces: emitting from inside the subprocess
                    # means we don't depend on the server-side stdout parser
                    # to land the event (parsed lines can race with EOF).
                    tlm.track(
                        "image_attempt",
                        category="pipeline",
                        success=ok,
                        duration_ms=int(attempt_dt * 1000),
                        metadata={
                            "stage": "image",
                            "niche": channel_path.stem,
                            "slug": slug,
                            "beat": i,
                            "attempt": attempt + 1,
                            "max_attempts": MAX_QC_RETRIES + 1,
                            "qc": qc_tag,
                            "qc_reason": (reason if not ok else None),
                            "provider": image_provider,
                            "seed": seed,
                            # b4: only the FIRST image_attempt of the
                            # render carries cold_load_s = wall-clock
                            # delta from render start to this attempt's
                            # completion. The atomic check-and-clear
                            # under _first_image_lock above guarantees
                            # exactly one beat (in either serial OR
                            # parallel mode) gets _was_first == True.
                            **(
                                {"cold_load_s": round(time.time() - _render_t0, 2)}
                                if _was_first else {}
                            ),
                        },
                    )
                    if ok:
                        break
                    print(
                        f"     [qc] beat {i} attempt {attempt+1} rejected: {reason}"
                    )
                    if attempt < MAX_QC_RETRIES:
                        print(f"     [qc] retrying with seed {seed + 1000}…")
                else:
                    # All retries failed — keep the last image and warn.
                    print(
                        f"     [qc] WARNING: beat {i} failed quality gate after "
                        f"{MAX_QC_RETRIES + 1} attempts; shipping anyway"
                    )
                beat_dt = time.time() - beat_t0
                tag = "" if attempts_used == 1 else f" (over {attempts_used} attempts)"
                print(f"     [beat-time] beat {i}: {beat_dt:.1f}s{tag}")
                # Persist the prompt-content hash next to the image so
                # the next render's cache check can verify the image
                # still corresponds to the current prompt. See the
                # _prompt_hash helper above for the schema.
                try:
                    hash_path.write_text(current_hash)
                except OSError as e:
                    print(f"     [cache] WARN: failed to write {hash_path.name}: {e}")
            else:
                print(f"     [{i+1}/{len(beat_list)}] cached")
            # Signal to the web UI that img_NN.png is on disk and safe
            # to fetch for the live slideshow. Parsed by web/server.py
            # into an image/done event ({i, total}). Always emitted —
            # both fresh-rendered AND cache-hit beats — so the slideshow
            # populates immediately on a re-run with cached images.
            print(f"[image-done] beat {i} of {len(beat_list)}")
            return p

        # 2026-05-11 image fan-out dispatcher.
        #
        # Run beat 0 sequentially first. Two reasons:
        #   1. **IP-Adapter bootstrap dependency.** When ``use_ip`` is
        #      True and ``char_ref_path`` is None, every beat with
        #      ``i > 0`` reads ``img_00.png`` as its reference (see
        #      _render_one_beat above). Beat 0 must finish writing
        #      its file before parallel workers start, otherwise
        #      beats 1..N race with an empty disk and lose the
        #      character lock.
        #   2. **Cold-load priming.** The first /generate against a
        #      cold ``cloudrun_flux2_klein`` triggers ~5-7 min of
        #      model-load. Doing this on a single worker first means
        #      parallel containers each pay their cold-load only
        #      ONCE — beat 0 hits the warmest container, then later
        #      beats hit the second/third instance as they spin up.
        #      Without sequential beat 0, all N workers race against
        #      the same cold container and the breaker can trip on
        #      transient 503s before the first warm response lands.
        if beat_list:
            results: dict[int, Path] = {0: _render_one_beat(0, beat_list[0])}
        else:
            results = {}

        # Beats 1..N-1: fan out on cloud providers, serial otherwise.
        # Local providers (mflux, sdxl_lightning, z_image_turbo on the
        # laptop path) share a single Apple GPU; threading them only
        # adds GIL contention with zero compute win.
        remaining = list(enumerate(beat_list))[1:]
        _is_cloud_image = image_provider.startswith("cloudrun_")
        # Default 2 = leaves 1 slot of the cloud service's
        # ``--max-instances=3`` ceiling free for cross-render bulk
        # overlap (scripts/ops/bulk_render_queue.py). Bump via the
        # ``YTFACTORY_IMAGE_WORKERS`` env to test wider fan-out, but
        # remember to verify the L4 GPU quota in asia-southeast1 has
        # headroom — see cloud/image-flux2-klein/deploy.sh comment
        # block + docs/cloud_cost_2026_05_11.md watch-list.
        _image_workers = int(os.environ.get("YTFACTORY_IMAGE_WORKERS", "2"))
        if remaining and _is_cloud_image and _image_workers > 1:
            print(
                f"[image] cloud fan-out: {len(remaining)} beats × "
                f"{_image_workers} workers (provider={image_provider})"
            )
            with ThreadPoolExecutor(
                max_workers=_image_workers,
                thread_name_prefix="image",
            ) as pool:
                futures = {pool.submit(_render_one_beat, i, b): i
                           for i, b in remaining}
                # ``as_completed`` re-raises the first exception when
                # ``fut.result()`` is called. Pending workers continue
                # to completion — same semantics as the long_form TTS
                # fan-out (pipeline/render/long_form.py:362). The
                # missing-files preflight below catches any beat that
                # didn't produce a file with a structured RuntimeError.
                for fut in as_completed(futures):
                    i = futures[fut]
                    results[i] = fut.result()
        else:
            for i, b in remaining:
                results[i] = _render_one_beat(i, b)

        # Reassemble in beat order so compose sees ``image_paths[i]``
        # for beat ``i``. Parallel completion order doesn't matter
        # to compose (which iterates by index), but the missing-files
        # preflight below and the downstream slideshow code assume
        # positional alignment with ``beat_list``.
        image_paths = [results[i] for i in range(len(beat_list))]

        print(f"     done in {time.time() - t0:.1f}s")
        _record_stage_done(
            "image_gen", t0, slug=slug, channel=channel_path.stem,
            extra={"provider": image_provider, "n_images": len(image_paths)},
        )

        # Pre-flight gate: every img_NN.png the compose stage will hand
        # to ffmpeg must exist. We've seen jobs where the image loop
        # silently skipped a beat (cached miss, exception swallowed
        # upstream) and ffmpeg crashed 30s into compose with
        # "Error opening input file img_10.png" — wasting all the wall
        # time the image stage ate. Fail fast here with a structured
        # error the latency view can fingerprint, instead of letting
        # ffmpeg explode opaquely.
        missing = [
            p for i, p in enumerate(image_paths)
            if beat_list[i].kind != "footage" and not p.exists()
        ]
        if missing:
            names = ", ".join(p.name for p in missing[:5])
            extra = "" if len(missing) <= 5 else f" (+{len(missing)-5} more)"
            raise RuntimeError(
                f"image stage incomplete: {len(missing)} of "
                f"{len(image_paths)} expected images missing on disk: "
                f"{names}{extra}. Refusing to invoke ffmpeg compose."
            )

        # Stage 7 — compose from images.
        # Closer panel + tail-hold are channel-config-driven. The panel
        # content is derived from `closer_format` (a single string in the
        # channel YAML) — nothing is hardcoded in captions.py. This keeps
        # the AITA "LIKE if YTA / COMMENT if NTA" format swappable per
        # channel, and per memory feedback the AITA closer must be the
        # like/comment split (never vague "vote in comments").
        tail_hold_s = float(cfg.get("closer_hold_s", 0.0))
        closer_panel_path: Path | None = None
        closer_format = cfg.get("closer_format")
        if closer_format:
            print(f"[compose] closer panel will be rendered inside compose() from closer_format: {closer_format!r}")

        # Tier-list rank chips ("#5" → "#1"). Channel YAML opt-in via
        # `ranked_chips: true`. Each chip spans from its rank's opening
        # beat ("Number five." / "Number four." / ...) to the NEXT
        # rank's opening beat — covering the full segment of beats that
        # describe that rank, not just the announcement beat. The last
        # rank (#1) spans to the final beat (where the closer panel
        # takes over). Beat splitter is finer-grained than the rank
        # structure, so this windowing is required for correctness.
        # Pass rank INTS, not paths — compose owns the PNG lifecycle so
        # `_wipe_stale_per_beat_artefacts` (which globs rank_chip_*.png)
        # can never delete a chip out from under ffmpeg. Same fix shape
        # as the closer_panel render-after-wipe contract above.
        rank_chips: list[tuple[int, int, int]] = []  # (start_idx, end_idx, rank)
        if cfg.get("ranked_chips"):
            _RANK_PHRASE = {
                "number five": 5, "number four": 4, "number three": 3,
                "number two": 2, "number one": 1,
            }
            rank_starts: list[tuple[int, int]] = []
            for i, b in enumerate(beat_list):
                bt = (b.text or "").lower().lstrip()
                for phrase, rank in _RANK_PHRASE.items():
                    if bt.startswith(phrase):
                        rank_starts.append((i, rank))
                        break
            rank_starts.sort(key=lambda r: r[0])
            for k, (start_idx, rank) in enumerate(rank_starts):
                end_idx = (rank_starts[k + 1][0]
                           if k + 1 < len(rank_starts)
                           else len(beat_list) - 1)
                rank_chips.append((start_idx, end_idx, rank))
            if rank_chips:
                print(f"[compose] {len(rank_chips)} rank chip(s) queued; "
                      f"windows: {rank_chips}")
            else:
                print("[compose] ranked_chips=true but no beat opens with "
                      "'Number five/four/three/two/one' — chips skipped")

        # Per-beat footage attach already happened pre-image-gen (see
        # block at slideshow path entry). Here we just fetch the
        # broadcast mp4s for already-tagged beats; no second tagging
        # pass. `n_footage_matched` is recomputed from the tagged beats.
        n_footage_matched = sum(1 for b in beat_list if b.kind == "footage")
        footage_clip_paths: dict[int, Path] = {}
        if n_footage_matched:
            from pipeline.footage import footage as _footage
            for i, b in enumerate(beat_list):
                if b.kind != "footage" or not b.footage:
                    continue
                fp = cache / f"footage_{i:02d}.mp4"
                # ``black_intro`` flag — when set in script.footage[],
                # prepends a black-screen + silence pad of length
                # equal to the beat's narration duration PLUS a small
                # tail-breath buffer. The narrator's setup line ("Last
                # kick of the season") plays over the black, building
                # suspense; the buffer (default 0.30s) gives the
                # word-final consonant room to finish before the
                # broadcast hard-cuts in. ASR's beat.end is sometimes
                # 50-200ms shy of Kokoro's actual word tail (caught
                # 2026-05-02 on v13b — "season" got chopped). The
                # buffer is overridable via script.footage[].black_intro_buffer_s.
                pre_pad_s = 0.0
                if b.footage.get("black_intro"):
                    buf = float(b.footage.get("black_intro_buffer_s", 0.30))
                    pre_pad_s = max(0.0, (b.end - b.start) + buf)
                    # Stash the actual pad length on the beat so compose
                    # can adjust the duck window without re-deriving it.
                    b.footage["pre_pad_s"] = pre_pad_s
                _footage.fetch_clip(
                    url=b.footage["url"],
                    in_s=float(b.footage["in_s"]),
                    out_s=float(b.footage["out_s"]),
                    out_path=fp,
                    pre_pad_s=pre_pad_s,
                )
                footage_clip_paths[i] = fp

        t0 = time.time()
        out_path = out_dir / "shorts" / f"{slug}.mp4"
        if n_footage_matched:
            print(f"[4/4] ffmpeg compose (hybrid: {n_footage_matched} footage + "
                  f"{len(beat_list) - n_footage_matched} image beats)…")
            beat_resolutions: list[tuple[str, Path]] = []
            for i, b in enumerate(beat_list):
                if b.kind == "footage":
                    beat_resolutions.append(("footage", footage_clip_paths[i]))
                else:
                    beat_resolutions.append(("image", image_paths[i]))
            compose.compose_hybrid(
                beat_resolutions=beat_resolutions,
                beats=beat_list,
                audio_path=audio_path,
                out_path=out_path,
                cache_dir=cache,
                tail_hold_s=tail_hold_s,
                rank_chips=rank_chips or None,
            )
            print(f"     done in {time.time() - t0:.1f}s")
            _record_stage_done(
                "compose", t0, slug=slug, channel=channel_path.stem,
                extra={"phase": "compose_hybrid"},
            )
            print(f"\n✓ wrote {out_path}")
            # v1: footage-bearing renders skip critic regen (Stage 7.5)
            # and auto-upload (Stage 8). Critic doesn't yet route through
            # compose_hybrid, so its recompose would silently drop the
            # footage cuts. Re-render explicitly to iterate.
            _print_render_summary(slug, channel_path.stem, _render_t0)
            return out_path
        # Track stage 4 (slideshow compose) start so the stage_done emit
        # below covers ONLY the ffmpeg compose call, not the image gen
        # block above (which already emitted "image_gen").
        compose_t0 = time.time()
        print("[4/4] ffmpeg compose (slideshow)…")
        compose.compose(
            image_paths=image_paths,
            beats=beat_list,
            audio_path=audio_path,
            out_path=out_path,
            cache_dir=cache,
            tail_hold_s=tail_hold_s,
            closer_panel_path=closer_panel_path,
            closer_format=closer_format,
            rank_chips=rank_chips or None,
        )
        print(f"     done in {time.time() - compose_t0:.1f}s")
        _record_stage_done(
            "compose", compose_t0, slug=slug, channel=channel_path.stem,
            extra={"phase": "compose_slideshow"},
        )

    print(f"\n✓ wrote {out_path}")

    # Stage 7.5 — auto-critique. If score < min_critic_score, patch the
    # weak beats' prompts and regenerate just those images, then
    # recompose ONCE. Single retry; second compose is final regardless.
    score: int | None = None
    if run_critic:
        try:
            from pipeline.llm import critic
            min_score = int(cfg.get("min_critic_score", 6))
            critique = critic.critique_short(
                slug=slug,
                mp4_path=out_path,
                cache_dir=cache,
                out_dir=out_dir / "critiques" / slug,
            )
            score = int(critique.get("score", 0) or 0)
            corrections = critique.get("beat_corrections") or {}
            if score < min_score and corrections:
                print(
                    f"\n[critic] score {score} < {min_score} — "
                    f"patching {len(corrections)} beat(s) and re-rendering…"
                )
                patched = critic.regenerate_with_corrections(
                    slug=slug,
                    cache_dir=cache,
                    beat_corrections=corrections,
                )
                if patched:
                    # Re-run image gen for the patched beats only.
                    # Emit the same `[3/4] ...generating N images` and
                    # `[image-done] beat i of n` markers the initial
                    # image stage uses, so the web SSE parser
                    # (web/server.py:parse_stdout_line) reports
                    # progress during this phase too. Without these,
                    # the browser sees "critic done" and then nothing
                    # for ~10 min while images regenerate, which reads
                    # as "the job hung" even though work is happening.
                    n_patched = len(patched)
                    print(
                        f"[3/4] {image_provider}: generating {n_patched} "
                        f"images (critic regen)…"
                    )
                    custom_prompts = images.load_prompts(
                        cache / "prompts.json",
                        n_beats=len(beat_list),
                        beat_texts=[b.text for b in beat_list],
                    )
                    from pipeline.llm import quality_gate
                    for n, i in enumerate(sorted(patched)):
                        b = beat_list[i]
                        kv = custom_prompts[i]["key_visual"]
                        scene = custom_prompts[i]["scene"]
                        # Last-beat icon embed on the critic-regen path
                        # too — guarantees the like/subscribe icons make
                        # it into beat N-1 even if the critic patched it.
                        scene = _append_last_beat_icons(
                            scene, i, len(beat_list),
                            beat_text=beat_list[i].text,
                        )
                        # Cast routing on the critic-regen path too (see
                        # earlier build_full_prompt call). Same rationale.
                        from pipeline.llm.cast_router import (
                            route_character_description, is_object_only_beat,
                        )
                        routed_desc, matched = route_character_description(
                            beat_text=b.text,
                            scene=scene,
                            key_visual=kv,
                            narrator_desc=character_description,
                            supporting_full=cast_supporting_full,
                        )
                        if matched:
                            print(f"[cast-router] beat {i} (regen) → {matched!r}")
                        elif is_object_only_beat(scene, kv, b.text):
                            print(f"[cast-router] beat {i} (regen) → object-only")
                            routed_desc = None
                        full_prompt = images.build_full_prompt(
                            style_prefix=cfg["image_style_prefix"],
                            character_description=routed_desc,
                            key_visual=kv,
                            scene=scene,
                            weighted=(image_provider != "mflux"),
                        )
                        p = cache / f"img_{i:02d}.png"
                        # `[N/M]` line matches _RE_IMG_BEAT in the
                        # SSE parser → browser shows live progress.
                        print(f"     [{n+1}/{n_patched}] {scene[:60]} (beat {i})")
                        # Use a different seed than the original to
                        # ensure we don't get the same broken output.
                        images.generate(
                            prompt=full_prompt,
                            style_prefix="",
                            seed=cfg["image_seed"] + 7777,
                            out_path=p,
                            width=cfg.get("image_width", 768),
                            height=cfg.get("image_height", 1344),
                            steps=cfg.get("image_steps", 4),
                            provider=image_provider,
                            extra_negative=cfg.get("negative_prompt"),
                        force_positive=cfg.get("force_positive"),
                        )
                        qc_kwargs2: dict = {}
                        if "image_min_mean_luminance" in cfg:
                            qc_kwargs2["min_mean_luminance"] = float(cfg["image_min_mean_luminance"])
                        if "image_min_p75_luminance" in cfg:
                            qc_kwargs2["min_p75_luminance"] = float(cfg["image_min_p75_luminance"])
                        ok, reason = quality_gate.check_image(
                            p,
                            expected_w=cfg.get("image_width", 768),
                            expected_h=cfg.get("image_height", 1344),
                            reject_text_artefacts=bool(cfg.get("quality_gate_ocr", False)),
                            **qc_kwargs2,
                        )
                        if not ok:
                            print(f"[critic] regen image {i} failed QC: {reason}")
                        # `[image-done]` marker so the slideshow flips
                        # the regenerated thumb in real time. Use the
                        # absolute beat index — the slideshow is keyed
                        # by beat, not by regen position.
                        print(f"[image-done] beat {i} of {len(beat_list)}")

                    # Recompose with the same closer-panel + tail-hold.
                    print("[critic] recomposing…")
                    compose.compose(
                        image_paths=image_paths,
                        beats=beat_list,
                        audio_path=audio_path,
                        out_path=out_path,
                        cache_dir=cache,
                        tail_hold_s=tail_hold_s,
                        closer_format=closer_format,
                        pass_label="recompose",
                        rank_chips=rank_chips or None,
                    )
                    print(f"✓ re-rendered {out_path}")
            elif score < min_score:
                print(
                    f"[critic] score {score} < {min_score} but no "
                    f"beat-level corrections — accepting as-is"
                )
        except Exception as e:
            print(f"[critic] skipped (non-fatal): {e}")

    # Stage 8 — optional auto-upload to YouTube. Gated on the channel
    # YAML's `upload.auto_upload: true` so adding the upload block by
    # itself doesn't change behaviour.
    upload_cfg = cfg.get("upload") or {}
    do_upload = upload_cfg.get("auto_upload", False)
    if upload_override is not None:
        do_upload = upload_override
    if do_upload:
        min_upload_score = int(upload_cfg.get("min_score", 0))
        if require_critic and score is None:
            # Cron / agent path: critique must succeed before any upload.
            # Refuse to ship a video that hasn't been graded by /critique-video,
            # because YouTube's algo punishes low-quality early uploads on
            # a channel and we'd rather skip than ship blind.
            print(
                f"[upload] skipped — --require-critic was set but the "
                f"critic produced no score (run_critic={run_critic}, "
                f"critique={'authored' if score is not None else 'missing'})"
            )
            do_upload = False
        elif score is not None and score < min_upload_score:
            print(
                f"[upload] skipped — critic score {score} < "
                f"upload.min_score {min_upload_score} (channel YAML)"
            )
            do_upload = False
        else:
            try:
                from pipeline import upload as up_mod

                # Locate channel_dir + script + raw the same way upload.py does.
                # Recursive scan supports nested layouts (parent/variant/scripts/<slug>.json).
                script_obj: dict = {}
                raw_obj: dict | None = None
                channel_dir = ""
                sp = _scan_intermediate(slug, "scripts")
                if sp is not None:
                    channel_dir = _channel_dir_for(slug, "scripts")
                    try:
                        script_obj = json.loads(sp.read_text())
                    except json.JSONDecodeError:
                        pass
                    rp = sp.parent.parent / "raw" / f"{slug}.json"
                    if rp.exists():
                        try:
                            raw_obj = json.loads(rp.read_text())
                        except json.JSONDecodeError:
                            raw_obj = None
                if not channel_dir:
                    # --text mode: no script/raw on disk. Build a minimal
                    # script dict so derive_metadata has something.
                    script_obj = {"slug": slug, "hook": text[:80], "title_options": []}
                    channel_dir = "_adhoc"

                up_mod.upload_short(
                    project_root=Path(".").resolve(),
                    channel_yaml=cfg,
                    channel_dir=channel_dir,
                    slug=slug,
                    mp4_path=out_path,
                    script=script_obj,
                    raw=raw_obj,
                )
            except Exception as e:
                print(f"[upload] skipped (non-fatal): {e}")

    # Refresh the research dashboard's flat index so /api/research/* and
    # the website pick up this render without manual rebuild.
    # Best-effort: never let an index error fail an otherwise-successful render.
    try:
        from pipeline import research as _research
        _research.rebuild(quiet=True)
    except Exception as e:
        print(f"[research] rebuild skipped (non-fatal): {e}")

    _print_render_summary(slug, channel_path.stem, _render_t0)
    return out_path


def _print_render_summary(slug: str, channel: str, render_t0: float) -> None:
    """Print a one-line summary of the slowest stage and total wall-clock.

    Reads `stage_done` events from this render (filtered by job_id when
    set, slug otherwise) so the operator can see at a glance which
    stage burned the most time without opening the dashboard.

    Read-only: pulls events from the in-memory parse cache via
    `tlm.read_events()`, never raises (latency-summary failure must
    not fail an otherwise-successful render).
    """
    try:
        events = tlm.read_events(since_ts=render_t0 - 1.0)
        job_id = os.environ.get("YTFACTORY_JOB_ID") or None
        stages: list[tuple[str, int]] = []
        for e in events:
            if e.get("event") != "stage_done":
                continue
            md = e.get("metadata") or {}
            if md.get("slug") != slug or md.get("niche") != channel:
                continue
            if job_id and e.get("job_id") and e["job_id"] != job_id:
                continue
            dur = e.get("duration_ms")
            if dur is None:
                continue
            stages.append((md.get("stage") or "?", int(dur)))
        total_s = time.time() - render_t0
        if not stages:
            print(f"[render-summary] slug={slug} total={total_s:.1f}s "
                  f"(no stage_done events captured)")
            return
        slowest = max(stages, key=lambda x: x[1])
        per_stage = " ".join(f"{n}={d/1000:.0f}s" for n, d in stages)
        print(f"[render-summary] slug={slug} total={total_s:.1f}s "
              f"slowest={slowest[0]} ({slowest[1]/1000:.1f}s) | {per_stage}")
    except Exception as e:
        # Pure observability — never block a render on a summary print.
        print(f"[render-summary] skipped (non-fatal): {e!r}")


def cli_main() -> None:
    """CLI entry point. Invoked by ``scripts/make_shorts.py`` (the thin shim).

    Renamed from ``main()`` during the 2026-05-05 renderer-promotion
    refactor; the old name is kept as an alias so any in-process caller
    (``python -m pipeline.render.shorts``) continues to work.
    """
    _cli_main_impl()


def _cli_main_impl() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--text", help="Narration text (overrides --script)")
    src.add_argument(
        "--script",
        help="Path to a script.json produced by /make-script (data/intermediate/<channel>/scripts/<slug>.json)",
    )
    ap.add_argument("--channel", default="mystoriesanimated/variants/aita_animated.yaml")
    ap.add_argument("--slug", default=None, help="Override the slug (default: from --script, else 'sample01')")
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "Output root for cache/, shorts/, uploads/, etc. Default "
            "derives from --channel via the per-channel layout: variant "
            "channels resolve via pipeline.niches.NICHE_CHANNEL "
            "(sports_ranked → sportstoriesanimated/ranked); simple "
            "channels with config.yaml at <root>/config.yaml use <root>. "
            "Pass an explicit --out only to override (legacy data/ root, "
            "scratch dir for testing, etc)."
        ),
    )
    ap.add_argument(
        "--no-critic",
        action="store_true",
        help="Skip the post-render critique loop (for fast iteration).",
    )
    ap.add_argument(
        "--require-critic",
        action="store_true",
        help=(
            "Refuse to upload to YouTube if the critic didn't run or didn't "
            "produce a score. Cron-triggered renders pass this so we never "
            "ship an ungraded Short."
        ),
    )
    ap.add_argument(
        "--tts-voice",
        default=None,
        help="Override channel YAML's tts_voice (e.g. 'af_bella', 'bm_george').",
    )
    ap.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Repeatable. Form-style channel override forwarded to "
            "make_short(cfg_overrides=...). Honored keys: audio_mode "
            "(voice|song), song_style, song_vocal_gender (f|m), song_model "
            "(V4_5|V5), visual_source (ai|footage|both), voice, music_bed, "
            "captions_density, visibility, schedule_at. Unknown keys are "
            "passed through to cfg_overrides — translation lives in "
            "_apply_form_overrides; only known keys take effect."
        ),
    )
    upload_grp = ap.add_mutually_exclusive_group()
    upload_grp.add_argument(
        "--upload",
        action="store_true",
        help="Force Stage 8 upload even if channel YAML's upload.auto_upload is false.",
    )
    upload_grp.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Stage 8 upload even if channel YAML's upload.auto_upload is true.",
    )
    args = ap.parse_args()

    # Pre-warm any cloud GPU containers this channel's render will hit.
    # Fire-and-forget on a daemon thread — never blocks the render
    # boot. No-op when no CLOUDRUN_*_URL is configured (laptop-only path).
    try:
        from pipeline.cloud import warm as _cloud_warm  # noqa: PLC0415

        _cloud_warm.warm_async(args.channel)
    except Exception:  # noqa: BLE001
        pass

    if args.script:
        text, script_slug, source_story = _load_script_text(Path(args.script))
        slug = args.slug or script_slug
    else:
        text = args.text or SAMPLE_TEXT
        slug = args.slug or "sample01"
        source_story = None

    upload_override: bool | None = None
    if args.upload:
        upload_override = True
    elif args.no_upload:
        upload_override = False

    # --require-critic implies --no-critic is invalid (refuse to bypass).
    if args.require_critic and args.no_critic:
        ap.error("--require-critic and --no-critic are mutually exclusive")

    # Parse --override KEY=VALUE entries into a flat dict. Bad entries
    # (no `=`) raise via ap.error so the user sees the typo immediately
    # rather than the override silently dropping.
    cfg_overrides: dict | None = None
    if args.override:
        cfg_overrides = {}
        for raw in args.override:
            if "=" not in raw:
                ap.error(f"--override expects KEY=VALUE, got: {raw!r}")
            k, v = raw.split("=", 1)
            cfg_overrides[k.strip()] = v

    channel_path = Path(args.channel)
    if args.out is None:
        out_dir = _resolve_channel_out_dir(channel_path)
        print(f"[out] resolved from --channel: out_dir={out_dir}")
    else:
        out_dir = Path(args.out)

    out_path = make_short(
        text=text,
        channel_path=channel_path,
        out_dir=out_dir,
        slug=slug,
        source_story=source_story,
        run_critic=not args.no_critic,
        require_critic=args.require_critic,
        tts_voice_override=args.tts_voice,
        upload_override=upload_override,
        cfg_overrides=cfg_overrides,
    )

    # Output manifest — consumed by ``workers/heavy/render_short.py``
    # so the cloud worker doesn't have to guess per-channel paths.
    # Pre-2026-05-05 the worker grepped ``data/shorts/<slug>.mp4`` which
    # silently broke when per-channel layout (NICHE_CHANNEL) landed.
    # Print on its own line with a stable prefix so any log-parser /
    # tail can pick it up; downstream consumers ignore lines that
    # don't start with ``OUTPUT_MANIFEST: ``.
    if out_path is not None:
        # Resolve thumb via paths.py (canonical location) with fallbacks.
        from pipeline.paths import RenderPaths as _RP  # noqa: PLC0415

        rp = _RP.from_channel_yaml(channel_path)
        thumb_candidates = [
            rp.short_thumb_for(slug),
            rp.root / "thumbs" / f"{slug}.png",   # older variant
            rp.root / "thumb" / f"{slug}.png",    # older variant
        ]
        thumb_path = next((c for c in thumb_candidates if c.exists()), None)
        manifest = {
            "mp4": str(out_path),
            "thumb": str(thumb_path) if thumb_path else None,
            "slug": slug,
            "channel_dir": rp.channel_dir,
        }
        print(f"OUTPUT_MANIFEST: {json.dumps(manifest)}")


def _resolve_channel_out_dir(channel_path: Path) -> Path:
    """Map channel YAML path → per-channel state root (cache/shorts/uploads/...).

    Routes through :class:`pipeline.paths.RenderPaths` (the canonical
    layout module since 2026-05-05). Returns ``RenderPaths.root``,
    which is ``<channel>/[<niche>/]`` — the per-slug subdir parent.
    Per-channel layout (memory: feedback_channels_subdir_layout) puts
    every channel's runtime state under a per-channel root, NEVER
    under the global ``data/`` dir.

    Lookup precedence (delegated to :meth:`RenderPaths.from_channel_yaml`):

    1. ``pipeline.niches.NICHE_CHANNEL`` — authoritative for variant
       channels (sports_ranked, aita, oddities, tih, …) where YAML and
       state dir don't share a path prefix.
    2. ``<channel_root>/config.yaml`` simple-channel convention.
    3. ``<channel_root>/variants/<variant>.yaml`` fallback — flat layout
       under the channel root, with a loud WARN so the operator adds a
       NICHE_CHANNEL entry.
    """
    from pipeline.paths import RenderPaths  # noqa: PLC0415 — lazy

    try:
        return RenderPaths.from_channel_yaml(channel_path).root
    except ValueError:
        # YAML doesn't fit any known layout — fall back to legacy ``data/``
        # root, but with a loud warning so the regression is visible.
        # The 2026-05-05 rivalry-recap render hit this when nothing was
        # registered for a new slug — the mp4 landed in ``data/shorts/``
        # instead of ``sportstoriesanimated/ranked/shorts/``.
        print(
            f"[out] WARN: could not resolve channel state dir from "
            f"{channel_path!r}; falling back to legacy 'data/' root. Add a "
            f"pipeline.niches.NICHE_CHANNEL entry to fix."
        )
        return Path("data")


if __name__ == "__main__":
    cli_main()


# Backward-compat alias — old in-process callers expecting `main`.
main = cli_main
