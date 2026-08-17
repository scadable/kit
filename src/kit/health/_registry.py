"""Registry internals. Import from ``kit.health``, not from here."""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

# A check answers with None when the dependency is reachable, or raises.
Check = Callable[[], Awaitable[None]]

DEFAULT_TIMEOUT_SECONDS = 5.0
"""How long one check may take.

Sized for a COLD connection, not a warm one. Measured against managed clusters
in tor1: Postgres 1.78s, Valkey 1.01s, object storage 0.64s on first contact,
dominated by the TLS handshake. A warm pool answers in milliseconds, but the
first check after boot is always cold, and so is the first after connections are
recycled, so a 1s budget reported healthy systems as unready. Checks run
concurrently, so this is the total rather than the sum.
"""

_NEVER_REGISTERED = "declared required at startup but no check was ever registered"


class State(enum.StrEnum):
    """One dependency's readiness outcome."""

    READY = "ready"
    """Probed, and it answered."""

    ERROR = "error"
    """Configured, and it did not answer."""

    UNCONFIGURED = "unconfigured"
    """Nothing to probe. Whether that is a failure depends on whether it was required."""


def failing_check(error: Exception) -> Check:
    """Turn a setup error into a probe that always reports it.

    Services fail soft when a backing system cannot be opened. Keeping that
    startup failure in the registry is what stops the dependency vanishing and
    recreating the green readiness report that once concealed an outage.
    """

    async def check() -> None:
        raise error

    return check


@dataclass(frozen=True, slots=True)
class Result:
    """One dependency's outcome.

    ``error`` and ``reason`` travel to the transport layer and stop there. They
    are logged, never serialized: readiness is unauthenticated at the ingress and
    driver errors routinely embed internal hostnames, addresses and ports.
    """

    name: str
    state: State | None = None
    error: Exception | None = None
    reason: str = ""
    required: bool = False
    informational: bool = False

    @property
    def effective(self) -> State:
        """The state, inferring one when it was not set.

        A ``Result`` built by hand, which tests do, has no state. Without this
        inference such a result carries an error and is silently NOT blocking,
        which is a quieter version of the exact bug three states were added to
        fix.
        """
        if self.state is not None:
            return self.state
        return State.ERROR if self.error is not None else State.READY

    @property
    def ready(self) -> bool:
        return self.effective is State.READY

    @property
    def blocking(self) -> bool:
        """Whether this result makes the service unready.

        An optional dependency that is simply absent is visible but not fatal. A
        required one that is absent is fatal, and that asymmetry is the point.
        Without it, marking something unconfigured becomes an escape hatch for
        turning a red deployment green.

        A FAILING optional dependency still blocks. Its absence is tolerated;
        its being configured and broken is not.
        """
        # Whose outage is it? An informational dependency belongs to a tenant, so
        # its state is reported and this service's verdict is unaffected.
        if self.informational:
            return False
        if self.effective is State.ERROR:
            return True
        if self.effective is State.UNCONFIGURED:
            return self.required
        return False


@dataclass(slots=True)
class _Entry:
    name: str
    check: Check | None = None
    required: bool = False
    informational: bool = False
    reason: str = ""


