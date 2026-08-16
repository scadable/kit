"""The in-process rate limiter.

# What it protects

Every route, but the one that matters is an unauthenticated public read whose
each request costs a database read and an object-storage read. A limit there is
the difference between a document somebody fetches and a bill somebody runs up.

# The rule this file holds

An unverified credential may NARROW an allowance. It may never create one.

That rule exists because of two opposite defects. The first version keyed on the
Authorization header, which is not a credential at this point in the chain but a
string the caller chose: the limiter runs before anything verified it, so every
distinct value minted a distinct identity with a full allowance, and a caller
rotating garbage got a fresh bucket per request. That is not a limit.

The second half was worse. Those minted identities filled the bucket table, and
a full table REFUSED anybody it had not seen before. So the same rotation that
escaped the limit also turned the limiter into the denial of service it exists
to prevent, aimed by an attacker at whichever customer arrived next.

Both are fixed, in opposite directions: the limiter trusted something it could
not check, and distrusted somebody it had no reason to. In practice that is two
charges per request. A credentialed request is billed to its own bucket at the
configured rate AND to its address at a ceiling it cannot escape by changing a
header. An anonymous request is billed to its address once.

# Where it sits

Innermost, inside the request id, the security headers, the logger, CORS and the
recoverer. A refusal must carry a request id so a caller can name the log line,
carry the security headers so it is not the one response that ships without
them, be logged so a mistuned limit is not silent, and carry the CORS headers or
a browser refuses to expose it and a limit doing its job reads as a network
failure. The cost is that a flooded request pays for those first; those are
microseconds against the reads this prevents.

# What it gives up, stated rather than hidden

The store is in this process, so with N replicas the effective limit is N times
the configured one. A shared store would be exact and would add a failure mode
whose only safe handling is refusing every request while it is unreachable,
turning a cache blip into a total outage. This bounds what one abusive caller
can cost, not what a customer is entitled to.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_TRACKED_BUCKETS = 100_000
"""A bucket is a short key and two numbers, so this is a few megabytes.

Reaching it means a hundred thousand live buckets inside one window, which
honest traffic does not produce, so the state is far more likely to be somebody
minting keys to exhaust the table.
"""

EVICTION_SAMPLE = 8
"""How many buckets are examined to choose one to drop.

Exact least-recently-used needs a list threaded through the map, or a scan of
every entry on each admission while full, and a scan per request under a flood is
itself a denial of service. The oldest of a random eight is old enough.
"""

SESSION_COOKIE = "scadable_session"


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A service's limit policy.

    The zero value is not "no limit", it is :data:`DEFAULT_RATE_LIMIT`, because a
    service that forgot to state a policy must be limited rather than unlimited,
    and an unset field is exactly what forgetting looks like. Turning the limiter
    off is possible and has to be typed: :func:`unlimited` says so in a way
    nobody writes by accident.
    """

    requests: int = 0
    window_seconds: float = 0.0
    burst: int = 0
    shared_addresses: int = 0
    """How many callers this service expects behind ONE address.

    The only thing that widens an address ceiling. A portal's callers are signed
    in and an office of twenty behind one NAT is ordinary; a public read has no
    credential at all and widening it would simply be a bigger hole. Zero and one
    both mean exactly the stated rate, which is the safe default.

    What it must NEVER be is a function of whether a credential arrived. That was
    the defect: sending a meaningless header bought twenty times the allowance.
    """
    off: bool = False

    def resolved(self) -> RateLimit:
        limit = self
        if limit.requests <= 0 or limit.window_seconds <= 0:
            limit = DEFAULT_RATE_LIMIT
        if limit.burst <= 0:
            limit = RateLimit(
                requests=limit.requests,
                window_seconds=limit.window_seconds,
                burst=limit.requests,
                shared_addresses=limit.shared_addresses,
            )
        return limit

    def per_address(self) -> RateLimit:
        """The ceiling one address may not exceed however many credentials it
        presents."""
        fan_out = max(self.shared_addresses, 1)
        return RateLimit(
            requests=self.requests * fan_out,
            window_seconds=self.window_seconds,
            burst=self.burst * fan_out,
        )

    @property
    def per_second(self) -> float:
        return self.requests / self.window_seconds


DEFAULT_RATE_LIMIT = RateLimit(requests=60, window_seconds=60.0, burst=20)
"""What an unstated policy becomes.

Deliberately the most restrictive number any surface uses, because it is what a
service gets by NOT choosing, and choosing too low fails as a customer saying so
while choosing too high fails as an invoice nobody reads. Twenty at once covers a
page load; sixty a minute is one a second sustained, which no person types and
every script exceeds.
"""


def unlimited() -> RateLimit:
    """Say, on purpose, that this service wants no rate limit."""
    return RateLimit(off=True)


