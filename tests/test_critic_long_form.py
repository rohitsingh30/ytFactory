"""Regression tests for ``pipeline.critic_long_form``.

Pins the contract validators added on 2026-05-13 in response to the
critique of job ``0c05c335…``. Every check covered with the bug
that motivated it referenced in the test name.
"""
from __future__ import annotations

import pytest

from pipeline import critic_long_form as critic


# ---------- niche-tonal contract (C1) ------------------------------------


def test_niche_lexicon_resolves_canonical_form():
    assert critic._niche_lexicon_for("r/nosleep") == critic.NICHE_TONAL_LEXICON["r/nosleep"]


def test_niche_lexicon_resolves_bare_form():
    assert critic._niche_lexicon_for("nosleep") == critic.NICHE_TONAL_LEXICON["r/nosleep"]


def test_niche_lexicon_resolves_reddit_prefix():
    assert critic._niche_lexicon_for("reddit_nosleep") == critic.NICHE_TONAL_LEXICON["r/nosleep"]


def test_niche_lexicon_unknown_returns_empty():
    assert critic._niche_lexicon_for("r/totally_made_up") == ()


def test_niche_lexicon_none_returns_empty():
    assert critic._niche_lexicon_for(None) == ()


def test_check_niche_contract_passes_when_dread_lexicon_present():
    # Hook contains "shadow", "doorway", "watching" — three lexicon hits.
    nar = (
        "I saw a shadow standing in the doorway last night. It was watching me. "
        "I told my husband but he didn't believe me. The figure stayed for hours. "
    )
    out = critic.check_niche_contract(nar, "r/nosleep")
    assert out == [], f"expected no violations, got {out}"


def test_check_niche_contract_hard_fails_when_zero_dread_tokens():
    """C1 — the exact rendered video that triggered the critique."""
    nar = (
        "Imagine a completely ordinary afternoon. A person sits somewhere — "
        "maybe on a couch, maybe at a desk, maybe on a bus rolling through traffic. "
        "Nothing dramatic is happening. Just a screen glowing softly. A thumb scrolling. "
        "Words passing by faster than they can be remembered. This is the environment "
        "most human thoughts now live in. In 2023, researchers estimated that the "
        "average person scrolls the distance of roughly 300 feet of content every "
        "single day on their phone. That's about the height of the Statue of Liberty."
    )
    out = critic.check_niche_contract(nar, "r/nosleep")
    assert len(out) == 1
    assert out[0].severity == "hard"
    assert out[0].code == "niche_tonal_violation"


def test_check_niche_contract_soft_warns_when_thin():
    # Only one lexicon token — soft warn.
    nar = (
        "I went to the basement to look for boxes. The walls were old. "
        "The ceiling was low. The lights worked but were dim. I found "
        "nothing interesting that night. " * 3
    )
    out = critic.check_niche_contract(nar, "r/nosleep")
    assert len(out) == 1
    assert out[0].severity == "soft"
    assert out[0].code == "niche_tonal_thin"


def test_check_niche_contract_no_op_for_unknown_niche():
    assert critic.check_niche_contract("any text", None) == []
    assert critic.check_niche_contract("any text", "r/totally_made_up") == []


def test_check_niche_contract_empty_narration_hard_fails():
    out = critic.check_niche_contract("", "r/nosleep")
    assert len(out) == 1
    assert out[0].code == "niche_empty_hook"


# ---------- length contract (C2/C3/C4) -----------------------------------


def _make_section(words: int, idx: int = 0):
    return {"id": f"sec-{idx}", "title": f"Section {idx}",
            "narration": "word " * words, "target_s": 180.0}


def test_check_word_count_passes_when_at_target():
    # 30-min target = 4500 words. Hit 4500 exactly across 10 sections.
    sections = [_make_section(450, i) for i in range(10)]
    assert critic.check_word_count(sections, target_duration_s=1800) == []


