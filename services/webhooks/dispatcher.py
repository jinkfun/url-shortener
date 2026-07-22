"""WebhookDispatcher — fact → durable delivery intent. No HTTP, no rendering.

Runs on the consumer ack path (stream mode) or at emit time (inline
mode), so its cost must be bounded and payload-size-independent: one
matcher pass, one event insert, N thin delivery rows (D14/D15). The
caller acks the stream message AFTER dispatch returns — delivery intent
recorded durably is the ack condition, not delivery success (TRD §3).
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from infrastructure.logging import get_logger
from repositories.webhook_delivery_repository import WebhookDeliveryRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from repositories.webhook_event_repository import WebhookEventRepository
from schemas.enums.webhook import DeliveryStatus
from schemas.models.webhook import WebhookEndpointDoc
from services.events.contract import DomainEvent
from services.webhooks.matcher import SubscriptionMatcher
from services.webhooks.signing import new_webhook_id

log = get_logger(__name__)


def make_delivery_row(
    event_oid: ObjectId, event: DomainEvent, endpoint: WebhookEndpointDoc
) -> dict:
    return {
        "endpoint_id": endpoint.id,
        "user_id": endpoint.user_id,
        "event_oid": event_oid,
        "event_type": event.type,
        "webhook_id": new_webhook_id(),
        "is_test": False,
        "rendered_body": None,
        "dropped_since_last": 0,
        "status": DeliveryStatus.PENDING.value,
        "attempts": [],
        "attempt_count": 0,
        "next_attempt_at": datetime.now(timezone.utc),
        "claimed_until": None,
        "created_at": datetime.now(timezone.utc),
        "completed_at": None,
    }


class WebhookDispatcher:
    def __init__(
        self,
        matcher: SubscriptionMatcher,
        event_repo: WebhookEventRepository,
        delivery_repo: WebhookDeliveryRepository,
        endpoint_repo: WebhookEndpointRepository,
        *,
        max_pending_per_endpoint: int = 1000,
    ) -> None:
        self._matcher = matcher
        self._event_repo = event_repo
        self._delivery_repo = delivery_repo
        self._endpoint_repo = endpoint_repo
        self._max_pending = max_pending_per_endpoint

    async def dispatch(self, event: DomainEvent) -> None:
        endpoints = await self._matcher.match(event)
        if not endpoints:
            return  # common case: cache hit, zero further Mongo work

        event_oid = await self._event_repo.insert_event(event)
        rows: list[dict] = []
        for endpoint in endpoints:
            # D13: the pending cap protects the queue itself. A subscriber
            # who can't drink max_pending deliveries has already lost the
            # facts — counting beats pretending.
            if (
                await self._delivery_repo.count_pending(endpoint.id)
                >= self._max_pending
            ):
                await self._endpoint_repo.increment_dropped(endpoint.id)
                log.warning(
                    "webhook_delivery_dropped_over_cap",
                    endpoint_id=str(endpoint.id),
                    event_type=event.type,
                )
                continue
            rows.append(make_delivery_row(event_oid, event, endpoint))

        await self._delivery_repo.insert_many_rows(rows)
        log.info(
            "webhook_dispatched",
            event_type=event.type,
            event_id=event.event_id,
            endpoints=len(rows),
        )
