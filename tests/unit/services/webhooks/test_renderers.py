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
    def test_clicked_renders_a_two_column_grid(self):
        doc = _discord("link.clicked", _clicked_payload())
        assert doc["username"] == "spoo.me"
        assert doc["avatar_url"].startswith("https://spoo.me/")
        (embed,) = doc["embeds"]
        assert embed["title"] == "Click: spoo.me/summer-drop"
        assert embed["url"] == "https://spoo.me/summer-drop"
        assert embed["timestamp"] == _TS
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Country"] == "🇮🇳 IN"
        assert fields["City"] == "Mumbai"
        assert fields["Browser"] == "Chrome"
        assert fields["OS"] == "Android"
        assert fields["Device"] == "mobile"
        assert fields["From"] == "x.com"
        assert fields["UTM"] == "newsletter / email"
        assert fields["Clicks so far"] == "4,102"
        assert all(f["inline"] for f in embed["fields"])
        # Spacer fields close each pair so rows hold two facts, not three.
        assert "\u200b" in fields
        # The webhook identity carries the brand; no footer without a notice.
        assert "footer" not in embed
        assert "·" not in json.dumps(embed, ensure_ascii=False)

    def test_sparse_click_is_never_an_empty_card(self):
        # A local/direct click has no geo and no referrer; the truth still
        # renders: a direct visit, count stated even at zero.
        doc = _discord(
            "link.clicked",
            _clicked_payload(
                city=None,
                country="unknown",
                referrer="(none)",
                utm=None,
                total_clicks=0,
            ),
        )
        fields = {f["name"]: f["value"] for f in doc["embeds"][0]["fields"]}
        assert "Country" not in fields
        assert "City" not in fields
        assert fields["From"] == "direct"
        assert fields["Browser"] == "Chrome"
        assert fields["Clicks so far"] == "0"

    def test_clicked_bot_field(self):
        doc = _discord(
            "link.clicked", _clicked_payload(is_bot=True, bot_name="GoogleBot")
        )
        fields = {f["name"]: f["value"] for f in doc["embeds"][0]["fields"]}
        assert fields["Bot"] == "GoogleBot"

    def test_dropped_notice_rides_the_footer(self):
        doc = _discord("link.clicked", _clicked_payload(dropped_since_last=3))
        assert doc["embeds"][0]["footer"]["text"] == "3 earlier deliveries dropped"

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
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Reason"] == "Max clicks reached"
        assert "Destination" in fields
        assert "Lifetime clicks" in fields

        (embed,) = _discord("link.deleted", EVENT_REGISTRY["link.deleted"].sample())[
            "embeds"
        ]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Lifetime clicks"] == "4,102"
        assert "days" in fields["Age"]

    def test_bare_created_link_still_fills_the_card(self):
        # Every configuration fact renders with its real answer, absent
        # settings included.
        payload = {"link": {**EVENT_REGISTRY["link.created"].sample()["link"]}}
        payload["link"]["expires_at"] = None
        payload["link"]["max_clicks"] = None
        fields = {
            f["name"]: f["value"]
            for f in _discord("link.created", payload)["embeds"][0]["fields"]
        }
        assert fields["Expires"] == "never"
        assert fields["Max clicks"] == "unlimited"
        assert fields["Password"] == "none"
        assert fields["Bots"] == "allowed"
        assert fields["Meta tags"] == "default"
        assert fields["Geo targeting"] == "none"

    def test_destination_field_is_full_width(self):
        (embed,) = _discord("link.created", EVENT_REGISTRY["link.created"].sample())[
            "embeds"
        ]
        by_name = {f["name"]: f for f in embed["fields"]}
        assert by_name["Destination"]["inline"] is False
        assert by_name["Expires"]["inline"] is True

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
        total = len(embed["title"]) + len(embed["description"])
        assert total <= _EMBED_TOTAL_MAX


class TestSlack:
    def test_clicked_is_title_fields_context(self):
        doc = _slack("link.clicked", _clicked_payload())
        assert doc["text"] == "Click: spoo.me/summer-drop"
        first, last = doc["blocks"][0], doc["blocks"][-1]
        assert first["type"] == "section"
        assert first["text"]["text"].startswith(
            "*<https://spoo.me/summer-drop|Click: spoo.me/summer-drop>*"
        )
        fields = [f["text"] for f in doc["blocks"][1]["fields"]]
        assert "*Country*\n🇮🇳 IN" in fields
        assert "*From*\nx.com" in fields
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
        doc = _slack("link.clicked", payload)
        fields = " ".join(f["text"] for f in doc["blocks"][1]["fields"])
        assert "&lt;Berlin &amp; Brandenburg&gt;" in fields
        assert "<Berlin" not in fields

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
