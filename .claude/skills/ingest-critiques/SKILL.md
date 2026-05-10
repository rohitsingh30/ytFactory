---
name: ingest-critiques
description: Pull critiques the `/judge-video` reviewer wrote into `/Users/rohit/evals/<project>/critiques/` BACK into the authoring side. Parses verdict (SHIP / FIX / BLOCK), weakest param, critical-block-param failures, and per-video fix instructions; updates `<project>/STATUS.md` AUTHORING-OWNED columns (`last_fix_attempted`, `last_fix_result`); sets a hold (`<channel>/_holds.json`) on every FIX/BLOCK slug so the cron uploaders skip it; clears holds on SHIP. Prints a routing table (Ship / Refix / Block) and the cross-cutting issues block. Auto-invoked when critiques newer than STATUS.md exist in `/Users/rohit/evals/<project>/critiques/` (per CLAUDE.md trigger). For each FIX, surfaces a per-slug re-render command — does NOT auto-rerender (one human gate before 30+ images burn). Use when the user says "ingest critiques", "pull the reviews", "what did the reviewer say", "process critiques", "what's blocked", or after `/judge-video` finishes. For dropping a NEW handoff use `/create-handoff-eval` (the inverse direction).
when_to_use: |
  - User says: "ingest critiques", "pull reviews", "process critiques", "what's blocked", "what did the reviewer say"
  - At session start if `/Users/rohit/evals/<project>/critiques/*_critique.md` mtime > `<project>/STATUS.md` mtime AND the table has any row with `last_fix_attempted=none|pending` (auto-invoke per CLAUDE.md)
  - Right after `/judge-video` finishes a batch
  - Before kicking a re-render — verify what's actually held
when_not_to_use: |
  - Writing a NEW handoff for the reviewer → use `/create-handoff-eval`
  - Engineer-mode debrief on what we learned → use `/update-docs`
  - Frame-by-frame re-critique → use `/critique-video` (engineer-mode, not reviewer-mode)
  - Auto-firing re-renders → not this skill's job; the routing table tells the user what to run
