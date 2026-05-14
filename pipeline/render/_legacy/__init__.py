"""Private legacy renderer helpers — kept for plugin convenience.

Pre-2026-05-14 these modules WERE the renderer orchestration. The
4-renderer-to-2-engine consolidation (see docs/render_engines_2026.md)
deleted their orchestration entry points (cli_main / _main_impl /
main) and moved the helpers here so plugins can keep importing
them while bigbang-follow-up inlines them function-by-function.

These modules MUST NOT be imported from anywhere outside
pipeline/render/{audio,timeline,visualize,overlays,music,compose}/
plugin modules. Touch sites are tracked in plan.md; each
inlining shrinks this dir.
"""
