"""Config — resolves service URLs (env), voice-ref paths, and channel YAML. The only place these live."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHANNELS_DIR = _REPO_ROOT / "pipeline" / "channels"
_VOICE_REFS_DIR = _REPO_ROOT / "pipeline" / "voice_refs"

# logical service name -> env var holding its Cloud Run URL (see CLAUDE.md)
_SERVICE_ENV = {
    "tts_chatterbox": "CLOUDRUN_TTS_CHATTERBOX_URL",
    "tts_indicf5": "CLOUDRUN_TTS_INDICF5_URL",
    "image_z_image_turbo": "CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL",
    "asr": "CLOUDRUN_ASR_URL",
    "editing_agent": "CLOUDRUN_EDITING_AGENT_URL",
    "clone_video": "CLOUDRUN_CLONE_VIDEO_URL",
}


class EnvConfig:
    """Config backed by environment variables + the repo's ``pipeline/channels/*.yaml`` files."""

    def __init__(
        self,
        channels_dir: Path = _CHANNELS_DIR,
        voice_refs_dir: Path = _VOICE_REFS_DIR,
    ) -> None:
        self._channels_dir = Path(channels_dir)
        self._voice_refs_dir = Path(voice_refs_dir)

    def service_url(self, name: str) -> str:
        env = _SERVICE_ENV.get(name)
        if env is None:
            raise KeyError(f"Config.service_url: unknown service '{name}' (known: {sorted(_SERVICE_ENV)})")
        url = os.environ.get(env)
        if not url:
            raise RuntimeError(f"Config.service_url: env var {env} for service '{name}' is unset")
        return url

    def voice_ref_path(self, voice_id: str) -> Path:
        wav = self._voice_refs_dir / f"{voice_id}.wav"
        if wav.exists():
            return wav
        folder = self._voice_refs_dir / voice_id
        if folder.is_dir():
            return folder
        raise FileNotFoundError(
            f"Config.voice_ref_path: no voice ref for '{voice_id}' under {self._voice_refs_dir}"
        )

    def channel_yaml(self, channel: str) -> dict[str, Any]:
        path = self._channels_dir / f"{channel}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Config.channel_yaml: no channel config at {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"Config.channel_yaml: {path} did not parse to a mapping")
        return data
