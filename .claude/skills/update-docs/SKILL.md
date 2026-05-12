---
name: update-docs
description: One-skill solution for tracking progress across the ytFactory repo. Reads the conversation since the last invocation (or session start), classifies every surprise / fix / pivot / correction / learning per the post-upload analysis taxonomy (ONE-OFF / CLASS-OF-BUG / PIPELINE-BUG / WORKFLOW-IMPROVEMENT / PRONUNCIATION), saves each finding to memory + the right project doc + EVERY related doc the finding touches (sweep grep across docs/ + <channel>/learnings/ + cloud/*/README.md + inline module docstrings; create new docs when a meta-pattern surfaces). Replaces the standalone post-upload debrief workflow and is auto-invoked after every meaningful unit of work. Use when the user says "update the docs", "save what we learned", "track progress", "debrief", "analyze this conversation", or after any render ships / `/critique-*` runs / durable user correction. For settings/hook changes use /update-config; for new skill authoring use /make-skill.
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

### 4. Save each finding (CLAUDE.md rule + sweep related docs)

Persistence is **memory + project doc + EVERY related doc the finding
touches**, not just the dual-save pair. The skill's job is to leave
the docs tree internally consistent — so a future reader landing on
*any* doc that names the affected module / endpoint / topic discovers
the new rule, not a stale claim.

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

**D) Sweep related docs** (the step memory-only-thinking misses —
2026-05-10 correction):

For each finding, before reporting done, grep the repo for every
file that names the affected modules / endpoints / topics. For each
hit, decide one of:

| state of the hit                                 | action                                                                 |
|--------------------------------------------------|------------------------------------------------------------------------|
| doc still accurate, no mention of new finding    | add a 1-3 line cross-reference pointing at the new project doc         |
| doc has a stale claim (URL, behaviour, schema)   | edit inline AND record as ONE-OFF in `_index.md` so the audit is logged |
| doc covers an adjacent topic that should link    | add a "see also" line                                                  |
| doc is the inline module docstring               | extend it; that's the implementation's source of truth                 |
| doc would NEED to exist but doesn't (meta-pattern: e.g. "every doc naming a Cloud Run URL should be audited for staleness") | create the new doc; record it in this run's report under CREATE       |

Concrete sweep checklist (run for every finding):

1. `grep -rn "<affected-module-or-endpoint>" docs/ <channel>/learnings/ web/README.md cloud/*/README.md` — every match is a candidate.
2. `grep -rn "<affected-module>" pipeline/ web/ control/ --include='*.py'` for inline docstrings worth extending.
3. `grep -rn "<related-skill-name>" .claude/skills/*/SKILL.md` if the finding changes a skill's authorial contract.
4. If a stale URL / version / count was edited inline, log a ONE-OFF
   in `.claude/skills/update-docs/learnings/_index.md` with the grep
   recipe future runs should use to catch siblings.

The sweep MUST surface in the report (Quality gate 8) — list every
related doc updated/created OR explicitly state "no related docs
touched — finding is self-contained because <reason>". Silent
omission is what created the original CLAUDE.md "save to BOTH" rule
in the first place; this gate extends it from 2 saves to N.

**ONE-OFFs** stay in the slug's JSON + `_index.md` line — no project
doc needed. They DO still get the related-doc sweep though: if a
ONE-OFF inline edit is "the kind of thing other docs probably also
have wrong" (stale URL, retired flag name, retired channel slug),
the sweep grep recipe goes into `_index.md` so the next session can
audit siblings.

### 5. Update-don't-duplicate

Per CLAUDE.md: "Do not write duplicate memories. First check if there
is an existing memory you can update before writing a new one."

For each finding, before writing:
1. Grep `MEMORY.md` for the topic.
2. If a memory file already covers it → READ it, UPDATE in place,
   add a `**2026-MM-DD update:**` block.
3. Only create a new file if no existing one fits.

Same rule for project docs: extend the existing `<channel>/learnings/<topic>.md`
or `docs/<topic>.md` rather than creating a sibling. Apply identically
to **every** doc the related-doc sweep (step 4D) surfaces.

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
   If >49152 bytes (48 KB cap — see CAP HISTORY below), surface a
   triage warning in the handoff: list the longest lines and ask the
   user to approve collapsing them into a topic file. Do NOT silently
   rewrite the index.

   **CAP HISTORY:**
   - **2026-05-04:** initial cap 24 KB. Set when the index was ~40
     entries.
   - **2026-05-10:** raised to 48 KB. The index has grown to 100+
     entries across 7 production channels + cross-channel rules +
     skill self-learnings. A line-by-line collapse pass on the
     200-280 B oversized entries (5 trimmed in the cross-engage
     2026-05-10 run) saves ~3-5 KB but doesn't move the structural
     floor. Rather than cap a healthy index growth pattern, raised
     the gate. New entries STILL must follow Q2 (line ≤200 chars,
     one sentence) — the cap controls aggregate size, not new-line
     discipline.
   - **Re-evaluate** when the index passes 60 KB OR when a single
     section (## Channels / ## Cross-channel learnings) becomes
     unreadable in one screen of scrolling, whichever fires first.
     At that point the right intervention is a structural rewrite
     (split per-section indexes into per-section files), not another
     cap bump.
4. **No prose in MEMORY.md** — MEMORY.md is index-only. Reject if any
   line has more than one sentence.
5. **Frontmatter present** — every memory file has `name`,
   `description`, `type` frontmatter. Block on missing.
6. **Channel layout compliance** — channel-specific learnings go under
   `<channel>/learnings/`, never under `docs/`. Cross-channel rules go
   under `docs/`, never under one channel.
7. **No new memory file when an existing one covers ≥80% of the
   topic** — defer to update-in-place.
8. **Related-doc sweep verifier** — for every finding, the report
   (Section 9) MUST list every related doc updated/created OR
   explicitly state "no related docs touched — finding is
   self-contained because <reason>". The grep recipe used must be
   shown so the next session can re-run it. Block on missing.
9. **Clean-tree verifier** — after the commit step (Section 8) runs,
   `git status --porcelain` MUST return empty (or only files the run
   intentionally deferred per Section 8's auto-skip rules, called out
   in the report). A non-empty working tree at end-of-run means
   either the commit failed or something got written after the
   commit — both are bugs that will strand findings on the agent's
   laptop. Block on hit and surface to the user.
10. **Fix-claim verifier** — for every "Fix:" / "Mitigation:" / "Now
    does X:" line written into a project doc this run, two checks:
    - The claim cites a **commit SHA** (`commit abc1234 (2026-MM-DD)`).
      "Now does X" without a SHA is ambiguous between intended and
      shipped — block, replace with "Planned (PR/branch-name):" if
      the fix isn't actually in the tree yet.
    - If the doc names a **specific test** as pinning the fix
      (`Test: tests/foo.py::Bar::baz`), spot-check it: read the test,
      verify it would fail on the buggy state. If the test asserts
      the BUGGY behaviour (e.g. `assertEqual(status, "failed")` when
      the bug is sending "failed"), the test green-lights the bug
      rather than catching it — block, fix the test before the doc
      claim ships. Memory: `feedback_doc_aspirational_claims.md`.
      Origin: 2026-05-12 audit found `_ack` "failed→error" doc-claim
      that hadn't shipped to code OR test for 24 h, accruing 158
      zombie LEASED tasks.

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

### 8. Commit + push (mandatory — established 2026-05-12)

Every `/update-docs` run **MUST** end with a single `git add -A && git commit`
that lands BOTH this run's edits AND any pre-existing unstaged work
the run found lying around. Stranded unstaged changes are a recurring
bug class — multiple prior runs persisted findings to disk but never
committed them, so the docs were "saved" only on the agent's laptop
and the source of truth never moved. The 2026-05-12 audit found 16
stranded files (a full prior `/update-docs` run's output of operator-
QoL fixes + composite-index findings) sitting unstaged for hours.

**Concrete recipe:**

```bash
# 1. Sanity-check what we're about to commit (do NOT skip this read).
git status --porcelain
git --no-pager diff --stat HEAD

# 2. If the diff includes code (not just docs), run the relevant
#    quick-check (typecheck / unit tests on touched modules). If
#    anything fails, STOP and surface to the user — do not commit
#    broken code under a /update-docs commit.
#
#      *.py modified  → PYTHONPATH=. .venv/bin/python -m pytest \
#                         <touched test files> -x -q
#      web-next/**    → (cd web-next && npm run typecheck)
#      .claude/skills/*/SKILL.md modified  →
#         .venv/bin/python scripts/lint_skill_md.py
#         (catches the 1024-char overflow + YAML-colon nested-mapping
#          failures that silently make a skill unloadable next session
#          — heuristics 30b + 30c. 2026-05-12 wired in after 10 skills
#          shipped broken across two waves.)
#
# 2b. (2026-05-12 add-on) If ANY production-code file is in the diff
#     (pipeline/, control/, web/, web-next/, cloud/*/, scripts/), run
#     the test-coverage gate FIRST. The gate enforces 100% coverage
#     on lines changed THIS session — pre-fix, /update-docs would
#     happily commit code with no test coverage and the bug would
#     surface on the user's next render.
#
.venv/bin/python scripts/coverage_gate.py
#
#     Exit 0 → ready to commit.
#     Exit 1 → uncovered changed lines. Author the missing tests
#              (or add explicit `# coverage: <≥6-word reason>`
#              comments) BEFORE committing. The gate's report
#              names the exact action.
#     Exit 3 → pytest itself failed. Fix the failing tests BEFORE
#              committing — committing broken code under a docs
#              commit defeats the gate.
#
#     The /test-coverage skill is the user-facing wrapper around
#     this gate. See `.claude/skills/test-coverage/SKILL.md`.
#
# 3. Stage everything and write ONE descriptive commit. Do NOT split
#    into per-file commits — the whole point is that /update-docs
#    runs are one atomic persistence step.

git add -A
git commit -m "$(cat <<EOF
docs(/update-docs): persist N findings from <session-summary>

<one paragraph per finding, classification + topic + save targets>

If the diff also includes code (e.g. an inline fix the docs
reference), name it explicitly so the commit isn't mis-classified
as docs-only:

CODE: <module> — <one-line what changed>
DOCS: <file> — <what was added>
SKILL: .claude/skills/<x>/SKILL.md — <what changed>

Validation: <tests run> <typecheck status>

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
EOF
)"

