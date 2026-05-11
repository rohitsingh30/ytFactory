# Per-render image fan-out — pattern + recipe

> **Established 2026-05-11** when the deferred latency win from
> 2026-05-10 (pipeline_latency_2026.md §9 c1) shipped as commit
> `a3c4e47`. Stage 6 wall-clock for a 7-image AITA Short cut from
> 33.4 s → ~17 s at zero compute cost. This doc captures the
> generalizable pattern so the next "serial loop with bootstrap
> dependency" refactor doesn't have to re-discover it.

## TL;DR

When a serial `for i in items` loop has a **bootstrap dependency**
(iteration `i > 0` reads state written by `i == 0`) AND each
iteration is otherwise pure-ish (no cross-iteration mutation, only
shared read-only context), the right parallelization shape is:

1. **Extract** the loop body into a closure `_render_one(i, item) -> result`.
2. **Run iteration 0 sequentially** to satisfy the bootstrap.
3. **Fan out iterations 1..N** via `ThreadPoolExecutor` (cloud / I/O-
   bound work) or skip the pool entirely (local single-GPU work).
4. **Reassemble** results in original order via a `dict[int, T]` →
   `[results[i] for i in range(N)]`.
5. **Atomic shared state** — any module-global flag the loop body
   touches (telemetry "first-event" markers, breaker state) goes
   behind a `threading.Lock`.

Worked example: `pipeline/render/shorts.py::_render_one_beat` lines
1736-2000 (closure) + 2040-2110 (dispatcher).

## Why a serial loop, not just a thread pool

The naive `ThreadPoolExecutor(map(generate, items))` would race on:

- **IP-Adapter bootstrap** — beats with `i > 0` read `img_00.png`
  as their character-lock reference (pipeline/render/shorts.py
  line 1750-1759 in `_render_one_beat`). If beat 0 hasn't finished
  writing yet, beats 1..N see no reference and lose character
  consistency. Result: every supporting character renders with
  random faces.
- **Cold-load priming** — the first `/generate` against a cold
  `cloudrun_flux2_klein` triggers ~5-7 min of model load. With N
  parallel workers all racing the cold container, the breaker can
  trip on transient 503s before the first warm response lands;
  every beat then falls back to local mflux (the disaster mode).
  Sequential beat 0 lets the warmest container respond first;
  beats 1..N then hit instances 2-3 as they spin up.

**Lesson:** Serial bootstrap + parallel rest is faster than full-
parallel-with-retry, even if the bootstrap costs the longest single
iteration's wall-clock.

## The dispatcher recipe (copy-pasteable)

```python
import threading as _threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Atomic shared-state guard for any module-global flag the closure
# reads/writes (telemetry first-event markers, etc.). Using a
# mutable single-element list because Python closures can't rebind
# enclosing locals without `nonlocal` — and `nonlocal` doesn't help
# under thread fan-out anyway (no atomicity).
_first_event_pending = [True]
_first_event_lock = _threading.Lock()

def _do_one(i: int, item) -> ResultT:
    """Pure-ish closure: reads shared context from enclosing scope,
    writes per-iteration output to disk + telemetry. Returns the
    result. MUST be safe to call from either the main thread (i=0)
    OR a ThreadPoolExecutor worker (i>0)."""
    # ... per-iteration body ...
    # For any shared-state check-and-clear:
    with _first_event_lock:
        was_first = _first_event_pending[0]
        if was_first:
            _first_event_pending[0] = False
    # Use `was_first` to decide whether to record one-shot metric.
    # Without the lock, N workers could each see True and re-record.
    return result

# 1. Sequential bootstrap (iteration 0).
results: dict[int, ResultT] = {}
if items:
    results[0] = _do_one(0, items[0])

# 2. Fan-out iterations 1..N — but ONLY for cloud/I/O-bound work.
#    Local single-GPU providers stay serial (threading just adds GIL
#    contention with zero compute win).
remaining = list(enumerate(items))[1:]
_use_pool = (
    remaining
    and provider.startswith("cloudrun_")  # cloud only
    and int(os.environ.get("MY_WORKERS", "2")) > 1
)
if _use_pool:
    workers = int(os.environ.get("MY_WORKERS", "2"))
    print(f"[fan-out] {len(remaining)} items × {workers} workers")
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="my-fanout",
    ) as pool:
        futures = {pool.submit(_do_one, i, x): i for i, x in remaining}
        for fut in as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()  # re-raises first exception
else:
    for i, x in remaining:
        results[i] = _do_one(i, x)

# 3. Reassemble in original order.
output = [results[i] for i in range(len(items))]
```

## Worker-count math

`max_workers` should default to `(cloud_service.max_instances - reserve)`
where `reserve` is the slot count you want free for cross-render
parallelism (`scripts/ops/bulk_render_queue.py`).

