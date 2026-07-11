You are the FINAL agent in a parallel de-slop of the ytFactory repo. Run me LAST,
after CLIs 01–07 have cleaned the code and the integration test pass is green.

The existing explainer/knowledge docs are AI slop and NOT source of truth. Your job:
archive them, then regenerate a SMALL, TRUTHFUL doc set derived ONLY from the actual
code. RULE: verify every claim by opening the referenced file; if you can't confirm
it in code, don't write it. No aspirational statements, no invented file:line refs.

YOUR SCOPE — you own: `ai/**`, and these root files/dirs: `HLD.md`, `CODE_FLOW.md`,
`HLD_diagram.md`, `video_script.md`, `design-deck/`. You MAY also fix references in
`README.md` and `CLAUDE.md` that point to files removed by CLIs 01–07 (read
de-slop/HANDOFF.md for the removal list). Do NOT edit code. Do NOT touch
docs/render_telemetry.md, docs/cost_optimized_deploy.md, docs/optimization_checklist.md
(those are operational and cited by CLAUDE.md) — only verify their links resolve.

STEP 1 — archive the slop (reversible, lose nothing):
- `mkdir -p de-slop/_archive_untracked` and `git mv`/`mv` the untracked root slop
  (HLD.md, CODE_FLOW.md, HLD_diagram.md, video_script.md, design-deck/) into it.
- `mkdir -p docs/_archive/ai-legacy` and move `ai/**` into it (git-tracked, so
  recoverable). Leave a one-line docs/_archive/README.md saying why.

STEP 2 — regenerate from code (verify each claim by reading the file):
- `HLD.md` (ONE page): the three planes (control = control/**, execution =
  cloud/render-worker-v2 + GPU services, state = Firestore + GCS); the boundary
  invariant (planes talk only via Firestore + GCS); the 7-stage loop named from
  entrypoint.py's real STAGES list; a compact code map.
- `LLD.md`: the render engine as named patterns, each with a real file:line:
  Facade (video.py::render_via_engines) → Strategy (engine.py::pick_engine) →
  Registry (contracts.py::register_plugin/get_plugin) → Protocol (contracts.py's
  six slot Protocols). List the six slots + where impls live. Show the
  "add a capability = 1 file + 1 register_plugin call" property with a real example
  from pipeline/render/visualize/.
- `CODE_FLOW.md`: what-calls-what from a job's birth (control/core/jobs.py) through
  the worker to short.mp4, every arrow a real function you opened. Real file:line only.

STEP 3 — verify:
- Every file:line reference in the three docs must resolve. Spot-check ~10 by opening
  them. Fix any that drifted.
- `grep` the new docs for module names and confirm each still exists post-cleanup
  (no references to modules CLIs 01–07 deleted).

OUTPUT: the three regenerated docs + a list of what you archived + any README/CLAUDE
reference fixes you made.
