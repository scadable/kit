"""What is safe to retry, and how long to wait before doing it.

Two decisions live here and both have a wrong answer that looks right.

WHICH METHODS. Only idempotent ones, by default: GET, HEAD, PUT, DELETE. A POST
that timed out may well have SUCCEEDED, with only the response lost, so retrying
it performs the operation twice. For a payment or an outbound email that is the
difference between one and two, and it is invisible from the caller's side. A
POST is retried only when the caller supplies an idempotency key, which makes
the duplicate the upstream's problem to collapse rather than ours to cause.

HOW LONG TO WAIT. Exponential with FULL JITTER. Without jitter every replica
retries on the same schedule, so an upstream that wobbled gets a synchronised
wall of traffic a fixed interval later, which is how a blip becomes an outage.
Full jitter, rather than a small random addition, because it spreads retries
across the whole window instead of clustering them at the end of it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from http import HTTPStatus

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
"""Safe to repeat by definition, per RFC 9110. POST and PATCH are absent
deliberately; see the module docstring."""

RETRYABLE_STATUS = frozenset(
    {
        HTTPStatus.TOO_MANY_REQUESTS,  # 429
        HTTPStatus.BAD_GATEWAY,  # 502
        HTTPStatus.SERVICE_UNAVAILABLE,  # 503
        HTTPStatus.GATEWAY_TIMEOUT,  # 504
    }
)
"""Nothing else, and in particular no other 4xx. A 400 or a 404 will fail
identically on every attempt, so retrying it spends the caller's deadline to
arrive at the same answer more slowly."""

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.1
DEFAULT_BACKOFF_CAP_SECONDS = 2.0
DEFAULT_RETRY_AFTER_CAP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times, how long between, and how much to believe an upstream."""

    attempts: int = DEFAULT_ATTEMPTS
    """TOTAL attempts, not retries after the first. Three means the original
    plus two, which is the number that has to fit inside the deadline."""

    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS

    retry_after_cap_seconds: float = DEFAULT_RETRY_AFTER_CAP_SECONDS
    """`Retry-After` is honoured and CAPPED. An upstream saying 3600 is not
    wrong, but obeying it parks a worker for an hour holding a request nobody is
    waiting for any more. Past the cap the call fails and the breaker learns
    from it, which is the more useful outcome."""

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.backoff_seconds <= 0 or self.backoff_cap_seconds <= 0:
            raise ValueError("backoff must be greater than zero")


def may_retry(method: str, *, idempotency_key: str = "") -> bool:
    """Whether repeating this request can be assumed safe.

    An idempotency key is the caller stating that the upstream will collapse
    duplicates. It is a promise about the OTHER service, which is why it has to
    be passed explicitly and cannot be inferred.
    """
    return method.upper() in IDEMPOTENT_METHODS or bool(idempotency_key)


def retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS


def backoff(attempt: int, policy: RetryPolicy, *, jitter: float | None = None) -> float:
    """Full jitter: a uniform draw from [0, exponential window].

    `attempt` is 1-based, so the wait after the first failure is drawn from the
    base window rather than from zero.
    """
    window = min(policy.backoff_seconds * (2 ** (attempt - 1)), policy.backoff_cap_seconds)
    fraction = random.random() if jitter is None else jitter  # noqa: S311
    return window * fraction


def retry_after(header: str, policy: RetryPolicy) -> float | None:
    """Seconds to wait per the upstream, or None when it did not say usefully.

    Only the delta-seconds form is read. The HTTP-date form is legal and rare,
    and parsing it means trusting the upstream's clock against ours; a skewed
    date produces a wait of either zero or a very long time, and neither failure
    announces itself.

    Returns None past the cap, which the caller treats as "stop", not as "wait
    the cap and try anyway".
    """
    try:
        seconds = float(header)
    except TypeError, ValueError:
        return None

    if seconds < 0 or seconds > policy.retry_after_cap_seconds:
        return None
    return seconds
