import pytest

from app.litellm_client import LiteLLMClient

pytestmark = pytest.mark.docker


async def test_healthz_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_reports_litellm_down(client, app):
    app.state.litellm = LiteLLMClient("http://127.0.0.1:1", "sk-test")
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"db": True, "litellm": False}


async def test_spa_fallback_serves_index(client):
    r = await client.get("/keys")
    assert r.status_code == 200
    assert "<!--spa-marker-->" in r.text
    r = await client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


async def test_the_inspector_and_the_progress_report_go_through_the_mirror_on_main(app, settings):
    # conftest: hf_endpoint and hf_mirror_port both point at the discard port; only the mirror sets the progress source
    assert app.state.progress.mirror_url == f"http://127.0.0.1:{settings.hf_mirror_port}" == app.state.hf.mirror.endpoint
    assert app.state.hf.mirror.token is None  # the tests set no mirror key
    assert app.state.hf.direct.endpoint == settings.hf_endpoint  # while the mirror is down
