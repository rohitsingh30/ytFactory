"""Walking-skeleton tests for pipeline.core + pipeline.infra.

Proves the spine assembles and runs: real Telemetry/Storage/Config injected into a Context,
the Orchestrator threads artifacts through stages in order, and a raising stage fails loud.
No network, no GPU — the AI seams are exercised as tiny in-test fakes.
"""
from __future__ import annotations

import pytest

from pipeline.core import (
    Artifact,
    Config,
    Context,
    Orchestrator,
    Storage,
    Telemetry,
)
from pipeline.core.contracts import RenderKind, RenderSpec, VisualKind
from pipeline.infra import EnvConfig, LocalStorage, LoggingTelemetry


def _spec() -> RenderSpec:
    return RenderSpec(
        channel="cosmosdecoded",
        niche="physics",
        source_ref="",
        kind=RenderKind.SHORT,
        visual_palette=(VisualKind.FOOTAGE,),
        aspect_ratio="9:16",
        output_resolution=(1080, 1920),
    )


def _ctx(tmp_path) -> Context:
    return Context(
        spec=_spec(),
        telemetry=LoggingTelemetry(),
        storage=LocalStorage(tmp_path / "artifacts"),
        config=EnvConfig(),
        work_dir=tmp_path / "work",
    )


# --- fake stages (the concern packages will provide the real ones) ----------


class _WriteSource:
    name = "source"

    def run(self, ctx: Context) -> None:
        ctx.put(Artifact.SOURCE, {"title": "hello"})


class _SourceToScript:
    name = "script"

    def run(self, ctx: Context) -> None:
        src = ctx.get(Artifact.SOURCE)
        ctx.put(Artifact.SCRIPT, f"script-of:{src['title']}")


class _Boom:
    name = "boom"

    def run(self, ctx: Context) -> None:
        raise RuntimeError("stage exploded")


class _NeverRuns:
    name = "never"

    def __init__(self) -> None:
        self.ran = False

    def run(self, ctx: Context) -> None:  # pragma: no cover - asserted not to run
        self.ran = True


# --- spine behaviour --------------------------------------------------------


def test_infra_satisfies_core_protocols(tmp_path):
    assert isinstance(LoggingTelemetry(), Telemetry)
    assert isinstance(LocalStorage(tmp_path), Storage)
    assert isinstance(EnvConfig(), Config)


def test_orchestrator_threads_artifacts_in_order(tmp_path):
    ctx = _ctx(tmp_path)
    result = Orchestrator().run([_WriteSource(), _SourceToScript()], ctx)

    assert result.ok is True
    assert result.stages_run == ["source", "script"]
    assert ctx.get(Artifact.SCRIPT) == "script-of:hello"


def test_orchestrator_fail_loud_aborts_and_propagates(tmp_path):
    ctx = _ctx(tmp_path)
    never = _NeverRuns()

    with pytest.raises(RuntimeError, match="stage exploded"):
        Orchestrator().run([_WriteSource(), _Boom(), never], ctx)

    assert never.ran is False                      # nothing after the failure ran
    assert ctx.has(Artifact.SOURCE) is True        # work before the failure survives
    assert ctx.has(Artifact.SCRIPT) is False


def test_context_get_missing_artifact_raises(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(KeyError, match="not produced yet"):
        ctx.get(Artifact.SCRIPT)


# --- infra impls ------------------------------------------------------------


def test_local_storage_roundtrip(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    src = tmp_path / "in.txt"
    src.write_text("payload", encoding="utf-8")

    uri = storage.put(src, "jobs/j1/out.txt")
    assert "jobs/j1/out.txt" in uri

    back = storage.get("jobs/j1/out.txt", tmp_path / "restored.txt")
    assert back.read_text(encoding="utf-8") == "payload"


def test_local_storage_rejects_key_escaping_root(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    src = tmp_path / "in.txt"
    src.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes storage root"):
        storage.put(src, "../escape.txt")


def test_env_config_service_url(monkeypatch):
    cfg = EnvConfig()
    monkeypatch.setenv("CLOUDRUN_ASR_URL", "https://asr.example")
    assert cfg.service_url("asr") == "https://asr.example"

    monkeypatch.delenv("CLOUDRUN_ASR_URL", raising=False)
    with pytest.raises(RuntimeError, match="is unset"):
        cfg.service_url("asr")

    with pytest.raises(KeyError, match="unknown service"):
        cfg.service_url("nope")


def test_env_config_channel_yaml_loads_real_channel():
    cfg = EnvConfig()
    data = cfg.channel_yaml("cosmosdecoded")
    assert isinstance(data, dict) and data

    with pytest.raises(FileNotFoundError):
        cfg.channel_yaml("no_such_channel")
