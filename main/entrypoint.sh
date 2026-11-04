#!/usr/bin/env bash
# Точка входа главного образа NSU LLM Cluster.
#   1. проверка обязательных переменных (код 64 и сообщение по-русски),
#   2. секреты: /var/lib/llm/secrets.env (генерируются при первом запуске, переменные окружения главнее),
#   3. манифесты общих сервисов -> каталог автодеплоя k3s,
#   4. Caddyfile и окружение портала в /run/llm, ConfigMap llm-ca с корневым сертификатом для подов нод,
#   5. supervisord (Caddy, API портала, контроллеры портала) в фоне и exec k3s server: k3s обязан быть PID 1 (cgroup v2).
# LLM_ENTRYPOINT_DRY_RUN=1 завершает работу после шага 4.
set -euo pipefail
umask 077   # секреты и производные файлы читает только root; перед запуском k3s возвращается 022
shopt -u patsub_replacement 2>/dev/null || true

MAIN_DIR=/opt/llm/main
DATA_DIR=/var/lib/llm
SECRETS_FILE="$DATA_DIR/secrets.env"
RUN_DIR=/run/llm
MANIFESTS_DIR=/var/lib/rancher/k3s/server/manifests
CERTS_DIR=/certs

die() { echo "Ошибка: $*" >&2; exit 64; }

# ------------------------------------------------------------------ обязательные переменные
for v in MAIN_ADVERTISE_IP OIDC_CLIENT_SECRET PUBLIC_HOSTNAME; do
  [ -n "${!v:-}" ] || die "не задана обязательная переменная $v (задайте её в файле .env, передаваемом контейнеру)."
