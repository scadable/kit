"""The bus, exercised against a stub connection rather than a server.

The stub is the same seam `service_client(transport=...)` uses: the real header,
settle, metric and error code runs, with no socket and no sleep of consequence.
"""

from __future__ import annotations

import builtins
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from kit.health import Registry
from kit.httpapi import install_conventions
from kit.messaging import (
    EVENT_ID,
    OCCURRED_AT,
    TENANT,
    Broker,
    BrokerTimeout,
    BrokerUnavailable,
    Envelope,
    MessageBus,
    envelope,
    register_brokers,
)
from kit.messaging._broker import Subscription, _delivery_count
from kit.observability import _metrics

WHEN = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def an_envelope(**overrides: Any) -> Envelope:
    fields: dict[str, Any] = {
        "tenant": "org_1",
        "event_id": "evt_1",
        "event_type": "repository.indexed",
        "occurred_at": WHEN,
    }
    fields.update(overrides)
    return Envelope(**fields)


class FakeMetadata:
    def __init__(self, delivered: int) -> None:
        self.num_delivered = delivered


class FakeMessage:
    """One delivery, recording how it was settled."""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        data: bytes = b"{}",
        delivered: int = 1,
        fails: bool = False,
    ) -> None:
        self.headers = headers if headers is not None else an_envelope().headers()
        self.data = data
        self.metadata = FakeMetadata(delivered)
        self.settled: list[tuple[str, float | None]] = []
        self._fails = fails

    async def ack(self) -> None:
        self._record("ack")

    async def nak(self, delay: float | None = None) -> None:
        self._record("nak", delay)

    async def term(self) -> None:
        self._record("term")

    def _record(self, outcome: str, delay: float | None = None) -> None:
        if self._fails:
            raise RuntimeError("the connection went away")
        self.settled.append((outcome, delay))


class FakeSubscription:
    def __init__(self, batches: list[Any]) -> None:
        self._batches = batches

    async def fetch(self, batch: int, timeout: float) -> Any:  # noqa: ASYNC109
        result = self._batches.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeStream:
    def __init__(self, *, publish_error: Exception | None = None) -> None:
        self.published: list[tuple[str, bytes, dict[str, str], float]] = []
        self._publish_error = publish_error
        self.subscription = FakeSubscription([])
        self.subscribe_error: Exception | None = None

    async def publish(
        self,
        subject: str,
        payload: bytes,
        headers: dict[str, str],
        timeout: float,  # noqa: ASYNC109
    ) -> None:
        if self._publish_error is not None:
            raise self._publish_error
        self.published.append((subject, payload, headers, timeout))

    async def pull_subscribe(self, subject: str, durable: str, stream: str) -> Any:
        if self.subscribe_error is not None:
            raise self.subscribe_error
        return self.subscription


class FakeConnection:
    def __init__(self, stream: FakeStream | None = None, *, drain_fails: bool = False) -> None:
        self.is_connected = True
        self._stream = stream or FakeStream()
        self.drained = False
        self._drain_fails = drain_fails

    def jetstream(self) -> FakeStream:
        return self._stream

    async def drain(self) -> None:
        if self._drain_fails:
            raise RuntimeError("drain refused")
        self.drained = True


def a_bus(connection: FakeConnection | None = None) -> MessageBus:
    return MessageBus(Broker(name="events", servers=("nats://x:4222",)), connection)


# --- the envelope ------------------------------------------------------------


def test_the_four_headers_go_out_and_come_back() -> None:
    """A round trip, because the wire form is the contract between services."""
    read = envelope(an_envelope().headers())

    assert read == an_envelope()


def test_the_event_id_rides_as_the_header_jetstream_dedupes_on() -> None:
    """NOT one of our own names, and that is the whole point: the server refuses
    a second publish carrying an id it has already seen."""
    assert EVENT_ID == "Nats-Msg-Id"
    assert an_envelope().headers()[EVENT_ID] == "evt_1"


def test_an_occurred_at_is_normalised_to_utc() -> None:
    """Two publishers in two zones must produce the same string for one moment."""
    elsewhere = WHEN.astimezone(_offset(hours=5))

    assert an_envelope(occurred_at=elsewhere).headers()[OCCURRED_AT] == WHEN.isoformat()


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("field", ["tenant", "event_id", "event_type"])
def test_a_blank_required_field_is_refused(field: str, blank: str) -> None:
    """A blank tenant scopes to nothing and a blank id defeats deduplication.
    Both are publisher bugs that would otherwise be found by the consumer."""
    with pytest.raises(ValueError, match=field):
        an_envelope(**{field: blank})


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc() -> None:
    """Reading it as UTC would be a guess that then looks like data."""
    with pytest.raises(ValueError, match="timezone aware"):
        an_envelope(occurred_at=datetime(2026, 9, 7, 12, 0))  # noqa: DTZ001


