"""Helpers that drive the full NSU ID login flow against the fake OIDC provider."""

from typing import Any
from urllib.parse import urlparse

import httpx

from tests.fakes.oidc_provider import DEFAULT_CLAIMS, create_fake_oidc_app


def install_fake_oidc(app, claims: dict[str, Any] | None = None):
    """Create (once per app) a fake provider and route the portal OIDC HTTP calls to it.

    Returns the fake provider ASGI app. Later calls reuse it and only replace the claims.
    """
    fake = getattr(app.state, "fake_oidc", None)
    if fake is None:
        settings = app.state.settings
        fake = create_fake_oidc_app(
            settings.oidc_issuer,
            settings.oidc_client_id,
            settings.oidc_client_secret.get_secret_value(),
            claims or DEFAULT_CLAIMS,
        )
        provider = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://fake-oidc")

        async def handler(request: httpx.Request) -> httpx.Response:
            return await provider.send(
                provider.build_request(request.method, request.url, headers=request.headers, content=request.content)
            )

        app.state.fake_oidc = fake
        app.state.fake_oidc_client = provider
        app.state.oidc_transport = httpx.MockTransport(handler)
        app.state.oidc = None
    if claims is not None:
        fake.state.user_claims = dict(claims)
    return fake


async def authorize(app, login_response: httpx.Response) -> str:
    """Follow the provider redirect from /auth/login and return the callback path with query."""
    provider = app.state.fake_oidc_client
    assert login_response.status_code == 302, login_response.text
    r = await provider.get(login_response.headers["location"])
    assert r.status_code == 302, r.text
    parsed = urlparse(r.headers["location"])
    return f"{parsed.path}?{parsed.query}"


async def login(client: httpx.AsyncClient, app, claims: dict[str, Any] | None = None) -> dict:
    """Log the client in through the fake provider and return the /api/me payload."""
    install_fake_oidc(app, claims)
    r = await client.get("/auth/login")
    callback = await authorize(app, r)
    r = await client.get(callback)
    assert r.status_code == 302, r.text
    me = await client.get("/api/me")
    assert me.status_code == 200, me.text
    return me.json()


def csrf_headers(me: dict) -> dict[str, str]:
    return {"X-CSRF-Token": me["csrf_token"]}
