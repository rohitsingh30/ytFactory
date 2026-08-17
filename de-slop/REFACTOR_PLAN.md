# pipeline/ Refactor Plan — HLD-governed nested structure

**Goal:** collapse `pipeline/`'s 23 folders + ~22 loose root files + flat/nested
shim duplication into **~11 concern packages, one per HLD box.** No logic changes —
pure `git mv` + import repointing, verified by import-smoke.

**Principle:** every package = one HLD box (SRP). The current mess is a half-finished
flat→nested migration; this finishes it, one way.

---

## 1. Target `pipeline/` (each package ← an HLD box)

```
pipeline/
  sources/     ← Source Router
  authoring/   ← Script + Casting  (was llm/)
  images/      ← Image Generation
  audio/       ← Audio + Caption/ASR
  footage/     ← Footage Planner + Retrieval
  render/      ← Chunking + Compose + Music + Overlays  (engine — already clean)
  upload/      ← Upload (+ thumbnails + metadata)
  critique/    ← Critique
  qa/          ← Eval / QA  (HLD "Eval Check")
  config/      ← Specs (channels, niches, variants, schemas)
  infra/       ← cross-cutting (NOT an HLD stage): observability, cloud-clients, auth, utils
  editing/     ← Editing/Polish  — ONLY if added to the HLD (pending)
```

---

## 2. Migration map (current → target)

| Target package | Absorbs |
|---|---|
| `sources/` | `sources/*` + `social/*` (reddit_scrape, x_scrape, x_screenshot, reddit_card) + `research/{youtube,wiki,aggregator}.py` |
| `authoring/` | `llm/*` + loose `critic_long_form.py` |
| `images/` | `images/*` — **delete `animation.py` (dead, 0 importers)** |
| `audio/` | `tts/*` + `voice/*` + `text/hindi_normalize.py` + loose `asr.py`, `asr_cloudrun.py`, `align.py`, `transcribe.py`, `captions.py`, `beats.py`, `voice_clone.py` |
| `footage/` | `footage/*` + loose `cosmos_footage_prep.py` |
| `render/` | keep as-is; absorb or delete loose legacy `compose.py` (dedupe vs `render/compose/`) |
| `upload/` | `upload/*` + loose `thumbnails.py` + `publish/metadata_generator.py` |
| `critique/` | `critique/*` |
| `qa/` | `quality/*` (evals, probe, preflight) + `render/qa/vbench_adapter.py` |
| `config/` | `channels/` + `variants/` + `schemas/*` + loose `channels.py`, `niches.py`, `niche_specs.py`, `era_anchor.py`, `era_taxonomy.yaml` + `research/channel_assets.py` |
| `infra/` | `observability/*` + `cloud/*` (→ `infra/cloud_clients/`) + `auth/identity.py` + `utils/*` + loose `paths.py`, `parallel.py`, `stage_overlap.py`, `cloudrun_auth.py`, `telemetry.py` |
| `editing/` | `editing/*` (pending HLD decision) |
| **growth-ops** | `research/cross_engage.py` → move OUT of pipeline to `control/` (not a render concern) |

**Kill the flat/nested duplication:** the shims in `audio/`, `quality/`, `utils/`,
`voice/`, `schemas/`, `cloud/` re-export loose root files. Once the root file lands
in its target package, repoint importers and **delete the shim**.

---

## 3. HLD gaps (surfaced by the mapping)
1. **Editing/Polish** — real code (`editing/`, the `editing-agent`), no HLD box. → add optional post-Compose box, or exclude.
2. **Observability + Cloud-ops** — cross-cutting (`infra/`); NOT pipeline stages. Confirmed omit from HLD.
3. **Ideation** — actually lives in `control/routes/discover_routes.py`, not `pipeline/`. HLD "Ideation AI" box = control-plane.

## 4. Other top-level areas (not in this refactor)
- `control/` — control plane (core: queue/jobs/scheduler/reconciler/…; routes: 19 FastAPI routers). Orchestrator. Left alone.
- `cloud/` — 12 deployed services. Each maps to an HLD box; not moved.
- `web/` (2 py legacy) + `web-next/` (116 TS/TSX UI). Separate track.
- `scripts/` — 17 ops/CLI tools. Left alone.

---

## 5. Parallelization — one agent per target package (disjoint)

| Agent | Owns (moves + repoints importers of its OLD paths) |
|---|---|
| A | `sources/` (+ social + research sources) |
| B | `authoring/` (llm) |
| C | `audio/` (tts + voice + text + loose audio files) |
| D | `images/` |
| E | `footage/` |
| F | `upload/` + `critique/` + `qa/` |
| G | `config/` |
| H | `infra/` |

`render/` needs no move (already clean); one agent just dedupes loose `compose.py`.

### Guardrails (every agent)
- **`git mv` only** — preserve history. No logic edits.
- After moving a file, repoint **every** importer repo-wide:
  `from pipeline.<old> import X` → `from pipeline.<pkg>.<mod> import X`
  (`grep -rn "pipeline.<old>"` to find them).
- A file imported by **two** moving packages = the one conflict point → log to
  `de-slop/REFACTOR_HANDOFF.md`, don't fight over it; resolve in integration.
- Delete a shim only after its canonical is moved AND importers repointed.
- **Verify before finishing:** `.venv/bin/python -c "import pipeline.<pkg>"` for the
  package + `python -m py_compile` on every moved file. Revert anything that won't import.

### Run order
1. Agents A–H in parallel (git worktree each, as in `de-slop/COORDINATION.md`).
2. Integration pass: merge, resolve `REFACTOR_HANDOFF.md`, `python -c "import pipeline"`
   (whole package tree must import), then a repo-wide `grep` for any surviving
   `pipeline.<oldpath>` reference.
3. Delete emptied folders + shims.

---

## 6. Status
- [ ] HLD decision on Editing/Polish box
- [ ] Confirm `infra/cloud_clients` rename (kills the `pipeline/cloud` vs `cloud/` clash)
- [ ] Author per-agent prompts (like `de-slop/prompts/`) once map is frozen
