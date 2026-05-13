"""Regression tests for ``pipeline.llm.rewrite_long_form`` validator wiring.

Pins the rewrite-loop behaviour added on 2026-05-13 — the rewriter
now hard-fails on any contract violation and retries ONCE before
giving up, so a misbehaving LLM call can't ship a script that would
produce a silent / off-niche / wrong-length / dead-pacing video.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import pipeline.llm.rewrite_long_form as _rlf
from pipeline.critic_long_form import LongFormContractError


def _ok_rewrite_payload(
    *, niche_words: str = "shadow doorway watching figure dread fear cold",
    section_words: int = 450,
    section_count: int = 10,
    panel_count: int = 15,
    panel_hold: float = 6.0,
) -> dict:
    """Build a payload that passes every contract check.

    niche_words seeds the hook with r/nosleep dread tokens. section_words
    × section_count must produce >= 0.92 * 4500 = 4140 words for the
    30-min length floor.
    """
    sec_text = (niche_words + " " + ("word " * (section_words - 7))).strip()
    return {
        "hook": niche_words + " " + ("dread shadow watching " * 5),
        "thesis": "A test thesis.",
        "sections": [
            {
                "id": f"sec-{i}", "title": f"S{i}",
                "narration": sec_text,
                "target_s": 180.0,
                "visual_brief": "A scene.",
            }
            for i in range(section_count)
        ],
        "panels": [
            {"scene": f"panel scene {i}", "hold_s": panel_hold}
            for i in range(panel_count)
        ],
        "sources": [],
        "title_options": [
            "There is something standing in the hallway",
            "The figure was watching me from the doorway",
            "The shadow appeared at midnight",
        ],
    }


def _bad_rewrite_payload() -> dict:
    """Build a payload that violates every contract check (the
    rendered job 0c05c335 mistake stack)."""
    return {
        "hook": "Imagine a completely ordinary afternoon. Nothing dramatic happens. " * 3,
        "thesis": "Attention is a currency.",
        "sections": [
            # All short — under-delivers on length contract
            {"id": f"s{i}", "title": f"S{i}",
             "narration": "Imagine a person sitting on a couch. " * 30,  # ~210 words
             "target_s": 180.0, "visual_brief": None}
            for i in range(10)
        ],
        "panels": [
            # All 30s holds — violates panel hold cap
            {"scene": "person on couch", "hold_s": 30.0}
            for _ in range(20)
        ],
        "sources": [],
        "title_options": ["If You Can See This Keep Reading"],
    }


# ---------- happy path ---------------------------------------------------


def test_rewrite_passes_when_payload_clean():
    """Clean payload → no retries, no errors, returns envelope."""
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    payload = _ok_rewrite_payload()
    with patch.object(_rlf._llm, "call_claude_cli", return_value=payload) as mocked:
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )
    # Single LLM call (no retry needed).
    assert mocked.call_count == 1
    assert env.kind == "long_form"
    assert env.long_form is not None
    assert len(env.long_form.sections) == 10


# ---------- retry on hard violation -------------------------------------


def test_rewrite_retries_once_on_hard_violation_then_succeeds():
    """C2 — first call returns under-delivered script, second call fixes it.

    Validator catches the under-delivery, retry prompt fires, second
    LLM call returns clean payload → success after 2 calls.
    """
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    bad = _bad_rewrite_payload()
    good = _ok_rewrite_payload()
    with patch.object(_rlf._llm, "call_claude_cli", side_effect=[bad, good]) as mocked:
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )
    assert mocked.call_count == 2
    # The second-call prompt MUST contain the violation summary.
    second_prompt = mocked.call_args_list[1][0][0]
    assert "previous rewrite violated" in second_prompt.lower()
    # The envelope returned is from the GOOD second attempt.
    assert env.long_form is not None
    assert len(env.long_form.sections) == 10


def test_rewrite_raises_after_two_hard_violations():
    """C2 — both attempts fail → LongFormContractError surfaces to worker.

    Note: the parse-time hold-cap clamps panel.hold_s to the soft max
    before the validator runs, so panel_hold_too_long itself doesn't
    surface in the violation list. The OTHER hard violations
    (niche_tonal + length_under_delivered + stock_anecdote) are what
    fail twice and raise.
    """
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    bad = _bad_rewrite_payload()
    with patch.object(_rlf._llm, "call_claude_cli", side_effect=[bad, bad]) as mocked:
        with pytest.raises(LongFormContractError) as ei:
            _rlf.rewrite_long_form(
                raw_story, channel_cfg={"niche": "r/nosleep"},
                target_duration_s=1800,
            )
    assert mocked.call_count == 2
    codes = {v.code for v in ei.value.violations}
    # Niche + length both surface (the panel-hold cap pre-empts the
    # panel_hold_too_long violation as defense-in-depth).
    assert "niche_tonal_violation" in codes
    assert "length_under_delivered_hard" in codes


# ---------- panel hold cap defense-in-depth ------------------------------


def test_rewrite_caps_panel_hold_s_at_parse_time():
    """C5 — even when LLM returns hold_s > 12, the parse step caps it
    to the soft max so a marginal violation doesn't burn an LLM
    round-trip."""
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    # Panels at 13s — over hard max but barely. Without the parse-time
    # cap, the validator would trip; with the cap, hold_s gets clamped
    # to 8.0 and the validator passes.
    payload = _ok_rewrite_payload(panel_hold=13.0)
    with patch.object(_rlf._llm, "call_claude_cli", return_value=payload):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )
    # All panels capped at PANEL_HOLD_SOFT_MAX_S (8.0).
    assert all(p.hold_s <= 12.0 for p in env.long_form.panels)


def test_default_panel_hold_when_missing_is_six_seconds():
    """C5 — pre-2026-05-13 default was 30.0 (the silent-mp4 root)."""
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    payload = _ok_rewrite_payload()
    # Strip hold_s from every panel so the parse default fires.
    for p in payload["panels"]:
        p.pop("hold_s")
    with patch.object(_rlf._llm, "call_claude_cli", return_value=payload):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )
    assert all(p.hold_s == 6.0 for p in env.long_form.panels), \
        "missing hold_s MUST default to 6.0 (was 30.0 pre-2026-05-13)"


# ---------- niche-title rules wiring -------------------------------------


def test_prompt_template_includes_niche_title_rules_for_nosleep():
    """C8 — when niche=r/nosleep, the per-niche title-rule block lands
    in the prompt instead of the generic 'no rule registered' line."""
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    payload = _ok_rewrite_payload()
    with patch.object(_rlf._llm, "call_claude_cli", return_value=payload) as mocked:
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )
    prompt = mocked.call_args_list[0][0][0]
    assert "noun-anchor (room / door / hallway" in prompt
    assert "dread verb" in prompt


def test_prompt_template_falls_back_for_unknown_niche():
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    payload = _ok_rewrite_payload()
    with patch.object(_rlf._llm, "call_claude_cli", return_value=payload) as mocked:
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/totally_made_up"},
            target_duration_s=1800,
        )
    prompt = mocked.call_args_list[0][0][0]
    assert "no niche-specific title rule registered" in prompt


# ---------- length-floor wiring ------------------------------------------


def test_prompt_template_includes_minimum_word_floor():
    """C2 — the prompt MUST surface the hard MIN/MAX word range so
    the LLM sees the contract before generating."""
    raw_story = {"slug": "x", "title": "T", "body": "B"}
    payload = _ok_rewrite_payload()
    with patch.object(_rlf._llm, "call_claude_cli", return_value=payload) as mocked:
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )
    prompt = mocked.call_args_list[0][0][0]
    assert "MINIMUM" in prompt
    assert "MAXIMUM" in prompt
    # Computed targets: 30 min × 150 wpm = 4500 words; floor = 4140.
    assert "4140" in prompt
    assert "4950" in prompt  # ceiling = 4500 * 1.10
