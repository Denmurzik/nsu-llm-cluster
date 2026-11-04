"""The k3s server gets the server token through K3S_TOKEN and the agent token through K3S_AGENT_TOKEN, never through
argv: /proc/<pid>/cmdline of PID 1 is readable by every process in the container, its environ only by root. k3s writes
server/agent-token (K10...::node:<agent token>), which the portal shows on the Nodes page."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def k3s_exec() -> str:
    """The `exec k3s server` command with its continuation lines (comments skipped)."""
    text = (ROOT / "main" / "entrypoint.sh").read_text(encoding="utf-8")
    return re.search(r"^[^#\n]*exec k3s server(?:.*\\\n)*.*$", text, re.M).group(0)


def test_k3s_server_gets_the_token_from_the_environment():
    cmd = k3s_exec()
    assert cmd.startswith('K3S_TOKEN="$CLUSTER_TOKEN" K3S_AGENT_TOKEN="$AGENT_TOKEN" exec k3s server')
    assert "--token" not in cmd and "--agent-token" not in cmd
    assert "--node-name llm-main" in cmd and cmd.endswith("--write-kubeconfig-mode 0644")


def test_k3s_server_disables_the_unused_addons():
    """metrics-server is unused (the portal reads no k3s metrics) and is the only reason for kubelet port 10250."""
    assert "--disable traefik,servicelb,metrics-server" in k3s_exec()
