"""OIDC client against NSU ID (Keycloak): authorization code flow with PKCE, RS256 id_token checks."""

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Request
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

_TIMEOUT = 10.0
_LEEWAY_S = 60  # tolerated clock skew between the portal host and NSU ID


class OidcError(Exception):
    """The provider answered with something the portal cannot accept."""


@dataclass
class TokenSet:
    id_token: str
    access_token: str
    claims: dict[str, Any]


def code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _token_kid(token: str) -> str | None:
    try:
        header = token.split(".", 1)[0]
        return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))).get("kid")
    except Exception:
        return None


class OidcClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        scopes: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.transport = transport
        self._metadata: dict[str, Any] | None = None
        self._keys: KeySet | None = None
        self._lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, timeout=_TIMEOUT)

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            async with self._lock:
                if self._metadata is None:
                    async with self._http() as http:
                        r = await http.get(f"{self.issuer}/.well-known/openid-configuration")
                    r.raise_for_status()
                    data = r.json()
                    if data.get("issuer", "").rstrip("/") != self.issuer:
                        raise OidcError("issuer in discovery document does not match configuration")
                    self._metadata = data
        return self._metadata

    async def authorization_url(self, redirect_uri: str, state: str, nonce: str, code_verifier: str) -> str:
        meta = await self.metadata()
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "response_mode": "query",
            "scope": self.scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def _load_keys(self) -> KeySet:
        meta = await self.metadata()
        async with self._http() as http:
            r = await http.get(meta["jwks_uri"])
        r.raise_for_status()
        self._keys = KeySet.import_key_set(r.json())
        return self._keys

    async def _verify(self, id_token: str) -> dict[str, Any]:
        keys = self._keys or await self._load_keys()
        kid = _token_kid(id_token)
        if kid is not None and not any(k.kid == kid for k in keys.keys):
            keys = await self._load_keys()  # key rotation
        try:
            token = jwt.decode(id_token, keys, algorithms=["RS256"])
            JWTClaimsRegistry(
                leeway=_LEEWAY_S,
                iss={"essential": True, "value": self.issuer},
                aud={"essential": True, "value": self.client_id},
                exp={"essential": True},
                sub={"essential": True},
            ).validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise OidcError(f"invalid id_token: {exc}") from exc
        return dict(token.claims)

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> TokenSet:
        meta = await self.metadata()
        async with self._http() as http:
            r = await http.post(
                meta["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": code_verifier,
                },
            )
        if r.status_code != 200:
            raise OidcError(f"token endpoint answered {r.status_code}")
        body = r.json()
        id_token = body.get("id_token")
        if not id_token:
            raise OidcError("token response has no id_token")
        claims = await self._verify(id_token)
        return TokenSet(id_token=id_token, access_token=body.get("access_token", ""), claims=claims)

    async def end_session_url(self, id_token: str, post_logout_redirect_uri: str) -> str | None:
        meta = await self.metadata()
        endpoint = meta.get("end_session_endpoint")
        if not endpoint:
            return None
        params = {
            "client_id": self.client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri,
        }
        if id_token:
            params["id_token_hint"] = id_token
        return f"{endpoint}?{urlencode(params)}"


def get_oidc(request: Request) -> OidcClient:
    state = request.app.state
    oidc = getattr(state, "oidc", None)
    if oidc is None:
        s = state.settings
        oidc = OidcClient(
            s.oidc_issuer,
            s.oidc_client_id,
            s.oidc_client_secret.get_secret_value(),
            s.oidc_scopes,
            transport=getattr(state, "oidc_transport", None),
        )
        state.oidc = oidc
    return oidc