def test_an_unparsable_timestamp_names_the_header() -> None:
    with pytest.raises(ValueError, match=OCCURRED_AT):
        envelope({**an_envelope().headers(), OCCURRED_AT: "yesterday"})


def test_absent_headers_are_refused_by_name() -> None:
    """`None` is what a message with no headers at all carries."""
    with pytest.raises(ValueError):
        envelope(None)


# --- publishing --------------------------------------------------------------


async def test_publishing_sends_the_headers_and_waits_for_the_stream() -> None:
    """Waiting is the point: an unwaited publish reports success while the
    message is still in flight."""
    connection = FakeConnection()
    bus = a_bus(connection)

    await bus.publish("events.repository.indexed", b"payload", an_envelope())

    subject, payload, headers, timeout = connection.jetstream().published[0]
    assert subject == "events.repository.indexed"
    assert payload == b"payload"
    assert headers[TENANT] == "org_1"
    assert timeout == bus.broker.publish_timeout_seconds


async def test_a_publish_timeout_is_its_own_type() -> None:
    """Separate from Unavailable because it means we stopped, not that the bus
    said no, and the two lead different places."""
    bus = a_bus(FakeConnection(FakeStream(publish_error=TimeoutError())))

    with pytest.raises(BrokerTimeout, match="events"):
        await bus.publish("events.x", b"", an_envelope())


async def test_a_failed_publish_names_the_broker() -> None:
    bus = a_bus(FakeConnection(FakeStream(publish_error=RuntimeError("no stream"))))

    with pytest.raises(BrokerUnavailable, match="events"):
        await bus.publish("events.x", b"", an_envelope())


async def test_publishing_before_connecting_is_refused() -> None:
    bus = MessageBus(Broker(name="events", servers=("nats://x:4222",)))

    with pytest.raises(BrokerUnavailable, match="not connected"):
        await bus.publish("events.x", b"", an_envelope())


# --- consuming ---------------------------------------------------------------


async def test_fetch_returns_unsettled_messages() -> None:
    """Nothing is acknowledged on the caller's behalf, so a handler that raises
    gets its message back rather than losing it."""
    raw = FakeMessage()
    subscription = Subscription("events", "EVENTS", FakeSubscription([[raw]]))

    [message] = await subscription.fetch()

    assert message.envelope.tenant == "org_1"
    assert message.payload == b"{}"
    assert raw.settled == []


async def test_an_empty_batch_is_not_an_error() -> None:
    """The ordinary case on a quiet stream. The client signals it as a timeout."""
    subscription = Subscription("events", "EVENTS", FakeSubscription([TimeoutError()]))

    assert await subscription.fetch() == ()


async def test_a_failed_fetch_names_the_broker() -> None:
    subscription = Subscription("events", "EVENTS", FakeSubscription([RuntimeError("gone")]))

    with pytest.raises(BrokerUnavailable, match="events"):
        await subscription.fetch()


async def test_an_unreadable_message_is_terminated_not_naked() -> None:
    """It will be malformed next time too, so a nak would spend the whole
    attempt budget rediscovering that."""
    bad = FakeMessage(headers={TENANT: "org_1"})
    subscription = Subscription("events", "EVENTS", FakeSubscription([[bad]]))

    assert await subscription.fetch() == ()
    assert bad.settled == [("term", None)]


async def test_terminating_an_unreadable_message_never_raises() -> None:
    """The connection may already be gone; that must not become the caller's
    problem on a message we were dropping anyway."""
    bad = FakeMessage(headers={}, fails=True)
    subscription = Subscription("events", "EVENTS", FakeSubscription([[bad]]))

    assert await subscription.fetch() == ()


async def test_a_readable_message_survives_beside_an_unreadable_one() -> None:
    """One poisonous message must not cost the batch."""
    subscription = Subscription(
        "events", "EVENTS", FakeSubscription([[FakeMessage(headers={}), FakeMessage()]])
    )

    assert len(await subscription.fetch()) == 1


# --- settling ----------------------------------------------------------------


async def test_the_three_settle_verbs_reach_the_message() -> None:
    raw = [FakeMessage(), FakeMessage(), FakeMessage()]
    subscription = Subscription("events", "EVENTS", FakeSubscription([raw]))
    one, two, three = await subscription.fetch(batch=3)

    await one.ack()
    await two.nak(delay_seconds=30.0)
    await three.term()

    assert raw[0].settled == [("ack", None)]
    assert raw[1].settled == [("nak", 30.0)]
    assert raw[2].settled == [("term", None)]


