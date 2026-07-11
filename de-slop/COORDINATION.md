# De-slop — parallel CLI coordination

Goal: strip project-wide AI slop (drifted docstrings, dead code, references to
deleted modules, aspirational comments) so the repo is clean enough to walk
through on camera for HLD / LLD / demo. **The function body is authoritative —
never trust a comment over the code.**

Not a rewrite. No behavior changes. Cosmetic + dead-code removal + truthful docs.

## Partition map (disjoint — one owner per path)

| CLI | Prompt file | Owns (edit ONLY these) | Priority |
|----|----|----|----|
| 01 | prompts/01_render_engine.md | `pipeline/render/**` (the LLD core) | P0 |
| 02 | prompts/02_llm_images.md | `pipeline/llm/**` + `pipeline/images/**` | P1 |
| 03 | prompts/03_pipeline_media.md | pipeline media modules (see prompt list) | P2 |
| 04 | prompts/04_pipeline_platform.md | pipeline config/support (remainder of `pipeline/`) | P2 |
| 05 | prompts/05_cloud_services.md | `cloud/**` (worker entrypoint + GPU services) | P0 |
| 06 | prompts/06_control_plane.md | `control/**` | P1 |
| 07 | prompts/07_web_scripts.md | `web/**` + `web-next/**` + `scripts/**` | P3 |
| 08 | prompts/08_docs_rebuild.md | `ai/**` + root explainer docs → regenerate | P1, **runs LAST** |

`pipeline/voice_refs/**` is audio DATA, not code — nobody edits it.

## How to run in parallel (git worktree per CLI — no file contention)

```bash
cd /Users/rohit/ytFactory
for id in 01 02 03 04 05 06 07; do
  git worktree add "../ytf-$id" -b "deslop/$id" 2>/dev/null || true
done
# then, in a separate terminal per CLI, cd into its worktree and feed its prompt:
#   cd ../ytf-01 && copilot -p "$(cat /Users/rohit/ytFactory/de-slop/prompts/01_render_engine.md)"
```

If you skip worktrees and run in one tree: still safe because scopes are
disjoint — but do NOT run `git commit` from two CLIs at once.

## Run order
1. CLIs **01–07 in parallel**.
2. **Integration pass** (below) — merge, full test suite, resolve HANDOFF.md.
3. **CLI 08 (docs)** last, against the now-clean code.

## Cross-scope findings → HANDOFF.md
If a CLI spots slop, a dead symbol, or a needed fix OUTSIDE its scope, it appends
ONE line to `de-slop/HANDOFF.md` (owner path + finding) and moves on. It does not
reach across the boundary.

## Integration pass (run after 01–07)
```bash
cd /Users/rohit/ytFactory
for id in 01 02 03 04 05 06 07; do git merge --no-ff "deslop/$id"; done   # resolve any conflicts (should be none if scopes held)
.venv/bin/pytest tests/ -x -q                                            # must be green
```
Then process every line in `de-slop/HANDOFF.md`, then run CLI 08.

## Non-negotiable guardrails (every CLI)
- No runtime behavior change. Cosmetic + dead-code only.
- Before deleting any symbol/file: `grep -rn "<name>" .` repo-wide. If used
  outside your scope → DON'T delete, log to HANDOFF.md.
- "legacy"/"deprecated" in a name is NOT proof of death — several are LIVE compat
  paths (to_legacy_long_form_dict, YTFACTORY_REWRITE_USE_LEGACY, _LEGACY_PROVIDER_MAP).
  Only remove references with 0 repo-wide callers; never delete a running path.
- Never touch `deploy.sh` cost flags (`min-instances` / `max-instances` /
  `--region`) — load-bearing (see CLAUDE.md cost rules).
- Never change channel/variant YAML semantics.
- Verify (tests / py_compile) before finishing; revert anything that goes red.
