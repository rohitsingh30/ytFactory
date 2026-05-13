"""Eval-contract helper for the authoring-side of /Users/rohit/evals/.

Single source of truth for the agent-to-agent contract spec'd at
/Users/rohit/evals/AGENT_CONTRACT.md. Owns:

- Handoff writing (drops markdown into <project>/inbox/, never overwrites)
- Critique parsing (verdict / weakest param / fix instructions / scores)
- STATUS.md updates — AUTHORING-OWNED columns only
  (last_fix_attempted, last_fix_result). Refuses to touch reviewer columns
- Holds registry — per-channel <project>/_holds.json. Cron uploaders gate
  on `is_held(project, slug)` before picking the next pending mp4

Library + CLI in one module. Invoke as:
    python -m pipeline.quality.evals init <project>
    python -m pipeline.quality.evals ingest <project>
    python -m pipeline.quality.evals status <project>
    python -m pipeline.quality.evals hold <project> <slug> --reason "..."
    python -m pipeline.quality.evals unhold <project> <slug>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Audit D3.7 — pre-fix this hardcoded `/Users/rohit/evals` as the
# only fallback. Now: env override (preferred), else look for an
# `evals/` sibling of this repo, else the legacy laptop path.
def _default_evals_root() -> Path:
    """Discover the eval workspace via env, repo-sibling, then legacy."""
    env_val = os.environ.get("YTFACTORY_EVALS_ROOT")
    if env_val:
        return Path(env_val)
    sibling = PROJECT_ROOT.parent / "evals"
    if sibling.exists():
        return sibling
    # Legacy default for the original laptop. Only hit on machines
    # that have neither YTFACTORY_EVALS_ROOT nor a repo-sibling
    # `evals/` dir — rare in practice but kept for back-compat.
    return Path("/Users/rohit/evals")

EVALS_ROOT = _default_evals_root()

# Audit D3.10 — scrollpulse was listed despite its render-config YAML
# (pipeline/channels/scrollpulse.yaml) not existing on disk; the channel
# is held out of rotation (in_rotation: false in pipeline/channels.yaml,
# per audit T1.1). Removed here too so the eval surface stays in lockstep
# with the active production channel set.
KNOWN_PROJECTS = ("mystoriesanimated", "cosmosdecoded", "historyrecapped",
                  "hindutavaanimated", "sportsrecapped", "rhymetimejunction")

# Verdict thresholds per /Users/rohit/evals/SCHEMA.md.
VERDICT_SHIP = "SHIP"
VERDICT_FIX = "FIX"
VERDICT_BLOCK = "BLOCK"

# Critical-block params — any of these <5 forces hold even on otherwise
# SHIP-level avg. Source: SCHEMA.md `### Verdict thresholds`.
CRITICAL_PARAMS = {
    "clipping_audible",
    "on_screen_text_correctness",
    "character_lock",
    "asset_topicality",
}

STATUS_HEADER = (
    "| video_slug | first_critique | verdict | weakest_param "
    "| last_fix_attempted | last_fix_result | re_critique | final_status |"
)
STATUS_DIVIDER = "|---|---|---|---|---|---|---|---|"


# ──────────────────────────────────────────────────────────────────────
# paths

def project_dir(project: str) -> Path:
    return EVALS_ROOT / project

def inbox_dir(project: str) -> Path:
    return project_dir(project) / "inbox"

def critiques_dir(project: str) -> Path:
    return project_dir(project) / "critiques"

def status_path(project: str) -> Path:
    return project_dir(project) / "STATUS.md"

def holds_path(project: str) -> Path:
    """Holds live INSIDE the channel root in ytFactory, not in evals/.
    They are an authoring-side concept (we're stopping our own uploader)
    and the cron uploader needs them locally.
    """
    return PROJECT_ROOT / project / "_holds.json"


# ──────────────────────────────────────────────────────────────────────
# bootstrap

def init_project(project: str) -> None:
    """Create inbox/ + critiques/ + STATUS.md skeleton if missing."""
    pdir = project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    inbox_dir(project).mkdir(exist_ok=True)
    critiques_dir(project).mkdir(exist_ok=True)
    sp = status_path(project)
    if not sp.exists():
        sp.write_text(
            f"# {project} — progress ledger\n\n"
            "Shared between the authoring agent (writes columns: "
            "`last_fix_attempted`, `last_fix_result`) and the judge-video "
            "reviewer (writes columns: `first_critique`, `verdict`, "
            "`weakest_param`, `re_critique`, `final_status`). "
            "See `/Users/rohit/evals/AGENT_CONTRACT.md` for column contract.\n\n"
            f"{STATUS_HEADER}\n{STATUS_DIVIDER}\n\n"
            "## Cross-cutting issues (pipeline-level)\n\n"
            "(none yet)\n"
        )


# ──────────────────────────────────────────────────────────────────────
# handoff

def handoff_path(project: str, batch_slug: str, date: dt.date | None = None) -> Path:
    """Return path WITH `-vN` suffix if a handoff for that date+slug exists."""
    date = date or dt.date.today()
    base = inbox_dir(project) / f"{date.isoformat()}-{batch_slug}.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = inbox_dir(project) / f"{date.isoformat()}-{batch_slug}-v{n}.md"
        if not candidate.exists():
            return candidate
        n += 1

def write_handoff(project: str, batch_slug: str, content_md: str,
                  date: dt.date | None = None,
                  track_slugs: Iterable[str] | None = None) -> Path:
    """Drop a handoff. Refuses to overwrite — auto-suffixes -v2 etc.

    `track_slugs` (the slugs in this batch) are appended to STATUS.md as
    new rows so they're tracked from the moment they go for review.
    """
    init_project(project)
    p = handoff_path(project, batch_slug, date)
    p.write_text(content_md)
    if track_slugs:
        for slug in track_slugs:
            ensure_status_row(project, slug)
    return p


# ──────────────────────────────────────────────────────────────────────
# critique parsing

@dataclass
class Critique:
    path: Path
    slug: str
    date: str | None
    verdict: str | None       # SHIP / FIX / BLOCK
    total: int | None         # X / 500
    avg: float | None         # X.X / 10
    weakest_param: str | None
    weakest_score: int | None
    critical_failures: list[tuple[str, int]]   # (param, score) where param ∈ CRITICAL_PARAMS and score < 5
    fix_instructions: str | None
    gut_check: str | None     # YES / MAYBE / NO

_RE_VERDICT = re.compile(r"^\*\*(SHIP|FIX|BLOCK)\*\*", re.M)
_RE_TOTAL_AVG = re.compile(
    r"total\s+\*\*([0-9]+)\s*/\s*500\*\*.*?avg\s+\*\*([0-9.]+)\s*/\s*10\*\*",
    re.IGNORECASE | re.DOTALL,
)
_RE_RUBRIC_ROW = re.compile(
    r"^\|\s*([a-z_][a-z0-9_]*)\s*\|\s*(\d{1,2})\s*\|", re.M | re.IGNORECASE
)
_RE_DATE = re.compile(r"^\*\*Date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.M)
_RE_GUT = re.compile(r"^\s*(YES|MAYBE|NO)\b", re.M | re.IGNORECASE)

def parse_critique(path: Path) -> Critique:
    text = path.read_text()
    slug = path.stem.replace("_critique", "")

    m_v = _RE_VERDICT.search(text)
    verdict = m_v.group(1) if m_v else None

    m_ta = _RE_TOTAL_AVG.search(text)
    total = int(m_ta.group(1)) if m_ta else None
    avg = float(m_ta.group(2)) if m_ta else None

    m_d = _RE_DATE.search(text)
    date = m_d.group(1) if m_d else None

    # All "| param | score | ..." rows. Find min.
    rows = [(p.lower(), int(s)) for p, s in _RE_RUBRIC_ROW.findall(text)]
    weakest_param = None
    weakest_score = None
    if rows:
        weakest_param, weakest_score = min(rows, key=lambda r: r[1])

    critical_failures = [(p, s) for p, s in rows
                          if p in CRITICAL_PARAMS and s < 5]

    # Specific fix instructions section (only present if Verdict=FIX).
    fix_instructions = None
    fix_match = re.search(
        r"##\s+Specific fix instructions\s*\n(.+?)(?=\n##\s+|\Z)",
        text, re.S,
    )
    if fix_match:
        fix_instructions = fix_match.group(1).strip()

    # Single-question gut check
    gut_section = re.search(
        r"##\s+Single-question gut check\s*\n(.+?)(?=\n##\s+|\Z)",
        text, re.S,
    )
    gut_check = None
    if gut_section:
        m_g = _RE_GUT.search(gut_section.group(1))
        if m_g:
            gut_check = m_g.group(1).upper()

    return Critique(
        path=path, slug=slug, date=date,
        verdict=verdict, total=total, avg=avg,
        weakest_param=weakest_param, weakest_score=weakest_score,
        critical_failures=critical_failures,
        fix_instructions=fix_instructions, gut_check=gut_check,
    )

def list_critiques(project: str) -> list[Critique]:
    cdir = critiques_dir(project)
    if not cdir.exists():
        return []
    return [parse_critique(p) for p in sorted(cdir.glob("*_critique.md"))]


# ──────────────────────────────────────────────────────────────────────
# STATUS.md table read/write

@dataclass
class StatusRow:
    slug: str
    first_critique: str
    verdict: str
    weakest_param: str
    last_fix_attempted: str
    last_fix_result: str
    re_critique: str
    final_status: str

    def to_md(self) -> str:
        cols = [self.slug, self.first_critique, self.verdict,
                self.weakest_param, self.last_fix_attempted,
                self.last_fix_result, self.re_critique, self.final_status]
        return "| " + " | ".join(c if c else "—" for c in cols) + " |"

def _split_status(text: str) -> tuple[list[str], int, list[str], list[str], int]:
    """Return (lines, table_start_idx, header_rows, body_rows, table_end_idx).

    `header_rows` = the 2 lines (header + divider).
    `body_rows` = data rows.
    `table_end_idx` = index in `lines` AFTER the last body row.
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("| video_slug "):
            start = i
            break
    if start is None:
        raise RuntimeError("STATUS.md missing table header — run init_project")
    header_rows = [lines[start], lines[start + 1]]
    body: list[str] = []
    j = start + 2
    while j < len(lines) and lines[j].strip().startswith("|"):
        body.append(lines[j])
        j += 1
    return lines, start, header_rows, body, j

def _row_from_line(ln: str) -> StatusRow | None:
    parts = [p.strip() for p in ln.strip().strip("|").split("|")]
    if len(parts) != 8:
        return None
    parts = [p if p != "—" else "" for p in parts]
    return StatusRow(*parts)

def read_status(project: str) -> list[StatusRow]:
    sp = status_path(project)
    if not sp.exists():
        return []
    _, _, _, body, _ = _split_status(sp.read_text())
    out = []
    for ln in body:
        r = _row_from_line(ln)
        if r:
            out.append(r)
    return out

def _write_status_rows(project: str, rows: list[StatusRow]) -> None:
    sp = status_path(project)
    text = sp.read_text()
    lines, start, header_rows, _, end = _split_status(text)
    new_block = header_rows + [r.to_md() for r in rows]
    out = lines[:start] + new_block + lines[end:]
    sp.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""))