def fingerprint(value: str) -> str:
    """Identify a credential without holding one.

    The identity becomes a key held in memory for the life of the process. A
    bearer token used directly would put a live credential in a heap dump, in a
    profile, and in anything that ever prints this table. The hash identifies
    exactly as well and reveals nothing.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def client_address(headers: dict[str, str], peer: str) -> str:
    """The address the proxy observed, taken from the RIGHTMOST forwarded entry.

    A proxy APPENDS the address it saw to any inbound ``X-Forwarded-For``, so the
    leftmost entry is whatever the client typed. Reading it would let anybody
    mint unlimited identities by varying one header, which defeats the limiter
    completely and silently. The rightmost is the one the proxy wrote.
    """
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        nearest = forwarded.split(",")[-1].strip()
        if nearest:
            return nearest
    return peer


@dataclass(slots=True)
class _Bucket:
    tokens: float
    seen: float


@dataclass(slots=True)
class Limiter:
    """A token bucket table.

    A token bucket rather than a fixed-window counter, because a fixed window
    lets twice the limit through across a boundary: everything at the end of one
    window plus everything at the start of the next.
    """

    limit: RateLimit
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("kit.httpapi"))
    now: Callable[[], float] = time.monotonic
    """Injectable so tests can prove refill without sleeping."""
    _buckets: dict[str, _Bucket] = field(default_factory=dict[str, _Bucket])
    _address_limit: RateLimit = field(init=False)
    _swept_at: float = 0.0
    _reported_at: float = 0.0
    _evicted: int = 0

    def __post_init__(self) -> None:
        self.limit = self.limit.resolved()
        self._address_limit = self.limit.per_address()

    def charges(self, credential: str, address: str) -> list[tuple[str, RateLimit]]:
        """What this request owes.

        The address is charged at the same rate whether or not a credential
        arrived, and that is the correction. Billing an anonymous request tightly
        and a credentialed one loosely meant presenting any attacker-chosen
        header moved the address onto the wider ceiling, so on a route with no
        authentication a garbage header bought the fan-out allowance. A
        credential only ever ADDS a bucket that can refuse a request the address
        would have allowed.
        """
        on_address = (f"a:{address}", self._address_limit)
        if not credential:
            return [on_address]
        return [(credential, self.limit), on_address]

    def allow(self, owed: list[tuple[str, RateLimit]]) -> tuple[bool, int, bool]:
        """Spend one token from every bucket owed, or none.

        All or nothing on purpose. Spending from the first and then refusing on
        the second would let a caller drain a bucket with requests that were
        never served, and a refusal that still costs something is a refusal a
        retry makes worse.

        Returns ``(allowed, retry_after_seconds, refused_by_address)``.
        """
        now = self.now()
        self._sweep(now)

        buckets: list[_Bucket] = []
        for index, (key, limit) in enumerate(owed):
            bucket = self._refilled(key, limit, now)
            if bucket.tokens < 1:
                by_address = index > 0 or (len(owed) == 1 and key.startswith("a:"))
                return False, _retry_after(limit, bucket.tokens), by_address
            buckets.append(bucket)

        for bucket in buckets:
            bucket.tokens -= 1
        return True, 0, False

    def _refilled(self, key: str, limit: RateLimit, now: float) -> _Bucket:
        held = self._buckets.get(key)
        if held is None:
            if len(self._buckets) >= MAX_TRACKED_BUCKETS:
                self._evict_one(now)
            held = _Bucket(tokens=float(limit.burst), seen=now)
            self._buckets[key] = held
            return held

        elapsed = now - held.seen
        if elapsed > 0:
            # Capping is what makes this a bucket rather than a bank: an identity
            # that was quiet for a day may not spend a day's allowance at once.
            held.tokens = min(float(limit.burst), held.tokens + elapsed * limit.per_second)
        held.seen = now
        return held

    def _evict_one(self, now: float) -> None:
        """Make room, never refuse for lack of it.

        This used to refuse, which handed the whole limiter to anybody who could
        fill the table: mint keys until full, and from then on every caller the
        limiter had not already seen was refused. That is our own protection,
        aimed by an attacker, at a customer who did nothing.

        Eviction cannot be aimed the same way. The victim is the least recently
        seen bucket, and an attacker's own buckets are by definition the most
        recently seen, so a flood evicts the attacker's own stale keys first.
        """
        if not self._buckets:
            return
        # Sampled at random rather than taking the first few. Python dicts
        # iterate in insertion order, so a naive scan would always examine the
        # same oldest-inserted entries and make the victim predictable, which is
        # to say aimable.
        keys = list(self._buckets)
        sample = random.sample(keys, min(EVICTION_SAMPLE, len(keys)))
        victim = min(sample, key=lambda key: self._buckets[key].seen)
        del self._buckets[victim]
        self._report(now)

    def _report(self, now: float) -> None:
        """Say, at most once a window, that the table is full.

        A full table is otherwise invisible: the limiter keeps answering
        correctly and only its precision degrades, which is the kind of failure
        discovered months later by accident.
        """
        self._evicted += 1
        if now - self._reported_at < self.limit.window_seconds:
            return
        self._reported_at = now
        self.logger.warning(
            "rate limiter table is full, so buckets are being dropped to admit new callers",
            extra={
                "tracked": len(self._buckets),
                "evicted_since_last_report": self._evicted,
                "window_seconds": self.limit.window_seconds,
            },
        )
        self._evicted = 0

    def _sweep(self, now: float) -> None:
        """Drop buckets nobody has touched for long enough to be full anyway.

        Done inline on a write rather than on a background task, because a task
        started per application would outlive every test that built one and would
        have to be stopped by somebody remembering to.

        The threshold is twice the window: after one window an idle bucket has
        refilled completely, so it holds what a new one would and dropping it
        changes no decision. Twice is margin against the arithmetic.
        """
        if now - self._swept_at < self.limit.window_seconds:
            return
        self._swept_at = now
        idle = 2 * self.limit.window_seconds
        for key in [k for k, b in self._buckets.items() if now - b.seen > idle]:
            del self._buckets[key]


def _retry_after(limit: RateLimit, tokens: float) -> int:
    """Seconds until one token exists, rounded up, never zero.

    Zero reads as "immediately" and would turn a well-behaved client into a hot
    loop.
    """
    return max(1, math.ceil((1 - tokens) / limit.per_second))