learnings_consulted:
  - /Users/rohit/evals/AGENT_CONTRACT.md   # contract source-of-truth
  - /Users/rohit/evals/SCHEMA.md           # 50-param rubric + critique format
  - /Users/rohit/evals/<project>/critiques/*_critique.md
  - /Users/rohit/evals/<project>/STATUS.md
helper_module:
  - pipeline/evals.py    # ingest(), route(), set_hold(), clear_hold(), update_status_authoring()
---

# /ingest-critiques — pull reviewer verdicts back into the loop

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

The reverse of `/create-handoff-eval`. The reviewer's critiques land at
`/Users/rohit/evals/<project>/critiques/<slug>_critique.md`. This skill
parses them, updates the shared ledger's authoring-owned columns, and
holds anything that didn't ship-rate so the cron uploader skips it.

## What it does

1. **Parses every `*_critique.md`** in the project's `critiques/` dir.
   For each: extracts verdict (SHIP / FIX / BLOCK), total + avg score,
   weakest param + score, the four critical-block params
   (`clipping_audible`, `on_screen_text_correctness`, `character_lock`,
   `asset_topicality`) if any < 5, and the `## Specific fix
   instructions` body if present.
2. **Routes by verdict + criticals**:
   - SHIP + no critical failures → `ship` (clear any prior hold)
   - FIX → `refix` (set hold + tag `last_fix_*` = `pending/pending`)
   - BLOCK → `block` (set hold + tag `pending/pending`)
   - Any verdict with critical-block param < 5 → forced `refix`
3. **Updates `<project>/STATUS.md` authoring columns only** —
   `last_fix_attempted`, `last_fix_result`. Reviewer columns
   (`first_critique`, `verdict`, `weakest_param`, `re_critique`,
   `final_status`) are NEVER touched (contract violation).
4. **Sets / clears holds** in `<channel>/_holds.json`. Held slugs are
   skipped by `scripts/upload_next.py`,
   `cosmosdecoded/scripts/cron_upload_daily.py`, and
   `historyrecapped/scripts/cron_upload_one.py`. The hold record
   carries `reason`, `set_at`, and `source_critique` path so a future
   reader can see WHY a slug is held.
5. **Prints the routing table** + cross-cutting issues block from
   STATUS.md.
6. **Suggests per-slug re-render commands** for FIX rows (does NOT
   auto-run — that's a human gate).

## How to run it

### 1. Determine the project

Ask via `AskUserQuestion` only if ambiguous:
- **Project** — channel slug. Tracked: `mystoriesanimated`,
  `cosmosdecoded`, `historyrecapped`, `hindutavaanimated`,
  `sportsrecapped`, `rhymetimejunction`, `scrollpulse`.
  Default: the project with the most unprocessed critiques (newer
  than its STATUS.md mtime).

If the user said something like "ingest cosmos" or "process the
mystoriesanimated critiques", skip the question and proceed.

### 2. Run ingest via the helper CLI

```bash
.venv/bin/python -m pipeline.evals ingest <project>
```

Or programmatically (preferred for richer output):

```python
from pipeline.evals import ingest, cross_cutting
summary = ingest(project="cosmosdecoded")
xc = cross_cutting("cosmosdecoded")
```

`ingest()` is **idempotent** — running it twice does not double-write
holds; it just confirms the existing state.

### 3. Print the routing table to chat

Format:

```
=== ingest summary for <project> ===

SHIP (N)
  <slug>  verdict=SHIP avg=7.42 weakest=loudness_range (4/10)

REFIX (N)
  <slug>  verdict=FIX avg=7.54 weakest=true_peak (4/10) [CRITICAL=...]
    fix: <first line of "## Specific fix instructions">

BLOCK (N)
  <slug>  verdict=BLOCK avg=7.08 weakest=true_peak (2/10) [CRITICAL=...]
    fix: (re-render won't help — pipeline-level fix needed)

HOLDS SET: N
  + <slug>
HOLDS CLEARED: N
  - <slug>

--- cross-cutting issues (pipeline-level) ---
<text from STATUS.md "## Cross-cutting issues" section>
```

### 4. Surface per-slug next-action commands

For each `refix` row, print the channel-appropriate re-render command:

| Channel | Re-render skill | Notes |
|---|---|---|
| `cosmosdecoded` | `/make-cosmos-short <slug>` (or `/make-cosmos-long`) | reads existing `cosmosdecoded/raw/<slug>.json` dossier |
| `historyrecapped` | `/make-history-short <slug>` (or `/make-sleep-history`) | bifurcates archival vs animated |
| `mystoriesanimated` | `/make-mystories-short` (re-pick variant) | one of nine variants |
| `hindutavaanimated` | `/make-hindutava-short` or `/make-hindutava-long` or `/make-katha` | by length |
| `sportsrecapped` | `/make-last5`, `/make-ranking`, `/make-tweet-reaction`, `/make-sports-doc` | by format |
| `scrollpulse` | `/make-reddit-thread` or `/make-tweet-reaction` (default split-screen) · AI Recap variant has no skill yet — flag manually | by format |
| `rhymetimejunction` | `/make-rhyme` | bilingual rhymes |

Print as a fenced bash block so the user can copy-paste:

```bash
# Suggested re-renders for FIX rows (NOT auto-run):
/make-cosmos-short pound-rebka-1959-short    # fix: chalkboard frame text
/make-cosmos-short penzias-wilson-1965-short # fix: -1.5 dB peak limiter
```

### 5. Surface BLOCK rows separately

BLOCK = **don't re-render this video** until the cross-cutting bug is
fixed. Print:

```
BLOCK rows pending pipeline-level fix:
  hubble-1929-short            (clipping_audible=3, weakest=true_peak)
  super-kamiokande-1998-short  (clipping_audible=2, weakest=true_peak)

These all share the cross-cutting issue:
  audio true peak sits at the clipping line — apply -1.5 dB
  true-peak limiter on encode for the whole channel BEFORE re-rendering.

Fix in pipeline/audio.py first, then re-run /ingest-critiques to
clear; re-renders happen after.
```

### 6. After fixes land, update authoring state

When the user actually re-renders one of the FIX slugs, update its
`last_fix_attempted` to a kebab-case label of the change:

```python
from pipeline.evals import update_status_authoring, clear_hold
update_status_authoring(
    "cosmosdecoded", "pound-rebka-1959-short",
    last_fix_attempted="chalkboard-text-rerender",
    last_fix_result="success",  # or "partial" / "regression"
)
clear_hold("cosmosdecoded", "pound-rebka-1959-short")
```

Then drop a `-v2` handoff via `/create-handoff-eval` for re-review.

## Important rules

- **Never write to `critiques/`.** Reviewer-owned. (Contract.)
- **Never modify reviewer-owned STATUS columns** (`first_critique`,
  `verdict`, `weakest_param`, `re_critique`, `final_status`). The
  helper enforces this — `update_status_authoring()` only touches
  `last_fix_attempted` + `last_fix_result`.
- **Default-hold on FIX/BLOCK.** A non-SHIP verdict ALWAYS halts the
  upload of that specific video. The user explicitly chose this
  policy: "stop the upload of that video [if below score]" — see
  CLAUDE.md `## Authoring↔reviewer eval contract`.
- **Don't auto-rerender.** Surface the suggested commands. The
  user chooses to invoke. Reason: pipeline-level bugs (BLOCK) need
  the fix landed before any rerender, and a wrong fix-spec on FIX
  burns ~30 images per Short.
- **Cross-cutting issues come before per-video fixes.** If STATUS.md
  has a `## Cross-cutting issues` body, surface it FIRST. Fixing a
  pipeline bug once unblocks every BLOCK row; re-rendering individual
  videos before the fix wastes work.
- **Idempotent.** Running twice does not double-set holds or stomp
  STATUS rows. Safe to invoke at session start unconditionally.

## Quality gates (run before reporting done)

1. **Helper invoked** — `pipeline.evals.ingest()` ran (file mtimes on
   `<channel>/_holds.json` and `<project>/STATUS.md` post-run).
2. **No reviewer columns touched** — diff `<project>/STATUS.md`
   before/after; only columns 5-6 (`last_fix_attempted`,
   `last_fix_result`) changed for any row.
3. **Holds match routing** — every `refix`/`block` slug has an entry
   in `<channel>/_holds.json` with a non-empty `reason`.
4. **Routing table printed** — chat output includes counts for SHIP /
   REFIX / BLOCK.
5. **Cross-cutting block surfaced** — if STATUS.md has a non-empty
   `## Cross-cutting issues` section, it's in the output.
6. **No re-render auto-fired** — no `/make-*` skill invoked or Bash
   render command run.
7. **Per-slug next-action commands printed** — at least one suggested
   command per FIX row, in a copy-pasteable bash block.

## Self-learning loop

Every time `/ingest-critiques` runs and the user later corrects the
output ("you missed a critique", "you held the wrong slug", "the
re-render command for X channel was wrong"):

1. Classify per `docs/post_upload_analysis.md` taxonomy.
2. Append a one-liner to `learnings/_index.md`.
3. If CLASS-OF-BUG (parser regression, mis-routing, hold leak):
   patch `pipeline/evals.py` + add a unit test in
   `tests/test_evals.py`.
4. If channel-format gap (a new channel needs a re-render mapping):
   update the table in step 4 above + add the channel to
   `KNOWN_PROJECTS` in `pipeline/evals.py`.

## Engineering-efficiency check

This skill is **read-mostly + audit-trail**. It writes to:

- `<project>/STATUS.md` — authoring columns only.
- `<channel>/_holds.json` — holds registry only.

It NEVER:

- Modifies critique files.
- Touches reviewer-owned STATUS columns.
- Auto-runs renders or uploads.
- Calls the `/judge-video` skill.

## See also

- `/Users/rohit/evals/AGENT_CONTRACT.md` — contract source-of-truth.
- `/Users/rohit/evals/SCHEMA.md` — 50-param rubric + critique format.
- `pipeline/evals.py` — helper module.
- `.claude/skills/create-handoff-eval/SKILL.md` — the inverse skill
  (drops a NEW handoff for review).
- `.claude/skills/update-docs/SKILL.md` — engineer-mode debrief.

## Memory pointer

`skill_ingest_critiques.md`.
