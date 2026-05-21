#!/usr/bin/env python3
"""
lint_skill_md.py — verify every .claude/skills/*/SKILL.md file is loadable.

Two failure modes the Claude Skills loader rejects on session start:

1. YAML parse failure — most commonly because the `description:` field
   contains an unquoted `key: value` substring (e.g.
   `image_provider: cloudrun_z_image_turbo`) which the YAML parser
   interprets as a nested mapping. Fix: switch to folded block scalar
   form (`description: >-` on its own line, body indented).

2. `description` field exceeds 1024 chars. The loader silently rejects
   the SKILL.md outright and the slash command becomes unavailable for
   the rest of the session.

Both failure modes produce no application-level error — the only signal
is a one-line `✖ .claude/skills/<name>/SKILL.md: ...` warning at the
very top of the session log. This linter catches them BEFORE commit so
the next session loads cleanly.

Source-of-truth docs:

- `.claude/skills/make-skill/learnings/heuristics.md` § 30b (cap) + 30c
  (YAML safety)
- `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_skill_description_1024_cap.md`
- `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_skill_description_yaml_colon_safety.md`

Usage::

    python scripts/lint_skill_md.py             # lint all skills
    python scripts/lint_skill_md.py --fix-suggest  # also print fix hints

Exits non-zero if any SKILL.md fails. Suitable as a pre-commit hook
and for the `/update-docs` Section 8 commit step.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Tuple

try:
    import yaml  # PyYAML
except ImportError:
    print(
        "lint_skill_md.py: PyYAML is required — pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)

DESCRIPTION_CAP = 1024
SKILL_GLOB = ".claude/skills/*/SKILL.md"


def _split_frontmatter(text: str) -> str | None:
    """Return the YAML frontmatter block between the first two ``---`` lines.

    Skill files are expected to look like::

        ---
        name: foo
        description: ...
        ---

        # body

    Returns the raw frontmatter text (without the fence lines) or None
    if no fence pair is found.
    """
    if not text.lstrip().startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1]


def _check_one(path: Path) -> List[str]:
    """Run all checks on one SKILL.md. Returns a list of error messages.

    Empty list means clean.
    """
    errors: List[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"could not read file: {exc}"]

    fm_text = _split_frontmatter(text)
    if fm_text is None:
        errors.append("no YAML frontmatter found (missing `---` fences)")
        return errors

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        # Most common cause is an unquoted `key: value` inside the
        # description field (heuristic 30c). Surface a fix hint.
        errors.append(
            f"YAML parse failure: {exc}\n"
            f"  → If the description contains a `key: value` substring "
            f"(e.g. `image_provider: cloudrun_z_image_turbo`), switch to "
            f"folded block scalar form:\n"
            f"      description: >-\n"
            f"        <body indented 2 spaces>\n"
            f"  → See heuristic 30c."
        )
        return errors

    if not isinstance(meta, dict):
        errors.append(
            f"frontmatter did not parse to a mapping (got {type(meta).__name__})"
        )
        return errors

    name = meta.get("name")
    if not name:
        errors.append("missing required `name` field")
    elif name != path.parent.name:
        errors.append(
            f"`name: {name}` does not match directory name `{path.parent.name}`"
        )

    desc = meta.get("description")
    if not desc:
        errors.append("missing required `description` field")
    elif not isinstance(desc, str):
        errors.append(
            f"`description` is {type(desc).__name__}, not str"
        )
    else:
        # Heuristic 30b — hard cap at 1024 chars.
        if len(desc) > DESCRIPTION_CAP:
            errors.append(
                f"description is {len(desc)} chars (>{DESCRIPTION_CAP} cap) — "
                f"the loader will silently reject this SKILL.md.\n"
                f"  → Trim to ≤{DESCRIPTION_CAP} chars (target ~980 for "
                f"safety margin). Drop adjectives + parenthetical asides "
                f"first; never drop trigger keywords or sibling-route "
                f"pointers. See heuristic 30b."
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repo root (default: cwd)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only failures, not the per-file OK lines",
    )
    args = parser.parse_args()

    skill_files = sorted(args.root.glob(SKILL_GLOB))
    if not skill_files:
        print(
            f"lint_skill_md.py: no SKILL.md files matched {args.root}/{SKILL_GLOB}",
            file=sys.stderr,
        )
        return 1

    fail_count = 0
    results: List[Tuple[Path, List[str]]] = []
    for skill_path in skill_files:
        errs = _check_one(skill_path)
        results.append((skill_path, errs))
        if errs:
            fail_count += 1

    width = max(len(str(p.relative_to(args.root))) for p, _ in results)
    for path, errs in results:
        rel = path.relative_to(args.root)
        if errs:
            print(f"FAIL  {str(rel):<{width}}")
            for err in errs:
                for line in err.splitlines():
                    print(f"      {line}")
        elif not args.quiet:
            print(f"OK    {rel}")

    print()
    if fail_count:
        print(
            f"lint_skill_md.py: {fail_count} of {len(results)} SKILL.md "
            f"file(s) failed validation."
        )
        return 1
    print(f"lint_skill_md.py: all {len(results)} SKILL.md files OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
