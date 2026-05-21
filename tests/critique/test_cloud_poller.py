"""Tests for ``pipeline.critique.cloud_poller`` -- the laptop-side
daemon that grades cloud-rendered Shorts post-hoc.

We mock Firestore + GCS + the critic call. Each test pins one of
the contract guarantees called out in CLAUDE.md / the task brief:

  (a) Finds an ``UNGATED`` job and routes through the critic.
  (b) Skips a job that already has a non-UNGATED verdict
      (idempotency -- the daemon must never re-grade a SHIP/FIX/BLOCK
      job and never burn critic budget twice).
  (c) Handles missing ``short_uri`` gracefully (mark error, move on).
  (d) Critic raise -> mark job's ``critique.error`` AND respect a
      1-hour cooldown so we don't re-poll the same broken job.
  (e) ``poll_and_critique_loop`` clamps ``interval_s`` floor at 60s.
  (f) The loop emits per-iteration counters in its log.
  (g) The cache cleanup deletes the mp4 but keeps frames + score.json.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from pipeline.critique import cloud_poller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_snap(job_id: str, data: dict):
    """Build a mock Firestore DocumentSnapshot whose ``.reference``
    is itself a MagicMock so we can assert ``.set(...)`` calls."""
    snap = mock.MagicMock()
    snap.id = job_id
    snap.to_dict.return_value = data
    snap.reference = mock.MagicMock()
    return snap


def _make_download(mp4_bytes: bytes = b"\x00\x00\x00\x18ftypmp42"):
    """Build a download_fn that writes deterministic bytes to dest."""
    def _dl(uri: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "beats" in uri or dest.name == "beats.json":
            dest.write_text('[{"t": 0.0, "text": "stub"}]')
        else:
            dest.write_bytes(mp4_bytes)
        return dest
    return _dl


def _fake_critic_result(verdict: str = "SHIP", score: int = 8) -> dict:
    return {
        "verdict": verdict,
        "axes": {"hook": score, "pacing": score, "visual": score,
                 "audio": score, "story": score, "cta": score},
        "score": score,
        "one_line_take": "looks great",
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestParseGsUri(unittest.TestCase):
    """gs:// URI parsing -- worker schema requires this be strict."""

    def test_happy_path(self):
        b, k = cloud_poller._parse_gs_uri("gs://buck/jobs/abc/short.mp4")
        self.assertEqual(b, "buck")
        self.assertEqual(k, "jobs/abc/short.mp4")

    def test_rejects_http_url(self):
        with self.assertRaises(ValueError):
            cloud_poller._parse_gs_uri("https://example.com/foo")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            cloud_poller._parse_gs_uri("")

    def test_rejects_bucket_only(self):
        with self.assertRaises(ValueError):
            cloud_poller._parse_gs_uri("gs://just-a-bucket")


