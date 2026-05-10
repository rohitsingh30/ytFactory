"""Tests for control/core/cloud_run.py — Cloud Run Job dispatcher."""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

from control.core import cloud_run  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_render_backend_default_is_sim(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YTFACTORY_RENDER_BACKEND", None)
            self.assertEqual(cloud_run.render_backend(), "sim")

    def test_render_backend_cloudrun(self):
        with patch.dict(os.environ, {"YTFACTORY_RENDER_BACKEND": "cloudrun"}):
            self.assertEqual(cloud_run.render_backend(), "cloudrun")

    def test_is_cloudrun_true(self):
        with patch.dict(os.environ, {"YTFACTORY_RENDER_BACKEND": "cloudrun"}):
            self.assertTrue(cloud_run.is_cloudrun())

    def test_is_cloudrun_false(self):
        with patch.dict(os.environ, {"YTFACTORY_RENDER_BACKEND": "sim"}):
            self.assertFalse(cloud_run.is_cloudrun())

    def test_project_id_from_env(self):
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "my-proj"}):
            self.assertEqual(cloud_run.project_id(), "my-proj")

    def test_project_id_default(self):
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_CLOUD_PROJECT"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(cloud_run.project_id(), cloud_run.DEFAULT_PROJECT)

    def test_region_from_env(self):
        with patch.dict(os.environ, {"YTFACTORY_CLOUDRUN_REGION": "us-central1"}):
            self.assertEqual(cloud_run.region(), "us-central1")

    def test_job_name_from_env(self):
        with patch.dict(os.environ, {"YTFACTORY_CLOUDRUN_JOB": "custom-job"}):
            self.assertEqual(cloud_run.job_name(), "custom-job")


class TriggerRenderJobTest(unittest.TestCase):
    def test_empty_job_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            cloud_run.trigger_render_job("")

    def test_sdk_path_success(self):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_op.metadata.name = "projects/p/locations/r/jobs/j/executions/exec-123"
        mock_client.run_job.return_value = mock_op

        with patch("google.cloud.run_v2.JobsClient", return_value=mock_client):
            with patch("google.cloud.run_v2.RunJobRequest") as MockRequest:
                with patch("google.cloud.run_v2.EnvVar"):
                    MockRequest.Overrides = MagicMock()
                    MockRequest.Overrides.ContainerOverride = MagicMock()
                    ref = cloud_run._trigger_via_sdk("job-abc")

        self.assertEqual(ref.job_id, "job-abc")
        self.assertEqual(ref.triggered_via, "sdk")

    def test_sdk_metadata_none_gives_pending(self):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_op.metadata = None  # no metadata
        mock_client.run_job.return_value = mock_op

        with patch("google.cloud.run_v2.JobsClient", return_value=mock_client):
            with patch("google.cloud.run_v2.RunJobRequest") as MockRequest:
                with patch("google.cloud.run_v2.EnvVar"):
                    MockRequest.Overrides = MagicMock()
                    MockRequest.Overrides.ContainerOverride = MagicMock()
                    ref = cloud_run._trigger_via_sdk("job-xyz")

        self.assertEqual(ref.execution_name, "(pending)")

    def test_sdk_import_error_falls_back_to_cli(self):
        """trigger_render_job falls back to CLI when SDK not installed."""
        with patch.object(cloud_run, "_trigger_via_sdk", side_effect=ImportError("no sdk")):
            with patch.object(cloud_run, "_trigger_via_cli") as mock_cli:
                mock_cli.return_value = cloud_run.ExecutionRef(
                    job_id="job-1", execution_name="exec-1", triggered_via="cli"
                )
                ref = cloud_run.trigger_render_job("job-1")
        self.assertEqual(ref.triggered_via, "cli")

    def test_cli_path_no_gcloud_raises(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                cloud_run._trigger_via_cli("job-1")
        self.assertIn("gcloud", str(cm.exception))

    def test_cli_path_gcloud_failure_raises(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "permission denied"
        proc.stdout = ""
        with patch("shutil.which", return_value="/usr/bin/gcloud"):
            with patch("subprocess.run", return_value=proc):
                with self.assertRaises(RuntimeError) as cm:
                    cloud_run._trigger_via_cli("job-1")
        self.assertIn("rc=1", str(cm.exception))

    def test_cli_path_success(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "projects/p/executions/exec-456\n"
        proc.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/gcloud"):
            with patch("subprocess.run", return_value=proc):
                ref = cloud_run._trigger_via_cli("job-2")
        self.assertEqual(ref.job_id, "job-2")
        self.assertEqual(ref.triggered_via, "cli")
        self.assertIn("exec-456", ref.execution_name)

    def test_cli_empty_stdout_gives_unknown(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/gcloud"):
            with patch("subprocess.run", return_value=proc):
                ref = cloud_run._trigger_via_cli("job-3")
        self.assertEqual(ref.execution_name, "(unknown)")


class StatusTest(unittest.TestCase):
    def test_status_returns_dict_with_backend(self):
        with patch.dict(os.environ, {"YTFACTORY_RENDER_BACKEND": "cloudrun"}):
            s = cloud_run.status()
        self.assertEqual(s["backend"], "cloudrun")
        self.assertIn("sdk_available", s)
        self.assertIn("cli_available", s)

    def test_sdk_available_false_on_import_error(self):
        import sys
        # Temporarily hide google.cloud.run_v2
        saved = sys.modules.get("google.cloud.run_v2")
        sys.modules["google.cloud.run_v2"] = None  # type: ignore[assignment]
        try:
            result = cloud_run._sdk_available()
            self.assertFalse(result)
        finally:
            if saved is not None:
                sys.modules["google.cloud.run_v2"] = saved
            elif "google.cloud.run_v2" in sys.modules:
                del sys.modules["google.cloud.run_v2"]

    def test_sdk_available_true_when_installed(self):
        # google-cloud-run is installed in the venv.
        result = cloud_run._sdk_available()
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
