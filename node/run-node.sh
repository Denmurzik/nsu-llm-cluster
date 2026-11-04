#!/usr/bin/env bash
# Starts (or removes) the NSU LLM node container on this Linux host.
# Usage: run-node.sh --main HOST --token TOKEN [options]      (see node/README.md)
set -euo pipefail

usage() {
  cat <<'EOF'
Использование:
  run-node.sh --main HOST --token TOKEN [--mode k3s|standalone] [--accel auto|cpu|nvidia]
              [--node-ip IP] [--name NAME] [--network bridge|host] [--image IMAGE]
              [--data-dir DIR] [--insecure-tls] [--from-main] [--dry-run]
  run-node.sh --uninstall --main HOST --name NAME [--token TOKEN] [--network bridge|host] [--data-dir DIR]
  --from-main  скачать образ ноды с главного (реестр https://HOST/v2/, node/pull-image.sh, нужен root) вместо docker load
EOF
}

MAIN=""
TOKEN=""
MODE="k3s"
ACCEL="auto"
NODE_IP=""
NAME=""
NETWORK="bridge"
IMAGE=""
DATA_DIR="/var/lib/nsu-llm-node"
INSECURE=0
FROM_MAIN=0
DRY=0
UNINSTALL=0
VERSION="0.1.0"
CONTAINER="llm-node"

die() { echo "Ошибка: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --main) MAIN="${2:?}"; shift 2 ;;
    --token) TOKEN="${2:?}"; shift 2 ;;
    --mode) MODE="${2:?}"; shift 2 ;;
    --accel) ACCEL="${2:?}"; shift 2 ;;
    --node-ip) NODE_IP="${2:?}"; shift 2 ;;
    --name) NAME="${2:?}"; shift 2 ;;
    --network) NETWORK="${2:?}"; shift 2 ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:?}"; shift 2 ;;
    --insecure-tls) INSECURE=1; shift ;;
    --from-main) FROM_MAIN=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done

[ -n "$MAIN" ] || { usage >&2; die "--main обязателен"; }
if [ "$UNINSTALL" = 0 ] && [ -z "$TOKEN" ]; then die "--token обязателен"; fi
case "$MODE" in k3s|standalone) ;; *) die "--mode: k3s или standalone" ;; esac
case "$ACCEL" in auto|cpu|nvidia) ;; *) die "--accel: auto, cpu или nvidia" ;; esac
case "$NETWORK" in bridge|host) ;; *) die "--network: bridge или host" ;; esac

NAME="${NAME:-$(hostname)}"
NAME="${NAME,,}"
# Same rule as the portal (DNS-1123 label, nodes/routes.py): a name it rejects could never register or be deleted.
[[ "$NAME" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]] || die "недопустимое имя ноды: $NAME (латинские буквы, цифры и дефис, до 63 символов)"
MAIN_URL="https://$MAIN"

# run CMD...: print under --dry-run, execute otherwise
run() {
  if [ "$DRY" = 1 ]; then printf '%q ' "$@"; echo; else "$@"; fi
}

# Download the portal CA once (trust on first use) so that later calls verify the server.
fetch_ca() {
  [ "$DRY" = 1 ] && return 0
  [ "$INSECURE" = 1 ] && return 0
  [ -s "$DATA_DIR/state/ca.crt" ] && return 0
  mkdir -p "$DATA_DIR/state"
  if curl -sSf -k --connect-timeout 5 -m 15 -o "$DATA_DIR/state/ca.crt.tmp" "$MAIN_URL/ca.crt" 2>/dev/null; then
    mv "$DATA_DIR/state/ca.crt.tmp" "$DATA_DIR/state/ca.crt"
  else
    rm -f "$DATA_DIR/state/ca.crt.tmp"
  fi
}

tls_args() {
  if [ "$INSECURE" = 1 ]; then echo "-k"
  elif [ -s "$DATA_DIR/state/ca.crt" ]; then echo "--cacert $DATA_DIR/state/ca.crt"
  fi
}

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
  # The host-network cleanup runs from the node image: take --image, else the image of the existing
  # container (read before it is removed), else any local nsu-llm-node image.
  # The image is also needed to remove DATA_DIR when its files belong to root (written by the container).
  if [ -z "$IMAGE" ]; then
    IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || true)"
    if [ -z "$IMAGE" ]; then
      IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^nsu-llm-node:' | head -n1 || true)"
    fi
  fi
  # Remove the container first so that the node cannot re-register after the DELETE.
  run docker rm -f "$CONTAINER" || true
  if [ -n "$TOKEN" ]; then
    url="$MAIN_URL/api/nodes/$NAME"
    if [ "$DRY" = 1 ]; then
      echo "curl -X DELETE -sS -m 15 $(tls_args) -H @<(Authorization: Bearer ...) $url"
    else
      # shellcheck disable=SC2046
      code="$(curl -X DELETE -sS -o /dev/null -w '%{http_code}' -m 15 $(tls_args) \
        -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") "$url" 2>&1)" || true
      # 204: removed, 404: already gone. Anything else (no connection, 401, 5xx) leaves the node object behind.
      case "$code" in
        204|404) ;;
        *) echo "Предупреждение: DELETE $url не выполнен (ответ: ${code:-нет}). Нода останется в кластере: удалите её на главном сервере (docker exec llm-main k3s kubectl delete node $NAME)." >&2 ;;
      esac
    fi
  fi
  run docker volume rm llm-node-k3s llm-node-kubelet llm-models || true
  if [ "$NETWORK" = host ] && [ -z "$IMAGE" ]; then
    echo "Предупреждение: образ ноды не найден локально, очистка интерфейсов (cni0, flannel.1) и цепочек iptables (KUBE-*, CNI-*, FLANNEL-*) хоста пропущена. Загрузите образ и повторите --uninstall --network host --image IMAGE." >&2
  elif [ "$NETWORK" = host ]; then
    # k3s with host networking leaves links and iptables chains in the host namespace (research §7.2).
    cleanup='mkdir -p /host-tmp; iptables-save > /host-tmp/nsu-llm-iptables.bak 2>/dev/null || true;
