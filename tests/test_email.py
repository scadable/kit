"""Outbound email, against a stub provider.

httpx.MockTransport, so the real retry, breaker and header code runs with no
socket and no message ever leaves the machine. The failures worth pinning here
are the ones that only show up when a provider is slow, refusing, or answering
something other than what was expected.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kit.clients import RetryPolicy, Upstream, UpstreamRejected, UpstreamUnavailable, service_client
from kit.email import Delivery, EmailMessage, ResendEmailer

FAST = RetryPolicy(attempts=3, backoff_seconds=0.001, backoff_cap_seconds=0.002)

SENDER = "SCADABLE <notifications@scadable.com>"

API_KEY = "re_not_a_real_key"


def message(**overrides: Any) -> EmailMessage:
    fields: dict[str, Any] = {
        "to": ("person@example.com",),
        "subject": "You have been invited",
        "text": "Join Acme.",
    }
    fields.update(overrides)
    return EmailMessage(**fields)


def emailer_for(
    *responses: httpx.Response | Exception, sender: str = SENDER
) -> tuple[ResendEmailer, list[httpx.Request]]:
    """An emailer whose provider answers with each response in turn."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    client = service_client(
        Upstream(name="resend", base_url="https://api.resend.com", token=API_KEY, retry=FAST),
        httpx.MockTransport(handle),
    )
    return ResendEmailer(client, sender=sender), seen


def accepted(identifier: object = "msg_1") -> httpx.Response:
    return httpx.Response(200, json={"id": identifier})


def body_of(request: httpx.Request) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(request.content)
    return decoded


# --- the happy path ---------------------------------------------------------


async def test_a_message_becomes_the_payload_resend_expects() -> None:
    emailer, seen = emailer_for(accepted())

    delivery = await emailer.send(
        message(to=("a@example.com", "b@example.com"), html="<p>Join Acme.</p>")
    )

    assert delivery == Delivery(id="msg_1", provider="resend")
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/emails"
    assert body_of(seen[0]) == {
        "from": SENDER,
        "to": ["a@example.com", "b@example.com"],
        "subject": "You have been invited",
        "text": "Join Acme.",
        "html": "<p>Join Acme.</p>",
    }


async def test_an_absent_body_half_is_omitted_rather_than_sent_empty() -> None:
    """Resend refuses an empty html body, so a text-only message carrying
    "html": "" fails as if it were malformed."""
    emailer, seen = emailer_for(accepted())

    await emailer.send(message())

    assert "html" not in body_of(seen[0])
    assert "reply_to" not in body_of(seen[0])


async def test_an_html_only_message_sends_no_text_field() -> None:
    """The other half of the omission: html alone is a legitimate message, and
    a "text": "" beside it is the same malformed-body failure in reverse."""
    emailer, seen = emailer_for(accepted())

    await emailer.send(message(text="", html="<p>Join Acme.</p>"))

    sent = body_of(seen[0])
    assert "text" not in sent
    assert sent["html"] == "<p>Join Acme.</p>"


async def test_reply_to_is_sent_when_set() -> None:
    emailer, seen = emailer_for(accepted())

    await emailer.send(message(reply_to="support@example.com"))

    assert body_of(seen[0])["reply_to"] == "support@example.com"


async def test_a_message_may_override_the_default_sender() -> None:
    emailer, seen = emailer_for(accepted())

    await emailer.send(message(sender="billing@scadable.com"))

    assert body_of(seen[0])["from"] == "billing@scadable.com"


# --- exactly once -----------------------------------------------------------


async def test_an_idempotency_key_is_forwarded_to_the_provider() -> None:
    emailer, seen = emailer_for(accepted())

    await emailer.send(message(), idempotency_key="invite_7")

    assert seen[0].headers["Idempotency-Key"] == "invite_7"


async def test_a_send_without_a_key_is_not_retried() -> None:
    """A POST that timed out may already have been delivered, so retrying it
    without a key is how one invitation becomes two."""
    emailer, seen = emailer_for(httpx.Response(503), accepted())

    with pytest.raises(UpstreamUnavailable):
        await emailer.send(message())

    assert len(seen) == 1


async def test_a_send_with_a_key_is_retried() -> None:
    emailer, seen = emailer_for(httpx.Response(503), accepted())

    delivery = await emailer.send(message(), idempotency_key="invite_7")

    assert delivery.id == "msg_1"
    assert len(seen) == 2


# --- the failure split ------------------------------------------------------


async def test_a_refused_address_is_permanent() -> None:
    emailer, seen = emailer_for(httpx.Response(422, json={"message": "invalid to field"}))

    with pytest.raises(UpstreamRejected) as refused:
        await emailer.send(message(), idempotency_key="invite_7")

    assert refused.value.status == 422
    assert len(seen) == 1, "a 4xx is the provider working correctly and must not be retried"


async def test_an_unreachable_provider_is_transient() -> None:
    emailer, _ = emailer_for(httpx.ConnectError("no route"))

    with pytest.raises(UpstreamUnavailable):
        await emailer.send(message(), idempotency_key="invite_7")


# --- the receipt ------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"nothing": "useful"}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"id": 7}),
    ],
)
async def test_an_unexpected_body_still_counts_as_accepted(response: httpx.Response) -> None:
    """The message was taken. Raising here would turn a delivered email into a
    caller-visible failure, and into two if the caller then retries."""
    emailer, _ = emailer_for(response)

    delivery = await emailer.send(message())

    assert delivery == Delivery(id="", provider="resend")


# --- refusing what no provider could accept ---------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"to": ()}, "recipient"),
        ({"subject": ""}, "subject"),
        ({"text": "", "html": ""}, "body"),
    ],
)
def test_a_message_no_provider_could_accept_is_refused_at_construction(
    overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        message(**overrides)


def test_an_emailer_without_a_sender_is_refused_at_construction() -> None:
    """The failure for an absent From is a 4xx per message at whatever hour the
    first one goes out. This moves it to boot."""
    with pytest.raises(ValueError, match="sender"):
        emailer_for(accepted(), sender="")