done
[[ "$MAIN_ADVERTISE_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || die "MAIN_ADVERTISE_IP должна быть адресом IPv4, получено: $MAIN_ADVERTISE_IP"
[[ "$PUBLIC_HOSTNAME" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] \
  || die "PUBLIC_HOSTNAME должна быть доменным именем (буквы, цифры, точки, дефисы), получено: $PUBLIC_HOSTNAME"
# Параллелизм: воркеры LiteLLM и API портала, длина очереди бэкенда в долях parallel (см. render_manifests.py
# про бюджет соединений Postgres: портал с PORTAL_WORKERS до 4 и LiteLLM с LITELLM_WORKERS до 16 в него помещаются).
: "${LITELLM_WORKERS:=4}"
: "${PORTAL_WORKERS:=4}"
: "${QUEUE_FACTOR:=2}"
check_int() { # check_int NAME MIN MAX
  local v="${!1}"
  if ! [[ "$v" =~ ^[0-9]{1,3}$ ]] || [ "$((10#$v))" -lt "$2" ] || [ "$((10#$v))" -gt "$3" ]; then
    die "$1 должна быть целым числом от $2 до $3, получено: $v"
  fi
}
check_int LITELLM_WORKERS 1 16
check_int PORTAL_WORKERS 1 4
check_int QUEUE_FACTOR 1 64
# пул соединений одного процесса портала: pool_size + max_overflow не больше 30 (бюджет в render_manifests.py)
: "${DB_POOL_SIZE:=10}"
: "${DB_MAX_OVERFLOW:=20}"
check_int DB_POOL_SIZE 1 30
check_int DB_MAX_OVERFLOW 0 29
[ $((10#$DB_POOL_SIZE + 10#$DB_MAX_OVERFLOW)) -le 30 ] \
  || die "DB_POOL_SIZE + DB_MAX_OVERFLOW должна быть не больше 30, получено: $DB_POOL_SIZE + $DB_MAX_OVERFLOW"
export LITELLM_WORKERS PORTAL_WORKERS QUEUE_FACTOR DB_POOL_SIZE DB_MAX_OVERFLOW
# Зеркало Hugging Face для нод (под hf-mirror, https://<PUBLIC_HOSTNAME>/hf/): адрес на главном и лимит кэша.
: "${HF_MIRROR_PORT:=8090}"
[[ "$HF_MIRROR_PORT" =~ ^[0-9]{4,5}$ ]] && [ "$((10#$HF_MIRROR_PORT))" -ge 1024 ] && [ "$((10#$HF_MIRROR_PORT))" -le 65535 ] \
  || die "HF_MIRROR_PORT должна быть целым числом от 1024 до 65535, получено: $HF_MIRROR_PORT"
export HF_MIRROR_PORT
# Фронт-прокси перед главным (nginx на tsga): адреса и подсети через запятую, их X-Forwarded-For и -Proto Caddy принимает.
TRUSTED_PROXIES="${TRUSTED_PROXIES:-}"; TRUSTED_PROXIES="${TRUSTED_PROXIES//[[:space:]]/}"
IFS=, read -ra proxies <<<"$TRUSTED_PROXIES"
for p in ${proxies[@]+"${proxies[@]}"}; do
  [[ "$p" =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?|[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(/[0-9]{1,3})?)$ ]] \
    || die "TRUSTED_PROXIES должна быть списком адресов или подсетей IPv4/IPv6 через запятую, получено: $TRUSTED_PROXIES"
done
# Реестр образов для класса (под registry, https://<PUBLIC_HOSTNAME>/v2/): порт на 127.0.0.1 и подсети, которым Caddy его отдаёт.
: "${REGISTRY_PORT:=5000}"
[[ "$REGISTRY_PORT" =~ ^[0-9]{4,5}$ ]] && [ "$((10#$REGISTRY_PORT))" -ge 1024 ] && [ "$((10#$REGISTRY_PORT))" -le 65535 ] \
  || die "REGISTRY_PORT должна быть целым числом от 1024 до 65535, получено: $REGISTRY_PORT"
: "${REGISTRY_ALLOW_FROM:=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1}"
registry_allow=""
IFS=',' read -ra _cidrs <<<"${REGISTRY_ALLOW_FROM//[[:space:]]/}"
for c in "${_cidrs[@]}"; do
  [[ "$c" =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?|[0-9A-Fa-f]*:[0-9A-Fa-f:]*(/[0-9]{1,3})?)$ ]] \
    || die "REGISTRY_ALLOW_FROM: нужен список адресов или подсетей через запятую (10.1.0.0/16,192.168.5.7), получено: $REGISTRY_ALLOW_FROM"
  registry_allow+="${registry_allow:+ }$c"
done
[ -n "$registry_allow" ] || die "REGISTRY_ALLOW_FROM не может быть пустым: перечислите подсети класса и администраторов"
export REGISTRY_PORT
if [ -n "${TLS_CERT:-}" ]; then
  for f in tls.crt tls.key; do
    [ -f "$CERTS_DIR/$f" ] || die "TLS_CERT задана, но файл $CERTS_DIR/$f не найден: смонтируйте каталог с сертификатом в $CERTS_DIR."
  done
fi

# ------------------------------------------------------------------ секреты
SECRET_NAMES=(LITELLM_MASTER_KEY LITELLM_SALT_KEY POSTGRES_PASSWORD LITELLM_DB_PASSWORD PORTAL_DB_PASSWORD CLUSTER_TOKEN AGENT_TOKEN BACKEND_API_KEY
  HF_MIRROR_TOKEN)
PG_DATA_GLOB="${PG_DATA_GLOB:-/var/lib/rancher/k3s/storage/pvc-*_llm_data-postgres-*}"
K3S_TOKEN_FILE=/var/lib/rancher/k3s/server/token

declare -A stored=()
if [ -f "$SECRETS_FILE" ]; then
  while IFS='=' read -r k v; do
    if [ -n "$k" ]; then stored["$k"]="$v"; fi
  done <"$SECRETS_FILE"
fi

generate() {
  case "$1" in
    LITELLM_MASTER_KEY|LITELLM_SALT_KEY) echo "sk-$(openssl rand -hex 16)" ;;
    CLUSTER_TOKEN|AGENT_TOKEN|BACKEND_API_KEY) echo "$(openssl rand -hex 32)" ;;
    # вид токена Hugging Face (hf_ и не меньше 34 букв и цифр): только такой llama.cpp отправляет зеркалу
    HF_MIRROR_TOKEN) echo "hf_$(openssl rand -hex 20)" ;;
    *) openssl rand -hex 24 ;;
  esac
}

pg_volume_exists() { compgen -G "$PG_DATA_GLOB" >/dev/null; }

# locked_reason NAME: почему секрет нельзя сменить на существующей установке (пусто, если можно)
locked_reason() {
  case "$1" in
    # initdb выполняется один раз: пароли ролей Postgres задаются только при создании тома
    POSTGRES_PASSWORD|LITELLM_DB_PASSWORD|PORTAL_DB_PASSWORD)
      if pg_volume_exists; then echo "том Postgres уже создан: пароль роли внутри базы не изменится"; fi ;;
    LITELLM_SALT_KEY)
      if pg_volume_exists; then echo "том Postgres уже создан: LiteLLM не расшифрует сохранённые в базе модели и учётные данные"; fi ;;
    # токен зашифровывает данные bootstrap k3s: с другим токеном k3s server не стартует, а ноды не подключаются
    CLUSTER_TOKEN)
      if [ -f "$K3S_TOKEN_FILE" ]; then echo "кластер k3s уже создан с прежним токеном: k3s не запустится, ноды не подключатся"; fi ;;
  esac
}

