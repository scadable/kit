"""The registry's state machine, which is the easiest thing here to get subtly wrong."""

from __future__ import annotations

import asyncio

import pytest

from kit.health import Registry, Result, State, failing_check


async def ok() -> None:
    return None


async def test_ready_joins_every_failure() -> None:
    """With three dependencies down, an operator needs to see three, not one."""
    registry = Registry()
    registry.add("postgres", failing_check(RuntimeError("postgres unreachable")))
    registry.add("valkey", failing_check(RuntimeError("valkey unreachable")))
    registry.add("bucket", ok)

    failures = await registry.ready()

    assert len(failures) == 2
    assert any("postgres unreachable" in f for f in failures)
    assert any("valkey unreachable" in f for f in failures)
    assert not any("bucket" in f for f in failures)


async def test_an_empty_registry_is_ready() -> None:
    assert await Registry().ready() == []
    assert await Registry().check() == []


async def test_results_keep_registration_order() -> None:
    """Readiness output is read by a human comparing one response to the next
    during an outage, so a list that reshuffles is harder to compare."""
    registry = Registry()
    for name in ("postgres", "valkey", "bucket"):
        registry.add(name, ok)

    results = await registry.check()

    assert [r.name for r in results] == ["postgres", "valkey", "bucket"]


async def test_checks_run_concurrently() -> None:
    """Readiness is on the request path and three serial network probes would
    stack their timeouts."""
    running = 0
    peak = 0

    async def slow() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    registry = Registry()
    for name in ("a", "b", "c"):
        registry.add(name, slow)

    started = asyncio.get_running_loop().time()
    await registry.check()
    elapsed = asyncio.get_running_loop().time() - started

    assert peak == 3
    assert elapsed < 0.15, "checks ran serially"


async def test_a_check_that_raises_something_unexpected_is_an_error() -> None:
    """Fail closed. Letting an unexpected type escape would take the whole
    readiness response down and report nothing at all, which is the failure this
    package exists to prevent."""

    async def explodes() -> None:
        # Not the connection error a check means to raise: a bug in the check.
        raise TypeError("unsupported operand type")

    registry = Registry()
    registry.add("odd", explodes)

    results = await registry.check()

    assert results[0].state is State.ERROR


async def test_cancellation_is_never_swallowed() -> None:
    """A check is not allowed to absorb shutdown.

    CancelledError is a BaseException precisely so it propagates, and a registry
    that caught it would report a dependency as merely unhealthy while the
    process was trying to exit.
    """

    async def cancelled() -> None:
        raise asyncio.CancelledError

    registry = Registry()
    registry.add("shutting-down", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await registry.check()


async def test_a_check_that_hangs_times_out() -> None:
    async def hangs() -> None:
        await asyncio.sleep(10)

    registry = Registry(timeout_seconds=0.05)
    registry.add("slow", hangs)

    results = await registry.check()

    assert results[0].state is State.ERROR


async def test_registration_satisfies_a_declaration() -> None:
    registry = Registry()
    registry.require("auth")
    registry.add("auth", ok)

    results = await registry.check()

    assert len(results) == 1, "the placeholder was not deduplicated"
    assert results[0].state is State.READY


async def test_unregistered_declarations_are_sorted() -> None:
    """Deterministic output, so two readiness responses can be diffed."""
    registry = Registry()
    registry.require("valkey", "auth", "postgres")

    results = await registry.check()

    assert [r.name for r in results] == ["auth", "postgres", "valkey"]


async def test_an_optional_dependency_that_is_broken_still_blocks() -> None:
    """Absence is tolerated; being configured and failing is not. Otherwise
    "optional" would silence a real outage."""
    registry = Registry()
    registry.add_optional("valkey", failing_check(RuntimeError("down")))

    assert await registry.ready() != []


async def test_informational_is_reported_but_never_blocks() -> None:
    """One tenant's agent being down says nothing about whether this service can
    serve everyone else."""
    registry = Registry()
    registry.add_informational("harness", failing_check(RuntimeError("tenant down")))
    registry.add("postgres", ok)

    results = await registry.check()

    assert await registry.ready() == []
    assert results[0].state is State.ERROR
    assert results[0].informational is True


async def test_informational_does_not_mask_a_real_failure() -> None:
    registry = Registry()
    registry.add_informational("harness", failing_check(RuntimeError("tenant")))
    registry.add("postgres", failing_check(RuntimeError("real")))

    failures = await registry.ready()

    assert len(failures) == 1
    assert "postgres" in failures[0]


@pytest.mark.parametrize(
    ("name", "check", "reason"),
    [
        ("", ok, ""),
        ("no-check", None, ""),
    ],
)
async def test_a_registration_carrying_no_information_is_dropped(
    name: str, check: object, reason: str
) -> None:
    """A missing check with no reason would report a dependency as ready without
    ever probing it."""
    registry = Registry()
    if check is None:
        registry.add_unconfigured(name or "no-check", reason)
    else:
        registry.add(name, ok)

    assert await registry.check() == []


def test_a_hand_built_result_with_an_error_is_not_silently_healthy() -> None:
    """Without inference, such a result carries an error and is silently NOT
    blocking, which is a quieter version of the bug three states fixed."""
    carrying_error = Result(name="postgres", error=RuntimeError("boom"))

    assert carrying_error.effective is State.ERROR
    assert carrying_error.blocking is True
    assert carrying_error.ready is False

    clean = Result(name="valkey")
    assert clean.effective is State.READY
    assert clean.blocking is False