# 4. Push if the branch tracks a remote and we're on main/a feature
#    branch the user expects to share. Skip the push if we're on a
#    detached HEAD or a checkpoint branch that shouldn't be pushed.
git push 2>&1 || echo "(push skipped — no upstream or push blocked)"
```

**Commit-grouping policy:**

- **One commit per run** is the default. The findings cohere by virtue
  of being captured in the same conversation; splitting them adds noise.
- **Two commits IS allowed** when the run's diff cleanly bisects into
  (a) durable docs/learnings (`docs(/update-docs): …`) and
  (b) a tightly-scoped code fix that's already tested (`fix(<scope>): …`).
  Use this when the code fix would survive on its own (e.g. it would
  pass code review without the docs context).
- **Never** leave the working tree dirty at the end of `/update-docs`.
  If something is intentionally NOT being committed (e.g. a render
  artifact, a local debug file), call it out explicitly in the report
  AND `git stash` or `git checkout --` it so the next run sees a
  clean slate.

**Trailer rule:** the commit MUST include the
`Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
trailer (CLAUDE.md "git_commit_trailer" requires it for every agent-
authored commit; `/update-docs` is no exception).

**Auto-skip cases (still report):**

- `git status --porcelain` returns empty → "no changes to commit —
  prior run already pushed". Report and exit cleanly.
