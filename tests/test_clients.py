"""The outbound transport, against a stub upstream.

httpx.MockTransport, so the real retry, breaker and header code runs with no
socket. Every failure here is one that only appears when a dependency is slow or
down, which is the moment nobody wants to be discovering it.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from kit.clients import (
    RetryPolicy,
    Upstream,
    UpstreamRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
    service_client,
)
from kit.clients._breaker import Breaker, BreakerPolicy, State
from kit.clients._retry import backoff, may_retry, retry_after, retryable_status

FAST = RetryPolicy(attempts=3, backoff_seconds=0.001, backoff_cap_seconds=0.002)


def responder(*responses: httpx.Response | Exception) -> tuple[Callable[..., object], list[str]]:
    """Answers with each item in turn, repeating the last one forever."""
    calls: list[str] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return handle, calls


def client_for(*responses: httpx.Response | Exception, **kwargs: object):
    handle, calls = responder(*responses)
    upstream = Upstream(name="brain", base_url="http://brain", retry=FAST, **kwargs)  # type: ignore[arg-type]
    return service_client(upstream, httpx.MockTransport(handle)), calls


def ok(payload: object = None) -> httpx.Response:
    return httpx.Response(200, json=payload if payload is not None else {"ok": True})


# --- retries ----------------------------------------------------------------


async def test_a_503_is_retried_and_can_succeed() -> None:
    client, calls = client_for(httpx.Response(503), ok())

    assert await client.get_json("/thing") == {"ok": True}
    assert len(calls) == 2
    await client.aclose()


async def test_a_400_is_never_retried() -> None:
    """It will fail identically on every attempt, so retrying spends the
    caller's deadline to arrive at the same answer more slowly."""
    client, calls = client_for(httpx.Response(400))

    with pytest.raises(UpstreamRejected):
        await client.get_json("/thing")

    assert len(calls) == 1
    await client.aclose()


async def test_a_post_is_not_retried_without_an_idempotency_key() -> None:
    """THE one that costs money. A POST that timed out may have SUCCEEDED with
    only the response lost, so a retry performs the operation twice and the
    caller cannot tell."""
    client, calls = client_for(httpx.Response(503))

    with pytest.raises(UpstreamUnavailable):
        await client.post_json("/charge", json={"amount": 100})

    assert len(calls) == 1, "a POST was retried, which is how a customer is charged twice"
    await client.aclose()


async def test_a_post_with_an_idempotency_key_is_retried() -> None:
    """The key is the caller stating the upstream will collapse duplicates,
    which makes the duplicate its problem rather than one we caused."""
    client, calls = client_for(httpx.Response(503), ok())

    await client.request("POST", "/charge", idempotency_key="abc-123", json={})

    assert len(calls) == 2
    await client.aclose()


async def test_attempts_are_bounded() -> None:
    client, calls = client_for(httpx.Response(503))

    with pytest.raises(UpstreamUnavailable):
        await client.get_json("/thing")

    assert len(calls) == FAST.attempts
    await client.aclose()


async def test_a_timeout_is_retried_and_typed() -> None:
    client, calls = client_for(httpx.ReadTimeout("too slow"))

    with pytest.raises(UpstreamTimeout):
        await client.get_json("/thing")

    assert len(calls) == FAST.attempts
    await client.aclose()


# --- Retry-After ------------------------------------------------------------


async def test_retry_after_beyond_the_cap_stops_rather_than_retrying_sooner() -> None:
    """Answering a 429 by retrying sooner than asked is how a rate limit
    becomes a ban. Past the cap the call fails instead."""
    client, calls = client_for(httpx.Response(429, headers={"retry-after": "3600"}))

    with pytest.raises(UpstreamUnavailable):
        await client.get_json("/thing")

    assert len(calls) == 1
    await client.aclose()


def test_retry_after_parsing() -> None:
    policy = RetryPolicy()

    assert retry_after("2", policy) == 2.0
    assert retry_after("99999", policy) is None, "past the cap"
    assert retry_after("-1", policy) is None
    assert retry_after("Wed, 21 Oct 2026 07:28:00 GMT", policy) is None, "date form is not read"
    assert retry_after("", policy) is None


# --- the breaker ------------------------------------------------------------


