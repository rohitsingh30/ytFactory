---
name: update-docs
description: One-skill solution for tracking progress across the ytFactory repo. Reads the conversation since the last invocation (or session start), classifies every surprise / fix / pivot / correction / learning per the post-upload analysis taxonomy (ONE-OFF / CLASS-OF-BUG / PIPELINE-BUG / WORKFLOW-IMPROVEMENT / PRONUNCIATION), then dual-saves each finding to BOTH `~/.claude/projects/-Users-rohit-ytFactory/memory/` and the right project doc (channel learnings or `docs/`). Replaces the standalone post-upload debrief workflow and is auto-invoked after every meaningful unit of work. Use when the user says "update the docs", "save what we learned", "track progress", "debrief", "analyze this conversation", or after any render ships / `/critique-*` runs / durable user correction. For settings/hook changes use /update-config; for new skill authoring use /make-skill.
---

# /update-docs — one-skill progress tracking

> **Cross-cutting skill — not a niche producer.** This skill does not
> author channel narrations or modify per-channel state via the
> website state API. The NicheVideo + state-client contract in
> CLAUDE.md applies only if this skill incidentally needs to read
> channel state as context.

Reads the relevant existing documentation, walks the conversation, and
updates everything in one shot. You are wearing the **Archivist** hat:
you don't write code, you don't render videos, you don't argue with the
user — you **persist what was learned** so the next session doesn't
re-discover the same bugs.

This is the callable form of the post-upload analysis rule
(`docs/post_upload_analysis.md`). That doc is the taxonomy source of
truth; this skill is the **executable workflow** that applies it.

## When to run it

**Auto-invoke** (Claude should run this without being asked when ANY of
the following fires — see CLAUDE.md "Auto-invocation rule"):

1. A channel uploader prints `UPLOAD OK` (any `<channel>/scripts/upload*.py`).
2. The user runs `/critique-audio` or `/critique-video` and surfaces ≥1
   regression.
3. The user issues a durable correction, preference, or rule
   ("from now on …", "stop doing …", "always do …", "the closer should
   always …").
4. The user explicitly asks: "update the docs", "save what we learned",
   "track progress", "debrief", "analyze this conversation".
5. End of session if ≥1 render shipped OR ≥1 durable learning surfaced
   that hasn't been persisted yet.

**Don't auto-invoke** for: trivial reads, exploratory grep, single-file
edits the user is mid-flow on, or sessions where nothing durable was
learned. If the conversation produced zero actionable findings, exit
with `no findings — skipping`.

## How to run it

### 1. Load the lay of the land (read-only)

Read these files BEFORE walking the conversation. They tell you where
each finding belongs:

- `CLAUDE.md` — the dual-save rule, post-upload taxonomy, channel
  layout reference.
- `docs/post_upload_analysis.md` — classification table (5 types) and
  per-type save destinations.
- `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md` — the
  index. Skim it to know what's already saved (avoid duplicates).
- For each channel touched in this conversation:
  `<channel>/learnings/channel.md` — channel-specific rules.

Don't dump these to the user; just absorb so you place findings
correctly.

### 2. Walk the conversation

Walk backward from the most recent assistant turn to either:
- the previous `/update-docs` invocation, OR
- the start of the session if this is the first run.

For every visible iteration (render attempt, debug round, script
rewrite, prompt-tuning loop, user correction, surprising tool result,
pivot), capture:

```
- what happened (one sentence)
- why it happened (root cause)
- what fixed it (or what's still broken)
- is this captured in a learning file already? (search MEMORY.md +
  channel/learnings/ + docs/)
```

Default to **write, not skip**. Small learnings compound. Per
CLAUDE.md: "missing them causes the next render to re-discover the
same bug."

### 3. Classify each finding

Per `docs/post_upload_analysis.md`:

| classification | meaning | save target |
|---|---|---|
| **ONE-OFF** | typo / single bad pronunciation / wrong date in this slug only | fix in the slug's JSON; one-line note in `.claude/skills/<skill>/learnings/_index.md` |
| **CLASS-OF-BUG** | recurring pattern that will hit the next 100 renders | dedicated learning file: project-doc + skill-side mirror + memory entry |
| **PIPELINE-BUG** | rendering / TTS / image-prep / upload code itself was wrong | fix in `pipeline/<module>.py`; learning file documents the bug + fix |
| **WORKFLOW-IMPROVEMENT** | the order/cadence of steps could be shortened | update CLAUDE.md if cross-channel; SKILL.md if skill-specific |
| **PRONUNCIATION** | a TTS provider mispronounced a token | extend `pipeline/tts/text_normalize.py::_ACRONYM_PHRASES` (cross-channel) OR per-slug `<slug>.pronounce.json` (skill-specific) |

If a finding is genuinely **trivia** (took 45 min to render, weather
in Mumbai, off-topic chatter) — drop it. The skill's value is in the
filter, not in volume.

### 4. Dual-save each finding (CLAUDE.md rule)

For every CLASS-OF-BUG, PIPELINE-BUG, WORKFLOW-IMPROVEMENT, or
cross-channel PRONUNCIATION:

**A) Project doc** (source of truth — editable, reviewable):
- Cross-channel rule → `docs/<topic>.md` AND link from each affected
  `<channel>/learnings/channel.md`.
