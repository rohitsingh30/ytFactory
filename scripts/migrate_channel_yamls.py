"""One-shot migration script — collapse channel YAMLs to the new
``defaults: { short: {...}, long: {...} }`` shape.

Pre-2026-05-14 each channel YAML had a flat top level for short
defaults plus zero-or-more nested blocks for long-form variants:

* ``long_form:``     — historyrecapped sleep mode + similar
* ``long_form_doc:`` — sportsrecapped tifo-style docs
* ``kathaa:``        — hindutavaanimated 50–70-min footage
* ``footage_only:``  — historyrecapped/cosmosdecoded shotlist mode
* ``sports_doc:``    — sports_doc kind (now folded into long+overlay_timeline)

Post-2026-05-14 the unified shape is:

.. code-block:: yaml

    # Channel-wide metadata (UI labels, branding, …) stays at top.
    name: SportsRecapped
    narrator_visual_mode: voice_only

    defaults:
      short:
        # Every spec field that can be a default lives here.
        # The new render engines read ONLY this block when kind=short.
        visual_mode: ai_beat_slideshow
        aspect_ratio: 9:16
        output_resolution: [1080, 1920]
        # ...
      long:
        visual_mode: longform_panels      # or archival_shotlist / overlay_timeline / footage_windows
        aspect_ratio: 16:9
        output_resolution: [1920, 1080]
        # ...

This script walks every YAML in:

* ``pipeline/channels/*.yaml``
* ``pipeline/variants/*/*.yaml``
* ``<channel>/config.yaml``     — per-channel render config

Old keys (``long_form:`` / ``long_form_doc:`` / ``kathaa:`` /
``footage_only:`` / ``sports_doc:`` and channel-top-level
``output_resolution`` / ``output_fps`` / ``aspect_ratio`` / ``visual_mode``
/ ``music_bed_default`` / ``captions_layout``) are MIGRATED into the
``defaults:`` block then DELETED. No backward-compat keys are left
behind — per the user's "force cutover, no transition period"
direction.

Comment preservation
--------------------

Channel YAMLs carry a lot of important documentation in comments
(why this voice was picked, what bug a setting was added to fix,
which audit closed which issue). ``yaml.safe_dump`` strips all
comments, which would destroy that documentation. So:

- If ``ruamel.yaml`` is installed (preserves comments + key order +
  flow style), use it.
- Otherwise fall back to ``yaml.safe_dump`` and PRINT A WARNING so
  the operator knows comments were dropped. The CI / dev path
  installs ruamel.yaml in the venv.

Usage
-----

Dry-run (default): print the proposed diff for each YAML, write nothing.

::

    .venv/bin/python scripts/migrate_channel_yamls.py

Apply (rewrites every file in place):

::

    .venv/bin/python scripts/migrate_channel_yamls.py --apply

Single-file:

::

    .venv/bin/python scripts/migrate_channel_yamls.py \
        --file pipeline/channels/sportsrecapped.yaml --apply

Idempotent: running twice is a no-op the second time.
"""
from __future__ import annotations

import argparse
import difflib
import io
import sys
from pathlib import Path
from typing import Any

import yaml

# ruamel.yaml preserves comments + key order + flow style. Fall back
# to PyYAML if not installed (with a loud warning — comments will be
# dropped).
try:
    from ruamel.yaml import YAML  # type: ignore
    _RUAMEL_OK = True

    def _ruamel_loader() -> "YAML":
        rt = YAML()
        rt.preserve_quotes = True
        rt.indent(mapping=2, sequence=4, offset=2)
        rt.width = 4096  # don't wrap long lines
        return rt