content=""
for name in "${SECRET_NAMES[@]}"; do
  value="${!name:-}"
  if [ -n "$value" ] && [ -n "${stored[$name]:-}" ] && [ "$value" != "${stored[$name]}" ]; then
    reason="$(locked_reason "$name")"
    [ -z "$reason" ] \
      || die "$name в окружении отличается от сохранённого в $SECRETS_FILE, а $reason. Уберите $name из .env (останется прежний) или удалите тома llm-main-* вместе с данными (run-main --uninstall)."
  fi
  [ -n "$value" ] || value="${stored[$name]:-}"
  [ -n "$value" ] || value="$(generate "$name")"
  [[ "$value" =~ ^[A-Za-z0-9._~-]+$ ]] \
    || die "значение $name содержит недопустимые символы: разрешены только A-Z a-z 0-9 . _ ~ -"
  export "$name=$value"
  content+="$name=$value"$'\n'
done

# Ноды подключаются с токеном агента (k3s --agent-token): с ним нельзя получить данные bootstrap сервера k3s.
# Установка, созданная до него, получает AGENT_TOKEN здесь один раз; новое значение k3s принимает и на существующем кластере.
[ "$AGENT_TOKEN" != "$CLUSTER_TOKEN" ] \
  || die "AGENT_TOKEN совпадает с CLUSTER_TOKEN, а токен агента должен быть отдельным. Уберите AGENT_TOKEN из .env (главный создаст его сам)."

mkdir -p "$DATA_DIR"
if [ ! -f "$SECRETS_FILE" ] || [ "$(cat "$SECRETS_FILE")" != "${content%$'\n'}" ]; then
  printf '%s' "$content" >"$SECRETS_FILE.tmp"
  mv "$SECRETS_FILE.tmp" "$SECRETS_FILE"
fi

# ------------------------------------------------------------------ локальные адреса мимо прокси
# httpx и urllib берут прокси из окружения: с прокси в .env запросы портала к LiteLLM, зеркалу и Redis на 127.0.0.1
# ушли бы через него. Локальные адреса дописываются в NO_PROXY и no_proxy (их получают портал, зеркало и k3s).
local_no_proxy() {
  [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}${http_proxy:-}${https_proxy:-}" ] || return 0
  : "${NO_PROXY:=${no_proxy:-}}" "${no_proxy:=$NO_PROXY}"
  NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,::1"
  no_proxy="${no_proxy:+$no_proxy,}127.0.0.1,localhost,::1"
  export NO_PROXY no_proxy
}
local_no_proxy

