#!/usr/bin/env bash
# Container entrypoint (PID 1). Modes:
#   args given        -> exec "$@" (used as the pod command inside the cluster)
#   MODE=k3s          -> exec k3s agent (default)
#   MODE=standalone   -> exec registrar.sh (registers in the portal, no Kubernetes)
set -euo pipefail

if [ $# -gt 0 ]; then
  exec "$@"
fi

MODE="${MODE:-k3s}"
NODE_NAME="${NODE_NAME:-$(hostname)}"
NODE_NAME="${NODE_NAME,,}"
NODE_IP="${NODE_IP:-}"
ACCEL="${ACCEL:-auto}"
if [ "$ACCEL" = "auto" ] || [ -z "$ACCEL" ]; then
  ACCEL="$(detect-accel.sh)"
fi
MAIN_URL="${MAIN_URL:-}"
if [ -z "$MAIN_URL" ] && [ -n "${MAIN_HOST:-}" ]; then
  MAIN_URL="https://$MAIN_HOST"
fi
export MODE NODE_NAME NODE_IP ACCEL MAIN_URL

case "$MODE" in
  k3s)
    : "${MAIN_HOST:?MAIN_HOST is required in k3s mode}"
    : "${CLUSTER_TOKEN:?CLUSTER_TOKEN is required}"
    # k3s reads the token from K3S_TOKEN: as --token it would sit in the agent's world-readable command line.
    export K3S_TOKEN="$CLUSTER_TOKEN"
    unset CLUSTER_TOKEN
    args=(agent
      --server "https://$MAIN_HOST:6443"
      --node-name "$NODE_NAME"
      --node-label "llm.nsu/accel=$ACCEL"
      # system pods (coredns, local-path-provisioner) do not tolerate the taint and stay on llm-main;
      # inference pods carry the toleration (portal/app/workloads/builders.py)
      --node-taint "llm.nsu/class=true:NoSchedule")
    if [ "$ACCEL" = "nvidia" ]; then
      vram="$(detect-accel.sh --vram)"
      [ -n "$vram" ] && args+=(--node-label "llm.nsu/vram-mib=$vram")
    fi
    # memory of the node for split placement; run-node.sh/.ps1 pass what Docker reports (in WSL: the memory of WSL)
    ram="${RAM_MIB:-$(detect-accel.sh --ram)}"
    [[ "$ram" =~ ^[0-9]+$ ]] && args+=(--node-label "llm.nsu/ram-mib=$ram")
    [ -n "$NODE_IP" ] && args+=(--node-external-ip "$NODE_IP")
    # k3s identifies an agent by /etc/rancher/node/password, which is outside the k3s volume: a re-created
    # container would get a new password and the server answers "Node password rejected". Keep it on the volume.
    node_etc="${K3S_NODE_ETC:-/etc/rancher/node}"
    k3s_data="${K3S_DATA_DIR:-/var/lib/rancher/k3s}"
    mkdir -p "$node_etc" "$k3s_data"
    (
      umask 077
      if [ ! -s "$k3s_data/node-password" ]; then
        if [ -s "$node_etc/password" ]; then
          cp "$node_etc/password" "$k3s_data/node-password"
        else
          head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$k3s_data/node-password"
        fi
      fi
      cp "$k3s_data/node-password" "$node_etc/password"
      chmod 600 "$k3s_data/node-password" "$node_etc/password"
    )
    # Wake-on-LAN: the NIC run-node passed (WOL_MAC, WOL_IP, WOL_PREFIX) is reported to the portal in the background
    # until the portal takes it; the token reaches the registrar through its environment, never argv.
    if [ -n "${WOL_MAC:-}" ]; then
      CLUSTER_TOKEN="$K3S_TOKEN" registrar.sh --wake-report &
    fi
    # Pod images come from the registry of main first (https://$MAIN_HOST/v2/, read-only, the campus LAN) and only then from
    # the internet: k3s appends the original registry to the endpoint list of a mirror. Pods ask for the bare name
    # nsu-llm-node (docker.io/library) or for ghcr.io/<owner>/nsu-llm-node; main keeps the repository as nsu-llm-node.
    # Other docker.io images answer 404 there and fall back at once. REGISTRY_MIRROR=0 switches the mirror off.
    if [ "${REGISTRY_MIRROR:-1}" != 0 ]; then
      reg_file="${K3S_REGISTRIES_FILE:-/etc/rancher/k3s/registries.yaml}"
      reg_ca="${STATE_DIR:-/var/lib/llm-node}/ca.crt"
      mkdir -p "$(dirname "$reg_file")"
      if [ "${MAIN_INSECURE_TLS:-}" = 1 ]; then reg_tls='insecure_skip_verify: true'
      elif [ -s "$reg_ca" ]; then reg_tls="ca_file: \"$reg_ca\""
      else reg_tls='insecure_skip_verify: false'; fi
      cat > "$reg_file" <<REGISTRIES
mirrors:
  docker.io:
    endpoint:
      - "https://$MAIN_HOST"
    rewrite:
      "^library/(nsu-llm-node)\$": "\$1"
  ghcr.io:
    endpoint:
      - "https://$MAIN_HOST"
    rewrite:
      "^[^/]+/(nsu-llm-node)\$": "\$1"
configs:
  "$MAIN_HOST":
    tls:
      $reg_tls
REGISTRIES
    fi
    # k3s must stay PID 1: it evacuates the root cgroup (cgroup v2) only as PID 1.
    exec k3s "${args[@]}"
    ;;
  standalone)
    : "${MAIN_URL:?MAIN_URL or MAIN_HOST is required in standalone mode}"
    : "${CLUSTER_TOKEN:?CLUSTER_TOKEN is required}"
    : "${NODE_IP:?NODE_IP is required in standalone mode}"
    exec registrar.sh
    ;;
  *)
    echo "unknown MODE: $MODE (expected k3s or standalone)" >&2
    exit 2
    ;;
esac
