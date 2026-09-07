"""What travels beside a message, and why these four and no others.

THE PAYLOAD IS THE SERVICE'S BUSINESS AND THESE ARE NOT. A publisher and a
consumer have to agree on where the tenant lives and on what makes a message the
same message, before either can read a byte of the body. Left to each service,
those two answers diverge, and the divergence is invisible until a consumer is
silently processing another tenant's event or reprocessing one it already did.

WHAT IS DELIBERATELY NOT HERE. There is no schema, no version field, no envelope
around the payload. The body is opaque bytes to this package. `kit` holds the
transport policy; what an event IS belongs to the services that agree on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

TENANT = "Scadable-Tenant"
"""Which tenant the message belongs to.

Not optional and not defaulted. A consumer scopes its work by this, and a
message that arrives without one cannot be scoped to anything, so it is refused
here rather than defaulted to something that would read as legitimate later.
"""

EVENT_ID = "Nats-Msg-Id"
"""What makes this message the same message. NOT one of our own header names.

`Nats-Msg-Id` is JetStream's own deduplication header: the server refuses a
second publish carrying an id it has already seen inside the stream's duplicate
window. Using our own name here would leave that mechanism switched off and
duplicate suppression entirely to consumers.

So publishing is idempotent within the window for free, and past the window it
is still the id a consumer dedupes on. That is why the constant is named for
what it means to us and valued for what NATS does with it.
"""

EVENT_TYPE = "Scadable-Event"
"""What kind of thing happened, so a consumer can route without parsing a body."""

OCCURRED_AT = "Scadable-Occurred-At"
"""When it happened, RFC 3339 in UTC.

When it HAPPENED, not when it was published or delivered. Those differ whenever
a relay was behind, and the difference is the thing anybody debugging a lag
actually wants.
"""


@dataclass(frozen=True, slots=True)
class Envelope:
    """The four headers, as one object, checked once.

    Frozen because a message's identity should not change between being received
    and being settled.
    """

    tenant: str
    event_id: str
    event_type: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        """Refuse a blank rather than carry one.

        A blank tenant scopes to nothing, a blank id makes every message unique
        and defeats deduplication, and a blank type gives a consumer nothing to
        route on. Each is a bug at the publisher that would otherwise be found
        by the consumer, hours later, as absent data.
        """
        for name, value in (
            ("tenant", self.tenant),
            ("event_id", self.event_id),
            ("event_type", self.event_type),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required and was blank")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone aware")

    def headers(self) -> dict[str, str]:
        """The wire form."""
        return {
            TENANT: self.tenant,
            EVENT_ID: self.event_id,
            EVENT_TYPE: self.event_type,
            OCCURRED_AT: self.occurred_at.astimezone(UTC).isoformat(),
        }


def envelope(headers: Mapping[str, str] | None) -> Envelope:
    """Read the four headers back, or say which one was missing.

    Raises `ValueError`, which the broker turns into a typed error naming itself.
    A message that cannot be read is not a message this package will hand to a
    consumer with fields quietly defaulted.
    """
    present = headers or {}
    occurred = present.get(OCCURRED_AT, "")
    try:
        at = datetime.fromisoformat(occurred)
    except ValueError:
        raise ValueError(f"{OCCURRED_AT} is not an RFC 3339 timestamp") from None
    # A naive timestamp is ambiguous rather than wrong, and reading it as UTC
    # would be a guess that looks like data. `Envelope` refuses it below.
    return Envelope(
        tenant=present.get(TENANT, ""),
        event_id=present.get(EVENT_ID, ""),
        event_type=present.get(EVENT_TYPE, ""),
        occurred_at=at,
    )
