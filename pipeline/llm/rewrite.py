"""Stage 3 — rewrite a story into a 22-32s hook-first narration.

Autonomous via the `claude` CLI subprocess wrapper (pipeline/llm.py).
The rewriter is channel-aware: when ``channel_cfg["closer_format"]``
is set (e.g. AITA channels), the prompt requires that exact format
in the narration's closing line — script_check will block the render
otherwise.

Length target raised from the original 50-80 words / 10-20s after
operator feedback "still 6 images on website, want longer, spicier
stories". Sentence count is image count (one beat per sentence per
beats.py). The prior 50-80 word ceiling forced 5-8 sentences max
even when the prompt asked for 8-12; bumping to 110-160 words
removes that bottleneck and produces 10-15 beats / images.

Cadence note (2026-05-23): for a 22-32s short, 10-15 beats →
~2-3 s per image. That sits in the empirically-validated sweet
spot for narrated Shorts (research/2026-05 — see
docs/panel_pacing_research_2026-05.md: 7-10 PPM body / 25 PPM hook
for short-form storytime). The cadence is shorter than long-form's
~25 s/panel because Shorts viewers swipe; long-form viewers stay.
Both formats run static stills with hard cuts (no on-clip motion,
no crossfade) on the long-form path; Shorts use a per-image punch-in
+ slow-drift zoom in pipeline/compose.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

from . import cli as llm
from . import script_lint
from pipeline import observability as _obs


@dataclass
class Script:
    slug: str
    hook: str               # the first 1.5s line — strongest curiosity gap
    narration: str          # full 22–32s narration including the hook
    title_options: list[str]
    source_url: str = ""
    source: str = ""
    # Free-form metadata blob (added 2026-05-14 per Phase 4b for
    # era_anchor; future renders may carry other directives here too).
    # Schema is intentionally loose — consumers read individual keys
    # they recognise (e.g. ``metadata.era_anchor``,
    # ``metadata.era_lock``) and ignore the rest. Keeps the dataclass
    # extensible without per-field migrations as new visual anchors
    # land.
    metadata: dict | None = None


# ---------------------------------------------------------------------------
# Length scaling — derive (words band, sentences band, image count target,
# duration band) from the form's `cfg["duration_max_s"]`. Single source of
# truth that both _BASE_PROMPT and the orchestrator's RewriteContract read
# from, so the prompt + the validator can never disagree.
#
# Pre-2026-05-12 these were hardcoded ("110-160 words / 10-15 sentences /
# 22-32 s") and ignored the form's length_s entirely — picking 90s on the
# Customize step still produced a 25s narration.
#
# WPM target (~150) matches the long-form rewriter's convention. The hi/lo
# bands give the LLM ~15% wiggle either way; sentence band assumes ~12 wpm
# average sentence length (TTS-friendly subtitle shape).
# ---------------------------------------------------------------------------


_DEFAULT_TARGET_DURATION_S = 27  # midpoint of legacy 22-32s band
# Spoken-narration WPM at the typical Shorts atempo. ~300 wpm matches
# the legacy "110-160 words / 22-32 s" ratio (140 words / 28 s × 60 ≈
# 300 wpm). Set as a single tunable here so a future TTS provider
# swap recalibrates length scaling consistently across the prompt +
# validator + script_check.
_TTS_WPM = 300


def _length_targets(duration_s: int | None) -> dict[str, int]:
    """Compute the length-target dict the prompt + validator share.

    Returns a dict of:
      duration_min, duration_max, words_lo, words_hi, words_aim,
      underweight_lo, underweight_hi, sentences_lo, sentences_hi,
      min_image_warning.

    All ints — straight kwargs into ``_BASE_PROMPT.format(...)``.

    ``duration_s`` is the form's target duration (typically
    ``cfg["duration_max_s"]`` after ``_apply_form_overrides``). When
    None, falls back to the legacy 22-32 s / 110-160 word band so
    pre-2026-05-12 callers keep the same output shape.
    """
    if duration_s is None or duration_s <= 0:
        duration_s = _DEFAULT_TARGET_DURATION_S

    # ±20% bands around the target.
    duration_min = max(8, int(round(duration_s * 0.85)))
    duration_max = max(duration_min + 4, int(round(duration_s * 1.15)))

    aim_words = int(round(duration_s * _TTS_WPM / 60))
    words_lo = max(35, int(round(aim_words * 0.80)))
    words_hi = max(words_lo + 20, int(round(aim_words * 1.15)))

    # "Underweight" example range — calls out 30-50% of target so the
    # LLM doesn't ship the lazy short version.
    underweight_lo = max(15, int(round(aim_words * 0.30)))
    underweight_hi = max(underweight_lo + 10, int(round(aim_words * 0.50)))

    # ~12 wpm avg sentence; sentence count IS image count.
    sentences_lo = max(5, aim_words // 14)
    sentences_hi = max(sentences_lo + 3, aim_words // 9)
    min_image_warning = max(3, sentences_lo // 2)

    return {
        "duration_min": duration_min,
        "duration_max": duration_max,
        "words_lo": words_lo,
        "words_hi": words_hi,
        "words_aim": aim_words,
        "underweight_lo": underweight_lo,
        "underweight_hi": underweight_hi,
        "sentences_lo": sentences_lo,
        "sentences_hi": sentences_hi,
        "min_image_warning": min_image_warning,
    }


# Subtitle-shape + prosody rules apply identically to ALL narration
# modes (Part-1 vanilla, Part-1 cliffhanger, Part-2 finale). Extracted
# into one constant so the rules can't drift between prompts. If you're
# tuning a rule, change it here, NOT inside _BASE_PROMPT or _PART2_PROMPT.
_SHARED_CRAFT_RULES = """\
SUBTITLE-FRIENDLY SENTENCE SHAPE (NON-NEGOTIABLE — drives caption
clarity AND visual density. Each sentence is one subtitle and one
image):