ip link show | grep "master cni0" | while read -r _ iface _; do ip link delete "${iface%%@*}"; done;
for l in cni0 flannel.1 flannel-v6.1 kube-ipvs0 flannel-wg flannel-wg-v6; do ip link delete "$l" 2>/dev/null || true; done;
iptables-save | grep -v KUBE- | grep -v CNI- | grep -iv flannel | iptables-restore;
ip6tables-save | grep -v KUBE- | grep -v CNI- | grep -iv flannel | ip6tables-restore || true'
    run docker run --rm --pull never --privileged --network host -v /tmp:/host-tmp --entrypoint bash "$IMAGE" -c "$cleanup" || true
  fi
  run rm -rf "$DATA_DIR" 2>/dev/null || true
  # Files written by the node container belong to root: without root, remove them through the node image
  # (the parent directory is mounted so that DATA_DIR itself goes too).
  if [ "$DRY" = 0 ] && [ -e "$DATA_DIR" ]; then
    parent="$(dirname "$DATA_DIR")"; base="$(basename "$DATA_DIR")"
    if [ -n "$IMAGE" ]; then
      docker run --rm --pull never -v "$parent:/p" --entrypoint rm "$IMAGE" -rf "/p/$base" || true
    fi
    if [ -e "$DATA_DIR" ]; then
      echo "Предупреждение: каталог $DATA_DIR не удалён (нет прав и нет образа ноды). Удалите его: sudo rm -rf $DATA_DIR" >&2
    fi
  fi
  [ "$DRY" = 1 ] || echo "Нода $NAME удалена."
  exit 0
fi

# ------------------------------------------------------------------ install
has_gpu() { nvidia-smi -L >/dev/null 2>&1; }

if [ "$ACCEL" = auto ]; then
  if has_gpu; then ACCEL=nvidia; else ACCEL=cpu; fi
fi
if [ "$ACCEL" = nvidia ] && [ "$DRY" = 0 ] && ! has_gpu; then
  die "--accel nvidia, но nvidia-smi на этом хосте не работает"
