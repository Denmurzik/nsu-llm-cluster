"""portal.env is the portal's only source of settings: supervisord runs with a clean environment (entrypoint.sh),
so a portal setting missing from portal_vars could no longer be set from .env."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# fixed inside the image or set by supervisord.conf, never taken from .env
INTERNAL = {
    "COOKIE_NAME", "COOKIE_SECURE", "OIDC_SCOPES", "LITELLM_URL", "KUBECONFIG", "AGENT_TOKEN_FILE", "K8S_NAMESPACE",
    "CONTROLLERS_ENABLED",
}


def portal_vars():
    text = (ROOT / "main" / "entrypoint.sh").read_text(encoding="utf-8")
    block = re.search(r"^portal_vars=\((.*?)\)", text, re.S | re.M).group(1)
    return set(block.split())


def settings_fields():
    tree = ast.parse((ROOT / "portal" / "app" / "config.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    return {n.target.id.upper() for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}


def test_every_portal_setting_reaches_portal_env():
    fields = settings_fields()
    assert "DATABASE_URL" in fields and "SESSION_TTL_HOURS" in fields
    assert fields - INTERNAL - portal_vars() == set()


def test_proxy_settings_reach_the_portal():
    # the HF inspector and OIDC discovery go out through the host's proxy, if .env sets one
    assert {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"} <= portal_vars()


def test_database_secrets_stay_out_of_portal_env():
    assert portal_vars().isdisjoint({"POSTGRES_PASSWORD", "LITELLM_DB_PASSWORD", "LITELLM_SALT_KEY"})


def test_portal_settings_do_not_shadow_k3s_variables():
    # k3s server inherits the entrypoint's environment: a portal setting named K3S_* in .env would also configure k3s
    assert {f for f in settings_fields() if f.startswith("K3S_")} == set()


def _portal_env_for(tmp_path, **env):
    """Run the real portal.env loop of entrypoint.sh (from `portal_vars=(` to its `done`) and return the written file."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        import pytest

        pytest.skip("bash is not available")
    text = (ROOT / "main" / "entrypoint.sh").read_text(encoding="utf-8")
    start = text.index("portal_vars=(")
    end = text.index("\ndone\n", start) + len("\ndone\n")
    script = f'set -euo pipefail\nRUN_DIR="{tmp_path.as_posix()}"\n' + text[start:end]
    full_env = {"PATH": __import__("os").environ["PATH"], "CLUSTER_TOKEN": "server-secret", "AGENT_TOKEN": "agent-secret", **env}
    subprocess.run([bash, "-c", script], env=full_env, check=True, capture_output=True)
    return (tmp_path / "portal.env").read_text(encoding="utf-8")


def test_portal_gets_the_server_token_only_while_legacy_nodes_are_allowed(tmp_path):
    assert "CLUSTER_TOKEN=server-secret" in _portal_env_for(tmp_path, NODE_LEGACY_TOKEN="true")
    # not set in .env: the default of this install (entrypoint.sh legacy_default, SEC-2 L3)
    out = _portal_env_for(tmp_path, legacy_default="true")
    assert "CLUSTER_TOKEN=server-secret" in out and "NODE_LEGACY_TOKEN=true" in out


def test_a_fresh_install_does_not_take_the_server_token_from_nodes(tmp_path):
    """SEC-2 L3: without NODE_LEGACY_TOKEN in .env a new install refuses the k3s server token from nodes."""
    for env in ({}, {"legacy_default": "false"}):
        out = _portal_env_for(tmp_path, **env)
        assert "CLUSTER_TOKEN" not in out and "server-secret" not in out, env
        assert "NODE_LEGACY_TOKEN=false" in out, env


def test_an_own_certificate_tells_the_portal_to_send_nodes_to_the_name(tmp_path):
    """With TLS_CERT (run-main --certs) the certificate is usually for PUBLIC_HOSTNAME only: the portal then gives the
    nodes the mirror by name, not by MAIN_ADVERTISE_IP (config.py node_hf_endpoint)."""
    import shutil
    import subprocess

    import pytest

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    text = (ROOT / "main" / "entrypoint.sh").read_text(encoding="utf-8")
    line = 'if [ -n "${TLS_CERT:-}" ]; then MAIN_OWN_CERT=true; fi\n'
    assert text.index(line) < text.index("portal_vars=(")
    start = text.index("portal_vars=(")
    end = text.index("\ndone\n", start) + len("\ndone\n")
    for env, want in (({"TLS_CERT": "1"}, True), ({}, False)):
        script = f'set -euo pipefail\nRUN_DIR="{tmp_path.as_posix()}"\n{line}' + text[start:end]
        full = {"PATH": __import__("os").environ["PATH"], "AGENT_TOKEN": "a", **env}
        subprocess.run([bash, "-c", script], env=full, check=True, capture_output=True)
        assert ("MAIN_OWN_CERT=true" in (tmp_path / "portal.env").read_text(encoding="utf-8")) is want


def test_server_token_stays_out_of_portal_env_when_legacy_is_off(tmp_path):
    for value in ("false", "False", "FALSE", "0", "no", "off"):
        out = _portal_env_for(tmp_path, NODE_LEGACY_TOKEN=value)
        assert "CLUSTER_TOKEN" not in out and "server-secret" not in out, value
        assert "AGENT_TOKEN=agent-secret" in out and f"NODE_LEGACY_TOKEN={value}" in out


LOCAL = "127.0.0.1,localhost,::1"


def _no_proxy_after(**env):
    """Run local_no_proxy of entrypoint.sh with `env` and return (NO_PROXY, no_proxy) it leaves."""
    import os
    import shutil
    import subprocess

    import pytest

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    text = (ROOT / "main" / "entrypoint.sh").read_text(encoding="utf-8")
    fn = re.search(r"^local_no_proxy\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
    script = f'set -euo pipefail\n{fn}local_no_proxy\nprintf "%s|%s" "${{NO_PROXY-unset}}" "${{no_proxy-unset}}"'
    out = subprocess.run([bash, "-c", script], env={"PATH": os.environ["PATH"], **env}, check=True, capture_output=True, text=True)
    return tuple(out.stdout.split("|"))


def test_local_addresses_bypass_the_proxy():
    """httpx and urllib take the proxy from the environment: the portal's calls to LiteLLM, the mirror and Redis on
    127.0.0.1 must not go through it."""
    assert _no_proxy_after() == ("unset", "unset")  # no proxy: nothing changes
    assert _no_proxy_after(HTTPS_PROXY="http://p:3128") == (LOCAL, LOCAL)
    assert _no_proxy_after(http_proxy="http://p:3128", NO_PROXY=".nsu.ru") == (f".nsu.ru,{LOCAL}", f".nsu.ru,{LOCAL}")
    if __import__("os").name != "nt":  # the Windows environment folds NO_PROXY and no_proxy into one variable
        both = _no_proxy_after(HTTP_PROXY="http://p:3128", NO_PROXY="a", no_proxy="b")
        assert both == (f"a,{LOCAL}", f"b,{LOCAL}")


def test_local_no_proxy_runs_before_the_manifests_and_portal_env():
    text = (ROOT / "main" / "entrypoint.sh").read_text(encoding="utf-8")
    call = text.index("\nlocal_no_proxy\n")
    assert call < text.index('render_manifests.py" --out') and call < text.index('>"$RUN_DIR/portal.env"')
