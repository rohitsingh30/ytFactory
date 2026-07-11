# de-slop HANDOFF — out-of-scope slop noticed while cleaning pipeline/render/**

- pipeline/llm/script_schema.py:115 — docstring references the unified
  `pipeline.render.video.render(spec)` orchestrator, but `render()` no longer
  exists (only `render_via_engines` / `render_long_form`). Update the reference.
- pipeline/render/video.py — the subprocess-teeing helpers (`_stream_subprocess`,
  `_maybe_emit_long_form_progress`, `_format_subprocess_failure`,
  `_extract_last_traceback`, `_is_telemetry_traceback`) are production-dead:
  long-form now renders in-process, nothing shells out. They are kept ONLY because
  tests/test_render_video.py + tests/test_otel_*.py pin their behavior (incl. the
  exact string "pipeline.render.long_form exited with code N"). If those tests are
  retired, this whole block + the `subprocess`/`shutil`/`sys` imports can be deleted.
- pipeline/render/input_registry.py::long_form_overlay_from_spec is production-dead
  (no non-test caller) but retained because tests/test_input_registry.py pins it.
