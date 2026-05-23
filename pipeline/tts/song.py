"""Song generation + leading-silence trimming for sung music tracks.

Used by rhymetimejunction (and any future channel that opts into sung
audio via ``audio_provider: sunoapi`` or ``external_song`` in its
config.yaml). NOT used for narration — narration goes through one of
the TTS providers (kokoro, chatterbox, styletts2).

Two helpers:

* :func:`synth_via_sunoapi` — generate a sung song via the unofficial
  sunoapi.org Suno wrapper. Pay-per-generation, ~$0.05-0.10/song.
* :func:`trim_song_for_short` — trim a sung WAV for the Shorts duration
  cap (handles intro skip, vocal-pickup drop, fade-out).

Plus :func:`_detect_leading_silence_s` — internal probe used by
``trim_song_for_short`` when ``drop_leading_silence=True``.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from pipeline import observability as _obs


def _detect_leading_silence_s(
    in_path: Path, threshold_db: float = -28.0, min_silence_s: float = 0.3
) -> float:
    """Return the duration of low-RMS leading silence at the start of a WAV.

    Suno songs frequently open with a 1-5s quiet guitar intro that
    Whisper hallucinates as plausible-but-wrong sung words at the wrong
    timestamps. Trimming this before passing to ASR collapses the
    caption-desync class of bug at the source rather than masking it
    after the fact. Threshold tuned to -28 dB based on the
    abhimanyu / hathi-raja sample (vocals at -14 to -20 dB, guitar
    intros around -34 to -38 dB).
    """
    import re as _re
    import subprocess as _sp

    cmd = [
        "ffmpeg", "-i", str(in_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_s}",
        "-f", "null", "-",
    ]
    res = _sp.run(cmd, capture_output=True, text=True)
    leading = 0.0
    for line in res.stderr.splitlines():
        m = _re.search(r"silence_start:\s*(\d+\.\d+)", line)
        if m and float(m.group(1)) < 0.05:
            # Find the matching silence_end on the next silence_end line
            # in subsequent output.
            continue
        m_end = _re.search(r"silence_end:\s*(\d+\.\d+).*silence_duration:\s*(\d+\.\d+)", line)
        if m_end:
            end_s = float(m_end.group(1))
            dur_s = float(m_end.group(2))
            start_s = end_s - dur_s
            if start_s < 0.05:
                leading = end_s
                break
    return leading


def trim_song_for_short(
    in_path: Path,
    out_path: Path,
    max_duration_s: float,
    *,
    trim_start_s: float = 0.0,
    drop_leading_silence: bool = False,
    leading_silence_threshold_db: float = -28.0,
    drop_vocal_pickup: bool = False,
    vocal_pickup_threshold_db: float = -22.0,
    fade_out_s: float = 0.0,
) -> tuple[float, float]:
    """Trim a song WAV for the Shorts duration cap.

    Pipeline:
      1. Skip the leading instrumental intro. Either via an explicit
         ``trim_start_s`` (recommended for Suno — the intro length is
         consistent at ~2-3s and ffmpeg's silencedetect uses PEAK not
         RMS so quiet-guitar intros are missed by auto-detection), OR
         via ``drop_leading_silence=True`` which probes silencedetect
         (works on truly silent intros only).
      2. (NEW 2026-05-03 v4 critique) If ``drop_vocal_pickup=True``,
         apply ffmpeg ``silenceremove`` on the trimmed audio to drop
         any residual quiet pickup beat between the rough intro cut
         and the actual vocal entry. Suno V4_5 vocals don't always
         land at exactly the requested start offset; this catches
         the 0.3-1.0s "breath / pickup" that left captions ahead of
         the audio.
      3. Cap to ``max_duration_s``.
      4. (NEW 2026-05-03 v4 critique) If ``fade_out_s > 0``, apply
         ``afade=t=out`` over the final N seconds. Eliminates the
         abrupt mid-chorus cut at the duration cap so the closer
         hold transitions gracefully from sung→quiet→still-image.

    Returns ``(trim_start_s, trim_end_s)`` on the SOURCE timeline so the
    caller can log what was cut.

    Caveat: if ``drop_vocal_pickup`` ate ~0.3-0.5s, the actual output
    is slightly less than ``max_duration_s`` and the fadeout's
    effective duration shrinks proportionally. Both effects are still
    net wins versus no-fadeout / no-pickup-drop.
    """
    import subprocess as _sp

    explicit_start = float(trim_start_s or 0.0)
    if drop_leading_silence and explicit_start <= 0.0:
        explicit_start = _detect_leading_silence_s(
            in_path, threshold_db=leading_silence_threshold_db
        )

    trim_end_s = explicit_start + max_duration_s

    af_parts: list[str] = []
    if drop_vocal_pickup:
        # Drop leading audio quieter than the vocal threshold until the
        # first sample louder. start_silence=0.05 tolerates a 50ms
        # low-level lead-in; start_duration=0.1 means after we detect
        # voice we keep 100ms before re-checking. Tuned for Suno's
        # vocal pickup transient.
        af_parts.append(
            f"silenceremove=start_periods=1:"
            f"start_threshold={vocal_pickup_threshold_db}dB:"
            f"start_silence=0.05:start_duration=0.1"
        )
    if fade_out_s > 0:
        # afade.st is in OUTPUT timeline AFTER any silenceremove. We
        # use max_duration_s as the assumed output duration; if
        # silenceremove cut some leading audio, output is shorter and
        # the fade truncates accordingly (still a fade, just shorter).
        af_parts.append(
            f"afade=t=out:st={max(0.0, max_duration_s - fade_out_s):.3f}:"
            f"d={fade_out_s:.3f}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{explicit_start:.3f}",
        "-i", str(in_path),
        "-t", f"{max_duration_s:.3f}",
    ]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    cmd += [
        "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
        str(out_path),
    ]
    _sp.run(cmd, check=True, capture_output=True)
    return (explicit_start, trim_end_s)


# ---------- sunoapi (Suno via sunoapi.org wrapper, sung music) -------------
#
# Suno is the de-facto best AI singing model for bilingual children's
# rhymes (Hinglish, kids' choir, melody adherence). There is no
# official Suno HTTP API for individual devs — the public path is
# the unofficial wrapper at https://sunoapi.org/.
#
# Pricing: pay-per-generation, ~$0.05-0.10/song on the V4_5 model.
# Top up credits at https://sunoapi.org/topup.
#
# API:
#   POST https://api.sunoapi.org/api/v1/generate
#     Authorization: Bearer <SUNOAPI_API_KEY>
#     body: {customMode, instrumental, callBackUrl, model, prompt, style, title, vocalGender}
#     returns: {data: {taskId}}
#   GET https://api.sunoapi.org/api/v1/generate/record-info?taskId=<id>
#     returns: {data: {status, response: {sunoData: [{audioUrl, ...}]}}}
#     poll until status == "SUCCESS" or "FAILED"
#
# Risk: sunoapi.org is an unofficial Suno wrapper; Suno periodically
# DMCAs these. Has weathered ~3 cycles since 2024, currently up. If
# it goes down, fall back to manual Suno (audio_provider:
# external_song with the songs/<slug>.wav drop convention).

_SUNOAPI_BASE = "https://api.sunoapi.org"
_SUNOAPI_GENERATE = f"{_SUNOAPI_BASE}/api/v1/generate"
_SUNOAPI_RECORD = f"{_SUNOAPI_BASE}/api/v1/generate/record-info"


def synth_via_sunoapi(
    lyrics: str,
    style: str,
    out_path: Path,
    *,
    title: str = "",
    model: str = "V4_5",
    vocal_gender: str = "f",
    poll_timeout_s: int = 300,
    poll_interval_s: int = 5,
) -> Path:
    """Generate a sung song via sunoapi.org's Suno wrapper.

    ``lyrics`` is the full lyrics block (one per line, may include
    [Verse 1] / [Chorus] tags — Suno honors those structural markers).
    ``style`` is a one-sentence description of genre + instrumentation
    + tempo (e.g. "cheerful upbeat children's nursery rhyme, female
    lead with kids choir, gentle acoustic guitar + tabla, 120 BPM").

    Returns the path to the downloaded WAV. Raises RuntimeError on
    auth failure, generation failure, or poll timeout.

    Sunoapi.org's required ``callBackUrl`` field is set to a placeholder;
    we poll the record-info endpoint instead of relying on the webhook
    callback (callbacks need a public-internet URL the laptop agent
    doesn't have).
    """
    metadata = {
        "provider": "sunoapi",
        "model": model,
        "vocal_gender": vocal_gender,
        "lyrics_chars": len(lyrics or ""),
        "style_chars": len(style or ""),
        "title": (title or "")[:100],
        "out_path": str(out_path),
        "poll_timeout_s": poll_timeout_s,
    }
    with _obs.timed("song_synth", category="tts", metadata=metadata):
        return _synth_via_sunoapi_impl(
            lyrics, style, out_path,
            title=title, model=model, vocal_gender=vocal_gender,
            poll_timeout_s=poll_timeout_s, poll_interval_s=poll_interval_s,
        )


def _synth_via_sunoapi_impl(
    lyrics: str,
    style: str,
    out_path: Path,
    *,
    title: str = "",
    model: str = "V4_5",
    vocal_gender: str = "f",
    poll_timeout_s: int = 300,
    poll_interval_s: int = 5,
) -> Path:
    import json as _json
    import os as _os
    import time as _time

    api_key = _os.environ.get("SUNOAPI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "audio_provider=sunoapi requires SUNOAPI_API_KEY env var.\n"
            "Sign up: https://sunoapi.org/api-key (~$10 top-up = ~100 songs).\n"
            "Set in shell: export SUNOAPI_API_KEY=...\n"
            "Or add to .env: SUNOAPI_API_KEY=..."
        )

    body = _json.dumps({
        "customMode": True,
        "instrumental": False,
        "callBackUrl": "https://example.com/noop",  # required by schema; we poll instead
        "model": model,
        "prompt": lyrics,
        "style": style,
        "title": title or "Untitled",
        "vocalGender": vocal_gender,
    }).encode("utf-8")
    # Cloudflare on api.sunoapi.org rejects the default urllib UA with
    # error 1010 ("access denied"). Adding a real browser-class UA + an
    # Accept header makes the request look like a normal client and gets
    # past the CF bot challenge. Confirmed 2026-05-03 — without this the
    # generate endpoint returns HTTP 403 immediately.
    _UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }

    print(f"[sunoapi] generate model={model} vocal={vocal_gender} "
          f"lyrics={len(lyrics)}c style={len(style)}c…")
    req = urllib.request.Request(_SUNOAPI_GENERATE, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"sunoapi generate HTTP {e.code}\n{detail}") from None

    task_id = (result.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"sunoapi generate returned no taskId: {result}")
    print(f"[sunoapi] task_id={task_id}; polling for completion…")

    deadline = _time.time() + poll_timeout_s
    while _time.time() < deadline:
        poll_req = urllib.request.Request(
            f"{_SUNOAPI_RECORD}?taskId={task_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=30) as resp:
                data = _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"[sunoapi] poll HTTP {e.code} — retrying")
            _time.sleep(poll_interval_s)
            continue

        outer = data.get("data") or {}
        status = outer.get("status", "?")
        if status == "SUCCESS":
            songs = (outer.get("response") or {}).get("sunoData") or []
            if not songs:
                raise RuntimeError(f"sunoapi SUCCESS but empty sunoData: {data}")
            audio_url = songs[0].get("audioUrl")
            if not audio_url:
                raise RuntimeError(f"sunoapi SUCCESS but no audioUrl in {songs[0]!r}")
            print(f"[sunoapi] downloading {audio_url}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # Cloudflare on the audio CDN (tempfile.aiquickdraw.com)
            # also rejects Python-urllib UA. Use the browser UA we set
            # for the API itself and stream the bytes ourselves rather
            # than urlretrieve which doesn't accept custom headers.
            dl_req = urllib.request.Request(
                audio_url,
                headers={"User-Agent": _UA, "Accept": "*/*"},
            )
            # sunoapi V4_5 returns .mp3, not .wav. Whisper / ffmpeg both
            # handle MP3 transparently downstream, but the pipeline names
            # the cache slot `narration.wav` by convention. We download
            # the MP3 to a sibling .mp3, then ffmpeg-transcode to the
            # caller's out_path so any caller asking for .wav gets a
            # proper WAV (44.1k mono PCM s16le) regardless of what Suno
            # served. Saves callers from having to know the extension.
            mp3_path = out_path.with_suffix(".mp3")
            with urllib.request.urlopen(dl_req, timeout=120) as resp:
                mp3_path.write_bytes(resp.read())
            if out_path.suffix.lower() == ".wav":
                import subprocess as _sp
                _sp.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-i", str(mp3_path),
                     "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
                     str(out_path)],
                    check=True,
                )
                # Keep mp3_path for debugging — small file, helpful when
                # listening to the raw Suno output without the WAV transcode.
            else:
                # Caller asked for .mp3 (or other) — just rename.
                mp3_path.rename(out_path)
            return out_path
        if status in ("FAILED", "ERROR", "FAIL", "CREATE_TASK_FAILED"):
            raise RuntimeError(f"sunoapi generation failed: status={status} body={data}")

        # Still pending — log every ~30s so a long render is observable.
        elapsed = int(poll_timeout_s - (deadline - _time.time()))
        if elapsed % 30 == 0:
            print(f"[sunoapi] status={status} (elapsed {elapsed}s)")
        _time.sleep(poll_interval_s)

    raise RuntimeError(
        f"sunoapi timed out after {poll_timeout_s}s for task {task_id}"
    )
