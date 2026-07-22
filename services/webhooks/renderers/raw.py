"""Raw flavor — the Standard Webhooks envelope, THE versioned contract."""

from __future__ import annotations

import json
from typing import Any


class RawRenderer:
    flavor = "raw"

    def render(self, event_type: str, timestamp: str, payload: dict[str, Any]) -> str:
        # Compact separators: the rendered body is stored per delivery row
        # and capped at max_payload_bytes — no cosmetic whitespace.
        return json.dumps(
            {"type": event_type, "timestamp": timestamp, "data": payload},
            separators=(",", ":"),
            default=str,
        )
