"""The liveness and readiness endpoints.

They live here rather than in each service because the shape of their answer is
a cross-service contract, not a per-service detail. The ops console reads
readiness across the whole fleet to tell one service's database outage from
another's cache outage, and that only works if every service answers in the same
shape. The fastest way to lose that property is for seven services to each own a
copy of this handler and drift apart one small change at a time.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from kit.health import Registry, State
from kit.httpapi._envelope import JSON_CONTENT_TYPE, StatusResponse, request_id


class ReadinessResponse(BaseModel):
    """The overall verdict plus one entry per dependency.

    ``checks`` carries a status word only, never the underlying error: readiness
    is unauthenticated at the ingress, and driver errors routinely embed internal
    hostnames, private addresses and ports. The full error goes to the log,
    keyed by the same request id.

    ``checks`` is always present, never omitted. An empty object says "asked,
    nothing is registered", which is a different and useful fact from a missing
    key, and a missing key is indistinguishable from an old build still being
    deployed.
    """

    status: str = Field(examples=["ready"])
    checks: dict[str, str] = Field(default_factory=dict)


def probe_router(
    registry: Registry,
    *,
    logger: logging.Logger | None = None,
) -> APIRouter:
    """Build ``/healthz`` and ``/readyz``.

    Mount at the application root. These are never versioned: they are the
    contract with the orchestrator, not with a customer, and a probe path that
    moved with an API version would need the deployment updated in lockstep.
    """
    log = logger or logging.getLogger("kit.httpapi")
    # Last state logged per dependency, so /readyz reports transitions rather
    # than repeating steady state on every kubelet poll. Per router, which means
    # per app, which means per process: a replica that restarts re-establishes
    # its own baseline and logs what it finds once.
    logged: dict[str, tuple[State, str, str]] = {}

    router = APIRouter(include_in_schema=False)

    @router.get("/healthz")
    async def healthz() -> StatusResponse:  # pyright: ignore[reportUnusedFunction]
        """Liveness. Touches NO dependency.

        A failing liveness probe restarts the container, so a database blip must
        not be able to reach this. The only question it answers is whether this
        process is alive enough to serve.
        """
        return StatusResponse(status="ok")

    @router.get("/readyz")
    async def readyz() -> Response:  # pyright: ignore[reportUnusedFunction]
        """Readiness. 200 when every required dependency answered, else 503.

        The status code carries the verdict because a kubelet readiness probe
        reads the status code and nothing else: a handler that always answered
        200 would never be taken out of the Service endpoints, which is the
        entire purpose of having this endpoint separate from liveness.

        The body carries the detail, because a status code cannot say WHICH
        dependency is down and that is what an operator needs at 3am.
        """
        results = await registry.check()

        checks: dict[str, str] = {}
        ready = True
        for result in results:
            checks[result.name] = str(result.effective)
            if result.blocking:
                ready = False

            # ON TRANSITION ONLY, never once per probe. A kubelet polls /readyz
            # every few seconds forever, so logging steady state here emits the
            # same warning thousands of times a day per replica. That is not a
            # louder signal, it is a quieter one: the line that matters (a
            # dependency that JUST broke) is buried in identical copies of a
            # line that has been true since boot, and it is billed by the
            # gigabyte in the log store.
            #
            # Keyed by dependency, holding the last state and reason we logged,
            # so a flap from ready to error to ready logs both edges while a
            # dependency sitting unconfigured since startup logs exactly once.
            previous = logged.get(result.name)
            current = (result.effective, result.reason, str(result.error))
            changed = previous != current

            if changed:
                logged[result.name] = current

            if result.effective is State.ERROR:
                if changed:
                    # The detail stays server-side.
                    log.warning(
                        "dependency check failed",
                        extra={
                            "request_id": request_id(),
                            "dependency": result.name,
                            "required": result.required,
                            "error": str(result.error),
                        },
                    )
            elif result.effective is State.UNCONFIGURED:
                if changed:
                    # Still logged, because "this deployment has no object
                    # storage" is exactly the fact somebody needs when a feature
                    # is mysteriously absent. Once is enough to establish it.
                    log.warning(
                        "dependency is not configured",
                        extra={
                            "request_id": request_id(),
                            "dependency": result.name,
                            "required": result.required,
                            "reason": result.reason,
                        },
                    )
            elif changed and previous is not None:
                # Recovery is worth a line: it is the other half of any
                # incident, and without it a log shows only the breakage.
                log.info(
                    "dependency recovered",
                    extra={
                        "request_id": request_id(),
                        "dependency": result.name,
                        "required": result.required,
                    },
                )

        body = ReadinessResponse(
            status="ready" if ready else "not_ready",
            checks=checks,
        )
        return JSONResponse(
            status_code=200 if ready else 503,
            # The wire shape stays a flat map of name to word. Nesting an object
            # here would break every consumer at once: the ops console compares
            # checks[name] == "error", and that comparison silently becomes false
            # against an object, so a rollout would stop alerts firing rather
            # than start them. One new state word costs nothing.
            content=body.model_dump(),
            media_type=JSON_CONTENT_TYPE,
        )

    return router
