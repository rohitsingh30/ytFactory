"""Regression — channel YAML's ``closer_format`` field must reach
``spec.extra`` so the shorts engine's closer_panel overlay activates.

History — until 2026-05-24, the YAML→spec.extra backfill list in
``cloud/render-worker-v2/entrypoint.py::_backfill_yaml_image_keys``
contained only image-related keys (``image_provider``,
``image_style_prefix``, ``image_seed``, ``image_steps``,
``force_positive``, ``default_scene_anchor``). The shorts engine's
closer_panel router at ``pipeline/render/short_engine.py:532`` reads
``spec.extra["closer_format"]``, so without the backfill the
channel + variant YAMLs declared ``closer_format`` but the value
never reached the render — every shorts render shipped without a
like/subscribe CTA.

Surfaced in preflight job 88d98126 — channel YAML
(``pipeline/channels/mystoriesanimated.yaml:123``) set
``closer_format: "LIKE if YTA, COMMENT if NTA. AITA?"`` and the
aita_animated variant YAML repeated it, but Firestore's render_spec
came back with ``spec.extra.closer_format = None``.

This test loads the backfill function and confirms ``closer_format``
is now in the iterated key list.
"""

from __future__ import annotations

from pathlib import Path
import re

# Import as plain text — the entrypoint module itself imports heavy
# cloud-only deps (FastAPI, OTLP, etc.) that we don't want pulled into
# pytest collection. Pin the contract via a source-level grep instead.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENTRYPOINT_PATH = (
    _REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"
)


def test_backfill_includes_closer_format() -> None:
    """The for-loop in _backfill_yaml_image_keys must list
    'closer_format' so the channel YAML's like/subscribe CTA reaches
    the shorts engine."""
    src = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
    # Find the function body.
    m = re.search(
        r"def _backfill_yaml_image_keys\([^)]*\)[^:]*:\s*\n"
        r"(?P<body>(?:.*\n)*?)"
        r"(?=\ndef |\nclass |\n@)",
        src,
    )
    assert m, (
        "Could not locate _backfill_yaml_image_keys() in "
        "cloud/render-worker-v2/entrypoint.py"
    )
    body = m.group("body")
    # The key list is a literal tuple of strings inside the for-loop.
    # Pin closer_format's presence.
    assert '"closer_format"' in body, (
        "_backfill_yaml_image_keys must list 'closer_format' in its "
        "for-loop key tuple. Without it, the channel + variant YAMLs' "
        "closer_format value never reaches spec.extra and the "
        "closer_panel overlay never activates. See preflight job "
        "88d98126 — the AITA shorts shipped without a like/subscribe "
        "CTA."
    )
    # Defense-in-depth: confirm the other already-shipped keys haven't
    # silently disappeared during the edit.
    for required_key in (
        '"image_provider"',
        '"image_style_prefix"',
        '"image_seed"',
        '"image_steps"',
        '"force_positive"',
        '"default_scene_anchor"',
        '"closer_format"',
    ):
        assert required_key in body, (
            f"backfill key {required_key} missing — previous coverage "
            f"regressed during this edit"
        )


def test_mystoriesanimated_yaml_declares_closer_format() -> None:
    """The channel YAML for mystoriesanimated must continue to declare
    closer_format so the backfill has something to forward. If the
    YAML ever drops this key we want a test failure, not silent
    no-CTA renders."""
    import yaml
    p = _REPO_ROOT / "pipeline" / "channels" / "mystoriesanimated.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg.get("closer_format"), (
        f"pipeline/channels/mystoriesanimated.yaml must declare a "
        f"non-empty closer_format. Got {cfg.get('closer_format')!r}."
    )


def test_aita_animated_variant_declares_closer_format() -> None:
    """The aita_animated variant overlay must also declare its
    closer_format (overrides the channel default for variant-specific
    CTA copy)."""
    import yaml
    p = (
        _REPO_ROOT
        / "pipeline"
        / "variants"
        / "mystoriesanimated"
        / "aita_animated.yaml"
    )
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg.get("closer_format"), (
        f"pipeline/variants/mystoriesanimated/aita_animated.yaml must "
        f"declare a non-empty closer_format. Got "
        f"{cfg.get('closer_format')!r}."
    )


