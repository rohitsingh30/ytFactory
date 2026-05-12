"""Regression tests for cloud/<svc>/deploy.sh hardening rules.

Audit Q2.65 — every GCS Fuse weights mount must be ``readonly=true``
so a misbehaving worker can't corrupt the canonical weights bucket.

Audit T1.11 — the render-worker-v2 deploy must use
``--update-secrets`` (additive) not ``--set-secrets`` (replace) so
subsequent secret-toggle redeploys don't regress the existing
secret bindings.

Audit S1.11 — Dockerfiles must NEVER reference ``ARG HF_TOKEN`` (or
similar leaky build-arg patterns) for secret values. BuildKit's
``RUN --mount=type=secret,id=...`` is the correct mechanism — the
secret is present only for the duration of the RUN and never lands
in the image layer history.
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

# Secret-shaped tokens that MUST NOT be passed via ARG. Add new
# tokens here as they're integrated; the gate fires on ANY
# match anywhere in any cloud/*/Dockerfile.
_LEAKY_ARG_NAMES = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "YTFACTORY_AGENT_TOKEN",
)
_ARG_RE = re.compile(r"^\s*ARG\s+([A-Z_][A-Z0-9_]*)\b", re.MULTILINE)


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


class TestNoPlaintextApiKeysInSetEnvVars(unittest.TestCase):
    """Audit S1.15 — secret-shaped values (API keys, HF tokens) MUST
    NOT land in --set-env-vars (which `gcloud run services describe`
    surfaces in plaintext to anyone with roles/run.viewer). They
    flow via Secret Manager (--set-secrets / --update-secrets)
    instead."""

    LEAKY_VAR_PATTERNS = (
        re.compile(r"AZURE_OPENAI_API_KEY=\$\{?[A-Z_][A-Z0-9_]*\}?"),
        re.compile(r"AZURE_OPENAI_WHISPER_API_KEY=\$\{?[A-Z_][A-Z0-9_]*\}?"),
        re.compile(r"HF_TOKEN=\$\{?[A-Z_][A-Z0-9_]*\}?"),
        re.compile(r"OPENAI_API_KEY=\$\{?[A-Z_][A-Z0-9_]*\}?"),
        re.compile(r"ANTHROPIC_API_KEY=\$\{?[A-Z_][A-Z0-9_]*\}?"),
    )

    def test_no_deploy_passes_secrets_in_env_vars(self) -> None:
        offenders: list[str] = []
        for deploy in sorted(CLOUD_DIR.glob("*/deploy.sh")):
            text = deploy.read_text()
            for ln_no, line in enumerate(text.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                # Only inspect --set-env-vars / --update-env-vars args.
                if "env-vars" not in line and "ENV_VARS" not in line:
                    continue
                for pat in self.LEAKY_VAR_PATTERNS:
                    m = pat.search(line)
                    if m:
                        offenders.append(
                            f"{deploy.relative_to(REPO_ROOT)}:{ln_no}: "
                            f"{m.group(0)}"
                        )
        self.assertEqual(
            offenders, [],
            "Cloud Run deploys must route API keys / HF tokens via "
            "Secret Manager (--update-secrets), not --set-env-vars "
            "(which leaks via `gcloud run services describe`).\n"
            "Offenders:\n  " + "\n  ".join(offenders),
        )


class TestDeployUsesNonDefaultServiceAccount(unittest.TestCase):
    """Audit S1.22 — every cloud/<svc>/deploy.sh must pin a
    --service-account= so the runtime SA isn't the broadly-privileged
    default Compute Engine one."""

    def test_every_deploy_specifies_service_account(self) -> None:
        offenders: list[str] = []
        for deploy in sorted(CLOUD_DIR.glob("*/deploy.sh")):
            text = deploy.read_text()
            # Skip files that don't actually run a deploy (some are
            # IAM-only helpers under cloud/iam/).
            if not (
                "gcloud run deploy" in text
                or "gcloud run jobs deploy" in text
            ):
                continue
            if "--service-account=" not in text:
                offenders.append(str(deploy.relative_to(REPO_ROOT)))
        self.assertEqual(
            offenders, [],
            "Cloud Run deploys must pin --service-account= to a "
            "purpose-specific SA, not the default Compute Engine SA.\n"
            "Offenders:\n  " + "\n  ".join(offenders),
        )


class TestNoLeakyArgSecrets(unittest.TestCase):
    """S1.11 — Dockerfiles must NEVER reference secret-shaped tokens
    via ARG. ARG values are baked into the image layer history,
    visible to anyone with image-pull access via `docker history`.
    BuildKit's ``RUN --mount=type=secret,id=...`` is the correct
    mechanism — see cloud/tts-indicparler/Dockerfile for the
    canonical pattern."""

    def test_no_dockerfile_uses_leaky_arg_for_secrets(self) -> None:
        offenders: list[str] = []
        for df in sorted(CLOUD_DIR.glob("*/Dockerfile")):
            text = df.read_text()
            for m in _ARG_RE.finditer(text):
                arg_name = m.group(1).upper()
                if arg_name in _LEAKY_ARG_NAMES:
                    rel = df.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}: ARG {arg_name} on line "
                                     f"{text[:m.start()].count(chr(10)) + 1}")
        self.assertEqual(
            offenders, [],
            "Dockerfiles must use BuildKit `RUN --mount=type=secret,id=...` "
            "for secret values; ARG bakes them into image layer history.\n"
            "Offenders:\n  " + "\n  ".join(offenders),
        )


class TestOtelCopyLandsBeforeCmd(unittest.TestCase):
    """Audit S1.10 — every OTel helper COPY must land BEFORE the
    CMD/ENTRYPOINT instruction so:

      1. Build cache stays efficient (CMD invalidates cache for
         later layers; helper bumps would force a re-CMD layer).
      2. The Dockerfile reads conventionally — every other `COPY
         <code>` line is above the CMD too.

    Pre-fix the add_otel_copy.sh awk pattern only matched the
    per-service-dir context (``^COPY server.py``); for repo-root
    services (render-worker-v2 / editing-agent / web-server) the
    OTel COPY lines fell through to the END-of-file branch and
    landed AFTER the CMD/ENTRYPOINT.
    """

    def test_otel_helpers_copy_before_cmd_or_entrypoint(self) -> None:
        helpers = ("otel_init.py", "cloud_run_json_exporter.py")
        offenders: list[str] = []
        for df in sorted(CLOUD_DIR.glob("*/Dockerfile")):
            text = df.read_text()
            lines = text.splitlines()
            # First CMD or ENTRYPOINT line (whichever comes first).
            cmd_line = None
            for i, ln in enumerate(lines):
                stripped = ln.lstrip()
                if stripped.startswith("CMD ") or stripped.startswith(("ENTRYPOINT ", "ENTRYPOINT[")):
                    cmd_line = i
                    break
            if cmd_line is None:
                continue
            for helper in helpers:
                # Find the COPY for this helper, if any.
                copy_line = None
                for i, ln in enumerate(lines):
                    if ln.lstrip().startswith("COPY") and helper in ln:
                        copy_line = i
                        break
                if copy_line is None:
                    continue
                if copy_line >= cmd_line:
                    rel = df.relative_to(REPO_ROOT)
                    offenders.append(
                        f"{rel}: COPY {helper} on line {copy_line + 1} "
                        f"is AFTER CMD/ENTRYPOINT on line {cmd_line + 1}",
                    )
        self.assertEqual(
            offenders, [],
            "OTel helper COPYs must land before CMD/ENTRYPOINT to "
            "preserve build-cache efficiency. See audit S1.10.\n"
            "Offenders:\n  " + "\n  ".join(offenders),
        )


# Audit S1.21 — per-service runtime-SA mapping. Pre-fix every Cloud Run
# service ran as a single ``tts-runner@`` SA (least-privilege violation:
# a compromised image-flux2-klein container had full TTS bucket access).
# Post-fix each service category gets its own SA — see
# docs/iam_per_service.md for the full rationale + role grants.
EXPECTED_RUNTIME_SA = {
    # service-dir-name (relative to cloud/) -> per-service SA prefix
    "tts-chatterbox":      "tts-runner",
    "tts-cosyvoice":       "tts-runner",
    "tts-f5":              "tts-runner",
    "tts-higgs":           "tts-runner",
    "tts-indicf5":         "tts-runner",
    "tts-indicparler":     "tts-runner",
    "image-flux2-klein":   "image-runner",
    "image-hidream":       "image-runner",
    "image-qwen":          "image-runner",
    "image-z-image-turbo": "image-runner",
    "render-worker-v2":    "render-runner",
    "editing-agent":       "render-runner",
    "web-server":          "web-runner",
    "clone-video-worker":  "web-runner",
    "web-next":            "web-next-runner",
    "weights-staging":     "weights-runner",
    "cobalt-api":          "cobalt-runner",
    "stats-refresh":       "stats-refresh-runner",
}

_SA_LINE_RE = re.compile(
    r'--service-account=(?P<q>"|\')?(?P<value>[^"\'\s\\]+)(?P=q)?'
)
_RUNTIME_SA_VAR_RE = re.compile(
    r'^RUNTIME_SA=(?P<q>"|\')?(?P<value>[^"\'\s\\]+)(?P=q)?',
    re.MULTILINE,
)


class TestServiceAccountIsolation(unittest.TestCase):
    """Audit S1.21 — every cloud/<svc>/deploy.sh must pin the per-service
    SA category documented in docs/iam_per_service.md. A non-TTS
    service falling back to ``tts-runner@`` is now a test failure.
    """

    def _resolved_sa(self, deploy_sh: Path) -> str | None:
        """Extract the SA prefix the deploy.sh resolves to.

        Two patterns supported:
          1. Inline:   ``--service-account="<sa>@${PROJECT}..."``
          2. Variable: ``RUNTIME_SA="<sa>@${PROJECT}..."`` then
                       ``--service-account="${RUNTIME_SA}"``
        Returns the SA prefix (everything before ``@``) or None if the
        deploy script has no service-account binding (some scripts are
        sub-commands, not full deploys).
        """
        text = deploy_sh.read_text()
        m = _SA_LINE_RE.search(text)
        if not m:
            return None
        value = m.group("value")
        if value.startswith("${RUNTIME_SA}") or value == "${RUNTIME_SA}":
            mv = _RUNTIME_SA_VAR_RE.search(text)
            if not mv:
                return None
            value = mv.group("value")
        # Strip any leading ${...} that wasn't fully expanded above.
        if "@" not in value:
            return None
        return value.split("@", 1)[0]

    def test_every_deploy_sh_uses_documented_per_service_sa(self) -> None:
        offenders: list[str] = []
        unmapped: list[str] = []
        for svc_dir in sorted(CLOUD_DIR.iterdir()):
            if not svc_dir.is_dir():
                continue
            if svc_dir.name in {"_shared", "_bench", "iam"}:
                continue
            deploy_sh = svc_dir / "deploy.sh"
            if not deploy_sh.exists():
                continue
            sa = self._resolved_sa(deploy_sh)
            if sa is None:
                # Script doesn't pin an SA (e.g. helper-only scripts).
                continue
            expected = EXPECTED_RUNTIME_SA.get(svc_dir.name)
            if expected is None:
                unmapped.append(
                    f"{svc_dir.name}: deploy.sh pins SA {sa!r} but is not in "
                    f"EXPECTED_RUNTIME_SA — add it to docs/iam_per_service.md "
                    f"and to this test's mapping."
                )
                continue
            if sa != expected:
                offenders.append(
                    f"{svc_dir.name}: deploy.sh pins {sa!r}, expected {expected!r} "
                    f"per docs/iam_per_service.md (audit S1.21)."
                )
        msg_parts = []
        if offenders:
            msg_parts.append("Service-account drift:\n  " + "\n  ".join(offenders))
        if unmapped:
            msg_parts.append("Unmapped services:\n  " + "\n  ".join(unmapped))
        self.assertFalse(msg_parts, "\n\n".join(msg_parts))

    def test_no_non_tts_service_uses_tts_runner_sa(self) -> None:
        """Belt-and-braces fail-fast for the specific S1.21 regression:
        a non-TTS deploy.sh referencing tts-runner@ would re-introduce
        the very vulnerability the audit flagged.
        """
        offenders: list[str] = []
        for svc_dir in sorted(CLOUD_DIR.iterdir()):
            if not svc_dir.is_dir():
                continue
            if svc_dir.name in {"_shared", "_bench", "iam"}:
                continue
            deploy_sh = svc_dir / "deploy.sh"
            if not deploy_sh.exists():
                continue
            sa = self._resolved_sa(deploy_sh)
            if sa == "tts-runner" and not svc_dir.name.startswith("tts-"):
                offenders.append(
                    f"{svc_dir.name}/deploy.sh pins tts-runner@ — "
                    f"non-TTS services must use a per-service SA "
                    f"(see docs/iam_per_service.md, audit S1.21)."
                )
        self.assertEqual(offenders, [],
                         "tts-runner@ leak:\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
