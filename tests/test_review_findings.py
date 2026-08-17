"""Guards for defects found in review. Each one shipped and was caught reading.

None of these were failing tests. They were untested contracts, which is the
failure mode 100% coverage does not protect against.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from kit.config import ConfigError, Service, load
from kit.health import Registry, failing_check
from kit.httpapi import CORS, RateLimit, install_conventions
from kit.httpapi._ratelimit import MAX_TRACKED_BUCKETS, Limiter, _Bucket, client_address
from kit.httpapi._ratelimit_middleware import identify

SERVICE = Service(name="kit-service", env_prefix="KIT_")


async def ok() -> None:
    return None


def app_with(limit: RateLimit, registry: Registry | None = None) -> FastAPI:
    app = FastAPI()
    install_conventions(app, readiness=registry or Registry(), rate_limit=limit)
    return app


# --- the probes must never be rate limited ---------------------------------


async def test_the_probes_are_never_rate_limited() -> None:
    """The 3am page this prevents: under exactly the traffic spike a limit
    exists to survive, a 429 on /healthz restarts a container that was working
    and a 429 on /readyz takes a healthy pod out of the Service endpoints. The
    limiter would turn a busy service into a shrinking one.
    """
    app = app_with(RateLimit(requests=60, window_seconds=60, burst=1))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Well past the burst of one.
        healthz = [(await client.get("/healthz")).status_code for _ in range(10)]
        readyz = [(await client.get("/readyz")).status_code for _ in range(10)]

    assert set(healthz) == {200}, "liveness was rate limited"
    assert set(readyz) == {200}, "readiness was rate limited"


async def test_ordinary_routes_are_still_limited() -> None:
    """Exempting the probes must not exempt everything."""
    app = app_with(RateLimit(requests=60, window_seconds=60, burst=1))

    @app.get("/thing")
    async def thing() -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/thing")
        second = await client.get("/thing")

    assert first.status_code == 200
    assert second.status_code == 429


# --- a refused request must not allocate -----------------------------------


def test_a_refused_request_leaves_no_bucket_behind() -> None:
    """Otherwise rotating garbage credentials fills the table with entries that
    never admitted a single request, which is the attack the address ceiling
    exists to stop, funded by the limiter itself.
    """
    limiter = Limiter(limit=RateLimit(requests=60, window_seconds=60, burst=2))

    # Exhaust the address bucket with anonymous requests.
    while limiter.allow(limiter.charges("", "1.2.3.4"))[0]:
        pass

    before = len(limiter._buckets)
    for index in range(50):
        allowed, _, _ = limiter.allow(limiter.charges(f"c:rotating-{index}", "1.2.3.4"))
        assert allowed is False

    assert len(limiter._buckets) == before, (
        "refused requests created buckets; a caller can fill the table for free"
    )


def test_eviction_does_not_scan_the_whole_table() -> None:
    """At capacity this runs on every admission. Copying every key first turns
    the overload path into its own denial of service under the flood it exists
    to survive."""
    limiter = Limiter(limit=RateLimit(requests=60, window_seconds=60, burst=5))
    for index in range(MAX_TRACKED_BUCKETS):
        limiter._buckets[f"filler:{index}"] = _Bucket(tokens=5.0, seen=float(index))

    limiter._evict_one(now=1_000_000.0)

    # The oldest, which is the front of an insertion-ordered table, and not a
    # random survivor of a full scan.
    assert "filler:0" not in limiter._buckets
    assert len(limiter._buckets) == MAX_TRACKED_BUCKETS - 1


# --- require() cannot be demoted -------------------------------------------


@pytest.mark.parametrize(
    "wire",
    [
        lambda r: r.add_optional_unconfigured("db", "missing"),
        lambda r: r.add_optional("db", failing_check(RuntimeError("down"))),
        lambda r: r.add_informational("db", failing_check(RuntimeError("down"))),
    ],
)
async def test_a_declared_dependency_cannot_be_demoted_by_how_it_is_registered(
    wire: object,
) -> None:
    """Without this, require("db") followed by add_optional("db") removes the
    declaration AND leaves a non-blocking entry, so a dependency the service
    said it cannot work without silently stops blocking, invisibly, with
    readiness still green.
    """
    registry = Registry()
    registry.require("db")
    wire(registry)  # type: ignore[operator]

    result = (await registry.check())[0]

    assert result.required is True
    assert result.blocking is True


async def test_a_duplicate_name_cannot_make_the_report_contradict_itself() -> None:
    """The wire shape is a map, so a duplicate collapses there while the verdict
    counts both, producing not_ready with every listed dependency reading ready.
    """
    registry = Registry()
    registry.add("db", failing_check(RuntimeError("down")))
    registry.add("db", ok)

    results = await registry.check()
    app = app_with(RateLimit(), registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")

    assert len(results) == 1
    body = response.json()
    ready_states = {state for state in body["checks"].values()}
    assert (body["status"] == "ready") == (ready_states == {"ready"})


# --- the proxy trust boundary ----------------------------------------------


def test_a_forwarded_header_is_ignored_unless_a_proxy_is_declared() -> None:
    """X-Forwarded-For is a header the caller can write. A process reached
    directly has no proxy to have written it, so anything present was typed by
    whoever is calling."""
    headers = {"x-forwarded-for": "10.9.9.9"}

    assert client_address(headers, "192.0.2.10") == "192.0.2.10"
    assert client_address(headers, "192.0.2.10", trusted_proxies=0) == "192.0.2.10"


def test_only_as_many_hops_are_trusted_as_are_declared() -> None:
    """Entries a caller controls are on the LEFT; infrastructure writes to the
    RIGHT. How far you may count from the right is the number of real proxies."""
    headers = {"x-forwarded-for": "spoofed, 203.0.113.9"}

    assert client_address(headers, "peer", trusted_proxies=1) == "203.0.113.9"
    assert client_address(headers, "peer", trusted_proxies=2) == "spoofed"


def test_identify_does_not_trust_a_forwarded_header_by_default() -> None:
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", b"10.9.9.9")],
        "client": ("192.0.2.10", 1234),
        "query_string": b"",
    }
    _, address = identify(StarletteRequest(scope))

    assert address == "192.0.2.10"


# --- configuration bounds ---------------------------------------------------


@pytest.mark.parametrize(
    ("env", "complaint"),
    [
        ({"PORT": "70000"}, "between 1 and 65535"),
        ({"KIT_LOG_LEVEL": "verbose"}, "LOG_LEVEL must be one of"),
        ({"KIT_SHUTDOWN_TIMEOUT": "nan"}, "finite"),
        ({"KIT_SHUTDOWN_TIMEOUT": "inf"}, "finite"),
    ],
)
def test_a_setting_outside_its_range_stops_startup(env: dict[str, str], complaint: str) -> None:
    """Positivity is not a bound. A port of 70000 cannot be bound, and a NaN
    timeout compares false against every limit, so it passes a positivity check
    and then makes every wait either instant or never."""
    with pytest.raises(ConfigError, match=complaint):
        load(SERVICE, env)


def test_cors_origins_cannot_be_mutated_past_the_exclusion_check() -> None:
    """A frozen dataclass holding a mutable field is not frozen."""
    cors = CORS(public_read=True)

    assert isinstance(cors.allowed_origins, tuple)
    with pytest.raises(AttributeError):
        cors.allowed_origins = ("https://app.example.com",)  # type: ignore[misc]
