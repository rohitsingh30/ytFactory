"""Script-shape validators.

Implements Principles #4 and #5 (DESIGN.md §14):

* **#4 Closing CTA**: AITA Shorts win or lose on the comment-section
  pile-on. The narration must end on a vote-prompting question.

* **#5 Hook + wedge timing**: The first 1.5s sets the hook (a
  question/claim). The next 1–2s introduces the inciting wedge (a
  number, a specific noun). Anything between is filler that costs
  retention.

Use ``check_script_text()`` on raw narration text before TTS, or
``check_beats()`` on the produced beats after stages 4-5.

CTA + cliffhanger CTA rules below pair each regex with a concrete
example string that is GUARANTEED to match the regex (enforced by
``tests/test_llm_script_check_invariants.py``). The
``pipeline.llm.contracts.rewrite_contract`` orchestrator pulls those
examples directly into the rewrite prompt — eliminating drift between
"what we ask the model to write" and "what the validator accepts".
Before this registry the prompt's GOOD example was hand-typed as
"what you would have done" while the regex required "what would you
have done" — Azure faithfully copied the GOOD example and got
rejected. With paired rules that's impossible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..beats import Beat


# AITA-class verdict acronyms (AITA, WIBTA, YTA, NTA) and the literal
# phrase "am I the asshole" are BANNED in spoken narration as of
# 2026-05-03 (user feedback). They're stripped from the audio path by
# pipeline.audio.normalize_for_tts; the rewriter is also instructed
# never to author them. The CTA detector accepts the natural-English
# replacements ("am I wrong", "was I wrong", "out of line") plus the
# generic question/verdict/comment-prompt patterns. The visual closer
# panel still renders the engagement ask via cfg["closer_format"] —
# that lives in pixels, never in audio.
@dataclass(frozen=True)
class CTARule:
    """One CTA acceptance rule paired with a guaranteed-matching example.

    The ``example`` field is the source of truth used by prompt-building
    code (see ``pipeline.llm.contracts.rewrite_contract``). Every rule's
    example MUST match its own pattern — verified by a unit test. If a
    pattern needs more variants, add them as separate ``CTARule``
    entries (each with their own paired example) rather than tweaking
    one pattern in isolation.
    """
    pattern: str
    example: str


# Closing CTA — must appear in the LAST sentence/clause.
_CTA_RULES: tuple[CTARule, ...] = (
    CTARule(r"\bam i (the )?wrong\b",                        "Am I wrong here?"),
    CTARule(r"\bam i the one (in the wrong|wrong here)\b",    "Am I the one in the wrong?"),
    CTARule(r"\bwas i (the )?wrong\b",                        "Was I wrong?"),
    CTARule(r"\b(was i|am i) out of line\b",                  "Was I out of line?"),
    CTARule(r"\bwhat would you (have )?(do|done)\b",          "What would you have done?"),
    # Same intent in the more natural reverse word order — Azure /
    # Claude both produce this phrasing readily and it's perfectly
    # idiomatic English. Pre-2026-05-10 this leaked through the
    # rewriter prompt as a GOOD example but the regex above didn't
    # accept it; this rule closes that gap.
    CTARule(r"\bwhat (you|do you) would (have )?(do|done)\b", "Tell me what you would have done."),
    CTARule(r"\bwhat do you think\b",                          "What do you think?"),
    CTARule(r"\bcomment[s]? (below|your)\b",                   "Comment below."),
    CTARule(r"\byour verdict\b",                               "Drop your verdict."),
    # Trailing-question-mark catch-all — last so the more specific
    # phrasings win the example-discovery race for prompt building.
    CTARule(r"\?$",                                            "Was I right to refuse?"),
)
_CTA_PATTERNS = tuple(r.pattern for r in _CTA_RULES)
_CTA_RE = re.compile("|".join(_CTA_PATTERNS), flags=re.IGNORECASE)


# Closer CTA patterns for Part-1 cliffhanger channels
# (channel_cfg.cliffhanger == True). The standard AITA vote-prompt
# patterns are deliberately absent from Part 1 — the verdict question
# lives in Part 2 — so the gate would otherwise reject every
# cliffhanger script. Here we accept any phrasing that names Part 2 or
# subscribes the viewer to it.
_CLIFFHANGER_CTA_RULES: tuple[CTARule, ...] = (
    CTARule(r"\bsubscribe\b",                                       "Subscribe for Part 2."),
    CTARule(r"\bpart\s*(2|two|ii)\b",                                "Part 2 drops next."),
    CTARule(r"\bnext\s+part\b",                                      "Catch the next part tomorrow."),
    CTARule(r"\b(coming|drops|drop)\s+(up\s+)?(next|soon|tomorrow)\b", "The finale drops tomorrow."),
    CTARule(r"\bdon'?t\s+miss\b",                                    "Don't miss the finale."),
    CTARule(r"\bhit\s+(the\s+)?(bell|subscribe|follow)\b",           "Hit the bell for the finale."),
    CTARule(r"\bfollow\s+for\s+(the\s+)?(rest|more|part)\b",         "Follow for the rest of the story."),
)
_CLIFFHANGER_CTA_PATTERNS = tuple(r.pattern for r in _CLIFFHANGER_CTA_RULES)
_CLIFFHANGER_CTA_RE = re.compile(
    "|".join(_CLIFFHANGER_CTA_PATTERNS), flags=re.IGNORECASE
)


def cta_examples(*, cliffhanger: bool = False) -> list[str]:
    """Return the GOOD-example strings for the requested CTA flavour.

    Used by the rewrite-stage orchestrator (``pipeline.llm.contracts.
    rewrite_contract``) to build the rewrite prompt's "GOOD examples"
    block from the SAME source the validator uses. Every returned
    string is guaranteed to satisfy the validator regex — no drift
    possible.
    """
    rules = _CLIFFHANGER_CTA_RULES if cliffhanger else _CTA_RULES
    # De-dup while preserving order — different rules occasionally
    # share an example phrasing.
    seen: set[str] = set()
    out: list[str] = []
    for r in rules:
        if r.example in seen:
            continue
        seen.add(r.example)
        out.append(r.example)
    return out

# Wedge tokens — concrete nouns / numbers that orient the audience.
_NUMBER_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
    r"thousand|million|first|second|third|\d+)\b",
    flags=re.IGNORECASE,
)

# Wedge alternates — when no number is in the opening, a named event
# (wedding/funeral/birthday/etc.) or a relationship abbreviation
# (MIL/FIL/SIL/BIL/DIL/OOP) is just as concrete an inciting hook for
# AITA stories. Without these alternates, openings like "I found my
# boyfriend's engagement ring" got flagged for "missing wedge" even
# though the prop is perfectly visualisable. Originated from the
# ER-nurse rewrite that the SPICY prompt produced — concrete, but
# numerical-wedge-free.
_WEDGE_EVENT_RE = re.compile(
    r"\b(?:wedding|funeral|engagement|birthday|"
    r"baby\s+shower|bachelorette|bachelor\s+party|"
    r"graduation|anniversary|honeymoon|"
    r"christmas|thanksgiving|"
    r"divorce|custody|adoption|"
    r"reception|reunion|"
    r"mil|fil|sil|bil|dil|oop)\b",
    flags=re.IGNORECASE,
)


@dataclass
class ScriptIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str


def _last_sentence(text: str) -> str:
    """Return the last sentence-ish chunk of text."""
    # Split on . ! ? but keep punctuation grouped with last sentence.
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[-1] if parts else text


# Closer rules are derived from the channel's own ``closer_format``
# string, not hardcoded for one niche. This keeps the validator
# niche-agnostic — TIFU, MaliciousCompliance, etc. each declare their
# own panel CTA in their channel YAML and the rules below shape the
# tail-token / leak-pattern checks accordingly.

# Glue words dropped from a closer_format when extracting required
# tail-anchor tokens. If a closer says "LIKE if you've been there,
# COMMENT your worst", the anchors that must appear in the narration
# tail are LIKE / COMMENT / been / there / worst — not "if" / "your".
_CTA_TOKEN_STOPWORDS: frozenset[str] = frozenset({
    "if", "the", "and", "or", "but", "your", "you", "youre", "youve",
    "youll", "youd", "do", "to", "is", "are", "be", "in", "on", "at",
    "of", "for", "with", "my", "this", "that", "these", "those", "what",
    "when", "would", "have", "has", "vote",
})

# A bigram leak pattern is built when an uppercase token is followed by
# one of these short glue words — e.g. "LIKE if", "COMMENT your". The
# bigram dramatically reduces false positives versus matching the
# uppercase token alone (lowercase "like" / "comment" appear all the
# time in normal narration).
_BIGRAM_TRIGGER_STOPWORDS: frozenset[str] = frozenset({
    "if", "the", "your", "you", "to", "for", "and", "or", "a", "an", "my",
})


def _required_closer_tokens(closer_format: str) -> list[str]:
    """Anchor tokens the narration tail must contain (case-insensitive).

    Pulled from the channel's ``closer_format`` declaration so each
    niche enforces its own panel CTA without a per-niche regex. Keeps
    uppercase tokens (LIKE, YTA, COMMENT, NTA, AITA) and content
    lowercase tokens ≥4 chars that aren't in ``_CTA_TOKEN_STOPWORDS``.
    """
    raw = re.findall(r"[A-Za-z][A-Za-z']*", closer_format)
    out: list[str] = []
    for t in raw:
        bare = t.replace("'", "")
        if bare.isupper() and len(bare) >= 2:
            out.append(bare.lower())
        elif len(bare) >= 4 and bare.lower() not in _CTA_TOKEN_STOPWORDS:
            out.append(bare.lower())
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _leak_patterns(closer_format: str) -> list[re.Pattern[str]]:
    """Distinctive panel substrings that MUST NOT appear in non-closer beats.

    Each uppercase token in ``closer_format`` becomes a case-insensitive
    word-boundary pattern. Common standalone words (LIKE, COMMENT) are
    bigrammed with their following glue word ("LIKE if", "COMMENT your")
    so legitimate narration uses don't false-trip; acronyms and tokens
    not followed by glue stay standalone.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z']*", closer_format)
    pats: list[re.Pattern[str]] = []
    for i, t in enumerate(tokens):
        if not (t.isupper() and len(t) >= 2):
            continue
        next_t = tokens[i + 1] if i + 1 < len(tokens) else None
        if next_t and next_t.lower() in _BIGRAM_TRIGGER_STOPWORDS:
            pats.append(re.compile(rf"\b{re.escape(t)}\s+{re.escape(next_t)}\b",
                                   flags=re.IGNORECASE))
        else:
            pats.append(re.compile(rf"\b{re.escape(t)}\b", flags=re.IGNORECASE))
    return pats


