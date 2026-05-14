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

class ClassifyLongFormRendererLineTests(unittest.TestCase):
    """Long-form (`pipeline.render.long_form`) prints with different
    prefixes than the SHORT renderer. Regexes added 2026-05-13 so the
    dashboard surfaces per-chunk + per-panel progress on long-form
    renders too. Pre-fix the long-form pills froze at "Generating
    images / Synthesizing narration" with no granularity.
    """

    @classmethod
    def setUpClass(cls):
        cls.ep = _load_entrypoint()

    # TTS substage
    def test_lf_tts_plan(self):
        ev = self.ep._classify_renderer_line(
            "[tts] 38421 chars → 89 chunks via cloudrun_chatterbox (target 380 chars each)"
        )
        self.assertEqual(ev, ("tts", "Planning 89 TTS chunks (38421 chars) via cloudrun_chatterbox"))

    def test_lf_tts_cloud_fanout(self):
        ev = self.ep._classify_renderer_line(
            "[tts] cloud fan-out: 89 chunks × 6 workers"
        )
        self.assertEqual(ev, ("tts", "Cloud TTS fan-out: 89 chunks × 6 workers"))

    def test_lf_tts_all_cached(self):
        ev = self.ep._classify_renderer_line(
            "[tts] all 89 chunks already cached; nothing to synth"
        )
        self.assertEqual(ev, ("tts", "All 89 TTS chunks cached — skipping"))

    def test_lf_tts_cloud_chunk_progress(self):
        # Renderer prints 0-indexed; we surface 1-indexed for users.
        ev = self.ep._classify_renderer_line(
            "[tts] cloud chunk 0042/0088: 4.2s 380 chars → /tmp/cache/c0042.wav"
        )
        self.assertEqual(ev, ("tts", "TTS cloud chunk 43/89"))

    def test_lf_tts_local_chunk_progress(self):
        ev = self.ep._classify_renderer_line(
            "[tts] chunk 0010/0088: 2.1s 220 chars → /tmp/cache/c0010.wav"
        )
        self.assertEqual(ev, ("tts", "TTS local chunk 11/89"))

    def test_lf_tts_done(self):
        ev = self.ep._classify_renderer_line(
            "[1/5] narration 89 chunks → narration.wav 1322.4s (22.0 min)"
        )
        self.assertEqual(ev, ("tts", "Synthesised 89 chunks → 1322.4s of narration"))

    # Image / panel substage
    def test_lf_panel_gen_progress(self):
        ev = self.ep._classify_renderer_line(
            "[panel] 7/24 gen → panel_007.png (seed 4242)"
        )
        self.assertEqual(ev, ("images", "Panel 7/24 → panel_007.png"))

    def test_lf_panel_fill_message(self):
        ev = self.ep._classify_renderer_line(
            "[2/5] panels total 190.0s < narration 1322.4s — extending each by 47.2s to fill"
        )
        self.assertEqual(ev, ("images", "Panel timing fit: 190.0s panels vs 1322.4s narration"))

    def test_lf_panel_seg_render(self):
        ev = self.ep._classify_renderer_line(
            "[seg ] 12/24 8.0s zoom→1.10 (240f) → seg_012.mp4"
        )
        self.assertEqual(ev, ("images", "Rendering panel 12/24 (8.0s Ken-Burns)"))

    def test_lf_panel_xfade(self):
        ev = self.ep._classify_renderer_line(
            "[xfade] 24 panels → video_track.mp4 (crossfade=0.5s)"
        )
        self.assertEqual(ev, ("images", "Crossfading 24 panel segments → video track"))

    def test_lf_video_done(self):
        ev = self.ep._classify_renderer_line(
            "[2/5] video → video_track.mp4 1322.4s"
        )
        self.assertEqual(ev, ("images", "Video track ready (1322.4s)"))

    # Caption substage
    def test_lf_caption_pngs(self):
        ev = self.ep._classify_renderer_line(
            "[cap] 142 sentence PNGs (cached: 0)"
        )
        self.assertEqual(ev, ("asr", "Authored 142 caption PNGs"))

    def test_lf_caption_pngs_authored_variant(self):
        ev = self.ep._classify_renderer_line(
            "[cap] 142 authored sentence PNGs across 89 chunks"
        )
        self.assertEqual(ev, ("asr", "Authored 142 caption PNGs"))

    # Compose / mux
    def test_lf_mux_start(self):
        ev = self.ep._classify_renderer_line(
            "[4/4] muxing video + (narration -6dB + music -28dB) + captions (142 PNG cues) + watermark → my-video.mp4…"
        )
        self.assertEqual(ev, ("compose", "Muxing video + narration + music"))

    def test_lf_mux_done(self):
        ev = self.ep._classify_renderer_line(
            "[done] /tmp/render/abc/mystoriesanimated/long_form/my-video.mp4 — 1322.4s (22.0 min), 87 MB, mean_volume=-16.0 dB"
        )
        self.assertEqual(ev, ("compose", "Wrote my-video.mp4 (1322.4s)"))

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
        """End-to-end: rewrite → narrate (alias→tts) → images → compose.

        Pre-2026-05-13: every transition cascade-marked the prior pill
        done. Post-2026-05-13: ``tts`` and ``images`` are siblings in
        :data:`_LF_OVERLAPPING_SUBSTAGES` so the cascade SKIPS them
        when the next stage is ALSO overlap-eligible (sibling running
        in parallel must not falsely mark its sibling done). Cascade
        DOES fire when the next stage is downstream of the overlap
        region (``compose``) — by then both overlap branches are
        guaranteed finished even if the renderer didn't emit explicit
        done events (older renderer / safety net).
        """
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}

        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "narrate", "tts starting", now=1100.0,
        )
        # rewrite done · 100.0s; tts running. (rewrite is NOT in
        # the overlap set so it cascades normally.)
        self.assertEqual(self._stage_msg(timeline, "rewrite"), "100.0s")
        self.assertEqual(self._stage_status(timeline, "tts"), "running")
        self.assertIn("tts", substage_t0)

        # Images starts while TTS is still running. Both are in the
        # overlap set → tts must REMAIN running (the parallel-overlap
        # property). Pre-2026-05-13 this would have stamped tts as
        # "done · 200.0s" — wrong when the renderer is actually
        # running both stages in parallel.
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images", "image_panels", now=1300.0,
        )
        self.assertEqual(self._stage_status(timeline, "tts"), "running")
        self.assertEqual(self._stage_msg(timeline, "tts"), "tts starting")
        self.assertEqual(self._stage_status(timeline, "images"), "running")

        # Compose starts. Compose is DOWNSTREAM of the overlap region
        # (rewrite → {tts, images} → compose), so by the time it fires
        # both tts and images must be done in process even if no
        # explicit done event arrived. Cascade fires here.
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "compose", "muxing", now=1500.0,
        )
        self.assertEqual(self._stage_msg(timeline, "tts"), "400.0s")
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

