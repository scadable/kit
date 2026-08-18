"""Calling anything outside this process.

One client per dependency, built once in the composition root, called in one
line:

    brain = service_client(Upstream(
        name="brain",
        base_url=settings.value("BRAIN_URL"),
        token=settings.value("BRAIN_SERVICE_TOKEN"),
    ))

    body = await brain.get_json("/api/v1/policies", params={"tenant": tenant})

The service still writes a small typed gateway on top of that, mapping the
response to domain objects; what the gateway no longer owns is retries,
timeouts, breakers, headers and trace context. Seven services each getting that
right independently is seven different behaviours the first time a dependency
gets slow.

Third parties fit the same shape. `Upstream(authorizer=...)` takes an async hook
called per request, which is what lets an OAuth token refresh without this
package knowing what OAuth is.

WHAT IS POLICY AND WHAT IS NOT. The retry counts, backoff, breaker thresholds
and timeouts are all parameters and yours to change per upstream. The header
names are not: `X-Request-ID` and `traceparent` are how the fleet correlates a
request across services, and a service that sends different ones is a service
whose calls cannot be followed.

`readiness` registers each upstream as an INFORMATIONAL check. An open breaker
is visible in `/readyz` and blocks nothing: taking every replica of this service
out of rotation because a dependency is down does not fix the dependency, and
turns one outage into two.
"""

from kit.clients._breaker import Breaker, BreakerPolicy, State
from kit.clients._client import (
    Authorizer,
    ServiceClient,
    Upstream,
    bearer,
    service_client,
)
from kit.clients._errors import (
    UpstreamError,
    UpstreamRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from kit.clients._health import register_upstreams
from kit.clients._retry import RetryPolicy

__all__ = [
    "Authorizer",
    "Breaker",
    "BreakerPolicy",
    "RetryPolicy",
    "ServiceClient",
    "State",
    "Upstream",
    "UpstreamError",
    "UpstreamRejected",
    "UpstreamTimeout",
    "UpstreamUnavailable",
    "bearer",
    "register_upstreams",
    "service_client",
]