# ------------------------------------------------------------------ манифесты общих сервисов
python3 "$MAIN_DIR/render_manifests.py" --out "$MANIFESTS_DIR" >/dev/null \
  || die "не удалось подготовить манифесты кластера (см. сообщение выше)."

# ------------------------------------------------------------------ Caddyfile
mkdir -p "$RUN_DIR"
CA_ROOT_DIR="$DATA_DIR/caddy-data/caddy/pki/authorities/local"
if [ -f "$CERTS_DIR/ca.crt" ]; then
  ca_handler=$'\t\troot * '"$CERTS_DIR"$'\n\t\trewrite * /ca.crt'$'\n\t\theader Content-Type application/x-x509-ca-cert'$'\n\t\tfile_server'
elif [ -z "${TLS_CERT:-}" ]; then
  ca_handler=$'\t\troot * '"$CA_ROOT_DIR"$'\n\t\trewrite * /root.crt'$'\n\t\theader Content-Type application/x-x509-ca-cert'$'\n\t\tfile_server'
else
  ca_handler=$'\t\trespond 404'
fi
if [ -n "${TLS_CERT:-}" ]; then
  global_tls=""
  site_tls=$'\ttls '"$CERTS_DIR/tls.crt $CERTS_DIR/tls.key"
else
  global_tls=$'\tlocal_certs'
  site_tls=$'\ttls internal'
fi
trusted=""
[ -z "$TRUSTED_PROXIES" ] || trusted=$'\tservers {\n\t\ttrusted_proxies static '"${TRUSTED_PROXIES//,/ }"$'\n\t}'
caddyfile="$(cat "$MAIN_DIR/Caddyfile.tmpl")"
caddyfile="${caddyfile//@@GLOBAL_TLS@@/$global_tls}"
caddyfile="${caddyfile//@@TRUSTED_PROXIES@@/$trusted}"
caddyfile="${caddyfile//@@SITE_TLS@@/$site_tls}"
caddyfile="${caddyfile//@@CA_HANDLER@@/$ca_handler}"
caddyfile="${caddyfile//@@PUBLIC_HOSTNAME@@/$PUBLIC_HOSTNAME}"
caddyfile="${caddyfile//@@MAIN_ADVERTISE_IP@@/$MAIN_ADVERTISE_IP}"
caddyfile="${caddyfile//@@HF_MIRROR_PORT@@/$HF_MIRROR_PORT}"
caddyfile="${caddyfile//@@REGISTRY_PORT@@/$REGISTRY_PORT}"
caddyfile="${caddyfile//@@REGISTRY_ALLOW_FROM@@/$registry_allow}"
printf '%s\n' "$caddyfile" >"$RUN_DIR/Caddyfile"

# ------------------------------------------------------------------ окружение портала
# Значения экранируются printf %q: файл читается командой `. portal.env` (supervisord.conf).
DATABASE_URL="postgresql+asyncpg://portal:${PORTAL_DB_PASSWORD}@127.0.0.1:5432/portal"
: "${OIDC_ISSUER:=https://sso-ng.nsu.ru/auth/realms/NSU}"
: "${OIDC_CLIENT_ID:=double.fresh.nsuts.ru}"
: "${NODE_IMAGE_REPO:=nsu-llm-node}"
: "${NODE_IMAGE_TAG:=0.1.0}"
: "${INFER_PORT:=18080}"
: "${RPC_PORT:=18050}"
: "${MAX_KEYS_PER_USER:=5}"
: "${KEY_RPM_LIMIT:=60}"
: "${KEY_TPM_LIMIT:=100000}"
: "${DEFAULT_MODEL_NAME:=gemma-4-e4b}"
: "${DEFAULT_MODEL_REPO:=unsloth/gemma-4-E4B-it-GGUF}"
: "${DEFAULT_MODEL_FILE:=gemma-4-E4B-it-Q4_K_M.gguf}"
: "${DEFAULT_CTX_PER_REQUEST:=8192}"
: "${DEFAULT_PARALLEL:=2}"
# Своим сертификатом (--certs) главный обычно подтверждает только имя: нодам тогда зеркало по имени, а не по IP (config.py).
if [ -n "${TLS_CERT:-}" ]; then MAIN_OWN_CERT=true; fi
WEB_DIST=/opt/llm/web
MIGRATIONS_DIR=/opt/llm/portal
REDIS_URL=redis://127.0.0.1:6379/0

