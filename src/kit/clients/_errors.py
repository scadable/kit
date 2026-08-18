"""Typed failures, so a caller cannot forget to check.

Every one of these NAMES THE UPSTREAM. A traceback saying "connection refused"
in a service that talks to four dependencies costs an incident responder the
first ten minutes working out which one, and that is the ten minutes that
matter.

They map onto the published vocabulary in `kit.httpapi`, but they do not carry a
status code. Which HTTP status a service renders an upstream failure as is a
decision for the API version doing the rendering, not for the transport.
"""

from __future__ import annotations


class UpstreamError(Exception):
    """Base for every outbound failure."""

    def __init__(self, upstream: str, detail: str = "") -> None:
        self.upstream = upstream
        self.detail = detail
        super().__init__(f"{upstream}: {detail}" if detail else upstream)


class UpstreamUnavailable(UpstreamError):
    """Could not be reached, refused the connection, or is being shed.

    Retrying LATER may work. Retrying now will not, which is what the breaker
    already decided.
    """


class UpstreamTimeout(UpstreamUnavailable):
    """Did not answer inside the deadline.

    A subclass, because to a caller deciding what to do it is the same class of
    problem, and separate because it is the one that means "we gave up" rather
    than "it said no". Those need different fixes: one is a deadline that is too
    short, the other is a dependency that is down.
    """


class UpstreamRejected(UpstreamError):
    """Answered, with a 4xx. THIS SERVICE sent something the upstream refused.

    Deliberately not retried and deliberately not counted against the breaker.
    A 4xx is the upstream working correctly, so shedding it would hide a bug in
    this service behind what looks like the other service's outage.
    """

    def __init__(self, upstream: str, status: int) -> None:
        self.status = status
        super().__init__(upstream, f"rejected the request with {status}")
