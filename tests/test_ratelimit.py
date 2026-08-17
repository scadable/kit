"""The limiter, including the two defects its design encodes."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from kit.health import Registry
from kit.httpapi import DEFAULT_RATE_LIMIT, RateLimit, install_conventions, unlimited
from kit.httpapi._ratelimit import (
    MAX_TRACKED_BUCKETS,
    Limiter,
    _Bucket,
    client_address,
    fingerprint,
)


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def limiter(limit: RateLimit, clock: Clock | None = None) -> Limiter:
    return Limiter(limit=limit, now=clock or Clock())


def build(limit: RateLimit) -> FastAPI:
    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=limit)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_an_unstated_policy_is_the_default_rather_than_no_limit() -> None:
    """A service that forgot to state a policy must be limited, because an unset
    field is exactly what forgetting looks like."""
    assert RateLimit().resolved() == DEFAULT_RATE_LIMIT
    assert unlimited().off is True


def test_a_partial_policy_gets_a_burst_equal_to_its_rate() -> None:
    resolved = RateLimit(requests=10, window_seconds=60).resolved()

    assert resolved.burst == 10


def test_a_burst_is_admitted_and_then_the_limit_refuses() -> None:
    lim = limiter(RateLimit(requests=60, window_seconds=60, burst=3))
    owed = lim.charges("", "1.2.3.4")

    assert all(lim.allow(owed)[0] for _ in range(3))
    allowed, retry_after, _ = lim.allow(owed)

    assert allowed is False
    assert retry_after >= 1, "zero reads as 'immediately' and makes a hot loop"


def test_a_rotated_credential_cannot_escape_its_address() -> None:
    """The first defect. Keying on an unverified header meant every distinct
    value minted a fresh identity with a full allowance, so a caller rotating
    garbage was not limited at all."""
    lim = limiter(RateLimit(requests=60, window_seconds=60, burst=2))

    refused = False
    for index in range(20):
        allowed, _, by_address = lim.allow(lim.charges(f"c:{fingerprint(str(index))}", "1.2.3.4"))
        if not allowed:
            refused = True
            assert by_address, "the address ceiling is what must stop this"
            break

    assert refused, "rotating credentials escaped the limit"


def test_a_full_table_still_admits_a_newcomer() -> None:
    """The second defect, and the worse one. Refusing at capacity handed the
    limiter to anybody who could fill it: mint keys until full, and every caller
    the limiter had not already seen was refused. Our own protection, aimed by an
    attacker, at a customer who did nothing.
    """
    clock = Clock()
    lim = limiter(RateLimit(requests=60, window_seconds=60, burst=5), clock)
    for index in range(MAX_TRACKED_BUCKETS):
        lim._buckets[f"filler:{index}"] = _Bucket(tokens=5.0, seen=clock.value)

    allowed, _, _ = lim.allow(lim.charges("", "brand.new.caller"))

    assert allowed is True
    assert len(lim._buckets) <= MAX_TRACKED_BUCKETS


def test_tokens_refill_over_time_but_never_past_the_burst() -> None:
    """Capping is what makes this a bucket rather than a bank: an identity that
    was quiet for a day may not spend a day's allowance at once."""
    clock = Clock()
    lim = limiter(RateLimit(requests=60, window_seconds=60, burst=2), clock)
    owed = lim.charges("", "1.2.3.4")

    lim.allow(owed)
    lim.allow(owed)
    assert lim.allow(owed)[0] is False

    clock.advance(1)
    assert lim.allow(owed)[0] is True

    clock.advance(86_400)
    assert all(lim.allow(owed)[0] for _ in range(2))
    assert lim.allow(owed)[0] is False, "an idle bucket banked more than its burst"


def test_a_forged_forwarded_header_cannot_mint_identities() -> None:
    """With one proxy declared, only the entry that proxy wrote is read.

    A proxy APPENDS what it saw, so entries the caller controls sit on the left.
    Reading those would let anybody mint unlimited identities by varying a
    header, which defeats the limiter completely and silently.
    """
    for forged in ("10.0.0.1, 203.0.113.9", "evil, 203.0.113.9", "203.0.113.9"):
        address = client_address({"x-forwarded-for": forged}, "peer", trusted_proxies=1)
        assert address == "203.0.113.9"


def test_the_header_is_ignored_when_no_proxy_is_declared() -> None:
    """The safe default. A process reached directly has no proxy to have written
    the header, so anything present was typed by whoever is calling."""
    assert client_address({"x-forwarded-for": "10.9.9.9"}, "192.0.2.10") == "192.0.2.10"


def test_the_peer_is_used_when_there_is_no_proxy() -> None:
    assert client_address({}, "192.0.2.10", trusted_proxies=1) == "192.0.2.10"
    assert client_address({"x-forwarded-for": "  "}, "192.0.2.10", 1) == "192.0.2.10"


def test_a_credential_is_never_the_key() -> None:
    """The identity is held in memory for the life of the process. A bearer token
    used directly would put a live credential in a heap dump."""
    secret = "Bearer sk-live-abcdef"  # noqa: S105

    identity = fingerprint(secret)

    assert secret not in identity
    assert "sk-live" not in identity
    assert identity == fingerprint(secret), "the same credential must be stable"


async def test_a_refusal_is_a_fully_formed_answer() -> None:
    """The whole argument for the limiter being innermost. A refusal must carry a
    request id, the security headers and the envelope, or a limit doing its job
    reads as a broken product."""
    app = build(RateLimit(requests=60, window_seconds=60, burst=1))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/thing")).status_code == 200
        refused = await client.get("/thing")

    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert refused.headers["x-request-id"]
    body = refused.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["request_id"] == refused.headers["x-request-id"]


async def test_unlimited_means_no_limiter_at_all() -> None:
    app = build(unlimited())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = [(await client.get("/thing")).status_code for _ in range(50)]

    assert set(statuses) == {200}
