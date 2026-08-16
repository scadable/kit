"""The readiness registry: dependency checks combined into one verdict.

Dependencies are declared with ``require()`` BEFORE anything is wired, so a
dependency that is never wired reports "unconfigured" by construction instead of
vanishing from the report. Success-only registration is forbidden: an empty
checks object must never be able to look healthy.

Three states, never two: ready, error, unconfigured. With only ready and failed,
a service whose dependency is absent has nothing truthful to register, so it
registers NOTHING, the dependency disappears, and an empty report is
indistinguishable from a verified-healthy one.

That is not hypothetical. Services in the previous generation answered readiness
with ``{"status": "ready", "checks": {}}`` while being unable to do the one thing
they existed to do, and two of them were doing exactly that in production.
"""

from kit.health._registry import Registry, Result, State, failing_check

__all__ = ["Registry", "Result", "State", "failing_check"]