- The only diff is in untracked files matching a `.gitignore` pattern
  → ignore them, but mention in the report so the user notices if
  something was accidentally gitignored.
- The user is on a branch with uncommitted state from THEIR own
  manual edits (i.e. not from this `/update-docs` run) → ask before
  committing. Use `ask_user` with explicit choices: "Commit your
  unstaged changes too" / "Commit only what /update-docs wrote" /
  "Skip commit, leave unstaged". Default safe choice is to ask, NOT
  to silently bundle the user's work into a docs commit.

### 9. Report back

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

related-doc sweep  (per Section 4D + Quality gate 8)
  - <finding-1>:
      grep recipe: grep -rn "<term>" docs/ <channel>/learnings/ web/README.md
      updated:  docs/<a>.md, <channel>/learnings/<b>.md
      created:  docs/<c>.md  (new — meta-pattern not previously covered)
      inline:   pipeline/<x>.py docstring extended
  - <finding-2>: no related docs touched — self-contained because
      <reason in one sentence>

MEMORY.md size: <N>KB / 24KB limit  [PASS|TRIAGE-NEEDED]

commit: <short-sha> "<commit-subject>" (or: "no commit — empty diff")
push:   <pushed | skipped because <reason>>

next: <suggested follow-up if any> | nothing — clean slate
```

If TRIAGE-NEEDED, list the top-5 longest entries and ask whether to
collapse them.

### 10. Self-learning hook (recursive — yes, this skill watches itself)

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
- **Always end with a `git add -A && git commit` (Section 8).** Stranded
  unstaged work is a CLASS-OF-BUG that bit `/update-docs` itself in
  May 2026. The skill's value is "the docs and the tree both reflect
  the new rule" — only the latter without the former is half a fix.

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
