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


def _make_section(words: int, idx: int = 0, target_words: int | None = None):
    # P3.2: per-section gate compares against section's OWN target_words
    # when present. Default = the actual word count so legacy tests that
    # don't care about per-section gates still pass; callers that DO want
    # to test the ±10% per-section band pass an explicit target_words.
    return {"id": f"sec-{idx}", "title": f"Section {idx}",
            "narration": "word " * words, "target_s": 180.0,
            "target_words": target_words if target_words is not None else words}


def test_check_word_count_passes_when_at_target():
    # 30-min target = 4500 words. Hit 4500 exactly across 10 sections.
    sections = [_make_section(450, i) for i in range(10)]
    assert critic.check_word_count(sections, target_duration_s=1800) == []


def test_check_word_count_hard_fails_outside_15pct_total_band():
    """P3.2 (Q59): total length outside ±15% of user's target → hard.

    Pre-2026-05-22 the floor was a loose 50% (asymmetric, low-only).
    Now: symmetric ±15%. 30-min target = 4500 → allowed [3825, 5175].
    Delivering 3000 words (67%) MUST hard-fail now.
    """
    # 30-min target = 4500 words. Deliver 3000 — below 85% threshold.
    counts = [300] * 10
    sections = [_make_section(c, i) for i, c in enumerate(counts)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    hard = [v for v in out if v.severity == "hard"]
    codes = {v.code for v in hard}
    assert "length_under_delivered_hard" in codes


def test_check_word_count_hard_fails_when_total_overshoots_15pct():
    """P3.2: symmetric — over-delivery beyond +15% also hard-fails."""
    # 30-min target = 4500 words. Deliver 5500 (122%) — above 115% ceiling.
    counts = [550] * 10
    sections = [_make_section(c, i) for i, c in enumerate(counts)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    hard = [v for v in out if v.severity == "hard"]
    codes = {v.code for v in hard}
    assert "length_over_delivered_hard" in codes


def test_check_word_count_severe_overshoot_fires_hard():
    """P3.2: severe overshoot fires the hard band (±15%). The soft
    band is wider than the hard and is mutually exclusive (via the
    ``elif`` branch in ``critic_long_form.check_word_count``) — only
    ONE severity fires at a given deviation, and hard wins when both
    bands are exceeded.
    """
    # 1800s @ 150wpm = 4500 target. Deliver 5900 words = 131%. Outside
    # both 115% (5175) and 130% (5850) ceilings — hard fires.
    sections = [_make_section(590, i) for i in range(10)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    hard_codes = {v.code for v in out if v.severity == "hard" and "length_over" in v.code}
    assert "length_over_delivered_hard" in hard_codes


def test_check_word_count_hard_fails_per_section_outside_10pct_band():
    """P3.2 (Q59): per-section ±10% gate. Section delivered 80 words
    against a 200-word target = 40% (outside ±10%) → hard."""
    sections = [_make_section(words=200, idx=i, target_words=200) for i in range(9)]
    # Section 9 hits its target band (180-220 = 200 ±10%) → no violation.
    # Replace section 9 with a low-count miss.
    sections.append(_make_section(words=80, idx=9, target_words=200))
    out = critic.check_word_count(sections, target_duration_s=1800)
    codes = {v.code for v in out if v.severity == "hard"}
    assert "section_degradation_hard" in codes


def test_check_word_count_per_section_in_band_passes():
    """P3.2: section delivered exactly its target_words passes the
    ±10% gate cleanly."""
    sections = [_make_section(words=450, idx=i, target_words=450) for i in range(10)]
    out = critic.check_word_count(sections, target_duration_s=1800)
    # Each section is on-target → no per-section hard violation.
    assert not any(v.code == "section_degradation_hard" for v in out)


def test_tolerance_env_defaults_are_15pct_total_and_10pct_section():
    """P3.2: pin the SHIPPED gate tolerances so a future tweak can't
    silently widen them back to the obsolete 2026-05-13 calibration
    (HARD_FLOOR_FRAC=0.50 = ±50% asymmetric-low-only, which was too
    loose AND symmetric-only on the low side per Q59).

    Gate philosophy (Q64): gates STAY. Tightening them is correct
    when the underlying machinery (STORM fan-out + iterative-extend
    retry) reliably hits the tighter band.
    """
    # Read fresh from os.environ — tests/conftest may have set a different
    # value; we want the SHIPPED defaults.
    import os
    total_tol = float(os.environ.get("YTFACTORY_LONG_FORM_TOTAL_TOL_FRAC", "0.15"))
    section_tol = float(os.environ.get("YTFACTORY_LONG_FORM_SECTION_TOL_FRAC", "0.10"))
    assert total_tol == 0.15, (
        f"YTFACTORY_LONG_FORM_TOTAL_TOL_FRAC default must be 0.15 (±15%); got {total_tol}. "
        "Widening this back to the pre-2026-05-22 50% floor would re-introduce "
        "the silent-short-render class of bug (Q59 / P3.2)."
    )
    assert section_tol == 0.10, (
        f"YTFACTORY_LONG_FORM_SECTION_TOL_FRAC default must be 0.10 (±10%); got {section_tol}."
    )


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

    Note: post-2026-05-22 P3.2 retightening the hard length band is
    ±15%, so this fixture's 57% delivery (2550 of 4500 expected words)
    fires the HARD length violation rather than the older SOFT warn.
    The other three CLASS-OF-BUG violations (niche, panel_hold,
    stock_anecdote) still hard-fail as before.
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
    # Hard violations: length under (57% < 85% floor), niche tonal,
    # panel hold, stock anecdote.
    assert "length_under_delivered_hard" in hard_codes
    assert "niche_tonal_violation" in hard_codes
    assert "panel_hold_too_long" in hard_codes
    assert "stock_anecdote_drift" in hard_codes


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


# ---------- C7: essay-drift opener (added 2026-05-14) -------------------
#
# Regression for the r/nosleep 'If you can see this' renders that
# shipped 16-23 minute meditation essays instead of the actual
# horror story. See pipeline_bug_catalogue_v2_2026-05-14.html.


def test_essay_drift_flags_imagine_ordinary_opener():
    narration = (
        "Imagine a completely ordinary afternoon. A person sits "
        "somewhere — maybe on a couch, maybe at a desk. The room is "
        "quiet. Nothing dramatic is happening. Just a screen glowing."
    )
    out = critic.check_no_essay_drift(narration)
    assert len(out) == 1
    assert out[0].code == "essay_drift_opener"
    assert out[0].severity == "soft"
    assert "imagine" in out[0].message.lower()


def test_essay_drift_flags_right_now_wherever_opener():
    narration = (
        "Right now, wherever you are, something small is happening. "
        "Maybe you're sitting on a couch, holding your phone."
    )
    out = critic.check_no_essay_drift(narration)
    assert len(out) == 1
    assert "right now" in out[0].message.lower()


def test_essay_drift_passes_real_narrative_opener():
    # Actual r/nosleep horror opener — should NOT trigger.
    narration = (
        "I moved into the Cedar Street apartment on March 3rd. It "
        "was a second-floor unit above a closed flower shop, with "
        "creaky stairs and a mailbox that leaned slightly to the left."
    )
    out = critic.check_no_essay_drift(narration)
    assert out == []


def test_essay_drift_only_checks_opener_not_mid_text():
    # Phrase appearing mid-narration should NOT trigger — opener-only signal.
    # Pad opener to push the phrase past 300 chars.
    prefix = "I moved into the Cedar Street apartment on March 3rd. " * 8
    narration = (
        prefix
        + "Then we sat down and I had to imagine a completely ordinary "
        + "evening, before the noise started. " * 3
    )
    # The phrase appears at chars 700+, well past the 300-char opener band.
    assert narration.lower().find("imagine") > 300, (
        f"test setup wrong: phrase at {narration.lower().find('imagine')} "
        f"is within opener band"
    )
    out = critic.check_no_essay_drift(narration)
    assert out == []


def test_essay_drift_empty_narration_returns_empty():
    assert critic.check_no_essay_drift("") == []
    assert critic.check_no_essay_drift(None) == []


# ---------- C8: source-fidelity check (added 2026-05-14) ----------------


def test_source_fidelity_flags_low_overlap():
    raw_body = (
        "I work as a junior project coordinator at Acme Software in "
        "Seattle. During the pandemic our team went fully remote and "
        "Zoom meetings became routine. On Tuesday March 15 my colleague "
        "Laura Chen vanished from a meeting. The recording shows her "
        "stand up, walk toward the door, and disappear off-camera. "
        "Police searched her apartment in Capitol Hill. No trace. "
        "Her laptop was still logged in showing the meeting feed."
    )
    # Generic narration with NO specific entities from the source.
    narration = (
        "Imagine a completely ordinary afternoon. The hum of an "
        "appliance. Light from a window. Words passing by faster than "
        "they can be remembered. This is the environment most of us "
        "live in every single day. We notice, then we don't notice. "
        "Then something feels off. Then nothing feels real anymore."
    )
    out = critic.check_source_fidelity(narration, raw_body)
    assert len(out) == 1
    assert out[0].code == "source_fidelity_low"
    assert out[0].severity == "soft"
    assert "%" in out[0].message  # mentions overlap percentage


def test_source_fidelity_passes_high_overlap():
    raw_body = (
        "I work as a junior project coordinator at Acme Software in "
        "Seattle. During the pandemic our team went fully remote and "
        "Zoom meetings became routine. On Tuesday March 15 my colleague "
        "Laura Chen vanished from a meeting. The recording shows her "
        "stand up, walk toward the door, and disappear off-camera. "
        "Police searched her apartment in Capitol Hill. No trace. "
        "Her laptop was still logged in showing the meeting feed."
    )
    # Faithful expansion that names the entities + scene from the source.
    narration = (
        "Laura Chen worked as a junior project coordinator for Acme "
        "Software, based in Seattle. The pandemic had pushed every "
        "team fully remote. By that Tuesday in March she was on a "
        "routine Zoom meeting. Mid-call, she stood up, walked toward "
        "the door, and disappeared off-camera. The recording captured "
        "the moment. Police searched her apartment in Capitol Hill "
        "but found no trace. Her laptop remained logged in to the "
        "meeting feed long after she vanished."
    )
    out = critic.check_source_fidelity(narration, raw_body)
    assert out == []


def test_source_fidelity_skipped_when_body_is_none():
    # LLM-only source (no reddit URL) — no body to compare against.
    narration = "Some narration."
    out = critic.check_source_fidelity(narration, None)
    assert out == []


def test_source_fidelity_skipped_when_body_too_sparse():
    # Source body too short to compute meaningful overlap.
    narration = "Some narration."
    short_body = "If you can see this, keep reading."
    out = critic.check_source_fidelity(narration, short_body)
    assert out == []


def test_source_fidelity_threshold_configurable():
    # 50% threshold with a body whose overlap is 45% → fail.
    raw_body = " ".join(f"entity{i:03d}" for i in range(40))
    narration = " ".join(f"entity{i:03d}" for i in range(18))  # 18/40 = 45%
    out = critic.check_source_fidelity(
        narration, raw_body, min_overlap_frac=0.50,
    )
    assert len(out) == 1
    assert out[0].code == "source_fidelity_low"

    # Same data with a 30% threshold → pass.
    out2 = critic.check_source_fidelity(
        narration, raw_body, min_overlap_frac=0.30,
    )
    assert out2 == []


def test_validate_envelope_includes_source_fidelity_when_body_provided():
    """End-to-end: validator picks up the new check when raw_body is passed."""
    # Use the legacy-dict envelope shape to keep the test light.
    env = {
        "narration": "Imagine a completely ordinary afternoon.",
        "sections": [{"narration": "Imagine a completely ordinary afternoon."}],
        "panels": [],
    }
    raw_body = " ".join(f"specifictoken{i}" for i in range(50))
    # Body has 50 unique anchors; narration has none → 0% overlap.
    out = critic.validate_long_form_envelope(
        env, target_duration_s=120, niche=None, raw_body=raw_body,
    )
    codes = {v.code for v in out}
    # Both essay-drift AND source-fidelity should fire.
    assert "essay_drift_opener" in codes
    assert "source_fidelity_low" in codes


def test_extract_anchor_words_empty_input():
    # Coverage for the early-return branch.
    assert critic._extract_anchor_words("") == set()
    assert critic._extract_anchor_words(None) == set()
