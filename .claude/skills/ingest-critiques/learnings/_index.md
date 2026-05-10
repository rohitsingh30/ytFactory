# /ingest-critiques — learnings index

One-liner per finding from prior runs. Detail in `<topic>.md` siblings.

## 2026-05-08 — skill authored

- Created alongside `pipeline/evals.py` to close the loop on the
  agent-to-agent contract spec'd at
  `/Users/rohit/evals/AGENT_CONTRACT.md`. Verdict-driven holds
  (`<channel>/_holds.json`) gate three cron uploaders so a
  reviewer-FIX/BLOCK on any slug stops that video from publishing
  without halting the queue. Smoke-tested against
  `/Users/rohit/evals/cosmosdecoded/critiques/` (5 critiques: 1 SHIP,
  2 FIX, 2 BLOCK with critical-block-param failures) — 4 holds set
  correctly, STATUS authoring columns updated, reviewer columns
  preserved.
