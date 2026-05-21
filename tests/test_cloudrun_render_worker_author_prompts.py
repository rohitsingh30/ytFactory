"""Tests for ``_author_prompts_for_engine`` in
``cloud/render-worker-v2/entrypoint.py``.

Added 2026-05-14 (P1b) to wire the prompt-refiner pre-step into the
new engine path. The helper:

  * loads ``cast.json`` (best-effort), falls back to channel YAML
    ``character_description`` when missing
  * resolves ``era_anchor_prefix`` from ``script.metadata.era_anchor``
  * extracts ``mood`` from script metadata or cast default_emotion
  * calls ``pipeline.llm.prompts.author_beat_prompts`` and writes
    ``<work_dir>/prompts.json``
  * returns a ``spec.extra.update(...)`` dict OR ``{}`` on any failure
    so the engine can transparently fall back to bare ``Segment.text``

Test contract: every failure mode must return ``{}``, no raised
exceptions ever escape the helper. Happy path must write prompts.json
AND return a dict with at least ``prompts_path``.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "render_worker_v2_entrypoint_for_author_prompts_tests",
        ENTRYPOINT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_ep = _load_entrypoint()


def _write_channel_yaml(dir_: Path, character_description: str | None = None) -> Path:
    """Write a minimal channel YAML — RenderPaths.from_channel_yaml only
    needs the file at a real path to derive sibling per-render dirs."""
    import yaml
    cfg = {
        "channel": "prorevenge",
        "kind": "short",
        "image_provider": "cloudrun_flux2_klein",
    }
    if character_description is not None:
        cfg["character_description"] = character_description
    p = dir_ / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class AuthorPromptsForEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        # Channel root must look like a real channel for RenderPaths.
        self.channel_root = self.tmp_path / "prorevenge"
        self.channel_root.mkdir()
        self.channel_yaml = _write_channel_yaml(self.channel_root)
        self.work_dir = self.tmp_path / "work"
        self.work_dir.mkdir()

    # ----- happy path ---------------------------------------------------

    def test_writes_prompts_json_and_returns_extra_dict(self):
        script = {
            "slug": "test-slug",
            "narration": "I set a tiny legal trap for my roommate.",
            "metadata": {},
            "beats": [
                {"text": "I set a tiny legal trap.", "start": 0.0, "end": 2.0},
                {"text": "He never noticed.", "start": 2.0, "end": 3.5},
            ],
        }
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            # Mock the LLM call: pretend it wrote prompts.json itself
            # (real impl does — write a minimal valid one for the
            # assertions further down).
            def _stub_author(*, narration, beats, source_story, out_path, **kwargs):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps([
                    {"key_visual": "kv0", "scene": "sc0", "narration_line": "I set a tiny legal trap."},
                    {"key_visual": "kv1", "scene": "sc1", "narration_line": "He never noticed."},
                ]))
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="comic illustration",
            )

        self.assertIn("prompts_path", out)
        prompts_path = Path(out["prompts_path"])
        self.assertTrue(prompts_path.exists(), "prompts.json must be on disk")
        self.assertEqual(prompts_path, self.work_dir / "prompts.json")
        # No era / cast / mood in this script → those keys omitted.
        self.assertNotIn("era_anchor_prefix", out)
        self.assertNotIn("character_description", out)
        self.assertNotIn("mood", out)

    def test_picks_up_channel_yaml_character_description_when_cast_missing(self):
        # Re-write channel YAML with character_description.
        self.channel_yaml = _write_channel_yaml(
            self.channel_root,
            character_description="adult man, brown hair, navy hoodie",
        )
        script = {
            "slug": "no-cast",
            "narration": "hello",
            "beats": [{"text": "hello world", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, narration, beats, source_story, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        self.assertEqual(
            out.get("character_description"),
            "adult man, brown hair, navy hoodie",
        )
        # And it was passed to author_beat_prompts.
        kwargs = mock_author.call_args.kwargs
        self.assertEqual(kwargs["cast_narrator_desc"], "adult man, brown hair, navy hoodie")

    def test_picks_up_cast_json_when_present(self):
        # Drop a cast.json at the path RenderPaths expects.
        from pipeline.paths import RenderPaths
        rp = RenderPaths.from_channel_yaml(self.channel_yaml)
        rp.cast.mkdir(parents=True, exist_ok=True)
        cast_doc = {
            "narrator": {
                "description": "young woman, red hair, denim jacket",
                "default_emotion": "frustrated",
            },
            "supporting": [{"name": "roommate", "description": "tall guy"}],
        }
        rp.cast_for("has-cast").write_text(json.dumps(cast_doc))

        script = {
            "slug": "has-cast",
            "narration": "x",
            "beats": [{"text": "first beat", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, narration, beats, source_story, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Cast description wins.
        self.assertEqual(
            out.get("character_description"),
            "young woman, red hair, denim jacket",
        )
        # Mood derives from cast default_emotion when script has none.
        self.assertEqual(out.get("mood"), "frustrated")
        # Supporting cast threaded into author_beat_prompts.
        kwargs = mock_author.call_args.kwargs
        self.assertEqual(len(kwargs["supporting"]), 1)
        self.assertEqual(kwargs["supporting"][0]["name"], "roommate")

    def test_era_anchor_resolution_when_metadata_present(self):
        # mystoriesanimated is contemporary; pick a key the taxonomy
        # actually knows so the test pins real behaviour. Inspect
        # taxonomy YAML to find a stable key.
        import yaml
        taxonomy_path = REPO_ROOT / "pipeline" / "era_taxonomy.yaml"
        if not taxonomy_path.exists():
            self.skipTest("era_taxonomy.yaml not present in this checkout")
        taxonomy = yaml.safe_load(taxonomy_path.read_text())
        era_keys = list((taxonomy or {}).get("eras", {}).keys())
        if not era_keys:
            self.skipTest("era_taxonomy.yaml has no entries")
        era_key = era_keys[0]

        script = {
            "slug": "with-era",
            "narration": "x",
            "metadata": {"era_anchor": era_key, "mood": "tense"},
            "beats": [{"text": "first beat", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, narration, beats, source_story, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Era anchor was resolved to a real costume/period string.
        self.assertIsNotNone(out.get("era_anchor_prefix"))
        self.assertTrue(out["era_anchor_prefix"])
        # Mood from metadata wins over default_emotion.
        self.assertEqual(out["mood"], "tense")

    # ----- failure modes -------------------------------------------------

    def test_author_beat_prompts_persistent_failure_raises(self):
        # POST-2026-05-16 contract: when author_beat_prompts fails on
        # every retry, the helper MUST raise rather than return {}.
        # Pre-fix this swallowed the exception → engine fell back to
        # bare Segment.text → unshippable floating-objects mp4 (job
        # 3cd2b3b5, AITA ketchup-on-stew). The render must FAIL LOUD so
        # the user sees stage=images_failed in Firestore.
        script = {
            "slug": "boom",
            "narration": "x",
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        with patch(
            "pipeline.llm.prompts.author_beat_prompts",
            side_effect=RuntimeError("LLM network down"),
        ), patch("time.sleep"):  # collapse the 2s+4s backoff for test speed
            with self.assertRaises(RuntimeError) as ctx:
                _ep._author_prompts_for_engine(
                    script_dict=script,
                    channel_yaml_path=self.channel_yaml,
                    work_dir=self.work_dir,
                    style_prefix="",
                )
        self.assertIn("author_beat_prompts failed after 3 attempts",
                      str(ctx.exception))

    def test_author_beat_prompts_succeeds_on_retry(self):
        # Transient failure on attempt 1+2, success on attempt 3 →
        # helper completes normally and returns the prompts_path dict.
        script = {
            "slug": "retry-success",
            "narration": "x",
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        n_calls = {"count": 0}

        def _flaky_author(*, out_path, **_):
            n_calls["count"] += 1
            if n_calls["count"] < 3:
                raise RuntimeError(f"transient azure 503 attempt {n_calls['count']}")
            out_path.write_text(json.dumps([
                {"key_visual": "medium shot of the character first",
                 "scene": "at a wooden table, warm pendant light overhead",
                 "narration_line": "first"},
            ]))
            return []

        with patch(
            "pipeline.llm.prompts.author_beat_prompts",
            side_effect=_flaky_author,
        ), patch("time.sleep"):
            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        self.assertEqual(n_calls["count"], 3, "should retry until success")
        self.assertIn("prompts_path", out)

    def test_no_beats_in_script_returns_empty_dict(self):
        # Pre-2026-05-15: "no beats" was the failure mode that made the
        # P1b refiner inert in production (the cloud rewrite stage emits
        # plain narration, not pre-segmented beats). After the fix this
        # only fires when there's NEITHER beats NOR shots NOR narration.
        script = {"slug": "empty"}  # no beats, no shots, no narration
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Author should not have been called for an empty script.
        mock_author.assert_not_called()
        self.assertEqual(out, {})

    def test_narration_only_script_sentence_splits_into_beats(self):
        # The prorevenge-shape script: rewrite stage emits plain
        # narration with no pre-segmented beats. This is what the
        # 215e411b canary surfaced — old P1b skipped the LLM author
        # entirely on this shape. Fixed 2026-05-15.
        script = {
            "slug": "narration-only",
            "narration": (
                "I caught my roommate stealing my groceries. "
                "I labeled every item with a marker. "
                "Two days later, half of it disappeared again."
            ),
        }
        captured_beats = []

        def _stub_author(*, beats, out_path, **kwargs):
            captured_beats.append(list(beats))
            out_path.write_text("[]")
            return []

        with patch(
            "pipeline.llm.prompts.author_beat_prompts",
            side_effect=_stub_author,
        ):
            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Narration sentence-split yielded 3 beats (one per sentence).
        self.assertEqual(len(captured_beats[0]), 3)
        self.assertIn("groceries", captured_beats[0][0].text)
        self.assertIn("marker", captured_beats[0][1].text)
        self.assertIn("disappeared", captured_beats[0][2].text)
        # Synthetic 1.5s spacing applied (the real timeline comes from
        # asr_beats post-TTS — the prompt author only uses .duration
        # cosmetically).
        self.assertEqual(captured_beats[0][0].start, 0.0)
        self.assertEqual(captured_beats[0][0].end, 1.5)
        self.assertEqual(captured_beats[0][1].start, 1.5)
        # And the helper returned the prompts_path → engine path is now
        # exercised on prorevenge-shape scripts.
        self.assertIn("prompts_path", out)

    def test_shots_shape_script_extracts_narration_lines(self):
        # The /make-* skill output shape (sportsrecapped, history,
        # cosmos channels). shots[] + optional closer.
        script = {
            "slug": "shots-shape",
            "shots": [
                {"narration_line": "First shot text"},
                {"narration_line": "Second shot text"},
                {"text": "Third uses text not narration_line"},
            ],
            "closer": {"narration_line": "Closer text"},
        }
        captured = []

        def _stub_author(*, beats, out_path, **kwargs):
            captured.append(list(beats))
            out_path.write_text("[]")
            return []

        with patch(
            "pipeline.llm.prompts.author_beat_prompts",
            side_effect=_stub_author,
        ):
            _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # 3 shots + 1 closer = 4 beats.
        texts = [b.text for b in captured[0]]
        self.assertEqual(texts, [
            "First shot text",
            "Second shot text",
            "Third uses text not narration_line",
            "Closer text",
        ])

    def test_beats_shape_takes_priority_over_shots_or_narration(self):
        # When all three shapes exist, beats[] wins (highest fidelity —
        # already pre-segmented with explicit timing).
        script = {
            "slug": "all-three",
            "beats": [
                {"text": "explicit beat", "start": 1.0, "end": 3.5},
            ],
            "shots": [{"narration_line": "ignored shot"}],
            "narration": "this narration is also ignored.",
        }
        captured = []

        def _stub_author(*, beats, out_path, **kwargs):
            captured.append(list(beats))
            out_path.write_text("[]")
            return []

        with patch(
            "pipeline.llm.prompts.author_beat_prompts",
            side_effect=_stub_author,
        ):
            _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        self.assertEqual(len(captured[0]), 1)
        self.assertEqual(captured[0][0].text, "explicit beat")
        # Real start/end preserved from the beat dict (not synthetic).
        self.assertEqual(captured[0][0].start, 1.0)
        self.assertEqual(captured[0][0].end, 3.5)

    def test_empty_text_beats_filtered_out(self):
        script = {
            "slug": "partial-empty",
            "narration": "x",
            "beats": [
                {"text": "", "start": 0.0, "end": 1.0},
                {"text": "  ", "start": 1.0, "end": 2.0},
                {"text": "real beat", "start": 2.0, "end": 3.5},
            ],
        }
        captured_beats = []

        def _stub_author(*, beats, out_path, **kwargs):
            captured_beats.append(list(beats))
            out_path.write_text("[]")
            return []

        with patch(
            "pipeline.llm.prompts.author_beat_prompts",
            side_effect=_stub_author,
        ):
            _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Only the "real beat" survived filtering.
        self.assertEqual(len(captured_beats[0]), 1)
        self.assertEqual(captured_beats[0][0].text, "real beat")

    def test_unknown_era_anchor_drops_to_none_without_failing(self):
        script = {
            "slug": "unknown-era",
            "narration": "x",
            "metadata": {"era_anchor": "definitely-not-a-real-era-key-12345"},
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Unknown era → era_anchor_prefix omitted, no crash.
        self.assertNotIn("era_anchor_prefix", out)
        # But prompts.json was still authored.
        self.assertIn("prompts_path", out)

    def test_progress_cb_called_when_authoring(self):
        script = {
            "slug": "progress-test",
            "narration": "x",
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        events: list[tuple[str, str]] = []

        def _cb(stage, msg):
            events.append((stage, msg))

        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
                progress_cb=_cb,
            )
        # At least one progress event fired.
        self.assertTrue(events)
        # And it tagged the TTS pill (first substage — see comment in
        # entrypoint.py about cascade semantics).
        self.assertEqual(events[0][0], "tts")
        # Message must EXPLICITLY say [prompts] / LLM / per-beat so an
        # operator scanning the dashboard or logs doesn't mistake this
        # for actual TTS work — the tag is just where the pill lives.
        self.assertIn("[prompts]", events[0][1])
        self.assertIn("per-beat", events[0][1])

    def test_cast_load_raising_does_not_crash_helper(self):
        """If load_cast() raises (corrupted cast.json, permissions
        error), the helper must log a warning and continue with no
        character_description — never propagate the exception."""
        script = {
            "slug": "cast-raises",
            "narration": "x",
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.llm.cast.load_cast", side_effect=OSError("disk gone")), \
             patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # No character description came through (cast failed, no YAML fallback).
        self.assertNotIn("character_description", out)
        # But prompts.json STILL got authored.
        self.assertIn("prompts_path", out)

    def test_channel_yaml_unreadable_does_not_crash(self):
        """If channel YAML can't be read (permissions, corruption),
        cast fallback still fails-soft and the helper continues."""
        # Drop a corrupted YAML at the channel path.
        self.channel_yaml.write_text("this: is: not: valid: yaml: [oops")
        script = {
            "slug": "bad-yaml",
            "narration": "x",
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Authoring still happened.
        self.assertIn("prompts_path", out)

    def test_era_resolution_raising_does_not_crash(self):
        """If era_prefix_for raises (corrupted taxonomy YAML), the
        helper falls back to no era anchor and the render proceeds."""
        script = {
            "slug": "era-explodes",
            "narration": "x",
            "metadata": {"era_anchor": "any-key"},
            "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
        }
        with patch("pipeline.era_anchor.era_prefix_for",
                   side_effect=RuntimeError("taxonomy broken")), \
             patch("pipeline.llm.prompts.author_beat_prompts") as mock_author:
            def _stub_author(*, out_path, **kwargs):
                out_path.write_text("[]")
                return []
            mock_author.side_effect = _stub_author

            out = _ep._author_prompts_for_engine(
                script_dict=script,
                channel_yaml_path=self.channel_yaml,
                work_dir=self.work_dir,
                style_prefix="",
            )
        # Era_anchor_prefix omitted, but prompts.json still authored.
        self.assertNotIn("era_anchor_prefix", out)
        self.assertIn("prompts_path", out)

    def test_engine_wiring_handles_spec_extra_is_none(self):
        """``_run_renderer_via_engines`` must initialise ``spec.extra``
        if it's None before merging the helper's updates. Pin the
        defensive ``if spec.extra is None: spec.extra = {}`` branch."""
        import sys
        from unittest import mock

        # Synthesize a minimal job + spec where spec.extra starts None.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            script_path = tmp / "script.json"
            script_path.write_text(json.dumps({
                "slug": "extra-none",
                "narration": "x",
                "beats": [{"text": "first", "start": 0.0, "end": 1.5}],
            }))
            channel_root = tmp / "prorevenge"
            channel_root.mkdir()
            channel_yaml = _write_channel_yaml(channel_root)
            job = {
                "_script_path": str(script_path),
                "_channel_yaml": str(channel_yaml),
                "_slug": "extra-none",
                "proposal": {"topic": "test", "kind": "short"},
            }

            class _FakeSpec:
                class _Kind:
                    value = "short"
                kind = _Kind()
                channel = "prorevenge"
                extra = None  # ← the critical setup

            seen_extra_after_call = {}

            def _stub_render(*, spec, **_):
                # By the time render_via_engines is called, spec.extra
                # must be a dict (the wiring must have initialised it).
                seen_extra_after_call["extra"] = spec.extra
                return tmp / "out.mp4"

            def _stub_author(*, out_path, **_):
                out_path.write_text("[]")
                return []

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
                 }), \
                 mock.patch(
                     "pipeline.llm.prompts.author_beat_prompts",
                     side_effect=_stub_author,
                 ):
                _ep._run_renderer_via_engines(job, tmp, progress_cb=None)

        # spec.extra was None → wiring must have promoted it to a dict
        # with at least 'prompts_path' (the always-emitted key when
        # authoring succeeds).
        self.assertIsInstance(seen_extra_after_call["extra"], dict)
        self.assertIn("prompts_path", seen_extra_after_call["extra"])


class BackfillYamlImageKeysTest(unittest.TestCase):
    """Pin the YAML → spec.extra hop that copies image_* config keys
    into spec.extra when they're not already there. Without this, the
    engine path runs with empty style_prefix and falls back to the
    default image provider regardless of channel YAML — silent quality
    loss vs the legacy renderer (rubber-duck #1, 2026-05-14)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def _write_yaml(self, **fields) -> Path:
        import yaml
        p = self.tmp_path / "ch.yaml"
        p.write_text(yaml.safe_dump(fields))
        return p

    def test_fills_in_image_keys_from_yaml(self):
        yp = self._write_yaml(
            image_provider="cloudrun_flux2_klein",
            image_style_prefix="comic illustration, soft palette",
            image_seed=42,
            image_steps=4,
        )
        out = _ep._backfill_yaml_image_keys(None, yp)
        self.assertEqual(out["image_provider"], "cloudrun_flux2_klein")
        self.assertEqual(out["image_style_prefix"], "comic illustration, soft palette")
        self.assertEqual(out["image_seed"], 42)
        self.assertEqual(out["image_steps"], 4)

    def test_preserves_existing_overrides(self):
        # If a per-render proposal already set image_provider, the
        # YAML default must NOT clobber it.
        yp = self._write_yaml(
            image_provider="cloudrun_flux2_klein",
            image_style_prefix="default style",
        )
        existing = {"image_provider": "cloudrun_z_image_turbo"}
        out = _ep._backfill_yaml_image_keys(existing, yp)
        self.assertEqual(out["image_provider"], "cloudrun_z_image_turbo")
        # But missing keys still backfilled.
        self.assertEqual(out["image_style_prefix"], "default style")

    def test_treats_empty_string_overrides_as_missing(self):
        # An empty string is functionally identical to missing — fall
        # back to YAML default.
        yp = self._write_yaml(image_style_prefix="yaml style")
        existing = {"image_style_prefix": ""}
        out = _ep._backfill_yaml_image_keys(existing, yp)
        self.assertEqual(out["image_style_prefix"], "yaml style")

    def test_yaml_unreadable_returns_existing_extra(self):
        # Corrupt YAML → log warning, return existing extra unchanged.
        bad = self.tmp_path / "broken.yaml"
        bad.write_text("this: is: not: yaml: [")
        existing = {"image_provider": "x"}
        out = _ep._backfill_yaml_image_keys(existing, bad)
        self.assertEqual(out, {"image_provider": "x"})

    def test_yaml_with_no_image_keys_returns_extra_unchanged(self):
        yp = self._write_yaml(channel="prorevenge", kind="short")
        out = _ep._backfill_yaml_image_keys(None, yp)
        # Nothing image_* in the YAML → empty dict result.
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
