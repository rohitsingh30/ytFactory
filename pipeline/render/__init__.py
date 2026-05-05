"""pipeline.render — channel-agnostic render orchestrators.

* ``shorts``        — 1080×1920 9:16 short-form (the make_short() pipeline).
* ``long_form``     — 1920×1080 16:9 long-form image-panel + footage hybrid.
* ``footage_only``  — channel-agnostic 16:9 / 9:16 footage-only renderer.
* ``sports_doc``    — sports-specific long-form documentary orchestrator.

Each module exposes a ``cli_main()`` entry point that the matching
``scripts/`` shim invokes. ``cli_main()`` prints an OUTPUT_MANIFEST line
on stdout when the render finishes so the cloud worker
(``workers/heavy/render_short.py``) can locate the produced mp4 + thumb
without guessing per-channel paths.
"""

