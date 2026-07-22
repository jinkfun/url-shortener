"""DeliveryExecutor — render-once, signing headers, retry ladder, disable
paths. post_public is patched; repos are AsyncMocks shaped per call."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from infrastructure.crypto import encrypt_secret
from infrastructure.safe_fetch import PostResult
from schemas.enums.webhook import (
    DeliveryStatus,
    EndpointDisabledReason,
    WebhookFlavor,
    WebhookStatus,
)
from schemas.models.webhook import (
    WebhookDeliveryDoc,
    WebhookEndpointDoc,
    WebhookEventDoc,
)
from services.webhooks.executor import (
    RETRY_SCHEDULE_SECONDS,
    SECRET_ENC_DOMAIN,
    DeliveryExecutor,
)
from services.webhooks.renderers import default_renderers
from services.webhooks.signing import (
    HEADER_ID,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    verify,
)

_MASTER = "test-master-secret"
_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


def _endpoint(**overrides: Any) -> WebhookEndpointDoc:
    doc = WebhookEndpointDoc(
        user_id=ObjectId(),
        url="https://example.com/hook",
        events=["*"],
        status=WebhookStatus.ACTIVE,
        flavor=WebhookFlavor.RAW,
        signing_secret_enc=encrypt_secret(_SECRET, _MASTER, domain=SECRET_ENC_DOMAIN),
        signing_secret_prefix=_SECRET[:14],
    )
    doc.id = ObjectId()
    return doc.model_copy(update=overrides)


def _delivery(**overrides: Any) -> WebhookDeliveryDoc:
    doc = WebhookDeliveryDoc(
        endpoint_id=ObjectId(),
        user_id=ObjectId(),
        event_oid=ObjectId(),
        event_type="link.clicked",
        webhook_id="msg_test",
        next_attempt_at=datetime.now(timezone.utc),
    )
    doc.id = ObjectId()
    return doc.model_copy(update=overrides)


def _event() -> WebhookEventDoc:
    doc = WebhookEventDoc(
        event_id="evt_test",
        type="link.clicked",
        owner_id=ObjectId(),
        occurred_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        payload={"alias": "a", "link_id": "x"},
    )
    doc.id = ObjectId()
    return doc


def _make(endpoint: WebhookEndpointDoc | None, *, max_consecutive: int = 10):
    deliveries = AsyncMock()
    endpoints = AsyncMock()
    endpoints.find_by_id.return_value = endpoint
    endpoints.record_exhausted.return_value = 1
    events = AsyncMock()
    events.find_by_oid.return_value = _event()
    executor = DeliveryExecutor(
        deliveries,
        endpoints,
        events,
        default_renderers(),
        master_secret=_MASTER,
        max_consecutive_failures=max_consecutive,
    )
    return executor, deliveries, endpoints, events


def _post(status: int | None, error: str | None = None):
    return AsyncMock(return_value=PostResult(status, error, None))


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_delivers_with_valid_standard_webhooks_headers(self):
        endpoint = _endpoint()
        executor, deliveries, endpoints, _ = _make(endpoint)
        post = _post(204)
        with patch("services.webhooks.executor.post_public", post):
            await executor.attempt(_delivery())

        url, body = post.await_args[0]
        headers = post.await_args.kwargs["headers"]
        assert url == endpoint.url
        assert headers[HEADER_ID] == "msg_test"
        # The signature verifies against the raw secret — the whole point.
        assert verify(
            headers[HEADER_ID],
            int(headers[HEADER_TIMESTAMP]),
            body,
            _SECRET,
            headers[HEADER_SIGNATURE],
        )
        deliveries.record_attempt_and_finish.assert_awaited_once()
        assert (
            deliveries.record_attempt_and_finish.await_args[0][2]
            is DeliveryStatus.SUCCESS
        )
        endpoints.record_success.assert_awaited_once_with(endpoint.id)

    @pytest.mark.asyncio
    async def test_renders_once_and_freezes_body(self):
        executor, deliveries, _, _events = _make(_endpoint())
        with patch("services.webhooks.executor.post_public", _post(204)):
            await executor.attempt(_delivery())
        deliveries.set_rendered_body.assert_awaited_once()
        body = deliveries.set_rendered_body.await_args[0][1]
        assert '"type":"link.clicked"' in body

    @pytest.mark.asyncio
    async def test_prerendered_body_skips_event_read(self):
        """Retries resend the frozen body — the event row is not re-read."""
        executor, _, _, events = _make(_endpoint())
        row = _delivery(rendered_body='{"type":"link.clicked","data":{}}')
        with patch("services.webhooks.executor.post_public", _post(204)):
            await executor.attempt(row)
        events.find_by_oid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dropped_since_last_rides_the_payload(self):
        executor, deliveries, _, _ = _make(_endpoint())
        with patch("services.webhooks.executor.post_public", _post(204)):
            await executor.attempt(_delivery(dropped_since_last=42))
        body = deliveries.set_rendered_body.await_args[0][1]
        assert '"dropped_since_last":42' in body


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_failure_reschedules_per_ladder(self):
        executor, deliveries, _, _ = _make(_endpoint())
        before = datetime.now(timezone.utc)
        with patch("services.webhooks.executor.post_public", _post(500)):
            await executor.attempt(_delivery(attempt_count=1))
        next_at = deliveries.record_attempt_and_reschedule.await_args[0][2]
        # attempt 2 of the ladder → RETRY_SCHEDULE_SECONDS[2] = 300s
        assert next_at >= before + timedelta(seconds=RETRY_SCHEDULE_SECONDS[2] - 1)

    @pytest.mark.asyncio
    async def test_exhaustion_marks_failed_and_counts_streak(self):
        endpoint = _endpoint()
        executor, deliveries, endpoints, _ = _make(endpoint)
        last = len(RETRY_SCHEDULE_SECONDS) - 1
        with patch("services.webhooks.executor.post_public", _post(500)):
            await executor.attempt(_delivery(attempt_count=last))
        assert (
            deliveries.record_attempt_and_finish.await_args[0][2]
            is DeliveryStatus.FAILED
        )
        endpoints.record_exhausted.assert_awaited_once()
        endpoints.disable.assert_not_awaited()  # streak=1 < 10

    @pytest.mark.asyncio
    async def test_streak_at_threshold_disables(self):
        endpoint = _endpoint()
        executor, _, endpoints, _ = _make(endpoint, max_consecutive=3)
        endpoints.record_exhausted.return_value = 3
        last = len(RETRY_SCHEDULE_SECONDS) - 1
        with patch("services.webhooks.executor.post_public", _post(None, "boom")):
            await executor.attempt(_delivery(attempt_count=last))
        endpoints.disable.assert_awaited_once_with(
            endpoint.id, EndpointDisabledReason.CONSECUTIVE_FAILURES
        )

    @pytest.mark.asyncio
    async def test_410_disables_immediately(self):
        endpoint = _endpoint()
        executor, deliveries, endpoints, _ = _make(endpoint)
        with patch("services.webhooks.executor.post_public", _post(410)):
            await executor.attempt(_delivery())
        endpoints.disable.assert_awaited_once_with(
            endpoint.id, EndpointDisabledReason.GONE
        )
        assert (
            deliveries.record_attempt_and_finish.await_args[0][2]
            is DeliveryStatus.FAILED
        )

    @pytest.mark.asyncio
    async def test_inactive_endpoint_terminal(self):
        executor, deliveries, _, _ = _make(_endpoint(status=WebhookStatus.PAUSED))
        with patch("services.webhooks.executor.post_public", _post(204)) as post:
            await executor.attempt(_delivery())
        deliveries.mark_failed.assert_awaited_once()
        post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_event_ttl_race_terminal_not_crash(self):
        executor, deliveries, _, events = _make(_endpoint())
        events.find_by_oid.return_value = None
        with patch("services.webhooks.executor.post_public", _post(204)) as post:
            await executor.attempt(_delivery())
        deliveries.mark_failed.assert_awaited_once()
        assert deliveries.mark_failed.await_args[0][1] == "event_expired"
        post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_after_grace_secret_sends_dual_signatures(self):
        old_secret = "whsec_b2xkLXNlY3JldC1vbGQtc2VjcmV0"
        endpoint = _endpoint(
            previous_secret_enc=encrypt_secret(
                old_secret, _MASTER, domain=SECRET_ENC_DOMAIN
            ),
            previous_secret_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        executor, _, _, _ = _make(endpoint)
        post = _post(204)
        with patch("services.webhooks.executor.post_public", post):
            await executor.attempt(_delivery())
        headers = post.await_args.kwargs["headers"]
        _, body = post.await_args[0]
        ts = int(headers[HEADER_TIMESTAMP])
        assert verify("msg_test", ts, body, _SECRET, headers[HEADER_SIGNATURE])
        assert verify("msg_test", ts, body, old_secret, headers[HEADER_SIGNATURE])
