"""pipeline.render — channel-agnostic render orchestrators.

The unified entry point is :mod:`pipeline.render.video`. Every render
in the cloud worker goes through ``video.render_via_engines(spec)``,
which dispatches by ``spec.kind`` to one of two engines.

* :mod:`.video`      — unified orchestrator / public entry. Builds the
                       long-form narration when needed, then calls the
                       engines.
* :mod:`.spec`       — typed RenderSpec dataclass + builder. Channel
                       YAML + variant overlay + form overrides +
                       inferred defaults → one fully-typed spec.
* :mod:`.engine`     — :func:`pick_engine` routes ``spec.kind`` to the
                       short or long engine.
* :mod:`.short_engine` / :mod:`.long_engine` — the two engines. Both
                       are branchless: they resolve the six plugin
                       slots (audio, timeline, visualize, overlays,
                       music, compose) via :mod:`.contracts` and call
                       them.
* :mod:`.contracts`  — Protocol definitions + the plugin registry.
* :mod:`.artifacts`  — emit_artifact helper. Stages call it to upload
                       intermediates to GCS + flag them on the Firestore
                       job doc the moment they're produced.
"""

