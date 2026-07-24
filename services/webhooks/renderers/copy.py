"""Shared copy for the lossy flavors — one place decides wording and which
fields earn space, so Discord and Slack never drift apart.

Density is frequency-aware: ``link.clicked`` (high frequency) renders as a
few compact lines that read well as a stream; lifecycle events (rare)
render as label/value pairs. Event types this module does not know —
including every future addition to the registry — fall back to the payload
as a JSON code block, so adding an event can never terminal-fail a
flavored endpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

EVENT_LABELS = {
    "link.created": "Link created",
    "link.updated": "Link updated",
    "link.deleted": "Link deleted",
    "link.expired": "Link expired",
    "link.clicked": "Click",
    "webhook.test": "Test delivery",
}

_EXPIRY_REASONS = {
    "max_clicks_reached": "Max clicks reached",
    "time_expired": "Expiry time passed",
}

# Analytics sentinels are for querying, not for humans — a dimension that
# resolved to one renders as absent, same as None.
_SENTINELS = {"", "unknown", "(none)"}

_VALUE_MAX = 512  # per rendered value; receivers cap harder, this is copy-level
_CODE_MAX = 1_500  # fallback JSON block


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _present(value: Any) -> bool:
    if value is None:
        return False
    return not (isinstance(value, str) and value.strip().lower() in _SENTINELS)


def _fmt(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return clip(json.dumps(value, separators=(",", ":"), default=str), _VALUE_MAX)
    return clip(str(value), _VALUE_MAX)


@dataclass(frozen=True)
class Copy:
    """Flavor-neutral content. Exactly one of ``lines`` / ``pairs`` /
    ``changes`` / ``code`` is populated (compact, field grid, old/new
    change set, or fallback code block).

    ``changes`` stays structured — (field, old, new) — because flavors
    disagree on the best rendering: Discord has colored diff blocks,
    Slack only has plain pairs."""

    title: str
    title_url: str | None
    # ``context`` is the full footer line (brand + time + notice) for flavors
    # without a native timestamp slot; ``notice`` alone is for flavors that
    # render time themselves (Discord's embed timestamp).
    context: str
    notice: str | None = None
    lines: list[str] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    code: str | None = None


def _link_of(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type == "link.clicked":
        return payload
    link = payload.get("link")
    return link if isinstance(link, dict) else {}


def _title(event_type: str, link: dict[str, Any]) -> str:
    label = EVENT_LABELS.get(event_type, event_type)
    alias, domain = link.get("alias"), link.get("domain")
    if _present(alias) and _present(domain):
        return f"{label} · {domain}/{alias}"
    return label


def _notice(payload: dict[str, Any]) -> str | None:
    dropped = payload.get("dropped_since_last")
    if isinstance(dropped, int) and dropped > 0:
        plural = "delivery" if dropped == 1 else "deliveries"
        return f"{dropped} earlier {plural} dropped"
    return None


def _referrer_host(referrer: Any) -> str:
    text = str(referrer)
    host = urlparse(text).netloc
    return host or text


def _clicked_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    place = [
        str(v) for v in (payload.get("city"), payload.get("country")) if _present(v)
    ]
    agent = ""
    if _present(payload.get("browser")) and _present(payload.get("os")):
        agent = f"{payload['browser']} on {payload['os']}"
    elif _present(payload.get("browser")):
        agent = str(payload["browser"])
    first = " · ".join(part for part in (", ".join(place), agent) if part)
    if first:
        lines.append(first)

    second: list[str] = []
    if _present(payload.get("referrer")):
        second.append(f"from {_referrer_host(payload['referrer'])}")
    total = payload.get("total_clicks")
    if isinstance(total, int):
        second.append(f"click #{total:,}")
    if second:
        lines.append(" · ".join(second))

    if payload.get("is_bot"):
        bot = payload.get("bot_name")
        lines.append(f"bot · {bot}" if _present(bot) else "bot traffic")

    return [clip(line, _VALUE_MAX) for line in lines] or ["Tracked click recorded"]


def _updated_changes(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for name, change in changes.items():
        old = _fmt(change.get("old")) if isinstance(change, dict) else _fmt(None)
        new = _fmt(change.get("new")) if isinstance(change, dict) else _fmt(change)
        out.append((str(name), old, new))
    return out


def _lifecycle_pairs(event_type: str, payload: dict[str, Any]) -> list[tuple[str, str]]:
    link = _link_of(event_type, payload)
    pairs: list[tuple[str, str]] = []

    if event_type == "link.expired":
        reason = payload.get("reason")
        pairs.append(("Reason", _EXPIRY_REASONS.get(reason, _fmt(reason))))

    if _present(link.get("long_url")):
        pairs.append(("Destination", _fmt(link["long_url"])))
    if event_type == "link.created":
        if _present(link.get("expires_at")):
            pairs.append(("Expires", _fmt(link["expires_at"])))
        if _present(link.get("max_clicks")):
            pairs.append(("Max clicks", _fmt(link["max_clicks"])))
        if link.get("password_protected"):
            pairs.append(("Password protected", "yes"))
    if event_type in ("link.deleted", "link.expired") and isinstance(
        link.get("total_clicks"), int
    ):
        pairs.append(("Total clicks", f"{link['total_clicks']:,}"))

    return pairs


def build_copy(event_type: str, timestamp: str, payload: dict[str, Any]) -> Copy:
    link = _link_of(event_type, payload)
    title = _title(event_type, link)
    url = link.get("short_url") if _present(link.get("short_url")) else None
    notice = _notice(payload)
    context = f"spoo.me · {timestamp}" + (f" · {notice}" if notice else "")

    if event_type == "link.clicked":
        return Copy(title, url, context, notice, lines=_clicked_lines(payload))
    if event_type == "webhook.test":
        message = payload.get("message")
        lines = [clip(str(message), _VALUE_MAX)] if _present(message) else []
        return Copy(title, url, context, notice, lines=lines)
    if event_type == "link.updated":
        return Copy(title, url, context, notice, changes=_updated_changes(payload))
    if event_type in ("link.created", "link.deleted", "link.expired"):
        return Copy(
            title, url, context, notice, pairs=_lifecycle_pairs(event_type, payload)
        )

    shown = {k: v for k, v in payload.items() if k != "dropped_since_last"}
    code = clip(json.dumps(shown, separators=(",", ":"), default=str), _CODE_MAX)
    return Copy(title, url, context, notice, code=code)
