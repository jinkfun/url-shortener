"""Discord flavor — one embed per event, ready for a Discord webhook URL.

Receiver limits enforced locally (title 256, description 4096, field
name 256 / value 1024, footer 2048, 6000 chars across the embed) so a
rendered body is never rejected for size by Discord itself; the engine's
max_payload_bytes cap still applies on top.
"""

from __future__ import annotations

import json
from typing import Any

from services.webhooks.renderers.copy import build_copy, clip

_COLORS = {
    "link.created": 0x43B581,
    "link.updated": 0x5865F2,
    "link.deleted": 0xF04747,
    "link.expired": 0xE67E22,
    "link.clicked": 0x99AAB5,
    "webhook.test": 0x99AAB5,
}
_DEFAULT_COLOR = 0x99AAB5

AVATAR_URL = "https://spoo.me/static/images/favicon.png"

_TITLE_MAX = 256
_DESCRIPTION_MAX = 4096
_FIELD_NAME_MAX = 256
_FIELD_VALUE_MAX = 1024
_FOOTER_MAX = 2048
_EMBED_TOTAL_MAX = 6000


def _fence(text: str) -> str:
    # A value containing ``` would break out of the block.
    return text.replace("```", "'''")


def _embed_size(embed: dict[str, Any]) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        total += len(field["name"]) + len(field["value"])
    return total


class DiscordRenderer:
    flavor = "discord"

    def render(
        self, event_id: str, event_type: str, timestamp: str, payload: dict[str, Any]
    ) -> str:
        copy = build_copy(event_type, timestamp, payload)

        # The webhook's own username and avatar carry the brand, and the
        # embed timestamp renders natively in the viewer's timezone — the
        # footer exists only for the drop notice.
        embed: dict[str, Any] = {
            "title": clip(copy.title, _TITLE_MAX),
            "color": _COLORS.get(event_type, _DEFAULT_COLOR),
            "timestamp": timestamp,
        }
        if copy.notice:
            embed["footer"] = {"text": clip(copy.notice, _FOOTER_MAX)}
        if copy.title_url:
            embed["url"] = copy.title_url

        if copy.changes:
            # Discord colors diff blocks: removed lines red, added green —
            # the old/new pair reads at a glance.
            diff_lines: list[str] = []
            for name, old, new in copy.changes:
                diff_lines.append(f"- {name}: {_fence(old)}")
                diff_lines.append(f"+ {name}: {_fence(new)}")
            embed["description"] = clip(
                "```diff\n" + "\n".join(diff_lines) + "\n```", _DESCRIPTION_MAX
            )
        elif copy.code:
            embed["description"] = clip(
                "```json\n" + _fence(copy.code) + "\n```", _DESCRIPTION_MAX
            )
        elif copy.lines:
            embed["description"] = clip("\n".join(copy.lines), _DESCRIPTION_MAX)

        if copy.pairs:
            # Two facts per row, never Discord's cramped three: a blank
            # spacer field closes each pair so the grid stays airy. Long
            # values like destination URLs take the full width up top.
            fields: list[dict[str, Any]] = []
            grid: list[tuple[str, str]] = []
            for name, value in copy.pairs:
                if name == "Destination" or len(value) > 32:
                    fields.append(
                        {
                            "name": clip(name, _FIELD_NAME_MAX),
                            "value": clip(value, _FIELD_VALUE_MAX),
                            "inline": False,
                        }
                    )
                else:
                    grid.append((name, value))
            for i, (name, value) in enumerate(grid):
                fields.append(
                    {
                        "name": clip(name, _FIELD_NAME_MAX),
                        "value": clip(value, _FIELD_VALUE_MAX),
                        "inline": True,
                    }
                )
                if i % 2 == 1 and i < len(grid) - 1:
                    fields.append({"name": "\u200b", "value": "\u200b", "inline": True})
            embed["fields"] = fields

        while _embed_size(embed) > _EMBED_TOTAL_MAX and embed.get("fields"):
            embed["fields"].pop()
        if _embed_size(embed) > _EMBED_TOTAL_MAX and "description" in embed:
            overflow = _embed_size(embed) - _EMBED_TOTAL_MAX
            embed["description"] = clip(
                embed["description"], max(1, len(embed["description"]) - overflow)
            )

        return json.dumps(
            {"username": "spoo.me", "avatar_url": AVATAR_URL, "embeds": [embed]},
            separators=(",", ":"),
            default=str,
        )