- Channel-specific → `<channel>/learnings/<topic>.md` AND link from
  that channel's `learnings/channel.md`.
- Pipeline-internal mechanic → inline docstring in the owning module.

**B) Memory entry** (terse pointer):
- `~/.claude/projects/-Users-rohit-ytFactory/memory/<type>_<topic>.md`
  with frontmatter (`name`, `description`, `type`).
- Type prefixes: `feedback_`, `project_`, `user_`, `reference_`,
  `skill_`.
- Body: rule + **Why:** + **How to apply:** for `feedback`/`project`
  types.

**C) MEMORY.md index update**:
- Add ONE line under the right section: `- [Title](file.md) — one-line hook`
- Each entry ≤200 chars (size budget — see Quality gate 3 below).
- Don't paste entire memory contents into the index.

**ONE-OFFs** stay in the slug's JSON + `_index.md` line — no project
doc needed.

### 5. Update-don't-duplicate

Per CLAUDE.md: "Do not write duplicate memories. First check if there
is an existing memory you can update before writing a new one."

For each finding, before writing:
1. Grep `MEMORY.md` for the topic.
2. If a memory file already covers it → READ it, UPDATE in place,
   add a `**2026-MM-DD update:**` block.
3. Only create a new file if no existing one fits.

Same rule for project docs: extend the existing `<channel>/learnings/<topic>.md`
or `docs/<topic>.md` rather than creating a sibling.

### 6. Quality gates (mechanical, not advisory)

Run BEFORE printing the summary. Block on hit:

1. **Dual-save verifier** — for every memory file written this run,
   check the corresponding project doc exists. If not, stop and write
   it before reporting done.
2. **Index entry length** — every new MEMORY.md line ≤200 chars.
   Truncate the hook if needed; never wrap.
3. **MEMORY.md size guard** — after writes, check total size:
   ```bash
   wc -c ~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md
   ```
   If >24576 bytes (24KB limit), surface a triage warning in the
   handoff: list the longest lines and ask the user to approve
   collapsing them into a topic file. Do NOT silently rewrite the
   index.
4. **No prose in MEMORY.md** — MEMORY.md is index-only. Reject if any
   line has more than one sentence.
5. **Frontmatter present** — every memory file has `name`,
   `description`, `type` frontmatter. Block on missing.
6. **Channel layout compliance** — channel-specific learnings go under
   `<channel>/learnings/`, never under `docs/`. Cross-channel rules go
   under `docs/`, never under one channel.
7. **No new memory file when an existing one covers ≥80% of the
   topic** — defer to update-in-place.

### 7. Skill self-update (if applicable)

If a finding is a CLASS-OF-BUG that will recur for a specific `make-*`
skill, also append a 1-line entry to that skill's learnings index:

```
.claude/skills/<skill>/learnings/_index.md
```

And if the rule belongs in the skill's prompt (not just retrievable
context), add a bullet to that SKILL.md's "Important rules" or
"Learnings from prior runs" section so the next invocation
internalizes it.

### 8. Report back

Print a compact summary:

