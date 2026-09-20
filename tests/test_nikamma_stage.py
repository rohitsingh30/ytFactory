"""Validate first-release staging without cluster access or real secrets."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "cloud/nikamma/stage.py"
spec = importlib.util.spec_from_file_location("nikamma_stage", SCRIPT)
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secret = self.root / "sealed.yaml"
        self.documents = [
            {"apiVersion": "bitnami.com/v1alpha1", "kind": "SealedSecret",
             "metadata": {"name": name, "namespace": "ytfactory"},
             "spec": {"encryptedData": {key: "synthetic-test-ciphertext" for key in keys}}}
            for name, keys in stage.REQUIRED_SECRETS.items()
        ]

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.secret.write_text(yaml.safe_dump_all(self.documents))

    def test_rejects_plaintext_payload(self):
        self.documents[0]["spec"]["template"] = {"stringData": {"secret": "plaintext"}}
        self.write()
        with self.assertRaisesRegex(ValueError, "Plaintext"):
            stage.validated_secrets(self.secret)

    def test_rejects_wrong_namespace(self):
        self.documents[0]["metadata"]["namespace"] = "another-app"
        self.write()
        with self.assertRaisesRegex(ValueError, "namespace"):
            stage.validated_secrets(self.secret)

    def test_missing_runtime_credentials_are_not_deployable(self):
        self.documents.pop()
        self.write()
        with self.assertRaisesRegex(ValueError, "Google credential"):
            stage.validated_secrets(self.secret)

    def test_secrets_cannot_enable_cloud_run_auth_bypass(self):
        self.documents[1]["spec"]["encryptedData"]["K_SERVICE"] = "synthetic-test-ciphertext"
        self.write()
        with self.assertRaisesRegex(ValueError, "authentication flags"):
            stage.validated_secrets(self.secret)

    def test_stages_both_immutable_images_and_refuses_to_overwrite(self):
        self.write()
        (self.root / "AGENTS.md").write_text("Test fixture")
        (self.root / "argocd/apps").mkdir(parents=True)
        (self.root / "argocd/root.yaml").write_text("{}")
        (self.root / "cluster/namespaces").mkdir(parents=True)
        command = [sys.executable, str(SCRIPT), "--nikamma-checkout", str(self.root),
                   "--sealed-secrets", str(self.secret),
                   "--web-image", "ghcr.io/rohitsingh30/ytfactory-web@sha256:" + "a" * 64,
                   "--api-image", "ghcr.io/rohitsingh30/ytfactory-api@sha256:" + "b" * 64]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.root / "apps/ytfactory"
        config = yaml.safe_load((app / "kustomization.yaml").read_text())
        self.assertEqual([item["digest"] for item in config["images"]], ["sha256:" + "a" * 64, "sha256:" + "b" * 64])
        self.assertTrue(all((app / resource).is_file() for resource in config["resources"]))
        self.assertTrue((self.root / "argocd/apps/ytfactory.yaml").is_file())
        again = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)