def check_script_text(
    text: str, *, channel_cfg: dict | None = None
) -> list[ScriptIssue]:
    """Validate a raw narration string before it's spoken.

    Checks structural rules:
    - Closing CTA in the last sentence (#4); for AITA channels with
      ``closer_format`` set, also enforces the LIKE-if-YTA / COMMENT-if-NTA
      split (memory feedback: vague "vote in comments" closers
      underperform on AITA).
    - Hook in the opening (~first 8 words contain a question or
      strong-claim construction) (#5)
    - Wedge: a number or quantity in the first ~30 words (#5)

    For AITA-class channels (``channel_cfg["closer_format"]`` set), the
    weak-hook / missing-wedge / weak-closer issues are escalated from
    warning → error to gate bad scripts at the door.
    """
    issues: list[ScriptIssue] = []
    text = text.strip()
    if not text:
        issues.append(ScriptIssue("error", "empty", "narration is empty"))
        return issues

    # Length warning. Shorts retention craters past ~30s; the rewrite
    # prompt targets 110–160 words but doesn't enforce it numerically.
    # Surface a warning so the operator can decide; never BLOCK the
    # render — even a long narration produces a playable mp4 (YouTube
    # Shorts allows up to 60s) and ``report(fail_on_error=True)`` would
    # otherwise abort the whole job for a soft retention nit.
    #
    # Word→duration estimate: ~0.35s per spoken word for natural
    # narration (~170 wpm) including modulation gaps. The estimate
    # over-counts a bit on speedy narrations and under-counts on
    # heavy-pause closers, so we flag a moderately wide band — anything
    # past these is at risk of viewer drop-off but might still ship.
    word_count = len([w for w in re.split(r"\s+", text) if w])
    est_duration = word_count * 0.35
    LENGTH_TARGET_WORDS = 165
    LENGTH_TARGET_S = 35.0
    if word_count > LENGTH_TARGET_WORDS or est_duration > LENGTH_TARGET_S:
        issues.append(
            ScriptIssue(
                "warning",
                "long_narration",
                f"narration is {word_count} words (~{est_duration:.0f}s) "
                f"— above retention target "
                f"{LENGTH_TARGET_WORDS} words / {LENGTH_TARGET_S:.0f}s. "
                f"Sweet spot is 110–160 words / ≤30s. Render will still "
                f"ship; consider a shorter rewrite next pass.",
            )
        )

    is_aita_class = bool(
        channel_cfg and channel_cfg.get("closer_format")
    )
    # Some channels (e.g. SportsStoriesAnimated) want the closer_format
    # panel but use third-person narration that doesn't fit AITA hook /
    # CTA patterns. They opt out of the strict AITA-class gating via
    # `script_check_strict: false`. Default stays True for backwards
    # compat — closer_format alone still implies strict for AITA.
    if channel_cfg and channel_cfg.get("script_check_strict") is False:
        is_aita_class = False
    is_cliffhanger = bool(channel_cfg and channel_cfg.get("cliffhanger"))
    # Severity for the soft principles. Warning by default; error for
    # channels that opt into strict gating via closer_format.
    soft_sev = "error" if is_aita_class else "warning"

    # #4 Closing CTA — scan the last 2 sentences combined, not just
    # the literal last sentence. Natural human Shorts narrators end
    # with the question + an invitation ("AITA? Tell me in the
    # comments.") and the question lives in the second-to-last
    # sentence; older code that looked only at the literal last
    # sentence rejected this perfectly fine ending.
    #
    # Cliffhanger channels (Part 1 of 2) have no AITA question — the
    # verdict lives in Part 2. They use a Part-2/subscribe CTA pattern
    # set instead. Selection is on channel_cfg.cliffhanger; the visual
    # SUBSCRIBE panel is still rendered separately from closer_format.
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    last = " ".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else text)
    cta_re = _CLIFFHANGER_CTA_RE if is_cliffhanger else _CTA_RE
    if not cta_re.search(last):
        if is_cliffhanger:
            cta_examples = (
                '"Subscribe for Part 2", "Part 2 drops next", '
                '"Don\'t miss it", "Hit the bell"'
            )
        else:
            cta_examples = (
                '"Am I wrong here?", "Was I out of line?", '
                '"What would you have done?"'
            )
        # Severity follows soft_sev: AITA-class channels fail the gate
        # (they live and die on the comment-section pile-on); channels
        # that opt out via script_check_strict: false (sports, war,
        # rhymes) only get a warning — their formats end on a hanging
        # beat or a chant rather than a verdict question.
        issues.append(
            ScriptIssue(
                soft_sev,
                "missing_cta",
                f"last sentence has no closing CTA "
                f"(e.g. {cta_examples}). "
                f"Last sentence: {last!r}",
            )
        )

    # #4b Channel-declared closer enforcement — RELAXED 2026-05.
    # Previously this required every anchor token from cfg["closer_format"]
    # to appear in the narration tail. That was wrong: the closer_format
    # describes the VISUAL panel, not what should be SPOKEN. Operator
    # feedback was that the spoken "LIKE if YTA, COMMENT if NTA" sounded
    # robotic — real human Shorts narrators end on the question and
    # let the visual panel carry the CTA. The narration just needs a
    # vote-prompt question (already enforced by #4 above via _CTA_RE),
    # so we no longer require the panel anchors in the spoken tail.
    # The visual panel still renders independently from
    # cfg["closer_format"] via compose.py:render_closer_panel.

    # #5a Hook in first ~8 words: must contain a question, an
    #     "Am I wrong" frame (the natural-English replacement for AITA
    #     framing per 2026-05-03 user feedback), a strong claim verb,
    #     OR a structural opener pattern (subject + verb / relationship
    #     phrase). The verb-allowlist approach keeps producing false
    #     positives on every new rewriter output (each narration uses
    #     verbs we haven't catalogued — "found", "cornered", "calls",
    #     etc.). The structural-pattern fallback catches "My MIL calls
    #     me a whale", "She refuses to come to the wedding", "He
    #     packed a bag" — all structurally strong openings regardless
    #     of the specific verb stem.
    head_words = text.split()[:8]
    head = " ".join(head_words)
    has_question = "?" in head
    # Natural-English wrong-frame replaces the older AITA acronym frame.
    # Detects "Am I wrong", "Was I wrong", "Was I the asshole" (still
    # catches the legacy phrase even though the rewriter no longer
    # produces it, so old hand-edited scripts validate), "Am I out
    # of line", "Was I out of line", and the variant "the one in
    # the wrong here".
    has_wrong_frame = re.search(
        r"\b(am i (the )?(wrong|asshole)|was i (the )?(wrong|asshole)|"
        r"(am i|was i) out of line|the one in the wrong)\b",
        head,
        flags=re.IGNORECASE,
    )
    # "My MIL ...", "Her sister ...", "His ex ...", "My husband Carol ..."
    has_relationship_subject = re.search(
        r"^\s*(my|her|his|our|their)\s+\w+",
        head,
        flags=re.IGNORECASE,
    )
    # "I refused", "She told", "They said", "He kicked" — pronoun +
    # any word ≥3 chars (catches verbs in any tense, gerunds, modals).
    # Excludes "I am" / "I was" / "I had a" (need a verb beyond the
    # auxiliary), but those rarely lead a strong AITA opening anyway.
    has_subject_verb = re.search(
        r"^\s*(?:so\s+|and\s+|but\s+|then\s+)?"
        r"(i|we|she|he|they)\s+\w{3,}",
        head,
        flags=re.IGNORECASE,
    )
    has_claim_verb = re.search(
        r"\b("
        # original past-tense set
        r"refused?|told|caught|discovered|kicked|left|stopped|said|"
        r"yelled|threatened|reported|fired|dumped|"
        # physical-action AITA verbs (past tense)
        r"threw|broke|smashed|wiped|spilled|poured|burned|cooked|baked|"
        r"hit|slapped|punched|shoved|pushed|kicked|grabbed|took|stole|"
        r"hid|locked|trapped|tossed|dumped|tore|ripped|cut|slashed|"
        # social/relationship AITA verbs (past tense)
        r"banned|blocked|disinvited|exposed|outed|reported|texted|"
        r"called|emailed|posted|published|filmed|recorded|"
        # decisive AITA verbs (past tense)
        r"sold|gave|made|charged|billed|cancelled|canceled|"
        r"divorced|ghosted|married|adopted|moved|"
        # claim verbs in present-progressive / gerund form. Sonnet
        # gravitates here ("My kids are boycotting my wedding…");
        # they're as strong a hook as past-tense verbs and the
        # validator must accept them.
        r"refusing|telling|catching|kicking|leaving|stopping|"
        r"yelling|threatening|reporting|firing|dumping|"
        r"throwing|breaking|smashing|hitting|slapping|punching|"
        r"shoving|pushing|grabbing|taking|stealing|hiding|locking|"
        r"trapping|tossing|tearing|ripping|cutting|slashing|"
        r"banning|blocking|disinviting|exposing|outing|texting|"
        r"calling|emailing|posting|publishing|filming|recording|"
        r"selling|giving|making|charging|billing|cancelling|canceling|"
        r"divorcing|ghosting|marrying|adopting|moving|"
        # common claim verbs the original list missed in either form
        r"boycott(?:ed|ing)?|demand(?:ed|ing)?|accus(?:ed|ing)|"
        r"confront(?:ed|ing)?|defend(?:ed|ing)?|"
        r"ignor(?:ed|ing)|argu(?:ed|ing)|fought|fighting|"
        r"shamed?|shaming|sued?|suing|"
        r"snapp(?:ed|ing)|tricked?|tricking|warned?|warning|"
        r"discover(?:ed|ing)|skipp(?:ed|ing)|missed|missing|"
        r"hir(?:ed|ing)|quit|quitting|resign(?:ed|ing)|"
        # Discovery / possession / financial / movement verbs the
        # rewrite-stage Sonnet produces frequently. "I found her
        # texts.", "I had a $14 salad.", "She lied about everything.",
        # "He cheated for years." — all valid AITA hooks the original
        # list rejected.
        r"found|finding|"
        r"had|having|got|getting|"
        r"paid|paying|owe(?:d|s|ing)?|spent|spending|"
        r"lied|lying|lies|cheat(?:ed|ing|s)?|"
        r"hid|hiding|hides|kept|keeping|keeps|"
        r"served|serving|serves|"
        r"lost|losing|loses|won|winning|wins|"
        r"walked|walking|drove|driving|drives|ran|running|runs|"
        r"wore|wearing|wears|brought|bringing|brings|"
        r"bought|buying|buys|hosted|hosting|hosts|"
        r"hated|hating|hates|loved|loving|loves|"
        r"saw|seeing|sees|heard|hearing|hears|"
        r"banned|banning|outed|outing|crashed|crashing|"
        r"corner(?:ed|ing)?|lectur(?:ed|ing)|scold(?:ed|ing)?|"
        r"mock(?:ed|ing)?|ambush(?:ed|ing)?|insult(?:ed|ing)?|"
        r"embarrass(?:ed|ing)?|interrupt(?:ed|ing)?|"
        r"intervened?|intervening|hosted|hosting"
        r")\b",
        head,
        flags=re.IGNORECASE,
    )
    if not (
        has_question
        or has_wrong_frame
        or has_claim_verb
        or has_relationship_subject
        or has_subject_verb
    ):
        issues.append(
            ScriptIssue(
                soft_sev,
                "weak_hook",
                f"first ~8 words don't contain a question, "
                f"\"Am I wrong\"-frame, claim verb, or subject-verb "
                f"opener. Hook is the highest-leverage 1.5s. "
                f"Head: {head!r}",
            )
        )

    # #5b Wedge: an inciting hook in the first ~30 words. A NUMBER is
    # the canonical wedge ("$400", "three bottles", "60 guests"), but
    # AITA narrations also wedge cleanly on a NAMED EVENT (wedding,
    # funeral, birthday) or an AITA-standard RELATIONSHIP abbreviation
    # (MIL, FIL, SIL, BIL, DIL, OOP) — both immediately orient the
    # listener even without a numeric anchor. A narration with at
    # least one of these in the opening passes.
    head30 = " ".join(text.split()[:30])
    has_number = bool(_NUMBER_RE.search(head30))
    has_event = bool(_WEDGE_EVENT_RE.search(head30))
    if not (has_number or has_event):
        # Always a WARNING (never an error), regardless of channel.
        # The validator can't enumerate every concrete wedge a real
        # AITA narrator uses (places, professions, named props,
        # relationships beyond the abbreviations); a hand-curated
        # allow-list of 30 tokens will keep producing false positives
        # as the rewriter uses words outside the list. The hook check
        # is the real load-bearing gate (it catches "Hi guys, today's
        # story is..." openings); the wedge is a soft retention
        # principle better surfaced as a hint than a hard block. The
        # critic loop catches genuinely-weak openings post-render.
        issues.append(
            ScriptIssue(
                "warning",
                "missing_wedge",
                "first ~30 words contain no number, named event, or "
                "relationship abbreviation. Concrete wedges "
                "(\"$400\", \"three bottles\", \"my MIL\", \"the wedding\") "
                "anchor the conflict early.",
            )
        )

    return issues


