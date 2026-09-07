"""The bus, in `/readyz`, without ever touching the network.

INFORMATIONAL, NEVER BLOCKING, and the reason is the same one `kit.clients` gives
about an open breaker. A web replica that cannot reach the bus can still serve
every read it owns. Failing readiness would take it out of rotation, which turns
one component's outage into two and does not reconnect anything.

It reads what the client already believes rather than sending a ping, because a
readiness probe that reaches out turns one kubelet's polling interval into load
on a shared dependency, from every replica, forever.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from kit.health import Registry
from kit.messaging._broker import MessageBus


def register_brokers(registry: Registry, buses: Iterable[MessageBus]) -> None:
    """Declare one check per bus, named for the broker."""
    for bus in buses:
        registry.add_informational(bus.broker.name, _check(bus))


def _check(bus: MessageBus) -> Callable[[], Awaitable[None]]:
    async def check() -> None:
        if not bus.connected:
            raise RuntimeError(f"{bus.broker.name} is not connected")

    return check
