You are ONE of several parallel agents de-slopping the ytFactory repo (an
AI-generated automated YouTube video pipeline). It is full of AI slop: drifted
docstrings/comments, references to deleted modules, dead code, aspirational
comments. RULE: the function BODY is authoritative — never trust a comment over
the code.

VERIFIED architecture (rely on this):
- 7-stage worker: rewrite → cast → images → tts → asr → compose → upload.
- LIVE media services only: TTS = Chatterbox (English) + IndicF5 (Hindi);
  ASR = faster-whisper. Env vars: CLOUDRUN_TTS_CHATTERBOX_URL,
  CLOUDRUN_TTS_INDICF5_URL, CLOUDRUN_ASR_URL. Any OTHER TTS/ASR provider mentioned
  in comments (higgs, cosyvoice, indicparler, f5, etc.) is RETIRED — treat mentions
  as stale unless the code path is actually reachable.

YOUR SCOPE — edit ONLY these under `pipeline/`:
  audio/  tts/  voice/  footage/  social/  editing/
  asr.py  asr_cloudrun.py  align.py  transcribe.py  captions.py  thumbnails.py
  compose.py  beats.py  voice_clone.py  cosmos_footage_prep.py  critic_long_form.py
Do NOT touch pipeline/render/, pipeline/llm/, pipeline/images/ (other CLIs own
them) or pipeline/voice_refs/ (audio DATA). Slop elsewhere → de-slop/HANDOFF.md.

FIX (de-slop only — no refactor, no features):
1) Comments/docstrings contradicting code → fix or delete. Kill mentions of retired
   TTS/ASR providers that no live code path reaches.
2) References to deleted modules → grep-confirm, fix or remove.
3) Dead code: private symbol with 0 callers, or whole file with 0 importers →
   remove. BEFORE removing: `grep -rn "<name>" .` repo-wide; if used outside your
   scope → log to HANDOFF.md instead.
4) Always-on/off flags → collapse to real behavior.

GUARDRAILS: no behavior change; don't rename public symbols used elsewhere (grep
first); surgical edits only.
CAUTION: "legacy" is NOT always slop — e.g. pipeline/asr.py's _LEGACY_PROVIDER_MAP
is a REAL provider-name compat map; keep it. Only delete provably-dead references.

VERIFY before finishing:
- Smoke-import the modules you edited, e.g.
  `.venv/bin/python -c "import pipeline.captions, pipeline.compose, pipeline.beats, pipeline.asr_cloudrun"`
- `.venv/bin/pytest tests/ -q -k "tts or asr or audio or caption or voice or compose or thumbnail or footage"`
- Revert anything that goes red. Leave edits staged, uncommitted.

OUTPUT: bullets — files cleaned; dead code/files removed (+ proof of 0 callers);
HANDOFF.md notes.
