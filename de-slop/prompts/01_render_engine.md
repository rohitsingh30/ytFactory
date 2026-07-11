You are ONE of several parallel agents de-slopping the ytFactory repo (an
AI-generated automated YouTube video pipeline). It is full of AI slop: docstrings
and comments that drifted from the code, references to deleted modules, dead code,
and aspirational comments for things never built. RULE: the function BODY is
authoritative — never trust a comment/docstring over the code.

VERIFIED architecture (rely on this — confirmed via production telemetry + code):
- cloud/render-worker-v2/entrypoint.py runs a 7-stage loop:
  rewrite → cast → images → tts → asr → compose → upload.
- The `compose` stage calls pipeline/render/video.py::render_via_engines(spec)
  IN-PROCESS (NOT a subprocess).
- render_via_engines → engine.py::pick_engine(spec) → short_engine.render_short OR
  long_engine.render_long → six plugin slots resolved via
  contracts.py::get_plugin(slot,name): audio, timeline, visualize, overlays(list),
  music, compose. Engines are BRANCHLESS (no `if visual_mode == ...`).
- Channel rules live in pipeline/channels/<ch>.yaml, never in code.

YOUR SCOPE — edit ONLY files under `pipeline/render/**`. Touch nothing else. Slop
you notice elsewhere → append ONE line to de-slop/HANDOFF.md, do not fix it.

KNOWN SLOP IN YOUR SCOPE (verify each against the code, then fix or delete):
- engine.py has transitional prose about `SPORTS_DOC` / `FOOTAGE_ONLY` "collapsing"
  and a "bigbang PR". Keep whatever enum values/behavior the code actually uses;
  DELETE speculative future-PR narration.
- contracts.py has a long History docstring (~lines 10–80). Trim to what is TRUE
  of the current code; drop the "before/after the 2026-05-14 landing" storytelling.
- CONFIRMED GONE: `pipeline/render/_legacy/` does NOT exist and
  `pipeline.render.{shorts,long_form,sports_doc,footage_only}` are deleted. Any
  mention is slop (e.g. the comment in pipeline/render/shared/caption_dispatch.py
  citing `_legacy/shorts.py`) — remove/repair it. The REAL library the plugins
  call is pipeline/render/shared/long_form_lib.py + footage_only_lib.py — keep those.
- Each slot dir (audio/ timeline/ visualize/ overlays/ music/ compose/) has module
  docstrings — make sure they name the impls that actually exist in that dir.

FIX (de-slop only — NOT a refactor, NOT new features):
1) Comments/docstrings contradicting code → correct to match, or delete.
2) References to deleted modules → grep-confirm, fix or remove.
3) Dead code: private symbol with 0 callers, or file with 0 importers → remove.
   BEFORE removing, `grep -rn "<name>" .` repo-wide; if used outside pipeline/render
   → DON'T remove, log to HANDOFF.md.
4) Always-on/off feature flags → collapse to real behavior.

GUARDRAILS: no behavior change; don't rename public symbols used outside your scope
(grep first); minimal surgical edits; keep the registry/Protocol design intact —
it is the GOOD part, protect it.
CAUTION: "legacy" is NOT always slop — some legacy names are LIVE compat code that
still runs (e.g. video.py calls env.to_legacy_long_form_dict()). Only remove legacy
references/comments that are provably dead (0 callers); never delete a live path.

VERIFY before finishing:
- `.venv/bin/python -c "import pipeline.render.video, pipeline.render.engine, pipeline.render.contracts, pipeline.render.short_engine, pipeline.render.long_engine"`
- `.venv/bin/pytest tests/ -q -k "render or engine or plugin or contract or spec or compose or overlay or timeline or visual"`
- Revert any change that turns a test red. Leave edits staged, uncommitted.

OUTPUT: bullets — files cleaned; dead code/files removed (+ proof of 0 callers);
anything logged to HANDOFF.md.
