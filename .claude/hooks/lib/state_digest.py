#!/usr/bin/env python3
"""Produce a state digest to inject into context on UserPromptSubmit.

Stdout: a markdown-formatted state block that gets prepended to the
agent's context. Covers patterns P17 (task list awareness),
P21 (state visibility — "what is happening"), P24 (repeated failures
same axis).

Reads:
- The current TaskList (if a way to do so exists; falls back to none)
- `.claude/state/recurrences.json` (cross-session failure-axis counters)
- Most-recent deploy status (gcloud run jobs list, cached briefly)
- Most-recent terminal job_id (Firestore lookup, gated by env)

Skips anything that errors. Goal is "always something useful in
context", not "perfectly accurate state."
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

STATE_DIR = Path(os.environ.get("PROJECT_ROOT", "/Users/rohit/ytFactory")) / ".claude" / "state"


def recurrences() -> dict:
    f = STATE_DIR / "recurrences.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def cached_deploy_status(max_age_s: int = 300) -> str | None:
    """Cached `gcloud run jobs list` line for the render worker. Cache
    in .claude/state/deploy_status.json with timestamp to avoid hitting
    gcloud on every prompt."""
    cache = STATE_DIR / "deploy_status.json"
    import time
    now = time.time()
    if cache.exists():
        try:
            d = json.loads(cache.read_text())
            if now - d.get("ts", 0) < max_age_s:
                return d.get("status")
        except Exception:
            pass
    # Refresh.
    try:
        result = subprocess.run(
            ["gcloud", "run", "jobs", "describe",
             "ytfactory-render-worker-v2",
             "--project=ytfactory-prod-v3",
             "--region=asia-southeast1",
             "--format=value(spec.template.spec.template.spec.containers[0].image)"],
            capture_output=True, text=True, timeout=8,
        )
        status = result.stdout.strip() or None
    except Exception:
        status = None
    try:
        cache.write_text(json.dumps({"ts": now, "status": status}))
    except Exception:
        pass
    return status


def main() -> int:
    parts: list[str] = []

    parts.append("## Session state digest (auto-injected)")
    parts.append("")

    # Cross-session recurrences — patterns that have already burned
    # this user's time, with counts. If a counter is non-zero, the
    # agent should treat that axis as high-risk.
    rec = recurrences()
    if rec:
        sorted_rec = sorted(rec.items(), key=lambda kv: -kv[1])
        top = [f"`{k}`={v}" for k, v in sorted_rec[:8] if v > 0]
        if top:
            parts.append("**Recurring failure axes (cross-session):** " + ", ".join(top))
            parts.append("")

    # Deploy status
    image = cached_deploy_status()
    if image:
        # Just the tag, not the full SHA path
        tag = image.split(":")[-1] if ":" in image else image
        parts.append(f"**render-worker-v2 current image tag:** `{tag}`")
        parts.append("")

    # Skill list (just count + names) — helps the agent NOT forget
    # available skills like /diagnose-render.
    skills_dir = STATE_DIR.parent / "skills"
    if skills_dir.exists():
        skills = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
        if skills:
            parts.append(f"**Available skills:** {', '.join(skills)}")
            parts.append("")

    parts.append("---")
    parts.append("")
    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
