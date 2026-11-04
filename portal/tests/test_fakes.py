import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet

from app.config import Settings
from tests.fakes.openai_backend import create_fake_openai_app
from tests.fakes.oidc_provider import create_fake_oidc_app

ISSUER = "http://fake-oidc"
CLAIMS = {
    "sub": "user-1",
    "email": "ivan@g.nsu.ru",
    "preferred_username": "ivan",
    "given_name": "Иван",
    "family_name": "Петров",
}
REDIRECT = "https://portal.test/auth/callback"


def _oidc_client():
    app = create_fake_oidc_app(ISSUER, "cid", "secret", CLAIMS)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER)


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def _authorize(c: httpx.AsyncClient, verifier: str = "v" * 50, **over):
    params = {
        "client_id": "cid",
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "openid profile email",
        "state": "st-1",
        "nonce": "nonce-1",
        "code_challenge": _pkce(verifier),
        "code_challenge_method": "S256",
        **over,
    }
    return await c.get("/authorize", params=params)


def _token_form(code: str, verifier: str = "v" * 50):
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": "cid",
        "client_secret": "secret",
        "code_verifier": verifier,
    }


async def test_fake_openai_stream_has_done_and_usage():
    app = create_fake_openai_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        assert (await c.get("/health")).status_code == 200
        models = (await c.get("/v1/models")).json()
        assert models["data"][0]["id"] == "fake-model"

        body = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}
        r = await c.post("/v1/chat/completions", json=body)
        data = r.json()
        assert data["choices"][0]["message"]["content"] == "Привет из тестовой модели"
        assert data["usage"]["total_tokens"] == data["usage"]["prompt_tokens"] + data["usage"]["completion_tokens"]

        r = await c.post(
            "/v1/chat/completions",
            json={**body, "stream": True, "stream_options": {"include_usage": True}},
        )
        lines = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
        text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks if ch["choices"])
        assert text.strip() == "Привет из тестовой модели"
        assert chunks[-1]["choices"] == []
        assert chunks[-1]["usage"]["completion_tokens"] > 0

        r = await c.post("/v1/chat/completions", json={**body, "stream": True})
        chunks = [json.loads(ln[6:]) for ln in r.text.splitlines() if ln.startswith("data: {")]
        assert all("usage" not in ch for ch in chunks)


