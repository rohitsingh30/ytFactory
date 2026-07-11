You are ONE of several parallel agents de-slopping the ytFactory repo (an
AI-generated automated YouTube video pipeline). It is full of AI slop: drifted
docstrings/comments, references to deleted modules, dead code, aspirational
comments. RULE: the function BODY is authoritative — never trust a comment over
the code.

VERIFIED architecture (rely on this):
- web/ = legacy FastAPI server (Cloud Run `web-server`). web-next/ = Next.js
  admin/wizard UI (Cloud Run `web-next`). scripts/ = laptop CLI entry points.
- The render path is in-process render_via_engines inside the cloud worker; any
  script referencing `pipeline.render.{shorts,long_form,sports_doc,footage_only}`
  is calling a DELETED module and is stale.

YOUR SCOPE — edit ONLY `web/**`, `web-next/**`, and `scripts/**`. Nothing else.
Slop elsewhere → one line in de-slop/HANDOFF.md.

FIX (de-slop only — no refactor, no features):
1) Comments/docstrings contradicting code → fix or delete.
2) Scripts importing deleted modules → grep-confirm the import target exists; if the
   whole script is dead (0 callers, broken imports, references retired pipelines),
   remove it. BEFORE removing: `grep -rn "<script-name>" .` (Makefile, docs, other
   scripts). If referenced → log to HANDOFF.md instead.
3) Stale README/UI copy that describes removed features.
4) Always-on/off flags → collapse to real behavior.

GUARDRAILS: no behavior change; don't rename public symbols/endpoints used elsewhere
(grep first); surgical edits only; don't change build config or dependencies.

VERIFY before finishing:
- Python scripts: `python -m py_compile <file>` for each you edited.
- web/ (FastAPI): `.venv/bin/python -c "import web.<module>"` where importable.
- web-next/ (TypeScript): if tooling is present, run `npm run -s typecheck` or
  `npx tsc --noEmit` in web-next/ and ensure no NEW errors vs baseline.
- `.venv/bin/pytest tests/ -q -k "web or script or cli"` (if such tests exist).
- Revert anything that goes red. Leave edits staged, uncommitted.

OUTPUT: bullets — files cleaned; dead scripts/files removed (+ proof of 0 callers);
HANDOFF.md notes.