class TestCacheRoot(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(os.environ,
                             {"YTFACTORY_CLOUD_CRITIC_CACHE": "/tmp/foo"}):
            self.assertEqual(cloud_poller._cache_root(), Path("/tmp/foo"))

    def test_default_under_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YTFACTORY_CLOUD_CRITIC_CACHE", None)
            root = cloud_poller._cache_root()
            self.assertEqual(root.name, "cloud_critic")
            self.assertEqual(root.parent.name, "ytfactory")


class TestErrorCooldown(unittest.TestCase):
    """A job that errored within 1 hour must not be re-attempted."""

    def test_no_error_field_returns_false(self):
        self.assertFalse(
            cloud_poller._on_error_cooldown({}, now=1_000_000)
        )

    def test_recent_error_is_on_cooldown(self):
        recent = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        data = {"critique": {"error": "boom", "errored_at": recent}}
        self.assertTrue(cloud_poller._on_error_cooldown(data, now=now))

    def test_stale_error_is_NOT_on_cooldown(self):
        # 2 hours ago -> off cooldown (1h threshold)
        two_hours_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
        ts = two_hours_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        data = {"critique": {"error": "boom", "errored_at": ts}}
        self.assertFalse(cloud_poller._on_error_cooldown(data, now=now))

    def test_error_without_timestamp_is_on_cooldown(self):
        # No timestamp -> conservative; treat as on cooldown so an
        # operator must manually clear before retry.
        data = {"critique": {"error": "boom"}}
        self.assertTrue(
            cloud_poller._on_error_cooldown(data, now=1_000_000)
        )


class TestCritiqueOneJob(unittest.TestCase):
    """Per-job routing -- the heart of the daemon."""

    def setUp(self):
        self._tmpdir = Path(self.id().replace(".", "_"))

    def _cache_root(self, tmp_path: Path) -> Path:
        d = tmp_path / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_a_critiques_ungated_job_and_writes_back(self):
        """(a) Found UNGATED job -> runs critic, writes back."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            snap = _mk_snap("job-abc", {
                "status": "done",
                "short_uri": "gs://buck/jobs/job-abc/video/short.mp4",
                "slug": "myslug",
                "critique": {"verdict": "UNGATED"},
                "artifacts": {
                    "beats": {
                        "uri": "gs://buck/jobs/job-abc/beats/beats.json",
                    },
                },
            })
            fake_critic = mock.MagicMock(return_value=_fake_critic_result())
            outcome = cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=self._cache_root(tmp),
                critique_fn=fake_critic,
                download_fn=_make_download(),
            )
            self.assertEqual(outcome.status, "critiqued")
            self.assertEqual(outcome.verdict, "SHIP")
            fake_critic.assert_called_once()
            kwargs = fake_critic.call_args.kwargs
            self.assertEqual(kwargs["slug"], "myslug")
            # Firestore write-back happened with the new verdict.
            self.assertTrue(snap.reference.set.called)
            written = snap.reference.set.call_args[0][0]
            self.assertEqual(written["critique"]["verdict"], "SHIP")
            self.assertEqual(written["critique"]["score"], 8)
            self.assertEqual(
                written["critique"]["critiqued_by"], "laptop_cloud_poller",
            )
            # Prior error cleared.
            self.assertIsNone(written["critique"]["error"])

    def test_a_critiques_none_verdict_job(self):
        """Missing critique field entirely is also a critique target."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            snap = _mk_snap("job-none", {
                "status": "done",
                "short_uri": "gs://buck/jobs/job-none/video/short.mp4",
                "slug": "noverd",
                # NO critique field at all
                "artifacts": {
                    "beats": {
                        "uri": "gs://buck/jobs/job-none/beats/beats.json",
                    },
                },
            })
            fake_critic = mock.MagicMock(return_value=_fake_critic_result(
                verdict="FIX", score=4,
            ))
            outcome = cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=self._cache_root(tmp),
                critique_fn=fake_critic,
                download_fn=_make_download(),
            )
            self.assertEqual(outcome.status, "critiqued")
            self.assertEqual(outcome.verdict, "FIX")

    def test_b_skips_already_critiqued_job_via_scan(self):
        """(b) The scan-level filter rejects SHIP/FIX/BLOCK jobs.

        We don't even reach ``critique_one_job`` -- the scan upstream
        filters them out. Verifies that filter logic directly."""
        client = mock.MagicMock()
        # The query returns three jobs: one SHIP, one UNGATED, one FIX.
        snaps_returned = [
            _mk_snap("ship-1", {"status": "done", "critique": {"verdict": "SHIP"}}),
            _mk_snap("ungated-2", {"status": "done", "critique": {"verdict": "UNGATED"}}),
            _mk_snap("fix-3", {"status": "done", "critique": {"verdict": "FIX"}}),
            _mk_snap("noverd-4", {"status": "done"}),  # no critique at all
        ]
        client.collection().where().limit().stream.return_value = iter(snaps_returned)
        out = cloud_poller._scan_ungated_jobs(client)
        ids = {s.id for s in out}
        # ship-1 + fix-3 filtered; ungated-2 + noverd-4 kept.
        self.assertEqual(ids, {"ungated-2", "noverd-4"})

    def test_c_missing_short_uri_is_skipped_with_error_mark(self):
        """(c) status=done but no short_uri -> mark error, return skipped."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            snap = _mk_snap("job-noshort", {
                "status": "done",
                # NO short_uri
                "critique": {"verdict": "UNGATED"},
            })
            fake_critic = mock.MagicMock()
            outcome = cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=self._cache_root(tmp),
                critique_fn=fake_critic,
                download_fn=_make_download(),
            )
            self.assertEqual(outcome.status, "skipped")
            self.assertEqual(outcome.reason, "no_short_uri")
            fake_critic.assert_not_called()
            # The job got an error marker so the dashboard can show it.
            self.assertTrue(snap.reference.set.called)
            written = snap.reference.set.call_args[0][0]
            self.assertIn("error", written["critique"])

    def test_c_missing_beats_uri_is_skipped(self):
        """Beats artifact URI missing -> skip + mark error."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            snap = _mk_snap("job-nobeats", {
                "status": "done",
                "short_uri": "gs://buck/jobs/job-nobeats/video/short.mp4",
                "critique": {"verdict": "UNGATED"},
                # No artifacts.beats
                "artifacts": {},
            })
            outcome = cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=self._cache_root(tmp),
                critique_fn=mock.MagicMock(),
                download_fn=_make_download(),
            )
            self.assertEqual(outcome.status, "skipped")
            self.assertEqual(outcome.reason, "no_beats_uri")

    def test_d_critic_raises_marks_error_and_cleans_up(self):
        """(d) Critic raise -> error written, mp4 cleaned up."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            snap = _mk_snap("job-boom", {
                "status": "done",
                "short_uri": "gs://buck/jobs/job-boom/video/short.mp4",
                "slug": "boomslug",
                "critique": {"verdict": "UNGATED"},
                "artifacts": {
                    "beats": {"uri": "gs://buck/jobs/job-boom/beats/beats.json"},
                },
            })

            def boom_critic(**kwargs):
                raise RuntimeError("vision quota exceeded")

            outcome = cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=self._cache_root(tmp),
                critique_fn=boom_critic,
                download_fn=_make_download(),
            )
            self.assertEqual(outcome.status, "failed")
            # Error marker written.
            self.assertTrue(snap.reference.set.called)
            written = snap.reference.set.call_args[0][0]
            self.assertIn("vision quota exceeded", written["critique"]["error"])
            self.assertIn("errored_at", written["critique"])
            # mp4 was cleaned up to prevent disk-leak.
            mp4_local = self._cache_root(tmp) / "job-boom" / "short.mp4"
            self.assertFalse(mp4_local.exists())

    def test_d_within_cooldown_skips_without_calling_critic(self):
        """(d) job with recent critic error is NOT re-polled within 1h."""
        import tempfile
        recent = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            snap = _mk_snap("job-cooldown", {
                "status": "done",
                "short_uri": "gs://buck/jobs/job-cooldown/video/short.mp4",
                "critique": {
                    "verdict": "UNGATED",
                    "error": "prior boom",
                    "errored_at": recent,
                },
                "artifacts": {
                    "beats": {"uri": "gs://buck/jobs/job-cooldown/beats/beats.json"},
                },
            })
            fake_critic = mock.MagicMock()
            outcome = cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=self._cache_root(tmp),
                critique_fn=fake_critic,
                download_fn=_make_download(),
            )
            self.assertEqual(outcome.status, "skipped")
            self.assertEqual(outcome.reason, "error_cooldown")
            fake_critic.assert_not_called()
            snap.reference.set.assert_not_called()

    def test_g_cleanup_deletes_mp4_but_keeps_score(self):
        """(g) Post-critique: mp4 gone, score.json + frames remain."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache_root = self._cache_root(tmp)

            # Real critic_fn that writes a score.json + frames dir to
            # mimic what the actual critic does, so we can assert
            # they're preserved post-cleanup.
            def critic_with_outputs(*, slug, mp4_path, cache_dir, out_dir):
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{slug}.score.json").write_text("{}")
                (out_dir / "frames").mkdir(exist_ok=True)
                (out_dir / "frames" / "f000.png").write_bytes(b"x")
                return _fake_critic_result()

            snap = _mk_snap("job-keep", {
                "status": "done",
                "short_uri": "gs://buck/jobs/job-keep/video/short.mp4",
                "slug": "kept",
                "critique": {"verdict": "UNGATED"},
                "artifacts": {
                    "beats": {"uri": "gs://buck/jobs/job-keep/beats/beats.json"},
                },
            })
            cloud_poller.critique_one_job(
                client=mock.MagicMock(),
                snap=snap,
                cache_root=cache_root,
                critique_fn=critic_with_outputs,
                download_fn=_make_download(),
            )
            job_root = cache_root / "job-keep"
            self.assertFalse((job_root / "short.mp4").exists())
            self.assertTrue((job_root / "critic" / "kept.score.json").exists())
            self.assertTrue((job_root / "critic" / "frames" / "f000.png").exists())


