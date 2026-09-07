"""The event bus, as transport policy the whole fleet shares.

    bus = MessageBus(Broker(name="events", servers=("nats://nats.nats:4222",)))
    await bus.connect()

    await bus.publish(
        "events.repository.indexed",
        payload,
        Envelope(tenant=tenant, event_id=uid, event_type="repository.indexed",
                 occurred_at=now),
    )

    subscription = await bus.subscribe(stream="EVENTS", subject="events.>",
                                       durable="brain")
    for message in await subscription.fetch(batch=8):
        await handle(message)
        await message.ack()

WHY THIS IS IN THE KIT AT ALL, given that "code moves in on its third copy". This
is the `kit.clients` case rather than the database-helpers case: it is not an
abstraction over a domain, it is the ack policy, the redelivery behaviour, the
reconnection settings and the header convention, and those are wrong in the same
way in every service that decides them alone. They are also wrong in a way that
only shows up during an incident, when a consumer has been quietly reprocessing
or quietly skipping.

THE BOUNDARY THAT KEEPS IT HONEST IS WHAT IT REFUSES TO HOLD. The payload is
opaque bytes. There is no schema, no event registry and no versioning: what an
event IS belongs to the services that agree on it, and a package holding that
would be a messaging product rather than a seam. It creates no streams and no
consumers either, because retention and replica counts are operational decisions
and a library that makes them makes them once, wrongly, in production.

WHAT IS POLICY AND WHAT IS NOT. Timeouts, batch sizes, reconnection and nak
delays are parameters and yours to change per broker. The four header names are
not: they are how tenancy and idempotency travel, and a service that renames one
stops being readable by every other service on the bus.

AT LEAST ONCE, SAID PLAINLY. `fetch` returns unsettled messages and settling is
an explicit `ack`, `nak` or `term`. Nothing here acknowledges on your behalf, so
a handler that raises gets its message back rather than losing it. Handlers must
be idempotent; `Envelope.event_id` is what they deduplicate on, and it rides the
wire as `Nats-Msg-Id` so the server suppresses duplicate publishes too.
"""

from __future__ import annotations

from kit.messaging._broker import (
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    Broker,
    Message,
    MessageBus,
    Subscription,
)
from kit.messaging._errors import (
    BrokerError,
    BrokerTimeout,
    BrokerUnavailable,
    MalformedMessage,
)
from kit.messaging._headers import (
    EVENT_ID,
    EVENT_TYPE,
    OCCURRED_AT,
    TENANT,
    Envelope,
    envelope,
)
from kit.messaging._health import register_brokers

__all__ = [
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "EVENT_ID",
    "EVENT_TYPE",
    "OCCURRED_AT",
    "TENANT",
    "Broker",
    "BrokerError",
    "BrokerTimeout",
    "BrokerUnavailable",
    "Envelope",
    "MalformedMessage",
    "Message",
    "MessageBus",
    "Subscription",
    "envelope",
    "register_brokers",
]
