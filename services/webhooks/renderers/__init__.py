"""Flavor registry — adding a flavor is a new module + one entry here."""

from services.webhooks.renderers.discord import DiscordRenderer
from services.webhooks.renderers.protocol import Renderer
from services.webhooks.renderers.raw import RawRenderer
from services.webhooks.renderers.slack import SlackRenderer


def default_renderers() -> dict[str, Renderer]:
    return {
        RawRenderer.flavor: RawRenderer(),
        DiscordRenderer.flavor: DiscordRenderer(),
        SlackRenderer.flavor: SlackRenderer(),
    }


__all__ = [
    "DiscordRenderer",
    "RawRenderer",
    "Renderer",
    "SlackRenderer",
    "default_renderers",
]
