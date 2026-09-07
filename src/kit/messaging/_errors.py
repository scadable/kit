"""What a broker failure is, as types a caller can branch on.

EVERY ONE OF THESE NAMES THE BROKER, for the reason `kit.clients._errors` gives
about upstreams: a fleet running more than one bus, or one service reading from
two, produces a log line that costs an incident responder the first ten minutes
working out which.

THEY CARRY NO STATUS CODE AND NO SUBJECT. Which HTTP status a service renders a
broker failure as belongs to the API version doing the rendering. The subject is
left off deliberately too: it frequently carries a tenant id, and an exception
message is the one place that reliably reaches a log aggregator unredacted.
"""

from __future__ import annotations


class BrokerError(Exception):
    """Something went wrong talking to the bus."""

    def __init__(self, broker: str, detail: str = "") -> None:
        super().__init__(f"{broker}: {detail}" if detail else broker)
        self.broker = broker
        self.detail = detail


class BrokerUnavailable(BrokerError):
    """The bus could not be reached, or would not take the message.

    Retrying LATER may work. This is the shape a publisher should let its own
    retry policy see, and the shape a caller should never turn into a dropped
    message: on this error the state change that produced the message has
    usually already been committed.
    """


class BrokerTimeout(BrokerUnavailable):
    """We gave up waiting.

    A subclass of Unavailable, deliberately, because to a caller deciding what to
    do next it is the same class of problem. Separate because it is the one that
    means we stopped rather than the bus said no, and the two lead to different
    places when somebody goes looking.
    """


class MalformedMessage(BrokerError):
    """A message arrived that this package will not hand on.

    Its headers do not carry the four fields a consumer needs, so there is no
    tenant to scope to and no id to dedupe on. Not a subclass of Unavailable:
    retrying changes nothing, because the message will be malformed next time
    too. The consumer's move is to terminate it rather than nak it, which is why
    the two settle verbs are separate.
    """
