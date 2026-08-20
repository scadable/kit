"""The message, the receipt, and the port. Import from ``kit.email``, not here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One email, in the terms every provider agrees on.

    Deliberately small. Templates, tags, scheduling, attachments and provider
    template ids are all absent, because those are exactly the fields that
    differ between providers and the ones that would quietly re-couple a service
    to the vendor it thinks it is abstracted from. A service renders its own
    copy to text and html and hands over finished content.

    TWO BODIES, NOT ONE. Sending html alone is a message that reads as a blank
    in a text-only client and scores worse with spam filters; sending text alone
    is a link nobody can click comfortably. Requiring at least one and accepting
    both is the honest middle.

    No address validation here. A malformed address is a 4xx from the provider,
    which arrives as UpstreamRejected and is already the permanent-failure half
    of the split. A regex in this file would be a second, worse opinion about
    what an address is, and it would be the one that rejects a valid one.
    """

    to: tuple[str, ...]
    subject: str
    text: str = ""
    html: str = ""
    sender: str = ""
    """Overrides the adapter's default sender. Empty means use it."""

    reply_to: str = ""

    def __post_init__(self) -> None:
        """Refuse a message no provider could accept.

        At construction rather than at send, so the failure lands on the line
        that built it instead of inside an adapter three frames away, and so a
        test of the caller catches it without a transport.
        """
        if not self.to:
            raise ValueError("an email needs at least one recipient")
        if not self.subject:
            raise ValueError("an email needs a subject")
        if not self.text and not self.html:
            raise ValueError("an email needs a text body, an html body, or both")


@dataclass(frozen=True, slots=True)
class Delivery:
    """What the provider said when it accepted the message.

    ACCEPTED, NOT DELIVERED, and the distinction is the whole reason this type
    is not a bool. The provider has taken custody and will retry, bounce and
    suppress on its own schedule; none of that has happened yet when this is
    returned. Treating it as proof of delivery is how "the email says it sent"
    turns into an argument with a customer.

    ``id`` is the provider's handle for the message, which is what a support
    question is answered with. It can be empty if a provider accepts without
    naming one, so it is a string rather than an Optional: absent and unknown
    are the same fact here and two spellings of it would both need handling.
    """

    id: str
    provider: str


class Emailer(Protocol):
    """What a service depends on, so it never names a vendor.

    A Protocol rather than a base class, so an adapter never imports this module
    to satisfy it. Structural typing means the port and the adapter never meet in
    an import graph, which is what lets a service declare the shape it needs in
    its own domain layer, where the import contract allows nothing but the
    standard library.
    """

    async def send(self, message: EmailMessage, *, idempotency_key: str = "") -> Delivery:
        """Hand one message to the provider.

        Raises ``UpstreamRejected`` when the provider refused it, which is
        permanent, and ``UpstreamUnavailable`` when it could not be reached,
        which is not. See the package docstring for why the caller has to tell
        those apart.
        """
        ...