def ensure_status_row(project: str, slug: str) -> StatusRow:
    """Add a default row for `slug` if missing. Does not modify existing."""
    init_project(project)
    rows = read_status(project)
    for r in rows:
        if r.slug == slug:
            return r
    new_row = StatusRow(
        slug=slug, first_critique="", verdict="", weakest_param="",
        last_fix_attempted="none", last_fix_result="pending",
        re_critique="", final_status="IN-LOOP",
    )
    rows.append(new_row)
    _write_status_rows(project, rows)
    return new_row

# Authoring-owned column names; reviewer-owned columns excluded.
AUTHORING_COLUMNS = {"last_fix_attempted", "last_fix_result"}

def update_status_authoring(project: str, slug: str, *,
                            last_fix_attempted: str | None = None,
                            last_fix_result: str | None = None) -> StatusRow:
    """Update authoring-owned columns only. Refuses to touch reviewer columns.

    Adds the row if missing.
    """
    rows = read_status(project)
    found = False
    for i, r in enumerate(rows):
        if r.slug != slug:
            continue
        found = True
        if last_fix_attempted is not None:
            r.last_fix_attempted = last_fix_attempted
        if last_fix_result is not None:
            r.last_fix_result = last_fix_result
        rows[i] = r
        break
    if not found:
        r = StatusRow(
            slug=slug, first_critique="", verdict="", weakest_param="",
            last_fix_attempted=last_fix_attempted or "none",
            last_fix_result=last_fix_result or "pending",
            re_critique="", final_status="IN-LOOP",
        )
        rows.append(r)
    _write_status_rows(project, rows)
    return next(r for r in rows if r.slug == slug)


