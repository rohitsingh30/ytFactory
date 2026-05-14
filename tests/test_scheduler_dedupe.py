"""Tests for the scheduler dedupe + test-fixture detector added
2026-05-14 to fix the duplicate-render bug surfaced by the
2026-05-13 audit.

Two failure modes the tests pin:

1. **Topic-uniqueness window**: the scheduler kept re-rendering the
   same cake-AITA / baghdad-mongols / ronaldinho topics (9, 6, 5
   times respectively in 6 days) because its only dedupe predicate
   was "is upload missing?" — never satisfied while uploads were
   paused. Fix adds a second predicate: "is there a recent (<30d)
   successful render for this slug?".

2. **Test-fixture leak**: 2 dev smoke-test topics ('AITA descriptor-
   registry smoke test all knobs', 'AITA slice 4 + 5 verify') reached
   the production renders bucket because no gate distinguished them
   from real proposals. Fix: regex-based test-fixture detector
   auto-flags such proposals as ``internal_only=True``.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from control.core import scheduler
from control.core.schema import ShortProposal


class TestFixtureDetectorTest(unittest.TestCase):

    def test_smoke_test_in_topic_is_flagged(self):
        # Smoke-tested 2026-05-14 audit examples.
        self.assertTrue(scheduler.is_test_fixture_topic(
            "AITA descriptor-registry smoke test all knobs"))
        self.assertTrue(scheduler.is_test_fixture_topic(
            "AITA slice 4 + 5 verify"))

    def test_real_topics_not_flagged(self):
        # Real production topics from the audit must not be flagged.
        for real in [
            "aita-for-ruining-the-cake-j-abb411",
            "ronaldinho-trophies-after-you-stopped-watching",
            "baghdad-mongols-1258",
            "If you can see this, it is very important that you keep reading",
            "TIFU by tracing the strange radio signal in my attic",
        ]:
            self.assertFalse(scheduler.is_test_fixture_topic(real),
                             f"real topic flagged as fixture: {real!r}")

    def test_kebab_slug_forms_flagged(self):
        for slug in [
            "smoke-test-render",
            "debug-render-v2",
            "dev-fixture-knobs",
            "qa-test-images",
        ]:
            self.assertTrue(scheduler.is_test_fixture_topic(slug),
                            f"slug should be flagged: {slug!r}")

    def test_descriptor_registry_pattern(self):
        self.assertTrue(scheduler.is_test_fixture_topic(
            "Descriptor Registry test"))
        self.assertTrue(scheduler.is_test_fixture_topic(
            "descriptor-registry-knobs"))

    def test_slice_verify_pattern(self):
        # "AITA slice 4 + 5 verify" — pattern: \b(slice|verify)\s+\d
        self.assertTrue(scheduler.is_test_fixture_topic("slice 7 verification"))
        self.assertTrue(scheduler.is_test_fixture_topic("verify 3 knobs"))

    def test_internal_dev_qa_pattern(self):
        for s in [
            "internal-only test render",
            "dev fixture for cast",
            "qa test for narrations",
            "debug-only build",
        ]:
            self.assertTrue(scheduler.is_test_fixture_topic(s),
                            f"should flag {s!r}")

    def test_falsy_inputs_return_false(self):
        # None / "" → not a fixture (different bug class).
        self.assertFalse(scheduler.is_test_fixture_topic(None))
        self.assertFalse(scheduler.is_test_fixture_topic(""))

    def test_case_insensitive(self):
        self.assertTrue(scheduler.is_test_fixture_topic("SMOKE TEST"))
        self.assertTrue(scheduler.is_test_fixture_topic("Smoke Test"))


class RecentlyRenderedSlugsTest(unittest.TestCase):

    def test_returns_empty_when_window_is_zero(self):
        # Dedupe disabled via env.
        result = scheduler._recently_rendered_slugs("foo", since_days=0)
        self.assertEqual(result, set())

    def test_returns_empty_when_firestore_unavailable(self):
        with patch.object(scheduler, "_firestore_client_safe", return_value=None):
            result = scheduler._recently_rendered_slugs("foo", since_days=30)
            self.assertEqual(result, set())

    def test_collects_topic_from_firestore_jobs(self):
        # Mock firestore client returning 3 done jobs with topics.
        cli = MagicMock()
        docs = [
            MagicMock(to_dict=lambda: {"topic": "topic-a"}),
            MagicMock(to_dict=lambda: {"topic": "topic-b"}),
            MagicMock(to_dict=lambda: {"topic": "topic-a"}),  # dup, set dedupes
        ]
        q = MagicMock()
        q.where.return_value = q
        q.stream.return_value = docs
        cli.collection.return_value.where.return_value = q
        with patch.object(scheduler, "_firestore_client_safe", return_value=cli):
            result = scheduler._recently_rendered_slugs("ch", since_days=30)
        self.assertEqual(result, {"topic-a", "topic-b"})

    def test_skips_jobs_without_topic_field(self):
        cli = MagicMock()
        docs = [
            MagicMock(to_dict=lambda: {"topic": "topic-a"}),
            MagicMock(to_dict=lambda: {}),  # no topic
            MagicMock(to_dict=lambda: {"topic": None}),  # null topic
        ]
        q = MagicMock()
        q.where.return_value = q
        q.stream.return_value = docs
        cli.collection.return_value.where.return_value = q
        with patch.object(scheduler, "_firestore_client_safe", return_value=cli):
            result = scheduler._recently_rendered_slugs("ch", since_days=30)
        self.assertEqual(result, {"topic-a"})

    def test_swallows_firestore_errors(self):
        cli = MagicMock()
        cli.collection.side_effect = RuntimeError("network down")
        with patch.object(scheduler, "_firestore_client_safe", return_value=cli):
            # Should not raise; returns empty set.
            result = scheduler._recently_rendered_slugs("ch", since_days=30)
            self.assertEqual(result, set())


class FirestoreClientSafeTest(unittest.TestCase):

    def test_returns_none_without_project_env(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch("os.environ.get", return_value=None):
            result = scheduler._firestore_client_safe()
            self.assertIsNone(result)

    def test_returns_none_when_firestore_import_fails(self):
        # Simulate google.cloud.firestore not importable. ImportError
        # is a subclass of Exception so the broad except catches it.
        with patch("os.environ.get", return_value="some-project"), \
             patch("builtins.__import__", side_effect=ImportError("no firestore")):
            result = scheduler._firestore_client_safe()
            self.assertIsNone(result)

    def test_constructs_firestore_client_when_env_set(self):
        # Project env IS set — should hit lines 132-135 (project lookup
        # + Client construction). Mock the firestore import target.
        fake_client_class = MagicMock(return_value=MagicMock(name="fs_client"))
        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "test-proj"}):
            with patch("google.cloud.firestore.Client", fake_client_class):
                result = scheduler._firestore_client_safe()
        self.assertIsNotNone(result)
        fake_client_class.assert_called_once_with(project="test-proj")

    def test_returns_none_when_env_var_is_empty_string(self):
        # Empty string → not "set" for our purposes; line 133 ``if not project``
        # branch.
        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": ""}):
            result = scheduler._firestore_client_safe()
            self.assertIsNone(result)


class NextUnrenderedNicheNarrationTest(unittest.TestCase):
    """Pin lines 310-312 (the niche-narration filtering path in
    _next_unrendered)."""

    def test_niche_narration_filtered_by_recent_render(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            project_root = Path(tmp)
            chan = "ch"
            chan_dir = project_root / chan
            # Niche-nested layout (e.g. mystoriesanimated/aita/narrations/x.json)
            (chan_dir / "myaita" / "narrations").mkdir(parents=True)
            (chan_dir / "myaita" / "narrations" / "fresh.json").write_text("{}")
            (chan_dir / "myaita" / "narrations" / "stale.json").write_text("{}")
            with patch.object(scheduler, "PROJECT_ROOT", project_root), \
                 patch.object(scheduler, "_uploaded_slugs", return_value=set()), \
                 patch.object(scheduler, "_recently_rendered_slugs",
                              return_value={"stale"}), \
                 patch.object(scheduler, "_state_bucket", return_value=None):
                result = scheduler._next_unrendered(chan)
            self.assertIsNotNone(result)
            slug, _ = result
            # "stale" filtered out → only "fresh" survives.
            self.assertEqual(slug, "fresh")

    def test_niche_narration_filtered_by_uploaded_slug(self):
        # Coverage twin of the previous test for the `slug in uploaded`
        # branch under the niche path.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            project_root = Path(tmp)
            chan = "ch"
            chan_dir = project_root / chan
            (chan_dir / "myaita" / "narrations").mkdir(parents=True)
            (chan_dir / "myaita" / "narrations" / "shipped.json").write_text("{}")
            (chan_dir / "myaita" / "narrations" / "queued.json").write_text("{}")
            with patch.object(scheduler, "PROJECT_ROOT", project_root), \
                 patch.object(scheduler, "_uploaded_slugs", return_value={"shipped"}), \
                 patch.object(scheduler, "_recently_rendered_slugs",
                              return_value=set()), \
                 patch.object(scheduler, "_state_bucket", return_value=None):
                result = scheduler._next_unrendered(chan)
            self.assertIsNotNone(result)
            slug, _ = result
            self.assertEqual(slug, "queued")


class NextUnrenderedDedupeIntegrationTest(unittest.TestCase):
    """Verify _next_unrendered (laptop path) and _next_unrendered_gcs
    both filter out recently-rendered slugs."""

    def test_laptop_path_excludes_recently_rendered_slug(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            project_root = Path(tmp)
            chan = "fakechannel"
            chan_dir = project_root / chan
            (chan_dir / "narrations").mkdir(parents=True)
            (chan_dir / "narrations" / "fresh.json").write_text("{}")
            (chan_dir / "narrations" / "recent.json").write_text("{}")
            with patch.object(scheduler, "PROJECT_ROOT", project_root), \
                 patch.object(scheduler, "_uploaded_slugs", return_value=set()), \
                 patch.object(scheduler, "_recently_rendered_slugs",
                              return_value={"recent"}), \
                 patch.object(scheduler, "_state_bucket", return_value=None):
                result = scheduler._next_unrendered(chan)
            # Only "fresh" survives the dedupe.
            self.assertIsNotNone(result)
            slug, _ = result
            self.assertEqual(slug, "fresh")

    def test_laptop_path_returns_none_when_all_dedupe_filtered(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            project_root = Path(tmp)
            chan = "fakechannel"
            chan_dir = project_root / chan
            (chan_dir / "narrations").mkdir(parents=True)
            (chan_dir / "narrations" / "a.json").write_text("{}")
            (chan_dir / "narrations" / "b.json").write_text("{}")
            with patch.object(scheduler, "PROJECT_ROOT", project_root), \
                 patch.object(scheduler, "_uploaded_slugs", return_value=set()), \
                 patch.object(scheduler, "_recently_rendered_slugs",
                              return_value={"a", "b"}), \
                 patch.object(scheduler, "_state_bucket", return_value=None):
                result = scheduler._next_unrendered(chan)
            self.assertIsNone(result)

    def test_gcs_path_excludes_recently_rendered_slug(self):
        # Mock GCS list_blobs returning 2 narration blobs; one is in
        # recently_rendered and should be filtered out.
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path

        cli = MagicMock()
        b1 = MagicMock()
        b1.name = "ch/narrations/keep.json"
        b1.updated = _dt(2026, 5, 1, tzinfo=_tz.utc)
        b2 = MagicMock()
        b2.name = "ch/narrations/skip.json"
        b2.updated = _dt(2026, 5, 2, tzinfo=_tz.utc)
        cli.list_blobs.return_value = [b1, b2]
        with patch.object(scheduler, "_gcs_client", return_value=cli), \
             patch.object(scheduler, "_uploaded_slugs_gcs", return_value=set()), \
             patch.object(scheduler, "_recently_rendered_slugs",
                          return_value={"skip"}):
            result = scheduler._next_unrendered_gcs("bucket-x", "ch")
        self.assertIsNotNone(result)
        slug, path = result
        self.assertEqual(slug, "keep")
        self.assertIn("keep.json", str(path))


class EnqueueRenderJobAutoFlagTest(unittest.TestCase):
    """Verify the test-fixture auto-flag helper behaviour (the 2026-05-14
    leak fix). The integration with _enqueue_render_job is verified
    indirectly via the helper's behaviour, since the queue + cloud_run
    side effects of _enqueue_render_job are out of scope for unit tests."""

    def test_smoke_test_topic_auto_flagged_internal_only(self):
        from control.core.jobs import _apply_test_fixture_autoflag
        proposal = ShortProposal(
            channel="mystoriesanimated",
            topic="AITA descriptor-registry smoke test all knobs",
            source_kind="user_text",
        )
        self.assertFalse(proposal.internal_only)
        result = _apply_test_fixture_autoflag(proposal)
        self.assertTrue(result, "helper should report it set the flag")
        self.assertTrue(
            proposal.internal_only,
            "smoke-test topic should be auto-flagged internal_only=True",
        )

    def test_real_topic_not_auto_flagged(self):
        from control.core.jobs import _apply_test_fixture_autoflag
        proposal = ShortProposal(
            channel="sportsrecapped",
            topic="ronaldinho-trophies-after-you-stopped-watching",
            source_kind="user_text",
        )
        result = _apply_test_fixture_autoflag(proposal)
        self.assertFalse(result, "helper should NOT have set the flag")
        self.assertFalse(proposal.internal_only,
                         "real topic must not be auto-flagged")

    def test_already_flagged_proposal_not_touched(self):
        # Caller (admin tooling) explicitly set internal_only=True.
        # Helper must NOT re-flag (would cause duplicate log lines).
        from control.core.jobs import _apply_test_fixture_autoflag
        proposal = ShortProposal(
            channel="mystoriesanimated",
            topic="real production topic",
            internal_only=True,
        )
        result = _apply_test_fixture_autoflag(proposal)
        self.assertFalse(
            result,
            "helper should report no-op when already flagged",
        )
        self.assertTrue(proposal.internal_only,
                        "flag must remain True (no-op preserves)")

    def test_already_flagged_smoke_test_no_double_flag(self):
        # Smoke-test topic AND already-flagged → still no double-flag.
        from control.core.jobs import _apply_test_fixture_autoflag
        proposal = ShortProposal(
            channel="mystoriesanimated",
            topic="smoke test all knobs",
            internal_only=True,
        )
        result = _apply_test_fixture_autoflag(proposal)
        self.assertFalse(result)
        self.assertTrue(proposal.internal_only)


class ProposalInternalOnlyFieldTest(unittest.TestCase):
    """The ShortProposal schema must carry the internal_only flag so
    enqueue gates can read it."""

    def test_default_is_false(self):
        p = ShortProposal(channel="x", topic="y")
        self.assertFalse(p.internal_only)

    def test_can_be_set_explicitly(self):
        p = ShortProposal(channel="x", topic="y", internal_only=True)
        self.assertTrue(p.internal_only)

    def test_round_trips_through_model_dump(self):
        p = ShortProposal(channel="x", topic="y", internal_only=True)
        dumped = p.model_dump()
        self.assertIn("internal_only", dumped)
        self.assertTrue(dumped["internal_only"])
        # And re-instantiating preserves the flag.
        p2 = ShortProposal(**dumped)
        self.assertTrue(p2.internal_only)


if __name__ == "__main__":
    unittest.main()
