# /editing-agent — learnings index

Append-only log of regressions caught by `/critique-video` (or by the
user) on outputs of this skill. Format:

```
- YYYY-MM-DD — class | one-off — what fired — fix location
```

Class-of-bug entries also live in
`~/.claude/projects/-Users-rohit-ytFactory/memory/skill_editing_agent_<topic>.md`
per CLAUDE.md dual-save rule.

## Open questions

- LUT library lives where? Decision: `pipeline/editing/luts/*.cube`
  shipped in the cloud container image; laptop reads from same path
  via `pipeline.paths`.
- Music library policy per channel — TBD when first non-bare run
  happens.

## Regression log

(none yet — first run will populate this)

## Pre-shipping notes

- 2026-05-11 — class — `auto-editor` was scoped out of v1 before
  any cloud build ran. Its PyPI metadata declares `Requires-Dist:
  pyav==13.1.*` but PyAV is published on PyPI as `av`, not `pyav`,
  and pip's resolver doesn't bridge the alias. Caught by the
  mandatory pip dry-run from `docs/cloud_service_dep_playbook.md`
  (steps 4-5). Fix: drop auto-editor entirely. The LLM planner
  emits explicit trim filters per shot, so we don't need
  auto-editor's silence-cut heuristic. PySceneDetect alone covers
  scene-boundary detection for assemble-clips mode.
  Files updated: `cloud/editing-agent/requirements.txt`,
  `cloud/editing-agent/Dockerfile`, `SKILL.md`,
  `docs/editing_agent.md`, `~/.claude/.../skill_editing_agent.md`.