# ---------------------------------------------------------------------
# Task #30 (2026-05-24): per-variant closer_format must reach spec.extra
# ---------------------------------------------------------------------
# Surfaced in the 88d98126 preflight aftermath: user pointed out
# "YTA for tifu, amzzing, are you stupid?" — meaning a TIFU short
# shipped with the AITA YTA/NTA closer despite tifu.yaml declaring
# its own override. Root cause: _backfill_yaml_image_keys loaded only
# the CHANNEL YAML; the variant overlay was never read for the backfill
# scan. Fix threads spec.source_variant_yaml as a second YAML overlay.


def test_backfill_accepts_variant_yaml_path() -> None:
    """The fix signature must take ``variant_yaml_path``. Source-level
    pin since the entrypoint module imports cloud-only deps."""
    src = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"def _backfill_yaml_image_keys\(\s*\n"
        r"(?P<sig>(?:\s+[^\n]+\n)+?)"
        r"\)\s*->\s*dict:",
        src,
    )
    assert m, "could not locate _backfill_yaml_image_keys() signature"
    sig = m.group("sig")
    assert "variant_yaml_path" in sig, (
        "fix regressed: _backfill_yaml_image_keys must accept a "
        "variant_yaml_path parameter so the variant overlay can shadow "
        "the channel default for closer_format. Pre-fix the variant "
        "YAML was completely ignored by this backfill and every TIFU "
        "render shipped with the AITA YTA/NTA closer."
    )


def test_backfill_loads_variant_yaml_in_body() -> None:
    """Source-level pin: the body must read the variant YAML and merge
    it on top of the channel cfg before scanning the backfill key list.
    Without the merge, ``variant_yaml_path`` would be ignored and the
    fix would be cosmetic."""
    src = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"def _backfill_yaml_image_keys\([^)]*?\)[^:]*:\s*\n"
        r"(?P<body>(?:.*\n)*?)"
        r"(?=\ndef |\nclass |\n@)",
        src,
    )
    assert m, "could not locate _backfill_yaml_image_keys() body"
    body = m.group("body")
    assert "variant_yaml_path" in body and "cfg.update" in body, (
        "fix regressed: the body must read the variant YAML and call "
        "cfg.update(v_cfg) so variant keys shadow channel keys before "
        "the backfill scan. Without cfg.update, the variant overlay is "
        "read but never applied — TIFU still gets the AITA closer."
    )


def test_call_site_threads_source_variant_yaml() -> None:
    """The call site at _main_from_firestore must pass
    ``spec.source_variant_yaml`` into the backfill. Without this hop,
    even a correct backfill function would never see the variant
    overlay."""
    src = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
    # Find every call to _backfill_yaml_image_keys.
    calls = re.findall(
        r"_backfill_yaml_image_keys\([^)]*\)", src,
    )
    assert calls, "expected at least one call site for _backfill_yaml_image_keys"
    threaded = [c for c in calls if "variant_yaml" in c]
    assert threaded, (
        "fix regressed: every call to _backfill_yaml_image_keys must "
        "thread the variant YAML path (typically spec.source_variant_yaml) "
        f"so the override actually reaches spec.extra. Calls found: {calls}"
    )


def test_tifu_variant_closer_differs_from_channel_default() -> None:
    """Sanity: the whole point of task #30 is that tifu.yaml's
    closer is DIFFERENT from the channel default. If they ever
    converge, the backfill fix is meaningless and a future edit
    might silently roll back the override."""
    import yaml
    chan = yaml.safe_load(
        (_REPO_ROOT / "pipeline" / "channels" / "mystoriesanimated.yaml")
        .read_text(encoding="utf-8")
    )
    tifu = yaml.safe_load(
        (_REPO_ROOT / "pipeline" / "variants" / "mystoriesanimated" / "tifu.yaml")
        .read_text(encoding="utf-8")
    )
    assert chan.get("closer_format") != tifu.get("closer_format"), (
        "tifu.yaml's closer_format must differ from the channel "
        "default — that's the whole reason task #30 exists. Channel: "
        f"{chan.get('closer_format')!r}, TIFU: {tifu.get('closer_format')!r}"
    )
    # AITA-shape guard: the channel default is AITA-specific; TIFU's
    # override must NOT contain YTA/NTA tokens.
    tifu_closer = (tifu.get("closer_format") or "").upper()
    assert "YTA" not in tifu_closer and "NTA" not in tifu_closer, (
        f"tifu.yaml's closer must not contain AITA tokens. "
        f"Got: {tifu.get('closer_format')!r}"
    )


