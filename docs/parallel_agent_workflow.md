# Parallel-agent workflow — git status BEFORE every edit

> **CLASS-OF-BUG, 2nd observation 2026-05-11.** Promoted from
> `update-docs/learnings/_index.md` ONE-OFF (2026-05-10) after the
> 2026-05-11 discover-route session surfaced the same hazard from a
> different angle.

## The hazard

The user runs **multiple agent sessions concurrently** against the
same working tree (different terminals, different
`/Users/rohit/.copilot/session-state/<id>/`). Two visible failure
modes so far:

1. **Stash-pop after parallel commit (2026-05-10).** Another agent
   commits while session A is mid-edit. Session A's WIP gets stashed
   when it tries to commit; the pop on resume can leave a mix of
   conflicts + silent overwrites. Several files silently re-overwritten
   that day; some of session A's fixes had to be re-applied.
2. **Pre-edited working tree (2026-05-11).** Session A starts on a
   "clean" assumption, edits `control/routes/discover_routes.py`,
   commits, deploys. Mid-deploy a re-read of the file shows
   *unfamiliar code* — turns out a parallel session had already
   added `field_validator` blocks + a `_safe_native_items_for`
   wrapper that session A was about to write. Session A had to
   reconcile (lucky: parallel work was a strict superset). Easily
   could have been a conflict.

## The rule

**Run `git status --short` IMMEDIATELY at the start of every session
on a long-running tree, and AGAIN before every commit.** Compare
against the user's stated mental model. Surface every uncommitted
file that wasn't yours — silently merging unrelated work is a
two-way trap (their work in your commit, your work in their commit).

```bash
# Session start — print everything not in HEAD
cd /Users/rohit/ytFactory
git --no-pager status --short          # full uncommitted list
git --no-pager log --oneline -5        # recent commits — was something just shipped?

# Before every commit
git --no-pager diff --cached --stat    # just-staged
git --no-pager status --short          # everything else not committed
```

If the working tree contains uncommitted changes that aren't yours:

1. **Read `git diff <file>` for each surprise change BEFORE staging
   anything.** Decide: include in this commit (mention in the
   message), or stage selectively (`git add -p`) to keep them out
   and let the other session ship them.
2. **Never `git add .`** on a tree with mystery changes. Always
   `git add <explicit paths>` for the work this session authored.
3. **Re-read the file you intend to edit immediately before editing.**
   The session's mental model of "current file state" can be out of
   date by minutes. Use `view` on the lines you're about to `edit`
   so the `old_str` lookup matches actual content (the discover-route
   `edit` call on 2026-05-11 failed silently with `No match found`
   because the file had been rewritten by the parallel session — caught
   by the next `view`, but cost a confused round-trip).

## Why this is CLASS-OF-BUG

The user has *already established* concurrent-session usage as a
preferred workflow (parallel /update-docs runs, parallel coding
agents, etc.). Per CLAUDE.md "default to write, not skip" — the
small overhead of a pre-edit `git status` is dwarfed by the cost of
a silent overwrite. The recurrence on 2026-05-11 confirms this is a
recurring class, not a one-off slip.

## When this rule does NOT apply

- Single-session tasks on a clean tree (`git status` is empty after
  the initial check).
- Pure read / exploration sessions that won't commit anything.

## Quality gate (mechanical)

For any session that intends to commit:

1. First `git status --short` printed in the conversation.
2. Pre-commit `git status --short` AND `git diff --cached --stat`
   printed before the commit message is finalized.
3. If mystery changes exist, the commit either explicitly stages
   only the session's own paths OR explicitly mentions the inherited
   changes in the commit message.

## Cross-references

- `~/.claude/projects/.../memory/feedback_parallel_agent_edit_detection.md`
  (terse pointer).
- `.claude/skills/update-docs/learnings/_index.md` 2026-05-10 entry
  (original one-off observation; promoted here today).
- CLAUDE.md "Persistence rule" (the dual-save rule that this doc is
  the project-side mirror for).

## Recurrence escalation

If a 3rd observation surfaces (especially a real conflict that costs
work), escalate to a hard quality gate inside relevant skills (e.g.
`/make-skill`, `/update-docs`) — auto-print `git status --short` as
the first tool call of the skill, before any read/edit.
