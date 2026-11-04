"""Offline tests for the e2e driver scripts (respx-mocked portal and fake provider)."""
import json

import httpx
import openai
import pytest
import respx

import admin
import common
import smoke

BASE = "https://portal.test"
PROVIDER = "http://provider.test:9999"


def mock_fake_login(mock, cookie="sess-abc"):
    mock.get(f"{BASE}/auth/login").respond(
        302, headers={"location": f"{PROVIDER}/authorize?client_id=x&state=s1"}
    )
    mock.get(f"{PROVIDER}/authorize").respond(
        302, headers={"location": f"{BASE}/auth/callback?code=c1&state=s1"}
    )
    mock.get(f"{BASE}/auth/callback").respond(
        302,
        headers={
            "location": "/",
            "set-cookie": f"nsullm_session={cookie}; HttpOnly; Secure; SameSite=Lax; Path=/",
        },
    )
    mock.get(f"{BASE}/").respond(200, text="<html></html>")


def sse(*events):
    body = "".join(f"data: {e if e == '[DONE]' else json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def chunk(content=None, usage=None):
    c = {
        "id": "x", "object": "chat.completion.chunk", "created": 1, "model": "tiny",
        "choices": [] if content is None else [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    if usage:
        c["usage"] = usage
    return c


def install_smoke_portal(mock, ready_backends=2, state=None):
    """Portal as seen by smoke.py: cookie auth, models, keys, chat, delete."""
    state = state if state is not None else {}
    mock.get(f"{BASE}/api/me").respond(
        200, json={"id": "u1", "email": "a@g.nsu.ru", "role": "user", "csrf_token": "csrf-1"}
    )
    mock.get(f"{BASE}/api/models").respond(
        200,
        json=[{"name": "tiny", "status": "ready", "ready_backends": ready_backends, "engine": "llamacpp",
               "mode": "replicas", "ctx_per_request": 2048, "parallel": 2, "group_size": 2}],
    )

    def create_key(request):
        state["create_csrf"] = request.headers.get("x-csrf-token")
        return httpx.Response(201, json={"id": "k1", "alias": "a", "key": "sk-live", "masked": "sk-...live",
                                         "created_at": "now", "expires_at": None})

    def delete_key(request):
        state["delete_csrf"] = request.headers.get("x-csrf-token")
        state["deleted"] = True
        return httpx.Response(204)

    def chat(request):
        if state.get("deleted") or request.headers.get("authorization") != "Bearer sk-live":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        body = json.loads(request.content)
        if body.get("stream"):
            usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            return sse(chunk("Pri"), chunk("vet"), chunk(usage=usage), "[DONE]")
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": "tiny",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        })

    mock.post(f"{BASE}/api/keys").mock(side_effect=create_key)
    mock.delete(f"{BASE}/api/keys/k1").mock(side_effect=delete_key)
    mock.post(f"{BASE}/v1/chat/completions").mock(side_effect=chat)
    return state


def test_fake_login_follows_redirects_and_keeps_cookie():
    with respx.mock(assert_all_called=False) as mock:
        mock_fake_login(mock)
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "csrf-9", "role": "admin"})
        session = common.Portal.login_fake(BASE, common.make_transport([], None))
        assert session.cookie == "sess-abc"
        me = session.me()
        assert me["csrf_token"] == "csrf-9"
        assert mock.calls.last.request.headers["cookie"] == "nsullm_session=sess-abc"
        session.close()


def test_smoke_success_path_exit_0(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock_fake_login(mock)
        state = install_smoke_portal(mock)
        code = smoke.main(["--base", BASE, "--model", "tiny", "--expect-backends", "2", "--timeout", "5"])
    out = capsys.readouterr().out
    assert code == 0, out
    result = json.loads(out.strip().splitlines()[-1])
    assert set(result) == {"ttft_s", "tokens_per_s", "backends"}
    assert result["backends"] == 2
    assert state["create_csrf"] == "csrf-1" and state["delete_csrf"] == "csrf-1"
    assert state["deleted"] is True


def revoked_client():
    http_client = httpx.Client(timeout=5)
    return openai.OpenAI(base_url=f"{BASE}/v1", api_key="sk-gone", http_client=http_client, max_retries=0)


def test_check_revoked_retries_through_429_until_401():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/v1/chat/completions").mock(side_effect=[
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(401, json={"error": {"message": "bad key"}}),
        ])
        smoke.check_revoked(revoked_client(), "tiny", timeout=5, interval=0.01)
    assert route.call_count == 3


