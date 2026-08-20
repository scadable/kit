"""The Resend adapter. Import from ``kit.email``, not here."""

from __future__ import annotations

from typing import Any, cast

from kit.clients import ServiceClient
from kit.email._message import Delivery, EmailMessage

PROVIDER = "resend"

SEND_PATH = "/emails"
"""Resend's send endpoint. The base URL lives on the Upstream, so a sandbox or a
proxy is configuration rather than a code change."""


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

        accepted = await self._client.post_json(
            SEND_PATH, json=payload, idempotency_key=idempotency_key
        )
        return Delivery(id=_accepted_id(accepted), provider=PROVIDER)


def _accepted_id(body: Any) -> str:
    """The provider's message id, or an empty string.

    Narrowed rather than trusted. A 200 whose body is not the object this
    expects is still an accepted message, so the send has succeeded and the
    receipt is merely unknown; raising here would turn a delivered email into a
    caller-visible failure and, if the caller retries, into two.
    """
    if not isinstance(body, dict):
        return ""

    identifier = cast("dict[str, Any]", body).get("id")
    return identifier if isinstance(identifier, str) else ""
