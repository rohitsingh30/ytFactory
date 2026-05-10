# /update-docs — related-doc sweep rule (CLASS-OF-BUG, 2026-05-10)

## What the user caught

After running `/update-docs` on the jobs-snapshot-unification work,
the user said "update the skill to not just update the memory, update
all related docs or create new ones".

What the skill DID save:
- `docs/jobs_snapshot_unification.md` (new project doc)
- `~/.claude/projects/.../memory/feedback_jobs_snapshot_unification.md`
- One MEMORY.md index line
- Cross-link added to `docs/full_cloud_cutover_2026_05_09.md` (because
  I happened to grep for it during the work)
- Inline edit to `docs/architecture.md` stale URL (because I happened
  to grep during a smoke-test failure)

What the skill DIDN'T do, but should have:
- A **systematic** grep across `docs/`, `<channel>/learnings/`,
  `web/README.md`, `cloud/*/README.md`, related skill SKILL.mds, and
  inline `pipeline/<module>.py` docstrings, scoring each hit for
  "stale claim" / "missing cross-reference" / "should exist but
  doesn't".
- The two related-doc updates that DID happen were ad-hoc, not driven
  by the skill's checklist. Easy to skip on the next run if the topic
  doesn't naturally surface during the work.

## The new rule (added to SKILL.md as Section 4D + Quality gate 8)

For every finding, before reporting done:

1. Run a sweep grep on every term naming an affected
   module/endpoint/topic across `docs/`, `<channel>/learnings/`,
   `web/README.md`, `cloud/*/README.md`, related SKILL.mds, and
   inline pipeline docstrings.
2. For each hit, decide one of:
   - update inline if stale,
   - add cross-reference if accurate-but-missing the new finding,
   - create a new doc if a meta-pattern surfaced (e.g. "every doc
     naming a Cloud Run URL should be audited for staleness"),
   - leave alone if genuinely unrelated.
3. The report MUST list every related doc updated/created (or
   explicitly state "self-contained because <reason>") with the grep
   recipe shown so the next session can re-run it. Quality gate 8
   blocks on missing.

## Why this is CLASS-OF-BUG, not ONE-OFF

The CLAUDE.md "save to BOTH" rule already established that
memory-only writes are a violation. The same logic extends past 2
saves to N: a finding lives across every doc that names the touched
module / endpoint / topic, and a "fix in one place" leaves the docs
tree internally inconsistent — the next reader landing on a stale
sibling makes the wrong call. Without an explicit sweep step, the
skill defaults to "the place I happened to think of" — which is what
this run did and which won't catch the next siblings.

## Cross-references

- `.claude/skills/update-docs/SKILL.md` § Section 4D — the rule
- `.claude/skills/update-docs/SKILL.md` § Quality gate 8 — the gate
- `~/.claude/projects/.../memory/feedback_update_docs_doc_sweep.md` —
  terse memory pointer
- MEMORY.md — index line under cross-channel learnings
