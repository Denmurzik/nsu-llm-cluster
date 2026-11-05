#!/usr/bin/env bash
# Запускает (или удаляет) контейнер главного сервера NSU LLM Cluster на Linux-хосте.
# Использование:
#   run-main.sh [--env-file FILE] [--advertise-ip IP] [--airgap] [--airgap-dir DIR] [--certs DIR]
#               [--image IMAGE] [--dry-run]
#   run-main.sh --uninstall [--dry-run]
#   run-main.sh --print-token [--dry-run]
#
#   --env-file      файл с переменными (OIDC_CLIENT_SECRET, PUBLIC_HOSTNAME, ADMIN_EMAILS, ...), по умолчанию .env
#   --advertise-ip  LAN-адрес этого хоста; по умолчанию MAIN_ADVERTISE_IP из env-файла, иначе определяется сам
#   --airgap        положить в каталог образов k3s системные образы k3s (архив релиза, sha256 проверяется), postgres,
#                   redis и LiteLLM: главный стартует без выхода в интернет
#   --certs         каталог с tls.crt, tls.key (и необязательно ca.crt): свой сертификат вместо внутреннего CA Caddy
#   --uninstall     удалить контейнер и тома llm-main-k3s, llm-main-kubelet, llm-main-data, llm-main-hf, llm-main-registry (данные, секреты, кэш моделей и реестр образов теряются)
#   --print-token   напечатать токен подключения нод (токен агента k3s, для run-node --token) и выйти
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash в Windows: /run и /var/run в аргументах docker не превращать в пути Windows

VERSION="0.2.0"
CONTAINER="llm-main"
IMAGE="nsu-llm-main:$VERSION"
ENV_FILE=".env"
ADVERTISE_IP=""
AIRGAP=0
AIRGAP_DIR="${NSU_LLM_AIRGAP_DIR:-$HOME/.nsu-llm-main/images}"
CERTS=""
UNINSTALL=0
PRINT_TOKEN=0
DRY=0
# сколько ждать, пока k3s запишет токен агента (первый запуск k3s занимает десятки секунд)
TOKEN_WAIT_S="${NSU_LLM_TOKEN_WAIT_S:-120}"
AIRGAP_IMAGES=("postgres:17-alpine" "redis:7-alpine" "ghcr.io/berriai/litellm:v1.106.6" "registry:2.8.3")
# версия k3s в образе (ARG K3S_VERSION в main/Dockerfile): системные образы (pause, coredns, local-path, metrics-server)
# берутся из архива того же релиза
K3S_VERSION="v1.36.5+k3s1"
K3S_RELEASE_URL="${NSU_LLM_K3S_RELEASE_URL:-https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION/+/%2B}}"
K3S_AIRGAP_FILE="k3s-airgap-images-amd64.tar.zst"

die() { echo "Ошибка: $*" >&2; exit 1; }
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --advertise-ip) ADVERTISE_IP="${2:?}"; shift 2 ;;
    --airgap) AIRGAP=1; shift ;;
    --airgap-dir) AIRGAP_DIR="${2:?}"; shift 2 ;;
    --certs) CERTS="${2:?}"; shift 2 ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --print-token) PRINT_TOKEN=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done

# Под MSYS/Git Bash docker.exe не понимает POSIX-путей вида /c/...: передаём ему путь Windows.
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# run CMD...: при --dry-run только печатает команду
run() {
  if [ "$DRY" = 1 ]; then printf '%q ' "$@"; echo; else "$@"; fi
}

# agent_token: токен подключения нод из контейнера (agent-token.sh; серверный токен k3s он не печатает),
# повторяет попытку, пока k3s не запишет токен, не дольше TOKEN_WAIT_S
agent_token() {
  local deadline=$((SECONDS + TOKEN_WAIT_S)) t
  while :; do
    if t="$(docker exec "$CONTAINER" /opt/llm/main/agent-token.sh 2>/dev/null)" && [ -n "$t" ]; then
      printf '%s\n' "$t"
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 3
  done
}

if [ "$PRINT_TOKEN" = 1 ]; then
  if [ "$DRY" = 1 ]; then run docker exec "$CONTAINER" /opt/llm/main/agent-token.sh; exit 0; fi
  agent_token || die "токен подключения нод не получен: контейнер $CONTAINER не запущен или k3s ещё стартует (docker logs $CONTAINER), повторите через минуту"
  exit 0
fi

