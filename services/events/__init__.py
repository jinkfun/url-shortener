"""Domain-event backbone — envelope contract, sink protocol, transports.

NOT webhook-specific: the webhooks dispatcher is one consumer; future
consumers (alerts, event log, audit) register their own consumer groups
on the same streams without touching this package.
"""

from services.events.contract import DOMAIN_EVENTS_STREAM, DomainEvent
from services.events.protocol import DomainEventSink
from services.events.sinks import (
    InlineDomainEventSink,
    NullDomainEventSink,
    StreamDomainEventSink,
)

__all__ = [
    "DOMAIN_EVENTS_STREAM",
    "DomainEvent",
    "DomainEventSink",
    "InlineDomainEventSink",
    "NullDomainEventSink",
    "StreamDomainEventSink",
]
