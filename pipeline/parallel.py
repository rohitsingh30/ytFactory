"""Bounded fan-out for embarrassingly-parallel CPU-bound jobs.

Used by long_form_lib.py and footage_only_lib.py to parallelise
ffmpeg trim+letterbox+grade calls across multiple cores. ThreadPoolExecutor
is intentional — the heavy work happens inside ffmpeg subprocesses and PIL
C extensions, both of which release the GIL while running. A ProcessPool
would only add fork overhead and memory pressure for no benefit.

WHY this is its own module:
    - Single source of truth for the worker-count cap. The MLX/Metal device
      is shared, so concurrent ffmpeg encodes that *also* talk to MLX (none
      currently do, but if any do in future) must coordinate here.
    - On M2 Max (12 perf + 4 efficiency cores) we want ~3-4 ffmpeg encodes
      in flight: each libx264 -preset veryfast uses ~3 threads internally,
      so 4×3=12 threads saturates without thrashing.
    - Print interleaving: each worker emits one [done] line on completion
      so progress is legible even when stdout interleaves.

DO NOT use this for:
    - F5-TTS-MLX, mlx_whisper, z_image_turbo, or any MLX/Metal call. The
      Metal device is shared and concurrent ops serialize anyway, with
      extra unified-memory fragmentation (see docs/long_form_model_inventory.md).
    - The final mux ffmpeg pass — already internally threaded; fan-out hurts.
    - Anything that mutates shared state from multiple threads.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable


def _default_workers() -> int:
    """Cap parallel ffmpeg jobs at min(cpu_count // 2, 4).

    Each ffmpeg libx264 -preset veryfast uses ~3 internal threads; running
    more than 4 in flight on a 12-perf-core M2 Max thrashes the scheduler
    and gives diminishing returns. Override with YTFACTORY_FFMPEG_WORKERS
    if you really know better (e.g. M3 Ultra).
    """
    override = os.environ.get("YTFACTORY_FFMPEG_WORKERS")
    if override:
        try:
            n = int(override)
            if n >= 1:
                return n
        except ValueError:
            pass
    cpu = os.cpu_count() or 4
    return max(1, min(cpu // 2, 4))


def run_parallel(
    jobs: Iterable[Callable[[], None]],
    *,
    max_workers: int | None = None,
    label: str = "job",
) -> None:
    """Run a list of zero-arg callables in a bounded ThreadPool.

    Each job should be self-contained: produce a unique output file,
    print its own start line, and not mutate shared state. If any job
    raises, all in-flight jobs finish and the first exception is re-raised.

    Caller is responsible for skipping already-cached outputs (so resume
    semantics stay simple — this helper has no opinions about caching).
    """
    jobs = list(jobs)
    if not jobs:
        return
    workers = max_workers if max_workers is not None else _default_workers()
    workers = max(1, min(workers, len(jobs)))
    if workers == 1 or len(jobs) == 1:
        # Skip the pool overhead for serial / singleton cases.
        for job in jobs:
            job()
        return

    print(f"[parallel] {len(jobs)} {label}s in flight (max_workers={workers})")
    t0 = time.time()
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(job) for job in jobs]
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc is not None:
                errors.append(exc)
    if errors:
        # Surface the first error; downstream stages won't run anyway.
        raise errors[0]
    print(f"[parallel] {len(jobs)} {label}s done in {time.time()-t0:.1f}s")
