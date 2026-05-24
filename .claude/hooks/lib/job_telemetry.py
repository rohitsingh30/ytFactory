#!/usr/bin/env python3
"""Auto-fetch + summarize telemetry for any ytFactory job_id mentioned
in the user's prompt. Injected into context by state_digest.py.

The user 2026-05-24: "you need to deep dive into telemetry every time."
This module enforces that structurally — when a 32-char hex job_id is
present in the prompt, the most actionable telemetry signals
(`fallback_count` from refiner_io.json, terminal state, stage envelope
counts) are fetched and prepended to context BEFORE the agent generates
the response. Closes the gap where P30 (forced-skill-invocation Stop
hook) only fires on render-trigger turns, missing retrospective
"what happened with <job_id>" turns.

Cache: .claude/state/job_telemetry/<id>.json with 1h TTL. gsutil calls
are slow (~2s each); cache survives across turns within an hour, then
refreshes. A failed fetch is also cached briefly so repeated mentions
don't re-thrash the network.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

# 32-char hex match. Matches every ytFactory job_id (Firestore-style
# UUIDs without dashes). Be conservative — avoid matching git SHAs or
# random hex strings by also requiring a leading word boundary and a
# trailing character class that excludes word chars (so we don't match
# the middle of a longer hex string).
JOB_ID_RE = re.compile(r"\b([0-9a-f]{32})\b")

STATE_DIR = Path(
    os.environ.get("PROJECT_ROOT", "/Users/rohit/ytFactory")
) / ".claude" / "state"
CACHE_DIR = STATE_DIR / "job_telemetry"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTL_SECONDS = 3600  # 1 hour; jobs are immutable once terminal
GS_BUCKET = "gs://ytfactory-prod-v3-artifacts"


def find_job_ids(text: str) -> list[str]:
    """Return unique 32-char hex IDs in `text`, preserving first-seen
    order. Caps at 4 to avoid runaway fetches when a transcript is
    pasted in."""
    seen: list[str] = []
    if not text:
        return seen
    for m in JOB_ID_RE.finditer(text):
        v = m.group(1)
        if v not in seen:
            seen.append(v)
        if len(seen) >= 4:
            break
    return seen


def _cache_path(job_id: str) -> Path:
    return CACHE_DIR / f"{job_id}.json"


def _is_fresh(p: Path) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) < TTL_SECONDS
    except FileNotFoundError:
        return False


def _gsutil_cat(uri: str, timeout: int = 6) -> str | None:
    try:
        r = subprocess.run(
            ["gsutil", "-q", "cat", uri],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        return r.stdout
    except Exception:
        return None


def _gsutil_ls(uri: str, timeout: int = 6) -> list[str]:
    try:
        r = subprocess.run(
            ["gsutil", "-q", "ls", uri],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return []
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def fetch_telemetry(job_id: str) -> dict:
    """Fetch the actionable telemetry surface for a job. Returns a dict
    safe to JSON-serialize. Caches the result for 1h."""
    cache = _cache_path(job_id)
    if _is_fresh(cache):
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    out: dict = {"job_id": job_id, "fetched_at": int(time.time())}

    # GCS artifact tree
    base = f"{GS_BUCKET}/jobs/{job_id}/"
    listing = _gsutil_ls(base)
    out["artifact_present"] = bool(listing)
    if listing:
        subdirs = [
            line.rstrip("/").rsplit("/", 1)[-1]
            for line in listing if line.endswith("/")
        ]
        out["artifact_subdirs"] = subdirs

    # refiner_io.json — the highest-signal artifact for refiner failures
    raw = _gsutil_cat(f"{base}refiner_io/refiner_io.json")
    if raw:
        try:
            d = json.loads(raw)
            out["refiner"] = {
                "fallback_count": d.get("fallback_count"),
                "n_input_beats": len(d.get("input_batch", {}).get("beats", []) or []),
                "parsed_shape": (
                    "wrapper" if isinstance(d.get("parsed"), dict)
                    and "refined_beats" in d.get("parsed", {})
                    else "single-beat" if isinstance(d.get("parsed"), dict)
                    and {"refined_visual", "refined_scene"} <= set(d.get("parsed", {}).keys())
                    else "list" if isinstance(d.get("parsed"), list)
                    else "none/other"
                ),
            }
            if out["refiner"]["fallback_count"] is not None:
                fc = out["refiner"]["fallback_count"]
                nb = out["refiner"]["n_input_beats"] or 0
                if nb and fc == nb:
                    out["refiner"]["status"] = "ALL_FALLBACK"
                elif fc == 0:
                    out["refiner"]["status"] = "ALL_REFINED"
                elif fc > 0:
                    out["refiner"]["status"] = f"PARTIAL ({fc}/{nb} fell back)"
        except Exception as e:
            out["refiner"] = {"parse_error": str(e)[:200]}

    # Event-stream rollup. Cheap aggregate: how many stage.start vs
    # stage.failed events.
    try:
        r = subprocess.run(
            ["gcloud", "logging", "read",
             f'jsonPayload."ytfactory.job_id"="{job_id}"',
             "--project=ytfactory-prod-v3",
             "--limit=500", "--freshness=72h",
             "--format=value(jsonPayload.ytfactory.event)"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0:
            evs = [line.strip() for line in r.stdout.splitlines() if line.strip()]
            ev_counts: dict[str, int] = {}
            for e in evs:
                ev_counts[e] = ev_counts.get(e, 0) + 1
            out["events"] = {
                "total": len(evs),
                "starts": sum(1 for e in evs if e.endswith(".start")),
                "ends": sum(1 for e in evs if e.endswith(".end")),
                "failed": sum(1 for e in evs if e.endswith(".failed") or e.endswith(".error")),
                "by_type_top5": sorted(
                    ev_counts.items(), key=lambda kv: -kv[1]
                )[:5],
            }
    except Exception:
        pass

    try:
        cache.write_text(json.dumps(out))
    except Exception:
        pass
    return out


def format_block(t: dict) -> str:
    """Format a telemetry dict as a compact markdown block for injection."""
    lines: list[str] = []
    jid = t.get("job_id", "?")
    lines.append(f"**Job `{jid[:12]}…`** "
                 f"(`{GS_BUCKET}/jobs/{jid}/`)")
    if not t.get("artifact_present"):
        lines.append("  - artifact dir EMPTY or unreachable")
        return "\n".join(lines)
    r = t.get("refiner") or {}
    if "fallback_count" in r:
        fc = r["fallback_count"]
        nb = r.get("n_input_beats")
        status = r.get("status", "?")
        shape = r.get("parsed_shape", "?")
        lines.append(
            f"  - refiner_io.json: `fallback_count={fc}` "
            f"(of {nb} beats); shape=`{shape}`; status=**{status}**"
        )
    elif "parse_error" in r:
        lines.append(f"  - refiner_io.json: parse error — {r['parse_error']}")
    else:
        lines.append("  - refiner_io.json: not present")
    e = t.get("events") or {}
    if e:
        lines.append(
            f"  - events: {e.get('total','?')} total, "
            f"{e.get('starts','?')} starts / {e.get('ends','?')} ends / "
            f"**{e.get('failed','?')} failed**"
        )
        top = e.get("by_type_top5") or []
        if top:
            lines.append("    top: " + ", ".join(f"`{k}`×{v}" for k, v in top))
    return "\n".join(lines)


def telemetry_block_for_prompt(text: str) -> str:
    """Main entry point — called by state_digest.py. Returns either
    an empty string (no job_ids found) or a markdown block summarizing
    each job_id's most actionable telemetry."""
    jids = find_job_ids(text)
    if not jids:
        return ""
    parts: list[str] = [
        "## Telemetry snapshot — job_id(s) in your prompt",
        "",
        "Auto-fetched per 'deep-dive telemetry every time' rule. The "
        "agent has these numbers BEFORE generating; do not need to "
        "guess about fallback_count, terminal state, or stage health.",
        "",
    ]
    for jid in jids:
        t = fetch_telemetry(jid)
        parts.append(format_block(t))
        parts.append("")
    parts.append("---")
    parts.append("")
    return "\n".join(parts)


if __name__ == "__main__":
    import sys
    txt = sys.stdin.read() if not sys.stdin.isatty() else " ".join(sys.argv[1:])
    print(telemetry_block_for_prompt(txt))