if [ "$UNINSTALL" = 1 ]; then
  run docker rm -f "$CONTAINER" || true
  run docker volume rm llm-main-k3s llm-main-kubelet llm-main-data llm-main-hf llm-main-registry || true
  [ "$DRY" = 1 ] || echo "Главный сервер удалён: контейнер и тома llm-main-*. Каталог образов $AIRGAP_DIR не тронут."
  exit 0
fi

[ "$DRY" = 1 ] || docker info >/dev/null 2>&1 || die "docker недоступен (docker info завершился ошибкой)"
[ -f "$ENV_FILE" ] || die "файл $ENV_FILE не найден: создайте его (обязательны OIDC_CLIENT_SECRET и PUBLIC_HOSTNAME)"
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1; }

# Адрес: параметр, затем env-файл (передаётся контейнеру через --env-file), затем автоопределение.
EXTRA_ENV=()
if [ -z "$ADVERTISE_IP" ] && [ -z "$(env_value MAIN_ADVERTISE_IP)" ]; then
  if command -v ip >/dev/null 2>&1; then
    ADVERTISE_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  fi
  [ -n "$ADVERTISE_IP" ] || ADVERTISE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ADVERTISE_IP" ] || die "не удалось определить адрес хоста, укажите --advertise-ip"
fi
[ -z "$ADVERTISE_IP" ] || EXTRA_ENV=(-e "MAIN_ADVERTISE_IP=$ADVERTISE_IP")

# GPU хоста (NVIDIA): портал показывает её на странице «Состояние». В разделение моделей главный не входит.
# nvidia-smi нет или он не отвечает: переменная не передаётся. Своё значение MAIN_GPU в env-файле главнее.
if [ -z "$(env_value MAIN_GPU)" ] && command -v nvidia-smi >/dev/null 2>&1; then
  HOST_GPU="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | tr -d '\r' | paste -sd ';' - | sed 's/;/; /g')" || HOST_GPU=""
  [ -z "$HOST_GPU" ] || EXTRA_ENV+=(-e "MAIN_GPU=$HOST_GPU")
fi

if [ "$DRY" != 1 ]; then
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "образ $IMAGE не найден (docker build -f main/Dockerfile -t $IMAGE . или docker load)"
fi

# Digest образа главного для блока «Версии» («Состояние»): изнутри контейнера его не видно, его знает тот, кто запускает. Digest реестра
# (RepoDigests, если образ скачан с ghcr.io), иначе id образа (собранного на месте); не определился: переменная не передаётся.
if [ "$DRY" != 1 ]; then
  main_digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE" 2>/dev/null | tr -d '\r' \
    | sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' | head -n1)"
  [ -n "$main_digest" ] || main_digest="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null | tr -d '\r' | head -n1)"
  [[ "$main_digest" =~ ^sha256:[0-9a-f]{64}$ ]] && EXTRA_ENV+=(-e "MAIN_IMAGE_DIGEST=$main_digest")
fi

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

# Архив системных образов k3s: скачивается, если его нет или он не совпадает с sha256sum-amd64.txt релиза.
fetch_k3s_airgap() {
  local dest="$AIRGAP_DIR/$K3S_AIRGAP_FILE" sums want got
  if [ "$DRY" = 1 ]; then
    run curl -fL --retry 3 -o "$(host_path "$dest")" "$K3S_RELEASE_URL/$K3S_AIRGAP_FILE"
    echo "# sha256 сверяется с $K3S_RELEASE_URL/sha256sum-amd64.txt"
    return
  fi
  sums="$(curl -fsSL --retry 3 "$K3S_RELEASE_URL/sha256sum-amd64.txt")" \
    || die "не удалось скачать контрольные суммы k3s $K3S_VERSION ($K3S_RELEASE_URL/sha256sum-amd64.txt)"
  want="$(awk -v f="$K3S_AIRGAP_FILE" '$2 == f {print $1}' <<<"$sums")"
  [ -n "$want" ] || die "в sha256sum-amd64.txt k3s $K3S_VERSION нет строки для $K3S_AIRGAP_FILE"
  if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$want" ]; then
    echo "Системные образы k3s $K3S_VERSION уже скачаны: $dest"
    return
  fi
  # host_path: под Git Bash curl бывает программой Windows и не понимает путей /c/...
  curl -fL --retry 3 -o "$(host_path "$dest.part")" "$K3S_RELEASE_URL/$K3S_AIRGAP_FILE" \
    || { rm -f "$dest.part"; die "не удалось скачать $K3S_RELEASE_URL/$K3S_AIRGAP_FILE"; }
  got="$(sha256_of "$dest.part")"
  if [ "$got" != "$want" ]; then
    rm -f "$dest.part"
    die "контрольная сумма $K3S_AIRGAP_FILE не совпала (ожидалась $want, получена $got): архив повреждён или подменён, повторите --airgap"
  fi
  mv "$dest.part" "$dest"
}