```
✓ /update-docs — N findings persisted

CLASS-OF-BUG  (M)
  - <topic>: docs/<file>.md + memory/<file>.md
  - ...

PIPELINE-BUG  (P)
  - pipeline/<module>.py:<line> — <one-line fix>

WORKFLOW-IMPROVEMENT  (Q)
  - CLAUDE.md § <section> — <change>

PRONUNCIATION  (R)
  - pipeline/tts/text_normalize.py: +<token> → <phonetic>
  - or: <slug>.pronounce.json: +<entry>

ONE-OFF  (S)
  - notes appended to .claude/skills/<skill>/learnings/_index.md

skipped  (T)
  - <topic>: already covered in <existing-file.md>

MEMORY.md size: <N>KB / 24KB limit  [PASS|TRIAGE-NEEDED]

next: <suggested follow-up if any> | nothing — clean slate
```

If TRIAGE-NEEDED, list the top-5 longest entries and ask whether to
collapse them.

### 9. Self-learning hook (recursive — yes, this skill watches itself)

If the user later corrects how `/update-docs` itself classified a
finding ("that was actually a class-of-bug, not a one-off") OR points
out a duplicate that slipped past the gate ("you wrote two entries
for the same topic"):

1. Classify the meta-bug:
   - ONE-OFF (single mis-call) → fix the saved file, no skill change.
   - CLASS-OF-BUG (the heuristic itself is wrong) → update this
     SKILL.md's classification table or quality gates AND append
     to `.claude/skills/update-docs/learnings/<topic>.md`.
2. Mirror to `~/.claude/projects/.../memory/feedback_update_docs_<topic>.md`
   per the dual-save rule.
3. If the same meta-bug fires twice, escalate to a hard quality gate
   in section 6.

## Important rules

- **Read-only on the deliverable.** Never touch a rendered mp4, an
  upload record, or a channel's `branding/`. This skill writes docs
  and memory only.
- **Default to write, not skip.** Per CLAUDE.md post-upload rule: small
  learnings compound; missing them causes regression on the next render.
- **Update existing > create new.** Always grep for an existing
  memory/doc covering the topic before creating a sibling.
- **MEMORY.md is index-only.** Each line ≤200 chars, one sentence,
  no prose. Detail lives in the linked topic file.
- **Dual-save is mandatory.** Memory-only writes are a violation
  (CLAUDE.md persistence rule). Project doc is the source of truth.
- **Cross-channel goes to `docs/`, channel-specific goes to
  `<channel>/learnings/`.** Never mix.
- **No fabrication.** Only persist findings the agent can verify from
  this conversation's evidence. Don't invent learnings to look thorough.
- **Don't touch this conversation's plan or tasks.** Memory is for
  future sessions; this session uses tasks/plan for in-flight state.
- Use `.venv/bin/python` for any helper commands (consistency with
  other ytFactory skills).
- Never invoke pipeline render code from this skill — it's docs-only.

## Auto-invocation rule (mirror in CLAUDE.md)

The CLAUDE.md "Post-upload analysis rule" section now points here.
Auto-trigger conditions are listed in `## When to run it` above.
The skill is callable any time but should fire automatically without
the user asking on the trigger conditions.

If you're unsure whether a moment qualifies as a trigger, err on the
side of running — the skill's first quality gate (`no findings —
skipping`) handles false positives gracefully.

## Learnings from prior runs

(Empty on day 1. Each invocation may append a note to
`.claude/skills/update-docs/learnings/_index.md` if a regression
in classification or save-target was caught and corrected.)

## Why this skill is separate from /make-skill

`/make-skill` authors *new* skills. `/update-docs` *runs* — it's the
operational counterpart that captures what each skill's runs taught us
and persists it back into the docs they all read. Without
`/update-docs`, every `/make-*` skill would need its own debrief logic,
which the codebase already proved doesn't scale (the LIGO Short
2026-05-08 surfaced 12 learnings across 6 iterations — half stayed in
conversation memory until the user explicitly asked).

## Reference

- `docs/post_upload_analysis.md` — classification taxonomy (source of truth)
- `CLAUDE.md` § "Persistence rule" — dual-save rule
- `CLAUDE.md` § "Post-upload analysis rule" — auto-invocation triggers
- `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md` — index
  this skill maintains