- Average sentence length 5–12 words. HARD CAP at 14. A 15-word
  sentence must be split.
- Each sentence stands ALONE. No "and then ... and then ..." run-ons,
  no clauses that only make sense paired with the next sentence. A
  reader who only saw one subtitle should still understand it.
- Use periods, not commas, to separate beats of action. "I refused.
  She yelled." is GOOD. "I refused, and she yelled" is BAD — it
  collapses two visual beats into one image and one cluttered caption.
- One concrete visual per sentence — a number, a prop, a place, a
  person doing one specific thing. If a sentence has no concrete
  visual hook, rewrite it.
- Prefer short verbs and concrete nouns. Cut filler: "just", "really",
  "kind of", "sort of", "basically", "literally", "you know", "I
  mean", "honestly", "totally", "very". These eat caption space and
  add no story value.
- No semicolons. No em-dashes WITHIN a single sentence (em-dashes
  BETWEEN sentences are fine — see prosody rules below). Use a
  period and split.

PROSODY (this is what fixes the "monotonous narrator" problem — the
voice model is FLAT, so the WORDS and PUNCTUATION have to do the work
of pauses, emphasis, and breath):

- VARY sentence length. Don't write 12 sentences all 8 words long —
  that's the monotony viewers complain about. Mix punchy 2–4 word
  beats with longer 10–12 word ones. The pattern that works:
  long-set-up → short-punch → long-context → short-reveal. Example
  rhythm: "She showed up to my baby shower uninvited. (long)
  Wearing white. (short) She brought her own cake and put it next to
  mine. (long) Then she lit a cigarette indoors. (short)"
- Use one-word and two-word sentences for DRAMATIC LANDINGS only —
  on the pivot reveal and the kicker. "She lied." "Every time."
  "All of it." Two rules, both non-negotiable:

    1. NEVER consecutive. A short fragment must be PRECEDED by a
       longer (≥6 word) sentence and FOLLOWED by another longer
       sentence. Two short fragments back-to-back reads as a
       staccato list, not a punch — the TTS chops them like a
       robot reading inventory.
    2. NEVER for enumeration / list items. If you want to list
       three things ("wine, crafts, just us"), use ONE sentence
       with commas — not three one-word sentences. The comma
       version reads as a single breath unit; the period version
       reads as three abrupt chops.

  Cap: ≤2 such dramatic-punch sentences per ENTIRE Short. Overuse
  blunts the effect; the third one stops sounding like a punch.

  BAD (staccato list):       Wine. Crafts. Just us.
  GOOD (one-breath list):    Wine, crafts, just us.

  BAD (back-to-back punch):  She lied. Every time. About all of it.
  GOOD (separated punch):    She had been lying for months. About
                             everything. Every single thing she said
                             was a lie.

- PARAGRAPH BREAKS (this is the biggest lever for breath / pacing).
  Insert a BLANK LINE (literal "\\n\\n") between major narrative
  beats — typically 3 to 4 times per Short, at the seams: end of
  setup → start of conflict; conflict → pivot ("wait what" reveal);
  pivot → consequence/stakes; before the closer. The TTS treats a
  blank line as a real breath, far longer than a comma or period.
  Without these, the narration becomes one breathless run. Example:

      I worked sixty hours a week. My wife stayed home with the kids.

      Then I found her texts. She was meeting Dave on Thursdays.

      Turns out Dave is my brother. I sat in my car and stared.

      I packed a bag that night. Am I wrong here?

  Each blank line is a held silence — that's where the listener
  catches up emotionally. Use them on the SEAMS, not within a beat.

- ELLIPSIS (...) for a hanging beat the listener leans into. ONE
  ellipsis per Short, max — usually on the pivot reveal. "Until I
  checked her phone..." or "And then I saw who was driving..."
  More than one and the rhythm collapses.

