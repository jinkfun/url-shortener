"""Flavor registry — adding a flavor is a new module + one entry here."""

from services.webhooks.renderers.protocol import Renderer
from services.webhooks.renderers.raw import RawRenderer


def default_renderers() -> dict[str, Renderer]:
    return {RawRenderer.flavor: RawRenderer()}


__all__ = ["RawRenderer", "Renderer", "default_renderers"]
