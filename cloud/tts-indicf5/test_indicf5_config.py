"""Regression tests that protect the IndicF5 Cloud Run service.

Locks in:
  1. Defaults in server.py point at `ai4bharat/IndicF5` (not a fork).
  2. The IndicF5 model.py still uses `model_type=inf5` and
     architectures=['INF5Model'] — if AI4Bharat refactors the model
     class, our `AutoModel.from_pretrained(..., trust_remote_code=True)`
     will fail loudly and these tests will catch it before deploy.
  3. The fork's requirements still pin transformers<4.50 and
     numpy<=1.26.4 — both of which our requirements.txt already satisfies.

Runs WITHOUT a GPU and WITHOUT downloading model weights — only the
small JSON config (~250 bytes). Required env: HF_TOKEN with IndicF5
ToS accepted (set automatically from ~/.cache/huggingface/token in
local + on Cloud Run via build-time secret).

Run:
    /tmp/higgs-validate/bin/python -m pytest \\
        cloud/tts-indicf5/test_indicf5_config.py -v
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))


def _server_default_repo() -> str:
    """Read INDICF5_MODEL_REPO default from server.py without importing it."""
    src = (HERE / "server.py").read_text()
    m = re.search(
        r'INDICF5_MODEL_REPO\s*=\s*os\.environ\.get\(\s*"INDICF5_MODEL_REPO"\s*,\s*"([^"]+)"',
        src,
    )
    assert m is not None, "INDICF5_MODEL_REPO default not found in server.py"
    return m.group(1)


def test_server_default_points_at_ai4bharat_upstream():
    """If anyone changes the default to a community fork, fail loud."""
    assert _server_default_repo() == "ai4bharat/IndicF5"


def test_env_var_override_is_wired():
    """server.py must read INDICF5_MODEL_REPO from env so we can swap
    mirrors without rebuilding."""
    src = (HERE / "server.py").read_text()
    assert re.search(r'os\.environ\.get\(\s*"INDICF5_MODEL_REPO"', src)


@pytest.mark.network
def test_indicf5_config_uses_inf5_schema():
    """Pull config.json (no weights) and verify the schema our installed
    f5_tts fork expects.

    The fork's model.py registers `INF5Config` and `INF5Model` with
    `model_type = "inf5"`. If AI4Bharat ever refactors the class
    or repo, this test fails loudly and tells us to update either the
    pinned IndicF5 fork in the Dockerfile or the server.py loader.
    """
    from huggingface_hub import hf_hub_download

    token = os.environ.get("HF_TOKEN") or os.environ.get(
        "HUGGING_FACE_HUB_TOKEN"
    )
    if not token:
        # Try the cached CLI token
        cached = Path.home() / ".cache" / "huggingface" / "token"
        if cached.exists():
            token = cached.read_text().strip()

    cfg_path = hf_hub_download(
        _server_default_repo(), "config.json", token=token,
    )
    cfg = json.loads(Path(cfg_path).read_text())

    assert cfg.get("model_type") == "inf5", (
        f"IndicF5 model_type changed: {cfg.get('model_type')!r}. "
        f"server.py uses AutoModel.from_pretrained(...,trust_remote_code=True) "
        f"which depends on the auto_map pointing at INF5Model."
    )
    assert "INF5Model" in (cfg.get("architectures") or []), (
        f"IndicF5 architectures changed: {cfg.get('architectures')!r}. "
        f"Expected INF5Model."
    )
    auto_map = cfg.get("auto_map") or {}
    assert auto_map.get("AutoModel", "").endswith("INF5Model"), (
        f"IndicF5 auto_map changed: {auto_map!r}"
    )
    assert auto_map.get("AutoConfig", "").endswith("INF5Config"), (
        f"IndicF5 auto_map changed: {auto_map!r}"
    )


@pytest.mark.network
def test_indicf5_fork_requirements_still_compatible():
    """The IndicF5 fork's requirements.txt has hard upper bounds:
    `transformers<4.50` and `numpy<=1.26.4`. Confirm our pinned
    versions in requirements.txt still satisfy them — if not, our
    installed transformers/numpy will conflict with the fork's
    expectations and the build will fail."""
    import urllib.request

    req_url = "https://raw.githubusercontent.com/AI4Bharat/IndicF5/main/requirements.txt"
    upstream_req = urllib.request.urlopen(req_url, timeout=30).read().decode()

    assert "transformers<4.50" in upstream_req, (
        "IndicF5 fork dropped transformers<4.50 pin; revisit our pin "
        "in cloud/tts-indicf5/requirements.txt"
    )
    assert "numpy<=1.26.4" in upstream_req, (
        "IndicF5 fork dropped numpy<=1.26.4 pin; revisit our pin"
    )

    # Verify our pin matches
    our_req = (HERE / "requirements.txt").read_text()
    assert "transformers==4.49.0" in our_req, (
        "Our transformers pin must satisfy IndicF5 fork's <4.50"
    )
    assert "numpy==1.26.4" in our_req, (
        "Our numpy pin must satisfy IndicF5 fork's <=1.26.4"
    )
