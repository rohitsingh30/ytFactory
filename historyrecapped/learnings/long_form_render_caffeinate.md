# Long-form renders need caffeinate + unbuffered python

When launching `scripts/historyrecapped/render_long_form.py` for any
panels-mode (Z-Image-Turbo) episode, wrap with `caffeinate -i` and run
python with `-u`.

## Why

- Image-panel mode generates ~1 panel/min via MLX on M2 Max. An 89-panel
  WW1 episode is ~90 min of GPU work — easily long enough for the laptop
  to hit idle-sleep. When the system sleeps, the MLX/GPU context dies and
  the python process is silently killed. No stack trace, no log line —
  just gone. On 2026-05-04 the `western-front-1914-1918-sleep` render
  died this way twice before we realised sleep was the cause.
- Without `python -u`, stdout is fully buffered when redirected to a
  file. The structured `[panel] N/89 gen → panel_NNN.png` prints sit in
  the buffer and are lost when the process is SIGKILL'd, so the log
  stops mid-tqdm-bar with no clue about which panel was in flight.
  tqdm writes to stderr (line-buffered) so its progress bars survive,
  but the structured prints don't.

## How

```bash
nohup caffeinate -i .venv/bin/python -u \
    scripts/historyrecapped/render_long_form.py \
    --channel historyrecapped --slug <slug> \
    > logs/<slug>.log 2>&1 &
disown
```

- `caffeinate -i` blocks idle-sleep only — display can still sleep,
  which is fine. Caffeinate exits with the wrapped command; no cleanup.
- `-u` forces unbuffered stdout/stderr (alt: `PYTHONUNBUFFERED=1`).
- All stages of `render_long_form.py` are cache-aware (panel PNGs at
  `cache/<slug>/panels/panel_NNN.png`, TTS chunks at
  `cache/<slug>/tts_chunks/`, Ken-Burns segments at
  `cache/<slug>/panel_segments/`). Re-launching after a kill just
  resumes from the next missing artefact — but only if you can SEE
  where it died, hence the unbuffered logs.

## Scope

Applies to any multi-hour ytFactory render that touches MLX/GPU:

- long-form sleep episodes (panels mode)
- large Shorts batches using `z_image_turbo` / `mflux`
- AnimateDiff renders

Short single-Short renders (sub-5-min wall time) don't need this.
