"""The Resend adapter. Import from ``kit.email``, not here."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, cast

from kit.clients import ServiceClient, UpstreamRejected, UpstreamUnavailable
from kit.email._message import Delivery, EmailMessage

PROVIDER = "resend"

SEND_PATH = "/emails"
"""Resend's send endpoint. The base URL lives on the Upstream, so a sandbox or a
proxy is configuration rather than a code change."""

TRANSIENT_REFUSALS = frozenset({HTTPStatus.CONFLICT})
"""Statuses that arrive as a refusal and are worth trying again.

A 409 from Resend means another request carrying the same idempotency key is
still in flight. That is a race with our own retry, not a rejection of the
message, and the answer is to try again shortly rather than to give up on it.
"""


class ResendEmailer:
    """Sends through Resend, over a kit.clients ServiceClient.

    ON TOP OF ServiceClient RATHER THAN httpx DIRECTLY, and that inheritance is
    most of the value: the bounded deadline, the jittered backoff, the circuit
    breaker, the trace context and the readiness line all come with it and
    behave the same way under load as every other outbound call in the fleet.
    The vendor SDK is deliberately not a dependency; every dependency the kit
    takes, every service inherits and cannot refuse.

    Satisfies ``Emailer`` structurally and never imports it.
    """

    def __init__(self, client: ServiceClient, *, sender: str) -> None:
        """Take the client already built, and the address to send as.

        The sender is required here rather than defaulted, because the failure
        for an absent one is a 4xx per message at whatever hour the first
        invitation goes out. Refusing at construction moves it to boot, where a
        deploy fails and the previous version keeps serving.
        """
        if not sender:
            raise ValueError("a sender address is required: every message needs a From")
        self._client = client
        self._sender = sender

    async def send(self, message: EmailMessage, *, idempotency_key: str = "") -> Delivery:
        """Hand one message over, and return what Resend said it accepted.

        The key is passed through rather than invented here. kit.clients retries
        a POST only when one is present, so this is the line that decides
        whether a timed-out send is retried at all, and only the caller knows
        whether a second copy is worse than none.
        """
        payload: dict[str, Any] = {
            "from": message.sender or self._sender,
            "to": list(message.to),
            "subject": message.subject,
        }

        # Omitted rather than sent empty. Resend rejects an empty html body, and
        # a message carrying "html": "" is a text email that fails as if it were
        # malformed.
        if message.text:
            payload["text"] = message.text
        if message.html:
            payload["html"] = message.html
        if message.reply_to:
            payload["reply_to"] = message.reply_to

        try:
            accepted = await self._client.post_json(
                SEND_PATH, json=payload, idempotency_key=idempotency_key
            )
        except UpstreamRejected as refused:
            # kit.clients raises Rejected for every non-retryable status at or
            # above 400, which includes a 500. Left alone, this package's own
            # contract would call a provider outage permanent and a caller
            # following it would mark a recoverable send dead.
            if refused.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                raise UpstreamUnavailable(refused.upstream, f"status {refused.status}") from refused
            if refused.status in TRANSIENT_REFUSALS:
                raise UpstreamUnavailable(refused.upstream, f"status {refused.status}") from refused
            raise

        return Delivery(id=_accepted_id(accepted, self._client.upstream.name), provider=PROVIDER)


def _accepted_id(body: Any, upstream: str) -> str:
    """The provider's message id, or a transient failure.

    Resend names every message it accepts, so a 200 without an id is not a
    receipt we merely failed to read: it is something other than Resend
    answering, a proxy or a misrouted base URL, and the message probably never
    reached a provider at all. Reporting that as an accepted delivery is the
    worse failure of the two available, because the invitation never arrives and
    nothing anywhere says so.

    Transient rather than permanent, so a caller retries. A retry carrying the
    same idempotency key is the provider's to collapse if the message did in
    fact land.
    """
    if isinstance(body, dict):
        identifier = cast("dict[str, Any]", body).get("id")
        if isinstance(identifier, str) and identifier:
            return identifier

    raise UpstreamUnavailable(upstream, "the send endpoint answered without a message id")