@dataclass(slots=True)
class Registry:
    """Dependency checks, combined into one readiness verdict.

    The zero value is usable: ``Registry()`` needs no arguments.
    """

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    _entries: list[_Entry] = field(default_factory=list[_Entry])
    _required: dict[str, str] = field(default_factory=dict[str, str])

    def require(self, *names: str) -> None:
        """Declare dependencies this service cannot work without, BEFORE wiring.

        This addresses the root cause rather than one instance of it. Every
        silent gap of this kind has the same shape: registration sits inside the
        success branch, so the failure path returns early and the dependency
        disappears from the report entirely. Declaring the name up front means a
        dependency that never gets a check attached resolves to unconfigured BY
        CONSTRUCTION, so forgetting to register something stops being the same as
        not needing it.

        There is deliberately nothing to seal. Declarations resolve on every
        call, so there is no ordering rule to forget.
        """
        for name in names:
            if name:
                self._required[name] = _NEVER_REGISTERED

    def add(self, name: str, check: Check) -> None:
        """Register a required dependency.

        Required is the default because the safe mistake is a service reporting
        itself unready when it is fine, not one reporting itself ready when it
        cannot serve.
        """
        self._add(_Entry(name=name, check=check, required=True))

    def add_optional(self, name: str, check: Check) -> None:
        """Register a dependency whose absence is tolerable but whose failure is not."""
        self._add(_Entry(name=name, check=check))

    def add_unconfigured(self, name: str, reason: str) -> None:
        """Record a required dependency that has nothing to probe."""
        self._add(_Entry(name=name, required=True, reason=reason))

    def add_optional_unconfigured(self, name: str, reason: str) -> None:
        """Record an optional dependency that has nothing to probe."""
        self._add(_Entry(name=name, reason=reason))

    def add_informational(self, name: str, check: Check) -> None:
        """Register a dependency whose failure belongs to a tenant, not to this service.

        This exists for one shape and should not be used for anything else: a
        dependency whose failure affects a SUBSET OF TENANTS rather than this
        service. One customer's agent being down is a real problem for that
        customer and says nothing about whether this service can serve everyone
        else, so blocking on it would take a healthy shared service unready
        because a single tenant's machine is rebooting.

        It is deliberately not a softer ``add_optional``. Optional already means
        "absence is fine, but configured-and-broken is a fault". The distinction
        here is not how much we care, it is WHOSE outage it is.

        The obvious abuse is turning a red deployment green by demoting a
        genuinely required dependency. Two things make that visible: the state
        still appears in the report exactly as it is, and the reason a dependency
        is per-tenant has to be written at the registration site.
        """
        self._add(_Entry(name=name, check=check, informational=True))

    def add_informational_unconfigured(self, name: str, reason: str) -> None:
        """Record a per-tenant dependency that has nothing to probe."""
        self._add(_Entry(name=name, informational=True, reason=reason))

    def _add(self, entry: _Entry) -> None:
        # A missing check with no reason carries no information, and registering
        # it would report a dependency as ready without ever probing it.
        if not entry.name or (entry.check is None and not entry.reason):
            return
        # One entry per name. The wire shape is a map, so a duplicate collapses
        # there while the verdict still counts both, which produces a response
        # saying not_ready with every listed dependency reading ready. Replacing
        # is the honest resolution: the later registration is the one the author
        # meant, and both surviving means the report contradicts itself.
        for index, existing in enumerate(self._entries):
            if existing.name == entry.name:
                self._entries[index] = entry
                return
        self._entries.append(entry)

    async def check(self) -> list[Result]:
        """Run every check concurrently, one result per dependency.

        Order is registration order, then any declared-required dependency that
        was never registered, sorted. Readiness output is read by humans
        diagnosing an outage, and a list that reshuffles between requests is
        harder to compare.
        """
        entries = list(self._entries)
        resolved: list[_Entry] = []
        unregistered = dict(self._required)
        for entry in entries:
            if entry.name in unregistered:
                unregistered.pop(entry.name)
                # A declaration cannot be demoted by how the dependency was
                # later registered. Without this, require("db") followed by
                # add_optional("db", ...) removes the declaration AND leaves a
                # non-blocking entry, so a dependency the service said it cannot
                # work without silently stops blocking. That is the exact
                # failure declaring dependencies up front exists to prevent, and
                # it would be invisible: readiness would stay green.
                entry = replace(entry, required=True, informational=False)
            resolved.append(entry)

        results: list[Result] = await asyncio.gather(*(self._run(entry) for entry in resolved))

        return results + [
            Result(
                name=name,
                state=State.UNCONFIGURED,
                reason=reason,
                required=True,
            )
            for name, reason in sorted(unregistered.items())
        ]

    async def _run(self, entry: _Entry) -> Result:
        if entry.check is None:
            return Result(
                name=entry.name,
                state=State.UNCONFIGURED,
                reason=entry.reason,
                required=entry.required,
                informational=entry.informational,
            )

        try:
            async with asyncio.timeout(self.timeout_seconds):
                await entry.check()
        except Exception as error:
            # Fail closed. Any exception, including a timeout and including one
            # the check never meant to raise, is a dependency that did not
            # answer. Letting an unexpected type escape would take the whole
            # readiness response down and report nothing at all.
            return Result(
                name=entry.name,
                state=State.ERROR,
                error=error,
                required=entry.required,
                informational=entry.informational,
            )

        return Result(
            name=entry.name,
            state=State.READY,
            required=entry.required,
            informational=entry.informational,
        )

    async def ready(self) -> list[str]:
        """Every blocking failure, as ``"name: detail"`` lines. Empty means ready.

        Joins EVERY blocking failure rather than the first. With three
        dependencies down, an operator needs to see three, not one.
        """
        failures: list[str] = []
        for result in await self.check():
            if not result.blocking:
                continue
            detail = str(result.error) if result.error is not None else result.reason
            failures.append(f"{result.name}: {detail}")
        return failures
