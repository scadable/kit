"""A circuit breaker, per upstream.

The problem it solves is not "the upstream is down". It is that RETRIES AMPLIFY
LOAD EXACTLY WHEN A DEPENDENCY IS WEAKEST. Three attempts against a service that
is struggling is four times the traffic at the worst possible moment, from every
replica at once, and it is how a dependency that was recovering does not.

Three states, and the third is the one that matters:

  closed    calls go through, failures counted
  open      calls fail immediately, no socket opened
  half open one call is allowed through to find out

Without half-open a breaker either never recovers or recovers by flooding. One
probe answers the question at the cost of one request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_RECOVERY_SECONDS = 30.0


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class BreakerPolicy:
    """When to give up on an upstream, and when to try again.

    The threshold counts CONSECUTIVE failures. A rate over a window would be
    more sophisticated and needs a window's worth of history to make its first
    decision, which on a low-traffic internal service can be an hour. Five in a
    row is unambiguous at any traffic level.
    """

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_seconds: float = DEFAULT_RECOVERY_SECONDS

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be greater than zero")


@dataclass(slots=True)
class Breaker:
    """One upstream's state. Not shared, not global, not thread-safe by design.

    A single event loop runs these, and the operations below are synchronous
    between awaits, so there is no interleaving to protect against. A lock here
    would be cargo cult and would serialise every outbound call in the process.
    """

    policy: BreakerPolicy = field(default_factory=BreakerPolicy)
    failures: int = 0
    opened_at: float = 0.0
    state: State = State.CLOSED

    def allows(self, now: float | None = None) -> bool:
        """Whether to attempt a call, moving to half-open when it is time.

        Reading this MUTATES the breaker, which is unusual enough to say out
        loud: the transition from open to half-open is driven by the clock, and
        there is no background task to drive it. The next caller after the
        recovery window is the probe.
        """
        if self.state is not State.OPEN:
            return True

        moment = time.monotonic() if now is None else now
        if moment - self.opened_at < self.policy.recovery_seconds:
            return False

        self.state = State.HALF_OPEN
        return True

    def succeeded(self) -> None:
        """Reset. One success closes a half-open breaker completely.

        Not a gradual reset. A half-open probe that succeeds is evidence the
        upstream is serving; keeping the breaker partly armed on that evidence
        would trip it again on the next unrelated blip.
        """
        self.failures = 0
        self.state = State.CLOSED

    def failed(self, now: float | None = None) -> None:
        """Count a failure, opening at the threshold.

        A failure while HALF OPEN reopens immediately regardless of the count.
        The probe was the question and it was answered.
        """
        moment = time.monotonic() if now is None else now

        if self.state is State.HALF_OPEN:
            self.failures = self.policy.failure_threshold
            self.state = State.OPEN
            self.opened_at = moment
            return

        self.failures += 1
        if self.failures >= self.policy.failure_threshold:
            self.state = State.OPEN
            self.opened_at = moment

    @property
    def is_open(self) -> bool:
        return self.state is State.OPEN
