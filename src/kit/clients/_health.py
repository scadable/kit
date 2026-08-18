"""Upstream state in the readiness report, without it deciding readiness.

INFORMATIONAL, never blocking. The reasoning is worth stating because the
opposite is the intuitive choice: if `brain` is down, marking this service
not-ready removes every one of its replicas from the load balancer. That does
not fix brain. It takes a service that could still serve most of its routes and
removes it too, so one dependency's outage becomes two services' outage, and the
recovery now needs both to come back.

What the report is for is the incident responder who runs `curl /readyz` and
needs to see, in one place, which upstream is being shed. That is a diagnosis,
not a scheduling decision.
"""

from __future__ import annotations

from collections.abc import Iterable

from kit.clients._client import ServiceClient
from kit.health import Registry


def register_upstreams(registry: Registry, clients: Iterable[ServiceClient]) -> None:
    """Add one informational check per upstream.

    Reports the BREAKER's opinion rather than making a request. A readiness
    probe that calls every upstream turns one kubelet's polling interval into
    load on five other services, from every replica, forever; and it would be
    measuring the wrong thing anyway, since what matters is whether this service
    is currently able to use the upstream.
    """
    for client in clients:
        registry.add_informational(client.upstream.name, _check(client))


def _check(client: ServiceClient):
    async def check() -> None:
        if client.breaker.is_open:
            raise RuntimeError(
                f"{client.upstream.name} is being shed after "
                f"{client.breaker.failures} consecutive failures"
            )

    return check