def check_beats(
    beats: list[Beat], *, channel_cfg: dict | None = None
) -> list[ScriptIssue]:
    """Validate beats after stage 5. Cheaper checks since beats already exist."""
    issues: list[ScriptIssue] = []
    if not beats:
        issues.append(ScriptIssue("error", "no_beats", "no beats produced"))
        return issues

    full_text = " ".join(b.text for b in beats)
    issues.extend(check_script_text(full_text, channel_cfg=channel_cfg))

    # Beats #5 timing: hook beat ≤ 2s, wedge beat (i ∈ {0, 1}) duration not exorbitant.
    if beats[0].duration > 2.5:
        issues.append(
            ScriptIssue(
                "warning",
                "slow_hook",
                f"beat 0 is {beats[0].duration:.2f}s (>2.5s). "
                f"Tighter hooks land before viewers can swipe.",
            )
        )

    # Closer CTA must not leak into earlier beats. The closer is always
    # the LAST beat (positional convention — there is no is_closer flag
    # in the Beat dataclass). Leak patterns are derived from the
    # channel's own ``closer_format`` so any niche YAML works without
    # per-niche regex edits. Severity: error when the channel declares
    # a closer (mute-mode CTA collision is a real bug); warning
    # otherwise (no panel to collide with).
    closer_format = channel_cfg.get("closer_format") if channel_cfg else None
    if closer_format:
        leak_pats = _leak_patterns(closer_format)
        leak_sev = "error"
        for i, beat in enumerate(beats[:-1]):
            for pat in leak_pats:
                m = pat.search(beat.text)
                if m:
                    issues.append(
                        ScriptIssue(
                            leak_sev,
                            "cta_leak",
                            f"beat {i} caption contains closer-panel CTA "
                            f"language ({m.group(0)!r}); CTA must appear only in "
                            f"the final beat. Beat text: {beat.text!r}",
                        )
                    )
                    break

    return issues


def report(issues: list[ScriptIssue], *, fail_on_error: bool = False) -> None:
    """Pretty-print issues. Optionally raise on any error-severity issue."""
    if not issues:
        print("[script_check] ✓ no issues")
        return
    for it in issues:
        prefix = "✗" if it.severity == "error" else "⚠"
        print(f"[script_check] {prefix} [{it.code}] {it.message}")
    if fail_on_error and any(i.severity == "error" for i in issues):
        raise ValueError("script_check failed (errors above)")