async def test_the_breaker_opens_and_then_makes_no_request() -> None:
    """The point of the breaker: the upstream gets to recover instead of being
    retried at by every replica simultaneously."""
    client, calls = client_for(httpx.Response(503))
    client.breaker.policy = BreakerPolicy(failure_threshold=1, recovery_seconds=60)

    with pytest.raises(UpstreamUnavailable):
        await client.get_json("/thing")

    before = len(calls)
    with pytest.raises(UpstreamUnavailable, match="circuit is open"):
        await client.get_json("/thing")

    assert len(calls) == before, "a socket was opened while the circuit was open"
    await client.aclose()


async def test_a_4xx_does_not_trip_the_breaker() -> None:
    """A 4xx is the upstream working correctly and refusing us. Shedding it
    would hide a bug in THIS service behind what looks like the other one's
    outage."""
    client, _ = client_for(httpx.Response(404))
    client.breaker.policy = BreakerPolicy(failure_threshold=1, recovery_seconds=60)

    for _ in range(3):
        with pytest.raises(UpstreamRejected):
            await client.get_json("/thing")

    assert client.breaker.is_open is False
    await client.aclose()


def test_the_breaker_probes_once_after_the_window() -> None:
    breaker = Breaker(policy=BreakerPolicy(failure_threshold=1, recovery_seconds=10))

    breaker.failed(now=0.0)
    assert breaker.allows(now=5.0) is False, "recovered early"
    assert breaker.allows(now=11.0) is True
    assert breaker.state is State.HALF_OPEN


def test_a_failed_probe_reopens_immediately() -> None:
    """Without this the breaker would need another full threshold of failures
    to reopen, so a dead upstream gets probed at the retry rate rather than the
    recovery rate."""
    breaker = Breaker(policy=BreakerPolicy(failure_threshold=5, recovery_seconds=10))
    breaker.state = State.HALF_OPEN

    breaker.failed(now=100.0)

    assert breaker.is_open is True
    assert breaker.opened_at == 100.0


def test_one_success_closes_a_half_open_breaker() -> None:
    breaker = Breaker(policy=BreakerPolicy(failure_threshold=2))
    breaker.failed()
    breaker.state = State.HALF_OPEN

    breaker.succeeded()

    assert breaker.state is State.CLOSED
    assert breaker.failures == 0


# --- headers ----------------------------------------------------------------


async def test_a_bearer_token_is_sent() -> None:
    captured: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return ok()

    client = service_client(
        Upstream(name="brain", base_url="http://brain", token="s3cret", retry=FAST),  # noqa: S106
        httpx.MockTransport(handle),
    )
    await client.get_json("/thing")

    assert captured["authorization"] == "Bearer s3cret"
    await client.aclose()


async def test_a_custom_authorizer_is_called_per_request() -> None:
    """Per request, not once, which is what lets an OAuth implementation
    refresh an expired token without this package knowing what OAuth is."""
    issued: list[str] = []

    async def authorize() -> dict[str, str]:
        issued.append(f"token-{len(issued)}")
        return {"Authorization": issued[-1]}

    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return ok()

    client = service_client(
        Upstream(name="vendor", base_url="http://vendor", authorizer=authorize, retry=FAST),
        httpx.MockTransport(handle),
    )
    await client.get_json("/a")
    await client.get_json("/b")

    assert seen == ["token-0", "token-1"]
    await client.aclose()


async def test_an_idempotency_key_is_sent_when_given() -> None:
    captured: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return ok()

    client = service_client(
        Upstream(name="brain", base_url="http://brain", retry=FAST), httpx.MockTransport(handle)
    )
    await client.request("POST", "/charge", idempotency_key="abc-123")

    assert captured["idempotency-key"] == "abc-123"
    await client.aclose()


# --- configuration ----------------------------------------------------------


async def test_an_unconfigured_upstream_fails_before_any_call() -> None:
    """Named, so the traceback says WHICH dependency is unset. In a service
    with four upstreams, "connection refused" costs ten minutes."""
    client = service_client(Upstream(name="brain", base_url=""))

    with pytest.raises(UpstreamUnavailable, match="brain"):
        await client.get_json("/thing")

    await client.aclose()


async def test_a_non_json_response_names_the_upstream() -> None:
    """An upstream answering 200 with HTML is a misrouted request or a captive
    portal, and a bare JSONDecodeError says nothing about which one."""
    client, _ = client_for(httpx.Response(200, text="<html>hello</html>"))

    with pytest.raises(UpstreamUnavailable, match="not JSON"):
        await client.get_json("/thing")

    await client.aclose()