portal_vars=(DATABASE_URL LITELLM_MASTER_KEY AGENT_TOKEN CLUSTER_TOKEN NODE_LEGACY_TOKEN OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET OIDC_REDIRECT_URI OIDC_POST_LOGOUT_REDIRECT_URI
  BACKEND_API_KEY PUBLIC_HOSTNAME MAIN_ADVERTISE_IP MAIN_OWN_CERT ADMIN_EMAILS ADMIN_REQUIRE_EMAIL_VERIFIED NODE_IMAGE_REPO NODE_IMAGE_TAG INFER_PORT RPC_PORT HF_TOKEN
  MAX_KEYS_PER_USER KEY_RPM_LIMIT KEY_TPM_LIMIT DEFAULT_MODEL_NAME DEFAULT_MODEL_REPO DEFAULT_MODEL_FILE
  DEFAULT_CTX_PER_REQUEST DEFAULT_PARALLEL WEB_DIST MIGRATIONS_DIR REDIS_URL PORTAL_WORKERS LITELLM_WORKERS QUEUE_FACTOR
  DB_POOL_SIZE DB_MAX_OVERFLOW SESSION_TTL_HOURS LITELLM_STREAM_CONNECTIONS BACKEND_RECENT_WINDOW_S HF_ENDPOINT STATUS_CACHE_TTL_S NODE_NOTREADY_GRACE_S
  SPLIT_REGROUP_GRACE_S SCHEDULE_DRAIN_S GPU_COMPUTE_RESERVE_MIB CONTROLLER_INTERVAL_S STANDALONE_TTL_S STANDALONE_PURGE_S BACKUP_DIR MAIN_GPU HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
  HF_MIRROR_TOKEN HF_MIRROR_MAX_GIB HF_MIRROR_PORT HF_MIRROR_ENABLED HF_MIRROR_URL MODEL_LOAD_MBPS RPC_CACHE_MAX_GIB
  HF_MIRROR_DATA_DIR LOCAL_MODELS_MAX_GIB UPLOAD_CHUNK_MIB LOCAL_MODELS_SCAN_S LOCAL_MODELS_SETTLE_S
  WOL_ENABLED WOL_DELAY_S WOL_RETRY_S WOL_MAX_TRIES WOL_UNICAST WOL_BROADCAST_OVERRIDE WOL_VIA_NODE WOL_VIA_NODE_AFTER)
: >"$RUN_DIR/portal.env"
# При NODE_LEGACY_TOKEN=false портал не принимает серверный токен k3s, поэтому и не получает его: так он остаётся только у k3s.
# Значения false/0/no/off/f/n (без учёта регистра) читаются так же, как их читает pydantic в настройках портала.
legacy_token=1
case "${NODE_LEGACY_TOKEN:-}" in
  [Ff][Aa][Ll][Ss][Ee]|0|[Nn][Oo]|[Oo][Ff][Ff]|[Ff]|[Nn]) legacy_token=0 ;;
esac
for v in "${portal_vars[@]}"; do
  if [ "$v" = CLUSTER_TOKEN ] && [ "$legacy_token" = 0 ]; then continue; fi
  if [ -n "${!v:-}" ]; then printf '%s=%q\n' "$v" "${!v}" >>"$RUN_DIR/portal.env"; fi
done

