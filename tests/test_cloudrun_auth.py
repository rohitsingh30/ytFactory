"""Tests for `pipeline.cloud.cloudrun_auth.get_id_token`.

Focus: prove the audience-keyed cache fixes the multi-service-URL
regression. The original `pipeline.tts.cloudrun._get_id_token` had
a single global cache, which meant a token issued for service A
got reused against service B → Cloud Run rejected with a
audience-claim mismatch. With multiple Cloud Run services in flight
(2 TTS + 1 image) this is no longer hypothetical.
"""
from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import patch, MagicMock

from pipeline import cloudrun_auth


class TestPerAudienceCache(unittest.TestCase):
    def setUp(self) -> None:
        cloudrun_auth._reset_cache_for_tests()

    def tearDown(self) -> None:
        cloudrun_auth._reset_cache_for_tests()

    def _stub_run(self, audience_to_token: dict[str, str]):
        """Build a `subprocess.run` replacement that returns the
        right token for whatever audience the caller asks for."""
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            for arg in cmd:
                if arg.startswith("--audiences="):
                    aud = arg.split("=", 1)[1]
                    token = audience_to_token.get(aud)
                    if token is None:
                        raise subprocess.CalledProcessError(
                            1, cmd, output="",
                            stderr=f"unknown audience {aud}",
                        )
                    res = MagicMock()
                    res.stdout = token + "\n"
                    return res
            # No --audiences= → user-account fallback. Return a marker
            # token; tests assert this never gets hit when audience
            # path succeeds.
            res = MagicMock()
            res.stdout = "USER-ACCOUNT-FALLBACK\n"
            return res

        return fake_run

    def test_cache_keyed_by_audience(self) -> None:
        """Different audiences → different cached tokens, no leak."""
        url_a = "https://ytfactory-tts-chatterbox-foo.run.app"
        url_b = "https://ytfactory-image-z-image-turbo-foo.run.app"
        with patch.object(
            subprocess, "run",
            side_effect=self._stub_run({url_a: "token-A", url_b: "token-B"}),
        ):
            tok_a = cloudrun_auth.get_id_token(url_a)
            tok_b = cloudrun_auth.get_id_token(url_b)
        self.assertEqual(tok_a, "token-A")
        self.assertEqual(tok_b, "token-B")
        # Repeated call returns the same cached value WITHOUT
        # re-invoking subprocess.run for that audience.
        with patch.object(subprocess, "run") as boom:
            tok_a2 = cloudrun_auth.get_id_token(url_a)
        self.assertEqual(tok_a2, "token-A")
        boom.assert_not_called()

    def test_no_global_token_leak(self) -> None:
        """Regression: the old global cache would have returned token-A
        for url_b on the first call. Verify the per-audience cache
        does NOT have that property."""
        url_a = "https://ytfactory-tts-chatterbox-foo.run.app"
        url_b = "https://ytfactory-image-z-image-turbo-foo.run.app"
        with patch.object(
            subprocess, "run",
            side_effect=self._stub_run({url_a: "token-A", url_b: "token-B"}),
        ):
            cloudrun_auth.get_id_token(url_a)  # populates cache for A only
            tok_b = cloudrun_auth.get_id_token(url_b)
        self.assertEqual(
            tok_b, "token-B",
            "Per-audience cache must mint a NEW token for url_b "
            "even after a prior call cached one for url_a. "
            "If this asserts token-A you've reintroduced the global cache.",
        )

    def test_user_account_fallback(self) -> None:
        """If gcloud rejects the audience-scoped form (user ADC),
        fall back to the no-audience form."""
        url = "https://ytfactory-tts-chatterbox-foo.run.app"

        def fake_run(cmd, **kwargs):  # noqa: ARG001
            if any(a.startswith("--audiences=") for a in cmd):
                raise subprocess.CalledProcessError(
                    1, cmd, output="",
                    stderr="ERROR: (gcloud.auth.print-identity-token) "
                           "Invalid account type for audiences flag",
                )
            res = MagicMock()
            res.stdout = "user-account-token\n"
            return res

        with patch.object(subprocess, "run", side_effect=fake_run):
            tok = cloudrun_auth.get_id_token(url)
        self.assertEqual(tok, "user-account-token")

    def test_real_error_bubbles_up(self) -> None:
        """A non-audience-related gcloud error (e.g. not logged in)
        must NOT be silently swallowed by the user-account fallback."""
        url = "https://ytfactory-tts-chatterbox-foo.run.app"

        def fake_run(cmd, **kwargs):  # noqa: ARG001
            raise subprocess.CalledProcessError(
                1, cmd, output="",
                stderr="ERROR: (gcloud.auth.print-identity-token) "
                       "You do not currently have an active account selected.",
            )

        with patch.object(subprocess, "run", side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                cloudrun_auth.get_id_token(url)


class TestCachedTokenReused(unittest.TestCase):
    """Test the cache + gcloud path of the simpler cloudrun_auth module."""

    def setUp(self) -> None:
        cloudrun_auth._reset_cache_for_tests()

    def tearDown(self) -> None:
        cloudrun_auth._reset_cache_for_tests()

    def test_cached_token_returned_immediately(self):
        """Unexpired cached token is returned without calling gcloud."""
        url = "https://service.run.app"
        cloudrun_auth._TOKENS[url] = ("cached-tok", time.time() + 3600)
        with patch.object(subprocess, "run") as mock_run:
            tok = cloudrun_auth.get_id_token(url)
        self.assertEqual(tok, "cached-tok")
        mock_run.assert_not_called()

    def test_expired_cache_calls_gcloud(self):
        """Expired token triggers a fresh gcloud call."""
        url = "https://service.run.app"
        cloudrun_auth._TOKENS[url] = ("old-tok", time.time() - 1)

        def fake_run(cmd, **kwargs):
            res = MagicMock()
            res.stdout = "fresh-tok\n"
            return res

        with patch.object(subprocess, "run", side_effect=fake_run):
            tok = cloudrun_auth.get_id_token(url)
        self.assertEqual(tok, "fresh-tok")

    def test_successful_call_caches_token(self):
        """After successful gcloud call, token is cached."""
        url = "https://service.run.app"

        def fake_run(cmd, **kwargs):
            res = MagicMock()
            res.stdout = "new-tok\n"
            return res

        with patch.object(subprocess, "run", side_effect=fake_run):
            tok = cloudrun_auth.get_id_token(url)

        self.assertEqual(tok, "new-tok")
        self.assertIn(url, cloudrun_auth._TOKENS)

    def test_non_audience_error_bubbles_up(self):
        """A non-account-type error raises immediately."""
        url = "https://service.run.app"

        with patch.object(subprocess, "run",
                          side_effect=subprocess.CalledProcessError(
                              1, [], stderr="You are not logged in")):
            with self.assertRaises(subprocess.CalledProcessError):
                cloudrun_auth.get_id_token(url)

    def test_audience_error_falls_through_to_raise(self):
        """Invalid account type tries bare form, which also fails → RuntimeError."""
        url = "https://service.run.app"
        call_n = [0]

        def fake_run(cmd, **kwargs):
            call_n[0] += 1
            if "--audiences=" in " ".join(cmd):
                raise subprocess.CalledProcessError(1, cmd, stderr="invalid account type")
            # bare form also fails
            raise subprocess.CalledProcessError(1, cmd, stderr="invalid account type")

        with patch.object(subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                cloudrun_auth.get_id_token(url)


if __name__ == "__main__":
    unittest.main()
