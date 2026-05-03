"""Heuristic drama-score for AITA-style raw stories.

Where visualizability (`pipeline/visualizability.py`) gates whether a
story CAN be drawn, this module ranks stories that can — picking the
spiciest from a pool of candidates.

The score is a weighted sum of:
- named-antagonist hits (relationship + a name, "my MIL Carol")
- conflict-verb count (screamed, threw, kicked, sued, ghosted, ...)
- escalation markers ("then", "the next day", "that's when")
- "wait what" pivot signals ("turns out", "found out", "discovered")
- specific dollar amounts and numeric stakes
- named-event types (wedding, funeral, divorce, custody, ...)
- Reddit engagement (score, num_comments) — the community already
  voted with their mouse on which stories land

Returns ``(float in 0..1, list[reason_strings])`` so callers can log
why one story beat another. Pure heuristic — no LLM call. Cheap
enough to run on 10–25 candidates per pull (Principle #20 pattern:
cheap upfront filtering keeps the LLM budget on the rendering loop).
"""

from __future__ import annotations

import math
import re


# Relationship tokens — both formal and the AITA-standard abbreviations.
# A "named antagonist" hit is one of these followed by a Capitalized name.
_RELATIONSHIPS = (
    r"mil|fil|sil|bil|dil|son-?in-?law|daughter-?in-?law|mother-?in-?law|"
    r"father-?in-?law|sister-?in-?law|brother-?in-?law|"
    r"husband|wife|fiance|fiancée|fiancee|partner|"
    r"boyfriend|girlfriend|ex|ex-husband|ex-wife|ex-boyfriend|ex-girlfriend|"
    r"step-?mom|step-?dad|step-?mother|step-?father|step-?son|step-?daughter|"
    r"step-?sister|step-?brother|step-?kid|"
    r"mother|father|mom|dad|sister|brother|daughter|son|"
    r"aunt|uncle|cousin|grandma|grandpa|grandmother|grandfather|"
    r"friend|neighbor|boss|coworker|landlord|roommate"
)

# "my MIL Carol" / "her sister Megan" — relationship tag + Capitalized name.
_NAMED_ANTAGONIST_RE = re.compile(
    rf"\b(?:my|her|his|their|our)\s+(?:{_RELATIONSHIPS})\s+[A-Z][a-z]+\b",
    flags=re.IGNORECASE,
)

# Conflict verbs — concrete bad-actor actions. Counts each unique stem.
_CONFLICT_RE = re.compile(
    r"\b("
    r"scream(?:ed|ing|s)?|yell(?:ed|ing|s)?|shout(?:ed|ing|s)?|"
    r"threw|threaten(?:ed|ing|s)?|kick(?:ed|ing|s)?|slap(?:ped|ping|s)?|"
    r"punch(?:ed|ing|es)?|shov(?:ed|ing|es)?|push(?:ed|ing|es)?|"
    r"sue(?:d|s|ing)?|cheat(?:ed|ing|s)?|stole|stolen|forg(?:ed|ing|es)?|"
    r"ghost(?:ed|ing|s)?|expos(?:ed|ing|es)?|disinvit(?:ed|ing|es)?|"
    r"banish(?:ed|ing|es)?|cut\s+off|kicked\s+out|locked\s+out|"
    r"refus(?:ed|ing|es)?|ban(?:ned|ning|s)?|fir(?:ed|ing|es)?|"
    r"divorc(?:ed|ing|es)?|cancel(?:l?ed|l?ing|s)?|reported|"
    r"crashed|smashed|broke|destroyed|ruined|"
    r"lied|lying|lies|gaslight(?:ed|ing|s)?|manipulat(?:ed|ing|es)?"
    r")\b",
    flags=re.IGNORECASE,
)

# Escalation markers — phrases that introduce a worse-thing-after-thing
# beat. Lots of these means the story has a curve, not a single event.
_ESCALATION_RE = re.compile(
    r"\b("
    r"then|the\s+next\s+(?:day|morning|night|week)|"
    r"that'?s\s+when|after\s+that|later\s+that|"
    r"it\s+gets\s+worse|what\s+(?:she|he|they)\s+did\s+next|"
    r"to\s+make\s+(?:it|things)\s+worse|finally|"
    r"eventually|in\s+the\s+end|by\s+the\s+time"
    r")\b",
    flags=re.IGNORECASE,
)

# "Wait what" pivot signals — moments where the story flips the read.
# Even one of these is comments-bait gold.
_PIVOT_RE = re.compile(
    r"\b("
    r"turns?\s+out|found\s+out|figured\s+out|discover(?:ed|s|ing)?|"
    r"learned\s+(?:later|that)|come\s+to\s+find\s+out|"
    r"the\s+(?:truth|kicker|catch|twist)|all\s+this\s+time|"
    r"plot\s+twist|hadn'?t\s+(?:told|mentioned|said)|"
    r"behind\s+my\s+back|the\s+whole\s+time"
    r")\b",
    flags=re.IGNORECASE,
)

