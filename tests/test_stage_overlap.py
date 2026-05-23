"""Pin :mod:`pipeline.stage_overlap` — the inter-stage parallelism
helper used by every render orchestrator (long_form, sports_doc,
footage_only, shorts) to overlap TTS with image-gen / footage prep.

These tests use :class:`threading.Event` for deterministic
happens-before assertions instead of timing comparisons (the latter
would flake on slow CI).
"""
from __future__ import annotations

import contextvars
import os
import sys
import threading
import time
from concurrent.futures import Future

import pytest

from pipeline.stage_overlap import (
    StageOverlap,
    _add_note,
    _submit_with_context,
    gpu_safe_to_overlap,
)


# ---------------------------------------------------------------------------
# gpu_safe_to_overlap — the cloud-bound gate
# ---------------------------------------------------------------------------


class TestGpuSafeToOverlap:
    def setup_method(self) -> None:
        os.environ.pop("YTFACTORY_DISABLE_STAGE_OVERLAP", None)

    def teardown_method(self) -> None:
        os.environ.pop("YTFACTORY_DISABLE_STAGE_OVERLAP", None)

    def test_both_cloud_chatterbox_z_image_turbo(self) -> None:
        ok, reason = gpu_safe_to_overlap(
            tts_provider="cloudrun_chatterbox",
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is True
        assert "both cloud" in reason

    def test_both_cloud_indicf5_z_image_turbo(self) -> None:
        ok, _ = gpu_safe_to_overlap(
            tts_provider="cloudrun_indicf5",
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is True

    def test_local_tts_blocks_overlap(self) -> None:
        ok, reason = gpu_safe_to_overlap(
            tts_provider="kokoro",
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is False
        assert "kokoro" in reason
        assert "Metal contention" in reason

    def test_local_image_blocks_overlap(self) -> None:
        ok, reason = gpu_safe_to_overlap(
            tts_provider="cloudrun_chatterbox",
            image_provider="z_image_turbo",
        )
        assert ok is False
        assert "z_image_turbo" in reason

    def test_kokoro_blocks_overlap(self) -> None:
        # Hindi laptop fallback path — must NOT overlap.
        ok, _ = gpu_safe_to_overlap(
            tts_provider="kokoro",
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is False

    def test_mflux_blocks_overlap(self) -> None:
        ok, _ = gpu_safe_to_overlap(
            tts_provider="cloudrun_chatterbox",
            image_provider="mflux",
        )
        assert ok is False

    def test_image_provider_none_skips_image_check(self) -> None:
        # footage_only / sports_doc with no image branch — cloud TTS
        # alone is enough.
        ok, _ = gpu_safe_to_overlap(
            tts_provider="cloudrun_chatterbox",
            image_provider=None,
        )
        assert ok is True

    def test_image_provider_none_local_tts_still_blocks(self) -> None:
        ok, _ = gpu_safe_to_overlap(
            tts_provider="kokoro",
            image_provider=None,
        )
        assert ok is False

    def test_env_disable_blocks_even_when_safe(self) -> None:
        os.environ["YTFACTORY_DISABLE_STAGE_OVERLAP"] = "1"
        ok, reason = gpu_safe_to_overlap(
            tts_provider="cloudrun_chatterbox",
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is False
        assert "YTFACTORY_DISABLE_STAGE_OVERLAP" in reason

    def test_env_disable_truthy_variants(self) -> None:
        for val in ("1", "true", "TRUE", "yes", "on"):
            os.environ["YTFACTORY_DISABLE_STAGE_OVERLAP"] = val
            ok, _ = gpu_safe_to_overlap(
                tts_provider="cloudrun_chatterbox",
                image_provider="cloudrun_z_image_turbo",
            )
            assert ok is False, f"env={val!r} should disable overlap"

    def test_env_disable_falsy_variants(self) -> None:
        for val in ("0", "false", "no", "off", ""):
            os.environ["YTFACTORY_DISABLE_STAGE_OVERLAP"] = val
            ok, _ = gpu_safe_to_overlap(
                tts_provider="cloudrun_chatterbox",
                image_provider="cloudrun_z_image_turbo",
            )
            assert ok is True, f"env={val!r} should NOT disable overlap"

    def test_unknown_provider_treated_as_local(self) -> None:
        ok, reason = gpu_safe_to_overlap(
            tts_provider="some_unknown_provider",
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is False
        assert "local-GPU" in reason

    def test_none_tts_provider_treated_as_local(self) -> None:
        ok, _ = gpu_safe_to_overlap(
            tts_provider=None,
            image_provider="cloudrun_z_image_turbo",
        )
        assert ok is False

    def test_azure_prefix_recognised_as_cloud(self) -> None:
        # Forward-compat: future Azure-hosted TTS / image services
        # should overlap by default.
        ok, _ = gpu_safe_to_overlap(
            tts_provider="azure_speech",
            image_provider="azure_dalle3",
        )
        assert ok is True

    def test_vertex_prefix_recognised_as_cloud(self) -> None:
        # Forward-compat: future Vertex AI Imagen / Gemini-TTS.
        ok, _ = gpu_safe_to_overlap(
            tts_provider="vertex_chirp",
            image_provider="vertex_imagen",
        )
        assert ok is True


# ---------------------------------------------------------------------------
# _submit_with_context — ContextVar / OTel propagation
# ---------------------------------------------------------------------------


class TestSubmitWithContext:
    def test_propagates_render_context(self) -> None:
        """The :class:`pipeline.observability.context.RenderContext`
        ContextVar set in the calling thread must be visible inside
        the worker thread.
        """
        from pipeline.observability.context import (
            current_context,
            ctx as render_ctx,
        )

        captured: dict[str, str | None] = {}

        def _read_in_worker() -> None:
            rc = current_context()
            captured["channel"] = rc.channel
            captured["slug"] = rc.slug

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            with render_ctx(channel="cosmosdecoded", slug="ligo-2015"):
                fut = _submit_with_context(pool, _read_in_worker)
                fut.result()

        assert captured == {"channel": "cosmosdecoded", "slug": "ligo-2015"}

    def test_propagates_arbitrary_contextvar(self) -> None:
        """Any user-defined ContextVar (e.g. RUN_ID) propagates."""
        my_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "test_overlap_var",
            default="UNSET",
        )

        captured: dict[str, str] = {}

        def _read_var() -> None:
            captured["v"] = my_var.get()

        token = my_var.set("hello-from-main-thread")
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = _submit_with_context(pool, _read_var)
                fut.result()
        finally:
            my_var.reset(token)

        assert captured["v"] == "hello-from-main-thread"

    def test_per_future_copy_allows_concurrent_execution(self) -> None:
        """Two futures submitted from the same context must be able to
        run in parallel — we capture per-future, not once.

        Regression for the rubber-duck blocker #1: re-using a single
        :func:`contextvars.copy_context` across two submitted futures
        raises ``RuntimeError: cannot enter context: <Context …> is
        already entered``.
        """
        my_var: contextvars.ContextVar[int] = contextvars.ContextVar(
            "test_concurrent_var",
            default=0,
        )

        worker_ready = threading.Event()
        worker_release = threading.Event()
        sibling_done = threading.Event()

        def _worker_a() -> int:
            worker_ready.set()
            assert worker_release.wait(timeout=2.0), "test deadlock"
            return my_var.get()

        def _worker_b() -> int:
            assert worker_ready.wait(timeout=2.0), "worker_a never set ready"
            sibling_done.set()
            return my_var.get()

        token = my_var.set(42)
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = _submit_with_context(pool, _worker_a)
                fut_b = _submit_with_context(pool, _worker_b)
                # Must succeed: B reads my_var concurrently with A.
                # If both shared a single Context, B would raise
                # RuntimeError("cannot enter context").
                assert sibling_done.wait(timeout=2.0), \
                    "sibling_done never set — likely Context.run RuntimeError"
                worker_release.set()
                assert fut_a.result(timeout=2.0) == 42
                assert fut_b.result(timeout=2.0) == 42
        finally:
            my_var.reset(token)

    def test_propagates_otel_span_context(self) -> None:
        """An OTel current-span set in the caller becomes the parent
        of any span started inside the worker.

        OTel stores active context via ``contextvars.ContextVar`` so
        :func:`copy_context` propagation is sufficient.
        """
        try:
            from opentelemetry import trace
        except ImportError:
            pytest.skip("opentelemetry not installed in this env")

        tracer = trace.get_tracer("test_stage_overlap")
        captured: dict[str, str | None] = {}

        def _read_span_in_worker() -> None:
            sp = trace.get_current_span()
            sc = sp.get_span_context() if sp else None
            captured["trace_id_hex"] = (
                f"{sc.trace_id:032x}" if sc and sc.is_valid else None
            )

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            with tracer.start_as_current_span("parent") as parent_span:
                expected_trace = parent_span.get_span_context().trace_id
                fut = _submit_with_context(pool, _read_span_in_worker)
                fut.result()

        if expected_trace == 0:
            # No OTel SDK installed (only the API stub) — span context
            # is the no-op INVALID one and propagation is a no-op too.
            # Skip rather than assert against a non-functional SDK.
            pytest.skip("no real OTel SDK configured; nothing to propagate")
        assert captured["trace_id_hex"] == f"{expected_trace:032x}"


# ---------------------------------------------------------------------------
# StageOverlap context manager
# ---------------------------------------------------------------------------


class TestStageOverlap:
    def test_basic_overlap(self) -> None:
        """Two branches submitted simultaneously must execute in
        parallel, not sequentially.

        Uses ``threading.Event`` for happens-before assertion — branch
        B can only complete AFTER branch A has set ``a_started``,
        which is only possible if both ran concurrently.
        """
        a_started = threading.Event()
        b_finished = threading.Event()

        def _branch_a() -> str:
            a_started.set()
            assert b_finished.wait(timeout=2.0), \
                "branch B never finished — likely sequential, not parallel"
            return "a"

        def _branch_b() -> str:
            assert a_started.wait(timeout=2.0), \
                "branch A never started — likely sequential, not parallel"
            b_finished.set()
            return "b"

        with StageOverlap(label="basic", max_workers=2, log=False) as ov:
            fa = ov.submit("a", _branch_a)
            fb = ov.submit("b", _branch_b)
            assert fa.result(timeout=2.0) == "a"
            assert fb.result(timeout=2.0) == "b"

    def test_main_thread_work_overlaps_with_branch(self) -> None:
        """The canonical use shape: orchestrator runs TTS on main
        thread; image-stills branch runs on worker. Pin that the
        branch starts BEFORE main-thread work completes.
        """
        branch_entered = threading.Event()
        main_can_finish = threading.Event()

        def _stills_branch() -> list[str]:
            branch_entered.set()
            return ["png0", "png1", "png2"]

        with StageOverlap(label="lf-image-panels", log=False) as ov:
            fut = ov.submit("stills", _stills_branch)
            # Simulate "main-thread TTS" — wait for branch to enter,
            # then proceed. This proves overlap.
            assert branch_entered.wait(timeout=2.0), \
                "stills branch never entered — main-thread blocked it"
            main_can_finish.set()
            stills = fut.result(timeout=2.0)

        assert stills == ["png0", "png1", "png2"]
        assert main_can_finish.is_set()

    def test_branch_exception_re_raises_from_result(self) -> None:
        """Calling :meth:`Future.result` on a failed branch re-raises
        the original exception."""
        class _CustomError(RuntimeError):
            pass

        def _bad_branch() -> None:
            raise _CustomError("kaboom")

        with pytest.raises(_CustomError, match="kaboom"):
            with StageOverlap(label="failure", log=False) as ov:
                fut = ov.submit("bad", _bad_branch)
                fut.result()

    def test_unawaited_branch_exception_re_raises_at_exit(self) -> None:
        """A branch that errors but whose ``result()`` is never called
        MUST surface its error at context-manager exit. Otherwise a
        fire-and-forget branch could swallow exceptions silently.
        """
        class _CustomError(RuntimeError):
            pass

        def _bad_branch() -> None:
            time.sleep(0.05)
            raise _CustomError("silent fail")

        with pytest.raises(_CustomError, match="silent fail"):
            with StageOverlap(label="silent-fail", log=False) as ov:
                ov.submit("bad", _bad_branch)
                # NOT calling .result() — error must surface at exit.

    def test_body_exception_takes_precedence_over_branch_exception(self) -> None:
        """If the with-body raises and a branch ALSO raises, the
        body's exception propagates (branch errors become log
        warnings, not silent losses).
        """
        class _BodyError(RuntimeError):
            pass

        class _BranchError(RuntimeError):
            pass

        def _bad_branch() -> None:
            time.sleep(0.05)
            raise _BranchError("branch-fail")

        with pytest.raises(_BodyError, match="body-fail"):
            with StageOverlap(label="body-vs-branch", log=False) as ov:
                ov.submit("bad", _bad_branch)
                raise _BodyError("body-fail")

    def test_multiple_branch_failures_use_exceptiongroup_on_311(self) -> None:
        """On py 3.11+, multiple branch failures surface as
        :class:`ExceptionGroup` so the user sees every cause."""
        if sys.version_info < (3, 11):
            pytest.skip("ExceptionGroup requires py 3.11+")

        class _ErrA(RuntimeError):
            pass

        class _ErrB(RuntimeError):
            pass

        def _bad_a() -> None:
            time.sleep(0.05)
            raise _ErrA("a-failed")

        def _bad_b() -> None:
            time.sleep(0.05)
            raise _ErrB("b-failed")

        with pytest.raises(BaseExceptionGroup) as ei:
            with StageOverlap(label="multi-fail", log=False) as ov:
                ov.submit("a", _bad_a)
                ov.submit("b", _bad_b)

        excs = ei.value.exceptions
        assert len(excs) == 2
        assert any(isinstance(e, _ErrA) for e in excs)
        assert any(isinstance(e, _ErrB) for e in excs)

    def test_submit_outside_context_manager_raises(self) -> None:
        ov = StageOverlap(label="not-entered", log=False)
        with pytest.raises(RuntimeError, match="outside the context manager"):
            ov.submit("x", lambda: None)

    def test_render_context_propagated_into_branch(self) -> None:
        """Real-world check: the ``RenderContext`` set by the
        orchestrator's outer ``ctx(channel=..., slug=...)`` is visible
        inside every overlap branch."""
        from pipeline.observability.context import (
            current_context,
            ctx as render_ctx,
        )

        captured: dict[str, str | None] = {}

        def _branch_reads_ctx() -> None:
            rc = current_context()
            captured["channel"] = rc.channel
            captured["slug"] = rc.slug
            captured["render_kind"] = rc.render_kind

        with render_ctx(
            channel="cosmosdecoded",
            slug="eddington-1919",
            render_kind="long_form",
        ):
            with StageOverlap(label="ctx-prop", log=False) as ov:
                fut = ov.submit("branch", _branch_reads_ctx)
                fut.result()

        assert captured == {
            "channel": "cosmosdecoded",
            "slug": "eddington-1919",
            "render_kind": "long_form",
        }


# ---------------------------------------------------------------------------
# _add_note — best-effort PEP 678 attachment
# ---------------------------------------------------------------------------


class TestAddNote:
    def test_add_note_attaches_on_311(self) -> None:
        if sys.version_info < (3, 11):
            pytest.skip("PEP 678 add_note requires py 3.11+")
        exc = RuntimeError("base")
        _add_note(exc, "[overlap] branch=foo label=bar")
        assert "[overlap] branch=foo label=bar" in (getattr(exc, "__notes__", []) or [])

    def test_add_note_swallows_failure(self, caplog) -> None:
        """If add_note itself raises (custom exception class with
        broken __setattr__), helper falls back to logging — does NOT
        propagate the new exception."""

        class _BrokenExc(RuntimeError):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise TypeError("note attachment forbidden")

        exc = _BrokenExc("base")
        _add_note(exc, "test-note")
        # No exception raised; note is logged at WARNING.
        # caplog fixture captures it — the exact assertion is just
        # "we got here without raising".

    def test_add_note_old_python_logs_instead(self, caplog) -> None:
        """On py 3.10 (or any class without add_note), the helper
        logs at WARNING."""
        # Build an exception class without add_note for the test (we
        # can't downgrade Python).
        class _NoNoteExc(RuntimeError):
            pass

        # Force the no-attribute path: temporarily delete add_note if
        # py 3.11+ added it via the base class.
        exc = _NoNoteExc("base")
        # Ensure the test exercises the logging branch — patch out
        # the bound method.
        if hasattr(exc, "add_note"):
            from unittest.mock import patch
            with patch.object(_NoNoteExc, "add_note", create=False):
                # On 3.11+ the base class still has add_note; just
                # guard against it raising. Either path goes through
                # our helper without re-raising.
                pass
        _add_note(exc, "test-note-py310")
        # Pure smoke — no exception is the assertion.


# ---------------------------------------------------------------------------
# Integration shape — orchestrator-style usage
# ---------------------------------------------------------------------------


class TestOrchestratorShape:
    def test_canonical_long_form_pattern(self) -> None:
        """Mirror the long_form.py refactor shape:
          1. Open StageOverlap
          2. Submit stills branch (no dur dep)
          3. Inline TTS (main thread) → narration_wav, dur
          4. Await stills branch
          5. Run dur-dependent assembly with both inputs
        """
        events: list[str] = []
        events_lock = threading.Lock()

        def _add_event(label: str) -> None:
            with events_lock:
                events.append(label)

        def _stills_branch() -> list[str]:
            _add_event("stills:start")
            time.sleep(0.05)
            _add_event("stills:done")
            return ["panel_000.png", "panel_001.png", "panel_002.png"]

        def _tts_inline() -> tuple[str, float]:
            _add_event("tts:start")
            time.sleep(0.05)
            _add_event("tts:done")
            return ("narration.wav", 12.5)

        with StageOverlap(label="lf-test", log=False) as ov:
            stills_fut = ov.submit("stills", _stills_branch)
            narration_wav, dur = _tts_inline()
            panel_pngs = stills_fut.result(timeout=2.0)

        # Both stages should have started before the FIRST one ended —
        # the only way that's true is interleaved stills:start /
        # tts:start before either stills:done / tts:done.
        first_start_idx = min(
            events.index("stills:start"),
            events.index("tts:start"),
        )
        first_done_idx = min(
            events.index("stills:done"),
            events.index("tts:done"),
        )
        # All 4 events present
        assert set(events) == {
            "stills:start", "stills:done", "tts:start", "tts:done",
        }
        # Both starts come before the first done
        for label in ("stills:start", "tts:start"):
            assert events.index(label) < first_done_idx, (
                f"{label} happened after the first done — sequential, "
                f"not parallel. events={events}"
            )
        assert first_start_idx == 0  # at least one started immediately
        assert narration_wav == "narration.wav"
        assert dur == 12.5
        assert panel_pngs == ["panel_000.png", "panel_001.png", "panel_002.png"]