def test_check_revoked_keeps_failing_on_other_statuses():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/v1/chat/completions").respond(500, json={"error": {"message": "boom"}})
        with pytest.raises(common.Fail, match="500"):
            smoke.check_revoked(revoked_client(), "tiny", timeout=5, interval=0.01)


def test_check_revoked_gives_up_when_429_never_ends():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/v1/chat/completions").respond(429, json={"error": {"message": "slow down"}})
        with pytest.raises(common.Fail):
            smoke.check_revoked(revoked_client(), "tiny", timeout=0.1, interval=0.02)


def test_smoke_cookie_login_skips_oidc_flow(capsys):
    with respx.mock(assert_all_called=False) as mock:
        install_smoke_portal(mock)
        code = smoke.main(["--base", BASE, "--login", "cookie", "--cookie", "given", "--model", "tiny",
                           "--expect-backends", "1", "--timeout", "5"])
        assert code == 0, capsys.readouterr().out
        assert mock.calls[0].request.headers["cookie"] == "nsullm_session=given"


def test_smoke_reports_missing_backends_exit_1(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock_fake_login(mock)
        install_smoke_portal(mock, ready_backends=1)
        code = smoke.main(["--base", BASE, "--model", "tiny", "--expect-backends", "2",
                           "--timeout", "0.3", "--poll-interval", "0.05"])
    captured = capsys.readouterr()
    assert code == 1
    assert "backends" in captured.err and "1" in captured.err and "2" in captured.err


def test_smoke_unreachable_exit_2(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=httpx.ConnectError("refused"))
        code = smoke.main(["--base", BASE, "--login", "cookie", "--cookie", "c", "--model", "tiny"])
    assert code == 2
    assert "refused" in capsys.readouterr().err


def test_admin_create_deployment_sends_csrf_and_body(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "csrf-7", "role": "admin"})
        route = mock.post(f"{BASE}/api/admin/deployments").respond(201, json={"id": 5, "name": "tiny-b"})
        code = admin.main([
            "--base", BASE, "--login", "cookie", "--cookie", "adm",
            "create-deployment", "--name", "tiny-b", "--repo", "ggml-org/gemma-3-270m-it-GGUF",
            "--file", "gemma-3-270m-it-Q8_0.gguf", "--mode", "replicas", "--accel", "cpu",
            "--ctx", "1024", "--parallel", "1", "--nodes", "n1,n2", "--auto-assign",
        ])
        request = route.calls.last.request
    assert code == 0, capsys.readouterr().err
    assert request.headers["x-csrf-token"] == "csrf-7"
    assert request.headers["cookie"] == "nsullm_session=adm"
    assert json.loads(request.content) == {
        "name": "tiny-b", "engine": "llamacpp", "repo": "ggml-org/gemma-3-270m-it-GGUF",
        "file": "gemma-3-270m-it-Q8_0.gguf", "mode": "replicas", "accel": "cpu", "ctx_per_request": 1024,
        "parallel": 1, "nodes": ["n1", "n2"], "auto_assign": True,
    }


def test_admin_create_vllm_needs_no_file_and_defaults_to_nvidia(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "csrf-7", "role": "admin"})
        route = mock.post(f"{BASE}/api/admin/deployments").respond(201, json={"id": 6, "name": "v"})
        code = admin.main([
            "--base", BASE, "--login", "cookie", "--cookie", "adm",
            "create-deployment", "--name", "v", "--engine", "vllm", "--repo", "meta-llama/Llama-3.1-8B-Instruct",
            "--ctx", "4096", "--parallel", "4",
        ])
        body = json.loads(route.calls.last.request.content)
    assert code == 0, capsys.readouterr().err
    assert body["engine"] == "vllm" and body["file"] is None and body["accel"] == "nvidia"


def test_admin_create_llamacpp_without_file_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stop:
        admin.main(["--base", BASE, "--login", "cookie", "--cookie", "x", "create-deployment", "--name", "t",
                    "--repo", "a/b", "--ctx", "1024", "--parallel", "1"])
    assert stop.value.code == 2
    assert "--file" in capsys.readouterr().err


