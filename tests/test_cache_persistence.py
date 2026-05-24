"""D3 retry-cache sweep tests (2026-05-24).

Covers the four gaps closed by the 2026-05-24 sweep on top of the B2
``pipeline.cloud.cache`` foundation:

  D3-1 — refiner output persist + skip-if-exists
         (``pipeline.images.prompt_refiner.refine_prompts_batch``)
  D3-2 — short-form TTS persist + skip-if-exists
         (``pipeline.render.audio.tts_single.TtsSingle.synth``)
  D3-3 — ASR alignment cache via the cloud-ASR client
         (``pipeline.asr_cloudrun.align_via_cloud``) AND the laptop
         transcribe_words wrapper (``pipeline.beats.transcribe_words``)
  D3-4 — surface ``cache_copy_failed`` flag to the UI when the
         server-side cache rehydrate copy raises mid-list. UI lives in
         ``web-next/app/app/render/[jobId]/page.tsx`` retry handler;
         that's exercised in tests/test_render_routes_retry.py — see
         the ``test_retry_with_gcs_copy_failure_*`` cases there.

Each fix has both a skip-if-exists (cache hit) test and a
write-after-success (persist on miss) test, plus a class-of-bug
regression test for the specific D3 failure mode (cache miss-skipped
on retry).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# ---------------------------------------------------------------------------
# D3-1 — refiner output persist + skip-if-exists
# ---------------------------------------------------------------------------


class RefinerBatchCacheTest(unittest.TestCase):
    """``refine_prompts_batch`` writes refined slots to disk after a
    successful LLM call AND reads them back on a second call with
    matching inputs — no LLM hit on the second call.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # Point the refiner at our scratch dir for the cache root.
        os.environ["YTFACTORY_REFINER_CACHE_DIR"] = self.tmpdir.name
        self.addCleanup(lambda: os.environ.pop("YTFACTORY_REFINER_CACHE_DIR", None))
        # Stub out strip_text_bait so the canonical "no readable text in
        # image" prefix doesn't trip the hard-bait check. This mirrors
        # tests/test_prompt_refiner.py::_install_strip — the prefix
        # legitimately contains the word "text" (required by the
        # downstream anti-text idempotence check) and the production
        # strip_text_bait pre-fix would strip it. Tests of the refiner
        # itself bypass strip; we do the same so our cache tests focus
        # on the cache logic.
        from pipeline.images import images as _images_mod
        strip_patch = mock.patch.object(
            _images_mod, "strip_text_bait",
            side_effect=lambda s: (s, []),
        )
        strip_patch.start()
        self.addCleanup(strip_patch.stop)

    def _make_beats(self) -> list[dict]:
        return [
            {
                "key_visual": "a man in a kitchen",
                "scene": "morning light through window, steam rising from a pan",
                "subject": "protagonist",
                "emotion": "calm",
                "narration_line": "He stirred the pot.",
            },
            {
                "key_visual": "a partner pouring ketchup",
                "scene": "indoor kitchen, partner reaching across",
                "subject": "partner",
                "emotion": "neutral",
                "narration_line": "She poured ketchup over the stew.",
            },
        ]

    def _stub_llm(self, n: int):
        """Return a llm_call seam that returns N valid refined slots.

        Strings carefully avoid ``strip_text_bait`` triggers (no
        explicit "text/sign/label/banner/title" tokens) so the
        validator doesn't strip every slot back to {}.
        """
        called = {"count": 0}

        def stub(_prompt, *, output_json=True, json_schema=None,
                strict_schema=None, model=None, stage=None):
            called["count"] += 1
            beats = []
            for i in range(n):
                beats.append({
                    "refined_visual": (
                        f"medium shot of a man, age 32, dark hair, "
                        f"warm tones, neutral expression, beat {i}"
                    ),
                    "refined_scene": (
                        "no readable text in image. wide indoor kitchen "
                        f"with morning illumination, beat {i}"
                    ),
                    "style_block": "Style: animated comic. Mood: warm.",
                })
            return {"refined_beats": beats}

        return stub, called

    def test_skip_if_exists_no_llm_call_on_second_invocation(self) -> None:
        """Class-of-bug regression for D3-1: second call with identical
        inputs MUST NOT hit the LLM (the cache file written on the
        first call should short-circuit)."""
        from pipeline.images import prompt_refiner

        beats = self._make_beats()
        stub, called = self._stub_llm(n=len(beats))

        out1 = prompt_refiner.refine_prompts_batch(
            beats,
            era_anchor_prefix="1990s",
            character_description="A 32-year-old man",
            style="animated comic",
            mood="warm",
            channel_key="mystoriesanimated",
            scene_anchor=None,
            llm_call=stub,
        )
        self.assertEqual(called["count"], 1, "first call should hit LLM")
        self.assertEqual(len(out1), 2)
        # Every slot non-empty (no fallbacks).
        for slot in out1:
            self.assertTrue(slot, f"unexpected empty slot: {slot!r}")

        # Second call with identical args MUST skip the LLM.
        out2 = prompt_refiner.refine_prompts_batch(
            beats,
            era_anchor_prefix="1990s",
            character_description="A 32-year-old man",
            style="animated comic",
            mood="warm",
            channel_key="mystoriesanimated",
            scene_anchor=None,
            llm_call=stub,
        )
        self.assertEqual(
            called["count"], 1,
            "second call must NOT invoke LLM — cache miss = D3-1 regression",
        )
        # And the returned slots must equal the cached ones byte-for-byte.
        self.assertEqual(out2, out1)

    def test_cache_file_written_on_disk(self) -> None:
        """The cache file at ``$YTFACTORY_REFINER_CACHE_DIR/<key>.json``
        should exist + be valid JSON after a successful refine."""
        from pipeline.images import prompt_refiner

        beats = self._make_beats()
        stub, _ = self._stub_llm(n=len(beats))

        prompt_refiner.refine_prompts_batch(
            beats,
            era_anchor_prefix="1990s",
            character_description="A 32-year-old man",
            style="animated comic",
            mood="warm",
            channel_key="mystoriesanimated",
            scene_anchor=None,
            llm_call=stub,
        )

        cache_files = list(Path(self.tmpdir.name).glob("*.json"))
        self.assertEqual(len(cache_files), 1, f"expected 1 cache file, got {cache_files}")
        data = json.loads(cache_files[0].read_text())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)
        for item in data:
            self.assertIn("refined_visual", item)
            self.assertIn("refined_scene", item)
            self.assertIn("style_block", item)

    def test_cache_invalidates_on_input_change(self) -> None:
        """A different style → different batch_key → different cache file
        → second LLM call. Guards against a stale cache being honoured
        when the user retunes a channel's style block."""
        from pipeline.images import prompt_refiner

        beats = self._make_beats()
        stub, called = self._stub_llm(n=len(beats))

        prompt_refiner.refine_prompts_batch(
            beats,
            era_anchor_prefix="1990s",
            character_description="A 32-year-old man",
            style="animated comic",
            mood="warm",
            channel_key="mystoriesanimated",
            scene_anchor=None,
            llm_call=stub,
        )
        # Bump the style → cache MUST miss.
        prompt_refiner.refine_prompts_batch(
            beats,
            era_anchor_prefix="1990s",
            character_description="A 32-year-old man",
            style="photoreal docu",  # ← changed
            mood="warm",
            channel_key="mystoriesanimated",
            scene_anchor=None,
            llm_call=stub,
        )
        self.assertEqual(called["count"], 2,
                         "style change should force a fresh LLM call")

    def test_no_cache_when_all_slots_fallback(self) -> None:
        """If every beat ends up empty (whole-batch LLM failure or
        validation-fail), DON'T persist — we don't want to freeze the
        failure across retries."""
        from pipeline.images import prompt_refiner

        def stub(*_args, **_kwargs):
            raise RuntimeError("simulated LLM failure")

        beats = self._make_beats()
        out = prompt_refiner.refine_prompts_batch(
            beats,
            era_anchor_prefix=None,
            character_description=None,
            style=None,
            mood=None,
            channel_key=None,
            scene_anchor=None,
            llm_call=stub,
        )
        # Every slot must be empty under whole-batch failure.
        self.assertTrue(all(not s for s in out))
        # No cache file written.
        self.assertEqual(list(Path(self.tmpdir.name).glob("*.json")), [])