# ──────────────────────────────────────────────────────────────────────
# holds registry — gates the cron uploader

@dataclass
class Hold:
    slug: str
    reason: str
    set_at: str          # ISO date
    source_critique: str # path

def _read_holds(project: str) -> dict[str, dict]:
    p = holds_path(project)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def _write_holds(project: str, holds: dict[str, dict]) -> None:
    p = holds_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(holds, indent=2, sort_keys=True))

def is_held(project: str, slug: str) -> bool:
    return slug in _read_holds(project)

def holds_for(project: str) -> list[Hold]:
    return [Hold(slug=k, **v) for k, v in _read_holds(project).items()]

def set_hold(project: str, slug: str, *, reason: str,
             source_critique: str | Path = "") -> None:
    holds = _read_holds(project)
    holds[slug] = {
        "reason": reason,
        "set_at": dt.date.today().isoformat(),
        "source_critique": str(source_critique),
    }
    _write_holds(project, holds)

def clear_hold(project: str, slug: str) -> bool:
    holds = _read_holds(project)
    if slug in holds:
        del holds[slug]
        _write_holds(project, holds)
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# routing — verdict + critical-failures → action

@dataclass
class Route:
    slug: str
    verdict: str | None
    avg: float | None
    weakest: str
    critical_failures: list[tuple[str, int]]
    action: str          # "ship" / "refix" / "block" / "untracked"
    held: bool
    fix_summary: str