# ------------------------------------------------------------------ корневой сертификат для подов нод
# ConfigMap llm-ca (render_manifests.py --ca): по нему поды нод проверяют https-адрес зеркала Hugging Face. Свой
# сертификат (--certs) с ca.crt или внутренний CA Caddy; публичный сертификат без ca.crt проверяется системными CA.
# Корневой сертификат Caddy появляется через несколько секунд после его первого запуска: тогда ждём его в фоне.
if [ -f "$CERTS_DIR/ca.crt" ]; then
  ca_src="$CERTS_DIR/ca.crt"
elif [ -z "${TLS_CERT:-}" ]; then
  ca_src="$CA_ROOT_DIR/root.crt"
else
  ca_src=""
fi
render_ca() { python3 "$MAIN_DIR/render_manifests.py" --ca "$ca_src" --out "$MANIFESTS_DIR" >/dev/null; }
ca_pending=0
if [ -n "$ca_src" ]; then
  if [ -s "$ca_src" ]; then render_ca || die "не удалось записать ConfigMap llm-ca из $ca_src (см. сообщение выше)."; else ca_pending=1; fi
fi
umask 022

if [ "${LLM_ENTRYPOINT_DRY_RUN:-}" = 1 ]; then
  exit 0
fi

# ------------------------------------------------------------------ Caddy и портал, затем k3s
mkdir -p "$DATA_DIR/caddy-data" "$DATA_DIR/caddy-config"
# Чистое окружение: секреты и весь .env не наследуются Caddy и порталом (портал читает /run/llm/portal.env сам).
env -i PATH="$PATH" HOME="${HOME:-/root}" HOSTNAME="${HOSTNAME:-llm-main}" supervisord -c /etc/llm/supervisord.conf &
if [ "$ca_pending" = 1 ]; then
  ( umask 077; until [ -s "$ca_src" ]; do sleep 2; done; render_ca || echo "Ошибка: ConfigMap llm-ca не записан" >&2 ) &
fi

# Адрес API для подов и туннелей нод: LAN-адрес при работающем hairpin, иначе адрес контейнера
# (см. api-advertise-ip.py; проверка не дольше 8 с). Адрес для нод (--tls-san, --node-external-ip) в любом случае LAN-адрес.
API_ADVERTISE_IP="$("$MAIN_DIR/api-advertise-ip.py")" || exit $?

# Существующий кластер: server/cred/passwd (пароли пользователей node и server) удаляется перед стартом. k3s восстанавливает
# его из данных bootstrap в datastore и снова записывает в него токены из окружения. Без этого первый старт с новым или
# сменённым AGENT_TOKEN обновляет passwd только на диске (kine сохраняет bootstrap лишь при создании кластера), и следующий
# старт k3s завершается ошибкой «passwd newer than datastore» (проверено на k3s v1.36.5).
if [ -f "$K3S_TOKEN_FILE" ]; then rm -f /var/lib/rancher/k3s/server/cred/passwd; fi

# Токены через окружение, а не argv: /proc/<pid>/cmdline PID 1 читает любой процесс контейнера, environ только root.
# Серверный токен остаётся на главном; k3s записывает server/agent-token (K10…::node:<токен агента>), его показывают
# портал («Подключение ноды») и run-main (agent-token.sh).
K3S_TOKEN="$CLUSTER_TOKEN" K3S_AGENT_TOKEN="$AGENT_TOKEN" exec k3s server \
  --node-name llm-main \
  --cluster-cidr 100.64.0.0/16 \
  --service-cidr 100.65.0.0/16 \
  --cluster-dns 100.65.0.10 \
  --tls-san "$MAIN_ADVERTISE_IP" \
  --tls-san "$PUBLIC_HOSTNAME" \
  --node-external-ip "$MAIN_ADVERTISE_IP" \
  --advertise-address "$API_ADVERTISE_IP" \
  --kube-apiserver-arg kubelet-preferred-address-types=Hostname,InternalIP,ExternalIP \
  --egress-selector-mode cluster \
  --disable traefik,servicelb,metrics-server \
  --write-kubeconfig-mode 0644
