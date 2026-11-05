#!/usr/bin/env bash
# Container entrypoint (PID 1). Modes:
#   args given        -> exec "$@" (used as the pod command inside the cluster)
#   MODE=k3s          -> exec k3s agent (default)
#   MODE=standalone   -> exec registrar.sh (registers in the portal, no Kubernetes)
set -euo pipefail

if [ $# -gt 0 ]; then
  exec "$@"
fi

# supervise CMD...: runs CMD again whenever it ends, with a backoff (SUPERVISE_DELAY_S, 5 s, doubling to 300 s; back to the
# start after a run of 10 min or more), and says in the container log how it ended. The background helpers of the k3s mode
# are never silently gone (task B1: a node whose registrar had ended never got its key). SUPERVISE_RESTARTS limits the
# restarts (tests; unset: no limit).
supervise() {
  set +e  # runs in a subshell of its own: a failing CMD is the case it handles
  local first="${SUPERVISE_DELAY_S:-5}" delay rc started n=0
  delay="$first"
  while :; do
    started=$SECONDS
    "$@"; rc=$?
    if [ -n "${SUPERVISE_RESTARTS:-}" ] && [ "$n" -ge "$SUPERVISE_RESTARTS" ]; then
      echo "$(date -u +%FT%TZ) entrypoint: $(basename "$1") ${*:2} ended (exit code $rc), not started again (SUPERVISE_RESTARTS=$SUPERVISE_RESTARTS)" >&2
      return "$rc"
    fi
    n=$((n + 1))
    [ $((SECONDS - started)) -lt 600 ] || delay="$first"
    echo "$(date -u +%FT%TZ) entrypoint: $(basename "$1") ${*:2} ended (exit code $rc), starting it again in ${delay}s" >&2
    sleep "$delay"
    delay=$((delay * 2)); [ "$delay" -le 300 ] || delay=300
  done
}

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
    # What the portal learns from this container in the background, until it takes it: the image it runs (version, revision, digest;
    # «Версии» of the admin pages and the update from the panel) and the NIC run-node passed (WOL_MAC, WOL_IP, WOL_PREFIX) for
    # Wake-on-LAN. The token reaches the registrar through its environment, never argv.
    # It enrols first (the node's own key, $STATE_DIR/node.key) and keeps the key valid; supervised: restarted if it ends.
    ( export CLUSTER_TOKEN="$K3S_TOKEN"; supervise registrar.sh --report ) &
    # The size of the models volume goes to the portal every few minutes (the «Ноды» page, the cleanup of task 2-26);
    # MODELS_REPORT_S=0 switches it off.
    if [ "${MODELS_REPORT_S:-300}" != 0 ]; then
      ( export CLUSTER_TOKEN="$K3S_TOKEN"; supervise registrar.sh --models-report ) &
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
    # monitoring: the load of the node (GPU, VRAM, RAM, CPU, model disk, llama/rpc up) goes to the portal every 20 s;
    # NODE_METRICS=0 switches it off. The token reaches the collector through its environment, never argv.
    if [ "${NODE_METRICS:-1}" != 0 ]; then
      ( export CLUSTER_TOKEN="$K3S_TOKEN"; supervise node-metrics.sh ) &
    fi
    # The images of older versions leave the containerd of this k3s a few minutes after the start (16 GB limit of a class PC);
    # nothing a container of containerd uses is removed. PRUNE_IMAGES=0 switches it off.
    [ "${PRUNE_IMAGES:-1}" = 0 ] || prune-node-images.sh &
    # k3s must stay PID 1: it evacuates the root cgroup (cgroup v2) only as PID 1.
    exec k3s "${args[@]}"
    ;;
  standalone)
    : "${MAIN_URL:?MAIN_URL or MAIN_HOST is required in standalone mode}"
    : "${CLUSTER_TOKEN:?CLUSTER_TOKEN is required}"
    : "${NODE_IP:?NODE_IP is required in standalone mode}"
    if [ "${NODE_METRICS:-1}" != 0 ]; then
      node-metrics.sh &  # monitoring, as in k3s mode
    fi
    exec registrar.sh
    ;;
  *)
    echo "unknown MODE: $MODE (expected k3s or standalone)" >&2
    exit 2
    ;;
esac