def _action_for(c: Critique) -> str:
    if c.verdict == VERDICT_SHIP and not c.critical_failures:
        return "ship"
    if c.verdict == VERDICT_BLOCK:
        return "block"
    if c.verdict == VERDICT_FIX or c.critical_failures:
        return "refix"
    return "untracked"

def route(project: str) -> list[Route]:
    out: list[Route] = []
    for c in list_critiques(project):
        action = _action_for(c)
        weakest = (
            f"{c.weakest_param} ({c.weakest_score}/10)"
            if c.weakest_param else "—"
        )
        fix_summary = ""
        if c.fix_instructions:
            first = c.fix_instructions.splitlines()[0].strip()
            fix_summary = first[:120]
        out.append(Route(
            slug=c.slug, verdict=c.verdict, avg=c.avg, weakest=weakest,
            critical_failures=c.critical_failures, action=action,
            held=is_held(project, c.slug), fix_summary=fix_summary,
        ))
    return out


# ──────────────────────────────────────────────────────────────────────
# ingest — read all critiques, set holds, update STATUS.md authoring cols

def ingest(project: str, *, dry_run: bool = False) -> dict:
    """Read every critique, update STATUS authoring columns, set/clear holds.

    For verdict ∈ {FIX, BLOCK} OR any critical-block param < 5 → set hold.
    For verdict == SHIP with no critical failures → clear hold.

    Returns a summary dict suitable for printing.
    """
    init_project(project)
    routes = route(project)
    summary = {
        "project": project,
        "ship": [], "refix": [], "block": [], "untracked": [],
        "holds_set": [], "holds_cleared": [],
    }
    for r in routes:
        summary[r.action].append({
            "slug": r.slug, "verdict": r.verdict, "avg": r.avg,
            "weakest": r.weakest,
            "critical_failures": r.critical_failures,
            "fix": r.fix_summary,
        })
        if dry_run:
            continue

        # Update STATUS.md authoring columns + ensure row exists.
        if r.action in ("refix", "block"):
            update_status_authoring(
                project, r.slug,
                last_fix_attempted="pending",
                last_fix_result="pending",
            )
            critique_file = critiques_dir(project) / f"{r.slug}_critique.md"
            reason = f"verdict={r.verdict}; weakest={r.weakest}"
            if r.critical_failures:
                reason += "; critical=" + ",".join(
                    f"{p}({s})" for p, s in r.critical_failures
                )
            if not is_held(project, r.slug):
                set_hold(project, r.slug, reason=reason,
                         source_critique=str(critique_file))
                summary["holds_set"].append(r.slug)
        elif r.action == "ship":
            if is_held(project, r.slug):
                clear_hold(project, r.slug)
                summary["holds_cleared"].append(r.slug)
            update_status_authoring(
                project, r.slug,
                last_fix_attempted=None,  # don't touch
                last_fix_result="success",
            )
    return summary


# ──────────────────────────────────────────────────────────────────────
# cross-cutting issues — pulled from STATUS.md narrative section

