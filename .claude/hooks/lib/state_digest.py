#!/usr/bin/env python3
"""Produce a state digest to inject into context on UserPromptSubmit.

Stdout: a markdown-formatted state block that gets prepended to the
agent's context. Covers patterns P17 (task list awareness),
P21 (state visibility — "what is happening"), P24 (repeated failures
same axis).

2026-05-24 — adds memory-content injection per user directive
'the broader doc/skill-enforcement layer (memory-content injection)':
when the user prompt mentions a topic that overlaps with memory file
descriptions, the most-relevant memory FILE BODIES (not just names)
get prepended. Closes the 'agent cites memory file slug without
retrieving its contents' failure mode that has been the dominant
shape of citation theatre across 200 mined sessions.

Reads:
- The current TaskList (if a way to do so exists; falls back to none)
- `.claude/state/recurrences.json` (cross-session failure-axis counters)
- Most-recent deploy status (gcloud run jobs list, cached briefly)
- Most-recent terminal job_id (Firestore lookup, gated by env)
- $USER_PROMPT env (passed by userprompt_init.sh) for memory relevance scoring
- /Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/*.md frontmatter
  + body for keyword-matched injection

Skips anything that errors. Goal is "always something useful in
context", not "perfectly accurate state."
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

STATE_DIR = Path(os.environ.get("PROJECT_ROOT", "/Users/rohit/ytFactory")) / ".claude" / "state"
MEMORY_DIR = Path(os.path.expanduser(
    "~/.claude/projects/-Users-rohit-ytFactory/memory"
))


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL,
)
_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def _memory_files():
    """Walk all .md files in the memory dir, parse frontmatter."""
    if not MEMORY_DIR.exists():
        return []
    out = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            text = p.read_text()
        except Exception:
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        fm, body = m.group(1), m.group(2).strip()
        name_m = _NAME_RE.search(fm)
        desc_m = _DESC_RE.search(fm)
        out.append({
            "path": p,
            "name": (name_m.group(1).strip() if name_m else p.stem),
            "description": (desc_m.group(1).strip() if desc_m else ""),
            "body": body,
        })
    return out


_STOPWORDS = {
    "this", "that", "with", "from", "into", "does", "need", "also",
    "just", "like", "what", "when", "want", "have", "make", "they",
    "them", "your", "still", "even", "tell", "give", "show", "would",
    "could", "should", "about", "there", "where", "which", "their",
    "first", "second", "third",
}


def _tokens(s: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9_]{4,}", (s or "").lower())
        if t not in _STOPWORDS
    }


def relevant_memories(user_prompt: str, top_n: int = 3) -> list[dict]:
    """Score memory files by token overlap with the user's prompt
    against the file's name + description. Return top N."""
    if not user_prompt:
        return []
    prompt_tokens = _tokens(user_prompt)
    if not prompt_tokens:
        return []

    scored = []
    for mem in _memory_files():
        haystack = f"{mem['name']} {mem['description']}"
        mem_tokens = _tokens(haystack)
        if not mem_tokens:
            continue
        overlap = prompt_tokens & mem_tokens
        if overlap:
            scored.append((len(overlap), mem))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:top_n]]


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

    # Job-telemetry pre-fetch (P31, 2026-05-24).
    # When the user's prompt mentions a 32-char hex job_id, auto-fetch
    # the highest-signal artifact (refiner_io.json fallback_count) +
    # event-stream rollup and inject BEFORE the agent generates. Closes
    # the "agent talks about <job_id> without ever opening the artifact"
    # failure mode that bit 88d98126 (fallback_count: 14 was in the
    # artifact the whole time, agent never opened it).
    user_prompt = os.environ.get("USER_PROMPT", "")
    if user_prompt:
        try:
            from job_telemetry import telemetry_block_for_prompt
            tel = telemetry_block_for_prompt(user_prompt)
            if tel:
                parts.append(tel)
        except Exception:
            pass

    # Memory-content injection (2026-05-24 add).
    # Score every feedback_*/project_* memory file's frontmatter
    # (name + description) against the user's current prompt.
    # Inject the TOP-3 most-relevant file BODIES so the agent
    # has the actual rule content in context, not just the slug.
    user_prompt = os.environ.get("USER_PROMPT", "")
    if user_prompt:
        memories = relevant_memories(user_prompt, top_n=3)
        if memories:
            parts.append("## Relevant memory content (top 3, body-injected)")
            parts.append("")
            for mem in memories:
                parts.append(f"### {mem['name']}")
                if mem.get("description"):
                    parts.append(f"*{mem['description']}*")
                parts.append("")
                # Cap body at 800 chars to keep digest readable.
                body = mem["body"]
                if len(body) > 800:
                    body = body[:800] + " […truncated; see full file]"
                parts.append(body)
                parts.append("")

    parts.append("---")
    parts.append("")
    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
