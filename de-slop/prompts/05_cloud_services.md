You are ONE of several parallel agents de-slopping the ytFactory repo (an
AI-generated automated YouTube video pipeline). It is full of AI slop: drifted
docstrings/comments, references to deleted modules, dead code, aspirational
comments. RULE: the function BODY is authoritative — never trust a comment over
the code.

VERIFIED architecture (rely on this — confirmed via production telemetry + code):
- cloud/render-worker-v2/entrypoint.py runs a 7-stage loop:
  rewrite → cast → images → tts → asr → compose → upload.
- The `compose` stage calls pipeline/render/video.py::render_via_engines(spec)
  IN-PROCESS. There is NO subprocess shell-out to a renderer.
- LIVE deployed services only: render-worker-v2, image-z-image-turbo,
  tts-chatterbox, tts-indicf5, asr-whisper, editing-agent, clone-video-worker,
  web-server, web-next. Any other service mentioned is RETIRED.

YOUR SCOPE — edit ONLY `cloud/**`. Nothing else. Slop elsewhere → de-slop/HANDOFF.md.

KNOWN SLOP IN YOUR SCOPE (HIGH-CONFIDENCE — verify line numbers may have shifted):
- entrypoint.py TOP module docstring (~lines 9 and 24–27) claims real mode "shells
  out to `python -m pipeline.render.shorts`". This is FALSE and the module doesn't
  exist. The real path calls render_via_engines IN-PROCESS (see the notes near
  ~lines 1889–1890 and ~2472–2478). Rewrite the top docstring to describe the
  actual in-process engine call.
- The `_RENDERER_SUBSTAGES` / subprocess-tailer comments (~lines 1495–1520) and the
  `YTFACTORY_USE_ENGINES` gate description (~line 2471) still describe the old
  subprocess renderer — reconcile to the in-process reality.
- The comment near ~line 1891 says plugins "still delegate to
  pipeline/render/_legacy/{shorts,long_form,sports_doc,footage_only}.py" —
  CONFIRMED FALSE: that package was deleted (`ls pipeline/render/_legacy/` → no
  such dir). Rewrite/remove it; the real delegation target is
  pipeline/render/shared/long_form_lib.py + footage_only_lib.py.
- Retired-service references in cloud/_shared/ and any deploy.sh.

FIX (de-slop only — no refactor, no features):
1) Comments/docstrings contradicting code → fix or delete.
2) References to deleted modules/services → grep-confirm, fix or remove.
3) Dead code / stub branches with 0 reachable callers → remove. BEFORE removing:
   `grep -rn "<name>" .` repo-wide; if used outside cloud/ → log to HANDOFF.md.

GUARDRAILS:
- NO behavior change.
- **Do NOT touch deploy.sh cost flags**: `min-instances`, `max-instances`,
  `--region` are load-bearing (CLAUDE.md cost rules). Leave them exactly as-is.
- Don't rename public symbols used outside cloud/ (grep first).

VERIFY before finishing (cloud modules need cloud deps — prefer py_compile):
- `python -m py_compile cloud/render-worker-v2/entrypoint.py` and any other .py you edited.
- `.venv/bin/pytest tests/ -q -k "cloud or worker or entrypoint or render_worker"`
- Revert anything that goes red. Leave edits staged, uncommitted.

OUTPUT: bullets — files cleaned; dead code removed (+ proof of 0 callers);
HANDOFF.md notes. Call out the entrypoint.py docstring fix explicitly.
