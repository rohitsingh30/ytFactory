"""Regression tests that protect the Higgs v2 Cloud Run service.

What we lock in (and why):

  1. The HIGGS_MODEL_REPO and HIGGS_TOKENIZER_REPO defaults in server.py
     point at the PierrunoYT community mirror — NOT the official bosonai/*
     checkpoints. Background:

       * In late 2026, Boson refactored the official bosonai/* configs:
         - main config:    model_type "higgs_audio" -> "higgs_audio_v2",
                           architectures HiggsAudioModel ->
                           HiggsAudioV2ForConditionalGeneration,
                           text_config block STRIPPED.
         - tokenizer cfg:  schema rewritten with acoustic_model_config,
                           dropping the n_filters/target_bandwidths shape
                           that boson_multimodal.audio_processing
                           .higgs_audio_tokenizer.HiggsAudioTokenizer
                           expects.
       * They DID NOT update the github boson_multimodal code to match.
       * The PierrunoYT mirror preserves the OLD configs. Until upstream
         self-heals, this mirror is the only working pair.

  2. The padding_patch is NOT installed because PierrunoYT's
     text_config.vocab_size (128256) > top-level pad_token_id (128001),
     so nn.Embedding(128256, 3072, 128001) is in-range and torch's
     internal assert is satisfied. No patch needed.

If either of those facts changes (PierrunoYT updates their mirror, or
upstream Boson fixes the official checkpoint), these tests will fail
LOUDLY and tell us to revisit the choice.

These tests run WITHOUT a GPU and WITHOUT downloading model weights —
only the small JSON config files (~5 KB each). Run:

    /tmp/higgs-validate/bin/python -m pytest \\
        cloud/tts-higgs/test_higgs_config.py -v
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


# ---------- defaults in server.py ----------


def _server_defaults() -> tuple[str, str]:
    """Read HIGGS_MODEL_REPO / HIGGS_TOKENIZER_REPO defaults from
    server.py without importing it (server.py needs fastapi + boson_multimodal
    which aren't in the test venv).
    """
    src = (HERE / "server.py").read_text()
    m_model = re.search(
        r'HIGGS_MODEL_REPO\s*=\s*os\.environ\.get\(\s*"HIGGS_MODEL_REPO"\s*,\s*"([^"]+)"',
        src,
    )
    m_tok = re.search(
        r'HIGGS_TOKENIZER_REPO\s*=\s*os\.environ\.get\(\s*"HIGGS_TOKENIZER_REPO"\s*,\s*"([^"]+)"',
        src,
    )
    assert m_model is not None, "HIGGS_MODEL_REPO default not found in server.py"
    assert m_tok is not None, "HIGGS_TOKENIZER_REPO default not found in server.py"
    return m_model.group(1), m_tok.group(1)


def test_server_defaults_point_at_pierrunoyt_mirror():
    """Lock in the choice. If anyone changes server.py to the bosonai/*
    repo, this test fails immediately."""
    model, tok = _server_defaults()
    assert model == "PierrunoYT/higgs-audio-v2-generation-3B-base", model
    assert tok == "PierrunoYT/higgs-audio-v2-tokenizer", tok


# ---------- live HF config compatibility ----------


@pytest.mark.network
def test_pierrunoyt_model_config_matches_installed_code_schema():
    """Pull config.json from the configured model repo (no weights) and
    confirm it has every field our installed boson_multimodal code reads:

      - model_type == "higgs_audio"        (not "higgs_audio_v2")
      - "HiggsAudioModel" in architectures (not the v2 ForCondGen class)
      - text_config block populated (not None)
      - text_config.vocab_size == 128256 (matches checkpoint shapes)
      - text_config.hidden_size == 3072  (matches checkpoint shapes)
      - top-level pad_token_id < text_config.vocab_size (no padding patch needed)
    """
    from huggingface_hub import hf_hub_download

    model_repo, _ = _server_defaults()
    cfg_path = hf_hub_download(model_repo, "config.json")
    cfg = json.loads(Path(cfg_path).read_text())

    assert cfg.get("model_type") == "higgs_audio", (
        f"{model_repo} model_type is {cfg.get('model_type')!r}; the "
        f"installed boson_multimodal at /opt/higgs-audio registers "
        f"AutoConfig.register('higgs_audio', ...). If upstream renamed, "
        f"either bump the boson_multimodal install or pick a new mirror."
    )
    assert "HiggsAudioModel" in (cfg.get("architectures") or []), (
        f"architectures changed: {cfg.get('architectures')!r}"
    )

    text_cfg = cfg.get("text_config")
    assert isinstance(text_cfg, dict), (
        f"text_config must be a dict in checkpoint config; got {type(text_cfg)!r}. "
        f"Without it HiggsAudioConfig falls back to LlamaConfig() defaults "
        f"(hidden_size=4096) which mismatches checkpoint weights (hidden_size=3072)."
    )
    assert text_cfg.get("vocab_size") == 128256, (
        f"text_config.vocab_size changed: {text_cfg.get('vocab_size')!r}"
    )
    assert text_cfg.get("hidden_size") == 3072, (
        f"text_config.hidden_size changed: {text_cfg.get('hidden_size')!r}"
    )

    pad_id = cfg.get("pad_token_id")
    vocab = text_cfg.get("vocab_size")
    assert pad_id is not None and vocab is not None
    assert pad_id < vocab, (
        f"pad_token_id ({pad_id}) >= text_config.vocab_size ({vocab}) — "
        f"nn.Embedding will crash. Re-introduce padding_patch.py."
    )


@pytest.mark.network
def test_pierrunoyt_tokenizer_config_uses_old_schema():
    """The audio tokenizer config must use the old n_filters /
    target_bandwidths schema that boson_multimodal.audio_processing
    .higgs_audio_tokenizer.HiggsAudioTokenizer.__init__ accepts.
    """
    from huggingface_hub import hf_hub_download

    _, tok_repo = _server_defaults()
    cfg_path = hf_hub_download(tok_repo, "config.json")
    cfg = json.loads(Path(cfg_path).read_text())

    assert "n_filters" in cfg, (
        f"{tok_repo} schema changed; got keys {list(cfg)[:8]!r}. "
        f"HiggsAudioTokenizer.__init__ won't bind."
    )
    assert "target_bandwidths" in cfg, (
        f"{tok_repo} missing target_bandwidths"
    )
    # Negative check: must NOT be the new bosonai/* schema
    assert "acoustic_model_config" not in cfg, (
        f"{tok_repo} appears to have moved to the new bosonai/* schema "
        f"(acoustic_model_config present). Pick a different mirror or "
        f"upgrade boson_multimodal."
    )


# ---------- env-var override path ----------


def test_env_var_override_works():
    """HIGGS_MODEL_REPO / HIGGS_TOKENIZER_REPO env vars can override the
    defaults at runtime — useful for trying a fresh upstream fix without
    rebuilding the image.
    """
    src = (HERE / "server.py").read_text()
    # Match os.environ.get("HIGGS_MODEL_REPO", ...) with any whitespace
    assert re.search(r'os\.environ\.get\(\s*"HIGGS_MODEL_REPO"', src), (
        "server.py must read HIGGS_MODEL_REPO from env"
    )
    assert re.search(r'os\.environ\.get\(\s*"HIGGS_TOKENIZER_REPO"', src), (
        "server.py must read HIGGS_TOKENIZER_REPO from env"
    )
