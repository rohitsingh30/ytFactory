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
    bucket via GCS Fuse must declare ``readonly=true``. Without it
    a compromised or misbehaving worker container could corrupt the
    canonical model weights for every other service that shares the
    bucket.

    The flag can live on either the volume DEFINITION line
    (``--add-volume="...,readonly=true"``) or the mount line
    (``--add-volume-mount="...,readonly=true"``). gcloud accepted both
    historically, but newer Cloud Run validators reject readonly on
    the mount line — see audit 2026-05-16. Either placement satisfies
    this test as long as the bucket is read-only at runtime."""

    def test_every_weights_mount_is_readonly(self) -> None:
        deploy_files = sorted(CLOUD_DIR.glob("*/deploy.sh"))
        offenders: list[str] = []
        checked = 0
        # Volume DEFINITION line — readonly here applies to the whole volume.
        volume_re = re.compile(
            r'--add-volume="name=weights,type=cloud-storage,bucket=[^"]*?(readonly=true)?[^"]*"'
        )
        for f in deploy_files:
            text = f.read_text()
            for m in WEIGHTS_MOUNT_RE.finditer(text):
                checked += 1
                trail = m.group(1)
                # Allow readonly on the mount line OR on the matching volume line.
                if "readonly=true" in trail:
                    continue
                vm = volume_re.search(text)
                if vm and "readonly=true" in vm.group(0):
                    continue
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
    # Post 2026-05-16 cost-optimization sweep — see
    # docs/cost_optimized_deploy.md. Dropped service mappings preserved
    # in git history.
    "tts-chatterbox":      "tts-runner",
    "tts-indicf5":         "tts-runner",
    "image-z-image-turbo": "image-runner",
    "asr-whisper":         "render-runner",
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


# ---------------------------------------------------------------------------
# 2026-05-13 — IAM completeness for `web-runner@` (post-mortem regression
# fence for the silent OAuth-callback 500).
#
# The 2026-05-12 SA flip from `tts-runner@` to `web-runner@` lost
# Firestore + bucket + signBlob + run.invoker grants because they don't
# fail at deploy time — only at first user request. 13-hour outage.
#
# These tests pin three properties:
#
#   1. Every role mentioned in docs/iam_per_service.md § "web-runner"
#      appears as a literal role string in cloud/iam/grant_web_runner.sh.
#      So adding a role to the doc forces adding it to the script.
#
#   2. cloud/iam/verify_web_runner.sh expects the same role set.
#
#   3. Every deploy.sh that pins `web-runner@` invokes verify_web_runner.sh
#      as preflight — so the next time grants drift, the deploy aborts
#      with a concrete fix command instead of "Deployed: <url>" + a
#      latent 500 waiting for the next user click.
# ---------------------------------------------------------------------------

GRANT_SCRIPT = CLOUD_DIR / "iam" / "grant_web_runner.sh"
VERIFY_SCRIPT = CLOUD_DIR / "iam" / "verify_web_runner.sh"
IAM_DOC = REPO_ROOT / "docs" / "iam_per_service.md"

# Match role lines in the doc's per-SA fenced code block. Examples
# the regex matches:
#   "roles/datastore.user                    # Firestore: ..."
#   "roles/storage.objectAdmin    on gs://ytfactory-prod-v2-state"
_DOC_ROLE_RE = re.compile(r"^\s*(roles/[a-zA-Z0-9._-]+)\b", re.MULTILINE)


def _extract_web_runner_roles_from_doc() -> set[str]:
    """Pull the role list under '### web-runner' in docs/iam_per_service.md.

    Returns the bare role strings (no resource/scope decoration). Empty
    set means parser failed — the test surfaces that loudly so a doc
    refactor doesn't silently disable the gate.
    """
    text = IAM_DOC.read_text()
    # Find the '### web-runner' section, stop at the next '### ' or end-of-file.
    m = re.search(
        r"^###\s+web-runner\s*\n(.*?)(?=^###\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return set()
    block = m.group(1)
    return set(_DOC_ROLE_RE.findall(block))


class TestWebRunnerGrantsAreComplete(unittest.TestCase):
    """2026-05-13 regression fence — see header comment above."""

    def test_grant_script_exists_and_is_executable(self) -> None:
        self.assertTrue(
            GRANT_SCRIPT.is_file(),
            f"missing canonical grant script {GRANT_SCRIPT.relative_to(REPO_ROOT)}",
        )
        # Bit-test via stat so we don't need to shell out.
        mode = GRANT_SCRIPT.stat().st_mode
        self.assertTrue(
            mode & 0o100,
            f"{GRANT_SCRIPT.relative_to(REPO_ROOT)} is not user-executable",
        )

    def test_verify_script_exists_and_is_executable(self) -> None:
        self.assertTrue(
            VERIFY_SCRIPT.is_file(),
            f"missing preflight verifier {VERIFY_SCRIPT.relative_to(REPO_ROOT)}",
        )
        mode = VERIFY_SCRIPT.stat().st_mode
        self.assertTrue(
            mode & 0o100,
            f"{VERIFY_SCRIPT.relative_to(REPO_ROOT)} is not user-executable",
        )

    def test_doc_lists_a_nonempty_role_set_for_web_runner(self) -> None:
        """If this fails, the doc parser regex broke (likely a doc
        section rename) — the role-completeness test below would
        silently degrade to vacuous true. Catch it explicitly."""
        roles = _extract_web_runner_roles_from_doc()
        self.assertGreater(
            len(roles), 0,
            f"could not parse any roles under '### web-runner' in "
            f"{IAM_DOC.relative_to(REPO_ROOT)} — fix the doc heading or "
            f"update _extract_web_runner_roles_from_doc() in this file.",
        )

    def test_every_doc_role_appears_in_grant_script(self) -> None:
        """The grant script is the executable contract for the doc.
        Every role the doc says web-runner needs must be a literal
        string in the script — otherwise a role added to the doc only
        is a footgun: the next operator running the script gets a
        false sense of completeness.
        """
        doc_roles = _extract_web_runner_roles_from_doc()
        script_text = GRANT_SCRIPT.read_text()
        missing = sorted(r for r in doc_roles if r not in script_text)
        self.assertEqual(
            missing, [],
            "Roles in docs/iam_per_service.md § web-runner that are "
            "NOT mentioned in cloud/iam/grant_web_runner.sh:\n  "
            + "\n  ".join(missing)
            + "\n\nFix: add the role(s) to the corresponding section of "
            "grant_web_runner.sh (PROJECT_ROLES / SELF_BINDING_ROLES / "
            "BUCKET_BINDINGS / ACCESSOR_SECRETS / WRITEBACK_ACCOUNTS).",
        )

    def test_every_doc_role_appears_in_verify_script(self) -> None:
        """Same contract for the verifier — operator running it should
        see every role the doc claims web-runner needs."""
        doc_roles = _extract_web_runner_roles_from_doc()
        script_text = VERIFY_SCRIPT.read_text()
        missing = sorted(r for r in doc_roles if r not in script_text)
        self.assertEqual(
            missing, [],
            "Roles in docs/iam_per_service.md § web-runner that are "
            "NOT mentioned in cloud/iam/verify_web_runner.sh:\n  "
            + "\n  ".join(missing)
            + "\n\nFix: add the role(s) to PROJECT_ROLES / SELF_BINDING_ROLES "
            "/ BUCKET_BINDINGS in verify_web_runner.sh.",
        )

    def test_every_web_runner_deploy_sh_invokes_verifier(self) -> None:
        """Every cloud/<svc>/deploy.sh that pins web-runner@ as the
        runtime SA MUST run verify_web_runner.sh as preflight — so the
        next IAM drift fails the deploy with a concrete fix instead of
        silently 500-ing on the next user request."""
        offenders: list[str] = []
        for svc, sa_prefix in EXPECTED_RUNTIME_SA.items():
            if sa_prefix != "web-runner":
                continue
            deploy_sh = CLOUD_DIR / svc / "deploy.sh"
            if not deploy_sh.exists():
                continue
            text = deploy_sh.read_text()
            if "verify_web_runner.sh" not in text:
                offenders.append(
                    f"{svc}/deploy.sh pins web-runner@ but does NOT invoke "
                    f"cloud/iam/verify_web_runner.sh as preflight."
                )
        self.assertEqual(
            offenders, [],
            "Missing IAM preflight on web-runner deploy(s):\n  "
            + "\n  ".join(offenders)
            + "\n\nFix: add the verifier call right after auth_setup.sh, e.g.\n"
            '    echo "==> Verifying web-runner IAM bindings (preflight)"\n'
            '    "$(cd "$(dirname "$0")" && pwd)/../iam/verify_web_runner.sh"',
        )


if __name__ == "__main__":
    unittest.main()