async def test_a_failed_settle_does_not_raise() -> None:
    """The work happened; only the acknowledgement did not. Raising would tell a
    caller its work failed, which is worse than the truth: the message comes back
    and the handler's idempotency covers it."""
    subscription = Subscription("events", "EVENTS", FakeSubscription([[FakeMessage(fails=True)]]))
    [message] = await subscription.fetch()

    await message.ack()


async def test_a_redelivery_reports_which_attempt_it_is() -> None:
    """Above one is how a handler tells poisonous from merely slow."""
    subscription = Subscription("events", "EVENTS", FakeSubscription([[FakeMessage(delivered=4)]]))

    [message] = await subscription.fetch()

    assert message.delivered == 4


@pytest.mark.parametrize("metadata", [None, FakeMetadata(0), "not metadata"])
def test_absent_delivery_metadata_reads_as_a_first_delivery(metadata: Any) -> None:
    """Zero would say "never delivered" about a message being held."""

    class Bare:
        pass

    message = Bare()
    if metadata is not None:
        message.metadata = metadata  # type: ignore[attr-defined]

    assert _delivery_count(message) == 1


# --- connecting --------------------------------------------------------------


async def test_connecting_twice_does_not_open_a_second_connection() -> None:
    connection = FakeConnection()
    bus = a_bus(connection)

    await bus.connect()

    assert bus.connected


async def test_connecting_with_no_servers_is_refused() -> None:
    bus = MessageBus(Broker(name="events"))

    with pytest.raises(BrokerUnavailable, match="no servers"):
        await bus.connect()