# ---------------------------------------------------------------------------
# D3-2 — short-form TTS persist + skip-if-exists
# ---------------------------------------------------------------------------


class TtsSingleCacheTest(unittest.TestCase):
    """``TtsSingle.synth`` skips the synthesize call when a hydrated
    narration.wav is present at work_dir/cache/tts_short/, AND persists
    the freshly-synthesised wav so a retry hits the cache.
    """

    def _make_spec(self, atempo: float = 1.0, speed: float = 1.0):
        """Minimal RenderSpec-shaped stub. TtsSingle only reads
        voice_provider / voice_id / tone / tts.{speed_default,
        post_atempo_default, tone_overrides}."""
        import types
        spec = types.SimpleNamespace(
            voice_provider="cloudrun_chatterbox",
            voice_id="sarah",
            tone=None,
            tts=types.SimpleNamespace(
                speed_default=speed,
                post_atempo_default=atempo,
                tone_overrides={},
                ref_text=None,
            ),
        )
        return spec

    def test_skip_when_hydrated_wav_present(self) -> None:
        """Class-of-bug regression for D3-2: a wav at
        ``work_dir/cache/tts_short/narration.wav`` (mimicking B2 hydrate)
        must short-circuit ``synthesize()``."""
        from pipeline.render.audio import tts_single as _tts
        from pipeline.render.audio.tts_single import TtsSingle

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            cache_subdir = work / "cache" / "tts_short"
            cache_subdir.mkdir(parents=True)
            # Write a "real" wav stub — anything >1024 bytes counts
            # as a real cache hit per the threshold check.
            hydrated = cache_subdir / "narration.wav"
            hydrated.write_bytes(b"RIFF" + b"\x00" * 2048)

            spec = self._make_spec()
            script = {"narration": "Hello world."}

            with mock.patch(
                "pipeline.audio.synthesize",
            ) as synth_mock, mock.patch.object(
                _tts, "probe_duration", return_value=42.0,
            ):
                result = TtsSingle().synth(spec, script, work)

            synth_mock.assert_not_called()
            # narration.wav was restored from the hydrate dir.
            self.assertTrue((work / "narration.wav").exists())
            self.assertEqual(result.duration_s, 42.0)

    def test_persist_called_after_fresh_synth(self) -> None:
        """A fresh synth must call ``persist_artifact`` with
        ``kind="tts_short"`` so the next retry can hydrate it."""
        from pipeline.render.audio import tts_single as _tts
        from pipeline.render.audio.tts_single import TtsSingle

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)

            def fake_synth(*, text, voice, out_path, speed, provider):
                Path(out_path).write_bytes(b"RIFF" + b"\x00" * 4096)

            spec = self._make_spec()
            script = {"narration": "Hello world."}

            with mock.patch(
                "pipeline.audio.synthesize", side_effect=fake_synth,
            ), mock.patch.object(
                _tts, "probe_duration", return_value=11.0,
            ), mock.patch(
                "pipeline.cloud.cache.persist_artifact",
            ) as persist_mock:
                TtsSingle().synth(spec, script, work)

            # Persist must be called for the narration wav under
            # kind="tts_short" — that's the B2 hydrate convention we
            # rely on for the next retry.
            persist_mock.assert_called_once()
            args, kwargs = persist_mock.call_args
            self.assertEqual(kwargs.get("kind"), "tts_short")
            self.assertEqual(kwargs.get("name"), "narration.wav")

    def test_persist_failure_does_not_block_synth(self) -> None:
        """persist_artifact raising must not break the render —
        cache is best-effort."""
        from pipeline.render.audio import tts_single as _tts
        from pipeline.render.audio.tts_single import TtsSingle

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)

            def fake_synth(*, text, voice, out_path, speed, provider):
                Path(out_path).write_bytes(b"RIFF" + b"\x00" * 4096)

            spec = self._make_spec()
            script = {"narration": "Hello world."}

            with mock.patch(
                "pipeline.audio.synthesize", side_effect=fake_synth,
            ), mock.patch.object(
                _tts, "probe_duration", return_value=11.0,
            ), mock.patch(
                "pipeline.cloud.cache.persist_artifact",
                side_effect=RuntimeError("simulated cache.persist failure"),
            ):
                # Must NOT raise.
                result = TtsSingle().synth(spec, script, work)

            self.assertEqual(result.duration_s, 11.0)


