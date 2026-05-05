"""Stage 7.6 — auto-critique a synthesised narration.wav.

Companion to ``pipeline/critic.py`` (which critiques the finished video
through a vision lens). This critic reviews the AUDIO and the SCRIPT
that produced it. It:

  1. Whisper-transcribes the audio (proxy for what a listener hears).
  2. Computes objective metrics — WPM, silence gaps, pause locations.
  3. Diffs the source narration against the Whisper transcript so
     letter-spelled tokens ("O U T" instead of "out"), digit-spelled
     numbers ("two zero zero zero" instead of "$2000"), and out-and-
     out mispronunciations show up explicitly in the prompt.
  4. Sends all of the above to ``claude`` (opus by default) acting as
     a professional audio + narration expert.
  5. Returns a structured JSON critique of script and pronunciation
     issues — one-off vs class-of-bug, with concrete fixes.

The class-of-bug findings are the main long-term value: each rendered
Short feeds back rules into the rewrite prompt, the audio normaliser,
or the per-paragraph pause stitching, so the pipeline gets smarter
each render rather than the operator hand-correcting every artefact.

Usage:

    .venv/bin/python -m pipeline.audio_critic \\
        path/to/narration.wav path/to/script.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from pathlib import Path

from .. import asr
from . import cli as llm


# Schema constrains free-form prose so the model can't ramble; mirrors
# the bounded shape used by the video critic.
_CRITIC_SCHEMA = {
    "type": "object",
    "required": [
        "score", "one_line_take", "top_issues",
        "per_finding", "system_corrections", "highest_leverage_change",
    ],
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "one_line_take": {"type": "string", "maxLength": 400},
        "top_issues": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 400},
        },
        "per_finding": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": [
                    "category", "what_is_wrong",
                    "classification", "fix",
                ],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "pronunciation", "pacing", "pauses",
                            "script", "emotion", "closer",
                        ],
                    },
                    "timestamp_s": {"type": "string", "maxLength": 32},
                    "what_was_said": {"type": "string", "maxLength": 200},
                    "what_was_meant": {"type": "string", "maxLength": 200},
                    "what_is_wrong": {"type": "string", "maxLength": 600},
                    "classification": {
                        "type": "string",
                        "enum": ["one-off", "class-of-bug"],
                    },
                    "fix": {"type": "string", "maxLength": 500},
                },
            },
        },
        "script_corrections": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["before", "after"],
                "properties": {
                    "before": {"type": "string", "maxLength": 300},
                    "after": {"type": "string", "maxLength": 300},
                    "reason": {"type": "string", "maxLength": 200},
                },
            },
        },
        "system_corrections": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["issue_class", "where", "fix"],
                "properties": {
                    "issue_class": {"type": "string", "maxLength": 80},
                    "where": {"type": "string", "maxLength": 200},
                    "fix": {"type": "string", "maxLength": 600},
                    "principle": {"type": "string", "maxLength": 200},
                },
            },
        },
        "highest_leverage_change": {"type": "string", "maxLength": 500},
    },
}


_CRITIC_PROMPT = """\
You are a professional audio narration expert AND a pipeline engineer.
A short-form storytelling video just rendered. Your job: review the
narration audio and the script that produced it, find every issue
that hurts comprehension, engagement, or fluency, and propose fixes.

You will be given:
- SOURCE_SCRIPT: the exact text that was fed to the TTS (after pre-
  normalisation: numbers spelled out, all-caps lowercased except
  whitelisted acronyms).
- TRANSCRIPT: what Whisper heard from the resulting audio. This is
  the proxy for what a real listener experiences — Whisper hears
  the same phonemes the listener does.
- METRICS: total duration, words-per-minute, silence-gap timing.
- DIFF: a word-level alignment of SOURCE_SCRIPT vs TRANSCRIPT, with
  insertions and deletions called out. Mismatches reveal mispronun-
  ciations (acronyms read letter-by-letter, numbers read digit-by-
  digit, words mis-said).

EVALUATION LENSES — examine each. Most will be silent on most stories;
that's fine. Speak up only when a lens flags something concrete.

