"""The bus, as one object per broker, built where everything else is built.

WHAT THIS OWNS. Connecting, publishing with the four headers, pulling a batch,
and the three ways a message can be settled. Reconnection is delegated to the
NATS client, whose parameters are set here rather than left at their defaults.

WHAT IT REFUSES TO OWN, and this is the important half:

  - IT CREATES NO STREAMS AND NO CONSUMERS. Creating a stream carries retention,
    replica and discard decisions, and a library that creates one on connect
    eventually creates the wrong one in production, silently, because the call
    is idempotent and the second caller's arguments are ignored. Streams are an
    operational act.

  - IT DOES NOT AUTO-ACK. `fetch` hands back messages that are still unsettled,
    and settling is an explicit call. An auto-acking API turns at-least-once
    into at-most-once at the moment a handler raises, which is the one moment
    anybody cares.

  - IT HOLDS NO SCHEMA. The payload is opaque bytes.

THE IMPORT OF `nats` IS FUNCTION LOCAL, always, because the dependency is an
optional extra and every service in the fleet inherits this package whether or
not it touches a bus. A module-scope import would make `import kit.messaging`
fail at startup for the services that never asked for it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kit.messaging._errors import BrokerTimeout, BrokerUnavailable, MalformedMessage
from kit.messaging._headers import Envelope, envelope
from kit.observability import (
    MESSAGING_CONSUMED,
    MESSAGING_DELIVERIES,
    MESSAGING_PUBLISHED,
    MESSAGING_SETTLED,
    record,
)

log = logging.getLogger("kit.messaging")

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_RECONNECT_WAIT_SECONDS = 2.0
DEFAULT_FETCH_TIMEOUT_SECONDS = 5.0
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 5.0

RECONNECT_FOREVER = -1
"""What `max_reconnect_attempts` means by default, and it is deliberate.

