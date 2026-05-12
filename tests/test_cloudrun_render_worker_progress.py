"""Tests for the renderer-log → user-visible-substep bridge in
cloud/render-worker-v2/entrypoint.py (2026-05-11).

Pre-fix the cloud worker only emitted a static ``"real-mode"`` msg
on the compose stage for the entire 5-15 min render. The dashboard's
"Composing video / compose / real-mode" pill never advanced, leaving
the user uncertain whether anything was happening.

The fix introduces:

1. A pure ``_classify_renderer_line`` helper that maps each notable
   ``pipeline.render.shorts`` stdout marker (``[1/4] TTS``,
   ``[3/4] z_image_turbo: generating 22 images``,
   ``[image-done] beat 12 of 22``, ``[4/4] ffmpeg compose``,
   ``[compose] wrote slug.mp4`` …) to a human-friendly substep
   string — or ``None`` for noise lines.

2. A ``_tail_renderer_log`` background poller that watches the log
   file and invokes a callback on every changed substep.

3. A ``progress_cb`` parameter threaded through
   ``_run_renderer_subprocess`` / ``_stage_render_real`` so the
   Firestore-driven main loop can push substep msgs onto the
   timeline.

These tests pin the contract so a regression (e.g. someone renames
``[3/4]`` to ``[3/5]`` upstream) shows up immediately.
"""
from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint():
    """Import cloud/render-worker-v2/entrypoint.py without polluting sys.path.

    Mirrors test_cloudrun_render_worker_preflight._load_entrypoint —
    the parent dir contains a hyphen so a regular import can't reach
    it. Reuse the same pattern so test ordering / fixtures stay
    consistent across the worker test suite.
    """
    spec = importlib.util.spec_from_file_location(
        "render_worker_v2_entrypoint_for_progress_tests",
        ENTRYPOINT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ClassifyRendererLineTests(unittest.TestCase):
    """Each line family the renderer prints must produce a non-empty,
    human-friendly substep summary. Lines outside the known set must
    return ``None`` so the tailer doesn't spam Firestore with junk
    updates on every prompt-author / ASR debug print.
    """

    @classmethod
    def setUpClass(cls):
        cls.ep = _load_entrypoint()

    def test_tts_start_with_provider(self):
        event = self.ep._classify_renderer_line("[1/4] TTS (cloudrun_chatterbox)")
        self.assertIsNotNone(event)
        stage, msg = event
        self.assertEqual(stage, "tts")
        self.assertIn("Synthesizing narration", msg)
        self.assertIn("cloudrun_chatterbox", msg)

    def test_tts_start_without_provider(self):
        event = self.ep._classify_renderer_line("[1/4] TTS")
        self.assertEqual(event, ("tts", "Synthesizing narration"))

    def test_tts_cached(self):
        self.assertEqual(
            self.ep._classify_renderer_line("[1/4] TTS cached"),
            ("tts", "Reusing cached narration"),
        )

    def test_beats_start(self):
        event = self.ep._classify_renderer_line("[2/4] faster_whisper aligning timestamps")
        self.assertEqual(event, ("asr", "Aligning captions (faster_whisper)"))

    def test_beats_cached(self):
        self.assertEqual(
            self.ep._classify_renderer_line("[2/4] beats cached"),
            ("asr", "Reusing cached caption alignment"),
        )

    def test_beats_done_summary(self):
        event = self.ep._classify_renderer_line("    22 beats, total 58.4s")
        self.assertEqual(event, ("asr", "Aligned 22 beats — 58.4s of audio"))

    def test_prompts_start(self):
        self.assertEqual(
            self.ep._classify_renderer_line("[prompts] authoring 22 beat prompts"),
            ("images", "Authoring 22 image prompts"),
        )

    def test_image_start(self):
        event = self.ep._classify_renderer_line(
            "[3/4] cloudrun_flux2_klein: generating 22 images"
        )
        self.assertEqual(event, ("images", "Generating 22 images via cloudrun_flux2_klein"))

    def test_image_per_beat_progress(self):
        self.assertEqual(
            self.ep._classify_renderer_line("    [12/22] beat=07 sarah-coffee"),
            ("images", "Image 12 of 22"),
        )

    def test_image_per_beat_done(self):
        self.assertEqual(
            self.ep._classify_renderer_line("[image-done] beat 7 of 22"),
            ("images", "Image 7 of 22 done"),
        )

    def test_compose_start(self):
        self.assertEqual(
            self.ep._classify_renderer_line("[4/4] ffmpeg compose 9:16"),
            ("compose", "Stitching video with ffmpeg"),
        )

    def test_compose_recompose(self):
        self.assertEqual(
            self.ep._classify_renderer_line("[critic] recomposing after patch"),
            ("compose", "Recomposing after critic patch"),
        )

    def test_compose_done_uses_basename_only(self):
        event = self.ep._classify_renderer_line(
            "[compose] wrote /tmp/render/abc/mystoriesanimated/shorts/aita07.mp4"
        )
        self.assertEqual(event, ("compose", "Wrote aita07.mp4"))

    def test_unknown_line_returns_none(self):
        # Random debug noise from upstream libraries must NOT cause a
        # Firestore write — that would cost both money and clarity.
        for line in (
            "DEBUG: loading model from /workspace/.cache/...",
            "INFO: pipeline.compose: x265 frame 142",
            "warning: deprecated arg --foo",
            "",
            "    something with a [bracket] but no marker",
        ):
            self.assertIsNone(self.ep._classify_renderer_line(line),
                              f"line should be ignored: {line!r}")

    def test_classifier_is_pure(self):
        """The classifier must touch no I/O, no clocks, no globals —
        called twice with the same input yields the same output."""
        line = "    [3/22] beat=01"
        a = self.ep._classify_renderer_line(line)
        b = self.ep._classify_renderer_line(line)
        self.assertEqual(a, b)
        self.assertEqual(a, ("images", "Image 3 of 22"))


class TailRendererLogTests(unittest.TestCase):
    """The tailer must:
      - invoke the callback for every changed substep,
      - dedupe back-to-back identical substeps (else every renderer
        line for the same stage would re-trigger a Firestore write),
      - exit cleanly when stop_event fires (so the worker doesn't
        leak a thread per render),
      - tolerate a missing log file at startup (the renderer process
        creates it asynchronously).
    """

    @classmethod
    def setUpClass(cls):
        cls.ep = _load_entrypoint()

    def _run_tailer(self, log_path: Path, lines: list[str], *, write_delay: float = 0.05):
        """Spin up the tailer thread, write the lines after a short
        delay so the tailer's first poll cycle finds them, then stop
        and join.

        Returns the list of msgs the callback received (in order).
        """
        seen: list[str] = []
        seen_lock = threading.Lock()

        def cb(stage: str, msg: str) -> None:
            # Tailer signature is (stage, msg); tests assert on msgs only.
            with seen_lock:
                seen.append(msg)

        stop = threading.Event()
        t = threading.Thread(
            target=self.ep._tail_renderer_log,
            args=(log_path, cb, stop),
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        t.start()
        # Give the tailer a tick to do its first poll.
        time.sleep(write_delay)
        for line in lines:
            with log_path.open("ab") as fh:
                fh.write(line.encode("utf-8") + b"\n")
        # Let the tailer pick up the writes.
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "tailer should exit promptly when stop_event is set")
        return seen

    def test_emits_substep_msgs_in_order(self):
        with self._tmp_log() as log_path:
            seen = self._run_tailer(log_path, [
                "[1/4] TTS (cloudrun_chatterbox)",
                "    22 beats, total 58.4s",
                "[3/4] cloudrun_flux2_klein: generating 22 images",
                "    [1/22] beat=00",
                "    [2/22] beat=01",
                "[4/4] ffmpeg compose 9:16",
                "[compose] wrote /tmp/render/x/aita07.mp4",
            ])
        self.assertEqual(seen, [
            "Synthesizing narration (cloudrun_chatterbox)",
            "Aligned 22 beats — 58.4s of audio",
            "Generating 22 images via cloudrun_flux2_klein",
            "Image 1 of 22",
            "Image 2 of 22",
            "Stitching video with ffmpeg",
            "Wrote aita07.mp4",
        ])

    def test_dedupes_consecutive_identical_substeps(self):
        # The renderer prints '[1/4] TTS' once at start AND inside a
        # retry loop on cache miss — our tailer must collapse the
        # duplicate so the dashboard doesn't flicker.
        with self._tmp_log() as log_path:
            seen = self._run_tailer(log_path, [
                "[1/4] TTS",
                "[1/4] TTS",
                "[1/4] TTS",
                "[4/4] ffmpeg compose",
            ])
        self.assertEqual(seen, [
            "Synthesizing narration",
            "Stitching video with ffmpeg",
        ])

    def test_ignores_unknown_lines(self):
        with self._tmp_log() as log_path:
            seen = self._run_tailer(log_path, [
                "DEBUG: loading model",
                "INFO: pipeline.compose x265 frame 42",
                "[1/4] TTS",
                "warning: ignored",
            ])
        self.assertEqual(seen, ["Synthesizing narration"])

    def test_exits_clean_when_log_never_appears(self):
        # The tailer is started before the renderer subprocess
        # creates the log file. It must not crash on FileNotFoundError
        # — just keep polling until stop_event fires.
        with self._tmp_log(create=False) as log_path:
            stop = threading.Event()
            seen: list[str] = []
            t = threading.Thread(
                target=self.ep._tail_renderer_log,
                args=(log_path, lambda stage, msg: seen.append(msg), stop),
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.2)
            stop.set()
            t.join(timeout=2)
            self.assertFalse(t.is_alive(),
                             "tailer must not deadlock when the log file is missing")
            self.assertEqual(seen, [])

    def test_callback_failure_does_not_kill_tailer(self):
        # If Firestore is briefly unavailable the callback may raise.
        # The tailer must log + continue, not unwind the thread (a
        # crashed tailer would stall every subsequent substep).
        with self._tmp_log() as log_path:
            seen: list[str] = []
            calls = {"n": 0}

            def cb(stage: str, msg: str) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated firestore outage")
                seen.append(msg)

            stop = threading.Event()
            t = threading.Thread(
                target=self.ep._tail_renderer_log,
                args=(log_path, cb, stop),
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.05)
            for line in ("[1/4] TTS", "[4/4] ffmpeg compose"):
                with log_path.open("ab") as fh:
                    fh.write(line.encode("utf-8") + b"\n")
            time.sleep(0.3)
            stop.set()
            t.join(timeout=2)
            self.assertFalse(t.is_alive())
            # First call raised; second call landed.
            self.assertEqual(seen, ["Stitching video with ffmpeg"])

    def test_holds_back_partial_trailing_line(self):
        # A half-written substep marker (no trailing newline yet) must
        # NOT be classified — wait for the renderer to flush the
        # newline. Otherwise a marker like '[3/4] cloudrun_flux2_kl'
        # would emit a confusingly-truncated msg.
        with self._tmp_log() as log_path:
            seen: list[str] = []
            stop = threading.Event()
            t = threading.Thread(
                target=self.ep._tail_renderer_log,
                args=(log_path, lambda stage, msg: seen.append(msg), stop),
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.05)
            with log_path.open("ab") as fh:
                fh.write(b"[3/4] cloudrun_flux2_klein: generating 22 ima")  # no newline
            time.sleep(0.15)
            self.assertEqual(seen, [], "tailer should withhold partial lines")
            with log_path.open("ab") as fh:
                fh.write(b"ges\n")  # complete the line
            time.sleep(0.2)
            stop.set()
            t.join(timeout=2)
            self.assertEqual(seen, ["Generating 22 images via cloudrun_flux2_klein"])

    # --- helpers --------------------------------------------------

    class _TmpLogCtx:
        def __init__(self, create: bool = True):
            self._create = create

        def __enter__(self) -> Path:
            import tempfile
            self._tmpdir = tempfile.TemporaryDirectory()
            self._path = Path(self._tmpdir.name) / "renderer.log"
            if self._create:
                self._path.touch()
            return self._path

        def __exit__(self, *exc):
            self._tmpdir.cleanup()

    def _tmp_log(self, *, create: bool = True):
        return self._TmpLogCtx(create=create)


class RunRendererSubprocessProgressTests(unittest.TestCase):
    """End-to-end check: drive a tiny child python process whose
    stdout mimics the renderer's substep markers, verify that the
    callback receives them in order. This pins the contract that
    PYTHONUNBUFFERED is set + the tailer is wired up.
    """

    @classmethod
    def setUpClass(cls):
        cls.ep = _load_entrypoint()

    def test_subprocess_substeps_reach_callback(self):
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)

            # Stand in for pipeline.render.shorts: print three known
            # substep markers with explicit flushes + tiny sleeps so
            # the tailer (poll_interval=1.5s by default) actually sees
            # them as separate events.
            child = (
                "import sys, time\n"
                "for line in ("
                "'[1/4] TTS', "
                "'    [3/22] beat=01', "
                "'[4/4] ffmpeg compose'"
                "):\n"
                "    print(line); sys.stdout.flush(); time.sleep(0.4)\n"
            )

            # Inject a fake rewrite-stage output so
            # _run_renderer_subprocess doesn't reject the job.
            job = {
                "_script_path": str(work_dir / "script.json"),
                "_channel_yaml": str(work_dir / "channel.yaml"),
                "proposal": {},
            }
            (work_dir / "script.json").write_text("{}")
            (work_dir / "channel.yaml").write_text("name: test\n")

            # Patch the subprocess command so we don't actually run
            # pipeline.render.shorts (no Cloud Run TTS, no L4 GPU …).
            import unittest.mock as _mock
            seen: list[str] = []
            seen_lock = threading.Lock()

            def cb(stage: str, msg: str) -> None:
                with seen_lock:
                    seen.append(msg)

            real_run = self.ep.subprocess.run

            def fake_run(cmd, *args, **kwargs):
                # Replace pipeline.render.shorts invocation with our
                # tiny child script. Keep cwd/env/stdout/stderr piping
                # so the test exercises the same plumbing as prod.
                kwargs = dict(kwargs)
                kwargs["check"] = False
                return real_run(
                    [sys.executable, "-u", "-c", child],
                    *args,
                    **kwargs,
                )

            # Speed up the tailer so the test isn't slow.
            real_tail = self.ep._tail_renderer_log

            def fast_tail(log_path, progress_cb, stop_event, **kw):
                kw.setdefault("poll_interval", 0.05)
                return real_tail(log_path, progress_cb, stop_event, **kw)

            with _mock.patch.object(self.ep.subprocess, "run", side_effect=fake_run), \
                 _mock.patch.object(self.ep, "_tail_renderer_log", side_effect=fast_tail), \
                 _mock.patch.object(self.ep, "REPO_ROOT", work_dir):
                # We don't care about the mp4-resolution branch here
                # (the fake child doesn't write one) — call the
                # subprocess wrapper directly and inspect callbacks.
                # Skip the post-run mp4 lookup by stubbing rglob path
                # resolution — easier: catch the RuntimeError that
                # comes from "no mp4 found".
                try:
                    self.ep._run_renderer_subprocess(job, work_dir, progress_cb=cb)
                except RuntimeError as exc:
                    # Expected — fake child writes no mp4. We only
                    # care that the tailer ran first.
                    self.assertIn("no mp4 found", str(exc))

            # Three substep markers → three distinct callbacks.
            self.assertEqual(seen, [
                "Synthesizing narration",
                "Image 3 of 22",
                "Stitching video with ffmpeg",
            ])


