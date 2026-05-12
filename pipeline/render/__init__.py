"""pipeline.render — channel-agnostic render orchestrators.

The unified entry point is :mod:`pipeline.render.video`. Every render
in the cloud worker goes through ``video.render(spec)`` post-2026-05-12.

The four legacy modules exist as the underlying executors that
``video.render`` dispatches to by ``spec.kind``. They retain their
``cli_main()`` argparse entry points so the laptop CLI workflow
(``python -m pipeline.render.shorts --script …``) continues to work
unchanged. Cloud renders skip the CLI shim and call ``video.render``
directly with a typed ``RenderSpec``.

* :mod:`.video`         — unified orchestrator. Dispatches by
                          ``spec.kind`` ∈ {short, long_form, sports_doc,
                          footage_only}. Single entry the cloud worker
                          and any new caller use.
* :mod:`.spec`          — typed RenderSpec dataclass + builder.
                          Channel YAML + variant overlay + form overrides
                          + inferred defaults → one fully-typed spec.
* :mod:`.shorts`        — 9:16 short-form (the make_short pipeline).
                          Owns rewrite/cast/compose for short kind.
                          Resolution-aware via :class:`compose.Resolution`.
* :mod:`.long_form`     — 16:9 long-form image-panel OR archival-footage
                          hybrid. Owns chunked TTS + panel/shotlist
                          dispatch.
* :mod:`.sports_doc`    — sports-specific long-form documentary
                          (chaptered narration + footage_plan + overlay
                          timeline). NOT collapsible into long_form's
                          panel pipeline — see Slice 1 rubber-duck
                          critique.
* :mod:`.footage_only`  — channel-agnostic 16:9 / 9:16 footage-only
                          renderer (no AI image gen).
* :mod:`.artifacts`     — emit_artifact helper. Stages call it to
                          upload intermediates to GCS + flag them on
                          the Firestore job doc the moment they're
                          produced.

Each legacy module's ``cli_main()`` prints an OUTPUT_MANIFEST line on
stdout when the render finishes so the cloud worker
(``workers/heavy/render_short.py``) can locate the produced mp4 +
thumb without guessing per-channel paths.
"""

