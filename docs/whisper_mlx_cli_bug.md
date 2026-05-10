# whisper-mlx CLI module-mode is broken — use the Python API

`python -m mlx_whisper.transcribe` (mlx_whisper 0.4.3+) is **dead in
module mode**. It exits cleanly with a `RuntimeWarning` and produces
zero output files — no JSON, no error, no model-fetch.

## Symptom

```bash
$ .venv/bin/python -m mlx_whisper.transcribe \
    --model mlx-community/whisper-medium-mlx \
    --word-timestamps True \
    --output-dir . \
    --output-format json \
    audio.wav
<frozen runpy>:128: RuntimeWarning: 'mlx_whisper.transcribe' found in sys.modules after import of package 'mlx_whisper', but prior to execution of 'mlx_whisper.transcribe'; this may result in unpredictable behaviour
$ ls *.json
ls: *.json: No such file or directory
```

Exit code is 0. No traceback. No output files.

## Root cause

Upstream packaging bug. `mlx_whisper/__init__.py` imports the
`transcribe` submodule at package init time, putting it in
`sys.modules` before runpy gets a chance to execute it as
`__main__`. runpy then sees the module is already imported and
no-ops the CLI dispatch.

## Use the Python API instead

```python
import mlx_whisper
import json

result = mlx_whisper.transcribe(
    audio_path,
    path_or_hf_repo='mlx-community/whisper-medium-mlx',
    word_timestamps=True,
)
json.dump(result, open(out_path, 'w'))
```

This works on first call. The model fetches from HuggingFace on
first run (~42 s for whisper-medium-mlx, then cached).

## Affected callers

Any skill or helper that scripts whisper transcription:

- `/clone-video-format` — fixed 2026-05-08, SKILL.md step 4 now
  prescribes the Python API.
- `pipeline/audio.py::transcribe()` — does not currently expose
  mlx_whisper; safe.
- Any future skill that wants word-level timestamps locally on M1/M2 —
  use the Python API.

## When the upstream is fixed

If a future mlx_whisper release fixes module-mode invocation, the
CLI form is fine to use again. Until then, hard rule: **Python API
only**.

## Surfaced

2026-05-08 during /clone-video-format on the Mega Football Hearts
video. The first background CLI invocation produced no output. A
foreground retry with absolute paths reproduced the same silent-exit.
The Python API worked on first call.

## Memory mirror

[`feedback_mlx_whisper_cli_silent_exit.md`](/Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/feedback_mlx_whisper_cli_silent_exit.md)