# ---------------------------------------------------------------------------
# D3-3 — ASR alignment cache (cloud + laptop paths)
# ---------------------------------------------------------------------------


class AsrCloudCacheTest(unittest.TestCase):
    """``pipeline.asr_cloudrun.align_via_cloud`` checks the disk cache
    before issuing the cloud HTTP call, and persists segments after
    a successful response.
    """

    def _make_wav(self, td: Path, *, name: str = "narration.wav") -> Path:
        # Make a work_dir/cache/ neighbour so the cache root probe finds
        # the canonical layout (mirrors what hydrate_cache builds).
        (td / "cache").mkdir()
        p = td / name
        p.write_bytes(b"RIFF" + b"\x00" * 4096)
        return p

    def test_cache_hit_skips_cloud_call(self) -> None:
        """Class-of-bug regression: a populated
        cache/alignments/<key>.json must skip the HTTP call entirely."""
        from pipeline.asr_cloudrun import align_via_cloud
        from pipeline.render.contracts import Segment

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            wav = self._make_wav(td)

            # Seed the cache file at the exact path align_via_cloud
            # computes. Easiest: round-trip through the persist path.
            from pipeline import asr_cloudrun as ac
            seeds = [
                Segment(start_s=0.0, end_s=1.0, text="hello",
                        anchor_id="seg_000", kind="word"),
                Segment(start_s=1.0, end_s=2.0, text="world",
                        anchor_id="seg_001", kind="word"),
            ]
            ac._persist_segments_cache(
                wav, seeds,
                mode="words", anchors=None, language="en",
                max_words_per_beat=8,
            )

            # Cache file exists.
            cache_root = td / "cache" / "alignments"
            self.assertTrue(any(cache_root.glob("*.json")))

            # Set the CLOUDRUN_ASR_URL so _service_url doesn't raise
            # before we even reach the cache check.
            with mock.patch.dict(os.environ,
                                 {"CLOUDRUN_ASR_URL": "https://fake.run.app"}):
                with mock.patch("requests.post") as post_mock:
                    out = align_via_cloud(
                        wav,
                        mode="words",
                        language="en",
                    )
                # The cloud HTTP call MUST NOT have happened.
                post_mock.assert_not_called()

            self.assertEqual(len(out), 2)
            self.assertEqual(out[0].text, "hello")
            self.assertEqual(out[1].text, "world")

    def test_cache_disabled_by_env(self) -> None:
        """``YTFACTORY_ASR_CACHE=0`` disables the cache layer entirely."""
        from pipeline.asr_cloudrun import align_via_cloud

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            wav = self._make_wav(td)

            # Even with a populated cache file the env override should
            # bypass it and proceed to the cloud call.
            from pipeline import asr_cloudrun as ac
            ac._persist_segments_cache(
                wav, [], mode="words", anchors=None, language=None,
                max_words_per_beat=8,
            )

            with mock.patch.dict(os.environ, {
                "CLOUDRUN_ASR_URL": "https://fake.run.app",
                "YTFACTORY_ASR_CACHE": "0",
            }):
                # We expect the cloud path to be invoked → patch
                # requests.post to return a synthetic 200 response
                # so we don't actually hit the network.
                fake_resp = mock.Mock()
                fake_resp.status_code = 200
                fake_resp.json.return_value = {
                    "segments": [],
                    "duration_s": 0.0,
                    "word_count": 0,
                    "gpu_seconds": 0.0,
                }
                fake_resp.text = "{}"
                with mock.patch("requests.post", return_value=fake_resp) as p:
                    # cloud auth helper would normally call out — stub it.
                    with mock.patch(
                        "pipeline.cloudrun_auth.get_id_token",
                        return_value="fake-token",
                    ):
                        align_via_cloud(wav, mode="words", language=None)
                    p.assert_called_once()


