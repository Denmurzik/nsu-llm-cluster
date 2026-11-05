"""Scoped mirror tokens (SEC-1 rule 4, SEC-2 L2): main/hf_mirror.py opens to a node only the repositories its token names.

The token format is the portal's (portal/app/mirror_tokens.py); `mint` below is the same algorithm, checked against the test
vector the portal side agreed on: mint("k", "*", ["a/b"]) has the payload eyJjIjoiKiIsInIiOlsiYS9iIl19.
"""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MAIN))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_mirror  # noqa: E402
from test_hf_mirror import MIRROR_TOKEN, UPSTREAM_TOKEN, FakeHub, Running, _disk_reserve, blob  # noqa: E402,F401


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint(secret: str, client: str, repos) -> str:
    payload = json.dumps({"c": client, "r": sorted({r for r in repos if r})}, separators=(",", ":"), ensure_ascii=True)
    body = f"nsm1.{_b64(payload.encode())}"
    return f"{body}.{_b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}"


def test_the_agreed_test_vector():
    token = mint("k", "*", ["a/b"])
    assert token.split(".")[1] == "eyJjIjoiKiIsInIiOlsiYS9iIl19"
    assert hf_mirror.scoped_token_repos("k", token) == {"a/b"}


@pytest.mark.parametrize("token", [
    mint("other", "*", ["a/b"]),                                         # signed with another key
    mint("k", "*", ["a/b"]).replace("eyJjIjoiKiIsInIiOlsiYS9iIl19", _b64(b'{"c":"*","r":["a/b","x/y"]}')),  # payload swapped
    "nsm1.e30",                                                          # two parts
    "nsm2." + mint("k", "*", ["a/b"]).split(".", 1)[1],                  # another prefix
    "nsm1.!!!." + mint("k", "*", []).split(".")[2],                      # not base64url
    "hf_" + "ab" * 20,
    "nsm1." + "A" * 20000 + ".x",                                        # oversized
])
def test_forged_or_malformed_tokens_open_nothing(token):
    assert hf_mirror.scoped_token_repos("k", token) is None


def _mint_payload(secret: str, payload: dict) -> str:
    body = "nsm1." + _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}"


def test_an_optional_expiry_is_enforced():
    import time

    assert hf_mirror.scoped_token_repos("k", _mint_payload("k", {"c": "*", "r": ["a/b"], "e": int(time.time()) + 600})) == {"a/b"}
    assert hf_mirror.scoped_token_repos("k", _mint_payload("k", {"c": "*", "r": ["a/b"], "e": int(time.time()) - 1})) is None
    assert hf_mirror.scoped_token_repos("k", _mint_payload("k", {"c": "*", "r": ["a/b"], "e": "never"})) is None
    assert hf_mirror.scoped_token_repos("k", _mint_payload("k", {"c": "*", "r": ["a/b"], "e": True})) is None


def test_no_secret_no_scoped_tokens():
    assert hf_mirror.scoped_token_repos("", mint("", "*", ["a/b"])) is None


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.close()


@pytest.fixture
def mirror(tmp_path, hub):
    hub.add("org/m", "model.gguf", blob(2000))
    hub.add("org/secret", "model.gguf", blob(2000))
    r = Running(hf_mirror.Mirror(root=tmp_path / "data", upstream=hub.url, upstream_token=UPSTREAM_TOKEN,
                                 mirror_token=MIRROR_TOKEN, max_bytes=10**9, revalidate_s=3600))
    yield r
    r.mirror.stop()
    r.close()


def _get(m, path, token, method="GET"):
    return m.request(method, path, {"Authorization": f"Bearer {token}"}, auth=False)[0].status


def test_a_node_token_opens_only_its_repositories(mirror, hub):
    node = mint(MIRROR_TOKEN, "pc-07", ["org/m"])
    pods = mint(MIRROR_TOKEN, "*", ["org/m"])
    assert _get(mirror, "/c/pc-07/org/m/resolve/main/model.gguf", node) == 200
    assert _get(mirror, "/c/pc-07/org/m/resolve/main/model.gguf", node, "HEAD") == 200
    assert _get(mirror, "/c/pc-07/api/models/org/m/revision/main", node) == 200
    assert _get(mirror, "/org/m/resolve/main/model.gguf", pods) == 200
    assert _get(mirror, "/c/pc-03/org/m/resolve/main/model.gguf", pods) == 200
    # the operator's HF_TOKEN is never used for a repository the portal did not give this node
    before = len(hub.hub_requests)
    assert _get(mirror, "/c/pc-07/org/secret/resolve/main/model.gguf", node) == 403
    assert _get(mirror, "/c/pc-07/api/models/org/secret/revision/main", node) == 403
    assert _get(mirror, "/api/models/org/secret", pods) == 403
    assert len(hub.hub_requests) == before


def test_a_token_minted_for_one_node_works_only_under_its_name(mirror):
    node = mint(MIRROR_TOKEN, "pc-07", ["org/m"])
    assert _get(mirror, "/org/m/resolve/main/model.gguf", node) == 403
    assert _get(mirror, "/c/pc-08/org/m/resolve/main/model.gguf", node) == 403


def test_a_node_token_has_no_admin_and_no_transfer_report(mirror):
    node = mint(MIRROR_TOKEN, "*", ["org/m"])
    assert _get(mirror, "/_mirror/transfers", node) == 403
    assert _get(mirror, "/admin/cache", node) == 403
    assert _get(mirror, "/admin/prefetch", node) == 403
    assert mirror.request("DELETE", "/admin/cache/org/m", {"Authorization": f"Bearer {node}"}, auth=False)[0].status == 403


def test_the_mirror_key_itself_still_opens_everything(mirror):
    assert _get(mirror, "/org/secret/resolve/main/model.gguf", MIRROR_TOKEN) == 200
    assert _get(mirror, "/_mirror/transfers", MIRROR_TOKEN) == 200
    assert _get(mirror, "/admin/cache", MIRROR_TOKEN) == 200


@pytest.fixture
def with_admin(tmp_path, hub):
    hub.add("org/m", "model.gguf", blob(2000))
    r = Running(hf_mirror.Mirror(root=tmp_path / "data2", upstream=hub.url, upstream_token=UPSTREAM_TOKEN,
                                 mirror_token=MIRROR_TOKEN, admin_token="admin-key", max_bytes=10**9, revalidate_s=3600))
    yield r
    r.mirror.stop()
    r.close()


def test_with_an_admin_key_the_signing_key_is_no_bearer_token(with_admin):
    """HF_MIRROR_ADMIN_TOKEN is the portal's key (portal/app/config.py mirror_admin_key); HF_MIRROR_TOKEN only signs."""
    assert _get(with_admin, "/admin/cache", "admin-key") == 200
    assert _get(with_admin, "/_mirror/transfers", "admin-key") == 200
    assert _get(with_admin, "/org/m/resolve/main/model.gguf", "admin-key") == 200
    assert _get(with_admin, "/admin/cache", MIRROR_TOKEN) == 401
    assert _get(with_admin, "/org/m/resolve/main/model.gguf", MIRROR_TOKEN) == 401
    assert _get(with_admin, "/org/m/resolve/main/model.gguf", mint(MIRROR_TOKEN, "*", ["org/m"])) == 200
    assert _get(with_admin, "/org/m/resolve/main/model.gguf", mint("admin-key", "*", ["org/m"])) == 401


def test_a_token_signed_with_another_key_is_refused(mirror):
    assert _get(mirror, "/org/m/resolve/main/model.gguf", mint("hf_" + "cd" * 20, "*", ["org/m"])) == 401
