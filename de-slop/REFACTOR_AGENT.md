# Refactor Agent — run one per package (parallel)

You are ONE agent executing the `pipeline/` restructure defined in
`de-slop/REFACTOR_PLAN.md`. You own **exactly one target package** (told to you as
"you are Agent X → package `<name>`"). Read your rows in REFACTOR_PLAN.md §2 for the
files that move into your package.

Goal: finish the half-done flat→nested migration — **`git mv` + repoint references +
delete dead/shim files + clean the code inside**. NO logic changes.

## Steps (in order)
1. **Move** every file assigned to your package with `git mv` (preserve history):
   `git mv pipeline/<old>.py pipeline/<pkg>/<mod>.py`
2. **Repoint EVERY reference repo-wide** for each moved module — not just imports:
   - imports: `from pipeline.<old> import X` / `import pipeline.<old>` → new path
   - **test mock.patch strings**: `mock.patch("pipeline.<old>.func")` → new path (critical — misses here break tests)
   - docstring/comment references
   Find them all: `grep -rn "pipeline\.<old>\b" . --include='*.py'` (+ check web-next `.ts/.tsx` for doc refs).
3. **Delete** the shim files your package's canonicals replace (the flat/nested
   duplicates listed in REFACTOR_PLAN §2) and any dead file in your scope
   (0 importers, verified by grep).
4. **Clean the code inside** each moved file: fix docstrings/comments that contradict
   the code, delete dead private symbols (0 callers, grep-verified), remove
   references to deleted modules. No behavior change.
5. **Update `__init__.py`** — if you moved a package that re-exported a public API
   (e.g. `pipeline.publish`), preserve the public import via the new package's
   `__init__` or repoint callers to the concrete module.

## Guardrails
- `git mv` only — never delete-and-recreate (loses history).
- Do NOT touch files outside your package's move set. A file imported by TWO moving
  packages = the one conflict → append a line to `de-slop/REFACTOR_HANDOFF.md`, don't fight it.
- No channel/variant YAML semantic changes. Don't touch `deploy.sh` cost flags.
- "legacy"/"deprecated" names may be LIVE compat — only delete provably-dead refs.

## Verify before finishing (REQUIRED)
- `.venv/bin/python -c "import pipeline.<pkg>"` (whole package imports)
- `python -m py_compile` on every file you moved
- **Zero surviving old paths:** `grep -rn "pipeline\.<old>\b" . --include='*.py' | grep -v __pycache__` must be empty for each module you moved
- Revert any change that won't import.

## Output
Bullet list: files moved (old→new), shims/dead files deleted (+ proof 0 refs),
code cleaned, anything logged to REFACTOR_HANDOFF.md.

---
**Package assignments** (REFACTOR_PLAN §5): A=sources · B=authoring · C=audio ·
D=images · E=footage · F=upload+critique+qa · G=config · H=infra.
Run A–H in git-worktrees (see `de-slop/COORDINATION.md`), then the integration pass.
