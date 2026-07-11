You are ONE of several parallel agents de-slopping the ytFactory repo (an
AI-generated automated YouTube video pipeline). It is full of AI slop: drifted
docstrings/comments, references to deleted modules, dead code, aspirational
comments. RULE: the function BODY is authoritative — never trust a comment over
the code.

VERIFIED architecture (rely on this):
- Channel rules live in pipeline/channels/<ch>.yaml + variant overlays in
  pipeline/variants/<ch>/*.yaml. NEVER restate channel rules in code.
- LIVE Cloud Run env vars (any others in comments are RETIRED services):
  CLOUDRUN_TTS_CHATTERBOX_URL, CLOUDRUN_TTS_INDICF5_URL,
  CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL, CLOUDRUN_ASR_URL, CLOUDRUN_EDITING_AGENT_URL,
  CLOUDRUN_CLONE_VIDEO_URL, CLOUDRUN_WEB_SERVER_URL, CLOUDRUN_WEB_NEXT_URL.

YOUR SCOPE — edit the REMAINDER of `pipeline/` NOT owned by other CLIs. Specifically:
  channels/  channels.py  channels.yaml  variants/  sources/  niches.py
  niche_specs.py  era_anchor.py  era_taxonomy.yaml  schemas/  upload/  publish/
  observability/  telemetry.py  cloud/  cloudrun_auth.py  auth/  utils/  text/
  quality/  critique/  research/  paths.py  parallel.py  preflight.py  probe.py
  stage_overlap.py
Do NOT touch: pipeline/render/, pipeline/llm/, pipeline/images/, or the CLI-03
media set (audio, tts, voice, footage, social, editing, asr*.py, align.py,
transcribe.py, captions.py, thumbnails.py, compose.py, beats.py, voice_clone.py,
cosmos_footage_prep.py, critic_long_form.py), or pipeline/voice_refs/ (DATA).
Slop elsewhere → one line in de-slop/HANDOFF.md.

KNOWN SLOP (verify against code, then fix/delete):
- Comments/env-var lists referencing RETIRED Cloud Run services (see live list).
- channels.py vs channels.yaml: reconcile any comment about the rotation/rules with
  what channels.yaml actually declares.
- Dead source adapters / niche specs with no reachable caller.

FIX (de-slop only — no refactor, no features):
1) Comments/docstrings contradicting code → fix or delete.
2) References to deleted modules/services → grep-confirm, fix or remove.
3) Dead code: private symbol with 0 callers, or file with 0 importers → remove.
   BEFORE removing: `grep -rn "<name>" .` repo-wide; if used outside your scope →
   log to HANDOFF.md.
4) Always-on/off flags → collapse to real behavior.

GUARDRAILS: no behavior change; don't change channel/variant YAML semantics; don't
rename public symbols used elsewhere (grep first); surgical edits only.
CAUTION: "legacy"/"deprecated" in a name is NOT proof it's dead — some are live
compat paths. Only remove references with 0 repo-wide callers; never a live path.

VERIFY before finishing:
- `.venv/bin/python -c "import pipeline.paths, pipeline.channels, pipeline.niches, pipeline.stage_overlap"`
- `.venv/bin/pytest tests/ -q -k "channel or source or upload or niche or observability or telemetry or path or schema"`
- Revert anything that goes red. Leave edits staged, uncommitted.

OUTPUT: bullets — files cleaned; dead code/files removed (+ proof of 0 callers);
HANDOFF.md notes.
