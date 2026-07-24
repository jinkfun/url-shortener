"""Discord and Slack flavors — full-shape pins per event type, receiver
size limits, escaping, and the fallback that keeps future event types from
terminal-failing flavored endpoints. Raw stays pinned via the executor
tests; these cover the lossy renderings."""

from __future__ import annotations

import json
from typing import Any

from schemas.enums.webhook import WebhookFlavor
from services.webhooks.registry import EVENT_REGISTRY, TEST_EVENT_SPEC
from services.webhooks.renderers import default_renderers
from services.webhooks.renderers.discord import _EMBED_TOTAL_MAX, DiscordRenderer
from services.webhooks.renderers.slack import SlackRenderer

_TS = "2026-07-24T14:32:00+00:00"


def _discord(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = DiscordRenderer().render("evt_x", event_type, _TS, payload)
    return json.loads(body)


def _slack(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = SlackRenderer().render("evt_x", event_type, _TS, payload)
    return json.loads(body)


def _clicked_payload(**overrides: Any) -> dict[str, Any]:
    payload = EVENT_REGISTRY["link.clicked"].sample()
    payload.update(overrides)
    return payload


class TestRegistry:
    def test_every_flavor_has_a_renderer(self):
        # Enum and registry must never drift: a flavor an endpoint can be
        # created with must be renderable, or deliveries terminal-fail.
        assert set(default_renderers()) == {f.value for f in WebhookFlavor}

    def test_renderers_expose_matching_flavor_attribute(self):
        for key, renderer in default_renderers().items():
            assert renderer.flavor == key


class TestDiscord:
    def test_clicked_is_compact_description(self):
        doc = _discord("link.clicked", _clicked_payload())
        assert doc["username"] == "spoo.me"
        (embed,) = doc["embeds"]
        assert embed["title"] == "Click · spoo.me/summer-drop"
        assert embed["url"] == "https://spoo.me/summer-drop"
        assert embed["timestamp"] == _TS
        assert "Mumbai, IN · Chrome on Android" in embed["description"]
        assert "from x.com · click #4,102" in embed["description"]
        assert "fields" not in embed
        # Discord renders the embed timestamp itself; footer is brand only.
        assert embed["footer"]["text"] == "spoo.me"

    def test_clicked_omits_absent_dimensions(self):
        doc = _discord(
            "link.clicked",
            _clicked_payload(
                city=None, country="unknown", referrer="(none)", browser=None, os=None
            ),
        )
        description = doc["embeds"][0]["description"]
        assert "unknown" not in description
        assert "none" not in description
        assert "from" not in description

    def test_clicked_bot_line(self):
        doc = _discord(
            "link.clicked", _clicked_payload(is_bot=True, bot_name="GoogleBot")
        )
        assert "bot · GoogleBot" in doc["embeds"][0]["description"]

    def test_dropped_notice_rides_the_footer(self):
        doc = _discord("link.clicked", _clicked_payload(dropped_since_last=3))
        assert doc["embeds"][0]["footer"]["text"] == (
            "spoo.me · 3 earlier deliveries dropped"
        )

    def test_updated_renders_a_diff_block(self):
        payload = EVENT_REGISTRY["link.updated"].sample()
        payload["changes"]["max_clicks"] = {"old": 5000, "new": 10000}
        description = _discord("link.updated", payload)["embeds"][0]["description"]
        assert description.startswith("```diff\n")
        assert description.endswith("\n```")
        assert "- long_url: https://example.com/old" in description
        assert "+ long_url: https://example.com/campaign" in description
        assert "- max_clicks: 5000" in description
        assert "+ max_clicks: 10000" in description

    def test_diff_values_cannot_break_out_of_the_block(self):
        payload = {
            "link": {"alias": "a", "domain": "spoo.me"},
            "changes": {"long_url": {"old": "x", "new": "```@everyone"}},
        }
        description = _discord("link.updated", payload)["embeds"][0]["description"]
        assert description.count("```") == 2  # only the fence itself

    def test_lifecycle_field_grids(self):
        (embed,) = _discord("link.expired", EVENT_REGISTRY["link.expired"].sample())[
            "embeds"
        ]
        names = [f["name"] for f in embed["fields"]]
        assert names == ["Reason", "Destination", "Total clicks"]
        assert embed["fields"][0]["value"] == "Max clicks reached"

        (embed,) = _discord("link.deleted", EVENT_REGISTRY["link.deleted"].sample())[
            "embeds"
        ]
        assert [f["name"] for f in embed["fields"]] == ["Destination", "Total clicks"]

    def test_test_event_renders_its_message(self):
        (embed,) = _discord("webhook.test", TEST_EVENT_SPEC.sample())["embeds"]
        assert embed["description"] == "If you can read this, your endpoint works."

    def test_unknown_event_type_falls_back_to_json(self):
        # A future registry addition must degrade, never terminal-fail.
        (embed,) = _discord("link.blocked", {"link_id": "x", "reason": "phishing"})[
            "embeds"
        ]
        assert embed["title"] == "link.blocked"
        assert embed["description"].startswith("```json\n")
        assert '"reason":"phishing"' in embed["description"]

    def test_receiver_limits_hold_under_pathological_changes(self):
        payload = {
            "link": {"alias": "a" * 500, "domain": "spoo.me"},
            "changes": {
                f"field_{i}": {"old": "x" * 900, "new": "y" * 900} for i in range(10)
            },
        }
        (embed,) = _discord("link.updated", payload)["embeds"]
        assert len(embed["title"]) <= 256
        assert len(embed["description"]) <= 4096
        total = (
            len(embed["title"])
            + len(embed["description"])
            + len(embed["footer"]["text"])
        )
        assert total <= _EMBED_TOTAL_MAX


class TestSlack:
    def test_clicked_is_one_section_plus_context(self):
        doc = _slack("link.clicked", _clicked_payload())
        assert doc["text"] == "Click · spoo.me/summer-drop"
        first, last = doc["blocks"][0], doc["blocks"][-1]
        assert first["type"] == "section"
        assert first["text"]["text"].startswith(
            "*<https://spoo.me/summer-drop|Click · spoo.me/summer-drop>*"
        )
        assert "Mumbai, IN · Chrome on Android" in first["text"]["text"]
        assert last["type"] == "context"
        assert last["elements"][0]["text"] == f"spoo.me · {_TS}"

    def test_updated_renders_old_to_new_pairs(self):
        doc = _slack("link.updated", EVENT_REGISTRY["link.updated"].sample())
        fields = doc["blocks"][1]["fields"]
        assert fields[0]["text"] == (
            "*long_url*\nhttps://example.com/old → https://example.com/campaign"
        )

    def test_field_sections_chunk_at_ten(self):
        payload = {
            "link": {"alias": "a", "domain": "spoo.me"},
            "changes": {f"f{i}": {"old": i, "new": i + 1} for i in range(12)},
        }
        doc = _slack("link.updated", payload)
        field_sections = [b for b in doc["blocks"] if "fields" in b]
        assert [len(s["fields"]) for s in field_sections] == [10, 2]

    def test_mrkdwn_control_characters_escaped(self):
        payload = _clicked_payload(city="<Berlin & Brandenburg>")
        text = _slack("link.clicked", payload)["blocks"][0]["text"]["text"]
        assert "&lt;Berlin &amp; Brandenburg&gt;" in text
        assert "<Berlin" not in text

    def test_dropped_notice_rides_the_context_line(self):
        doc = _slack("link.clicked", _clicked_payload(dropped_since_last=1))
        assert doc["blocks"][-1]["elements"][0]["text"] == (
            f"spoo.me · {_TS} · 1 earlier delivery dropped"
        )

    def test_unknown_event_type_falls_back_to_json(self):
        doc = _slack("link.blocked", {"link_id": "x", "reason": "phishing"})
        assert doc["text"] == "link.blocked"
        code = doc["blocks"][1]["text"]["text"]
        assert code.startswith("```")
        assert "phishing" in code

    def test_output_stays_under_the_engine_payload_cap(self):
        payload = {
            "link": {"alias": "a" * 500, "domain": "spoo.me"},
            "changes": {
                f"field_{i}": {"old": "x" * 900, "new": "y" * 900} for i in range(10)
            },
        }
        body = SlackRenderer().render("evt_x", "link.updated", _TS, payload)
        assert len(body.encode()) < 20_480
        assert len(json.loads(body)["blocks"]) <= 50


class TestAllEventsBothFlavors:
    def test_every_registered_sample_renders_valid_json(self):
        renderers = default_renderers()
        specs = [*EVENT_REGISTRY.values(), TEST_EVENT_SPEC]
        for spec in specs:
            for flavor in ("discord", "slack"):
                body = renderers[flavor].render("evt_x", spec.name, _TS, spec.sample())
                parsed = json.loads(body)
                assert parsed  # non-empty, valid JSON
                assert len(body.encode()) < 20_480
