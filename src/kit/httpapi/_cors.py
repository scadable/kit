"""Cross-origin access, in two mutually exclusive modes.

Mode one is a credentialed ALLOW-LIST and never a wildcard. Two reasons, and the
second is the one that matters. A browser rejects ``Access-Control-Allow-Origin:
*`` outright when the request carries credentials, so a wildcard would simply not
work. And reflecting whatever ``Origin`` arrives, which is the usual shortcut,
means any website on the internet can drive the API as whoever is logged in:
their cookie rides along and the attacker's page reads the response. That is the
whole class of attack CORS exists to prevent, reintroduced by the convenience of
not maintaining a list.

Mode two, ``public_read``, emits exactly the wildcard the paragraph above
refuses, and that is not a contradiction. Read what the argument is ABOUT: both
halves concern a CREDENTIALED API. Take the credential away and both evaporate.
A public document read is unauthenticated, accepts no cookie, and its whole
distribution model is customer websites embedding a document their owner
published, which is cross-origin by definition and from an origin set we neither
know nor may decide. An allow-list there is not a stricter version of the same
thing, it is a list of which of our customers may show their own policy.

So they are DISJOINT modes rather than a dial, and public read never sends
``Access-Control-Allow-Credentials``. That absence is what makes the wildcard
both browser-legal and safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_AGE_SECONDS = 600
"""Long enough to matter for a chatty UI, short enough that changing the
allow-list takes effect the same day."""

CREDENTIALED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
CREDENTIALED_HEADERS = "Authorization, Content-Type"

PUBLIC_READ_METHODS = "GET, HEAD, OPTIONS"
PUBLIC_READ_HEADERS = "If-None-Match"

EXPOSED_HEADERS = "Retry-After, X-Request-ID"
"""Response headers this chain writes that a cross-origin caller must be able to
read.

Only a handful are readable cross-origin by default and neither of these is
among them. ``Retry-After`` is the entire actionable content of a 429: without
it a client can see that it was refused and not for how long, so it either
retries immediately or invents a number. ``X-Request-ID`` is what a customer
quotes when reporting a failure, and a support conversation that cannot name the
log line is the failure this header exists to prevent.
"""


def normalize_origins(origins: list[str]) -> set[str]:
    """Trim whitespace, drop one trailing slash, discard empties.

    A service reading a comma-separated variable that happens to be empty hands
    over a one-element list holding an empty string. That configures no origin,
    so it is not a contradiction, and treating it as one would make an unset
    environment variable crash a correctly configured service.
    """
    cleaned: set[str] = set()
    for origin in origins:
        trimmed = origin.strip().removesuffix("/").strip()
        if trimmed:
            cleaned.add(trimmed)
    return cleaned


@dataclass(frozen=True, slots=True)
class CORS:
    """Which cross-origin mode this service runs, if any."""

    allowed_origins: list[str] = field(default_factory=list[str])
    public_read: bool = False

    def __post_init__(self) -> None:
        if self.public_read and normalize_origins(self.allowed_origins):
            # Raising here is intended. This runs once, at construction, before
            # the process serves anything, so a service configured into a
            # contradiction fails to start rather than serving whichever mode
            # happened to win. Choosing one silently is worse in both
            # directions: choosing the allow-list leaves every customer embed
            # broken in a browser with a green deployment, and choosing the
            # wildcard quietly widens a credentialed API, which is the mistake
            # CORS exists to prevent.
            raise ValueError(
                "public_read and allowed_origins are mutually exclusive. "
                "public_read answers every origin with a wildcard and no "
                "credentials; allowed_origins answers named origins with "
                "credentials. A surface that needs both is two surfaces."
            )

    @property
    def enabled(self) -> bool:
        return self.public_read or bool(normalize_origins(self.allowed_origins))
