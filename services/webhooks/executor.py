"""DeliveryExecutor — the Mongo claim loop that actually delivers.

Mongo-only by design: claim → render (first attempt only) →
sign → POST → record. No Redis anywhere in the retry path, which is what
lets the same class run in the click worker (prod) or embedded in the
app lifespan (self-host rungs). Atomic claims with a lease make N
concurrent executors safe; the claim query is stateless over
``next_attempt_at``, so restarts self-heal on the first loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from infrastructure.crypto import decrypt_secret
from infrastructure.logging import get_logger
from infrastructure.safe_fetch import post_public
from repositories.webhook_delivery_repository import WebhookDeliveryRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from repositories.webhook_event_repository import WebhookEventRepository
from schemas.enums.webhook import DeliveryStatus, EndpointDisabledReason, WebhookStatus
from schemas.models.webhook import (
    DeliveryAttempt,
    WebhookDeliveryDoc,
    WebhookEndpointDoc,
)
from services.webhooks.renderers import Renderer
from services.webhooks.signing import (
    HEADER_ID,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    sign,
)

log = get_logger(__name__)

SECRET_ENC_DOMAIN = "webhook-signing-secret-v1"
USER_AGENT = "spoo.me-webhooks/1.0 (+https://spoo.me)"

# Standard Webhooks retry convention: immediate, 5s, 5m, 30m, 2h, 5h, 10h.
RETRY_SCHEDULE_SECONDS = (0, 5, 300, 1800, 7200, 18000, 36000)

# Type of the on-disable hook — wiring plugs email notification in here so
# the executor never grows an email dependency.
OnDisabled = Callable[[WebhookEndpointDoc, str], Awaitable[None]]


class DeliveryExecutor:
    def __init__(
        self,
        delivery_repo: WebhookDeliveryRepository,
        endpoint_repo: WebhookEndpointRepository,
        event_repo: WebhookEventRepository,
        renderers: dict[str, Renderer],
        *,
        master_secret: str,
        delivery_timeout: float = 15.0,
        max_payload_bytes: int = 20_480,
        max_consecutive_failures: int = 10,
        poll_interval: float = 1.0,
        lease_seconds: int = 60,
        on_disabled: OnDisabled | None = None,
    ) -> None:
        self._deliveries = delivery_repo
        self._endpoints = endpoint_repo
        self._events = event_repo
        self._renderers = renderers
        self._master_secret = master_secret
        self._timeout = delivery_timeout
        self._max_bytes = max_payload_bytes
        self._max_consecutive = max_consecutive_failures
        self._poll_interval = poll_interval
        self._lease = lease_seconds
        self._on_disabled = on_disabled

    # ── Loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Long-lived task; cancellation is the shutdown path."""
        log.info("webhook_executor_started", poll_interval=self._poll_interval)
        while True:
            try:
                row = await self._deliveries.claim_due(lease_seconds=self._lease)
                if row is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                await self.attempt(row)
            except asyncio.CancelledError:
                log.info("webhook_executor_stopped")
                raise
            except Exception as exc:
                # One bad row must not kill the loop.
                log.error(
                    "webhook_executor_tick_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(self._poll_interval)

    # ── One attempt ──────────────────────────────────────────────────────

    async def attempt(self, row: WebhookDeliveryDoc) -> None:
        endpoint = await self._endpoints.find_by_id(row.endpoint_id)
        if endpoint is None or endpoint.status != WebhookStatus.ACTIVE:
            await self._deliveries.mark_failed(row.id, "endpoint_inactive")
            return

        body = row.rendered_body
        if body is None:
            body = await self._render(row, endpoint)
            if body is None:
                return  # _render already recorded the terminal failure

        started = time.monotonic()
        result = await post_public(
            endpoint.url,
            body,
            headers=self._headers(row.webhook_id, body, endpoint),
            timeout=self._timeout,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        attempt = DeliveryAttempt(
            attempted_at=datetime.now(timezone.utc),
            status_code=result.status_code,
            duration_ms=duration_ms,
            error=result.error,
            response_body=result.body_snippet,
        )

        ok = result.status_code is not None and 200 <= result.status_code < 300
        if ok:
            await self._deliveries.record_attempt_and_finish(
                row.id, attempt, DeliveryStatus.SUCCESS
            )
            await self._endpoints.record_success(endpoint.id)
            log.info(
                "webhook_delivered",
                endpoint_id=str(endpoint.id),
                event_type=row.event_type,
                webhook_id=row.webhook_id,
                status_code=result.status_code,
                duration_ms=duration_ms,
                attempt=row.attempt_count + 1,
                is_test=row.is_test,
            )
            return
        log.warning(
            "webhook_delivery_attempt_failed",
            endpoint_id=str(endpoint.id),
            event_type=row.event_type,
            webhook_id=row.webhook_id,
            status_code=result.status_code,
            error=result.error,
            duration_ms=duration_ms,
            attempt=row.attempt_count + 1,
        )

        if result.status_code == 410:
            await self._deliveries.record_attempt_and_finish(
                row.id, attempt, DeliveryStatus.FAILED
            )
            await self._disable(endpoint, EndpointDisabledReason.GONE)
            return

        # attempt_count on the claimed row predates this attempt.
        attempts_done = row.attempt_count + 1
        if attempts_done >= len(RETRY_SCHEDULE_SECONDS):
            await self._deliveries.record_attempt_and_finish(
                row.id, attempt, DeliveryStatus.FAILED
            )
            reason = result.error or f"status {result.status_code}"
            streak = await self._endpoints.record_exhausted(endpoint.id, reason)
            if streak >= self._max_consecutive:
                await self._disable(
                    endpoint, EndpointDisabledReason.CONSECUTIVE_FAILURES
                )
            return

        delay = RETRY_SCHEDULE_SECONDS[attempts_done]
        await self._deliveries.record_attempt_and_reschedule(
            row.id,
            attempt,
            datetime.now(timezone.utc) + timedelta(seconds=delay),
        )

    # ── Internals ────────────────────────────────────────────────────────

    async def _render(
        self, row: WebhookDeliveryDoc, endpoint: WebhookEndpointDoc
    ) -> str | None:
        event = await self._events.find_by_oid(row.event_oid)
        if event is None:
            # TTL race at the 30-day edge — terminal, never a crash.
            await self._deliveries.mark_failed(row.id, "event_expired")
            return None
        renderer = self._renderers.get(endpoint.flavor.value)
        if renderer is None:
            await self._deliveries.mark_failed(
                row.id, f"unknown_flavor:{endpoint.flavor}"
            )
            return None
        payload = dict(event.payload)
        if row.dropped_since_last:
            payload["dropped_since_last"] = row.dropped_since_last
        body = renderer.render(event.type, event.occurred_at.isoformat(), payload)
        if len(body.encode()) > self._max_bytes:
            await self._deliveries.mark_failed(row.id, "payload_over_cap")
            return None
        await self._deliveries.set_rendered_body(row.id, body)
        return body

    def _headers(
        self, webhook_id: str, body: str, endpoint: WebhookEndpointDoc
    ) -> dict[str, str]:
        """Fresh timestamp per attempt (replay protection); dual signatures
        during a rotation grace window, space-delimited per the spec."""
        ts = int(time.time())
        secret = decrypt_secret(
            endpoint.signing_secret_enc, self._master_secret, domain=SECRET_ENC_DOMAIN
        )
        signatures = [sign(webhook_id, ts, body, secret)]
        if endpoint.previous_secret_enc and (
            endpoint.previous_secret_expires_at
            and endpoint.previous_secret_expires_at > datetime.now(timezone.utc)
        ):
            previous = decrypt_secret(
                endpoint.previous_secret_enc,
                self._master_secret,
                domain=SECRET_ENC_DOMAIN,
            )
            signatures.append(sign(webhook_id, ts, body, previous))
        return {
            HEADER_ID: webhook_id,
            HEADER_TIMESTAMP: str(ts),
            HEADER_SIGNATURE: " ".join(signatures),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    async def _disable(
        self, endpoint: WebhookEndpointDoc, reason: EndpointDisabledReason
    ) -> None:
        await self._endpoints.disable(endpoint.id, reason)
        log.warning(
            "webhook_endpoint_disabled",
            endpoint_id=str(endpoint.id),
            reason=reason.value,
        )
        if self._on_disabled is not None:
            try:
                await self._on_disabled(endpoint, reason.value)
            except Exception as exc:
                log.error("webhook_disabled_notify_failed", error=str(exc))