class LfAdvanceTimelineParallelTests(unittest.TestCase):
    """Pin the stage-overlap-aware behaviour of
    :func:`_lf_advance_timeline` (added 2026-05-13).

    The new contract:

    * Two siblings in :data:`_LF_OVERLAPPING_SUBSTAGES` (``tts`` and
      ``images``) can BOTH be in ``running`` simultaneously without
      one cascading the other to ``done``.
    * Explicit ``<key>_done`` events (e.g. ``tts_done``,
      ``images_done``) mark the corresponding pill ``done`` with the
      message body as the elapsed-time text and DO NOT touch any
      other pill.
    * When a downstream non-overlapping pill (``compose``) starts
      while overlap-eligible pills are still ``running``, the
      cascade DOES fire on them — by then they're guaranteed
      finished even if no explicit done event was emitted (graceful
      downgrade for older renderers).
    """

    @classmethod
    def setUpClass(cls):
        cls.ep = _load_entrypoint()

    def _seed_timeline(self) -> list[dict]:
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
    def _stage(timeline: list[dict], key: str) -> dict | None:
        for s in timeline:
            if s.get("stage") == key:
                return s
        return None

    def test_overlapping_set_constants_shape(self):
        """Defends against accidental shape change to the overlap
        set — the implementation behaviour above ALL hinges on this
        constant. The cloud worker's behaviour stays consistent with
        the renderer's emit shape only as long as both agree on which
        substages can run in parallel."""
        self.assertEqual(
            self.ep._LF_OVERLAPPING_SUBSTAGES, frozenset({"tts", "images"})
        )

    def test_tts_running_then_images_running_both_running_simultaneously(self):
        """The core overlap property: ``tts`` and ``images`` both in
        ``running`` after the renderer fires sequential ``running``
        events for them."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}

        # tts starts.
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "tts", "chunked TTS via cloudrun_chatterbox",
            now=1100.0,
        )
        # images starts a moment later (parallel branch dispatched).
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images", "image_panels",
            now=1100.5,
        )

        tts = self._stage(timeline, "tts")
        images = self._stage(timeline, "images")
        self.assertEqual(tts["status"], "running")
        self.assertEqual(images["status"], "running")
        # The TTS msg must NOT have been clobbered with "X.Ys" (the
        # cascade-marker shape) — that would mean images-running
        # accidentally cascaded TTS.
        self.assertEqual(tts["msg"], "chunked TTS via cloudrun_chatterbox")
        self.assertEqual(images["msg"], "image_panels")

    def test_explicit_tts_done_event_marks_only_tts(self):
        """``progress_cb("tts_done", "12.4s")`` from the renderer's
        parallel path must mark ONLY the tts pill done — must NOT
        touch images / compose / rewrite."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}

        # Both tts and images currently running (overlap in flight).
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "tts", "chunked TTS", now=1100.0,
        )
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images", "image_panels", now=1101.0,
        )
        # tts finishes first → renderer emits explicit done event.
        timeline, resolved = self.ep._lf_advance_timeline(
            timeline, substage_t0, "tts_done", "12.4s", now=1112.4,
        )

        tts = self._stage(timeline, "tts")
        images = self._stage(timeline, "images")
        self.assertEqual(tts["status"], "done")
        self.assertEqual(tts["msg"], "12.4s")
        # images stays running — overlap branch still in flight.
        self.assertEqual(images["status"], "running")
        self.assertEqual(images["msg"], "image_panels")
        # Resolved key for the closure's Firestore write.
        self.assertEqual(resolved, "tts")

    def test_explicit_images_done_event_marks_only_images(self):
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "tts", "chunked TTS", now=1100.0,
        )
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images", "image_panels", now=1101.0,
        )
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images_done", "5.1s", now=1106.1,
        )

        tts = self._stage(timeline, "tts")
        images = self._stage(timeline, "images")
        self.assertEqual(images["status"], "done")
        self.assertEqual(images["msg"], "5.1s")
        self.assertEqual(tts["status"], "running")  # still in flight

    def test_done_event_for_unknown_substage_is_silently_ignored(self):
        """Defence-in-depth: ``progress_cb("typo_done", ...)`` from a
        future renderer with a typo MUST NOT crash the closure or
        corrupt the timeline."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}

        # No exception, no timeline mutation beyond the
        # already-pre-marked seed.
        timeline_after, resolved = self.ep._lf_advance_timeline(
            timeline, substage_t0, "lkjsdf_done", "garbage", now=1200.0,
        )
        # Pills unchanged.
        self.assertEqual(
            self._stage(timeline_after, "rewrite")["status"], "running"
        )
        self.assertEqual(
            self._stage(timeline_after, "tts")["status"], "pending"
        )
        # Resolved is the (sans-suffix) key the renderer named, even
        # though it doesn't exist in the order — best-effort.
        self.assertEqual(resolved, "lkjsdf")

    def test_alias_resolution_works_on_done_events(self):
        """``narrate_done`` should be aliased to ``tts_done`` so the
        legacy ``narrate`` substage emits with the same shape — even
        though long_form.py only emits ``tts_done`` today, third-
        party renderers may still use ``narrate``."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0, "tts": 1100.0}
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "narrate", "tts running", now=1100.0,
        )
        timeline, resolved = self.ep._lf_advance_timeline(
            timeline, substage_t0, "narrate_done", "8.2s", now=1108.2,
        )
        self.assertEqual(resolved, "tts")
        self.assertEqual(self._stage(timeline, "tts")["status"], "done")
        self.assertEqual(self._stage(timeline, "tts")["msg"], "8.2s")

    def test_compose_starting_cascades_overlapping_pills_to_done(self):
        """Even though tts and images are overlap-eligible, when
        ``compose`` (downstream) starts while either is still
        running, the cascade DOES fire on them — by then both are
        guaranteed finished in process even if the renderer didn't
        emit explicit done events. Graceful downgrade for older
        renderers that don't emit the new markers."""
        timeline = self._seed_timeline()
        substage_t0 = {"rewrite": 1000.0}

        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "tts", "chunked TTS", now=1100.0,
        )
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "images", "image_panels", now=1101.0,
        )
        # Compose starts WITHOUT explicit done events for tts/images
        # (e.g. older renderer).
        timeline, _ = self.ep._lf_advance_timeline(
            timeline, substage_t0, "compose", "muxing", now=1500.0,
        )

        tts = self._stage(timeline, "tts")
        images = self._stage(timeline, "images")
        compose = self._stage(timeline, "compose")
        self.assertEqual(tts["status"], "done")
        self.assertEqual(tts["msg"], "400.0s")  # 1500.0 - 1100.0
        self.assertEqual(images["status"], "done")
        self.assertEqual(images["msg"], "399.0s")  # 1500.0 - 1101.0
        self.assertEqual(compose["status"], "running")


