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
