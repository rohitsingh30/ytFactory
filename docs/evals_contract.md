# Authoring↔reviewer eval contract — automation

Source-of-truth contract: [`/Users/rohit/evals/AGENT_CONTRACT.md`](file:///Users/rohit/evals/AGENT_CONTRACT.md).
Detailed schemas: [`/Users/rohit/evals/SCHEMA.md`](file:///Users/rohit/evals/SCHEMA.md).

This doc covers the **authoring side** of the contract — the ytFactory
half. Two agents share the `/Users/rohit/evals/` workspace; ytFactory
produces videos and writes handoffs; `/judge-video` (reviewer) reads
those handoffs, scores the videos against the 50-param rubric, and
writes critiques back. Both agents edit `<project>/STATUS.md` (only
their own columns).

## Components

| Piece | Path | Role |
|---|---|---|
| Helper library + CLI | `pipeline/evals.py` | handoff writer, critique parser, STATUS updater (authoring cols only), holds registry |
| Handoff skill | `.claude/skills/create-handoff-eval/` | drops contract-compliant handoff to `<project>/inbox/` |
| Ingest skill | `.claude/skills/ingest-critiques/` | reads critiques, updates STATUS, sets/clears holds, prints routing |
| Holds registry | `<channel>/_holds.json` | per-channel JSON; cron uploaders skip held slugs |
| CLAUDE.md section | `## Authoring↔reviewer eval contract` | auto-invocation rules + contract-floor rules |

## How it flows

```
[ user runs a /make-* skill ]
            │
            ▼
[ batch lands at <channel>/shorts/*.mp4 ]
            │
            ▼   auto-invoke (CLAUDE.md trigger 1)
[ /create-handoff-eval ]
            │
            ├── writes /Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch>.md
            └── ensures STATUS.md row exists per slug
            │
            ▼   user invokes manually
[ /judge-video <handoff-path> ]
            │
            ▼   reviewer writes
[ /Users/rohit/evals/<project>/critiques/<slug>_critique.md ]
            │
            ▼   auto-invoke (CLAUDE.md trigger 2 or 3)
[ /ingest-critiques ]
            │
            ├── parses verdicts + critical-block params
            ├── updates STATUS authoring columns (last_fix_*)
            ├── sets hold in <channel>/_holds.json on FIX/BLOCK
            ├── clears hold on SHIP
            └── prints routing table + cross-cutting issues
            │
            ▼
[ cron uploaders skip held slugs ]
[ user runs /make-* skill on FIX rows to re-render ]
            │
            ▼   loop
[ user invokes /create-handoff-eval again with -v2 batch slug ]
```

## Hold policy

A hold is set on a slug when ANY of:

- Verdict is `FIX`
- Verdict is `BLOCK`
- Any critical-block param < 5 (`clipping_audible`,
  `on_screen_text_correctness`, `character_lock`, `asset_topicality`)

A held slug is skipped by all three cron uploaders. The hold record
carries `reason`, `set_at`, and `source_critique` path so a future
reader can see WHY it's held.

A hold clears when:
- A new SHIP verdict (with no critical failures) lands for that slug,
  OR
- The user runs `python -m pipeline.evals unhold <project> <slug>`.

## CLI cheat sheet

```bash
# Bootstrap a new project
.venv/bin/python -m pipeline.evals init <project>

# Pull critiques, set holds, update STATUS
.venv/bin/python -m pipeline.evals ingest <project>
.venv/bin/python -m pipeline.evals ingest <project> --dry-run

# Pretty-print STATUS table
.venv/bin/python -m pipeline.evals status <project>

# Manual hold management
.venv/bin/python -m pipeline.evals holds  <project>
.venv/bin/python -m pipeline.evals hold   <project> <slug> --reason "..."
.venv/bin/python -m pipeline.evals unhold <project> <slug>
```

## Contract-floor rules (must never violate)

- **Path is non-negotiable.** Handoff drops at
  `/Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch-slug>.md`.
  Old `docs/eval_handoff_*` is RETIRED.
- **Never write to `<project>/critiques/`.** Reviewer-owned area.
- **Never modify reviewer-owned STATUS columns** (`first_critique`,
  `verdict`, `weakest_param`, `re_critique`, `final_status`). The
  helper enforces this — `update_status_authoring()` only touches
  `last_fix_attempted` + `last_fix_result`.
- **Never overwrite an existing handoff.** `write_handoff()`
  auto-suffixes `-vN`.
- **Never call `/judge-video` directly.** The user invokes; this
  side reacts to the resulting critique files.

## Adding a new project

```bash
.venv/bin/python -m pipeline.evals init <new_project>
```

…creates `inbox/` + `critiques/` + a STATUS.md skeleton. Then add
`<new_project>` to `KNOWN_PROJECTS` in `pipeline/evals.py` and to the
auto-invoke project list in CLAUDE.md.

If the new project's cron uploader needs the hold gate, add:

```python
from pipeline.evals import is_held
# in pending-candidate enumeration:
if is_held("<new_project>", slug):
    continue
```
