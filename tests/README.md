# Tests

Unit tests for the spec interpreter, `make_spec`, and the autonomous-
pipeline modules under `pipeline/`. No TTS, no Whisper, no ffmpeg, no
`claude -p` subprocess — these run in a couple of seconds.

## Run

```bash
.venv/bin/python -m unittest discover tests/
```

Single file:

```bash
.venv/bin/python -m unittest tests.test_spec_lang
```

Single test:

```bash
.venv/bin/python -m unittest tests.test_spec_lang.ResolveValueSubstitutionTest.test_pure_substitution_preserves_lists
```

## What's covered

### Spec interpreter (`render_from_spec.py` + `make_spec.py`)

| File | What it covers |
|---|---|
| `test_spec_lang.py` | `_lookup_path`, `_eval_ast`, `resolve_value` — substitution + expression eval, list passthrough |
| `test_spec_time.py` | `resolve_time`, `resolve_visibility` — time tokens incl. `beat[i]` and arithmetic |
| `test_spec_color.py` | `_parse_color` — hex 3 / 6 / 8, tuples |
| `test_spec_layout.py` | `measure_primitive`, `expand_template`, `resolve_position` — auto-layout, `each` loops, anchors |
| `test_make_spec.py` | `substitute`, end-to-end `make_spec` against a synthetic channel template |
| `test_smoke.py` | `_deep_merge`, `_emoji_pop_scale_expr`, `draw_primitive` (PIL roundtrip), `render_template_to_png` |

### Autonomous pipeline (`pipeline/*.py`)

| File | What it covers |
|---|---|
| `test_pipeline_llm.py` | `_parse_inner_json` — fence stripping, balanced-brace fallback, error envelopes |
| `test_pipeline_script_check.py` | `check_script_text`, `check_beats` — CTA, hook, wedge, AITA-channel like/comment-split closer, slow_hook |
| `test_pipeline_visualizability.py` | `score_visualizability` — length, concrete nouns, action verbs, numbers, dialogue penalty |
| `test_pipeline_quality_gate.py` | `check_image` — file size, dimensions, all-black, all-white, corrupt files |
| `test_pipeline_prompts.py` | `_validate_and_clean` — count mismatch, missing scene, opening directives |
| `test_pipeline_cast.py` | `load_cast` — missing file, malformed JSON, missing description |
| `test_pipeline_critic.py` | `regenerate_with_corrections` — patches `prompts.json`, deletes affected `img_NN.png` for regen |

## What's not covered (intentionally — these are integration concerns)

- TTS audio generation (Kokoro / F5-TTS)
- Whisper / Parakeet word-timestamping
- `claude -p` subprocess calls (`pipeline/llm.call_claude_cli`, `pipeline/cast.author_cast`, `pipeline/prompts.author_beat_prompts`, `pipeline/critic.critique_short`)
- ffmpeg compose
- yt-dlp downloads (`pull_backgrounds.py`, `data/_cooking_bg_research.py`)
- Reddit / Wikipedia / TIH / YouTube source adapters
- Image gen (SDXL-Lightning, MFLUX) and IP-Adapter

For a manual end-to-end check on the spec-driven path:

```bash
SLUG=amitheasshole-aita-for-ruining-my-daughter-in-laws-birth-pla
.venv/bin/python make_spec.py \
    --channel channels/aita_cooking.yaml \
    --script  data/intermediate/aita_cooking/scripts/${SLUG}.json
.venv/bin/python render_from_spec.py \
    --spec    data/intermediate/aita_cooking/specs/${SLUG}.yaml
```

## Adding tests

- New pure-logic function? Add a focused unit test in the file matching
  the concern. New time tokens → `test_spec_time.py`. New primitive →
  `test_spec_layout.py` and `test_smoke.py`.
- Use `tests._helpers.FakeBeat` for any test that needs beat objects;
  it has the same `.start`/`.end`/`.duration` API as
  `pipeline.beats.Beat` without the Whisper dependency.
- For modules that transitively import torch (e.g. `pipeline.prompts`),
  pre-register a stub in `sys.modules` BEFORE the import — see
  `test_pipeline_prompts.py` for the pattern.
- Keep tests fast (<1s each). Use `tempfile.TemporaryDirectory()` for
  filesystem fixtures.
