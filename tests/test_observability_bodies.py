"""Unit tests for ``pipeline.observability.bodies``.

These tests cover the foundation invariants the rest of the telemetry
work depends on:

* :func:`bodies_enabled` resolution order — explicit env > Cloud Run >
  off.
* :func:`redact_secrets` catches every shape we care about and never
  raises.
* :func:`_normalise_body` coerces every input type the pipeline throws
  at it.
* :func:`track_io` always emits an event (never raises), even on weird
  inputs.

The fan-out subscription mechanism (used by EventsBuffer) is also
covered so a future refactor can't silently break it.
"""
from __future__ import annotations

import os

import pytest

from pipeline.observability import bodies, telemetry


# ---- bodies_enabled --------------------------------------------------


@pytest.mark.parametrize(
    "env_value, k_service, cloud_run_job, expected",
    [
        ("1", "", "", True),
        ("0", "test-svc", "", False),
        ("false", "", "test-job", False),
        ("", "test-svc", "", True),
        ("", "", "test-job", True),
        ("", "", "", False),
        (None, "", "", False),
        (None, "test-svc", "", True),
    ],
)
def test_bodies_enabled_resolution(monkeypatch, env_value, k_service, cloud_run_job, expected):
    """Explicit env wins; otherwise Cloud Run signals enable bodies."""
    monkeypatch.delenv("YTFACTORY_TELEMETRY_BODIES", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    if env_value is not None:
        monkeypatch.setenv("YTFACTORY_TELEMETRY_BODIES", env_value)
    if k_service:
        monkeypatch.setenv("K_SERVICE", k_service)
    if cloud_run_job:
        monkeypatch.setenv("CLOUD_RUN_JOB", cloud_run_job)
    assert bodies.bodies_enabled() is expected


# ---- redact_secrets --------------------------------------------------


@pytest.mark.parametrize(
    "raw, marker",
    [
        ("Authorization: Bearer abcdef1234567890ABCDEFGHIJ", "Bearer [REDACTED]"),
        ("api_key=AKIAIOSFODNN7EXAMPLE123456", "api_key=[REDACTED]"),
        ("sk-1234567890abcdefghijklmnopqrstuv", "[REDACTED:sk]"),
        ("sk-ant-api03-VERYLONGTOKEN1234567890abc", "[REDACTED:sk-ant]"),
        ("AIzaSyD-AbCdEfGhIjKlMnOpQrStUvWxYz12345", "[REDACTED:google-api-key]"),
        ("ya29.ABCDEFGHIJKLMNOP1234567890", "[REDACTED:oauth-access-token]"),
        (
            "Token: eyJhbGciOiJSUzI1NiIs.eyJzdWIiOiJ0ZXN0In0.fakeSig123456",
            "[REDACTED:jwt]",
        ),
    ],
)
def test_redact_secrets_catches_common_shapes(raw, marker):
    out = bodies.redact_secrets(raw)
    assert marker in out
    # The original secret body should be GONE
    assert raw not in out or marker == raw  # sanity


def test_redact_secrets_handles_empty_string():
    assert bodies.redact_secrets("") == ""


def test_redact_secrets_handles_non_secret_text():
    plain = "the quick brown fox jumps over the lazy dog"
    assert bodies.redact_secrets(plain) == plain


def test_redact_secrets_never_raises_on_weird_input():
    # Pass something stringy but with regex-unsafe chars
    out = bodies.redact_secrets("\x00\x01\x02 weird (chars) [{}]")
    assert isinstance(out, str)


# ---- _normalise_body -------------------------------------------------


def test_normalise_body_none():
    assert bodies._normalise_body(None) is None


def test_normalise_body_str():
    assert bodies._normalise_body("hello") == "hello"


def test_normalise_body_bytes():
    assert bodies._normalise_body(b"hello") == "hello"


def test_normalise_body_dict_stable_hash():
    a = bodies._normalise_body({"b": 2, "a": 1})
    b = bodies._normalise_body({"a": 1, "b": 2})
    assert a == b  # sort_keys=True → identical for equivalent dicts


def test_normalise_body_list():
    assert "1" in bodies._normalise_body([1, 2, 3])


def test_normalise_body_falls_back_to_str_for_unknown():
    class Weird:
        def __str__(self):
            return "weird-instance"
    assert bodies._normalise_body(Weird()) == "weird-instance"


# ---- _body_fields ----------------------------------------------------


def test_body_fields_truncation_and_hash():
    long = "x" * (bodies.TEL_BODY_MAX_CHARS + 100)
    fields = bodies._body_fields("input", long, capture_body=True)
    assert fields["input_chars"] == len(long)
    assert len(fields["input_sha256"]) == 16
    assert fields["input_truncated"] is True
    assert len(fields["input_preview"]) == bodies.TEL_BODY_MAX_CHARS


def test_body_fields_cheap_mode_omits_preview():
    fields = bodies._body_fields("input", "abc", capture_body=False)
    assert "input_chars" in fields
    assert "input_sha256" in fields
    assert "input_preview" not in fields
    assert "input_truncated" not in fields


def test_body_fields_none_returns_empty():
    assert bodies._body_fields("input", None, capture_body=True) == {}


def test_body_fields_redacts_before_hash():
    raw = "Bearer abcdef1234567890ABCDEFGHIJ extra-text"
    redacted = bodies.redact_secrets(raw)
    fields = bodies._body_fields("input", raw, capture_body=True)
    expected_sha = bodies._sha256_short(redacted)
    assert fields["input_sha256"] == expected_sha
    assert "Bearer [REDACTED]" in fields["input_preview"]


# ---- track_io --------------------------------------------------------


def test_track_io_does_not_raise_on_none(monkeypatch):
    # Force bodies enabled so we exercise the full code path
    monkeypatch.setenv("YTFACTORY_TELEMETRY_BODIES", "1")
    bodies.track_io("smoke.event", input_text=None, output_text=None)


def test_track_io_does_not_raise_on_weird_types(monkeypatch):
    monkeypatch.setenv("YTFACTORY_TELEMETRY_BODIES", "1")
    bodies.track_io(
        "smoke.event",
        input_text={"foo": 1, "bar": [1, 2, 3]},
        output_text=b"\xff\xfe binary garbage \x00",
        input_meta={"stage": "test"},
        output_meta={"status": 200},
    )


def test_track_io_swallows_subscriber_errors(monkeypatch):
    """A broken subscriber must not break the pipeline."""

    def bad_subscriber(_event):
        raise RuntimeError("subscriber blew up")

    telemetry.subscribe(bad_subscriber)
    try:
        # Should not raise
        telemetry.track("smoke.event", success=True)
    finally:
        telemetry.unsubscribe(bad_subscriber)


def test_track_subscriber_receives_event():
    received: list[dict] = []

    def capture(event):
        received.append(event)

    telemetry.subscribe(capture)
    try:
        telemetry.track(
            "test.subscriber",
            category="test",
            success=True,
            metadata={"k": "v"},
        )
    finally:
        telemetry.unsubscribe(capture)

    assert any(ev.get("event") == "test.subscriber" for ev in received)


def test_track_subscribe_is_idempotent():
    def cb(_):
        pass

    telemetry.subscribe(cb)
    telemetry.subscribe(cb)
    telemetry.subscribe(cb)
    # Should appear exactly once
    assert telemetry._SUBSCRIBERS.count(cb) == 1
    telemetry.unsubscribe(cb)


def test_track_unsubscribe_missing_is_noop():
    def cb(_):
        pass

    # Should not raise
    telemetry.unsubscribe(cb)