async def test_fake_oidc_full_code_flow():
    async with _oidc_client() as c:
        disc = (await c.get("/.well-known/openid-configuration")).json()
        assert disc["issuer"] == ISSUER
        assert disc["token_endpoint"] == ISSUER + "/token"

        r = await _authorize(c)
        assert r.status_code == 302
        loc = urlparse(r.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
        q = parse_qs(loc.query)
        assert q["state"] == ["st-1"]
        code = q["code"][0]

        tok = await c.post("/token", data=_token_form(code))
        assert tok.status_code == 200
        jwks = KeySet.import_key_set((await c.get("/jwks")).json())
        claims = jwt.decode(tok.json()["id_token"], jwks).claims
        assert claims["iss"] == ISSUER
        assert claims["aud"] == "cid"
        assert claims["nonce"] == "nonce-1"
        assert claims["email"] == "ivan@g.nsu.ru"
        assert claims["preferred_username"] == "ivan"
        assert claims["exp"] - claims["iat"] == 300

        reuse = await c.post("/token", data=_token_form(code))
        assert reuse.status_code == 400

        code2 = parse_qs(urlparse((await _authorize(c)).headers["location"]).query)["code"][0]
        bad = await c.post("/token", data=_token_form(code2, verifier="x" * 50))
        assert bad.status_code == 400


async def test_fake_oidc_rejections_and_logout():
    async with _oidc_client() as c:
        assert (await _authorize(c, client_id="other")).status_code == 400
        assert (await _authorize(c, code_challenge_method="plain")).status_code == 400

        r = await _authorize(c, error_test="access_denied")
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"] == ["access_denied"]
        assert q["state"] == ["st-1"]

        code = parse_qs(urlparse((await _authorize(c)).headers["location"]).query)["code"][0]
        wrong_secret = await c.post("/token", data={**_token_form(code), "client_secret": "nope"})
        assert wrong_secret.status_code == 401

        r = await c.get("/logout", params={"post_logout_redirect_uri": "https://portal.test/"})
        assert r.status_code == 302
        assert r.headers["location"] == "https://portal.test/"


def test_settings_admin_email_set():
    s = Settings(admin_emails=" Admin@G.nsu.ru, second@g.nsu.ru ,,")
    assert s.admin_email_set == frozenset({"admin@g.nsu.ru", "second@g.nsu.ru"})
    assert Settings(admin_emails="").admin_email_set == frozenset()
    assert Settings(public_hostname="x.test").public_base_url == "https://x.test"


def test_fakes_do_not_import_fastapi():
    # Task 5's integration test runs openai_backend.py in a bare python:3.12-slim with only starlette + uvicorn.
    import subprocess
    import sys
    from pathlib import Path

    fakes = Path(__file__).parent / "fakes"
    for name in ("openai_backend", "oidc_provider"):
        code = (
            "import sys, runpy; sys.modules['fastapi'] = None; "
            f"runpy.run_path(r'{fakes / (name + '.py')}', run_name='not_main')"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


async def test_fake_openai_delay_ms_is_per_token_and_fill_max_tokens():
    reply = "раз два три четыре"
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 6, "stream": True}

    # default: no delay, the reply is returned as is
    app = create_fake_openai_app(reply=reply)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        started = time.perf_counter()
        r = await c.post("/v1/chat/completions", json=body)
        assert time.perf_counter() - started < 0.2
        chunks = [json.loads(ln[6:]) for ln in r.text.splitlines() if ln.startswith("data: {")]
        text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks if ch["choices"])
        assert text.strip() == reply

    # delay_ms: one pause before the first chunk (time to first token) and one before every token
    app = create_fake_openai_app(reply=reply, delay_ms=40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        started = time.perf_counter()
        r = await c.post("/v1/chat/completions", json=body)
        assert time.perf_counter() - started >= 0.04 * (4 + 1) * 0.9
        started = time.perf_counter()
        r = await c.post("/v1/chat/completions", json={**body, "stream": False})
        assert time.perf_counter() - started >= 0.04 * 4 * 0.9
        assert r.json()["choices"][0]["message"]["content"] == reply

    # fill_max_tokens: the reply is cycled up to max_tokens words
    app = create_fake_openai_app(reply=reply, fill_max_tokens=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        r = await c.post("/v1/chat/completions", json={**body, "stream": False})
        data = r.json()
        assert len(data["choices"][0]["message"]["content"].split()) == 6
        assert data["usage"]["completion_tokens"] == 6
        r = await c.post("/v1/chat/completions", json={**body, "stream": False, "max_tokens": None})
        assert r.json()["usage"]["completion_tokens"] == 4


def test_mirror_for_the_nodes_defaults_to_the_ip_of_main():
    """Class PCs often have no DNS for PUBLIC_HOSTNAME: the mirror address the nodes get is https://<MAIN_ADVERTISE_IP>/hf
    (the IP site of Caddy serves /hf, the internal CA issues a certificate for the IP). HF_MIRROR_URL still wins; with an
    own certificate (TLS_CERT), which need not cover the IP, the name stays."""
    assert Settings(public_hostname="llm.test", main_advertise_ip="192.168.3.21").node_hf_endpoint == "https://192.168.3.21/hf"
    assert Settings(public_hostname="llm.test").node_hf_endpoint == "https://llm.test/hf"
    assert Settings(
        public_hostname="llm.test", main_advertise_ip="192.168.3.21", hf_mirror_url="https://mirror.test/hf/"
    ).node_hf_endpoint == "https://mirror.test/hf"
    assert Settings(
        public_hostname="llm.test", main_advertise_ip="192.168.3.21", main_own_cert=True
    ).node_hf_endpoint == "https://llm.test/hf"
