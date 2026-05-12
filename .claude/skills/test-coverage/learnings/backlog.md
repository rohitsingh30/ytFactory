# Test-coverage backlog — historical untested code

The `/test-coverage` skill enforces 100% coverage on **lines changed
this session**. The historical backlog (code that was never tested
before the skill landed) is logged here for incremental work.

## Snapshot 2026-05-12 (skill establishment)

| dir | est. files | est. coverage | priority |
|---|---|---|---|
| `pipeline/render/` | ~30 | ~40% | high (frequently edited) |
| `pipeline/llm/` | ~15 | ~50% | medium |
| `pipeline/tts/` | ~25 | ~30% | medium |
| `pipeline/footage/` | ~20 | ~35% | medium |
| `pipeline/upload/` | ~10 | ~60% | high (revenue-critical) |
| `cloud/render-worker-v2/` | 1 (1858 LoC) | ~70% | high |
| `cloud/tts-*/` | ~7 services | ~25% | medium (Cloud Run, mostly integration) |
| `cloud/image-*/` | ~4 services | ~25% | medium |
| `web/server.py` | 1 (~4400 LoC) | ~30% | high (HTTP surface) |
| `control/routes/` | ~15 | ~50% | high (HTTP surface) |
| `control/core/` | ~12 | ~60% | medium |
| `web-next/lib/` | ~8 | ~10% | medium (helpers only — components out of scope) |
| `web-next/components/` | ~30 | 0% | LOW (no React test infra yet) |
| `web-next/app/**/*.tsx` | ~40 | 0% | LOW (no React test infra yet) |
| `scripts/` | ~50 | ~5% | LOW (one-off ops scripts) |

Numbers are eyeball estimates — get accurate ones with:

```bash
.venv/bin/coverage run --source=<dir> -m pytest tests/ -q
.venv/bin/coverage report
```

## Strategy

1. **Don't backfill all at once.** A whole-repo sweep would block
   on dozens of integration mocks (Firestore, GCS, Cloud Run,
   ffmpeg, real LLMs). The /test-coverage skill prevents NEW gaps
   from compounding — that's the win to lock in first.

2. **Prioritize by edit frequency.** Modules touched in the last
   30 days carry the most regression risk. `pipeline/render/`,
   `cloud/render-worker-v2/`, `web/server.py`, `control/routes/`
   are the top candidates.

3. **Extract pure helpers first.** Most low-coverage modules have
   80% pure logic + 20% I/O glue. Extract the pure helpers (à la
   `_lf_advance_timeline` from `cloud/render-worker-v2/entrypoint.py`)
   and unit-test them. The integration glue gets `# coverage:`
   justifications until proper integration tests land.

4. **Frontend is a separate track.** React components need vitest +
   @testing-library/react. See `frontend-test-infra.md` for the
   standing TODO. Until then: extract logic into `web-next/lib/*.ts`
   pure helpers and pin THOSE.

## Work units (no priority order)

- [ ] `pipeline/render/spec.py::build_spec` integration test for every
      (channel × variant × length_kind) combo
- [ ] `pipeline/upload/upload.py` mock-Cloud-Run + mock-OAuth path
- [ ] `web/server.py::_job_snapshot` full status-matrix test
- [ ] `control/routes/render_routes.py::preview_mp4` all four return
      branches (sim, gs, local, 404)
- [ ] `cloud/render-worker-v2/entrypoint.py::_main_from_firestore`
      full integration test with mocked Firestore + mocked
      `pipeline.render.video.render`
- [ ] `pipeline/llm/rewrite.py` mock LLM + happy path + retry path
- [ ] `pipeline/llm/rewrite_long_form.py` same
- [ ] `pipeline/render/long_form.py::main` mock TTS + image gen +
      ffmpeg subprocess

## Adding to this list

Whenever `/test-coverage` skips a line via `# coverage:`, the
justification reveals what test would have covered it. Pull those
into here as concrete work units — each one is a half-day to a
day of mocking + tests.