# Specific dollar/numeric stakes — concrete numbers anchor the conflict.
_DOLLAR_RE = re.compile(
    r"\$\s*\d{2,}|\b\d{2,}\s*(?:k|grand|thousand|dollars?|bucks?)\b",
    flags=re.IGNORECASE,
)

# Named events with built-in social stakes.
_EVENT_RE = re.compile(
    r"\b("
    r"wedding|funeral|engagement|birthday|baby\s+shower|"
    r"bachelorette|bachelor\s+party|graduation|anniversary|"
    r"honeymoon|holiday|christmas|thanksgiving|"
    r"divorce|custody|adoption|"
    r"surgery|hospital|hospice|"
    r"ceremony|reception|reunion"
    r")\b",
    flags=re.IGNORECASE,
)

# Stakes the narrator names directly — concrete losses/consequences.
_STAKES_RE = re.compile(
    r"\b("
    r"won'?t\s+speak|stopped\s+speaking|no\s+contact|nc\s+with|"
    r"moved\s+out|kicked\s+out|left\s+me|left\s+(?:him|her|them)|"
    r"called\s+off|cancel(?:l?ed|ling)\s+the|broke\s+(?:up|off)|"
    r"can'?t\s+(?:come|return|go)\s+back|lost\s+my|lost\s+the|"
    r"got\s+fired|got\s+evicted|getting\s+sued|"
    r"divorc(?:ing|ed)|going\s+to\s+therapy"
    r")\b",
    flags=re.IGNORECASE,
)


def score_drama(
    title: str,
    body: str,
    *,
    reddit_score: int | None = None,
    num_comments: int | None = None,
) -> tuple[float, list[str]]:
    """Score a story 0..1 on drama density. Returns ``(score, reasons)``.

    Reasons are human-readable strings keyed by the lever that fired,
    so the caller can log WHY one story beat another in the
    candidate pool.
    """
    text = f"{title}\n{body}"
    if not text.strip():
        return 0.0, ["empty"]

    reasons: list[str] = []
    points = 0.0

    # Each pattern: count distinct matches, cap, weight, normalize.
    levers: list[tuple[str, re.Pattern[str], float, int]] = [
        ("named_antagonist",  _NAMED_ANTAGONIST_RE, 0.18, 4),
        ("conflict",          _CONFLICT_RE,         0.10, 6),
        ("escalation",        _ESCALATION_RE,       0.08, 5),
        ("pivot",             _PIVOT_RE,            0.18, 3),
        ("dollar_stakes",     _DOLLAR_RE,           0.08, 4),
        ("named_event",       _EVENT_RE,            0.10, 3),
        ("named_stakes",      _STAKES_RE,           0.14, 3),
    ]
    for label, pat, weight_per, cap in levers:
        n = len(pat.findall(text))
        if n == 0:
            continue
        contribution = min(n, cap) * weight_per / cap
        points += contribution
        reasons.append(f"{label}={n}(+{contribution:.2f})")

    # Reddit engagement: the community already voted. Logarithmic so a
    # 50k-upvote post doesn't drown out a 5k post that's also fine.
    if reddit_score is not None and reddit_score > 0:
        # log10(1000)=3, log10(10000)=4, log10(50000)=4.7 — clamp to 5.
        eng = min(5.0, math.log10(max(1, reddit_score))) / 5.0  # 0..1
        contribution = 0.10 * eng
        points += contribution
        reasons.append(f"upvotes={reddit_score}(+{contribution:.2f})")
    if num_comments is not None and num_comments > 0:
        eng = min(4.0, math.log10(max(1, num_comments))) / 4.0
        contribution = 0.08 * eng
        points += contribution
        reasons.append(f"comments={num_comments}(+{contribution:.2f})")

    score = max(0.0, min(1.0, points))
    if not reasons:
        reasons.append("no_drama_signals")
    return score, reasons


def pick_best(stories: list[dict]) -> tuple[dict | None, list[tuple[dict, float, list[str]]]]:
    """Sort ``stories`` by drama score, return ``(best, ranked)``.

    Each story is a dict in the RawStory shape (``title, body,
    metadata``). ``ranked`` is the full list sorted descending so
    the caller can log the leaderboard.
    """
    scored: list[tuple[dict, float, list[str]]] = []
    for s in stories:
        meta = s.get("metadata") or {}
        sc, reasons = score_drama(
            s.get("title", ""),
            s.get("body", ""),
            reddit_score=meta.get("score"),
            num_comments=meta.get("num_comments"),
        )
        scored.append((s, sc, reasons))
    scored.sort(key=lambda t: t[1], reverse=True)
    best = scored[0][0] if scored else None
    return best, scored
