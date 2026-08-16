"""Every failure answers in one shape, whatever raised it."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from kit.health import Registry
from kit.httpapi import REQUEST_ID_HEADER, RateLimit, install_conventions


class Body(BaseModel):
    count: int


def build() -> FastAPI:
    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("something internal with a secret in it")

    @app.get("/missing")
    async def missing() -> None:
        raise HTTPException(status_code=404, detail="No such policy")

    @app.post("/typed")
    async def typed(body: Body) -> Body:
        return body

    return app


async def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build()), base_url="http://test")


async def test_unrouted_path_returns_the_envelope() -> None:
    """Registering FastAPI's HTTPException subclass instead of Starlette's base
    leaves this answering {"detail": ...}, because the router raises the base
    class before FastAPI is involved."""
    async with await client() as c:
        response = await c.get("/nope")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"]["code"] == "not_found"
    assert "detail" not in body


async def test_an_unhandled_exception_never_leaks_its_message() -> None:
    async with await client() as c:
        response = await c.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text


async def test_validation_failure_uses_the_house_shape() -> None:
    """FastAPI injects its own 422 shape unless it is overridden, so without
    this a service documents a shape it does not send."""
    async with await client() as c:
        response = await c.post("/typed", json={"count": "not a number"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "detail" not in response.json()


async def test_the_envelope_request_id_matches_the_header() -> None:
    """A caller reporting a failure must be able to name the exact log line."""
    async with await client() as c:
        response = await c.get("/missing")

    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert response.json()["request_id"] != ""


async def test_an_inbound_request_id_is_honoured() -> None:
    """So a trace spans services rather than restarting at each hop."""
    async with await client() as c:
        response = await c.get("/missing", headers={REQUEST_ID_HEADER: "from-upstream"})

    assert response.headers[REQUEST_ID_HEADER] == "from-upstream"
    assert response.json()["request_id"] == "from-upstream"


async def test_security_headers_are_on_every_response() -> None:
    async with await client() as c:
        response = await c.get("/missing")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