def test_admin_patch_enabled_and_auto_assign_flags():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "c", "role": "admin"})
        mock.get(f"{BASE}/api/admin/deployments").respond(200, json=[{"id": 8, "name": "tiny-b"}])
        patch = mock.patch(f"{BASE}/api/admin/deployments/8").respond(200, json={})
        common_args = ["--base", BASE, "--login", "cookie", "--cookie", "x", "patch-deployment", "--name", "tiny-b"]
        assert admin.main(common_args + ["--no-enabled", "--auto-assign"]) == 0
        assert json.loads(patch.calls.last.request.content) == {"enabled": False, "auto_assign": True}
        assert admin.main(common_args + ["--enabled", "--no-auto-assign"]) == 0
        assert json.loads(patch.calls.last.request.content) == {"enabled": True, "auto_assign": False}


def test_admin_patch_assign_and_delete_resolve_name_to_id():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "c", "role": "admin"})
        mock.get(f"{BASE}/api/admin/deployments").respond(
            200, json=[{"id": 3, "name": "tiny"}, {"id": 8, "name": "tiny-b"}]
        )
        patch = mock.patch(f"{BASE}/api/admin/deployments/8").respond(200, json={})
        assign = mock.put(f"{BASE}/api/admin/nodes/laptop/assignment").respond(200, json={})
        delete = mock.delete(f"{BASE}/api/admin/deployments/8").respond(204)
        common_args = ["--base", BASE, "--login", "cookie", "--cookie", "x"]
        assert admin.main(common_args + ["patch-deployment", "--name", "tiny-b", "--ctx", "4096"]) == 0
        assert json.loads(patch.calls.last.request.content) == {"ctx_per_request": 4096}
        assert admin.main(common_args + ["assign", "--node", "laptop", "--deployment", "tiny-b"]) == 0
        assert json.loads(assign.calls.last.request.content) == {"deployment_id": 8}
        assert admin.main(common_args + ["assign", "--node", "laptop", "--none"]) == 0
        assert json.loads(assign.calls.last.request.content) == {"deployment_id": None}
        assert admin.main(common_args + ["delete-deployment", "--name", "tiny-b"]) == 0
        assert delete.called


def test_admin_unknown_deployment_exit_1(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "c"})
        mock.get(f"{BASE}/api/admin/deployments").respond(200, json=[])
        code = admin.main(["--base", BASE, "--login", "cookie", "--cookie", "x", "status", "--name", "nope"])
    assert code == 1
    assert "nope" in capsys.readouterr().err


def test_admin_wait_ready_times_out_exit_1(capsys):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "c"})
        mock.get(f"{BASE}/api/admin/deployments").respond(200, json=[{"id": 3, "name": "tiny"}])
        mock.get(f"{BASE}/api/admin/deployments/3/status").respond(200, json={"ready_backends": 1, "nodes": []})
        code = admin.main(["--base", BASE, "--login", "cookie", "--cookie", "x", "wait-ready",
                           "--name", "tiny", "--backends", "2", "--timeout", "0.3", "--poll-interval", "0.05"])
    assert code == 1
    assert "tiny" in capsys.readouterr().err


def test_admin_wait_ready_succeeds_when_backends_reached():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/me").respond(200, json={"csrf_token": "c"})
        mock.get(f"{BASE}/api/admin/deployments").respond(200, json=[{"id": 3, "name": "tiny"}])
        mock.get(f"{BASE}/api/admin/deployments/3/status").mock(side_effect=[
            httpx.Response(200, json={"ready_backends": None}),
            httpx.Response(200, json={"ready_backends": 2}),
        ])
        code = admin.main(["--base", BASE, "--login", "cookie", "--cookie", "x", "wait-ready",
                           "--name", "tiny", "--backends", "2", "--timeout", "5", "--poll-interval", "0.01"])
    assert code == 0


def test_resolve_transport_dials_given_ip():
    seen = []

    class Recorder(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append(request)
            return httpx.Response(200, text="ok")

    transport = common.ResolveTransport({"llm.example": "10.1.2.3"}, Recorder())
    with httpx.Client(transport=transport) as client:
        assert client.get("https://llm.example/api/me").text == "ok"
        client.get("https://other.example/x")
    dialed, untouched = seen
    assert dialed.url.host == "10.1.2.3"
    assert dialed.headers["host"] == "llm.example"
    assert dialed.extensions["sni_hostname"] == "llm.example"
    assert untouched.url.host == "other.example"


def test_parse_resolve_rejects_garbage():
    assert common.parse_resolve(["a.b:1.2.3.4"]) == {"a.b": "1.2.3.4"}
    with pytest.raises(ValueError):
        common.parse_resolve(["nonsense"])
