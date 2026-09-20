import os
import unittest
from unittest.mock import patch

from pipeline import cloudrun_auth


class MountedCredentialTests(unittest.TestCase):
    def setUp(self):
        cloudrun_auth._reset_cache_for_tests()

    def tearDown(self):
        cloudrun_auth._reset_cache_for_tests()

    def test_mounted_credential_mints_and_caches_audience_specific_tokens(self):
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/secrets/google.json"}), \
             patch.object(cloudrun_auth, "_metadata_token", return_value=None), \
             patch("google.oauth2.id_token.fetch_id_token", side_effect=["token-a", "token-b"]) as sdk, \
             patch.object(cloudrun_auth.subprocess, "run") as cli:
            self.assertEqual(cloudrun_auth.get_id_token("https://a.run.app"), "token-a")
            self.assertEqual(cloudrun_auth.get_id_token("https://a.run.app"), "token-a")
            self.assertEqual(cloudrun_auth.get_id_token("https://b.run.app"), "token-b")
            self.assertEqual([call.args[1] for call in sdk.call_args_list], ["https://a.run.app", "https://b.run.app"])
            cli.assert_not_called()

    def test_invalid_mounted_credential_does_not_switch_to_cli_identity(self):
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/secrets/google.json"}), \
             patch.object(cloudrun_auth, "_metadata_token", return_value=None), \
             patch("google.oauth2.id_token.fetch_id_token", side_effect=ValueError("invalid credential")), \
             patch.object(cloudrun_auth.subprocess, "run") as cli:
            with self.assertRaises(ValueError):
                cloudrun_auth.get_id_token("https://a.run.app")
            cli.assert_not_called()