# ---------------------------------------------------------------------------
# In-process engine-path stdout-classifier proxy (post-2026-05-14)
# ---------------------------------------------------------------------------
#
# After the 2026-05-14 bigbang the cloud worker calls render_via_engines()
# IN-PROCESS instead of shelling out, so the legacy `[1/4] TTS …` lines
# went to the worker's own stdout where nothing read them — substage pills
# in the dashboard froze at "pending" for the entire 5-15 min render. Fix:
# wrap sys.stdout with _StdoutProgressProxy for the duration of the engine
# call so the existing _classify_renderer_line regex set still bridges
# legacy plugin print()s into progress_cb.
#
# These tests pin the proxy contract (faithful pass-through, per-thread
# buffering, dedup, reentrancy bypass, deactivate-after-restore safety) PLUS
# the wiring inside _run_renderer_via_engines (initial event attaches to
# tts not compose; render_via_engines is called inside the wrap context;
# progress_cb is forwarded into the engine).

class StdoutProgressProxyTests(unittest.TestCase):
    """Pin the proxy's contract: every write reaches the wrapped stream
    in order; complete lines are classified through the supplied
    classifier and fired to the callback; partial lines stay buffered
    per-thread; consecutive duplicate events dedupe; the proxy survives
    deactivation without blowing up."""

    def setUp(self):
        self.ep = _load_entrypoint()

    def _make_pair(self, classifier=None):
        import io  # noqa: PLC0415
        wrapped = io.StringIO()
        events: list[tuple[str, str]] = []
        cls = classifier or self.ep._classify_renderer_line
        proxy = self.ep._StdoutProgressProxy(
            wrapped=wrapped,
            progress_cb=lambda s, m: events.append((s, m)),
            classifier=cls,
        )
        return proxy, wrapped, events

    def test_emits_classified_events_in_order(self):
        proxy, _, events = self._make_pair()
        proxy.write("[1/4] TTS (cloudrun_chatterbox)\n")
        proxy.write("[2/4] faster_whisper aligning timestamps\n")
        proxy.write("[3/4] cloudrun_flux2_klein: generating 22 images\n")
        proxy.write("[4/4] ffmpeg compose 9:16\n")
        stages = [s for s, _ in events]
        self.assertEqual(stages, ["tts", "asr", "images", "compose"])

    def test_passes_through_to_wrapped_stream_verbatim(self):
        """Cloud Logging consumes one structured-JSON-per-line from
        stdout; the proxy must forward every byte unchanged so the
        JSON exporter still works."""
        proxy, wrapped, _ = self._make_pair()
        proxy.write("hello\nworld\n[1/4] TTS\n")
        self.assertEqual(wrapped.getvalue(), "hello\nworld\n[1/4] TTS\n")

    def test_holds_back_partial_trailing_line(self):
        """Half-written lines (no '\\n' yet) MUST NOT be classified —
        otherwise '[1/4] T' in one write + 'TS\\n' in the next would
        miss the match."""
        proxy, _, events = self._make_pair()
        proxy.write("[1/4] T")
        self.assertEqual(events, [], "partial line shouldn't classify yet")
        proxy.write("TS (cloudrun_chatterbox)\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "tts")

    def test_dedupes_consecutive_identical_events(self):
        """Same line classified to the same (stage, msg) twice in a
        row should fire ONCE — pre-fix would have flooded Firestore."""
        proxy, _, events = self._make_pair()
        proxy.write("[1/4] TTS (cloudrun_chatterbox)\n")
        proxy.write("[1/4] TTS (cloudrun_chatterbox)\n")
        self.assertEqual(len(events), 1)

    def test_passes_unclassified_lines_through_silently(self):
        proxy, wrapped, events = self._make_pair()
        proxy.write("a totally unknown line\n")
        proxy.write("another one\n")
        self.assertEqual(events, [])
        self.assertEqual(
            wrapped.getvalue(), "a totally unknown line\nanother one\n",
        )

    def test_per_thread_buffering_no_interleave(self):
        """Two threads writing partial lines simultaneously must not
        interleave half-bytes into the wrong buffer. Pre-critique
        design used a single shared buffer + lock, which would let
        '[1/4]' from thread-A combine with '[3/4]' from thread-B."""
        proxy, _, events = self._make_pair()
        # Thread A writes its [1/4] line one chunk at a time. Thread B
        # interleaves its [3/4] line writes between A's writes. Per-
        # thread buffering must keep them separate.
        a_chunks = ["[1/4] T", "TS (provA)", "\n"]
        b_chunks = ["[3/4] provB: generating 5 images", "\n"]
        barrier = threading.Barrier(2)

        def write_chunks(chunks):
            barrier.wait()
            for c in chunks:
                proxy.write(c)
                time.sleep(0.001)  # encourage interleave

        ta = threading.Thread(target=write_chunks, args=(a_chunks,))
        tb = threading.Thread(target=write_chunks, args=(b_chunks,))
        ta.start(); tb.start()
        ta.join(); tb.join()

        # Both events must be present and well-formed (not garbled).
        stages = sorted(s for s, _ in events)
        self.assertEqual(
            stages, ["images", "tts"],
            f"expected both events to fire cleanly, got {events!r}",
        )
        for stage, msg in events:
            self.assertNotIn(
                "TS", msg.replace("TTS", ""),
                f"garbled msg from cross-thread interleave: {msg!r}",
            )

    def test_reentrancy_callback_writes_pass_through(self):
        """If progress_cb writes to stdout (e.g., a logger handler
        configured to emit there), the recursive write MUST pass
        through unclassified — otherwise we'd recurse on every line
        and likely deadlock on the cb_lock."""
        import io  # noqa: PLC0415
        wrapped = io.StringIO()
        events: list[tuple[str, str]] = []

        def chatty_cb(stage, msg):
            events.append((stage, msg))
            # Recurse: the callback writes a line that WOULD classify.
            sys_stdout = wrapped  # we hand the proxy this stream
            # write through the proxy itself to simulate logger->stdout
            proxy.write("[1/4] TTS (recursive)\n")

        proxy = self.ep._StdoutProgressProxy(
            wrapped=wrapped,
            progress_cb=chatty_cb,
            classifier=self.ep._classify_renderer_line,
        )
        proxy.write("[1/4] TTS (cloudrun_chatterbox)\n")
        # Only the OUTER event fires; the recursive one is bypassed.
        self.assertEqual(len(events), 1)
        # But the recursive write IS still echoed to wrapped.
        self.assertIn("[1/4] TTS (recursive)", wrapped.getvalue())

    def test_callback_exception_does_not_kill_writer(self):
        proxy, _, events = self._make_pair()

        def boom(stage, msg):
            raise RuntimeError("simulated cb failure")

        proxy._cb = boom
        # Must not raise, and must keep accepting writes after the failure.
        proxy.write("[1/4] TTS\n")
        proxy.write("[4/4] ffmpeg compose\n")
        self.assertEqual(events, [], "events list isn't used in this test")

    def test_deactivate_drains_trailing_partial_line(self):
        """If the renderer exits without emitting a trailing newline
        (e.g. mid-write crash) the buffered remainder MUST still be
        classified on deactivate so we don't lose the last event."""
        proxy, _, events = self._make_pair()
        proxy.write("[1/4] TTS (cloudrun_chatterbox)")  # NO newline
        self.assertEqual(events, [])  # held back so far
        proxy.deactivate()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "tts")

    def test_writes_after_deactivate_pass_through_silently(self):
        """A logging handler that captured the proxy reference BEFORE
        the context manager exited must still be able to write. The
        proxy stays in memory; subsequent writes pass through."""
        proxy, wrapped, events = self._make_pair()
        proxy.deactivate()
        proxy.write("[1/4] TTS (post-deactivate)\n")
        self.assertEqual(events, [], "post-deactivate must not classify")
        self.assertIn("[1/4] TTS (post-deactivate)", wrapped.getvalue())

    def test_attribute_delegation(self):
        """Stream attributes the proxy doesn't override (encoding,
        isatty, etc.) must delegate to the wrapped stream."""
        import io  # noqa: PLC0415
        wrapped = io.StringIO()
        wrapped.custom_attr = "hello"
        proxy = self.ep._StdoutProgressProxy(
            wrapped=wrapped,
            progress_cb=lambda s, m: None,
            classifier=self.ep._classify_renderer_line,
        )
        self.assertEqual(proxy.custom_attr, "hello")
        # isatty / writable should delegate too (StringIO supports them).
        self.assertEqual(proxy.writable(), wrapped.writable())

    def test_empty_string_write_short_circuits(self):
        """Empty / non-string writes return immediately without
        touching the buffer or firing the classifier."""
        proxy, _, events = self._make_pair()
        proxy.write("")
        self.assertEqual(events, [])
        # Bytes (technically misuse of a str-mode stream) should also
        # be forwarded without classification — the proxy doesn't
        # promise to handle non-str writes, just must not crash.
        proxy.write(b"")  # type: ignore[arg-type]
        self.assertEqual(events, [])

    def test_wrapped_write_failure_does_not_propagate(self):
        """If sys.stdout itself raises mid-render (e.g. broken pipe
        when the parent closed the FD), the proxy MUST swallow it —
        the render is otherwise fine, and a raise here would be a
        very confusing crash."""

        class BoomStream:
            def write(self, _s):
                raise OSError("EPIPE simulated")

            def flush(self):
                raise OSError("EPIPE simulated")

        events: list[tuple[str, str]] = []
        proxy = self.ep._StdoutProgressProxy(
            wrapped=BoomStream(),
            progress_cb=lambda s, m: events.append((s, m)),
            classifier=self.ep._classify_renderer_line,
        )
        # Must not raise.
        proxy.write("[1/4] TTS (cloudrun_chatterbox)\n")
        # Even though write to wrapped failed, the line was still
        # buffered + classified → callback fired.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "tts")

    def test_flush_exception_does_not_propagate(self):
        """``proxy.flush()`` is called by Python at interpreter
        shutdown for any stdout-shaped object. Must not raise — would
        spam stderr at process exit."""

        class BoomStream:
            def write(self, _s):
                return len(_s)

            def flush(self):
                raise OSError("EPIPE simulated")

        proxy = self.ep._StdoutProgressProxy(
            wrapped=BoomStream(),
            progress_cb=lambda s, m: None,
            classifier=self.ep._classify_renderer_line,
        )
        # Must not raise.
        proxy.flush()

    def test_deactivate_dedupes_against_last_event(self):
        """If the buffered partial line matches the SAME event that
        already fired (last full-line write was the same string), we
        must NOT re-fire it on deactivate — same dedup policy as
        normal write()."""
        proxy, _, events = self._make_pair()
        proxy.write("[1/4] TTS (cloudrun_chatterbox)\n")
        self.assertEqual(len(events), 1)
        # Now buffer an IDENTICAL partial line (no newline). On
        # deactivate the trailing-line drain MUST see it matches
        # _last_event and skip.
        proxy.write("[1/4] TTS (cloudrun_chatterbox)")
        proxy.deactivate()
        self.assertEqual(
            len(events), 1,
            "deactivate drain must dedupe against _last_event "
            "to avoid double-firing the same event",
        )

    def test_deactivate_is_idempotent(self):
        """Calling deactivate twice must be safe — the worker may
        defensive-double-call from both finally branches."""
        proxy, _, _ = self._make_pair()
        proxy.deactivate()
        proxy.deactivate()  # must not raise


