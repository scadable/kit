"""The outbound transport. One client per upstream, built once at startup.

A service does not write retry logic. It builds a client per dependency in its
composition root and calls it:

    brain = service_client(Upstream(name="brain", base_url=..., token=...))
    body = await brain.get_json("/api/v1/policies", params={"tenant": tenant})

A client rather than a bare function because retries, a connection pool and a
breaker all need state that belongs to ONE upstream. A module-level registry
would make the call sites shorter and would give two tests in one process a
shared breaker, and give a reader no way to see where "brain" was configured.

Every call carries, without the caller doing anything:

  a deadline           `timeout=None` is not reachable through this API
  traceparent          so the upstream's spans join this request's trace
  X-Request-ID         the INBOUND id, propagated, so one id spans the fan-out
  Authorization        from a pluggable hook, so OAuth fits without a second client
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from kit.clients._breaker import Breaker, BreakerPolicy
from kit.clients._errors import UpstreamRejected, UpstreamTimeout, UpstreamUnavailable
from kit.clients._retry import RetryPolicy, backoff, may_retry, retry_after, retryable_status
from kit.httpapi import REQUEST_ID_HEADER, request_id
from kit.observability import (
    CLIENT_ATTEMPTS,
    CLIENT_BREAKER_OPEN,
    CLIENT_DURATION,
    CLIENT_REQUESTS,
    instrument_client,
    record,
)

DEFAULT_TIMEOUT_SECONDS = 10.0

Authorizer = Callable[[], Awaitable[Mapping[str, str]]]
"""Returns the headers that authenticate one call.

