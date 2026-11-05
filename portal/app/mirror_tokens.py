"""Scoped tokens for the Hugging Face mirror on main (task SEC-1 rule 4, infra finding L2).

A node never gets HF_MIRROR_TOKEN itself: with it, it could download through main every repository the operator's
HF_TOKEN sees (gated and private ones too) and every own model. It gets a token that names the repositories the portal
assigned to it, signed with HF_MIRROR_TOKEN (main/hf_mirror.py checks the signature and the repository of each request):

    nsm1.<b64url(payload)>.<b64url(HMAC-SHA256(HF_MIRROR_TOKEN, "nsm1." + b64url(payload)))>
    payload = {"c": "<client: the node, or * for the pods of a deployment>", "r": ["<repo>", ...]}  (compact JSON, sorted)

base64url without padding. No expiry: the token only opens files of repositories the node serves anyway, and a new
HF_MIRROR_TOKEN revokes every one. The same input always gives the same token, so a pod template that carries it does
not change (and restart the pods) while the deployment's repositories stay the same.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Iterable

PREFIX = "nsm1"
ANY_CLIENT = "*"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint(secret: str, client: str, repos: Iterable[str]) -> str:
    """The token for `client` (a node name, or ANY_CLIENT) to the repositories `repos`; empty without a secret."""
    if not secret:
        return ""
    payload = json.dumps({"c": client, "r": sorted({r for r in repos if r})}, separators=(",", ":"), ensure_ascii=True)
    body = f"{PREFIX}.{_b64(payload.encode())}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def repos_of(repo: str | None, draft_repo: str | None = None, mmproj_file: str | None = None) -> set[str]:
    """The repositories of a deployment's files: the model, its draft model, and the repository of an own projector
    (`local/<name>/<file>`; a projector of the model's repository is already the model's)."""
    repos = {r for r in (repo, draft_repo) if r}
    if mmproj_file and mmproj_file.startswith("local/"):
        repos.add("local/" + mmproj_file[len("local/"):].partition("/")[0])
    return repos
