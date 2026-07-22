"""Payload builders — internal facts → public webhook payloads.

The one place internal shapes (ClickEvent, UrlV2Doc) are projected into
the payloads the registry documents. Privacy inherits structurally from
the sources (ClickEvent strips password material and sanitizes UTM);
this module additionally guarantees no raw IP and no password hashes
ever reach a payload.
"""

from __future__ import annotations

from typing import Any

from ua_parser import parse as ua_parse

from infrastructure.geoip import GeoIPService
from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.url import UrlV2Doc
from services.click.bot_detection import get_bot_name, is_bot_request
from services.click.events import ClickEvent
from services.click.handlers import classify_device
from services.events.contract import DomainEvent


def short_url_of(domain: str, alias: str) -> str:
    return f"https://{domain}/{alias}"


def link_snapshot(doc: UrlV2Doc) -> dict[str, Any]:
    """The public snapshot carried by every link.* lifecycle event."""
    return {
        "link_id": str(doc.id),
        "alias": doc.alias,
        "domain": doc.domain,
        "short_url": short_url_of(doc.domain, doc.alias),
        "long_url": doc.long_url,
        "status": doc.status.value,
        "password_protected": doc.password is not None,
        "block_bots": bool(doc.block_bots),
        "max_clicks": doc.max_clicks,
        "expires_at": doc.expire_after.isoformat() if doc.expire_after else None,
        "total_clicks": doc.total_clicks,
        "created_at": doc.created_at.isoformat(),
    }


def link_owner_id(doc: UrlV2Doc) -> str | None:
    """Anonymous links have no possible subscriber — producers skip emit."""
    if doc.owner_id == ANONYMOUS_OWNER_ID:
        return None
    return str(doc.owner_id)


def build_link_expired(doc: UrlV2Doc, reason: str) -> DomainEvent | None:
    """link.expired fires at DISCOVERY time: the max-clicks branch of the
    click handler, or the redirect path's lazy time-expiry flip. Both are
    once-per-link (atomic conditional updates gate the emit)."""
    owner = link_owner_id(doc)
    if owner is None:
        return None
    return DomainEvent(
        type="link.expired",
        owner_id=owner,
        data={"link": link_snapshot(doc), "reason": reason},
    )


def build_link_clicked(
    event: ClickEvent,
    system_default_domain: str,
    geoip: GeoIPService | None = None,
) -> DomainEvent | None:
    """Adapt a ClickEvent (events:clicks wire) to a link.clicked DomainEvent.

    Returns None for unowned links — subscriptions are per-owner, so
    anonymous/v1 clicks have no possible subscriber and skip the
    (comparatively expensive) UA parse entirely.
    """
    owner_id = event.url.owner_id
    if owner_id is None or owner_id == str(ANONYMOUS_OWNER_ID):
        return None

    ua = ua_parse(event.user_agent)
    browser = ua.user_agent.family if ua.user_agent else None
    os_family = ua.os.family if ua.os else None
    device = classify_device(ua, event.user_agent)
    bot = is_bot_request(event.user_agent)

    # Geo links carry the routing decision; everything else resolves the
    # same mmdb the stats consumer uses. IP itself never leaves this frame.
    country = event.resolved_country
    if country is None and geoip is not None:
        country = geoip.get_country_code(event.client_ip)

    domain = event.url.domain or system_default_domain
    return DomainEvent(
        type="link.clicked",
        owner_id=owner_id,
        occurred_at=event.enqueued_at,
        data={
            "link_id": event.url.id,
            "alias": event.short_code,
            "domain": domain,
            "short_url": short_url_of(domain, event.short_code),
            "clicked_at": event.enqueued_at.isoformat(),
            "country": country,
            "city": event.cf_city,
            "browser": browser,
            "os": os_family,
            "device": device,
            "referrer": event.referrer,
            "utm": {
                "source": event.utm_source,
                "medium": event.utm_medium,
                "campaign": event.utm_campaign,
            },
            "is_bot": bot,
            "bot_name": get_bot_name(event.user_agent) if bot else None,
            "total_clicks": event.url.total_clicks,
        },
    )
