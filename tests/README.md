# Tests

Unit tests for the autonomous-pipeline modules under `pipeline/`. No
TTS, no Whisper, no ffmpeg, no `claude -p` subprocess — these run in
a couple of seconds.

## Run

```bash
.venv/bin/python -m unittest discover tests/
```

Single file:

```bash
.venv/bin/python -m unittest tests.test_pipeline_llm
```

Single test:

```bash
.venv/bin/python -m unittest tests.test_pipeline_llm.ParseInnerJsonTest.test_balanced_brace_fallback
```

## What's covered

| File | What it covers |
|---|---|
| `test_pipeline_llm.py` | `_parse_inner_json` — fence stripping, balanced-brace fallback, error envelopes |
| `test_pipeline_script_check.py` | `check_script_text`, `check_beats` — CTA, hook, wedge, AITA closer, slow_hook |
| `test_pipeline_visualizability.py` | `score_visualizability` — length, concrete nouns, action verbs, dialogue penalty |
| `test_pipeline_quality_gate.py` | `check_image` — file size, dimensions, all-black, all-white, corrupt |
| `test_pipeline_prompts.py` | `_validate_and_clean` — count mismatch, missing scene, opening directives |
| `test_pipeline_cast.py` | `load_cast` — missing file, malformed JSON, missing description |
| `test_pipeline_critic.py` | `regenerate_with_corrections` — patches prompts.json, deletes affected images |

## What's not covered (intentionally — these are integration concerns)

- TTS audio generation (Kokoro / F5-TTS)
- Whisper / Parakeet word-timestamping
- `claude -p` subprocess calls
- ffmpeg compose
- yt-dlp downloads
- Reddit / Wikipedia / TIH / YouTube source adapters
- Image gen (Z-Image-Turbo, MFLUX, SDXL) and IP-Adapter

## Adding tests

- New pure-logic function? Add a focused unit test in the file
  matching the concern.
- Use `tests._helpers.FakeBeat` for any test that needs beat objects;
  it has the same `.start`/`.end`/`.duration` API as
  `pipeline.beats.Beat` without the Whisper dependency.
- For modules that transitively import torch (e.g. `pipeline.prompts`),
  pre-register a stub in `sys.modules` BEFORE the import — see
  `test_pipeline_prompts.py` for the pattern.
- Keep tests fast (<1s each). Use `tempfile.TemporaryDirectory()` for
  filesystem fixtures.
