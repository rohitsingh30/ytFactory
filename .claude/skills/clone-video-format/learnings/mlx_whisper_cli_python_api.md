# whisper-mlx: use Python API, not CLI module-mode

**Surfaced:** 2026-05-08, /clone-video-format Hearts run.

**Symptom:** `python -m mlx_whisper.transcribe ...` exits with a
RuntimeWarning and produces no output files. CLI is dead.

**Classification:** CLASS-OF-BUG. Affects /clone-video-format step 4
and any future skill scripting whisper-mlx transcription.

**Fix shipped 2026-05-08:** SKILL.md step 4 updated to use the
Python API:

```python
import mlx_whisper, json
result = mlx_whisper.transcribe(
    audio_path,
    path_or_hf_repo='mlx-community/whisper-medium-mlx',
    word_timestamps=True,
)
json.dump(result, open(out_path, 'w'))
```

**Cross-channel doc (full reference):**
[`docs/whisper_mlx_cli_bug.md`](/Users/rohit/ytFactory/docs/whisper_mlx_cli_bug.md)

**Memory mirror:**
[`feedback_mlx_whisper_cli_silent_exit.md`](/Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/feedback_mlx_whisper_cli_silent_exit.md)
