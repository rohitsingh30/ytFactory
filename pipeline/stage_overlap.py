"""Inter-stage parallelism for render orchestrators.

This module is the **stage-overlap** sibling of :mod:`pipeline.parallel`
(which fans out *within* a stage — N ffmpeg workers, N image beats).
Stage overlap fans out *across* stages: TTS chunked synthesis runs in
parallel with image-stills generation / footage-source download / chapter
card rendering, even though they conceptually live in different
"stages" of the orchestrator.

When a render orchestrator can identify an **independent prefix** of
stage X — work that knows everything from the rewrite-time JSON and
needs nothing from a yet-to-run sibling stage — that prefix can be
launched on a worker thread BEFORE the sibling starts. The
duration-dependent assembly tail then re-joins both branches.

Why a dedicated module (vs piggy-backing on :mod:`pipeline.parallel`)
======================================================================

:func:`pipeline.parallel.run_parallel` is purpose-built for a list of
identical zero-arg callables (e.g. N ffmpeg trims). It does not:

* propagate :mod:`contextvars` to worker threads (so OTel current-span
  + :class:`pipeline.observability.context.RenderContext` evaporate the
  moment a worker thread runs the callable);
* return heterogeneous typed results ("the TTS branch returns a
  ``(Path, list[Path])`` tuple, the stills branch returns
  ``list[Path]``, the cards branch returns ``dict[str, Path]``");
* gate on the cross-cutting "is local-GPU contention possible?"
  predicate — the per-stage version of the rule encoded in
  :file:`feedback_gpu_one_render_at_a_time.md`.

Stage overlap needs all three. It also has a much smaller fan-out
factor (2-4 branches per render, not 20+ images), so the overhead
shape is different.

Per-future ``copy_context()`` (NOT shared!)
-------------------------------------------

A common mistake is to call ``contextvars.copy_context()`` once and
re-use the same Context across all submitted futures::

    ctx = contextvars.copy_context()
    pool.submit(lambda: ctx.run(fn1))   # WRONG
    pool.submit(lambda: ctx.run(fn2))   # WRONG — Context.run can't
                                        #         be re-entered
                                        #         concurrently.

:meth:`contextvars.Context.run` raises ``RuntimeError: cannot enter
context: <_contextvars.Context …> is already entered`` when two
threads try to enter the same Context simultaneously. Each
:meth:`StageOverlap.submit` call therefore captures a *fresh* snapshot
via ``contextvars.copy_context()``.

GPU contention gate
-------------------

Cloud-bound TTS (``cloudrun_chatterbox``, ``cloudrun_f5``,
``cloudrun_indicf5``, …) and cloud-bound image-gen
(``cloudrun_flux2_klein``, …) hit physically distinct L4 GPUs in
``asia-southeast1`` — overlap is free.

Local providers (``f5_tts`` / ``kokoro`` / ``mflux`` /
``z_image_turbo``) all dispatch to the same Metal command queue on
M2 Max. Concurrent diffusion + TTS triples per-step latency and can
trigger Metal command-buffer timeouts (see
``docs/long_form_model_inventory.md`` and the dual-save memory
``feedback_gpu_one_render_at_a_time.md``). :func:`gpu_safe_to_overlap`
returns ``False`` whenever EITHER provider is local-GPU; the
orchestrator must then fall back to sequential.

The cloud-providers can technically degrade to local fallback
mid-render (see ``pipeline.tts.cloudrun._synth_cloudrun_*`` /
``pipeline.images_cloudrun._materialise_png``), at which point the
overlap could re-introduce contention. We accept that risk — fallback
is a rare event, the F5 reset_mlx_state hook (long-form stage-1
boundary) bounds the damage, and the alternative ("disable cloud
overlap entirely whenever fallback could conceivably trigger") leaves
all the low-risk wins on the table. Set
``YTFACTORY_DISABLE_STAGE_OVERLAP=1`` to globally force sequential if
debugging suggests overlap is the trigger of a regression.

Failure semantics
-----------------

When one branch raises, in-flight sibling work is NOT cancelled
(``concurrent.futures.Future.cancel`` cannot stop an already-running
HTTP/ffmpeg/TTS call anyway, and killing it would discard valuable
cache writes that a retry could re-use). :meth:`StageOverlap.__exit__`
waits for ALL submitted futures to settle, then re-raises the first
exception. On Python 3.11+ subsequent exceptions are surfaced via
``ExceptionGroup``.
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Cloud-bound provider name prefixes — providers that hit a remote GPU
# (Cloud Run, Azure, future managed inference) and therefore do NOT
# contend with the local Metal queue.
_CLOUD_PROVIDER_PREFIXES = ("cloudrun_", "azure_", "vertex_")


def gpu_safe_to_overlap(
    *,
    tts_provider: str | None,
    image_provider: str | None = None,
) -> tuple[bool, str]:
    """Return ``(safe, reason)`` — should TTS overlap with image-gen?

    ``safe=False`` means the orchestrator MUST run the two stages
    sequentially. The current rule is conservative: BOTH providers
    must be cloud-bound (one of :data:`_CLOUD_PROVIDER_PREFIXES`) AND
    the global ``YTFACTORY_DISABLE_STAGE_OVERLAP`` env must NOT be
    set.

    Edge cases:

    * ``image_provider`` may be ``None`` for orchestrators that don't
      generate images (e.g. footage_only with shotlist-only path).
      In that case the provider check is skipped — the only relevant
      check is the env override.
    * Empty / unknown string treated as "local" (safe-by-default).

    Returns the reason string for logging — every orchestrator should
    print the gate decision so post-mortem on a slow render doesn't
    require reading code.
    """
    if os.environ.get("YTFACTORY_DISABLE_STAGE_OVERLAP", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return False, "disabled by YTFACTORY_DISABLE_STAGE_OVERLAP env"

    tts = (tts_provider or "").strip().lower()
    if not tts.startswith(_CLOUD_PROVIDER_PREFIXES):
        return False, f"tts_provider={tts_provider!r} is local-GPU (Metal contention risk)"

    if image_provider is not None:
        img = (image_provider or "").strip().lower()
        if not img.startswith(_CLOUD_PROVIDER_PREFIXES):
            return False, f"image_provider={image_provider!r} is local-GPU (Metal contention risk)"
        return True, f"both cloud (tts={tts_provider}, images={image_provider})"

    return True, f"tts cloud (tts={tts_provider}); no image branch"


def _submit_with_context(
    pool: ThreadPoolExecutor,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> Future[T]:
    """Submit ``fn(*args, **kwargs)`` to ``pool`` with the current
    :mod:`contextvars` snapshot propagated to the worker.

    The snapshot is captured **per-call** (not shared across submits)
    because :meth:`contextvars.Context.run` cannot be entered
    concurrently — re-using the same Context for two simultaneous
    workers would raise ``RuntimeError: cannot enter context``.
    Capturing per submit costs O(small) — Context is a copy-on-write
    immutable map of ContextVar→value pairs.

    What this propagates (because every backing store uses
    :class:`contextvars.ContextVar`):

    * :class:`pipeline.observability.context.RenderContext` — the
      ``channel`` / ``slug`` / ``job_id`` / ``run_id`` identity attached
      to every span/log.
    * The OpenTelemetry active span — so any ``obs.timed`` /
      ``@obs.traced`` call inside the worker becomes a CHILD span of
      whatever was active in the calling thread, instead of a detached
      root span. This is what keeps the dashboard's per-render
      envelope linkage intact across overlap branches.
    * :mod:`pipeline.utils.run_context` ``RUN_ID`` and any other
      ContextVar-backed singletons.

    What this does NOT propagate (no ContextVar to capture):

    * subprocess CWD (changed in worker = changed in process — caller
      must not :func:`os.chdir`)
    * Python warnings filters
    * the standard ``logging`` MDC (we don't use one)
    """
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)


class StageOverlap:
    """Context manager that owns a :class:`ThreadPoolExecutor` for
    the duration of a render's overlap region.

    Usage::

        with StageOverlap(label="long_form image_panels") as overlap:
            # Independent branch on a worker thread
            stills_fut = overlap.submit(
                "panel_stills",
                _generate_panel_stills,
                panels=panels, ...
            )
            # Main-thread work in parallel
            narration_wav, chunks = synth_long_narration(...)
            dur = probe_duration(narration_wav)
            # Re-join
            panel_pngs = stills_fut.result()  # blocks; raises if branch failed
        # __exit__ waits for any unawaited futures + re-raises
        # the first exception. ExceptionGroup on py 3.11+ for
        # multi-branch failures.

        # Now run dur-dependent assembly with both inputs ready.
        video_path = _assemble_kenburns(panel_pngs, dur, ...)

    The ``label`` is included in the worker thread name (visible in
    :command:`py-spy` / Cloud Trace) AND the structured log lines this
    helper emits — pick something short but unique per call site.
    """

    def __init__(
        self,
        label: str,
        *,
        max_workers: int = 4,
        log: bool = True,
    ) -> None:
        self.label = label
        self.max_workers = max(1, max_workers)
        self._log = log
        self._pool: ThreadPoolExecutor | None = None
        self._futures: list[tuple[str, Future[Any]]] = []
        self._t0: float = 0.0
        self._lock = threading.Lock()

    def __enter__(self) -> "StageOverlap":
        self._t0 = time.time()
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"overlap-{self.label[:24]}",
        )
        if self._log:
            print(
                f"[overlap] {self.label} pool open "
                f"(max_workers={self.max_workers})"
            )
        return self

    def submit(
        self,
        branch_label: str,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> Future[T]:
        """Schedule ``fn(*args, **kwargs)`` on a worker thread with
        :mod:`contextvars` propagation. Returns a :class:`Future` —
        call :meth:`Future.result` when you need the value.

        ``branch_label`` is included in failure messages — pick
        something descriptive (``"panel_stills"``,
        ``"footage_download"``, ``"chapter_cards"``).
        """
        if self._pool is None:
            raise RuntimeError(
                "StageOverlap.submit called outside the context manager"
            )
        fut = _submit_with_context(self._pool, fn, *args, **kwargs)
        with self._lock:
            self._futures.append((branch_label, fut))
        if self._log:
            print(f"[overlap] {self.label} → {branch_label} submitted")
        return fut

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._pool is None:
            return False
        with self._lock:
            futures = list(self._futures)
        if futures:
            _, fut_objs = zip(*futures)
            wait(fut_objs)
        # Drain pool — block on any straggler threads.
        self._pool.shutdown(wait=True)
        self._pool = None

        # Surface any branch errors. Skip futures the caller already
        # consumed (those will have re-raised at .result() time);
        # surface unconsumed errors here so a "fire-and-forget" branch
        # cannot silently swallow an exception.
        errors: list[tuple[str, BaseException]] = []
        for branch_label, fut in futures:
            exc = fut.exception()
            if exc is not None:
                errors.append((branch_label, exc))

        elapsed = time.time() - self._t0
        if self._log:
            ok = len(futures) - len(errors)
            print(
                f"[overlap] {self.label} pool closed in {elapsed:.1f}s — "
                f"{ok}/{len(futures)} branch(es) ok"
            )

        # If the body itself raised, propagate that — branch errors
        # become __notes__ / suppressed siblings.
        if exc_type is not None:
            for branch_label, exc in errors:
                logger.warning(
                    "[overlap] %s branch %r also failed (suppressed by primary "
                    "exception in body): %r",
                    self.label, branch_label, exc,
                )
            return False  # let body's exception propagate

        if not errors:
            return False  # nothing to raise

        if len(errors) == 1:
            branch_label, exc = errors[0]
            note = f"[overlap] branch={branch_label} label={self.label}"
            _add_note(exc, note)
            raise exc

        # Multiple branches failed — use ExceptionGroup on py 3.11+
        # so the user sees every cause, not just the first.
        if sys.version_info >= (3, 11):
            for branch_label, exc in errors:
                _add_note(exc, f"[overlap] branch={branch_label} label={self.label}")
            raise BaseExceptionGroup(  # type: ignore[name-defined]  # noqa: F821
                f"{self.label}: {len(errors)} stage-overlap branches failed",
                [exc for _, exc in errors],
            )
        # py 3.10 fallback — surface first, log the rest.
        primary_label, primary_exc = errors[0]
        for branch_label, exc in errors[1:]:
            logger.warning(
                "[overlap] %s branch %r failed alongside primary %r: %r",
                self.label, branch_label, primary_label, exc,
            )
        _add_note(primary_exc, f"[overlap] branch={primary_label} label={self.label}")
        raise primary_exc


def _add_note(exc: BaseException, note: str) -> None:
    """Best-effort :pep:`678` note attachment (py 3.11+).

    On older Pythons the note is logged at WARNING instead so the
    information isn't lost; the exception itself is unchanged.
    """
    add = getattr(exc, "add_note", None)
    if callable(add):
        try:
            add(note)
            return
        except Exception:  # noqa: BLE001
            pass
    logger.warning("%s :: %s", note, exc)


__all__ = [
    "StageOverlap",
    "gpu_safe_to_overlap",
]