- DO NOT use ALL CAPS for emphasis. Tested in production: Kokoro
  (and most neural TTS) reads ALL-CAPS tokens as letter-spelled
  acronyms — "OUT" became "O U T", "AGAIN" became "A G A I N",
  destroying the line. Get emphasis from WORD CHOICE and SENTENCE
  LENGTH instead: a punchy 2-word sentence after a long set-up
  ("She lied. Every time.") lands the kicker harder than any
  capitalisation could.

- DO NOT use AITA-class verdict acronyms in narration: AITA, WIBTA,
  YTA, NTA, NAH, ESH. User feedback 2026-05-03 — the channel never
  pronounces these. Use natural English instead: "Am I wrong for…",
  "Was I out of line?", "Am I the one in the wrong here?". The
  visual closer panel still displays "LIKE if YTA / COMMENT if NTA"
  on screen — that lives in pixels, never in narration. The
  audio-path defensive layer (pipeline/audio.py) will STRIP any
  sentence still containing these acronyms before TTS, so
  authoring them anyway just deletes that sentence from the
  spoken output. Don't author them in the first place.

- Reddit-class relationship/meta abbreviations stay allowed and
  read correctly letter-by-letter: MIL, FIL, SIL, BIL, DIL, OOP,
  NC, TIFU, TIL. These are the ONLY all-caps tokens that should
  appear in narration. Everything else: regular case.

- EM-DASH (—) BETWEEN sentences for a hard interruption beat,
  not inside a sentence. "I told her no — she lost it." Read as two
  sentences with a sharper cut than a period.

- Question marks aren't only for the closer. Mid-narration "?" lands
  hard if it follows a reveal: "What do you think she said?" or
  "Want to know the worst part?" — invites the viewer to think
  before you answer.

- Numbers and currency:

  * **YEARS, DATES, AGES, SIMPLE COUNTS** — use DIGITS. Modern Cloud
    Run TTS (Chatterbox v2 + IndicF5) reads "1258", "2005", "the
    1990s", "she was 42", "60 guests", "3 kids" naturally — pre-fix
    we asked the LLM to spell these as words ("twelve fifty-eight",
    "two thousand five") which slowed pace AND risked
    mispronunciation drift across TTS providers. The 2026-05-13
    audit (docs/pipeline_bug_catalogue_v2_2026-05-14.html) flagged
    'twelve fifty-eight' as audible regression on the
    historyrecapped/baghdad-mongols-1258 renders. Use digits.

  * **CURRENCY WITH SYMBOL** — STILL spell out, because the dollar/
    euro symbol is the actual failure mode. TTS reads "$2000" as
    "two zero zero zero" or "$ two thousand" depending on the
    model. Write "two thousand dollars" / "four hundred euros"
    instead. (If you're authoring the digit form for emphasis,
    write "2000 dollars" — drop the symbol.)

  * **DECIMALS, FRACTIONS, RATIOS** — spell out ("one and a half",
    "two-thirds", "three to one"). TTS handles "1.5" inconsistently
    (sometimes "one point five", sometimes "one decimal five").

  * **PHONE NUMBERS, ZIP CODES** — never narrated; if the source
    contains one, redact or paraphrase ("a New York area code").