def _assert_non_aita_subscribe_closer(closer: str, variant: str) -> None:
    """Shared assertions for non-AITA-verdict variant closers.

    User rule 2026-05-24: every script narration already ends with a
    comment-prompting question. So the closer panel must:
      - keep LIKE (cheap engagement, fits any non-verdict story)
      - keep / add SUBSCRIBE (channel growth ask)
      - NOT contain COMMENT (would duplicate the narration's question)
      - NOT contain AITA / YTA / NTA tokens (those are AITA-only)
    """
    assert closer, (
        f"{variant} must declare a non-empty closer_format. Without it, "
        f"the backfill inherits the channel-default AITA closer. "
        f"Got: {closer!r}"
    )
    upper = closer.upper()
    assert "YTA" not in upper and "NTA" not in upper and "AITA" not in upper, (
        f"{variant} closer must not contain AITA tokens (those are "
        f"AITA-variant-only). Got: {closer!r}"
    )
    assert "LIKE" in upper, (
        f"{variant} closer must keep LIKE — cheap engagement that "
        f"fits any non-verdict story. User correction 2026-05-24. "
        f"Got: {closer!r}"
    )
    assert "SUBSCRIBE" in upper or "FOLLOW" in upper, (
        f"{variant} closer must include SUBSCRIBE — narration already "
        f"asks for comments, so the closer panel should drive the "
        f"channel growth ask. Got: {closer!r}"
    )
    assert "COMMENT" not in upper, (
        f"{variant} closer must NOT include COMMENT — every script "
        f"already ends with a comment-prompting question, so adding "
        f"COMMENT to the closer panel duplicates the script's ask. "
        f"User rule 2026-05-24. Got: {closer!r}"
    )
    # User rule 2026-05-24 (after the research-sourced closer
    # rewrite): each clause must be a complete predicate, and the
    # whole closer must end with terminal punctuation ('.', '!', or
    # '?'). Fragments like "LIKE if you learned, SUBSCRIBE for more
    # history" read as chips, not sentences.
    stripped = closer.rstrip()
    assert stripped.endswith((".", "!", "?")), (
        f"{variant} closer must end with terminal punctuation "
        f"('.', '!', or '?') — viewers read it as a sentence, not "
        f"a chip-list. User rule 2026-05-24 after the closer-CTA "
        f"research pass. Got: {closer!r}"
    )
    # Both clauses must contain a verb-ish token (LIKE / SUBSCRIBE /
    # FOLLOW already enforced above) plus enough content past the
    # action verb that the clause isn't just the verb alone.
    # "SUBSCRIBE" alone = fragment; "SUBSCRIBE for X" = predicated.
    clauses = [c.strip() for c in closer.split(",") if c.strip()]
    assert len(clauses) >= 2, (
        f"{variant} closer must split on comma into >=2 clauses for "
        f"the 2-row panel. Got {len(clauses)} clause(s): {closer!r}"
    )
    for i, c in enumerate(clauses):
        # A fully-predicated clause has at least 4 words past the
        # action verb. "LIKE if X" alone is 3 words and reads thin;
        # "LIKE if you learned something new" is 6 and reads
        # complete. Threshold of >=4 words total per clause caught
        # the regression the user flagged 2026-05-24.
        word_count = len(c.split())
        assert word_count >= 4, (
            f"{variant} closer clause {i+1} is too thin "
            f"({word_count} words) — reads as a fragment, not a "
            f"complete predicate. Aim for >=4 words per clause. "
            f"Got clause {i+1}: {c!r} (full closer: {closer!r})"
        )


def test_today_in_history_closer_shape() -> None:
    """Today-In-History is informational, not a moral dispute. Must
    use LIKE + SUBSCRIBE (no COMMENT, no AITA tokens) per the
    user's 2026-05-24 closer-shape rule."""
    import yaml
    p = (
        _REPO_ROOT / "pipeline" / "variants" / "mystoriesanimated"
        / "today_in_history.yaml"
    )
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    _assert_non_aita_subscribe_closer(
        cfg.get("closer_format") or "", "today_in_history.yaml"
    )


def test_wiki_oddities_closer_shape() -> None:
    """Wiki-Oddities is strange-but-true facts, not a moral dispute.
    Must use LIKE + SUBSCRIBE (no COMMENT, no AITA tokens) per the
    user's 2026-05-24 closer-shape rule."""
    import yaml
    p = (
        _REPO_ROOT / "pipeline" / "variants" / "mystoriesanimated"
        / "wiki_oddities.yaml"
    )
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    _assert_non_aita_subscribe_closer(
        cfg.get("closer_format") or "", "wiki_oddities.yaml"
    )