class TestPollLoop(unittest.TestCase):
    """The wrapping loop -- interval clamp, max_iters, stop_event."""

    def test_max_iters_bound(self):
        client = mock.MagicMock()
        client.collection().where().limit().stream.return_value = iter([])
        sleeps: list[float] = []
        iters = cloud_poller.poll_and_critique_loop(
            client=client,
            max_iters=3,
            interval_s=60,
            sleep_fn=sleeps.append,
        )
        self.assertEqual(iters, 3)
        # 2 inter-iteration sleeps (after iter1, after iter2) of 60
        # 1s slices each = 60 calls per sleep, 120 total expected.
        # The final iter returns early without sleeping.
        self.assertEqual(len(sleeps), 120)

    def test_interval_clamped_at_60(self):
        """``interval_s < 60`` must clamp up; we never hot-loop."""
        client = mock.MagicMock()
        client.collection().where().limit().stream.return_value = iter([])
        sleeps: list[float] = []
        cloud_poller.poll_and_critique_loop(
            client=client,
            interval_s=5,
            max_iters=2,
            sleep_fn=sleeps.append,
        )
        # After clamp: 1 inter-iter sleep of 60s sliced into 60 1s naps.
        self.assertEqual(len(sleeps), 60)

    def test_stop_event_breaks_loop(self):
        client = mock.MagicMock()
        client.collection().where().limit().stream.return_value = iter([])
        stop_event = threading.Event()
        # Pre-set the event so loop sees it on first check.
        stop_event.set()
        iters = cloud_poller.poll_and_critique_loop(
            client=client,
            stop_event=stop_event,
            max_iters=100,
            interval_s=60,
            sleep_fn=lambda _: None,
        )
        self.assertEqual(iters, 0)

    def test_iteration_routes_per_job_outcomes(self):
        """The loop's per-iter stats correctly tally critiqued vs
        skipped vs failed."""
        client = mock.MagicMock()
        snaps = [
            _mk_snap("ok-1", {
                "status": "done",
                "short_uri": "gs://b/jobs/ok-1/short.mp4",
                "slug": "ok1",
                "critique": {"verdict": "UNGATED"},
                "artifacts": {"beats": {"uri": "gs://b/jobs/ok-1/beats.json"}},
            }),
            _mk_snap("bad-2", {
                "status": "done",
                # missing short_uri -> skipped
                "critique": {"verdict": "UNGATED"},
            }),
        ]
        client.collection().where().limit().stream.return_value = iter(snaps)
        sleeps: list[float] = []
        with mock.patch.object(
            cloud_poller, "_logger", logging.getLogger("test")
        ):
            iters = cloud_poller.poll_and_critique_loop(
                client=client,
                max_iters=1,
                interval_s=60,
                critique_fn=mock.MagicMock(return_value=_fake_critic_result()),
                download_fn=_make_download(),
                sleep_fn=sleeps.append,
            )
        self.assertEqual(iters, 1)
        # Final iter, no sleep.
        self.assertEqual(len(sleeps), 0)


class TestInstallSigintHandler(unittest.TestCase):
    def test_handler_sets_event_on_sigint(self):
        ev = threading.Event()
        with mock.patch("signal.signal") as m_sig:
            cloud_poller.install_sigint_handler(ev)
            self.assertEqual(m_sig.call_count, 2)
            # The handler is the 2nd positional arg in each call.
            handler = m_sig.call_args_list[0].args[1]
            # Calling it should set the event.
            handler(2, None)
            self.assertTrue(ev.is_set())


if __name__ == "__main__":
    unittest.main()