# ---------------------------------------------------------------------------
# Long-form progress walker — regression tests for the cascade-coercion
# bug fixed 2026-05-12.
#
# The bug: the long-form ``_lf_progress`` callback only knew the SHORT
# substages ``("tts", "asr", "images", "compose")``. When
# ``video.render_long_form`` emitted ``("rewrite", ...)`` as its FIRST
# progress event (before ANY actual TTS/image work), the unknown-stage
# defensive coercion turned it into ``"compose"``. The cascade-walker
# then marked tts/asr/images all "done" with msg "—" — leaving the
# user staring at "5 / 7 stages complete · 71%" 20 s into a 30-min
# render, with the front-end then 404'ing on
# ``/api/jobs/<id>/preview.mp4`` because the mp4 didn't exist yet.
#
# Fix: a new long-form taxonomy + a pure
# :func:`_lf_advance_timeline` helper that:
#   - resolves aliases (``narrate`` → ``tts``);
#   - coerces unknown stages to ``compose`` WITHOUT cascading priors;
#   - only marks a prior pill "done" if we actually saw it START (its
#     key is recorded in ``substage_t0``).
# ---------------------------------------------------------------------------


class LfAdvanceTimelineTests(unittest.TestCase):
    """Pin the long-form progression contract.

    Each test exercises :func:`_lf_advance_timeline` directly so the
    fix is locked-in without spinning up Firestore or
    ``video.render_long_form``.
    """

    @classmethod
    def setUpClass(cls):
        cls.ep = _load_entrypoint()

    def _seed_timeline(self) -> list[dict]:
        """Initial timeline as the long-form pre-mark step would leave it:
        rewrite=running, cast=done(skipped), asr=done(skipped), rest pending.
        Mirrors the pre-mark block in ``_main_from_firestore`` for the
        long-form (caption_align=authored) path."""
        from copy import deepcopy
        timeline = self.ep._empty_timeline()
        timeline = self.ep._set_stage(
            timeline, "rewrite", "running", "long-form rewriter authoring envelope",
        )
        timeline = self.ep._set_stage(
            timeline, "cast", "done", "skipped — long-form has no cast stage",
        )
        timeline = self.ep._set_stage(
            timeline, "asr", "done",
            "skipped — captions aligned from authored TTS chunk timings",
        )
        return deepcopy(timeline)

    @staticmethod
    def _stage_status(timeline: list[dict], key: str) -> str | None:
        for s in timeline:
            if s.get("stage") == key:
                return s.get("status")
        return None

    @staticmethod
    def _stage_msg(timeline: list[dict], key: str) -> str | None:
        for s in timeline:
            if s.get("stage") == key:
                return s.get("msg")
        return None

    def test_constants_shape(self):
        # Defends against accidental rename/reorder. Cast is intentionally
        # NOT in the order — it's pre-marked "done · skipped" and never
        # transitions inside _lf_advance_timeline.
        self.assertEqual(
            self.ep._LF_SUBSTAGES_ORDER,
            ("rewrite", "tts", "images", "compose"),
        )
        self.assertEqual(self.ep._LF_SUBSTAGE_ALIASES, {"narrate": "tts"})

    def test_rewrite_event_does_not_cascade_to_done(self):
        """The bug: pre-fix, ``_lf_progress("rewrite", ...)`` got
        coerced to "compose" and walked tts/asr/images all to "done".
        Post-fix, the rewrite event lands on the rewrite pill and
        leaves tts/images pending."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}
        timeline, resolved = self.ep._lf_advance_timeline(
            timeline, substage_t0, "rewrite",
            "long-form rewrite (1800s target)",
            now=1001.0,
        )
        self.assertEqual(resolved, "rewrite")
        self.assertEqual(self._stage_status(timeline, "rewrite"), "running")
        self.assertEqual(
            self._stage_msg(timeline, "rewrite"),
            "long-form rewrite (1800s target)",
        )
        # Critical: tts/images must NOT be marked done by a rewrite event.
        self.assertEqual(self._stage_status(timeline, "tts"), "pending")
        self.assertEqual(self._stage_status(timeline, "images"), "pending")
        self.assertEqual(self._stage_status(timeline, "compose"), "pending")
        # And cast / asr stay in their pre-skipped state.
        self.assertEqual(self._stage_status(timeline, "cast"), "done")
        self.assertEqual(self._stage_status(timeline, "asr"), "done")

    def test_narrate_alias_routes_to_tts_pill(self):
        """``video.render_long_form`` emits ("narrate", ...) when chunked
        TTS starts. The alias must route it onto the canonical tts pill
        AND mark the rewrite pill done with its accurate elapsed."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}
        timeline, resolved = self.ep._lf_advance_timeline(
            timeline, substage_t0, "narrate",
            "long-form chunked narration starting",
            now=1090.0,
        )
        self.assertEqual(resolved, "tts")
        self.assertEqual(self._stage_status(timeline, "tts"), "running")
        self.assertEqual(
            self._stage_msg(timeline, "tts"),
            "long-form chunked narration starting",
        )
        # rewrite was running + had a t0 → flips to done with elapsed.
        self.assertEqual(self._stage_status(timeline, "rewrite"), "done")
        self.assertEqual(self._stage_msg(timeline, "rewrite"), "90.0s")

    def test_unknown_stage_coerced_to_compose_no_cascade(self):
        """Defence-in-depth: if upstream emits a stage we don't know
        about, attach the msg to the compose pill so the user still
        sees progress — but do NOT mark prior pills done unless they
        actually started (i.e. their t0 was recorded)."""
        timeline = self._seed_timeline()
        # Only rewrite has a t0 — tts and images never started.
        substage_t0 = {"rewrite": 1000.0}
        timeline, resolved = self.ep._lf_advance_timeline(
            timeline, substage_t0, "some_future_stage",
            "weird upstream marker",
            now=1100.0,
        )
        self.assertEqual(resolved, "compose")
        self.assertEqual(self._stage_status(timeline, "compose"), "running")
        # rewrite did start → marked done with elapsed.
        self.assertEqual(self._stage_status(timeline, "rewrite"), "done")
        # tts/images never started → must stay pending (the bug pre-fix
        # marked them "done · —" here).
        self.assertEqual(self._stage_status(timeline, "tts"), "pending")
        self.assertEqual(self._stage_status(timeline, "images"), "pending")

    def test_full_long_form_walk_marks_done_with_real_elapsed(self):
        """End-to-end: rewrite → narrate (alias→tts) → images → compose,
        each transition marks the prior with accurate elapsed-s."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}

        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "narrate", "tts starting", now=1100.0,
        )
        # rewrite done · 100.0s; tts running.
        self.assertEqual(self._stage_msg(timeline, "rewrite"), "100.0s")
        self.assertEqual(self._stage_status(timeline, "tts"), "running")
        self.assertIn("tts", substage_t0)

        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images", "image_panels", now=1300.0,
        )
        self.assertEqual(self._stage_msg(timeline, "tts"), "200.0s")
        self.assertEqual(self._stage_status(timeline, "images"), "running")

        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "compose", "muxing", now=1500.0,
        )
        self.assertEqual(self._stage_msg(timeline, "images"), "200.0s")
        self.assertEqual(self._stage_status(timeline, "compose"), "running")

        # Pre-skipped pills stayed pre-skipped throughout.
        self.assertEqual(self._stage_status(timeline, "cast"), "done")
        self.assertEqual(
            self._stage_msg(timeline, "cast"),
            "skipped — long-form has no cast stage",
        )
        self.assertEqual(self._stage_status(timeline, "asr"), "done")
        self.assertIn("authored", self._stage_msg(timeline, "asr") or "")

    def test_repeat_event_for_same_stage_is_idempotent_for_t0(self):
        """``_maybe_emit_long_form_progress`` may emit several events
        for the same substage (e.g. [3/5], [4/5], [5/5] all map to
        compose). The substage's start time is recorded ONCE so its
        eventual "done · Xs" reflects its true total duration."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}
        # First compose event — records t0.
        self.ep._lf_advance_timeline(
            timeline, substage_t0, "compose", "music bed", now=1300.0,
        )
        first_t0 = substage_t0["compose"]
        # Second compose event — must NOT overwrite t0.
        self.ep._lf_advance_timeline(
            timeline, substage_t0, "compose", "caption burn", now=1400.0,
        )
        self.assertEqual(substage_t0["compose"], first_t0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