except ImportError:
    _RUAMEL_OK = False

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level keys that get LIFTED into `defaults.short:` (the channel's
# Shorts defaults — same shape that existing channels use at top level).
_SHORT_DEFAULTS_KEYS: set[str] = {
    "visual_mode",
    "aspect_ratio",
    "output_resolution",
    "output_fps",
    "tts_provider",
    "tts_voice",
    "tts_ref_text",
    "tts_speed",
    "tts_post_atempo",
    "tts_chunk_target_chars",
    "tts_chunk_join_silence_s",
    "voice_provider",
    "voice_id",
    "captions_layout",
    "captions_density",
    "captions_enabled",
    "music_bed_default",
    "music_bed",
    "music_policy",
    "image_provider",
    "image_seed",
    "image_steps",
    "motion_provider",
    "audio_mode",
    "duration_target_s",
    "duration_max_s",
    "render_mode",
    "critic_loop",
}

# Top-level keys that stay at the channel root (NOT moved into
# ``defaults:``) — these are channel-wide identity / branding /
# upload-policy things that don't vary by short vs long.
_CHANNEL_WIDE_KEYS: set[str] = {
    "name",
    "source_adapter",
    "narrator_visual_mode",
    "character_description",
    "image_style_prefix",
    "branding",
    "fiction_disclosure",
    "upload",
    "title_template",
    "description_template",
    "tags",
    "comments_persona",
    "comments_throttle",
    "fixed_first_comment",
    "comments_disabled",
    "music_dir",
    "footage_dir",
    "kathaa",  # special — stays for now; bigbang PR migrates it differently
    "long_form",
    "long_form_doc",
    "footage_only",
    "sports_doc",
    "defaults",
}

# Old per-kind blocks that get LIFTED into `defaults.long:`.
_LONG_BLOCK_NAMES = ("long_form", "long_form_doc", "kathaa",
                     "footage_only", "sports_doc")


def migrate_yaml_doc(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply the migration to a single parsed YAML doc.

    Returns ``(new_doc, notes)`` — ``notes`` is a per-doc summary of
    what got moved (empty list = the doc was already in the new shape
    or had nothing to migrate).

    Idempotent: a doc that already has ``defaults:`` at the root and no
    legacy keys flows through unchanged.
    """
    notes: list[str] = []

    # Idempotency: already-migrated docs carry ``defaults:`` AND don't
    # carry any of the old per-kind block names. Bail early.
    has_defaults = isinstance(doc.get("defaults"), dict)
    has_legacy_block = any(k in doc for k in _LONG_BLOCK_NAMES)
    has_top_level_short_keys = any(k in doc for k in _SHORT_DEFAULTS_KEYS)
    if has_defaults and not has_legacy_block and not has_top_level_short_keys:
        return doc, []  # already in new shape

    new_doc: dict[str, Any] = {}
    short_defaults: dict[str, Any] = {}
    long_defaults: dict[str, Any] = {}

    for key, value in list(doc.items()):
        if key in _SHORT_DEFAULTS_KEYS:
            short_defaults[key] = value
            notes.append(f"  {key!r} → defaults.short.{key}")
        elif key in _LONG_BLOCK_NAMES:
            continue
        else:
            new_doc[key] = value

    for block_name in _LONG_BLOCK_NAMES:
        block = doc.get(block_name)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            long_defaults[key] = value
            notes.append(f"  {block_name}.{key} → defaults.long.{key}")
        if block_name == "long_form_doc" or block_name == "sports_doc":
            long_defaults.setdefault("overlay_timeline", True)
            notes.append(f"  {block_name} → defaults.long.overlay_timeline=true")
        if block_name in {"kathaa", "footage_only"}:
            long_defaults.setdefault("visual_mode", "footage_windows")
            notes.append(f"  {block_name} → defaults.long.visual_mode=footage_windows")

    defaults: dict[str, Any] = {}
    if short_defaults:
        defaults["short"] = short_defaults
    if long_defaults:
        defaults["long"] = long_defaults
    if defaults:
        new_doc["defaults"] = defaults

    out: dict[str, Any] = {}
    preferred_order = [
        "name", "source_adapter", "narrator_visual_mode",
        "character_description", "image_style_prefix",
        "music_dir", "footage_dir",
        "branding",
    ]
    for k in preferred_order:
        if k in new_doc:
            out[k] = new_doc.pop(k)
    for k in list(new_doc.keys()):
        if k != "defaults":
            out[k] = new_doc[k]
    if "defaults" in new_doc:
        out["defaults"] = new_doc["defaults"]

    return out, notes


def _dump_yaml(doc: dict[str, Any]) -> str:
    """Serialize a doc back to YAML, preserving comments via ruamel
    when available. Falls back to PyYAML safe_dump (drops comments)
    with a printed warning."""
    if _RUAMEL_OK:
        rt = _ruamel_loader()
        buf = io.StringIO()
        rt.dump(doc, buf)
        return buf.getvalue()
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, indent=2)


def _load_yaml(text: str) -> dict[str, Any]:
    """Load YAML preserving comments via ruamel.yaml when available so
    the round-trip retains them."""
    if _RUAMEL_OK:
        rt = _ruamel_loader()
        result = rt.load(text)
        # ruamel returns CommentedMap; it acts like dict.
        return result if result is not None else {}
    return yaml.safe_load(text) or {}


def find_yamls(extra_files: list[Path] | None = None) -> list[Path]:
    out: list[Path] = []
    out.extend(sorted((REPO_ROOT / "pipeline" / "channels").glob("*.yaml")))
    variants_dir = REPO_ROOT / "pipeline" / "variants"
    if variants_dir.exists():
        out.extend(sorted(variants_dir.glob("**/*.yaml")))
    for candidate in REPO_ROOT.iterdir():
        if not candidate.is_dir():
            continue
        cfg = candidate / "config.yaml"
        if cfg.exists():
            out.append(cfg)
    if extra_files:
        out.extend(extra_files)
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            deduped.append(p)
    return deduped


def migrate_one_file(path: Path, *, apply: bool) -> tuple[bool, list[str]]:
    text = path.read_text()
    try:
        doc = _load_yaml(text)
    except Exception as exc:  # noqa: BLE001
        return False, [f"  YAML parse error — skipping: {exc}"]

    if not isinstance(doc, dict):
        return False, ["  not a YAML mapping — skipping"]

    new_doc, notes = migrate_yaml_doc(doc)
    if not notes:
        return False, ["  already in new shape — skipping"]

    new_text = _dump_yaml(new_doc)

    diff = list(difflib.unified_diff(
        text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path) + " (before)",
        tofile=str(path) + " (after)",
        n=2,
    ))

    if apply:
        path.write_text(new_text)
        notes.insert(0, f"  WROTE {path}")
    else:
        notes.insert(0, f"  --dry-run (use --apply to rewrite). {len(diff)} diff lines:")
        for line in diff[:12]:
            notes.append("    " + line.rstrip("\n"))
        if len(diff) > 12:
            notes.append(f"    … {len(diff) - 12} more lines")

    return True, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Rewrite files in place (default: dry-run)")
    ap.add_argument("--file", action="append", default=[],
                    type=Path, metavar="PATH",
                    help="Single file to migrate (repeatable). "
                         "Default: every channel + variant + per-channel "
                         "config.yaml under the repo root.")
    args = ap.parse_args(argv)

    if not _RUAMEL_OK:
        print("WARNING: ruamel.yaml not installed; comments will be DROPPED. "
              "Install via .venv/bin/pip install ruamel.yaml", file=sys.stderr)

    if args.file:
        targets = [Path(p) for p in args.file]
    else:
        targets = find_yamls()

    if not targets:
        print("no YAMLs found to migrate", file=sys.stderr)
        return 1

    print(f"migrate_channel_yamls: {len(targets)} file(s) "
          f"({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"comments-preserved={_RUAMEL_OK}")
    print()

    n_changed = 0
    for path in targets:
        rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
        print(f"[{rel}]")
        changed, notes = migrate_one_file(path, apply=args.apply)
        for n in notes:
            print(n)
        if changed:
            n_changed += 1
        print()

    print(f"{'rewrote' if args.apply else 'would rewrite'}: "
          f"{n_changed}/{len(targets)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Top-level keys that get LIFTED into `defaults.short:` (the channel's
# Shorts defaults — same shape that existing channels use at top level).
_SHORT_DEFAULTS_KEYS: set[str] = {
    "visual_mode",
    "aspect_ratio",
    "output_resolution",
    "output_fps",
    "tts_provider",
    "tts_voice",
    "tts_ref_text",
    "tts_speed",
    "tts_post_atempo",
    "tts_chunk_target_chars",
    "tts_chunk_join_silence_s",
    "voice_provider",
    "voice_id",
    "captions_layout",
    "captions_density",
    "captions_enabled",
    "music_bed_default",
    "music_bed",
    "music_policy",
    "image_provider",
    "image_seed",
    "image_steps",
    "motion_provider",
    "audio_mode",
    "duration_target_s",
    "duration_max_s",
    "render_mode",
    "critic_loop",
}

# Top-level keys that stay at the channel root (NOT moved into
# ``defaults:``) — these are channel-wide identity / branding /
# upload-policy things that don't vary by short vs long.
_CHANNEL_WIDE_KEYS: set[str] = {
    "name",
    "source_adapter",
    "narrator_visual_mode",
    "character_description",
    "image_style_prefix",
    "branding",
    "fiction_disclosure",
    "upload",
    "title_template",
    "description_template",
    "tags",
    "comments_persona",
    "comments_throttle",
    "fixed_first_comment",
    "comments_disabled",
    "music_dir",
    "footage_dir",
    "kathaa",  # special — stays for now; bigbang PR migrates it differently
    "long_form",
    "long_form_doc",
    "footage_only",
    "sports_doc",
    "defaults",
}

# Old per-kind blocks that get LIFTED into `defaults.long:` (nested
# under whichever name was in use). The migration honors:
# - ``long_form:`` keys → ``defaults.long.*``
# - ``long_form_doc:`` keys → ``defaults.long.*`` AND
#   ``defaults.long.overlay_timeline = true`` (sports_doc shape)
# - ``kathaa:`` keys → ``defaults.long.*`` AND
#   ``defaults.long.visual_mode = "footage_windows"``
# - ``footage_only:`` keys → ``defaults.long.*`` AND
#   ``defaults.long.visual_mode = "footage_windows"``
# - ``sports_doc:`` keys → ``defaults.long.*`` AND
#   ``defaults.long.overlay_timeline = true``
_LONG_BLOCK_NAMES = ("long_form", "long_form_doc", "kathaa",
                     "footage_only", "sports_doc")


def migrate_yaml_doc(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply the migration to a single parsed YAML doc.

    Returns ``(new_doc, notes)`` — ``notes`` is a per-doc summary of
    what got moved (empty list = the doc was already in the new shape
    or had nothing to migrate).

    Idempotent: a doc that already has ``defaults:`` at the root and no
    legacy keys flows through unchanged.
    """
    notes: list[str] = []

    # Idempotency: already-migrated docs carry ``defaults:`` AND don't
    # carry any of the old per-kind block names. Bail early.
    has_defaults = isinstance(doc.get("defaults"), dict)
    has_legacy_block = any(k in doc for k in _LONG_BLOCK_NAMES)
    has_top_level_short_keys = any(k in doc for k in _SHORT_DEFAULTS_KEYS)
    if has_defaults and not has_legacy_block and not has_top_level_short_keys:
        return doc, []  # already in new shape

    new_doc: dict[str, Any] = {}
    short_defaults: dict[str, Any] = {}
    long_defaults: dict[str, Any] = {}

    # Pass 1: lift top-level "short defaults" keys into defaults.short.
    for key, value in list(doc.items()):
        if key in _SHORT_DEFAULTS_KEYS:
            short_defaults[key] = value
            notes.append(f"  {key!r} → defaults.short.{key}")
        elif key in _LONG_BLOCK_NAMES:
            # Handled below.
            continue
        else:
            # Channel-wide key — stays at root.
            new_doc[key] = value

    # Pass 2: merge per-kind blocks into defaults.long, layered in
    # order so later blocks override earlier ones (matters for channels
    # that ship multiple long modes).
    for block_name in _LONG_BLOCK_NAMES:
        block = doc.get(block_name)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            long_defaults[key] = value
            notes.append(f"  {block_name}.{key} → defaults.long.{key}")
        # Block-name implies a visual_mode / overlay_timeline default
        # in the new shape.
        if block_name == "long_form_doc" or block_name == "sports_doc":
            long_defaults.setdefault("overlay_timeline", True)
            notes.append(f"  {block_name} → defaults.long.overlay_timeline=true")
        if block_name in {"kathaa", "footage_only"}:
            long_defaults.setdefault("visual_mode", "footage_windows")
            notes.append(f"  {block_name} → defaults.long.visual_mode=footage_windows")

    # Build the defaults block only if either side has content.
    defaults: dict[str, Any] = {}
    if short_defaults:
        defaults["short"] = short_defaults
    if long_defaults:
        defaults["long"] = long_defaults
    if defaults:
        new_doc["defaults"] = defaults

    # Order keys so the output is stable: name first, then known
    # channel-wide keys, then defaults. Pure cosmetic.
    out: dict[str, Any] = {}
    preferred_order = [
        "name", "source_adapter", "narrator_visual_mode",
        "character_description", "image_style_prefix",
        "music_dir", "footage_dir",
        "branding",
    ]
    for k in preferred_order:
        if k in new_doc:
            out[k] = new_doc.pop(k)
    # Then the rest of channel-wide keys in original order.
    for k in list(new_doc.keys()):
        if k != "defaults":
            out[k] = new_doc[k]
    if "defaults" in new_doc:
        out["defaults"] = new_doc["defaults"]

    return out, notes


def find_yamls(extra_files: list[Path] | None = None) -> list[Path]:
    """Find every channel + variant + per-channel-config YAML under the
    repo root. Used in the default (whole-tree) mode."""
    out: list[Path] = []
    out.extend(sorted((REPO_ROOT / "pipeline" / "channels").glob("*.yaml")))
    variants_dir = REPO_ROOT / "pipeline" / "variants"
    if variants_dir.exists():
        out.extend(sorted(variants_dir.glob("**/*.yaml")))
    # Per-channel <channel>/config.yaml files. Detect by looking at
    # known top-level dirs in REPO_ROOT.
    for candidate in REPO_ROOT.iterdir():
        if not candidate.is_dir():
            continue
        cfg = candidate / "config.yaml"
        if cfg.exists():
            out.append(cfg)
    if extra_files:
        out.extend(extra_files)
    # De-dupe while preserving order.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            deduped.append(p)
    return deduped


def migrate_one_file(path: Path, *, apply: bool) -> tuple[bool, list[str]]:
    """Migrate a single YAML file. Returns ``(changed, notes)``.

    When ``apply=False`` only prints the proposed diff; does not write.
    """
    text = path.read_text()
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return False, [f"  YAML parse error — skipping: {exc}"]

    if not isinstance(doc, dict):
        return False, ["  not a YAML mapping — skipping"]

    new_doc, notes = migrate_yaml_doc(doc)
    if not notes:
        return False, ["  already in new shape — skipping"]

    new_text = yaml.safe_dump(
        new_doc, sort_keys=False, default_flow_style=False, indent=2,
    )

    diff = list(difflib.unified_diff(
        text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path) + " (before)",
        tofile=str(path) + " (after)",
        n=2,
    ))

    if apply:
        path.write_text(new_text)
        notes.insert(0, f"  WROTE {path}")
    else:
        notes.insert(0, f"  --dry-run (use --apply to rewrite). {len(diff)} diff lines:")
        # Print first 12 lines of the diff for visibility.
        for line in diff[:12]:
            notes.append("    " + line.rstrip("\n"))
        if len(diff) > 12:
            notes.append(f"    … {len(diff) - 12} more lines")

    return True, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Rewrite files in place (default: dry-run)")
    ap.add_argument("--file", action="append", default=[],
                    type=Path, metavar="PATH",
                    help="Single file to migrate (repeatable). "
                         "Default: every channel + variant + per-channel "
                         "config.yaml under the repo root.")
    args = ap.parse_args(argv)

    if args.file:
        targets = [Path(p) for p in args.file]
    else:
        targets = find_yamls()

    if not targets:
        print("no YAMLs found to migrate", file=sys.stderr)
        return 1

    print(f"migrate_channel_yamls: {len(targets)} file(s) "
          f"({'APPLY' if args.apply else 'DRY-RUN'})")
    print()

    n_changed = 0
    for path in targets:
        rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
        print(f"[{rel}]")
        changed, notes = migrate_one_file(path, apply=args.apply)
        for n in notes:
            print(n)
        if changed:
            n_changed += 1
        print()

    print(f"{'rewrote' if args.apply else 'would rewrite'}: "
          f"{n_changed}/{len(targets)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