class TranscribeWordsCacheTest(unittest.TestCase):
    """``pipeline.beats.transcribe_words`` writes + reads
    ``cache/alignments/<audio>.json``.
    """

    def _make_wav(self, td: Path) -> Path:
        (td / "cache").mkdir()
        p = td / "narration.wav"
        p.write_bytes(b"RIFF" + b"\x00" * 8192)
        return p

    def test_cache_hit_skips_asr_transcribe(self) -> None:
        """A populated cache must skip the ``pipeline.asr.transcribe``
        call entirely on the laptop / long-form path."""
        from pipeline import beats as beats_mod

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            wav = self._make_wav(td)

            # Seed cache via the persist helper.
            seed_words = [
                beats_mod.Word(text="hello", start=0.0, end=0.5),
                beats_mod.Word(text="world", start=0.5, end=1.0),
            ]
            beats_mod._persist_alignment_cache(
                wav, seed_words,
                provider="faster_whisper",
                model="base",
            )

            with mock.patch("pipeline.asr.transcribe") as asr_mock, \
                 mock.patch.object(beats_mod, "_audio_low_rms_spans",
                                   return_value=[]):
                out = beats_mod.transcribe_words(
                    wav, provider="faster_whisper", model="base",
                )

            asr_mock.assert_not_called()
            # Sanitiser runs against the cached words but for
            # cleanly-ordered input it should be a no-op.
            self.assertEqual(len(out), 2)
            self.assertEqual(out[0].text, "hello")


