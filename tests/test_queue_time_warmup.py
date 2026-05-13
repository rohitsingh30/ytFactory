"""Regression tests for queue-time warmup wiring.

Pins the 2026-05-13 fix that wires `pipeline.cloud.warm.warm_async_http`
into `_enqueue_render_job` so that GPU services start warming AS SOON
AS a render is queued — not later, when the worker hits its first
TTS / image call and discovers the service is cold (and possibly
GPU-quota-capped).

Background: canary 9b96e438 hit a chatterbox cold-start 502 because
the service had scaled to zero between renders. The pre-fix render
flow was:

  POST /api/render → create job doc → dispatch worker JOB →
  worker boots (~30s) → rewrite (~2-7 min) → first TTS call →
  chatterbox cold-start 502 → render fails

Post-fix:

  POST /api/render → create job doc → kick off warm_async_http →
  dispatch worker JOB → worker boots → rewrite → first TTS call →
  chatterbox is WARM (warmup completed during the rewrite stage)
"""
from __future__ import annotations

from unittest.mock import patch

from control.core import jobs as _jobs
from control.core.schema import ShortProposal


def test_enqueue_render_job_fires_warm_async_http():
    """When _enqueue_render_job creates a job, it MUST kick off the
    HTTP warmup in the background. Verifies the call lands; the
    actual provider warmups themselves are mocked away (they're
    pinned by per-provider tests in test_tts_cloudrun_full.py /
    test_images_cloudrun.py)."""
    proposal = ShortProposal(
        channel="mystoriesanimated",
        format="long_form",
        topic="A test render",
        source_kind="reddit_url",
        source_ref="https://reddit.com/r/nosleep/comments/x/y/",
        length_s=1800,
    )

    captured: dict = {}

    def _fake_warm(channel):
        captured["channel"] = channel

    # We need to mock create_job, cloud_run.render_backend, and the
    # downstream dispatch since this is a unit test. We only care
    # about whether warm_async_http gets called with the right
    # channel.
    with patch.object(_jobs, "create_job"), \
         patch("control.core.cloud_run.render_backend", return_value="sim"), \
         patch("control.core.queue.get_queue") as queue_mock, \
         patch("control.core.queue.new_task_id", return_value="task-x"), \
         patch("pipeline.cloud.warm.warm_async_http", side_effect=_fake_warm):
        queue_mock.return_value.enqueue = lambda task: None
        _jobs._enqueue_render_job(proposal, owner_uid="rohit@example.com")

    assert captured.get("channel") == "mystoriesanimated", \
        f"warm_async_http should be called with channel='mystoriesanimated'; got {captured}"


def test_enqueue_render_job_swallows_warm_failure():
    """If warm_async_http raises (e.g. import error / network glitch),
    the render dispatch MUST proceed anyway — warmup is opportunistic.
    Pre-fix ANY failure in warm_async_http would crash _enqueue_render_job."""
    proposal = ShortProposal(
        channel="mystoriesanimated",
        format="long_form",
        topic="A test render",
        source_kind="reddit_url",
        source_ref="https://reddit.com/r/nosleep/comments/x/y/",
        length_s=1800,
    )

    def _exploding_warm(channel):
        raise RuntimeError("simulated warm failure (e.g. URL env unset)")

    with patch.object(_jobs, "create_job"), \
         patch("control.core.cloud_run.render_backend", return_value="sim"), \
         patch("control.core.queue.get_queue") as queue_mock, \
         patch("control.core.queue.new_task_id", return_value="task-x"), \
         patch("pipeline.cloud.warm.warm_async_http", side_effect=_exploding_warm):
        queue_mock.return_value.enqueue = lambda task: None
        # Must NOT raise
        resp = _jobs._enqueue_render_job(proposal, owner_uid="rohit@example.com")
    assert resp.job_id  # render dispatched despite warm failure


# ---------- pure-HTTP warm helper unit tests -----------------------------


def test_warm_async_http_dispatches_to_provider_warmups():
    """warm_async_http MUST call the per-provider warmup functions for
    BOTH TTS and image providers configured on the channel."""
    from pipeline.cloud import warm as _warm

    tts_calls: list[str] = []
    img_calls: list[str] = []

    def _fake_tts_warmup(provider):
        tts_calls.append(provider)
        return None  # would normally return a Thread

    def _fake_img_warmup(provider):
        img_calls.append(provider)
        return None

    # mystoriesanimated channel YAML uses cloudrun_chatterbox + cloudrun_flux2_klein
    with patch.object(_warm, "_providers_for_channel",
                      return_value=({"chatterbox"}, {"flux"})), \
         patch("pipeline.tts.cloudrun.warmup", side_effect=_fake_tts_warmup), \
         patch("pipeline.images.images.warmup", side_effect=_fake_img_warmup):
        thread = _warm.warm_async_http("mystoriesanimated")
        thread.join(timeout=5)

    assert "cloudrun_chatterbox" in tts_calls, \
        f"TTS warmup not fired for chatterbox; got tts_calls={tts_calls}"
    assert "cloudrun_flux2_klein" in img_calls, \
        f"image warmup not fired for flux; got img_calls={img_calls}"


def test_warm_async_http_returns_thread_immediately():
    """The HTTP wrapper MUST return a daemon thread without blocking,
    so the caller (HTTP request handler) doesn't hang waiting for
    warmup probes that can take 30-90 s on cold-load."""
    from pipeline.cloud import warm as _warm
    import time

    # Make the inner warmups slow to verify the WRAPPER doesn't wait.
    def _slow_warmup(provider):
        time.sleep(0.5)
        return None

    with patch.object(_warm, "_providers_for_channel",
                      return_value=({"chatterbox"}, set())), \
         patch("pipeline.tts.cloudrun.warmup", side_effect=_slow_warmup):
        t0 = time.time()
        thread = _warm.warm_async_http("mystoriesanimated")
        elapsed = time.time() - t0
    assert elapsed < 0.1, \
        f"warm_async_http blocked the caller for {elapsed:.2f}s; should return immediately"
    assert thread.daemon, "warmup thread MUST be daemon (don't keep the process alive)"