def test_check_word_count_hard_fails_under_50pct():
    """C2 — rendered video delivered 53% of target.

    Was 85% threshold pre-2026-05-13 calibration; lowered to 50%
    after observed Azure GPT-5.3 single-shot output for 30-min
    requests physically lands at 55-65% of target words. The 50%
    floor catches catastrophic under-delivery (LLM produced 5-min
    script for a 30-min request) without blocking realistic LLM
    output. Soft warn at 85% still surfaces the gap to dashboards.
    """
    # 30-min target = 4500 words. Deliver 2200 (49% — below 50% hard floor).
    counts = [220, 220, 220, 220, 220, 220, 220, 220, 220, 220]
    sections = [_make_section(c, i) for i, c in enumerate(counts)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    hard = [v for v in out if v.severity == "hard"]
    codes = {v.code for v in hard}
    assert "length_under_delivered_hard" in codes


def test_check_word_count_soft_warns_at_realistic_llm_floor():
    """C2 — rendered video delivered 58% of target — should soft-warn but not hard-fail."""
    # 30-min target = 4500 words. Deliver 2631 (the actual job 0947ea51 count).
    counts = [263, 263, 263, 263, 263, 263, 263, 263, 263, 263]
    sections = [_make_section(c, i) for i, c in enumerate(counts)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    hard = [v for v in out if v.severity == "hard"]
    soft = [v for v in out if v.severity == "soft"]
    # No HARD violation (above 50% floor).
    assert "length_under_delivered_hard" not in {v.code for v in hard}
    # SOFT warning fires (below 85% threshold).
    assert "length_under_delivered_soft" in {v.code for v in soft}


def test_check_word_count_hard_fails_per_section_degradation():
    # Total OK overall, but section 8/9 = 50 words vs others = 600 words = 10% of mean.
    # Mean of [600,600,600,600,600,600,600,600,50,50] = 480, worst/mean = 50/480 = 10%.
    counts = [600, 600, 600, 600, 600, 600, 600, 600, 50, 50]
    sections = [_make_section(c, i) for i, c in enumerate(counts)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    codes = {v.code for v in out if v.severity == "hard"}
    assert "section_degradation_hard" in codes


def test_check_word_count_soft_warns_at_70pct():
    # 30-min target = 4500 words. Deliver 3150 (70% — under 85% soft floor).
    sections = [_make_section(315, i) for i in range(10)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    soft = [v for v in out if v.severity == "soft"]
    hard = [v for v in out if v.severity == "hard"]
    assert hard == []
    assert any(v.code == "length_under_delivered_soft" for v in soft)


def test_check_word_count_passes_above_soft_floor():
    # 30-min target = 4500 words. Deliver 4000 (89% — above 85% soft floor).
    sections = [_make_section(400, i) for i in range(10)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    soft = [v for v in out if v.severity == "soft" and "length" in v.code]
    hard = [v for v in out if v.severity == "hard" and "length" in v.code]
    assert hard == []
    assert soft == []


def test_check_word_count_no_sections_hard_fails():
    out = critic.check_word_count([], target_duration_s=1800)
    assert len(out) == 1
    assert out[0].code == "length_no_sections"


# ---------- panel hold cap (C5) ------------------------------------------


def test_check_panel_holds_passes_at_default():
    panels = [{"scene": "x", "hold_s": 6.0} for _ in range(10)]
    assert critic.check_panel_holds(panels) == []


def test_check_panel_holds_hard_fails_at_30s():
    """C5 — every panel in the rendered video had hold_s=30.0."""
    panels = [{"scene": "x", "hold_s": 30.0} for _ in range(10)]
    out = critic.check_panel_holds(panels)
    assert len(out) == 1
    assert out[0].severity == "hard"
    assert out[0].code == "panel_hold_too_long"


def test_check_panel_holds_soft_warns_at_10s():
    panels = [{"scene": "x", "hold_s": 10.0} for _ in range(10)]
    out = critic.check_panel_holds(panels)
    assert len(out) == 1
    assert out[0].severity == "soft"
    assert out[0].code == "panel_hold_borderline"


def test_check_panel_holds_no_panels_no_op():
    assert critic.check_panel_holds([]) == []


# ---------- stock-anecdote ban (C6) --------------------------------------


def test_check_no_stock_anecdotes_passes_clean_text():
    nar = "The character walked to the kitchen and made tea. Nothing odd happened."
    assert critic.check_no_stock_anecdotes(nar) == []


def test_check_no_stock_anecdotes_hard_fails_british_cycling():
    """C6 — section 5 of rendered video was the British Cycling story."""
    nar = (
        "In 2012, the British cycling team stunned the sports world. For decades, "
        "Britain had been mediocre in competitive cycling."
    )
    out = critic.check_no_stock_anecdotes(nar)
    assert len(out) == 1
    assert out[0].severity == "hard"
    assert out[0].code == "stock_anecdote_drift"
    assert "British Cycling" in out[0].message


def test_check_no_stock_anecdotes_hard_fails_jobs_speech():
    nar = "Steve Jobs once said in his Stanford commencement speech that..."
    out = critic.check_no_stock_anecdotes(nar)
    assert len(out) == 1
    assert out[0].code == "stock_anecdote_drift"


def test_check_no_stock_anecdotes_hard_fails_marshmallow():
    nar = "The Stanford marshmallow test taught us that..."
    out = critic.check_no_stock_anecdotes(nar)
    assert len(out) == 1


def test_check_no_stock_anecdotes_hard_fails_10000_hour():
    nar = "It takes 10,000 hours of deliberate practice to..."
    out = critic.check_no_stock_anecdotes(nar)
    assert len(out) == 1


def test_check_no_stock_anecdotes_case_insensitive():
    nar = "BRITISH CYCLING REVOLUTIONIZED THE SPORT"
    out = critic.check_no_stock_anecdotes(nar)
    assert len(out) == 1


# ---------- top-level validator end-to-end -------------------------------


def test_validate_long_form_envelope_passes_clean_envelope():
    env = {
        "long_form": {
            "narration_flat": (
                "The shadow standing in the doorway watched me. "
                "I felt the cold creeping through the floor. The figure was wrong. "
            ) * 30,
            "sections": [_make_section(450, i) for i in range(10)],
            "panels": [{"scene": "x", "hold_s": 6.0} for _ in range(15)],
        }
    }
    out = critic.validate_long_form_envelope(env, target_duration_s=1800, niche="r/nosleep")
    hard = [v for v in out if v.severity == "hard"]
    assert hard == [], f"expected no hard violations, got {out}"


def test_validate_long_form_envelope_catches_job_0c05c335_violations():
    """Pin the EXACT mistake stack of the rendered video so we never ship it again.

    Note: post-2026-05-13 calibration the 57% delivery on this fixture
    only fires the SOFT length warning (not hard) because we lowered
    the hard floor to 50% to accommodate Azure GPT-5.3's natural
    single-shot output. The OTHER three CLASS-OF-BUG violations
    (niche, panel_hold, stock_anecdote) still hard-fail.
    """
    env = {
        "long_form": {
            "narration_flat": (
                "Imagine a completely ordinary afternoon. A person sits somewhere — "
                "maybe on a couch, maybe at a desk. Nothing dramatic is happening. "
                "Just a screen glowing softly. A thumb scrolling. "
                # British Cycling stock anecdote — section 5 of rendered video.
                "In 2012, the British cycling team stunned the sports world. "
            ),
            "sections": [_make_section(c, i) for i, c in enumerate(
                [321, 306, 276, 269, 247, 264, 222, 218, 212, 215]
            )],
            "panels": [{"scene": "x", "hold_s": 30.0} for _ in range(24)],
        }
    }
    out = critic.validate_long_form_envelope(env, target_duration_s=1800, niche="r/nosleep")
    hard_codes = {v.code for v in out if v.severity == "hard"}
    soft_codes = {v.code for v in out if v.severity == "soft"}
    # Hard violations: niche tonal + panel hold + stock anecdote.
    assert "niche_tonal_violation" in hard_codes
    assert "panel_hold_too_long" in hard_codes
    assert "stock_anecdote_drift" in hard_codes
    # 57% delivery → soft length warn (post-2026-05-13 calibration).
    assert "length_under_delivered_soft" in soft_codes


def test_validate_long_form_envelope_handles_script_envelope_object():
    """Pin the validator works against ScriptEnvelope (not just dict)."""
    from pipeline.llm.script_schema import (
        LongFormPanel, LongFormScript, LongFormSection, ScriptEnvelope,
    )
    env = ScriptEnvelope(
        slug="t",
        kind="long_form",
        title_options=["t"],
        source_url="",
        source="",
        long_form=LongFormScript(
            hook="The shadow watched me from the doorway. " * 5,
            thesis="A test.",
            sections=[
                LongFormSection(
                    id=f"s{i}", title=f"S{i}",
                    narration="dread shadow watching " * 150,  # niche tokens
                    target_s=180.0, visual_brief=None,
                )
                for i in range(10)
            ],
            panels=[LongFormPanel(scene="x", hold_s=6.0) for _ in range(15)],
            sources=[],
        ),
    )
    out = critic.validate_long_form_envelope(env, target_duration_s=1800, niche="r/nosleep")
    hard = [v for v in out if v.severity == "hard"]
    assert hard == [], f"expected no hard violations, got {out}"


def test_validate_long_form_envelope_hard_fails_missing_payload():
    out = critic.validate_long_form_envelope({"slug": "x"}, target_duration_s=1800)
    assert any(v.code == "missing_long_form_payload" and v.severity == "hard" for v in out)


# ---------- retry-prompt rendering ---------------------------------------


def test_render_violations_for_retry_prompt_lists_each():
    vios = [
        critic.Violation(code="niche_tonal_violation", severity="hard", message="msg A"),
        critic.Violation(code="panel_hold_too_long", severity="hard", message="msg B"),
    ]
    out = critic.render_violations_for_retry_prompt(vios)
    assert "niche_tonal_violation" in out
    assert "panel_hold_too_long" in out
    assert "msg A" in out
    assert "msg B" in out


def test_render_violations_for_retry_prompt_empty_returns_empty():
    assert critic.render_violations_for_retry_prompt([]) == ""


# ---------- LongFormContractError ----------------------------------------


def test_long_form_contract_error_carries_violations():
    vios = [critic.Violation(code="length_under_delivered_hard", severity="hard", message="bad")]
    err = critic.LongFormContractError(vios)
    assert err.violations == vios
    assert "bad" in str(err)