A consumer that gives up reconnecting becomes a process that is running, healthy
by any liveness probe, and consuming nothing. Retrying forever turns that into a
visible gap in throughput instead, which is the failure somebody notices.
"""


@dataclass(frozen=True, slots=True)
class Broker:
    """One bus, and everything this service knows about reaching it."""

    name: str
    """Short and STABLE. It becomes a metric label and a readiness check name, so
    changing it starts a new time series and orphans the dashboard watching the
    old one."""

    servers: tuple[str, ...] = ()
    credentials_path: str = ""
    """A `.creds` file, when the bus requires one.

    Empty today: the deployed bus has no authentication and is reached only
    through a NetworkPolicy. It is carried anyway so that turning auth on is a
    config change in one service rather than a release of this package."""

    token: str = ""
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    reconnect_wait_seconds: float = DEFAULT_RECONNECT_WAIT_SECONDS
    max_reconnect_attempts: int = RECONNECT_FOREVER
    publish_timeout_seconds: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a broker needs a name; it labels metrics and health checks")

    @property
    def configured(self) -> bool:
        """Whether an operator set this up, answered without opening a socket."""
        return bool(self.servers)


@dataclass(frozen=True, slots=True)
class Message:
    """One delivery, still unsettled.

    `delivered` is the attempt number this delivery is, counting from one. Above
    one means the message has been redelivered, which is the signal a handler
    uses to decide that something is poisonous rather than merely slow.
    """

    envelope: Envelope
    payload: bytes
    delivered: int
    stream: str
    _broker: str = field(repr=False, default="")
    _message: Any = field(repr=False, default=None)

    async def ack(self) -> None:
        """Done. Do not send this again."""
        await self._settle("ack")

    async def nak(self, delay_seconds: float | None = None) -> None:
        """Not now. Send it again, optionally after a delay.

        The delay is the back pressure knob: naking without one on a dependency
        that is down produces a hot loop against it.
        """
        await self._settle("nak", delay_seconds)

    async def term(self) -> None:
        """Never. Stop redelivering this message.

        For a message that will fail identically every time: a malformed
        payload, or work whose subject no longer exists. A nak would retry it
        until the consumer's limit and then dead-letter it anyway, having spent
        the attempts to learn what was already known.
        """
        await self._settle("term")

    async def _settle(self, outcome: str, delay_seconds: float | None = None) -> None:
        try:
            if outcome == "ack":
                await self._message.ack()
            elif outcome == "nak":
                await self._message.nak(delay=delay_seconds)
            else:
                await self._message.term()
        except Exception as error:
            # A FAILED SETTLE IS NOT A FAILED HANDLER. The work happened; only
            # the acknowledgement did not, so the message will be redelivered
            # and the handler's idempotency is what saves it. Raising here would
            # tell a caller its work failed, which is worse than the truth.
            log.warning(
                "could not settle message",
                extra={"broker": self._broker, "outcome": outcome, "error": str(error)},
            )
            return
        record(MESSAGING_SETTLED, 1, broker=self._broker, stream=self.stream, outcome=outcome)


class Subscription:
    """A durable consumer, pulled in batches.

    PULL RATHER THAN PUSH, because a pull consumer takes work at the rate the
    process can finish it. A push consumer is handed messages at the server's
    rate, and a slow handler turns that into an ack timeout and redelivery of
    work that is still running.
    """

    def __init__(self, broker: str, stream: str, subscription: Any) -> None:
        self.broker = broker
        self.stream = stream
        self._subscription = subscription

    async def fetch(
        self, batch: int = 1, timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS
    ) -> Sequence[Message]:
        """Take up to `batch` messages, or none.

        AN EMPTY BATCH IS NOT AN ERROR and is the ordinary case on a quiet
        stream. The NATS client signals it with a timeout, which is caught here
        and turned into an empty sequence, so a caller's loop does not have to
        treat "nothing happened" as a failure.
        """
        try:
            raw = await self._subscription.fetch(batch, timeout=timeout_seconds)
        except TimeoutError:
            return ()
        except Exception as error:
            raise BrokerUnavailable(self.broker, f"fetch failed: {error}") from None

        messages: list[Message] = []
        for one in raw:
            try:
                read = envelope(dict(one.headers or {}))
            except ValueError as error:
                # TERMINATED, NOT NAKED. It will be malformed next time too, and
                # a nak would spend the whole attempt budget rediscovering that.
                await _terminate(one)
                log.warning(
                    "terminated an unreadable message",
                    extra={"broker": self.broker, "stream": self.stream, "error": str(error)},
                )
                continue
            delivered = _delivery_count(one)
            record(MESSAGING_CONSUMED, 1, broker=self.broker, stream=self.stream)
            record(MESSAGING_DELIVERIES, delivered, broker=self.broker, stream=self.stream)
            messages.append(
                Message(
                    envelope=read,
                    payload=one.data,
                    delivered=delivered,
                    stream=self.stream,
                    _broker=self.broker,
                    _message=one,
                )
            )
        return tuple(messages)


class MessageBus:
    """The connection, built once in a composition root and passed around.

    Never a module-level singleton: a test substitutes one by passing a
    different object, not by monkeypatching this module.
    """

    def __init__(self, broker: Broker, connection: Any | None = None) -> None:
        """`connection` exists for tests, and for one real case.

        A test passes a stub and exercises the real header, settle and metric
        code with no server and no sleep. The real case is a service that
        already holds a connection and wants this package's policy on top of it.
        """
        self.broker = broker
        self._connection = connection
        self._stream: Any = None if connection is None else connection.jetstream()

    @property
    def connected(self) -> bool:
        """What the client believes, without asking the network.

        A readiness check reads this. It does not ping the bus, for the reason
        `kit.clients` does not call its upstreams: a probe that reaches out turns
        one kubelet's polling interval into load on a shared dependency, from
        every replica, forever.
        """
        return self._connection is not None and bool(self._connection.is_connected)

    async def connect(self) -> None:
        """Open the connection, or say why not.

        Idempotent: calling it twice is a no-op rather than a second connection,
        because a composition root that retries should not leak one.
        """
        if self._connection is not None:
            return
        if not self.broker.configured:
            raise BrokerUnavailable(self.broker.name, "no servers configured")

        try:
            import nats as _nats
        except ImportError:
            raise BrokerUnavailable(
                self.broker.name,
                "the nats extra is not installed; add scadable-kit[nats]",
            ) from None

        options: dict[str, Any] = {
            "servers": list(self.broker.servers),
            "connect_timeout": self.broker.connect_timeout_seconds,
            "reconnect_time_wait": self.broker.reconnect_wait_seconds,
            "max_reconnect_attempts": self.broker.max_reconnect_attempts,
        }
        if self.broker.credentials_path:
            options["user_credentials"] = self.broker.credentials_path
        if self.broker.token:
            options["token"] = self.broker.token

        # ERASED AT THE BOUNDARY, the way `_metrics` erases the OpenTelemetry
        # SDK. `nats-py` ships no type information, so under pyright's strict
        # mode every call through it is a partially-unknown type. Narrowing here
        # keeps the strictness on OUR code, where it is worth having, rather
        # than turning the setting off for the package.
        nats: Any = _nats

        try:
            self._connection = await nats.connect(**options)
        except TimeoutError:
            raise BrokerTimeout(self.broker.name, "connect timed out") from None
        except Exception as error:
            raise BrokerUnavailable(self.broker.name, f"connect failed: {error}") from None
        self._stream = self._connection.jetstream()

    async def publish(self, subject: str, payload: bytes, message: Envelope) -> None:
        """Publish, and wait for the stream to say it stored it.

        WAITING IS THE POINT. JetStream's publish returns an acknowledgement
        that the message is persisted to the stream's replicas; not waiting for
        it makes this a fire and forget call that returns success while the
        message is still in flight, which is indistinguishable from working
        right up until a leader election eats one.

        Deduplication comes free: the envelope's id rides as `Nats-Msg-Id`, and
        the stream refuses a second copy inside its duplicate window.
        """
        stream = self._require_stream()
        try:
            await stream.publish(
                subject,
                payload,
                headers=message.headers(),
                timeout=self.broker.publish_timeout_seconds,
            )
        except TimeoutError:
            raise BrokerTimeout(self.broker.name, "publish timed out") from None
        except Exception as error:
            raise BrokerUnavailable(self.broker.name, f"publish failed: {error}") from None
        record(MESSAGING_PUBLISHED, 1, broker=self.broker.name, stream=subject.split(".")[0])

    async def subscribe(self, stream: str, subject: str, durable: str) -> Subscription:
        """Bind to a durable consumer that already exists.

        `durable` is a name the operator created. Binding rather than creating is
        what keeps ack policy, max deliveries and the dead-letter arrangement in
        one place instead of in whichever service connected first.
        """
        pull = self._require_stream()
        try:
            subscription = await pull.pull_subscribe(subject, durable=durable, stream=stream)
        except Exception as error:
            raise BrokerUnavailable(
                self.broker.name, f"could not bind consumer {durable!r}: {error}"
            ) from None
        return Subscription(self.broker.name, stream, subscription)

    async def close(self) -> None:
        """Drain and close, so in-flight work finishes.

        `drain` rather than `close`: it stops taking new messages, lets what is
        already in hand settle, and then closes. A bare close abandons them and
        they come back as redeliveries to whoever is left.
        """
        if self._connection is None:
            return
        try:
            await self._connection.drain()
        except Exception as error:
            log.warning(
                "could not drain the broker connection",
                extra={"broker": self.broker.name, "error": str(error)},
            )
        self._connection = None
        self._stream = None

    def _require_stream(self) -> Any:
        if self._stream is None:
            raise BrokerUnavailable(self.broker.name, "not connected")
        return self._stream


async def _terminate(message: Any) -> None:
    """Terminate a message we could not read, and never raise doing it."""
    try:
        await message.term()
    except Exception:
        return


def _delivery_count(message: Any) -> int:
    """Which attempt this is, counting from one.

    Absent metadata is reported as a first delivery rather than as zero: zero
    would say "never delivered" about a message being held, and the number is
    used to decide whether something is poisonous.
    """
    metadata = getattr(message, "metadata", None)
    delivered = getattr(metadata, "num_delivered", None)
    if not isinstance(delivered, int) or delivered < 1:
        return 1
    return delivered


__all__ = [
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "Broker",
    "MalformedMessage",
    "Message",
    "MessageBus",
    "Subscription",
]