Async and called PER REQUEST rather than once, because that is what lets an
OAuth implementation refresh an expired token without this module knowing what
OAuth is. A bearer token ignores the freedom and returns the same header every
time.
"""


def bearer(token: str) -> Authorizer:
    """The SCADABLE service-to-service case: one static token."""

    async def authorize() -> Mapping[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    return authorize


@dataclass(frozen=True, slots=True)
class Upstream:
    """One dependency, and everything this service knows about calling it."""

    name: str
    """Short and STABLE. It becomes a metric label and a readiness check name,
    so changing it silently starts a new time series and orphans the dashboard
    that was watching the old one."""

    base_url: str
    token: str = ""
    """Convenience for the common case. Ignored when `authorizer` is given."""

    authorizer: Authorizer | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    breaker: BreakerPolicy = field(default_factory=BreakerPolicy)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


class ServiceClient:
    """One upstream's transport. Construct via `service_client`."""

    def __init__(self, upstream: Upstream, transport: httpx.AsyncBaseTransport | None = None):
        self.upstream = upstream
        self.breaker = Breaker(policy=upstream.breaker)
        self._authorize = upstream.authorizer or bearer(upstream.token)
        self._client = httpx.AsyncClient(
            base_url=upstream.base_url,
            # An explicit Timeout, never None. A call with no deadline holds a
            # worker until the process dies, and it is the failure that looks
            # like a slow service rather than a broken one.
            timeout=httpx.Timeout(upstream.timeout_seconds),
            transport=transport,
        )
        instrument_client(self._client)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        return _decode(await self.request("GET", path, **kwargs))

    async def post_json(self, path: str, **kwargs: Any) -> Any:
        return _decode(await self.request("POST", path, **kwargs))

    async def put_json(self, path: str, **kwargs: Any) -> Any:
        return _decode(await self.request("PUT", path, **kwargs))

    async def delete_json(self, path: str, **kwargs: Any) -> Any:
        return _decode(await self.request("DELETE", path, **kwargs))

    async def request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str = "",
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One logical call: breaker, retries, deadline, headers, metrics.

        Raises rather than returning a failed response. A 500 that comes back as
        an object is a 500 somebody forgets to check, and the whole point of a
        typed failure is that forgetting is not possible.
        """
        if not self.upstream.configured:
            raise UpstreamUnavailable(self.upstream.name, "no base URL is configured")

        if not self.breaker.allows():
            # No socket opened. This is the entire value of the breaker: the
            # upstream gets to recover instead of being retried at by every
            # replica simultaneously.
            record(CLIENT_REQUESTS, 1, upstream=self.upstream.name, method=method, outcome="shed")
            raise UpstreamUnavailable(self.upstream.name, "circuit is open")

        started = asyncio.get_running_loop().time()
        attempts = 0
        last: Exception | None = None
        policy = self.upstream.retry
        retryable = may_retry(method, idempotency_key=idempotency_key)

        # `while True`, not `while attempts < policy.attempts`. Every exit below
        # is an explicit break, return or raise, so the loop condition could
        # never be the thing that ended it: it read like a second bound and was
        # unreachable, which is the kind of line a later reader relies on.
        while True:
            attempts += 1
            try:
                response = await self._send(method, path, idempotency_key, headers, kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last = _as_failure(self.upstream.name, error)
                if not retryable or attempts >= policy.attempts:
                    break
                await asyncio.sleep(backoff(attempts, policy))
                continue

            if not retryable_status(response.status_code):
                self._settle(response.status_code, attempts, started, method)
                if response.status_code >= 400:
                    raise UpstreamRejected(self.upstream.name, response.status_code)
                return response

            last = UpstreamUnavailable(self.upstream.name, f"status {response.status_code}")
            if not retryable or attempts >= policy.attempts:
                break

            wait = _wait_for(response, policy, attempts)
            if wait is None:
                # The upstream asked for longer than the cap. Stop rather than
                # ignore it: a 429 answered by retrying sooner than asked is how
                # a rate limit becomes a ban.
                break
            await asyncio.sleep(wait)

        self._fail(attempts, started, method)
        raise last or UpstreamUnavailable(self.upstream.name, "exhausted attempts")

    async def _send(
        self,
        method: str,
        path: str,
        idempotency_key: str,
        headers: Mapping[str, str] | None,
        kwargs: dict[str, Any],
    ) -> httpx.Response:
        outgoing = dict(headers or {})
        outgoing.update(await self._authorize())

        # The INBOUND request id, propagated. A fresh id per hop would make each
        # service's logs internally consistent and unjoinable to each other,
        # which is the opposite of the point.
        inbound = request_id()
        if inbound:
            outgoing[REQUEST_ID_HEADER] = inbound
        if idempotency_key:
            outgoing["Idempotency-Key"] = idempotency_key

        # traceparent is not set here. instrument_client injects it from the
        # active span, which is the only source that stays correct when a call
        # happens inside a background task.
        return await self._client.request(method, path, headers=outgoing, **kwargs)

    def _settle(self, status: int, attempts: int, started: float, method: str) -> None:
        # A 4xx is the upstream working correctly and refusing us, so it must
        # not trip the breaker. Shedding an upstream because this service keeps
        # sending it malformed requests would hide our own bug behind an outage.
        if status >= 500:
            self.breaker.failed()
        else:
            self.breaker.succeeded()
        self._observe(attempts, started, method, outcome="ok" if status < 400 else "rejected")

    def _fail(self, attempts: int, started: float, method: str) -> None:
        was_open = self.breaker.is_open
        self.breaker.failed()
        if self.breaker.is_open and not was_open:
            record(CLIENT_BREAKER_OPEN, 1, upstream=self.upstream.name)
        self._observe(attempts, started, method, outcome="failed")

    def _observe(self, attempts: int, started: float, method: str, *, outcome: str) -> None:
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        name = self.upstream.name
        # Labels are the upstream NAME and the method, never the path. A path
        # carries ids, and one label carrying ids is how a metrics backend dies.
        record(CLIENT_REQUESTS, 1, upstream=name, method=method, outcome=outcome)
        record(CLIENT_DURATION, elapsed, upstream=name, method=method)
        record(CLIENT_ATTEMPTS, attempts, upstream=name)


def service_client(
    upstream: Upstream, transport: httpx.AsyncBaseTransport | None = None
) -> ServiceClient:
    """Build the transport for one dependency. Call this in the composition root.

    `transport` exists for tests: an `httpx.MockTransport` exercises the real
    retry, breaker and header code against a stub upstream, with no socket and
    no sleep of consequence.
    """
    return ServiceClient(upstream, transport)


def _wait_for(response: httpx.Response, policy: RetryPolicy, attempt: int) -> float | None:
    header = response.headers.get("retry-after", "")
    if header:
        asked = retry_after(header, policy)
        return None if asked is None else asked
    return backoff(attempt, policy)


def _as_failure(name: str, error: Exception) -> Exception:
    if isinstance(error, httpx.TimeoutException):
        return UpstreamTimeout(name, str(error))
    return UpstreamUnavailable(name, str(error))


def _decode(response: httpx.Response) -> Any:
    """JSON, or a typed failure naming the upstream.

    An upstream that answers 200 with HTML is a misrouted request or a captive
    portal, and the raw JSONDecodeError says nothing about which service it came
    from.
    """
    try:
        return response.json()
    except ValueError as error:
        raise UpstreamUnavailable("upstream", f"response was not JSON: {error}") from None
