"""Regression tests for cloud/<svc>/deploy.sh hardening rules.

Audit Q2.65 — every GCS Fuse weights mount must be ``readonly=true``
so a misbehaving worker can't corrupt the canonical weights bucket.

Audit T1.11 — the render-worker-v2 deploy must use
``--update-secrets`` (additive) not ``--set-secrets`` (replace) so
subsequent secret-toggle redeploys don't regress the existing
secret bindings.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUD_DIR = REPO_ROOT / "cloud"


WEIGHTS_MOUNT_RE = re.compile(
    r"--add-volume-mount=\"volume=weights,mount-path=/models/hf([^\"]*)\""
)


class TestWeightsMountIsReadonly(unittest.TestCase):
    """Q2.65 — every cloud/<svc>/deploy.sh that mounts the weights
    bucket via GCS Fuse must declare ``readonly=true`` on the mount.
    Without it a compromised or misbehaving worker container could
    corrupt the canonical model weights for every other service that
    shares the bucket."""

    def test_every_weights_mount_is_readonly(self) -> None:
        deploy_files = sorted(CLOUD_DIR.glob("*/deploy.sh"))
        # Filter to only ones that actually mount weights.
        offenders: list[str] = []
        checked = 0
        for f in deploy_files:
            text = f.read_text()
            for m in WEIGHTS_MOUNT_RE.finditer(text):
                checked += 1
                trail = m.group(1)
                if "readonly=true" not in trail:
                    offenders.append(f"{f.relative_to(REPO_ROOT)}: missing readonly=true")
        self.assertGreaterEqual(
            checked, 1, "expected at least one weights mount in cloud/*/deploy.sh",
        )
        self.assertEqual(
            offenders, [],
            "weights mounts must be readonly=true:\n" + "\n".join(offenders),
        )


class TestRenderWorkerSecretsAreAdditive(unittest.TestCase):
    """T1.11 — the render-worker-v2 deploy must use --update-secrets
    (additive) not --set-secrets (replace) so toggling
    ANTHROPIC_API_KEY on/off via re-deploy doesn't regress the
    AZURE_OPENAI_API_KEY binding."""

    def test_render_worker_uses_update_secrets(self) -> None:
        deploy = CLOUD_DIR / "render-worker-v2" / "deploy.sh"
        self.assertTrue(deploy.exists(), f"{deploy} should exist")
        text = deploy.read_text()
        # MUST use --update-secrets …
        self.assertIn(
            "--update-secrets=", text,
            "render-worker-v2/deploy.sh should use --update-secrets "
            "(additive) for AZURE_OPENAI_API_KEY",
        )
        # MUST NOT use --set-secrets= for the canonical secret binding.
        # (The flag string can still appear in a comment explaining the
        # bug; check we removed the actual flag invocation.)
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                "--set-secrets=", stripped,
                "render-worker-v2/deploy.sh must not use --set-secrets= "
                "(destructive) for any active secret flag — see audit T1.11",
            )


if __name__ == "__main__":
    unittest.main()
