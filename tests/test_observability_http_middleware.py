"""Audit Q2.11 — ``_CARRY_KEYS`` must include every dashboard-filterable
identity attr so the FastAPI middleware can promote them onto HTTP
server spans.

Pre-fix the set was ``("channel", "slug", "job_id", "niche", "account")``
only; the dashboard slices by ``render_kind``, ``render_mode``,
``run_id``, and ``user`` returned empty for HTTP spans because the
attribute was never set on them, even when a non-HTTP child span in
the same trace carried the value.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from pipeline.observability.http_middleware import (
    _CARRY_KEYS,
    attach_identity_attrs,
)


class TestCarryKeysCoverage(unittest.TestCase):
    def test_carry_keys_includes_all_dashboard_filterable_attrs(self):
        for k in (
            "channel", "slug", "job_id", "niche", "account",
            # Audit Q2.11 — the four newly-promoted keys.
            "render_kind", "render_mode", "run_id", "user",
        ):
            with self.subTest(key=k):
                self.assertIn(k, _CARRY_KEYS)


class TestAttachIdentityAttrsTwoPasses(unittest.IsolatedAsyncioTestCase):
    """Audit Q2.12 — pre-fix this attached attrs ONLY after
    call_next returned. On streaming/SSE/file responses, the OTel
    auto-instrumentor often calls ``end()`` on the server span
    BEFORE the response object propagates back through the
    middleware chain — span.set_attribute is then a silent no-op.
    Now run TWO passes: query-param attrs pre-handler (span
    guaranteed open), path-param attrs post-handler (path_params
    populated).
    """

    async def test_query_params_attached_pre_call_next(self):
        """Query-param identity attrs must be on the span BEFORE
        call_next runs."""
        attached: list[tuple[str, str]] = []

        class _Span:
            def is_recording(self_inner): return True
            def set_attribute(self_inner, k, v): attached.append((k, v))

        seen_attached_at_call_next: list[tuple[str, str]] = []

        async def _fake_call_next(_request):
            # Snapshot what's already attached at this moment.
            seen_attached_at_call_next.extend(attached)
            return MagicMock()

        request = MagicMock()
        request.query_params = {"channel": "rhymetimejunction", "job_id": "abc"}
        request.path_params = {}

        with patch("pipeline.observability.http_middleware._trace.get_current_span",
                   return_value=_Span()):
            await attach_identity_attrs(request, _fake_call_next)

        keys_pre = {k for k, _ in seen_attached_at_call_next}
        self.assertIn("ytfactory.channel", keys_pre)
        self.assertIn("ytfactory.job_id", keys_pre)

    async def test_path_params_attached_post_call_next(self):
        attached: list[tuple[str, str]] = []

        class _Span:
            def is_recording(self_inner): return True
            def set_attribute(self_inner, k, v): attached.append((k, v))
            _attributes: dict = {}

        async def _fake_call_next(_request):
            return MagicMock()

        request = MagicMock()
        request.query_params = {}
        request.path_params = {"channel": "historyrecapped", "slug": "s1"}

        with patch("pipeline.observability.http_middleware._trace.get_current_span",
                   return_value=_Span()):
            await attach_identity_attrs(request, _fake_call_next)

        keys = {k for k, _ in attached}
        self.assertIn("ytfactory.channel", keys)
        self.assertIn("ytfactory.slug", keys)

    async def test_post_handler_skipped_when_span_already_ended(self):
        """If the span ended during call_next (streaming case),
        the post-handler set_attribute calls must be NO-OPs (not
        crash, not warn). is_recording() returning False is the
        signal."""
        ended_span = MagicMock()
        ended_span.is_recording.return_value = False

        attached_pre: list = []

        class _OpenSpan:
            def is_recording(self_inner): return True
            def set_attribute(self_inner, k, v): attached_pre.append((k, v))

        # First call (pre-handler) returns OpenSpan; second
        # (post-handler) returns ended_span.
        spans = [_OpenSpan(), ended_span]
        async def _fake_call_next(_request):
            return MagicMock()

        request = MagicMock()
        request.query_params = {"channel": "x"}
        request.path_params = {"slug": "s1"}

        with patch("pipeline.observability.http_middleware._trace.get_current_span",
                   side_effect=lambda: spans.pop(0)):
            await attach_identity_attrs(request, _fake_call_next)

        # Pre-handler attached query-param attrs.
        self.assertEqual([k for k, _ in attached_pre], ["ytfactory.channel"])
        # Post-handler did NOT attempt set_attribute on the ended span.
        ended_span.set_attribute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