def cross_cutting(project: str) -> str | None:
    sp = status_path(project)
    if not sp.exists():
        return None
    text = sp.read_text()
    m = re.search(
        r"##\s+Cross-cutting issues[^\n]*\n(.+?)(?=\n##\s+|\Z)",
        text, re.S,
    )
    if not m:
        return None
    body = m.group(1).strip()
    return body if body and body != "(none yet)" else None


# ──────────────────────────────────────────────────────────────────────
# CLI

def _cmd_init(args) -> int:
    init_project(args.project)
    print(f"initialised {project_dir(args.project)}")
    return 0

def _cmd_ingest(args) -> int:
    s = ingest(args.project, dry_run=args.dry_run)
    print(f"\n=== ingest summary for {s['project']} ===\n")
    for action in ("ship", "refix", "block", "untracked"):
        items = s[action]
        if not items:
            continue
        print(f"{action.upper()} ({len(items)})")
        for it in items:
            crit = ""
            if it["critical_failures"]:
                crit = " CRITICAL=" + ",".join(
                    f"{p}({s_})" for p, s_ in it["critical_failures"]
                )
            print(f"  {it['slug']}  verdict={it['verdict']} "
                  f"avg={it['avg']} weakest={it['weakest']}{crit}")
            if it["fix"]:
                print(f"    fix: {it['fix']}")
        print()
    if s["holds_set"]:
        print(f"HOLDS SET: {len(s['holds_set'])}")
        for sl in s["holds_set"]:
            print(f"  + {sl}")
    if s["holds_cleared"]:
        print(f"HOLDS CLEARED: {len(s['holds_cleared'])}")
        for sl in s["holds_cleared"]:
            print(f"  - {sl}")
    cc = cross_cutting(args.project)
    if cc:
        print("\n--- cross-cutting issues (pipeline-level) ---")
        print(cc)
    if args.dry_run:
        print("\n(DRY RUN — no STATUS.md / holds writes)")
    return 0

def _cmd_status(args) -> int:
    rows = read_status(args.project)
    if not rows:
        print(f"no rows in STATUS.md for {args.project}")
        return 0
    print(f"\n=== STATUS for {args.project} ===\n")
    print(f"{'slug':<60} {'verdict':<6} {'fix_state':<10} {'final':<10} held?")
    for r in rows:
        held = "🔒" if is_held(args.project, r.slug) else ""
        fix_state = f"{r.last_fix_attempted}/{r.last_fix_result}"
        print(f"{r.slug[:60]:<60} {r.verdict or '—':<6} "
              f"{fix_state[:10]:<10} {r.final_status or '—':<10} {held}")
    return 0

def _cmd_hold(args) -> int:
    set_hold(args.project, args.slug, reason=args.reason)
    print(f"held {args.project}/{args.slug}: {args.reason}")
    return 0

def _cmd_unhold(args) -> int:
    if clear_hold(args.project, args.slug):
        print(f"unheld {args.project}/{args.slug}")
        return 0
    print(f"{args.project}/{args.slug} was not held", file=sys.stderr)
    return 1

def _cmd_holds(args) -> int:
    h = holds_for(args.project)
    if not h:
        print(f"no holds for {args.project}")
        return 0
    print(f"holds for {args.project}:")
    for hold in h:
        print(f"  {hold.slug}  set={hold.set_at}  reason={hold.reason}")
    return 0

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.quality.evals")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="bootstrap inbox/ + critiques/ + STATUS.md")
    p.add_argument("project")
    p.set_defaults(fn=_cmd_init)

    p = sub.add_parser("ingest", help="parse critiques, update STATUS, set holds")
    p.add_argument("project")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("status", help="pretty-print STATUS.md table")
    p.add_argument("project")
    p.set_defaults(fn=_cmd_status)

    p = sub.add_parser("hold", help="manually hold a slug from upload")
    p.add_argument("project")
    p.add_argument("slug")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=_cmd_hold)

    p = sub.add_parser("unhold", help="release a previously-held slug")
    p.add_argument("project")
    p.add_argument("slug")
    p.set_defaults(fn=_cmd_unhold)

    p = sub.add_parser("holds", help="list held slugs")
    p.add_argument("project")
    p.set_defaults(fn=_cmd_holds)

    args = ap.parse_args(argv)
    return args.fn(args)

if __name__ == "__main__":
    sys.exit(main())