Today: `cloudrun_flux2_klein --max-instances=3`, reserve=1, so
default workers=2. Tunable via `YTFACTORY_IMAGE_WORKERS` env.

To go wider (e.g. workers=4 to halve stage 6 again):
1. File a GPU quota request with Google for asia-southeast1
   `NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion 3 → 6+`
   (currently 3 — see `docs/cloud_run_quota_self_service.md`).
2. Bump `cloud/image-flux2-klein/deploy.sh:--max-instances=N`.
3. Redeploy.
4. Update this doc + `docs/cloudrun_image.md` + `docs/parallel_bulk_renders.md`.

## Cost: marginal $/mo of fan-out

**Compute: $0 delta.** Same total GPU-seconds (e.g. 30 images ×
5 s/image = 150 s either serial or parallel) — Cloud Run bills per
request-handling time, not per container-uptime. Splitting across
N containers costs the same total as one container handling N
serial requests.

**Cold-load amplification: ~$0.075 per cold container per render
burst.** With `--min-instances=0`, each NEW container that spins
up pays its own ~5-min cold-load (5 min × $0.90/hr ≈ $0.075).
Workers=2 fan-out doubles this on first burst (~$0.15 per cold
batch). Mitigated to ~$0 by Cloud Scheduler prewarm at known
render windows.

See `docs/cloud_cost_2026_05_11.md` § "Marginal cost of bumping
max-instances" for the full cost matrix.

## When NOT to use this pattern

- **Single-GPU local provider** (mflux, sdxl_lightning,
  z_image_turbo on M2 Max). Threading serializes on the Metal
  command queue; you pay GIL overhead for zero compute win. The
  dispatcher above gates on `provider.startswith("cloudrun_")`
  to skip the pool for local providers.
- **Cross-iteration writes to shared mutable state** (e.g. the
  loop body appends to a shared `cache_dict`). Refactor to write
  per-iteration files or return tuples; reassemble after the pool.
- **Iteration body that depends on `i-1`'s result, not just `i=0`'s.**
  This pattern doesn't help — you need a pipeline (asyncio Queue
  or producer/consumer threads), not parallelism.

## Telemetry invariant — exactly one cold_load_s per render

The atomic check-and-clear (`with _first_event_lock`) guarantees
exactly ONE worker records the cold-load metric. Pre-fix the bare
`_first_image_pending = True` rebind worked fine for the serial
loop but broke under fan-out — multiple beat threads could each
see `True` before any of them set it `False`, causing N parallel
renders to all stamp `cold_load_s` and poison the dashboard's
per-render cold-load tax view.

If you add new render-scoped one-shot metrics (e.g. `first_seed_used_s`,
`first_warm_response_dt`), use the same lock pattern.

## Where this is wired

- `pipeline/render/shorts.py::_render_one_beat` — the closure
- `pipeline/render/shorts.py` lines 2040-2110 — the dispatcher
- `cloud/image-flux2-klein/deploy.sh:49` — `--max-instances=3`
  comment block explains the per-render fan-out + bulk reserve
  math and the asia-southeast1 quota ceiling
- `pipeline/images/images_cloudrun.py:163-188` — `_BREAKER_LOCK`
  protects the cloud breaker, which means parallel beats can race
  on first failure but the SECOND failure trips the breaker once
  (acceptable: ≤2 wasted cloud failures vs 1 in serial mode)

## Sweep recipe — find candidate loops to apply this pattern to

```bash
# Serial loops with cloud calls inside that could benefit:
grep -rnB2 "for [a-z_]\+ in [a-z_]\+:" pipeline/render/ \
  | grep -A2 "cloudrun_\|requests\.\(post\|get\)" | head -40

# Loops with bootstrap dependencies (i==0 special case):
grep -rn "elif i == 0:\|if i == 0:" pipeline/ --include='*.py'
```

Today's known candidates (NOT YET refactored):

- `pipeline/render/long_form.py` image_panels rendering (40-300
  panels). Currently local mflux; would need the
  Z-Image-Turbo cloud P3.5 fix first.
- `pipeline/render/sports_doc.py` — similar image-loop shape.

## Cross-references

- `docs/pipeline_latency_2026.md` § 9 — the deferral that this doc
  resolves
- `docs/cloudrun_image.md` § "Per-call timings" — measured speedup
- `docs/parallel_bulk_renders.md` — the COMPOSING cross-render pattern
- `docs/cloud_run_quota_self_service.md` § "GPU quota in
  asia-southeast1" — how to bump if you want workers=4
- `docs/cloud_cost_2026_05_11.md` § "Marginal cost of bumping
  max-instances" — the cost math
- `~/.claude/projects/.../memory/feedback_parallel_per_beat_fanout.md`
  — terse memory pointer
