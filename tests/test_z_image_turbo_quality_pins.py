"""Regression pins for the 2026-05-24 Z-Image-Turbo quality additions.

These tests pin three additive recommendations from the research
playbook at ``docs/z_image_turbo_quality_playbook.md`` (O-Z1, O-Z2,
O-Z3). They guard against silent regressions of the values when the
server / client is refactored.

Sources (every assertion below has a corresponding citation):

- **O-Z1 — max_sequence_length=1024**
  https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8
  Tongyi-MAI staff: "Locally, you can set max_sequence_length=1024 to
  accommodate longer prompts."

- **O-Z2 — In-domain 9:16 = 720×1280 (and 16:9 = 1280×720)**
  https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/28
  Tongyi-MAI staff (QJerry): "not exceeded 256 pixels fluctuation of
  1024 resolution grid (like 768 ~ 1280)".
  Canonical bucket table:
  https://github.com/SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions

- **O-Z3 — _native_flash attention backend on the transformer**
  https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/139
  "25% faster — saved about 52 seconds on a 50-step Full-HD generation"

These pins are NOT a behavioural test of the model — they just confirm
the wire parameters and the client/server defaults stayed in the
intended position so a stray default change can't silently revert
quality.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_PATH = _REPO_ROOT / "cloud" / "image-z-image-turbo" / "server.py"


def _server_source() -> str:
    return _SERVER_PATH.read_text(encoding="utf-8")


# ----- O-Z1 — max_sequence_length=1024 in the /generate pipe call -----


def test_server_passes_max_sequence_length_1024_to_pipe() -> None:
    """The /generate handler must pass max_sequence_length=1024 to the
    diffusers ZImagePipeline call. Without this, the Qwen3-4B text
    encoder defaults to 512 tokens and truncates our verb-led anti-
    text safety suffix (the first thing dropped when our refined
    prompt + character description exceed 512 tokens).
    """
    src = _server_source()
    # The pipe call spans multiple lines with comments containing
    # parens. Search the inference_mode block as a contiguous chunk
    # ending at the OUTER closing paren — recognised by the line
    # pattern ``^            )\n`` (12-space indent, lone paren).
    m = re.search(
        r"with\s+torch\.inference_mode\(\):\s*\n"
        r"\s*result\s*=\s*pipe\(\n"
        r"(?P<body>(?:.*\n)*?)"  # non-greedy multi-line body
        r"\s*\)\s*\n",  # outer closing paren on its own line
        src,
    )
    assert m, (
        "Could not locate the 'with torch.inference_mode(): result = "
        "pipe(...)' block in cloud/image-z-image-turbo/server.py."
    )
    body = m.group("body")
    assert re.search(r"max_sequence_length\s*=\s*1024", body), (
        "cloud/image-z-image-turbo/server.py /generate handler must "
        "pass max_sequence_length=1024 to ZImagePipeline. Source:\n"
        "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8\n"
        f"Found pipe(...) body:\n{body}"
    )


# ----- O-Z2 — in-domain 9:16 / 16:9 buckets ----------------------------


def test_server_aspect_dims_are_in_domain() -> None:
    """The server's _ASPECT_DIMS map must use in-domain resolutions per
    Tongyi-MAI Discussion #28: 9:16 = 720×1280, 16:9 = 1280×720. The
    prior 768×1344 / 1344×768 buckets were ~64 px off-domain on the
    long side (training band is 1024 ± 256).
    """
    spec = importlib.util.spec_from_file_location(
        "_z_image_turbo_server", _SERVER_PATH,
    )
    # Don't actually exec the module — it imports diffusers + GPU
    # deps. Read the literal instead.
    src = _server_source()
    m = re.search(
        r"_ASPECT_DIMS\s*=\s*\{[^}]+\}",
        src,
        flags=re.DOTALL,
    )
    assert m, "Could not find _ASPECT_DIMS literal in server.py"
    block = m.group(0)
    assert '"9:16": (720, 1280)' in block, (
        f"9:16 must be (720, 1280) per Tongyi-MAI Discussion #28. "
        f"_ASPECT_DIMS block:\n{block}"
    )
    assert '"16:9": (1280, 720)' in block, (
        f"16:9 must be (1280, 720) per Tongyi-MAI Discussion #28. "
        f"_ASPECT_DIMS block:\n{block}"
    )
    # Off-domain values must NOT come back.
    assert "(768, 1344)" not in block, (
        "9:16 reverted to off-domain (768, 1344). See O-Z2 in "
        "docs/z_image_turbo_quality_playbook.md."
    )
    assert "(1344, 768)" not in block, (
        "16:9 reverted to off-domain (1344, 768). See O-Z2 in "
        "docs/z_image_turbo_quality_playbook.md."
    )


def test_client_default_width_height_match_in_domain() -> None:
    """pipeline.images.images.generate's default width/height must
    match the server's new in-domain 9:16 bucket (720×1280). Keeps
    client + server aligned so an explicit-default call at any site
    doesn't silently request an off-domain resolution.
    """
    from pipeline.images import images as _images
    sig = inspect.signature(_images.generate)
    w_default = sig.parameters["width"].default
    h_default = sig.parameters["height"].default
    assert w_default == 720, (
        f"images.generate(width=...) default must be 720, got {w_default}. "
        f"Aligning client + server keeps explicit-default calls in-domain."
    )
    assert h_default == 1280, (
        f"images.generate(height=...) default must be 1280, got {h_default}."
    )


# ----- O-Z3 — _native_flash attention backend --------------------------


def test_server_sets_native_flash_attention_backend() -> None:
    """server._pipe() must attempt set_attention_backend('_native_flash')
    after enabling slicing. The call is fails-open (try/except) so
    older diffusers / older hardware silently keeps the default SDPA
    path — but the attempt must be present so newer rollouts pick up
    the ~25% speedup from PyTorch's flash kernel.
    """
    src = _server_source()
    # The call must appear inside the _pipe() lazy-load block. Match
    # from `def _pipe():` to the next top-level `def `, `class `, or
    # `@app.` decorator.
    pipe_block = re.search(
        r"def _pipe\(\)[^\n]*\n(?:.*\n)*?(?=\ndef |\nclass |\n@app\.)",
        src,
    )
    assert pipe_block, "Could not locate _pipe() in server.py"
    body = pipe_block.group(0)
    assert "set_attention_backend" in body, (
        "_pipe() must call set_attention_backend('_native_flash') for "
        "the FlashAttention speedup (O-Z3). Source: "
        "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/139"
    )
    assert "_native_flash" in body, (
        "_pipe() must request the '_native_flash' attention backend "
        "(O-Z3). Source: "
        "https://huggingface.co/docs/diffusers/main/en/optimization/attention_backends"
    )
