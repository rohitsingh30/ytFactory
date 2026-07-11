You are ONE of several parallel agents de-slopping the ytFactory repo (an
AI-generated automated YouTube video pipeline). It is full of AI slop: drifted
docstrings/comments, references to deleted modules, dead code, aspirational
comments. RULE: the function BODY is authoritative — never trust a comment over
the code.

VERIFIED architecture (rely on this):
- control/ is the laptop CONTROL PLANE (FastAPI + Firestore queue + scheduler). It
  NEVER renders. It writes a Firestore job doc, then triggers the Cloud Run Job;
  the two planes communicate only through Firestore (state) + GCS (artifacts).
- The canonical layout is control/core/* (state) + control/routes/* (FastAPI
  routers). A legacy flat layout was removed in an earlier refactor.

YOUR SCOPE — edit ONLY `control/**`. Nothing else. Slop elsewhere → de-slop/HANDOFF.md.

KNOWN SLOP IN YOUR SCOPE (verify, then fix/delete):
- control/render_routes.py (top-level) is LIKELY dead — both servers appear to use
  control/routes/render_routes.py. CONFIRM with `grep -rn "control.render_routes\|from control import render_routes\|control/render_routes" .`; if it has zero
  importers, remove it. If unsure, log to HANDOFF.md instead of deleting.
- core/ vs routes/ docstrings that still mention the old flat modules.
- The *.plist files reference script paths — verify those targets still exist; fix
  or note stale ones in HANDOFF.md (don't delete plists).

FIX (de-slop only — no refactor, no features):
1) Comments/docstrings contradicting code → fix or delete.
2) References to deleted modules → grep-confirm, fix or remove.
3) Dead code: private symbol with 0 callers, or file with 0 importers → remove.
   BEFORE removing: `grep -rn "<name>" .` repo-wide; if used outside control/ →
   log to HANDOFF.md.

GUARDRAILS: no behavior change; don't rename public symbols/routes used elsewhere
(grep first); surgical edits only.

VERIFY before finishing:
- `.venv/bin/python -c "import control.core, control.routes"` (and any module you edited)
- `.venv/bin/pytest tests/ -q -k "control or route or jobs or queue or scheduler or reconcil"`
- Revert anything that goes red. Leave edits staged, uncommitted.

OUTPUT: bullets — files cleaned; dead code/files removed (+ proof of 0 callers);
HANDOFF.md notes.