def test_a_deadline_is_not_optional() -> None:
    """`timeout=None` must not be reachable. A call with no deadline holds a
    worker until the process dies, and it looks like slowness rather than a
    hang."""
    client = service_client(Upstream(name="brain", base_url="http://brain"))

    assert client._client.timeout.read is not None
    assert client._client.timeout.connect is not None


# --- the policy functions ---------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "OPTIONS"])
def test_idempotent_methods_may_be_retried(method: str) -> None:
    assert may_retry(method) is True


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_unsafe_methods_may_not_be(method: str) -> None:
    assert may_retry(method) is False
    assert may_retry(method, idempotency_key="k") is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, True), (502, True), (503, True), (504, True), (400, False), (404, False), (500, False)],
)
def test_which_statuses_are_retried(status: int, expected: bool) -> None:
    """500 is absent deliberately: it usually means the upstream threw on this
    specific request, so a retry reproduces it and spends the deadline."""
    assert retryable_status(status) is expected


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(backoff_seconds=1.0, backoff_cap_seconds=4.0)

    windows = [backoff(attempt, policy, jitter=1.0) for attempt in (1, 2, 3, 4)]

    assert windows == [1.0, 2.0, 4.0, 4.0]


def test_backoff_is_jittered_across_the_whole_window() -> None:
    """Full jitter, not a small random addition. Without spreading retries
    across the window, every replica retries at nearly the same instant and the
    upstream that wobbled gets a synchronised wall of traffic."""
    policy = RetryPolicy(backoff_seconds=1.0)

    assert backoff(1, policy, jitter=0.0) == 0.0
    assert backoff(1, policy, jitter=0.5) == 0.5


# --- the remaining verbs and failure shapes ---------------------------------


async def test_put_and_delete_go_through_the_same_path() -> None:
    """Both are idempotent, so both are retried, and both need the same
    headers. A verb that skipped this would skip the breaker too."""
    client, calls = client_for(httpx.Response(503), ok(), ok())

    assert await client.put_json("/thing", json={}) == {"ok": True}
    assert await client.delete_json("/thing") == {"ok": True}

    assert calls[0].startswith("PUT"), "PUT was not retried after a 503"
    assert len(calls) == 3
    await client.aclose()


async def test_a_500_is_not_retried_but_does_count_against_the_breaker() -> None:
    """Both halves matter. A 500 usually means the upstream threw on THIS
    request, so retrying reproduces it; but a service returning 500s is
    unhealthy, and not counting it would leave the breaker blind to the most
    common way an upstream fails.
    """
    client, calls = client_for(httpx.Response(500))
    client.breaker.policy = BreakerPolicy(failure_threshold=2, recovery_seconds=60)

    for _ in range(2):
        with pytest.raises(UpstreamRejected):
            await client.get_json("/thing")

    assert len(calls) == 2, "a 500 was retried"
    assert client.breaker.is_open is True
    await client.aclose()


async def test_a_refused_connection_is_unavailable_not_a_timeout() -> None:
    """Different fixes. A timeout means the deadline is too short or the
    dependency is slow; a refused connection means it is not there."""
    client, _ = client_for(httpx.ConnectError("refused"))

    with pytest.raises(UpstreamUnavailable) as raised:
        await client.get_json("/thing")

    assert not isinstance(raised.value, UpstreamTimeout)
    await client.aclose()


# --- policies refuse nonsense at construction -------------------------------


def test_a_policy_that_would_never_attempt_is_refused() -> None:
    """Zero attempts is a client that silently makes no calls, which presents
    as the upstream being down."""
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=0)


def test_a_zero_backoff_is_refused() -> None:
    """It would retry as fast as the loop allows, which is the thundering herd
    the backoff exists to prevent."""
    with pytest.raises(ValueError, match="backoff"):
        RetryPolicy(backoff_seconds=0)


def test_a_breaker_that_never_trips_is_refused() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        BreakerPolicy(failure_threshold=0)


def test_a_breaker_that_never_recovers_is_refused() -> None:
    with pytest.raises(ValueError, match="recovery_seconds"):
        BreakerPolicy(recovery_seconds=0)