class CaptureRendererStdoutTests(unittest.TestCase):
    """Pin the context-manager wrapper: swap sys.stdout for the
    duration; restore on normal AND exceptional exit; no-op when
    progress_cb is None (laptop CLI / tests must pay zero cost)."""

    def setUp(self):
        self.ep = _load_entrypoint()
        self._orig_stdout = sys.stdout

    def tearDown(self):
        sys.stdout = self._orig_stdout

    def test_no_op_when_progress_cb_is_none(self):
        before = sys.stdout
        with self.ep._capture_renderer_stdout(None):
            self.assertIs(sys.stdout, before, "must not swap stdout")
        self.assertIs(sys.stdout, before)

    def test_swaps_and_restores_on_normal_exit(self):
        before = sys.stdout
        events: list[tuple[str, str]] = []
        with self.ep._capture_renderer_stdout(lambda s, m: events.append((s, m))):
            self.assertIsNot(sys.stdout, before, "stdout must be swapped")
            print("[1/4] TTS (cloudrun_chatterbox)")
        self.assertIs(sys.stdout, before, "stdout must be restored")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "tts")

    def test_swaps_and_restores_on_exception(self):
        before = sys.stdout
        events: list[tuple[str, str]] = []
        with self.assertRaises(RuntimeError):
            with self.ep._capture_renderer_stdout(lambda s, m: events.append((s, m))):
                print("[1/4] TTS (cloudrun_chatterbox)")
                raise RuntimeError("simulated renderer crash")
        self.assertIs(sys.stdout, before, "stdout must be restored even on raise")
        # Trailing event still fired before the raise.
        self.assertEqual(len(events), 1)


