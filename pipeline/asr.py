"""Provider-switchable ASR (transcribe + word timestamps).

Two backends ship today:

    * ``whisper_mlx`` — current default. ``mlx-whisper`` with whatever
      model id the channel config / call site passes. Reuses what is
      already cached.

    * ``parakeet_mlx`` — opt-in. ``parakeet-mlx`` package + the MLX
      community Parakeet-TDT 0.6B v2 model. ~10× faster than whisper-
      large-v3-turbo on M-series for English. Single model replaces
      both stage-1 (long-form transcribe) and stage-5 (word timestamps
      on TTS output) calls.

All backends return the **Whisper-shaped result dict** so existing
callers (``pipeline.transcribe``, ``pipeline.beats``) don't have to
care which engine produced the data:

    {
      "text": str,
      "segments": [
        {"text": str, "start": float, "end": float,
         "words": [{"word": str, "start": float, "end": float}, ...]},
        ...
      ]
    }

Heavy imports (``mlx_whisper`` / ``parakeet_mlx``) live inside the
backend functions, so importing this module never triggers a model
download.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


# Map of provider key -> (default model id). The model id may be
# overridden per-call (e.g. by channel config).
PROVIDER_DEFAULTS: dict[str, str] = {
    "whisper_mlx": "mlx-community/whisper-large-v3-mlx-4bit",
    "whisper_mlx_base": "mlx-community/whisper-base-mlx",
    "parakeet_mlx": "mlx-community/parakeet-tdt-0.6b-v2",
}

DEFAULT_PROVIDER = "whisper_mlx"


def transcribe(
    audio_path: Path,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
) -> dict[str, Any]:
    """Run ASR on ``audio_path`` and return a Whisper-shaped result dict.

    ``provider`` selects the backend. ``model`` overrides the default
    model for that provider (handy when the same provider is used for
    both stage-1 long-form and stage-5 short-form passes).

    The result is post-processed to strip Whisper's classic trailing-
    repetition hallucination (e.g. 13× "that" appended after a real
    closing line, when the audio fades into silence). This bubbled up
    from a real audio-critic finding 2026-05; without the strip every
    closer beat had a junk caption tail and the binding-integrity
    ratio looked artificially low.
    """
    if provider in ("whisper_mlx", "whisper_mlx_base"):
        result = _transcribe_whisper(
            audio_path,
            model or PROVIDER_DEFAULTS[provider],
        )
    elif provider == "parakeet_mlx":
        result = _transcribe_parakeet(
            audio_path,
            model or PROVIDER_DEFAULTS["parakeet_mlx"],
        )
    else:
        raise ValueError(
            f"unknown ASR provider {provider!r}. "
            f"choices: {list(PROVIDER_DEFAULTS)}"
        )
    return _strip_trailing_repetition(result)


# ---------- Whisper post-processing ----------------------------------------


def _strip_trailing_repetition(result: dict[str, Any], min_run: int = 4) -> dict[str, Any]:
    """Remove same-token runs of length >= ``min_run`` at the tail.

    Whisper (especially the 4-bit MLX quantization) loops on the same
    token at the end of a clip — "that that that…" / "railway railway…".
    Real failure observed: 221 × "railway" appended to a clean Kokoro
    render. The pattern hits even when the audio's tail is real speech
    (not silence) — it's a quantization quirk, not just a silence
    artifact. Two-layer defense:

    1. SEGMENT-LEVEL: walk segments from the end backwards, find the
       last segment that actually has words, and strip a trailing
       same-token run there. The previous version only looked at
       ``segments[-1]`` and missed cases where Whisper wrote an
       empty trailing segment after the hallucinated one.
    2. TEXT-LEVEL: also strip a trailing same-token run from the
       joined top-level ``text`` string. Catches cases where the
       segment structure is unexpected (e.g. multi-segment runs).

    Idempotent — safe to call on already-cleaned results. Preserves
    legitimate doubles ("no, no!") because the first occurrence in
    the run is always kept.
    """
    def _norm(s: str) -> str:
        return s.strip().lower().strip(".,!?;:'\"")

    def _trim_word_run(words: list[dict]) -> tuple[list[dict], int, str]:
        """Trim a trailing same-token run from a word-list.

        Returns ``(cleaned, run_len, last_token)``. ``run_len`` is the
        number of dropped tokens (0 if no run); a non-zero result
        means the cleanup fired.
        """
        if len(words) < min_run:
            return words, 0, ""
        last_token = _norm(words[-1].get("word") or "")
        if not last_token:
            return words, 0, ""
        run_len = 0
        for w in reversed(words):
            if _norm(w.get("word") or "") == last_token:
                run_len += 1
            else:
                break
        if run_len < min_run:
            return words, 0, last_token
        # Keep the first occurrence ("no, no!" → "no, no" stays;
        # "that × 13" → "that" stays).
        return words[: len(words) - run_len + 1], run_len - 1, last_token

    segments = result.get("segments") or []
    cleaned_anything = False
    cleanup_log: list[str] = []

    # Pass 1: segment-level. Walk backwards to find the last segment
    # with actual words. Earlier code only inspected segments[-1] and
    # missed empty-trailing-segment cases.
    for seg in reversed(segments):
        words = list(seg.get("words") or [])
        if not words:
            continue
        new_words, dropped, tok = _trim_word_run(words)
        if dropped > 0:
            seg["words"] = new_words
            seg["text"] = " ".join((w.get("word") or "").strip() for w in new_words).strip()
            if new_words:
                seg["end"] = float(new_words[-1].get("end") or seg.get("end") or 0.0)
            cleanup_log.append(f"segment {tok!r} ×{dropped + 1}")
            cleaned_anything = True
        # Whether we cleaned or not, this is the last segment with
        # content — no need to look further back.
        break

    # Pass 2: text-level. Operates on the joined top-level string in
    # case the hallucination spans multiple segments or the segment
    # structure was odd. Rebuild from segments first if we just
    # cleaned a segment, otherwise use whatever was there.
    if cleaned_anything:
        result["text"] = " ".join(s.get("text", "").strip() for s in segments).strip()
    text = result.get("text") or ""
    import re as _re
    tokens = _re.findall(r"\S+", text)
    if len(tokens) >= min_run:
        last_token = _norm(tokens[-1])
        if last_token:
            run_len = 0
            for t in reversed(tokens):
                if _norm(t) == last_token:
                    run_len += 1
                else:
                    break
            if run_len >= min_run:
                # Drop all but the first occurrence in the run.
                kept = tokens[: len(tokens) - run_len + 1]
                result["text"] = " ".join(kept)
                cleanup_log.append(f"text-level {last_token!r} ×{run_len - 1}")
                cleaned_anything = True

    if cleaned_anything:
        print(f"[asr] stripped trailing-repetition hallucination: {'; '.join(cleanup_log)}")
    return result


# ---------- whisper_mlx ----------------------------------------------------


def _transcribe_whisper(audio_path: Path, model: str) -> dict[str, Any]:
    import mlx_whisper  # heavy: pulls torch/mlx — kept lazy

    return mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        word_timestamps=True,
    )


# ---------- parakeet_mlx ---------------------------------------------------


def _transcribe_parakeet(audio_path: Path, model: str) -> dict[str, Any]:
    """Transcribe with parakeet-mlx and adapt the result to Whisper shape.

    Requires the optional ``parakeet-mlx`` package:

        .venv/bin/pip install parakeet-mlx

    On first call, downloads ``mlx-community/parakeet-tdt-0.6b-v2``
    (~600 MB) into the HuggingFace cache.
    """
    try:
        from parakeet_mlx import from_pretrained  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "ASR provider 'parakeet_mlx' requires the parakeet-mlx package.\n"
            "  install: .venv/bin/pip install parakeet-mlx\n"
            f"  underlying error: {e}"
        ) from e

    pk = from_pretrained(model)
    result = pk.transcribe(str(audio_path))

    # parakeet-mlx 0.5.x returns AlignedResult{text, sentences:[AlignedSentence{
    # text, tokens:[AlignedToken{text, start, end, ...}], start, end}]}.
    # Tokens are SentencePiece subwords whose text uses **leading space** to
    # mark a word-start (e.g. " cleaned", "le", "an", " the"). We merge each
    # word's subword run into a single Whisper-shaped "word" record so
    # downstream beat-splitting and source alignment see real words.
    segments: list[dict] = []
    sentences = getattr(result, "sentences", None) or []
    for sent in sentences:
        token_list = getattr(sent, "tokens", None) or getattr(sent, "words", []) or []
        words: list[dict] = []
        cur: dict | None = None
        for tok in token_list:
            text = getattr(tok, "text", "") or ""
            start = float(getattr(tok, "start", 0.0))
            end = float(getattr(tok, "end", start))
            is_word_start = text.startswith(" ") or text.startswith("▁") or cur is None
            stripped = text.lstrip(" ▁")
            if is_word_start:
                if cur is not None:
                    words.append(cur)
                cur = {"word": stripped, "start": start, "end": end}
            else:
                # Continuation subword — append to the in-progress word and
                # extend its end timestamp.
                cur["word"] = (cur.get("word") or "") + stripped
                cur["end"] = end
        if cur is not None:
            words.append(cur)

        seg_text = (getattr(sent, "text", "") or "").strip() or " ".join(
            w["word"] for w in words
        )
        seg_start = float(getattr(sent, "start", words[0]["start"] if words else 0.0))
        seg_end = float(getattr(sent, "end", words[-1]["end"] if words else 0.0))
        segments.append(
            {"text": seg_text, "start": seg_start, "end": seg_end, "words": words}
        )

    return {
        "text": (getattr(result, "text", "") or "").strip()
        or " ".join(s["text"] for s in segments),
        "segments": segments,
    }
