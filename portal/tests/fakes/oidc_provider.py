"""Fake OpenID Connect provider (authorization code + PKCE S256) for tests. Pure Starlette, no fastapi.

Run as a file or as a module:
    python tests/fakes/oidc_provider.py --port 18100 --issuer http://127.0.0.1:18100
"""

import argparse
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

DEFAULT_CLAIMS = {
    "sub": "fake-user-1",
    "email": "user@g.nsu.ru",
    "preferred_username": "user",
    "given_name": "Иван",
    "family_name": "Петров",
}


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _redirect(base: str, params: dict[str, str]) -> RedirectResponse:
    sep = "&" if "?" in base else "?"
    return RedirectResponse(base + sep + urlencode(params), status_code=302)


def _bad(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def create_fake_oidc_app(
    issuer: str,
    client_id: str,
    client_secret: str,
    user_claims: dict[str, Any] | None = None,
) -> Starlette:
    issuer = issuer.rstrip("/")
    prefix = urlparse(issuer).path
    key = RSAKey.import_key(
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        {"kid": "fake-key-1", "use": "sig", "alg": "RS256"},
    )
    codes: dict[str, dict[str, Any]] = {}

    async def discovery(request: Request):
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "end_session_endpoint": f"{issuer}/logout",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["openid", "profile", "email"],
            }
        )

    async def jwks(request: Request):
        return JSONResponse({"keys": [key.as_dict(private=False)]})

    async def authorize(request: Request):
        q = request.query_params
        redirect_uri = q.get("redirect_uri", "")
        if q.get("client_id") != client_id:
            return _bad("unauthorized_client", "unknown client_id")
        if not redirect_uri:
            return _bad("invalid_request", "redirect_uri is required")
        if q.get("response_type") != "code":
            return _bad("unsupported_response_type", "only response_type=code")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return _bad("invalid_request", "PKCE with S256 is required")
        state = q.get("state", "")
        if q.get("error_test"):
            return _redirect(redirect_uri, {"error": q["error_test"], "state": state})
        code = secrets.token_urlsafe(24)
        codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": q["code_challenge"],
            "nonce": q.get("nonce", ""),
            "used": False,
        }
        return _redirect(redirect_uri, {"code": code, "state": state})

    async def token(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        if form.get("grant_type") != "authorization_code":
            return _bad("unsupported_grant_type", "only authorization_code")
        if form.get("client_id") != client_id or not secrets.compare_digest(
            form.get("client_secret", "").encode(), client_secret.encode()
        ):
            return _bad("invalid_client", "bad client credentials", status=401)
        entry = codes.get(form.get("code", ""))
        if entry is None or entry["used"]:
            return _bad("invalid_grant", "code is unknown or already used")
        entry["used"] = True
        if form.get("redirect_uri") != entry["redirect_uri"]:
            return _bad("invalid_grant", "redirect_uri mismatch")
        if _s256(form.get("code_verifier", "")) != entry["code_challenge"]:
            return _bad("invalid_grant", "PKCE verification failed")
        now = int(time.time())
        claims = {
            **app.state.user_claims,
            "iss": issuer,
            "aud": client_id,
            "nonce": entry["nonce"],
            "iat": now,
            "exp": now + 300,
        }
        id_token = jwt.encode({"alg": "RS256", "kid": "fake-key-1"}, claims, key)
        return JSONResponse(
            {
                "access_token": secrets.token_urlsafe(24),
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": id_token,
            }
        )

    async def logout(request: Request):
        target = request.query_params.get("post_logout_redirect_uri")
        if not target:
            return JSONResponse({"status": "logged out"})
        return RedirectResponse(target, status_code=302)

    app = Starlette(
        routes=[
            Route(f"{prefix}/.well-known/openid-configuration", discovery),
            Route(f"{prefix}/jwks", jwks),
            Route(f"{prefix}/authorize", authorize),
            Route(f"{prefix}/token", token, methods=["POST"]),
            Route(f"{prefix}/logout", logout),
        ]
    )
    app.state.user_claims = dict(user_claims or DEFAULT_CLAIMS)
    app.state.issued_codes = codes
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Fake OIDC provider")
    parser.add_argument("--port", type=int, default=18100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--issuer", default=None)
    parser.add_argument("--client-id", default="double.fresh.nsuts.ru")
    parser.add_argument("--client-secret", default="secret")
    parser.add_argument("--email", default=DEFAULT_CLAIMS["email"], help="e-mail claim of the signed-in user")
    args = parser.parse_args()
    issuer = args.issuer or f"http://{args.host}:{args.port}"
    claims = {**DEFAULT_CLAIMS, "email": args.email}
    app = create_fake_oidc_app(issuer, args.client_id, args.client_secret, claims)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