# Офлайн-режим: системные образы k3s, postgres, redis и LiteLLM кладутся в каталог, который k3s импортирует при старте.
if [ "$AIRGAP" = 1 ]; then
  run mkdir -p "$AIRGAP_DIR"
  fetch_k3s_airgap
  for img in "${AIRGAP_IMAGES[@]}"; do
    file="$AIRGAP_DIR/$(echo "$img" | tr '/:' '__').tar"
    run docker pull --platform linux/amd64 "$img"
    run docker save --platform linux/amd64 -o "$(host_path "$file")" "$img"
  done
fi

if [ "$DRY" = 1 ] || docker inspect "$CONTAINER" >/dev/null 2>&1; then run docker rm -f "$CONTAINER"; fi

# Логи supervisord, Caddy, портала и контроллеров идут в stdout контейнера, а Docker по умолчанию не ограничивает размер
# его журнала: здесь 5 файлов по 50 МБ, старые удаляются.
# nofile: Docker 25+ под systemd оставляет контейнеру мягкий лимит 1024 дескрипторов, а 1000 пользователей держат
# тысячи соединений через Caddy и LiteLLM (EMFILE).
ARGS=(run -d --name "$CONTAINER" --hostname llm-main --privileged --restart unless-stopped
  --ulimit nofile=1048576:1048576
  --log-driver json-file --log-opt max-size=50m --log-opt max-file=5
  --tmpfs /run --tmpfs /var/run
  # только IPv4 (SEC-2 L1): по IPv6 контейнер без IPv6 получает соединение через docker-proxy с адресом шлюза bridge (172.x),
  # и фильтры по адресу клиента (REGISTRY_ALLOW_FROM, NODE_ALLOW_FROM) приняли бы любой адрес из интернета за частный
  -p 0.0.0.0:443:443 -p 0.0.0.0:80:80 -p 0.0.0.0:6443:6443
  -v llm-main-k3s:/var/lib/rancher/k3s -v llm-main-kubelet:/var/lib/kubelet -v llm-main-data:/var/lib/llm
  -v llm-main-hf:/var/lib/llm-hf -v llm-main-registry:/var/lib/llm-registry
  --env-file "$(host_path "$ENV_FILE")")
if [ "$AIRGAP" = 1 ] || compgen -G "$AIRGAP_DIR/*.tar" >/dev/null || compgen -G "$AIRGAP_DIR/*.tar.zst" >/dev/null; then
  ARGS+=(-v "$(host_path "$AIRGAP_DIR"):/var/lib/rancher/k3s/agent/images:ro")
fi
if [ -n "$CERTS" ]; then
  ARGS+=(-v "$(host_path "$(cd "$CERTS" && pwd)"):/certs:ro" -e TLS_CERT=1)
fi
ARGS+=("${EXTRA_ENV[@]}" "$IMAGE")
run docker "${ARGS[@]}"

if [ "$DRY" != 1 ]; then
  echo "Главный сервер запущен: контейнер $CONTAINER, образ $IMAGE."
  echo "Первый запуск занимает несколько минут (k3s скачивает образы postgres и LiteLLM). Проверка:"
  echo "  curl -sk https://${ADVERTISE_IP:-$(env_value MAIN_ADVERTISE_IP)}/readyz"
  echo "Откройте порты 443, 80 и 6443/tcp в брандмауэре хоста (nodes подключаются к 6443)."
  echo "Ожидание токена подключения нод (k3s запускается, до $TOKEN_WAIT_S с)..."
  if token="$(agent_token)"; then
    echo "Токен подключения нод (токен агента k3s, храните его как пароль): $token"
    echo "  sudo ./node/run-node.sh --main ${ADVERTISE_IP:-$(env_value MAIN_ADVERTISE_IP)} --token $token"
  else
    echo "Токен подключения нод пока не готов: выполните позже $0 --print-token или откройте раздел «Ноды» в интерфейсе."
  fi
fi