LENSES GROW OVER TIME — every system_correction emitted by this critic
in a previous render that ships as a code fix MUST be added back into
this lens list so the next render is checked against it. The list is
the regression-proof memory of the pipeline. When you add a finding,
classify it and propose the lens text that should go HERE for next
time, in the system_corrections.principle field.

  L1 PRONUNCIATION — words mis-rendered. Letter-spelled acronyms
     where the word should be normal (e.g. "OUT" → "O U T"). Digit-
     spelled numbers ("two zero zero zero" instead of "two thousand").
     Mispronounced names. Cite both source AND transcript form.

  L2 PACING — WPM in storytelling range (130-180 is the audiobook
     zone; 200+ is rushed; under 110 is sedated). Tempo variation:
     does the narrator slow down on the kicker, speed up on the
     setup? Or is it monotone tempo throughout?

  L3 PAUSES — does the audio breathe? Each paragraph break in the
     source script SHOULD produce a silence gap >0.5s in the audio.
     Long stretches (>5s) without any silence ≥0.3s are run-ons.
     Pauses in the wrong place (mid-clause) are also flaggable.

  L4 SCRIPT — punctuation quality. Run-on sentences. Missing periods.
     Filler words ("just", "really", "kind of"). Ambiguous pronouns
     (the "she" referent unclear). Sentences that don't stand alone
     as a subtitle.

  L5 EMOTION — the source script has dramatic beats: the pivot, the
     kicker, the closer. Does the audio land them, or are they
     delivered flat? (Hard to judge from transcript alone but flag
     when a moment that should hit reads as throwaway.)

  L6 CLOSER — does the closer land cleanly? Is there a real silence
     (>=0.5s) before it to set it up? The narration SHOULD end with
     a verbal CTA, but it must sound like a real human storyteller
     — natural conversational language ("Am I the asshole? Tell me
     in the comments." / "What would you have done? Drop your take
     below.") — NOT the robotic YouTube-preset format ("LIKE if YTA,
     COMMENT if NTA"). If the audio contains literal "LIKE if",
     "COMMENT if", "smash that like", or "don't forget to subscribe"
     phrasing, flag as a class-of-bug regression on the rewrite
     prompt's _closer_block. The acronyms YTA/NTA in the spoken
     closer should be expanded ("you're the asshole" / "not the
     asshole") per L7, not letter-spelled or phoneticised.

  L7 ACRONYM EXPANSION — AITA-class acronyms (AITA, WIBTA, YTA, NTA,
     ESH, NAH, MIL, FIL, SIL, BIL, DIL, OOP) MUST be expanded to
     their natural English phrases in the audio: "AITA" → "am I the
     asshole", "MIL" → "mother in law", "YTA" → "you're the asshole".
     If the transcript shows letter-spelled (W I B T A) OR phoneticised-
     as-word (transcribed as "Aira"/"Zira"/"Antigua"), that's a
     class-of-bug — the normalize_for_tts mapping is wrong or missing.
     Cite the specific acronym + how it was rendered.

  L8 EUPHEMISM HANDLING — single-letter euphemisms ("f off", "b**ch",
     standalone "F"/"B"/"D" tokens) read as letter names by Kokoro
     ("eff", "bee") or get swallowed entirely. Source should use
     written-out forms ("eff off", "freaking", "damn") OR be handled
     by normalize_for_tts. Flag if you see standalone single-letter
     euphemism tokens in the source.

  L9 ALL-CAPS LEAKAGE — non-acronym ALL-CAPS tokens in the source
     ("WHOLE", "HE", "AGAIN") get correctly lowercased by
     normalize_for_tts. But verify by checking the transcript: if
     these source tokens are still letter-spelled in transcript, the
     normalizer regression-tested. Flag the source tokens explicitly.

  L10 BINDING INTEGRITY — if the word-overlap ratio between source
     and transcript is <0.5, the wrong audio is bound to this script.
     This is a hard failure: do not rate any other lens on a render
     where binding is broken; declare binding failure as the top issue.

  L11 TRAILING-WHISPER-REPETITION — Whisper hallucinates same-token
     runs ("that that that that…", "the the the the…") on the
     trailing silence of a WAV. Look for ≥4 identical adjacent tokens
     at the END of the transcript that are NOT in the source. If
     found, that's a CLASS-OF-BUG regression on the asr-side
     post-processing in `pipeline/asr.py:_strip_trailing_repetition`
     (which should kill ≥4-token tail runs) AND on the audio-side
     trailing-silence trim in `pipeline/audio.py:_trim_trailing_silence`
     (which should leave nothing for Whisper to hallucinate over).
     Both should be active; if you still see the run in the
     transcript, ONE of them is broken or insufficiently aggressive.

  L12 QUOTED DIALOGUE FRAMING — TTS does not voice-act. Quoted lines
     in the source ("Don't feel good. Gotta bail." / "My back hurts.")
     sound identical to narration in the audio. Each quote MUST be
     framed with a speaker tag and a leading comma so prosody beats
     before the line ("She texted, \"My back hurts.\""). Bare quotes
     with no speaker frame are a CLASS-OF-BUG on
     `pipeline/rewrite.py:_BASE_PROMPT` — flag the specific quoted
     line and propose either a speaker-tag rewrite or a paraphrase.
     Quotes should also be SHORT (≤10 words); if a long quoted block
     appears, suggest paraphrasing it as narration instead.

  L13 STACCATO-LIST FRAGMENTS — short one/two-word sentences are
     ONLY a "dramatic punch" when separated by longer sentences.
     Three or more consecutive short fragments ("Wine. Crafts. Just
     us.") read as a robot enumerating a list, not as drama —
     because Kokoro/F5-TTS chop each period sharply with no rising
     tension between them. Detection: any run of 3+ consecutive
     sentences each ≤2 words. Fix: route list-style enumeration
     through commas inside a single sentence ("Wine, crafts, just
     us.") — that gives the TTS one breath unit and reads as a
     natural beat. Two consecutive one-word sentences also gets a
     warning if neither is the pivot or the kicker. Class-of-bug on
     `pipeline/rewrite.py:_BASE_PROMPT` PROSODY block — the rule
     should explicitly forbid back-to-back fragments and require a
     longer sentence between any two short punches.

  L14 COMPOSE CACHE INTEGRITY — only callable when the rendered mp4
     is also on disk. Verify (a) the number of cached caption_*.png
     OR word_*.png files inside the slug's cache dir matches the
     CURRENT compose contract (caption_NN.png count == len(beats),
     OR word_NNNN.png count == sum(len(beat.words) for beat in
     beats)), and (b) any img_NN.png whose index ≥ len(beats) is a
     stale leftover that was supposed to have been wiped at compose
     start. The off-by-one between cached artefacts and beats was
     the highest-impact sync bug in the pipeline (multi-second
     caption drift on every re-rendered Short). Class-of-bug on
     `pipeline/compose.py:_wipe_stale_per_beat_artefacts` if either
     check fails — the wipe didn't run or didn't cover the file
     pattern. (Principle #40 in DESIGN.md.)

  L15 FLAT-TEMPO REGRESSION — Kokoro's flat-prosody output is
     compensated by PER-SENTENCE speed modulation in
     ``pipeline/audio.py:_modulate_sentence_speed`` (hook ~8%
     slower, closer ~12% slower, "!" sentences ~10% faster,
     "..." sentences ~18% slower, punch fragments ≤3 words after
     ≥6-word setup ~18% slower). If consecutive sentences land at
     identical WPM (within ±2%), modulation didn't fire. Symptom:
     "every line reads at the same tempo, sounds monotonous".
     Detection: compute WPM per sentence using the Whisper word
     timestamps; if spread between min and max sentence-WPM is
     <8%, that's a class-of-bug regression — modulation disabled
     in channel cfg, the heuristic stopped picking up the
     patterns, or the synth path bypassed the multi-sentence
     branch. Cite the WPM per sentence and the file:function
     target.

  L16 HINDI TATSAMA RESPELLING — Kokoro's Hindi voices (h*) collapse
     Devanagari conjuncts (द्ध, क्ष, ज्ञ, र्भ) and re-segment proper
     nouns at wrong syllable boundaries. Symptoms: "अभिमन्यु" → "अब
     ही मन्यू", "कुरुक्षेत्र" → "कुरुक शेत्र", "ज्ञान" → "ग्यान",
     "योद्धा" → "योधा" (gemination lost). Detection: any source
     Devanagari token containing a tatsama-prone conjunct (see list
     above) where the Whisper transcript shows the token split,
     re-segmented, or with the conjunct simplified. Class-of-bug:
     ``pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS`` is missing the
     token. Add it (canonical → phonetic). Captions/source-text
     keep canonical Devanagari; only the TTS path sees the
     respelling. Conjuncts to watch: द्ध, क्ष, ज्ञ, र्भ, र्ज, ण्य, म्न्य.

  L17 HINDI NUMERAL HOMOPHONE — Hindi numerals collide acoustically
     with common postpositions/verbs: सात (seven) ↔ साथ (with),
     दो (two) ↔ दो (give imperative), नौ (nine) ↔ नौ (boat). And
     "सोलह" reads as "soleh" (extra schwa) on hm_psi. Detection:
     compare source token against transcript token at the same
     position; if a numeral lemma was heard as its homophone (or
     a fuzzy variant), CLASS-OF-BUG on
     ``pipeline/audio.py:_HINDI_NUMERAL_RESPELLINGS``. Cite the
     specific numeral. Fix: respell with explicit vowels, or use
     the ASCII digit form which Kokoro phonologises cleanly.

  L18 HINDI CLOSER = THREE PARAGRAPHS — for devotional/storytelling
     Hindi channels the closer must produce three discrete units:
     (1) lesson/moral, (2) blessing ("जय श्री कृष्णा।" or similar),
     (3) CTA ("कमेंट करें और चैनल को सब्सक्राइब करें।"). Each on
     its own line with a blank line between, so the per-paragraph
     stitcher inserts the >0.5s gap that L3 requires. Em-dash-joined
     closers fuse on hm_psi and read as one breathless run-on.
     Class-of-bug on ``pipeline/rewrite.py:_BASE_PROMPT`` (Hindi
     closer block) if a Hindi script lands a single-paragraph
     blessing+CTA fusion.

  L19 DANDA AS SENTENCE TERMINATOR — Hindi sentences end in danda
     "।" (U+0964) and deergh viram "॥" (U+0965), NOT ASCII period.
     The sentence splitter at ``pipeline/audio.py:_SENTENCE_SPLIT_RE``
     and the modulator's hook/closer detection MUST recognise these
     or every Hindi narration synthesises as one giant sentence,
     bypassing modulation AND blowing past Kokoro's 510-phoneme
     cap. Detection: if a Hindi source has multiple "।" but the
     synth log shows only one sentence, the regex regressed.

  L20 OVER-PAUSED PROSODY — explicit silence inserted between
     sentences (per-beat ``post_pause_s`` in script.json's
     ``narration_prosody``) totals more than ~5% of audio
     duration → reads as "weird" / "too many pauses" even when
     each individual pause is justified. Tightened from the
     original 8% threshold per user feedback 2026-05-03 v2:
     pauses concentrated at paragraph boundaries (typically 4-6
     transitions in a 60s story) sound natural; pauses scattered
     after every sentence sound choppy. Cap individual non-paragraph
     ``post_pause_s`` at ≤0.15s; reserve ≥0.4s pauses for paragraph
     boundaries (where the source has ``\\n\\n``). Total-pause
     budget ≤5% of synthesised duration. Gravitas comes from
     PER-SENTENCE speed drops on weight words (0.70-0.82x), not
     from dead air.

  L21 DEVANAGARI TOKENISER REGRESSION — a critic-internal lens.
     If word_count_source AND word_count_heard are both 0 in the
     metrics block, the tokeniser dropped every word — almost
     certainly because the regex was ASCII-only. Class-of-bug on
     ``pipeline/audio_critic.py:_tokenise``: must use
     ``[\\w']+`` with ``re.UNICODE`` so Devanagari survives. When
     this lens fires, NO downstream WPM-based lens can produce
     reliable signal — the rest of the report is degraded. Always
     cite that this happened so the user knows to disregard
     pacing-related findings until the tokeniser is fixed.

ENGINEERING THINKING — for EVERY issue:
  • ONE-OFF — unique to this script. Fix lives in `script_corrections`.
  • CLASS-OF-BUG — the pipeline could let this through on any future
    Short. Fix lives in `system_corrections` with file:function
    targets. Reference: `pipeline/audio.py:normalize_for_tts`,
    `pipeline/rewrite.py:_BASE_PROMPT`, the per-paragraph stitcher,
    DESIGN.md §14 principles.

Cite specific source/transcript text on every finding.

Return ONLY a JSON object with the schema you have been given.
=================================================================

SOURCE_SCRIPT:
\"\"\"
{source_script}
\"\"\"

TRANSCRIPT (Whisper, what the listener hears):
\"\"\"
{transcript}
\"\"\"

METRICS:
  total_duration_s:    {duration_s:.2f}
  word_count_source:   {n_source_words}
  word_count_heard:    {n_heard_words}
  wpm:                 {wpm:.0f}     (target 130-180 for storytelling)
  silence_gaps_>=0.3s: {n_short_gaps}
  silence_gaps_>=0.5s: {n_long_gaps}  (paragraph-break-class)
  longest_run_no_breath_s: {longest_run_s:.2f}
  paragraph_breaks_in_source: {n_source_paragraphs}

DIFF (word-level, ▲=in-source-only, ▼=in-transcript-only, =equal):
{diff_text}
"""


def _silence_gaps(audio_path: Path, threshold_db: float = -40.0,
                  min_duration_s: float = 0.3) -> list[tuple[float, float, float]]:
    """Return [(start, end, duration), …] for silence runs.

    Uses ffmpeg's silencedetect filter — same approach as the operator's
    manual probing. Threshold and min duration tuned to catch both
    paragraph breaks (≥0.5s) and the smaller punctuation pauses (≥0.3s).
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(audio_path),
        "-af", f"silencedetect=n={threshold_db}dB:d={min_duration_s}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out: list[tuple[float, float, float]] = []
    cur_start: float | None = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: (-?\d+(?:\.\d+)?)", line)
        if m:
            cur_start = float(m.group(1))
            continue
        m = re.search(r"silence_end: (-?\d+(?:\.\d+)?) \| silence_duration: (-?\d+(?:\.\d+)?)", line)
        if m and cur_start is not None:
            end = float(m.group(1))
            dur = float(m.group(2))
            out.append((cur_start, end, dur))
            cur_start = None
    return out


def _audio_duration(audio_path: Path) -> float:
    # Use the memoized helper. probe_duration_or_none returns None on
    # failure; legacy call site fell back to 0.0, preserve that.
    from pipeline.probe import probe_duration_or_none  # noqa: PLC0415
    return probe_duration_or_none(audio_path) or 0.0


def _diff_lines(source_words: list[str], heard_words: list[str]) -> str:
    """Pretty word-level diff for the prompt.

    Each non-equal run is printed on its own line so the LLM can see
    where SOURCE and TRANSCRIPT disagree without us hand-rolling
    pattern detectors. Equal runs are collapsed to a count to keep
    the prompt short.
    """
    sm = difflib.SequenceMatcher(a=source_words, b=heard_words, autojunk=False)
    lines: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_chunk = " ".join(source_words[i1:i2])
        b_chunk = " ".join(heard_words[j1:j2])
        if tag == "equal":
            lines.append(f"=  ({i2 - i1} matched words) {a_chunk[:80]}")
        elif tag == "replace":
            lines.append(f"▲  source:     {a_chunk}")
            lines.append(f"▼  transcript: {b_chunk}")
        elif tag == "delete":
            lines.append(f"▲  source-only: {a_chunk}")
        elif tag == "insert":
            lines.append(f"▼  transcript-only: {b_chunk}")
    return "\n".join(lines) if lines else "(perfect match — no differences)"


def _tokenise(text: str) -> list[str]:
    """Word-tokens — Latin lowercased, Devanagari preserved.

    The original regex was ``[a-z0-9']+`` which silently dropped every
    Devanagari character, leaving Hindi renders with word_count=0 and
    therefore wpm=0 — every WPM-based lens (L2, L15) was bypassed.
    Class-of-bug fix per critique 2026-05-03 v2.

    \\w with re.UNICODE matches Latin + Devanagari + apostrophe-bearing
    contractions; .lower() is a no-op on Devanagari and still
    case-folds the English path.
    """
    return [t.lower() for t in re.findall(r"[\w']+", text, flags=re.UNICODE)]


def critique_audio(
    *,
    audio_path: Path,
    source_script: str,
    out_path: Path | None = None,
    asr_provider: str = "whisper_mlx",
) -> dict:
    """Critique a narration WAV against its source script.

    Returns the parsed critique dict. When ``out_path`` is provided,
    also writes the critique JSON to that path (same shape used by the
    video critic for downstream tooling).
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"audio missing: {audio_path}")
    print(f"[audio-critic] transcribing {audio_path.name} via {asr_provider}…")
    asr_result = asr.transcribe(audio_path, provider=asr_provider)
    transcript = (asr_result.get("text") or "").strip()
    if not transcript:
        raise RuntimeError("ASR returned empty transcript")

    print(f"[audio-critic] computing metrics…")
    duration = _audio_duration(audio_path)
    short_gaps = _silence_gaps(audio_path, min_duration_s=0.3)
    long_gaps = [g for g in short_gaps if g[2] >= 0.5]
    # Longest run between silences — helps spot run-on stretches.
    boundaries = [0.0] + [g[1] for g in short_gaps] + [duration]
    starts = [0.0] + [g[0] for g in short_gaps]
    longest_run = max(
        (b - s for s, b in zip(boundaries[:-1], starts[1:] + [duration])
         if (b - s) > 0),
        default=duration,
    )
    src_tokens = _tokenise(source_script)
    heard_tokens = _tokenise(transcript)
    n_source_paragraphs = len(re.split(r"\n\s*\n", source_script.strip())) if source_script.strip() else 0
    wpm = len(heard_tokens) / (duration / 60.0) if duration else 0.0
    diff_text = _diff_lines(src_tokens, heard_tokens)

    prompt = _CRITIC_PROMPT.format(
        source_script=source_script.strip(),
        transcript=transcript,
        duration_s=duration,
        n_source_words=len(src_tokens),
        n_heard_words=len(heard_tokens),
        wpm=wpm,
        n_short_gaps=len(short_gaps),
        n_long_gaps=len(long_gaps),
        longest_run_s=longest_run,
        n_source_paragraphs=n_source_paragraphs,
        diff_text=diff_text,
    )

    critic_model = llm.model_for("audio_critic")
    print(f"[audio-critic] sending to claude CLI ({critic_model})…")
    raw = llm.call_claude_cli(
        prompt,
        output_json=True,
        model=critic_model,
        json_schema=_CRITIC_SCHEMA,
        timeout_s=600,
        budget_usd=2.0,
    )
    if not isinstance(raw, dict):
        raise ValueError(f"audio-critic returned {type(raw).__name__}, not object")

    print(f"[audio-critic] score={raw.get('score')!r} — "
          f"{(raw.get('one_line_take') or '(no take)')[:120]}")
    sys_corr = raw.get("system_corrections") or []
    if sys_corr:
        print("[audio-critic] === CLASS-OF-BUG fixes ===")
        for c in sys_corr:
            print(f"  • [{c.get('issue_class','?')}] {c.get('where','?')}")
            print(f"    fix: {c.get('fix','?')}")
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(raw, indent=2))
        print(f"[audio-critic] wrote {out_path}")
    return raw


def _load_script_text(script_path: Path) -> str:
    """Pull narration out of either a /make-script JSON or a raw .txt."""
    if script_path.suffix == ".json":
        d = json.loads(script_path.read_text())
        return d.get("narration") or d.get("text") or ""
    return script_path.read_text()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Critique a narration WAV against its source script."
    )
    ap.add_argument("audio_path", type=Path, help="path/to/narration.wav")
    ap.add_argument("script_path", type=Path,
                    help="path/to/script.json (use 'narration' field) or .txt")
    ap.add_argument("--out", type=Path, default=None,
                    help="where to write the critique JSON (default: alongside audio as <name>.audio.score.json)")
    ap.add_argument("--asr-provider", default="whisper_mlx")
    args = ap.parse_args()

    source_script = _load_script_text(args.script_path)
    if not source_script.strip():
        raise SystemExit(f"empty script: {args.script_path}")
    out_path = args.out or args.audio_path.with_suffix(".audio.score.json")
    critique_audio(
        audio_path=args.audio_path,
        source_script=source_script,
        out_path=out_path,
        asr_provider=args.asr_provider,
    )


if __name__ == "__main__":
    main()