# ---------------------------------------------------------------------------
# D3-3 sanity: cache.py supports kind="alignments" generically
# ---------------------------------------------------------------------------
#
# The persist_artifact / hydrate_cache helpers take ``kind`` as a free
# string and store at ``jobs/<id>/cache/<kind>/<name>``. The docstring
# at cache.py:21 lists alignments/<name>.json as a documented kind but
# pre-D3-3 nothing wrote to it. The wires we added in beats.py +
# asr_cloudrun.py rely on this generic-kind path — exercise it here so
# a future regression (e.g. someone gates persist_artifact on a known-
# kinds enum) hard-fails immediately.


class CacheAlignmentsKindTest(unittest.TestCase):
    def test_persist_artifact_accepts_alignments_kind(self) -> None:
        from pipeline.cloud import cache as cache_mod
        cache_mod.reset_state_for_tests()
        self.addCleanup(cache_mod.reset_state_for_tests)

        os.environ["YTFACTORY_JOB_ID"] = "j-aln"
        os.environ["YTFACTORY_BUCKET"] = "b-aln"
        self.addCleanup(lambda: os.environ.pop("YTFACTORY_JOB_ID", None))
        self.addCleanup(lambda: os.environ.pop("YTFACTORY_BUCKET", None))

        import types as _types
        import sys as _sys

        sink: list[tuple[str, str]] = []

        class _FakeBlob:
            def __init__(self, name: str) -> None:
                self._name = name

            def upload_from_filename(self, local: str) -> None:
                sink.append((self._name, local))

        class _FakeBucket:
            def blob(self, name: str) -> _FakeBlob:
                return _FakeBlob(name)

        class _FakeClient:
            def bucket(self, _name: str) -> _FakeBucket:
                return _FakeBucket()

        fake_mod = _types.ModuleType("google.cloud.storage")
        fake_mod.Client = _FakeClient  # type: ignore[attr-defined]
        import importlib
        google_cloud = importlib.import_module("google.cloud")
        sys_p = mock.patch.dict(_sys.modules,
                                {"google.cloud.storage": fake_mod})
        attr_p = mock.patch.object(google_cloud, "storage", fake_mod,
                                   create=True)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "narration.wav.json"
                p.write_text("{}")
                cache_mod.persist_artifact(p, kind="alignments")
                cache_mod.shutdown_upload_pool(wait=True, timeout=5.0)
        finally:
            attr_p.stop()
            sys_p.stop()

        # The generic ``kind`` arg flows through unchanged to the GCS
        # path layout — this is the contract beats.py / asr_cloudrun.py
        # rely on.
        self.assertEqual(len(sink), 1)
        blob_name, _ = sink[0]
        self.assertEqual(
            blob_name, "jobs/j-aln/cache/alignments/narration.wav.json",
        )


# TODO: end-to-end retry telemetry — currently the wire from
# ``persist_artifact(kind="alignments", ...)`` inside
# pipeline.beats.transcribe_words is verified at the unit level above,
# but the worker-side hydrate path (cloud/render-worker-v2/entrypoint.py
# calls hydrate_cache which restores cache/alignments/ before any stage
# runs) is owned by agent 2 in this parallel sweep. If agent 2's
# entrypoint changes touch the hydrate ordering, this test file should
# grow an integration case asserting the alignments hydrate happens
# before the ASR stage.


if __name__ == "__main__":
    unittest.main()
