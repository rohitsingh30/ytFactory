"""Regression tests for ``_salvage_truncated_json``.

The 2026-05-13 silent-mp4 stack surfaced a separate failure mode: gpt-5.x
reasoning deployments sometimes "stop" mid-output (finish_reason="stop"
even though the JSON is incomplete). The salvager recovers what was
written so the validator can decide what to do, instead of raising an
opaque "could not parse JSON" error.

Job 7dca182d4f5f449b8902eb861e62a7f8 was the canary — Azure returned
hook + thesis fully + first section's id+title+then mid-key cut.
"""
from __future__ import annotations

import json

import pytest

from pipeline.llm.cli import _parse_inner_json, _salvage_truncated_json


# ---------- direct salvager unit tests ------------------------------------


def test_salvage_recovers_full_object_when_balanced():
    s = '{"a": 1, "b": "two"}'
    out = _salvage_truncated_json(s)
    # Already balanced — salvager returns None, main loop handles it.
    assert out is None


def test_salvage_recovers_truncation_at_key():
    """Job 7dca182d's exact failure mode — cut after `"narration"`
    (mid-key, no value, no comma)."""
    s = '''{
  "hook": "Three weeks ago I installed a new security camera.",
  "thesis": "When a camera reports motion in a non-existent room, the question is whether the camera is wrong or the apartment is.",
  "sections": [
    {
      "id": "installing-the-camera",
      "title": "The Camera Was Supposed to Be Simple",
      "narration"'''
    out = _salvage_truncated_json(s)
    assert isinstance(out, dict)
    assert out["hook"].startswith("Three weeks ago")
    assert out["thesis"].startswith("When a camera")
    assert isinstance(out["sections"], list)
    assert len(out["sections"]) == 1
    assert out["sections"][0]["id"] == "installing-the-camera"
    assert out["sections"][0]["title"] == "The Camera Was Supposed to Be Simple"
    # The truncated narration field is NOT present (better to drop than
    # to fabricate).
    assert "narration" not in out["sections"][0]


def test_salvage_recovers_truncation_mid_string():
    """Output cut mid-string — salvager closes the unterminated string
    AND drops the partial value to land at a valid prior key:value."""
    s = '''{
  "hook": "this hook is fine",
  "thesis": "this thesis was cut off mid-thoug'''
    out = _salvage_truncated_json(s)
    assert isinstance(out, dict)
    # We may end up with hook only (thesis was cut). That's fine —
    # validator catches "missing required field".
    assert "hook" in out


def test_salvage_recovers_truncation_in_array():
    """Sections array cut after element 2 with mid-key truncation —
    the salvager keeps the partial element with whatever fields landed
    fully (id), drops the truncated key (title) and pads the array close."""
    s = '''{
  "title": "A",
  "sections": [
    {"id": "s0", "title": "T0"},
    {"id": "s1", "title": "T1"},
    {"id": "s2", "title"'''
    out = _parse_inner_json(s)
    assert isinstance(out, dict)
    assert out["title"] == "A"
    assert isinstance(out["sections"], list)
    assert len(out["sections"]) == 3  # s0, s1, partial s2
    assert out["sections"][0]["id"] == "s0"
    assert out["sections"][1]["id"] == "s1"
    assert out["sections"][2]["id"] == "s2"
    # The truncated title field is dropped — better partial than fabricated.
    assert "title" not in out["sections"][2]


def test_salvage_returns_none_for_garbage():
    assert _salvage_truncated_json("not even close to json") is None
    assert _salvage_truncated_json("") is None


def test_salvage_returns_none_when_no_opener_found():
    assert _salvage_truncated_json("just plain text no brackets") is None


# ---------- end-to-end via _parse_inner_json -----------------------------


def test_parse_inner_json_uses_salvager_on_truncated_input():
    """Truncated JSON that fails json.loads + bracket-balance walk
    MUST fall through to the salvager and recover."""
    # Outer object truncated mid-section element, no complete inner blocks
    # ahead — salvager IS reached and returns the outer dict.
    s = '''{
  "hook": "real story here",
  "thesis": "real thesis here",
  "sections": [{"id": "intro", "title"'''
    out = _parse_inner_json(s)
    assert isinstance(out, dict)
    assert out["hook"] == "real story here"
    assert out["thesis"] == "real thesis here"
    assert isinstance(out["sections"], list)


def test_parse_inner_json_still_raises_on_unsalvageable():
    from pipeline.llm.cli import ClaudeCLIError
    with pytest.raises(ClaudeCLIError, match="could not parse JSON"):
        _parse_inner_json("totally unrelated text with no json structure")


def test_parse_inner_json_handles_real_canary_truncation():
    """Pin the EXACT bytes that came back from job 7dca182d."""
    s = '''{
  "hook": "Three weeks ago I installed a new security camera in my apartment. Nothing fancy. Just a small motion-detection camera pointed down the hallway outside my bedroom. The idea was simple: if anything moved at night, I'd get a notification on my phone.",
  "thesis": "When a home security camera repeatedly reports motion in a room that does not exist, the real question becomes whether the camera is wrong or the apartment is.",
  "sections": [
    {
      "id": "installing-the-camera",
      "title": "The Camera Was Supposed to Be Simple",
      "narration"'''
    out = _parse_inner_json(s)
    assert "hook" in out
    assert "thesis" in out
    assert "sections" in out
    assert len(out["sections"]) == 1
    # The validator will then surface "0 sections passed
    # check_word_count" so the worker can fail cleanly with an
    # actionable error, instead of "could not parse JSON".
