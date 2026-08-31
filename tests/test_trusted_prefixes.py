"""Prefixes that are not browser surfaces, and what exempting one does.

An operator or fleet surface is reached by another server holding a credential.
It is not a browser surface, so two of the conventions aimed at browsers and
public callers are wrong for it, and `trusted_prefixes` turns both off with one
statement because they are one fact about the service.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from kit.health import Registry
from kit.httpapi import CORS, RateLimit, install_conventions

ORIGIN = "https://app.example.com"
ADMIN = "/api/admin"


def app_with(*, trusted: tuple[str, ...] = (), limit: RateLimit | None = None) -> FastAPI:
    app = FastAPI()
    install_conventions(
        app,
        readiness=Registry(),
        cors=CORS(allowed_origins=(ORIGIN,)),
        rate_limit=limit if limit is not None else RateLimit(),
        trusted_prefixes=trusted,
    )

    @app.get("/api/v1/thing")
    async def customer() -> dict[str, bool]:
        return {"ok": True}

    @app.get(f"{ADMIN}/v1/organizations")
    async def operator() -> dict[str, bool]:
        return {"ok": True}

    return app


async def get(app: FastAPI, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


class TestCORSIsSkippedUnderATrustedPrefix:
    async def test_a_customer_route_still_answers_a_browser(self) -> None:
        """The exemption must be narrow. Everything not under the prefix keeps
        the credentialed allow-list it had, or this is a regression dressed as a
        hardening."""
        response = await get(app_with(trusted=(ADMIN,)), "/api/v1/thing", {"origin": ORIGIN})

        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-credentials"] == "true"

    async def test_an_operator_route_names_no_origin(self) -> None:
        """THE POINT OF THE FEATURE, in one assertion.

        `access-control-allow-origin` is a statement that a browser at that
        origin may read the response. On a surface only another server ever
        calls, that statement is false, and a false statement in security config
        is the kind that becomes true when somebody later adds an origin.
        """
        response = await get(
            app_with(trusted=(ADMIN,)), f"{ADMIN}/v1/organizations", {"origin": ORIGIN}
        )

        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers

    async def test_without_the_prefix_the_operator_route_would_carry_the_header(self) -> None:
        """The before picture, so the test above is measuring the feature rather
        than a route that never had CORS in the first place."""
        response = await get(app_with(), f"{ADMIN}/v1/organizations", {"origin": ORIGIN})

        assert response.headers["access-control-allow-origin"] == ORIGIN

    async def test_it_does_not_hide_the_route(self) -> None:
        """WHAT THIS DELIBERATELY DOES NOT DO, pinned so nobody claims otherwise.

        The tempting justification is that exempting a prefix conceals an
        operator surface from a browser probe. It does not. The route answers
        exactly as before; only the header is gone. Anybody documenting this as
        a disclosure fix is wrong, and this test is where they find out.
        """
        response = await get(
            app_with(trusted=(ADMIN,)), f"{ADMIN}/v1/organizations", {"origin": ORIGIN}
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

    async def test_a_request_with_no_origin_is_unaffected(self) -> None:
        """The real caller. A server-side client sends no Origin at all, which is
        why CORS was inert for it either way."""
        response = await get(app_with(trusted=(ADMIN,)), f"{ADMIN}/v1/organizations")

        assert response.status_code == 200


class TestTheLimiterIsSkippedUnderATrustedPrefix:
    LIMIT = RateLimit(requests=1, window_seconds=60.0, burst=1)

    async def test_a_customer_route_is_still_limited(self) -> None:
        app = app_with(trusted=(ADMIN,), limit=self.LIMIT)

        first = await get(app, "/api/v1/thing")
        second = await get(app, "/api/v1/thing")

        assert first.status_code == 200
        assert second.status_code == 429

    async def test_an_operator_route_is_not(self) -> None:
        """A per-address bucket is the wrong instrument here. Every operator
        action arrives from one trusted address, so the ceiling refuses an
        operator rather than an abuser, and the credential is what guards it."""
        app = app_with(trusted=(ADMIN,), limit=self.LIMIT)

        for _ in range(5):
            response = await get(app, f"{ADMIN}/v1/organizations")
            assert response.status_code == 200

    async def test_the_probes_stay_exempt_by_exact_match(self) -> None:
        """`EXEMPT_PATHS` is exact and stays exact. Widening it to prefixes would
        have been one less field and would have exempted every path merely
        BEGINNING with `/healthz`, which reads as a simplification and is a
        hole."""
        app = app_with(limit=self.LIMIT)

        for _ in range(5):
            assert (await get(app, "/healthz")).status_code == 200

    async def test_a_path_merely_beginning_with_a_probe_name_is_not_exempt(self) -> None:
        app = app_with(limit=self.LIMIT)

        first = await get(app, "/healthz-fake")
        second = await get(app, "/healthz-fake")

        # 404 rather than 200: the route does not exist. What matters is that the
        # limiter counted it, which the second answer proves.
        assert first.status_code == 404
        assert second.status_code == 429


@pytest.mark.parametrize("public_read", [True, False])
async def test_both_cors_modes_honour_the_exemption(public_read: bool) -> None:
    """The wildcard branch and the credentialed branch are separate calls in
    `install_conventions`, so exempting one and forgetting the other is a live
    possibility rather than a hypothetical."""
    app = FastAPI()
    install_conventions(
        app,
        readiness=Registry(),
        cors=CORS(allowed_origins=() if public_read else (ORIGIN,), public_read=public_read),
        rate_limit=RateLimit(),
        trusted_prefixes=(ADMIN,),
    )

    @app.get(f"{ADMIN}/v1/organizations")
    async def operator() -> dict[str, bool]:
        return {"ok": True}

    response = await get(app, f"{ADMIN}/v1/organizations", {"origin": ORIGIN})

    assert "access-control-allow-origin" not in response.headers