fi
[ -n "$IMAGE" ] || IMAGE="nsu-llm-node:$VERSION-$([ "$ACCEL" = nvidia ] && echo gpu || echo cpu)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$FROM_MAIN" = 1 ]; then
  [[ "$IMAGE" =~ ^nsu-llm-node:[A-Za-z0-9._-]+$ ]] || die "--from-main: образ с главного называется nsu-llm-node:<версия>-cpu|gpu, а не $IMAGE (уберите --image)"
  [ "$UNINSTALL" = 1 ] || [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "--from-main: Docker доверяет реестру главного через /etc/docker/certs.d, нужны права root: sudo $0 ..."
fi

if [ -z "$NODE_IP" ]; then
  NODE_IP="$(ip -4 route get "$MAIN" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)"
  [ -n "$NODE_IP" ] || die "не удалось определить адрес этого хоста, укажите --node-ip"
fi

# k3s applies --node-label only at first registration, so changed labels need a clean re-join.
LABELS="llm.nsu/accel=$ACCEL"
if [ "$ACCEL" = nvidia ]; then
  VRAM="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i 0 2>/dev/null | head -n1 | tr -d ' \r' || true)"
  [ -n "$VRAM" ] && LABELS="$LABELS"$'\n'"llm.nsu/vram-mib=$VRAM"
fi
if [ "$MODE" = k3s ] && [ -f "$DATA_DIR/state/labels" ]; then
  if [ "$(cat "$DATA_DIR/state/labels")" != "$LABELS" ]; then
    die "Метки ноды изменились — выполните --uninstall и запустите снова"
  fi
fi

if [ "$DRY" = 0 ]; then
  docker info >/dev/null 2>&1 || die "docker недоступен"
fi
# Образ с главного (реестр на https://$MAIN/v2/): тот же Docker, имя nsu-llm-node:<версия>-cpu|gpu, дальше всё как с docker load
if [ "$FROM_MAIN" = 1 ]; then
  [ ! -f "$HERE/pull-image.sh" ] && die "нет $HERE/pull-image.sh: скопируйте каталог node целиком"
  # CA главного закрепляется один раз (state/ca.crt, как для портала): по нему проверяется реестр и он же закрепляется для Docker
  ca_args=()
  if [ "$DRY" = 1 ]; then ca_args=(--ca "$DATA_DIR/state/ca.crt")
  else
    mkdir -p "$DATA_DIR/state"
    fetch_ca
    [ ! -s "$DATA_DIR/state/ca.crt" ] || ca_args=(--ca "$DATA_DIR/state/ca.crt")
  fi
  run bash "$HERE/pull-image.sh" --main "$MAIN" --image "$IMAGE" ${ca_args[@]+"${ca_args[@]}"} || die "образ $IMAGE не удалось скачать с главного (см. сообщение выше); запасной путь: docker load -i nsu-llm-node-*.tar.gz без --from-main"
fi
if [ "$DRY" = 0 ]; then
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "образ $IMAGE не найден (--from-main, docker load или docker build)"
  mkdir -p "$DATA_DIR/state" "$DATA_DIR/airgap"
  fetch_ca
  [ "$MODE" = k3s ] && printf '%s\n' "$LABELS" | sed '$d' > /dev/null
  [ "$MODE" = k3s ] && printf '%s' "$LABELS" > "$DATA_DIR/state/labels"
fi

# k3s imports node.tar at start so that pods can use this image without a registry.
if [ "$MODE" = k3s ]; then
  id=""
  [ "$DRY" = 1 ] || id="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
  if [ "$DRY" = 1 ] || [ "$(cat "$DATA_DIR/airgap/node.id" 2>/dev/null || true)" != "$id" ]; then
    run docker save --platform linux/amd64 "$IMAGE" -o "$DATA_DIR/airgap/node.tar"
    [ "$DRY" = 1 ] || printf '%s' "$id" > "$DATA_DIR/airgap/node.id"
  fi
fi

# Re-running replaces the container; volumes (k3s identity, models) stay.
if [ "$DRY" = 0 ] && docker inspect "$CONTAINER" >/dev/null 2>&1; then
  docker rm -f "$CONTAINER" >/dev/null
fi

args=(docker run -d --name "$CONTAINER" --hostname "$NAME" --restart unless-stopped --stop-timeout 45
  -v llm-models:/var/lib/llm-models -v "$DATA_DIR/state:/var/lib/llm-node")
if [ "$MODE" = k3s ]; then
  args+=(--privileged --tmpfs /run --tmpfs /var/run
    --ulimit nproc=65535 --ulimit nofile=65535:65535
    -v llm-node-k3s:/var/lib/rancher/k3s -v llm-node-kubelet:/var/lib/kubelet
    -v "$DATA_DIR/airgap:/var/lib/rancher/k3s/agent/images")
fi
if [ "$NETWORK" = host ]; then
  args+=(--network host)
else
  # 18050 (ggml-rpc-server of split deployments) has no authentication: only a k3s node needs it published,
  # and it must stay inside the class network (docs/deploy-class.md).
  args+=(-p 18080:18080)
  [ "$MODE" = k3s ] && args+=(-p 18050:18050)
fi
[ "$ACCEL" = nvidia ] && args+=(--gpus all)
# The token reaches docker through the environment (-e NAME), not through the docker command line.
export CLUSTER_TOKEN="$TOKEN"
args+=(-e "MODE=$MODE" -e "MAIN_HOST=$MAIN" -e CLUSTER_TOKEN -e "NODE_NAME=$NAME"
  -e "NODE_IP=$NODE_IP" -e "ACCEL=$ACCEL")
[ "$INSECURE" = 1 ] && args+=(-e MAIN_INSECURE_TLS=1)
# Memory Docker may give the node (inside WSL: the memory of WSL), the label llm.nsu/ram-mib for split placement.
# Not in state/labels: a changed WSL size must not stop the node; the portal never trusts it above the kubelet's value.
if [ -z "${RAM_MIB:-}" ] && [ "$DRY" = 0 ]; then
  RAM_MIB="$(docker info --format '{{.MemTotal}}' 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {print int($1 / 1048576)}' || true)"
fi
[ -n "${RAM_MIB:-}" ] && args+=(-e "RAM_MIB=$RAM_MIB")
for v in HF_ENDPOINT HF_TOKEN THREADS; do
  [ -n "${!v:-}" ] && args+=(-e "$v")
done
args+=("$IMAGE")

run "${args[@]}"
if [ "$DRY" = 0 ]; then
  echo "Нода $NAME запущена: контейнер $CONTAINER, образ $IMAGE, режим $MODE, адрес $NODE_IP."
fi
