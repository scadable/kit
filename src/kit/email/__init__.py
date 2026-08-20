"""Outbound email: one port, one adapter, and no vendor name in the caller.

WHY THIS IS IN THE KIT AT ALL, given that "code moves in on its third copy".
This is the kit.clients case rather than the database-helpers case. It is not an
abstraction over a domain, it is delivery policy plus a provider swap, and both
are wrong in the same way in every service that writes them alone: a second
timeout, a second retry opinion, a second answer to "did that send twice".

The boundary that keeps that true is what this package refuses to hold.
Templates, rendering, and which person receives which email are the service's,
and they are the parts that differ. What is here is only what every provider
agrees on: recipients, a subject, two bodies, a reply-to, and a receipt.

CONTRACT, not overridable: the shape of EmailMessage and Delivery, and the
meaning of the two failures below. A service that invents its own message type
cannot be handed to a different adapter, which is the one thing this exists for.

POLICY, yours: the sender address, the deadline, the retry counts and the
breaker thresholds, all of which are parameters on the Upstream you construct.

THE FAILURE SPLIT IS THE POINT. Sending is the one call where "try again" and
"never try again" are opposite, and kit.clients already distinguishes them:

    UpstreamRejected      a 4xx. A malformed or suppressed address. PERMANENT,
                          and retrying it for a week cannot help.
    UpstreamUnavailable   a 5xx, a timeout, or an open breaker. TRANSIENT, and
                          worth trying again later.

A caller that collapses those either retries bad addresses forever or drops
recoverable sends, and both read from outside as "email is flaky".

EXACTLY ONCE IS THE CALLER'S TO ASK FOR. A POST that timed out may well have
been delivered, so kit.clients does not retry one without an idempotency key.
Pass a key that is stable for the message and the duplicate becomes the
provider's to collapse rather than ours to cause; omit it and a timeout is a
send that is not retried at all. There is no third option, and the choice
belongs to whoever knows whether a second copy is worse than none.
"""

from kit.email._message import Delivery, Emailer, EmailMessage
from kit.email._resend import ResendEmailer

__all__ = ["Delivery", "EmailMessage", "Emailer", "ResendEmailer"]