- QUOTED DIALOGUE: TTS does not voice-act. A quoted line ("Don't
  feel good. Gotta bail.") sounds identical to the narrator —
  there's no vocal character distinction in the audio. To frame
  dialogue clearly:
    * Keep quoted lines SHORT — ideally 3-7 words, never more than
      ~10. "Don't feel good." stays. "Don't feel good, gotta bail,
      not coming, sorry" should be split or paraphrased.
    * Tag the speaker BEFORE the quote, with a comma or period:
      "She texted, \"My back hurts.\""  GOOD
      "\"My back hurts.\""               BAD (no speaker frame)
    * The comma before the opening quote biases TTS prosody toward
      a beat — Kokoro and most neural TTS pause on commas.
    * Or paraphrase: instead of "She said, \"You're being selfish\"",
      write "She called me selfish." Often cleaner anyway."""


_BASE_PROMPT = """\
You are writing a {duration_min}–{duration_max} second YouTube Shorts
narration in the style of top AITA / Reddit-story channels — long
enough to actually tell the story, short enough to keep retention.

LENGTH (NON-NEGOTIABLE — short narrations under-deliver on this format):
- {words_lo}–{words_hi} words total. Aim for ~{words_aim}. Under
  {words_lo} is too thin; do not ship a narration in the
  {underweight_lo}–{underweight_hi} word range — that produces only
  a flat slideshow.
- {sentences_lo}–{sentences_hi} short sentences. Each sentence
  becomes ONE image AND ONE caption, so sentence count IS image
  count. {min_image_warning} sentences = {min_image_warning} images
  = a flat slideshow; we want {sentences_lo}+.
- Spoken duration ~{duration_min}–{duration_max} seconds at typical
  TTS pace.

OPENING:
- Hook in the first ~8 words: a curiosity-gap question, an
  "Am I wrong for…" / "Was I out of line for…" frame, OR a strong-
  claim verb (refused, told, caught, left, threw, etc.). NEVER use
  the AITA acronym in the spoken hook (rule above) — phrase the
  question in natural English. NEVER "Hi guys" or "today's story is".
- Inciting wedge in the first ~30 words: a number, a name, a
  specific prop ("$4200" → "4,200 dollars" or "four thousand
  dollars" depending on TTS comfort, "my SIL Megan", "the second
  wedding cake", "the group chat"). For YEARS / AGES / COUNTS use
  digits ("1258", "she was 42", "60 guests") — modern TTS handles
  these naturally; only currency-with-symbol and decimals need
  spelling out (see Prosody rules).

SPICY (this is what separates retentive AITA from algorithmic mush —
mine the source story for these and put them in):
- NAME the antagonist with their relationship: "my MIL Carol", "my
  SIL Megan", "my husband's brother Dave". Anonymous "my friend"
  reads as fake; named relationships read as a Reddit post you
  actually saw. (If the source uses initials/abbreviations, keep
  them — DIL, MIL, SIL, BIL, OOP, NC, are all standard.)
- SPECIFIC numbers and dates — preferred form depends on type:
  * Years / dates / ages / simple counts → DIGITS ("1258", "2005",
    "she was 42", "60 guests", "two days before the wedding").
    Modern TTS reads these naturally.
  * Currency with $/€ symbol → SPELL OUT ("four thousand two
    hundred dollars" or write "$4,200" without symbol risk).
  * Decimals / fractions → SPELL OUT ("one and a half", "two-thirds").
  Vague "a lot of money" or "a while ago" loses viewers — pick a
  number even if you have to estimate from context.
- ESCALATION CURVE: hook → first wrong → antagonist doubles down →
  the moment it broke → the kicker. The story should get WORSE as
  it goes, not just describe one event.
- ONE unmistakable villain action: the named worst thing the
  antagonist did. Don't soften it. If they screamed, say screamed,
  not "got upset". If they threw something, say threw, not
  "reacted poorly".
- A "WAIT WHAT" pivot somewhere in the middle: a detail that flips
  how the viewer reads the story (she'd been doing it for months;
  the kid wasn't even hers; the bill was forged; he'd told his mom
  first). This is the comments-bait — viewers stay to argue about
  the pivot.
- STAKES the narrator names directly: what they will lose or
  already lost. "Now she won't speak to me." "I had to cancel the
  venue." "He moved out the next day." Without stakes the closer
  feels academic.
- Conversational, present tense. Don't editorialize ("crazy
  story!", "this is wild"). Let the facts hit.

{shared_rules}

BEFORE YOU RETURN: count your sentences. If under {sentences_lo}, you
have not followed the brief — go back and break long sentences into
more short ones, or add a missing escalation/pivot/stakes beat from
the source story. If under {words_lo} words total, do the same. Then
re-read your narration aloud: does the rhythm vary, or are all
sentences the same length? If the latter, rewrite — vary it.

{closer_block}

Input story (raw — mine the specific details, names, numbers, props):
\"\"\"
{story}
\"\"\"

Return ONLY a JSON object (no prose, no markdown fences):
{{
  "hook": "<first ~5–10 words of the narration>",
  "narration": "<full {words_lo}–{words_hi} word narration, {sentences_lo}–{sentences_hi} short sentences, including the hook AND the closer>",
  "title_options": ["<click-bait title A>", "<title B>", "<title C>"],
  "metadata": {{
    "era_anchor": "<OPTIONAL — only when the topic is set in a specific historical period (e.g. baghdad-mongols-1258, ww1-trench, roman-pompeii). Pick a kebab-case key from the curated taxonomy at pipeline/era_taxonomy.yaml: 13c-mongol-yuan-warband, 12c-norman-knight-mailcoat, 1c-roman-legion-segmentata, 1c-roman-republic-toga, 16c-tudor-court, 16c-mughal-court, 17c-mughal-shahjahan, 18c-georgian-britain, ww1-1914-1918-trench, ww2-1939-1945-european-theater, cold-war-1947-1991-civilian, byzantine-1453-fall-of-constantinople, edo-japan-1603-1868-samurai, ancient-egypt-pharaonic-new-kingdom. Anchors the image-gen costume + period — without it, 'soldiers' renders as WW1 trench infantry by default. OMIT for non-historical topics (AITA, sports, contemporary mystery)>"
  }}
}}
"""


# Part-2 prompt for the cliffhanger format. Auto-rendered by
# F24 (2026-05-23): Part-2 watcher deleted; this rewriter is now manual-only.
# Uses the SAME _SHARED_CRAFT_RULES as Part 1 (subtitle shape, prosody,
# TTS quirks — all the bits that don't depend on story structure) but
# completely overrides OPENING / SPICY / closer behavior because the
# story shape is different: Part 2 starts at the kicker, not the hook.
_PART2_PROMPT = """\
You are writing PART 2 of a two-part YouTube Shorts AITA narration.
Part 1 cut on a cliffhanger; this is the finale. Your job is to deliver
the kicker, the verdict moment, and the aftermath — the payoff Part 1
deliberately withheld.

LENGTH (NON-NEGOTIABLE):
- 110–160 words total. Aim for ~140. Same as Part 1.
- 10–15 short sentences (one per beat / image / caption).
- Spoken duration ~22–32 seconds.

OPENING — RECAP THEN PIVOT (Part 2 specific, OVERRIDES the standard
hook/wedge guidance):
- The first 1-2 sentences are a TIGHT recap that re-grounds anyone
  who never saw Part 1. Name the antagonist + the in-progress
  conflict in ~15 words. NEVER summarize the entire Part-1 plot.
  GOOD: "Last time, my MIL Carol crashed my baby shower with sixty
  uninvited guests. Then she lit a cigarette indoors."
  BAD: "If you missed Part 1, my MIL Carol used to come over and..."
  (too vague, no momentum)
- Then immediately pivot to the kicker / next beat that Part 1 cut
  before. Use a transitional phrase that signals continuity:
  "Here's what happened next.", "And then she said it.", "What I
  haven't told you yet is..."
- DO NOT repeat any sentence verbatim from Part 1. Don't re-narrate
  scenes Part 1 already covered. Pick up AFTER the cut.

PART 1 CONTEXT (this is what Part 1 already showed — DO NOT repeat it,
DO continue from where it left off):
\"\"\"
{part1_narration}
\"\"\"

STORY SHAPE (Part 2 specific):
- BEAT 1: recap (1-2 sentences, ~15 words)
- BEATS 2-4: the kicker — the worst thing the antagonist did, OR
  the reveal Part 1 hinted at. Highest-density specifics here.
- BEATS 5-8: the verdict moment + immediate consequence (cops
  called, husband walked out, wedding cancelled, court date — the
  thing that resolves the in-progress stake).
- BEATS 9-11: the aftermath — what's true now. Resolved stakes ARE
  allowed in Part 2 (unlike Part 1). "She hasn't spoken to me since."
  "He moved out a week later." "We're filing for custody."
- BEAT 12+: the closer.

SPICY (same rules as Part 1 — name the antagonist with their
relationship, spell numbers as words, ONE unmistakable villain action,
conversational present tense). Mine the source story below for any
specifics Part 1 didn't already use.

{shared_rules}

BEFORE YOU RETURN: count your sentences. Under 10 means you collapsed
the story; expand the kicker or the aftermath. Verify you did NOT
repeat any sentence from the Part 1 context above. Verify the recap
is in the first ~15 words, not the first 30.

{closer_block}

Original source story (full — mine for kicker/aftermath details Part 1
didn't already use):
\"\"\"
{story}
\"\"\"

Return ONLY a JSON object (no prose, no markdown fences):
{{
  "hook": "<first ~5–10 words of the narration — the recap line>",
  "narration": "<full 110–160 word Part-2 narration, 10–15 short sentences, including the recap, the kicker, the aftermath AND the closer>",
  "title_options": ["<part-2 title A>", "<title B>", "<title C>"]
}}
"""


def _cliffhanger_closer_block() -> str:
    """Prompt branch for Part-1 cliffhanger channels.

    The story is intentionally cut BEFORE the resolution. The audio CTA
    asks the viewer to subscribe so they catch Part 2 — phrased as a
    real human storyteller would, not the robotic "smash subscribe"
    formula. The visual SUBSCRIBE panel is rendered separately from
    cfg["closer_format"], same mechanism as the standard AITA closer.
    """
    return (
        "CLIFFHANGER FORMAT (Part 1 of 2 — non-negotiable, OVERRIDES "
        "earlier escalation/stakes guidance where they conflict):\n"
        "- Cut the narration BEFORE the resolution. Do NOT reveal the "
        "verdict, the kicker, or how it ended. Stop at the highest-"
        "tension moment: the line of dialogue that broke it, the "
        "discovery that flipped the story, the moment right before "
        "the consequence lands.\n"
        "- ESCALATION OVERRIDE: the curve here is hook → first wrong → "
        "antagonist doubles down → the moment it broke → CUT. The "
        "kicker stays out — that's Part 2's payoff. If you write the "
        "kicker, you've ruined the format.\n"
        "- STAKES OVERRIDE: name only IN-PROGRESS stakes (what's "
        "happening RIGHT NOW), not RESOLVED stakes. \"She's pounding "
        "on the door.\" GOOD. \"She hasn't spoken to me since.\" BAD "
        "(that resolves it).\n"
        "- Pick a hard-cut beat the viewer cannot un-want to know — "
        "right when the antagonist did the worst thing AND right "
        "before the narrator reacts; or right when a hidden detail "
        "surfaces AND right before its meaning lands. The viewer "
        "must feel they were robbed of the payoff.\n"
        "- The narration's last STORY sentence (before the CTA) "
        "should be a hanging beat — a short fragment, a reveal "
        "without resolution, or an ellipsis. Examples: \"...and then "
        "I opened the box.\", \"That's when she said it.\", \"I "
        "still hadn't seen what was on the other side of the door.\"\n"
        "- Then end with a NATURAL HUMAN Part-2 CTA — the way a "
        "podcast or a friend mid-story would tease the next part. "
        "Two-sentence ending, max. Use varied phrasing across "
        "renders, not a fixed template.\n\n"
        "GOOD examples (sounds like a real person teasing Part 2):\n"
        "  - \"Part two drops next. Subscribe so you don't miss it.\"\n"
        "  - \"You're not gonna believe what happened next. Hit "
        "subscribe — Part 2 is wild.\"\n"
        "  - \"The rest of this story is insane. Subscribe for "
        "Part 2.\"\n"
        "  - \"Part 2 has the verdict — and the receipts. "
        "Subscribe.\"\n\n"
        "BAD — DO NOT use:\n"
        "  - \"AITA?\" or \"Am I the asshole?\" (verdict acronyms / "
        "the literal phrase are BANNED across all spoken narration on "
        "this channel — visual panel handles them, audio never says "
        "them)\n"
        "  - \"LIKE if YTA, COMMENT if NTA\" (every token here is "
        "banned in audio; visual panel only)\n"
        "  - \"Smash that subscribe button\" or \"Don't forget to "
        "subscribe\" (robotic creator voice)\n"
        "  - Resolving the story (no verdict, no how-it-ended, no "
        "moral, no \"and now we don't speak\" — Part 2 owns those)\n\n"
        "The verbal CTA must sound like a HUMAN ending Part 1 of a "
        "two-part story. The SUBSCRIBE / for Part 2 prompt is ALSO "
        "rendered as a separate VISUAL panel on screen — muted "
        "viewers still see and respond to it — but the audio should "
        "phrase the ask conversationally, not robotically."
    )


def _closer_block(closer_format: str | None) -> str:
    if not closer_format:
        return (
            "End with a question or twist that drives comments — but "
            "any vote-prompt CTA is fine here."
        )
    # User feedback 2026-05-03: the spoken closer must NEVER include
    # AITA-class verdict acronyms (AITA, WIBTA, YTA, NTA, NAH, ESH) or
    # the phrase "am I the asshole". The visual closer panel — rendered
    # separately from cfg["closer_format"] via compose.render_closer_panel —
    # carries those tokens on-screen as the engagement ask; the audio
    # path stays clean. Three components in the audio close, 2-3
    # sentences max:
    #   1. A natural verdict question phrased in plain English
    #      ("Am I wrong here?" / "Was I out of line?" — never AITA)
    #   2. A natural prompt for comments (real podcaster register)
    #   3. A short conversational subscribe ask (no "smash that…")
    return (
        "End the narration with a NATURAL HUMAN three-part CTA, "
        "2-3 short sentences max:\n"
        "  (a) A verdict question phrased in plain English — "
        "\"Am I wrong here?\", \"Was I out of line?\", "
        "\"Am I the one in the wrong?\". DO NOT use AITA-class "
        "acronyms (AITA, WIBTA, YTA, NTA, NAH, ESH) or the literal "
        "phrase \"am I the asshole\" anywhere in the spoken closer. "
        "The audio path strips any sentence containing those tokens; "
        "authoring them just loses the closer.\n"
        "  (b) An invitation for comments — a real podcaster's line, "
        "not a YouTube-preset.\n"
        "  (c) A short conversational subscribe ask — phrased the way "
        "a real storyteller would say it, NEVER \"smash that "
        "subscribe button\" / \"don't forget to subscribe\" / "
        "\"hit the bell\". Examples below.\n\n"
        "GOOD examples (this is what we want — sounds like a real "
        "person ending a story):\n"
        "  - \"Am I wrong here? Drop your verdict below — and stick "
        "around if you want more stories like this one.\"\n"
        "  - \"What would you have done? Tell me in the comments. "
        "Subscribe and I'll see you on the next one.\"\n"
        "  - \"Was I out of line? You decide. Subscribe for one of "
        "these every day.\"\n"
        "  - \"Am I the one in the wrong? Comment below. And follow "
        "along — I post more of these.\"\n"
        "  - \"Tell me what you would have done. Subscribe if you want "
        "more — there's another one tomorrow.\"\n\n"
        "BAD — DO NOT use any of these:\n"
        "  - \"AITA?\" / \"Am I the asshole?\" (verdict acronyms / "
        "phrase banned in spoken narration; visual panel handles it)\n"
        "  - \"LIKE if YTA, COMMENT if NTA. AITA?\" (every token here "
        "is banned in audio)\n"
        "  - \"Like if you think yes, comment if you think no.\"\n"
        "  - \"Smash that like button and subscribe.\"\n"
        "  - \"Don't forget to subscribe and hit the bell.\"\n\n"
        "The verbal CTA must sound like a HUMAN ending a story, not "
        "a creator reading off a checklist. Keep the subscribe line "
        "to ~6 words, casual register. Don't over-explain why."
    )


@_obs.traced("llm.rewrite.rewrite", category="llm")
def rewrite(raw_story: dict, channel_cfg: dict | None = None) -> Script:
    """Rewrite a raw story dict into a Script.

    Routes through the orchestrator (constraint-aware prompt + auto-retry
    on validation failure) by default. Set ``YTFACTORY_REWRITE_USE_LEGACY=1``
    to fall back to the pre-orchestrator single-shot path while
    debugging.

    ``raw_story`` follows the schema produced by pull_stories.py:
    ``{slug, title, body, source, url, metadata}``.
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    if not title and not body:
        raise ValueError("raw_story has no title or body")

    cfg = channel_cfg or {}

    # Default path: orchestrator-driven with constraint-aware prompt and
    # auto-retry on script_check failures. Old single-shot path stays
    # callable via the env override below.
    if os.environ.get("YTFACTORY_REWRITE_USE_LEGACY", "0").lower() not in ("1", "true", "yes"):
        return _rewrite_via_orchestrator(raw_story, cfg)

    return _rewrite_legacy(raw_story, cfg)


def _rewrite_via_orchestrator(raw_story: dict, channel_cfg: dict) -> Script:
    """Orchestrated path — see :class:`pipeline.llm.contracts.RewriteContract`.

    The contract gathers constraints from the SAME validators (script_check
    + script_lint) the renderer later runs, builds a prompt with examples
    derived from those validators (drift-impossible), and validates the
    output inline with the same gates. On failure the orchestrator
    regenerates with a focused diff prompt up to ``YTFACTORY_LLM_MAX_RETRIES``
    times before raising.
    """
    from .contracts import RewriteContract  # PLC0415 — avoid import cycle
    from .orchestrator import StageContext, run_stage  # PLC0415

    contract = RewriteContract()
    ctx = StageContext(
        channel=channel_cfg.get("channel") or channel_cfg.get("name") or "unknown",
        channel_cfg=dict(channel_cfg),
        raw_input=dict(raw_story),
    )

    print(f"[rewrite] orchestrated rewrite for {raw_story.get('slug')!r} "
          f"(retries={os.environ.get('YTFACTORY_LLM_MAX_RETRIES', '2')})…")
    # Pass llm_call through the rewrite module's `llm` binding so existing
    # tests that ``patch.object(rw.llm, "call_claude_cli", ...)`` continue
    # to mock both the legacy and orchestrated paths uniformly.
    result = run_stage(contract, ctx, llm_call=llm.call_claude_cli)
    raw = result.output
    if result.attempts > 1:
        print(f"[rewrite] succeeded on attempt {result.attempts}/"
              f"{int(os.environ.get('YTFACTORY_LLM_MAX_RETRIES', '2')) + 1}")
    if result.final_warnings:
        for w in result.final_warnings:
            print(f"[rewrite] WARNING {w.constraint}: {w.reason}")

    # The contract validates; we still run script_lint for the auto-fix
    # side-effect (consecutive fragments collapse, etc.) before persisting.
    lint = script_lint.lint_and_fix(raw["narration"].strip())
    if lint.fixed:
        for fix_msg in lint.fixes_applied:
            print(f"[rewrite] script_lint: {fix_msg}")

    return Script(
        slug=raw_story.get("slug") or "untitled",
        hook=raw["hook"].strip(),
        narration=lint.narration,
        title_options=[t.strip() for t in raw["title_options"]][:3],
        source_url=raw_story.get("url") or "",
        source=raw_story.get("source") or "",
    )


def _rewrite_legacy(raw_story: dict, channel_cfg: dict) -> Script:
    """Pre-orchestrator single-shot rewrite path. Kept for fallback debug."""
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    story_text = f"{title}\n\n{body}" if title else body

    cfg = channel_cfg or {}
    closer_format = cfg.get("closer_format")
    is_cliffhanger = bool(cfg.get("cliffhanger"))

    closer_block = (
        _cliffhanger_closer_block() if is_cliffhanger
        else _closer_block(closer_format)
    )

    prompt = _BASE_PROMPT.format(
        shared_rules=_SHARED_CRAFT_RULES,
        story=story_text[:6000],  # cap context size
        closer_block=closer_block,
        **_length_targets(cfg.get("duration_max_s") if cfg else None),
    )

    print(f"[rewrite] authoring narration via claude CLI for {raw_story.get('slug')!r}…")
    raw = llm.call_claude_cli(prompt, output_json=True, model=llm.model_for("rewrite"), stage="rewrite")

    if not isinstance(raw, dict):
        raise ValueError(f"rewrite expected a JSON object, got {type(raw).__name__}")
    for required in ("hook", "narration", "title_options"):
        if required not in raw:
            raise ValueError(f"rewrite missing required field {required!r}: {raw!r}")
    if not isinstance(raw["title_options"], list) or not raw["title_options"]:
        raise ValueError("rewrite must return at least one title option")

    # Post-pass: deterministic linter + auto-fix for the structural
    # violations claude doesn't reliably honour (specifically:
    # consecutive ≤3-word fragments, which read as staccato lists
    # rather than dramatic punches). See pipeline/script_lint.py for
    # the rules and the auto-fix scope.
    lint = script_lint.lint_and_fix(raw["narration"].strip())
    if lint.fixed:
        for fix_msg in lint.fixes_applied:
            print(f"[rewrite] script_lint: {fix_msg}")
    for issue in lint.violations:
        # Violations remaining AFTER auto-fix are content-shape problems
        # (long sentences, total word count) that we can't safely rewrite
        # without changing the story. Surface them so the operator (or
        # the critic) can see what slipped through.
        print(f"[rewrite] script_lint WARNING: {issue}")

    return Script(
        slug=raw_story.get("slug") or "untitled",
        hook=raw["hook"].strip(),
        narration=lint.narration,
        title_options=[t.strip() for t in raw["title_options"]][:3],
        source_url=raw_story.get("url") or "",
        source=raw_story.get("source") or "",
    )


@_obs.traced("llm.rewrite.rewrite_part2", category="llm")
def rewrite_part2(
    raw_story: dict,
    part1_narration: str,
    channel_cfg: dict | None = None,
) -> Script:
    """Author the Part-2 finale of a cliffhanger short.

    Manual invocation (the Part-2 watcher daemon was deleted 2026-05-23 per F24).
    ``part2_trigger.subs_delta`` threshold. Takes the original full
    story plus the Part-1 narration so the LLM can continue from the
    cut without repeating scenes.

    ``channel_cfg`` should be the parsed Part-2 channel YAML — the one
    with ``part_two: true`` set (e.g. aita_cliffhanger_part2_animated).
    Its ``closer_format`` (standard "LIKE if YTA, COMMENT if NTA. AITA?")
    drives the audio CTA shape via the standard ``_closer_block``.
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    story_text = f"{title}\n\n{body}" if title else body
    if not story_text:
        raise ValueError("raw_story has no title or body")
    if not part1_narration.strip():
        raise ValueError("part1_narration is empty — Part 2 needs Part-1 context")

    cfg = channel_cfg or {}
    closer_format = cfg.get("closer_format")
    # Part 2 uses the STANDARD closer block (LIKE/COMMENT/AITA?) — the
    # cliffhanger / subscribe CTA was Part 1's job.
    prompt = _PART2_PROMPT.format(
        shared_rules=_SHARED_CRAFT_RULES,
        story=story_text[:6000],
        part1_narration=part1_narration.strip()[:4000],
        closer_block=_closer_block(closer_format),
    )

    print(f"[rewrite_part2] authoring Part-2 narration via claude CLI for {raw_story.get('slug')!r}…")
    raw = llm.call_claude_cli(prompt, output_json=True, model=llm.model_for("rewrite"), stage="rewrite_part2")

    if not isinstance(raw, dict):
        raise ValueError(f"rewrite_part2 expected a JSON object, got {type(raw).__name__}")
    for required in ("hook", "narration", "title_options"):
        if required not in raw:
            raise ValueError(f"rewrite_part2 missing required field {required!r}: {raw!r}")
    if not isinstance(raw["title_options"], list) or not raw["title_options"]:
        raise ValueError("rewrite_part2 must return at least one title option")

    # Same lint pass as Part 1 — staccato-fragment auto-fix is universal.
    lint = script_lint.lint_and_fix(raw["narration"].strip())
    if lint.fixed:
        for fix_msg in lint.fixes_applied:
            print(f"[rewrite_part2] script_lint: {fix_msg}")
    for issue in lint.violations:
        print(f"[rewrite_part2] script_lint WARNING: {issue}")

    return Script(
        slug=raw_story.get("slug") or "untitled",
        hook=raw["hook"].strip(),
        narration=lint.narration,
        title_options=[t.strip() for t in raw["title_options"]][:3],
        source_url=raw_story.get("url") or "",
        source=raw_story.get("source") or "",
    )


def load_script(path: Path) -> Script:
    with path.open() as f:
        raw = json.load(f)
    # Tolerate scripts written before source_url/source were added.
    raw.setdefault("source_url", "")
    raw.setdefault("source", "")
    return Script(**raw)


def save_script(script: Script, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(asdict(script), f, indent=2)