class EngineDispatchProgressTests(unittest.TestCase):
    """Pin the wiring inside ``_run_renderer_via_engines``:

    - the placeholder event the worker fires BEFORE engine starts
      uses the FIRST substage (``tts``), not ``compose``. Pre-fix
      this was ``("compose", "engine path: short")`` which made
      _compose_progress cascade-mark tts/asr/images "done" at t≈0.
    - the engine call is wrapped in ``_capture_renderer_stdout`` so
      legacy plugin print()s reach progress_cb.
    - ``progress_cb`` is forwarded into ``render_via_engines`` so
      explicit boundary events fire from inside the engine.
    """

    def setUp(self):
        self.ep = _load_entrypoint()

    def _build_job(self, tmp: Path, kind: str = "short") -> dict:
        """Minimal job dict: a script JSON file + a synthetic channel YAML
        the spec builder can consume. Both are read by
        ``_run_renderer_via_engines`` upfront."""
        script = tmp / "script.json"
        script.write_text(
            '{"slug": "test_slug", "narration": "hello", '
            '"shots": [{"narration_line": "first beat"}]}'
        )
        channel_yaml = tmp / "channel.yaml"
        channel_yaml.write_text(
            "channel_dir: " + str(tmp) + "\n"
            "name: testchannel\n"
            "aspect: 9:16\n"
            "kind: " + kind + "\n"
        )
        return {
            "_script_path": str(script),
            "_channel_yaml": str(channel_yaml),
            "_slug": "test_slug",
            "proposal": {"topic": "test", "kind": kind},
            "job_id": "test-job",
        }

    def test_initial_placeholder_event_uses_tts_not_compose(self):
        """Critical: pre-fix the worker emitted ``("compose", "engine
        path: short")`` BEFORE the engine actually started. The
        ``_compose_progress`` closure cascades EVERY earlier pill to
        ``done`` when it sees a later pill go ``running``, so this
        falsely marked tts/asr/images "done" at t≈0. Must attach to
        the FIRST substage."""
        captured: list[tuple[str, str]] = []
        # Stub the spec builder + render_via_engines so the test
        # doesn't try to actually render anything. The point is just
        # to observe the first event fired before render starts.
        from unittest import mock  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            job = self._build_job(tmp)

            # Stub spec build + render so we never touch the real engine.
            class _FakeSpec:
                class _Kind:
                    value = "short"
                kind = _Kind()
                channel = "testchannel"

            with mock.patch.object(
                self.ep, "_run_renderer_via_engines",
                wraps=self.ep._run_renderer_via_engines,
            ):
                # We need to mock the imports inside the function. The
                # cleanest way is to patch sys.modules entries the
                # function imports lazily.
                fake_video = mock.MagicMock()
                fake_video.render_via_engines = mock.MagicMock(
                    return_value=tmp / "out.mp4",
                )
                fake_spec_mod = mock.MagicMock()
                fake_spec_mod.build_spec = mock.MagicMock(return_value=_FakeSpec())
                fake_paths = mock.MagicMock()
                fake_rp = mock.MagicMock()
                fake_rp.short_for = mock.MagicMock(return_value=tmp / "out.mp4")
                fake_rp.long_form_for = mock.MagicMock(return_value=tmp / "out.mp4")
                fake_paths.RenderPaths.from_channel_yaml = mock.MagicMock(
                    return_value=fake_rp,
                )
                with mock.patch.dict(sys.modules, {
                    "pipeline.render.video": fake_video,
                    "pipeline.render.spec": fake_spec_mod,
                    "pipeline.paths": fake_paths,
                }):
                    self.ep._run_renderer_via_engines(
                        job, tmp,
                        progress_cb=lambda s, m: captured.append((s, m)),
                    )

        # First event must be ("tts", …) — NOT ("compose", …).
        self.assertGreaterEqual(len(captured), 1, f"no events fired: {captured!r}")
        self.assertEqual(
            captured[0][0], "tts",
            f"first event must attach to the first substage; got {captured[0]!r}",
        )
        self.assertNotEqual(
            captured[0][0], "compose",
            "compose-as-first cascades earlier pills to false 'done' "
            "(_compose_progress cascade behaviour, see _RENDERER_SUBSTAGES "
            "ordering)",
        )

    def test_render_via_engines_called_inside_capture_context(self):
        """The engine call MUST happen INSIDE the
        ``_capture_renderer_stdout`` context so legacy plugin print()s
        flow through the classifier. We verify by checking that
        sys.stdout is the proxy at the moment render_via_engines
        is called."""
        from unittest import mock  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        observed: dict[str, Any] = {}

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            job = self._build_job(tmp)

            class _FakeSpec:
                class _Kind:
                    value = "short"
                kind = _Kind()
                channel = "testchannel"

            def _stub_render(**kw):
                # Snapshot sys.stdout at the moment the engine is invoked.
                observed["stdout_during_call"] = sys.stdout
                observed["progress_cb_kwarg"] = kw.get("progress_cb")
                return tmp / "out.mp4"

            fake_video = mock.MagicMock()
            fake_video.render_via_engines = _stub_render
            fake_spec_mod = mock.MagicMock()
            fake_spec_mod.build_spec = mock.MagicMock(return_value=_FakeSpec())
            fake_paths = mock.MagicMock()
            fake_rp = mock.MagicMock()
            fake_rp.short_for = mock.MagicMock(return_value=tmp / "out.mp4")
            fake_paths.RenderPaths.from_channel_yaml = mock.MagicMock(
                return_value=fake_rp,
            )

            user_cb = lambda s, m: None
            with mock.patch.dict(sys.modules, {
                "pipeline.render.video": fake_video,
                "pipeline.render.spec": fake_spec_mod,
                "pipeline.paths": fake_paths,
            }):
                self.ep._run_renderer_via_engines(
                    job, tmp, progress_cb=user_cb,
                )

        self.assertIsInstance(
            observed.get("stdout_during_call"), self.ep._StdoutProgressProxy,
            "render_via_engines was called OUTSIDE the capture context "
            "→ legacy plugin print()s won't reach progress_cb",
        )
        self.assertIs(
            observed.get("progress_cb_kwarg"), user_cb,
            "progress_cb must be forwarded into render_via_engines so "
            "engine boundary events fire too",
        )

    def test_no_capture_context_when_progress_cb_is_none(self):
        """Laptop CLI / tests pass progress_cb=None → no proxy swap,
        no overhead, no classifier wired up."""
        from unittest import mock  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        observed: dict[str, Any] = {}

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            job = self._build_job(tmp)

            class _FakeSpec:
                class _Kind:
                    value = "short"
                kind = _Kind()
                channel = "testchannel"

            def _stub_render(**kw):
                observed["stdout_during_call"] = sys.stdout
                return tmp / "out.mp4"

            fake_video = mock.MagicMock()
            fake_video.render_via_engines = _stub_render
            fake_spec_mod = mock.MagicMock()
            fake_spec_mod.build_spec = mock.MagicMock(return_value=_FakeSpec())
            fake_paths = mock.MagicMock()
            fake_rp = mock.MagicMock()
            fake_rp.short_for = mock.MagicMock(return_value=tmp / "out.mp4")
            fake_paths.RenderPaths.from_channel_yaml = mock.MagicMock(
                return_value=fake_rp,
            )

            with mock.patch.dict(sys.modules, {
                "pipeline.render.video": fake_video,
                "pipeline.render.spec": fake_spec_mod,
                "pipeline.paths": fake_paths,
            }):
                self.ep._run_renderer_via_engines(
                    job, tmp, progress_cb=None,
                )

        self.assertNotIsInstance(
            observed.get("stdout_during_call"), self.ep._StdoutProgressProxy,
            "must NOT swap stdout when progress_cb is None",
        )


# Module-level imports for the new tests above.
import sys  # noqa: E402  (placed here so older test classes don't pull it)
from typing import Any  # noqa: E402

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
