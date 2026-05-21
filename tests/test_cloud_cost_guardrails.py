"""Static guardrails for the 2026-05-17 cost audit.

Five rules (see CLAUDE.md "Cost guardrails" + docs/cost_optimized_deploy.md
"Iron rules"). Each rule maps to a real GCP bill line item that bit us:

  1. min-instances=0  — `Cloud Run CPU/Memory instance-based`
  2. max-instances=1  — `NVIDIA L4 GPU` SKU spend × 2
  3. --region=asia-southeast1 on `gcloud builds submit` —
     `Artifact Registry Inter Region Egress Intercontinental`
  4. threading.Lock around `_model()`/`_pipe()` lazy-load —
     OOM on cold-start race, manifests as failed renders
  5. (No test here — judgment call, covered by code review)

Tests are static-file scans, no GCP access required. They run in
~50 ms and fail with a clear `assert` message naming the offending
file + line so future agents see the rule before the bill does.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLOUD_DIR = REPO_ROOT / "cloud"

# Only services that actually run on a GPU (L4) need the GPU-specific
# rules. CPU-only services like render-worker-v2, web-server, web-next,
# editing-agent, cobalt-api, clone-video-worker, stats-refresh,
# weights-staging follow different cost dynamics.
GPU_SERVICES = (
    "image-z-image-turbo",
    "tts-chatterbox",
    "tts-indicf5",
    "asr-whisper",
)

# Every directory under cloud/ that has its own deploy.sh and is NOT
# the _shared helpers or the _bench staging area. Used for rule 3
# (build region applies to every service, not just GPU ones).
def _deploy_scripts() -> list[Path]:
    return sorted(
        p for p in CLOUD_DIR.glob("*/deploy.sh")
        if "_bench" not in p.parts and "_shared" not in p.parts
    )


def _gpu_deploy_scripts() -> list[Path]:
    return [CLOUD_DIR / svc / "deploy.sh" for svc in GPU_SERVICES]


def _gpu_server_files() -> list[Path]:
    return [CLOUD_DIR / svc / "server.py" for svc in GPU_SERVICES]


# ---------------------------------------------------------------------------
# Rule 2 — max-instances=1 on every GPU service deploy.sh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deploy_path", _gpu_deploy_scripts(), ids=lambda p: p.parent.name)
def test_gpu_service_max_instances_is_1(deploy_path: Path) -> None:
    """Every GPU service deploy.sh MUST set --max-instances=1.

    Caught 2026-05-17 cost audit: TTS + ASR were defaulted to
    --max-instances=2, doubling potential L4 spend (₹1,243/day on the
    main GPU SKU). The render pipeline calls TTS/ASR sequentially per
    chunk; concurrency=2 already covers any in-instance pipelining.
    """
    assert deploy_path.exists(), f"deploy script missing: {deploy_path}"
    text = deploy_path.read_text()
    # Strip line-continuation backslashes so the regex sees one logical line.
    flat = re.sub(r"\\\s*\n\s*", " ", text)
    m = re.search(r"--max-instances=(\d+)", flat)
    assert m, (
        f"{deploy_path.relative_to(REPO_ROOT)}: --max-instances flag missing "
        "(must be =1 per cost-audit rule 2)"
    )
    value = int(m.group(1))
    assert value == 1, (
        f"{deploy_path.relative_to(REPO_ROOT)}: --max-instances={value} "
        f"(must be =1; second L4 instance is pure cost — see CLAUDE.md "
        f"Cost guardrails rule 2)"
    )


# ---------------------------------------------------------------------------
# Rule 3 — --region=asia-southeast1 on every `gcloud builds submit`
# ---------------------------------------------------------------------------

# These deploy scripts are out-of-rotation today but their flags still
# count if/when they're next deployed. Use the same lookup as rule 2
# but apply to ALL services with a deploy.sh.
@pytest.mark.parametrize(
    "deploy_path", _deploy_scripts(), ids=lambda p: p.parent.name,
)
def test_builds_submit_pins_region(deploy_path: Path) -> None:
    """Every `gcloud builds submit` in a deploy.sh MUST pass --region.

    Without --region, Cloud Build runs in the global pool (US Iowa).
    Artifact Registry sits in asia-southeast1, so every image push
    crosses the Pacific — that's the "Artifact Registry Inter Region
    Egress Intercontinental" SKU (₹430/day on z-image-turbo alone).
    """
    text = deploy_path.read_text()
    # Find every "gcloud builds submit" invocation on a non-comment
    # line, expanding line-continuations so each invocation is one
    # logical command.
    invocations = _extract_submit_invocations(text)
    assert invocations or "gcloud builds submit" not in text, (
        f"{deploy_path.relative_to(REPO_ROOT)}: failed to parse the "
        "`gcloud builds submit` invocation (regex update needed in "
        "_extract_submit_invocations)"
    )
    for cmd in invocations:
        assert "--region=" in cmd or "--region " in cmd, (
            f"{deploy_path.relative_to(REPO_ROOT)}: `gcloud builds submit` "
            f"is missing --region= (must pin to asia-southeast1 to avoid "
            f"intercontinental egress — see CLAUDE.md Cost guardrails "
            f"rule 3). Offending line: {cmd[:200]!r}"
        )


def test_shared_submit_build_pins_region() -> None:
    """`cloud/_shared/submit_build.sh::submit_build()` MUST pin --region.

    This is the shared helper sourced by every service that doesn't
    use --config=cloudbuild.yaml directly. If it loses the --region
    flag, every service that calls it regresses silently.
    """
    helper = REPO_ROOT / "cloud" / "_shared" / "submit_build.sh"
    text = helper.read_text()
    assert "CLOUDBUILD_REGION" in text or "GCP_REGION" in text, (
        f"{helper.relative_to(REPO_ROOT)}: missing region pickup "
        "(expected CLOUDBUILD_REGION or GCP_REGION env var)"
    )
    invocations = _extract_submit_invocations(text)
    assert invocations, f"{helper.name}: no `gcloud builds submit` found"
    for cmd in invocations:
        assert "--region" in cmd or "${region_arg}" in cmd, (
            f"{helper.name}: `gcloud builds submit` invocation missing "
            f"--region or region_arg expansion. Offending: {cmd[:200]!r}"
        )


def _extract_submit_invocations(text: str) -> list[str]:
    """Return every `gcloud builds submit ...` invocation that is NOT
    inside a comment. Handles line continuations (backslash-newline)
    so each invocation is returned as a single flattened string.
    """
    results: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        # Skip empty + shell comment lines outright.
        if stripped.startswith("#") or not stripped:
            i += 1
            continue
        if "gcloud builds submit" in line:
            # Skip if the match is inside a quoted string within a
            # comment that the line-level filter didn't catch (e.g.
            # echo "...gcloud builds submit..."). We only flag actual
            # bare invocations: an odd number of unescaped " or '
            # before the match means we're inside a string literal.
            idx = line.find("gcloud builds submit")
            preceding = line[:idx]
            if preceding.count('"') % 2 == 1 or preceding.count("'") % 2 == 1:
                i += 1
                continue
            # Collect all continuation lines.
            invocation = line.rstrip()
            while invocation.endswith("\\"):
                invocation = invocation[:-1].rstrip()  # drop trailing \
                i += 1
                if i >= len(lines):
                    break
                invocation += " " + lines[i].strip()
            results.append(invocation)
        i += 1
    return results


# ---------------------------------------------------------------------------
# Rule 4 — threading.Lock around lazy-load in every GPU server.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("server_path", _gpu_server_files(), ids=lambda p: p.parent.name)
def test_gpu_server_lazy_load_is_locked(server_path: Path) -> None:
    """Every GPU service server.py MUST guard its lazy-load with a Lock.

    Without a lock, two concurrent requests during cold start (typically
    /readyz from the warmer + first /generate or /synth from the
    pipeline) both see `_MODEL is None`, both start loading the model,
    and double-allocate VRAM → OOM on the 22 GiB L4.

    We accept any of these idioms:
      * `threading.Lock()` at module level
      * Any `Lock()` referenced inside `_model()` / `_pipe()` /
        `_get_model()`
    """
    assert server_path.exists(), f"server file missing: {server_path}"
    text = server_path.read_text()
    assert "import threading" in text or "from threading import" in text, (
        f"{server_path.relative_to(REPO_ROOT)}: missing `import threading` "
        "(needed for the lazy-load Lock — see CLAUDE.md Cost guardrails "
        "rule 4)"
    )
    assert re.search(r"_[A-Za-z0-9_]*LOCK\s*=\s*threading\.Lock\(\)", text) or \
           re.search(r"=\s*Lock\(\)", text), (
        f"{server_path.relative_to(REPO_ROOT)}: missing `_*_LOCK = "
        "threading.Lock()` at module level (CLAUDE.md Cost guardrails "
        "rule 4)"
    )
    # And the lock has to actually wrap the load — naively asserting the
    # lock exists isn't enough. Look for `with _*_LOCK:` somewhere in the
    # file's lazy-load function body.
    assert re.search(r"with\s+_[A-Za-z0-9_]*LOCK\s*:", text), (
        f"{server_path.relative_to(REPO_ROOT)}: lock object exists but "
        "is never used (`with _*_LOCK:` block not found — CLAUDE.md "
        "Cost guardrails rule 4)"
    )