async def test_connecting_without_the_extra_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure a service that forgot the extra can actually read."""
    real = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "nats":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    bus = MessageBus(Broker(name="events", servers=("nats://x:4222",)))

    with pytest.raises(BrokerUnavailable, match=r"scadable-kit\[nats\]"):
        await bus.connect()


class FakeNats:
    """Stands in for the `nats` module, so the real connect path is exercised."""

    def __init__(self, connection: Any = None, error: Exception | None = None) -> None:
        self.connection = connection
        self.error = error
        self.options: dict[str, Any] = {}

    async def connect(self, **options: Any) -> Any:
        self.options = options
        if self.error is not None:
            raise self.error
        return self.connection


@pytest.fixture
def nats_module(monkeypatch: pytest.MonkeyPatch):
    """Install a stub `nats` module for the duration of one test."""

    def install(fake: FakeNats) -> FakeNats:
        module = types.ModuleType("nats")
        module.connect = fake.connect  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "nats", module)
        return fake

    return install


async def test_connecting_passes_the_reconnect_settings_through(nats_module: Any) -> None:
    """Retrying forever is deliberate: a consumer that gives up becomes a
    process that is running, healthy by any liveness probe, and consuming
    nothing."""
    fake = nats_module(FakeNats(FakeConnection()))
    bus = MessageBus(Broker(name="events", servers=("nats://x:4222",)))

    await bus.connect()

    assert fake.options["max_reconnect_attempts"] == -1
    assert fake.options["servers"] == ["nats://x:4222"]
    assert "user_credentials" not in fake.options
    assert "token" not in fake.options
    assert bus.connected


async def test_credentials_are_passed_when_set(nats_module: Any) -> None:
    """Carried even though the deployed bus has no auth, so turning it on is a
    config change in one service rather than a release of this package."""
    fake = nats_module(FakeNats(FakeConnection()))
    bus = MessageBus(
        Broker(
            name="events",
            servers=("nats://x:4222",),
            credentials_path="/etc/nats/creds",
            token="t",  # noqa: S106
        )
    )

    await bus.connect()

    assert fake.options["user_credentials"] == "/etc/nats/creds"
    assert fake.options["token"] == "t"  # noqa: S105


async def test_a_connect_timeout_is_its_own_type(nats_module: Any) -> None:
    nats_module(FakeNats(error=TimeoutError()))
    bus = MessageBus(Broker(name="events", servers=("nats://x:4222",)))

    with pytest.raises(BrokerTimeout, match="connect timed out"):
        await bus.connect()


async def test_a_refused_connect_names_the_broker(nats_module: Any) -> None:
    nats_module(FakeNats(error=RuntimeError("no route to host")))
    bus = MessageBus(Broker(name="events", servers=("nats://x:4222",)))

    with pytest.raises(BrokerUnavailable, match="events"):
        await bus.connect()


async def test_connecting_an_already_connected_bus_is_a_no_op(nats_module: Any) -> None:
    """A composition root that retries should not leak a second connection."""
    fake = nats_module(FakeNats(FakeConnection()))
    bus = a_bus(FakeConnection())

    await bus.connect()

    assert fake.options == {}


async def test_a_broker_needs_a_name() -> None:
    """It labels metrics and health checks, so a blank one is a series nobody
    can find."""
    with pytest.raises(ValueError, match="name"):
        Broker(name=" ")


def test_configured_answers_without_opening_a_socket() -> None:
    assert not Broker(name="events").configured
    assert Broker(name="events", servers=("nats://x:4222",)).configured


# --- subscribing -------------------------------------------------------------


async def test_subscribing_binds_an_existing_durable_consumer() -> None:
    """Binding rather than creating keeps ack policy and max deliveries in one
    place instead of in whichever service connected first."""
    bus = a_bus(FakeConnection())

    subscription = await bus.subscribe(stream="EVENTS", subject="events.>", durable="brain")

    assert subscription.stream == "EVENTS"


async def test_binding_a_missing_consumer_names_it() -> None:
    stream = FakeStream()
    stream.subscribe_error = RuntimeError("consumer not found")
    bus = a_bus(FakeConnection(stream))

    with pytest.raises(BrokerUnavailable, match="brain"):
        await bus.subscribe(stream="EVENTS", subject="events.>", durable="brain")


# --- closing -----------------------------------------------------------------


async def test_closing_drains_so_in_flight_work_finishes() -> None:
    """A bare close abandons held messages and they come back as redeliveries."""
    connection = FakeConnection()
    bus = a_bus(connection)

    await bus.close()

    assert connection.drained
    assert not bus.connected


async def test_closing_an_unconnected_bus_is_a_no_op() -> None:
    await MessageBus(Broker(name="events")).close()


async def test_a_failed_drain_does_not_raise_on_the_way_down() -> None:
    bus = a_bus(FakeConnection(drain_fails=True))

    await bus.close()

    assert not bus.connected


# --- readiness ---------------------------------------------------------------


async def readiness_of(registry: Registry) -> httpx.Response:
    app = FastAPI()
    install_conventions(app, readiness=registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/readyz")


async def test_a_connected_bus_reports_ready() -> None:
    registry = Registry()
    register_brokers(registry, [a_bus(FakeConnection())])

    response = await readiness_of(registry)

    assert response.json()["checks"]["events"] == "ready"


async def test_a_disconnected_bus_is_visible_but_does_not_block() -> None:
    """THE decision, asserted. A replica that cannot reach the bus still serves
    every read it owns, so failing readiness would turn one component's outage
    into two and would not reconnect anything."""
    registry = Registry()
    register_brokers(registry, [MessageBus(Broker(name="events"))])

    response = await readiness_of(registry)

    assert response.status_code == 200, "a disconnected bus took the service out of rotation"
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["events"] == "error"


# --- metrics -----------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any]]] = []

    def add(self, value: float, attributes: dict[str, Any]) -> None:
        self.calls.append((value, attributes))


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    """Every messaging instrument, so a name absent from `_build` shows up here
    as a missing key rather than as silence."""
    made = {
        name: Recorder()
        for name in (
            _metrics.MESSAGING_PUBLISHED,
            _metrics.MESSAGING_CONSUMED,
            _metrics.MESSAGING_SETTLED,
            _metrics.MESSAGING_DELIVERIES,
        )
    }
    monkeypatch.setattr(_metrics, "_instruments", dict(made))
    return made


async def test_the_metric_label_is_the_stream_never_the_subject(
    recorders: dict[str, Recorder],
) -> None:
    """A subject carries a tenant id in most useful designs, and one label
    holding one turns a single series into millions."""
    await a_bus(FakeConnection()).publish("events.repository.indexed", b"", an_envelope())

    _, attributes = recorders[_metrics.MESSAGING_PUBLISHED].calls[0]
    assert attributes == {"broker": "events", "stream": "events"}


async def test_settling_is_counted_by_outcome(recorders: dict[str, Recorder]) -> None:
    subscription = Subscription("events", "EVENTS", FakeSubscription([[FakeMessage()]]))
    [message] = await subscription.fetch()

    await message.nak()

    _, attributes = recorders[_metrics.MESSAGING_SETTLED].calls[0]
    assert attributes["outcome"] == "nak"


async def test_a_failed_settle_is_not_counted_as_settled(
    recorders: dict[str, Recorder],
) -> None:
    """Counting it would report an acknowledgement that never reached the bus."""
    subscription = Subscription("events", "EVENTS", FakeSubscription([[FakeMessage(fails=True)]]))
    [message] = await subscription.fetch()

    await message.ack()

    assert recorders[_metrics.MESSAGING_SETTLED].calls == []


def _offset(hours: int) -> Any:
    """A fixed offset, without importing zoneinfo for one assertion."""
    return __import__("datetime").timezone(timedelta(hours=hours))
