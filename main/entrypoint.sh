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
# по умолчанию частные IPv4, их IPv4-mapped формы в IPv6 (::ffff:a.b.c.d в hex) и loopback; публичный IPv6 только явно
: "${REGISTRY_ALLOW_FROM:=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1,::ffff:a00:0/104,::ffff:ac10:0/108,::ffff:c0a8:0/112,::ffff:7f00:0/104}"
registry_allow=""
IFS=',' read -ra _cidrs <<<"${REGISTRY_ALLOW_FROM//[[:space:]]/}"
for c in "${_cidrs[@]}"; do
  [[ "$c" =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?|[0-9A-Fa-f]*:[0-9A-Fa-f:]*(/[0-9]{1,3})?)$ ]] \
    || die "REGISTRY_ALLOW_FROM: нужен список адресов или подсетей через запятую (10.1.0.0/16,192.168.5.7), получено: $REGISTRY_ALLOW_FROM"
  registry_allow+="${registry_allow:+ }$c"
done
[ -n "$registry_allow" ] || die "REGISTRY_ALLOW_FROM не может быть пустым: перечислите подсети класса и администраторов"
# Сам реестр слушает 127.0.0.1:REGISTRY_BACKEND_PORT и отвечает только с паролем (htpasswd, учётка nsu-push, SEC-2 M2). На
# 127.0.0.1:REGISTRY_PORT Caddy отдаёт его только на чтение (GET, HEAD) с подставленным паролем: этим адресом пользуются
# портал и install-main.sh. Записывает в реестр только registry-push.sh (пароль из secrets.env).
: "${REGISTRY_BACKEND_PORT:=5001}"
[[ "$REGISTRY_BACKEND_PORT" =~ ^[0-9]{4,5}$ ]] && [ "$((10#$REGISTRY_BACKEND_PORT))" -ge 1024 ] && [ "$((10#$REGISTRY_BACKEND_PORT))" -le 65535 ] \
  || die "REGISTRY_BACKEND_PORT должна быть целым числом от 1024 до 65535, получено: $REGISTRY_BACKEND_PORT"
[ "$((10#$REGISTRY_BACKEND_PORT))" != "$((10#$REGISTRY_PORT))" ] \
  || die "REGISTRY_BACKEND_PORT и REGISTRY_PORT должны различаться (сейчас обе $REGISTRY_PORT)"
export REGISTRY_PORT REGISTRY_BACKEND_PORT
# API нод (/api/nodes/*) Caddy принимает только из этих подсетей (по умолчанию частные сети, SEC-2 L1).
# не задана: те же подсети, что REGISTRY_ALLOW_FROM (установка с классом в публичной подсети уже перечислила его там)
: "${NODE_ALLOW_FROM:=$REGISTRY_ALLOW_FROM}"
node_allow=""
IFS=',' read -ra _cidrs <<<"${NODE_ALLOW_FROM//[[:space:]]/}"
for c in "${_cidrs[@]}"; do
  [[ "$c" =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?|[0-9A-Fa-f]*:[0-9A-Fa-f:]*(/[0-9]{1,3})?)$ ]] \
    || die "NODE_ALLOW_FROM: нужен список адресов или подсетей через запятую (10.1.0.0/16,192.168.5.7), получено: $NODE_ALLOW_FROM"
  node_allow+="${node_allow:+ }$c"
done
[ -n "$node_allow" ] || die "NODE_ALLOW_FROM не может быть пустым: перечислите подсети класса"
if [ -n "${TLS_CERT:-}" ]; then
  for f in tls.crt tls.key; do
    [ -f "$CERTS_DIR/$f" ] || die "TLS_CERT задана, но файл $CERTS_DIR/$f не найден: смонтируйте каталог с сертификатом в $CERTS_DIR."
  done
fi

# ------------------------------------------------------------------ секреты
SECRET_NAMES=(LITELLM_MASTER_KEY LITELLM_SALT_KEY POSTGRES_PASSWORD LITELLM_DB_PASSWORD PORTAL_DB_PASSWORD CLUSTER_TOKEN AGENT_TOKEN BACKEND_API_KEY
  HF_MIRROR_TOKEN REDIS_PASSWORD REGISTRY_PASSWORD HF_MIRROR_ADMIN_TOKEN MAIN_BACKEND_KEY)
PG_DATA_GLOB="${PG_DATA_GLOB:-/var/lib/rancher/k3s/storage/pvc-*_llm_data-postgres-*}"
K3S_TOKEN_FILE=/var/lib/rancher/k3s/server/token

declare -A stored=()
# NODE_LEGACY_TOKEN по умолчанию (SEC-2 L3): новая установка не принимает серверный токен k3s от нод (false); установка,
# созданная до этой версии (secrets.env уже есть, а записи NODE_LEGACY_TOKEN_DEFAULT в нём нет), сохраняет прежнее
# поведение (true: её ноды могли подключиться с серверным токеном). Решение записывается в secrets.env один раз.
legacy_default=false
[ ! -f "$SECRETS_FILE" ] || legacy_default=true
if [ -f "$SECRETS_FILE" ]; then
  while IFS='=' read -r k v; do
    if [ -n "$k" ]; then stored["$k"]="$v"; fi
  done <"$SECRETS_FILE"
fi

generate() {
  case "$1" in
    LITELLM_MASTER_KEY|LITELLM_SALT_KEY) echo "sk-$(openssl rand -hex 16)" ;;
    CLUSTER_TOKEN|AGENT_TOKEN|BACKEND_API_KEY|MAIN_BACKEND_KEY) echo "$(openssl rand -hex 32)" ;;
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

# HF_MIRROR_TOKEN (SEC-2 L2): до этой версии он лежал в llm-node-secrets и в desired standalone-нод, то есть известен всем
# нодам класса; теперь он открывает все репозитории и подписывает токены нод (nsm1), поэтому на такой установке он один раз
# заменяется новым (метка HF_MIRROR_TOKEN_MAIN_ONLY в secrets.env). sha256 прежнего значения остаётся в secrets.env
# (HF_MIRROR_TOKEN_LEAKED): главный не стартует ни с ним, ни с ним же из .env, пока его не уберут или не заменят.
sha_of() { printf '%s' "$1" | sha256sum | cut -c1-64; }
leaked="${stored[HF_MIRROR_TOKEN_LEAKED]:-}"
if [ -n "${stored[HF_MIRROR_TOKEN]:-}" ] && [ -z "${stored[HF_MIRROR_TOKEN_MAIN_ONLY]:-}" ]; then
  leaked="$(sha_of "${stored[HF_MIRROR_TOKEN]}")"
  unset 'stored[HF_MIRROR_TOKEN]'
  [ -n "${HF_MIRROR_TOKEN:-}" ] \
    || echo "HF_MIRROR_TOKEN заменён новым: прежний был у всех нод класса (llm-node-secrets); ноды получают от портала токены только на свои модели" >&2
fi
if [ -n "$leaked" ] && [ -n "${HF_MIRROR_TOKEN:-}" ] && [ "$(sha_of "$HF_MIRROR_TOKEN")" = "$leaked" ]; then
  die "HF_MIRROR_TOKEN в .env — прежнее значение, которое версии до SEC-2 раздавали всем нодам: с ним нода сама выпустит себе токен на любую модель. Уберите строку HF_MIRROR_TOKEN из .env (главный создаст новый ключ) или задайте новое значение: HF_MIRROR_TOKEN=hf_\$(openssl rand -hex 20)"
fi

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

case "${stored[NODE_LEGACY_TOKEN_DEFAULT]:-}" in
  true|false) legacy_default="${stored[NODE_LEGACY_TOKEN_DEFAULT]}" ;;
esac
content+="NODE_LEGACY_TOKEN_DEFAULT=$legacy_default"$'\n'
content+="HF_MIRROR_TOKEN_MAIN_ONLY=1"$'\n'
[ -z "$leaked" ] || content+="HF_MIRROR_TOKEN_LEAKED=$leaked"$'\n'

mkdir -p "$DATA_DIR"
if [ ! -f "$SECRETS_FILE" ] || [ "$(cat "$SECRETS_FILE")" != "${content%$'\n'}" ]; then
  printf '%s' "$content" >"$SECRETS_FILE.tmp"
  mv "$SECRETS_FILE.tmp" "$SECRETS_FILE"
fi

# ------------------------------------------------------------------ ключ бэкенда лидера split на главном
# Лидер split-группы на главном (SPLIT_LEADER_ON_MAIN) читает ключ из того же файла, что и под на ноде (builders.BACKEND_KEY_HOST_DIR):
# hostPath пода на llm-main это файловая система этого контейнера. Ключа ноды у главного нет, этот свой, нодам он не выдаётся; его
# знают только LiteLLM и портал (MAIN_BACKEND_KEY). Каталог только для root (umask 077), файл пишется при каждом старте.
main_key_dir=/var/lib/llm-node/backend
mkdir -p "$main_key_dir"
printf '%s' "$MAIN_BACKEND_KEY" >"$main_key_dir/key.tmp"
mv "$main_key_dir/key.tmp" "$main_key_dir/key"

# ------------------------------------------------------------------ учётка записи в реестр
# bcrypt-хеш REGISTRY_PASSWORD (caddy hash-password, пароль через stdin) для Secret llm-registry-auth. Хеш с солью пересчитывается
# только при смене пароля (рядом лежит sha256 пароля): иначе каждый старт менял бы манифест и перезапускал реестр. Стоимость 6:
# реестр проверяет хеш на каждый запрос, а пароль из 48 hex перебором не подобрать и при малой стоимости.
REGISTRY_USER=nsu-push
htpasswd_file="$DATA_DIR/registry.htpasswd"
pw_sum="$(printf '%s' "$REGISTRY_PASSWORD" | sha256sum | cut -c1-64)"
if [ ! -s "$htpasswd_file" ] || [ "$(cat "$htpasswd_file.sum" 2>/dev/null)" != "$pw_sum" ]; then
  pw_hash="$(printf '%s\n' "$REGISTRY_PASSWORD" | caddy hash-password --bcrypt-cost 6 2>/dev/null)" \
    || die "не удалось получить хеш пароля реестра (caddy hash-password)"
  [[ "$pw_hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] || die "caddy hash-password вернул не bcrypt-хеш"
  printf '%s:%s\n' "$REGISTRY_USER" "$pw_hash" >"$htpasswd_file.tmp"
  mv "$htpasswd_file.tmp" "$htpasswd_file"
  printf '%s' "$pw_sum" >"$htpasswd_file.sum"
fi
REGISTRY_HTPASSWD="$(head -n1 "$htpasswd_file")"
export REGISTRY_HTPASSWD
registry_auth="Basic $(printf '%s:%s' "$REGISTRY_USER" "$REGISTRY_PASSWORD" | base64 -w0)"

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
# /ca.crt.p7s: подпись /ca.crt ключом CA сервера k3s (sign-ca.sh, в фоне ниже). Нода проверяет CA k3s по хешу из токена
# K10<sha256>::node:..., а по нему эту подпись (node/rootfs/opt/llm/bin/fetch-main-ca.sh), и не верит первому ответу /ca.crt
# (SEC-2 M1). Нет /ca.crt (публичный сертификат без ca.crt): нет и подписи.
CA_SIG_DIR="$DATA_DIR/ca-sign"
if [ "$ca_handler" = $'\t\trespond 404' ]; then
  ca_sig_handler=$'\t\trespond 404'
else
  ca_sig_handler=$'\t\troot * '"$CA_SIG_DIR"$'\n\t\trewrite * /ca.crt.p7s'$'\n\t\theader Content-Type application/pkcs7-signature'$'\n\t\tfile_server'
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
caddyfile="${caddyfile//@@CA_SIG_HANDLER@@/$ca_sig_handler}"
caddyfile="${caddyfile//@@PUBLIC_HOSTNAME@@/$PUBLIC_HOSTNAME}"
caddyfile="${caddyfile//@@MAIN_ADVERTISE_IP@@/$MAIN_ADVERTISE_IP}"
caddyfile="${caddyfile//@@HF_MIRROR_PORT@@/$HF_MIRROR_PORT}"
caddyfile="${caddyfile//@@REGISTRY_PORT@@/$REGISTRY_PORT}"
caddyfile="${caddyfile//@@REGISTRY_ALLOW_FROM@@/$registry_allow}"
caddyfile="${caddyfile//@@REGISTRY_BACKEND_PORT@@/$REGISTRY_BACKEND_PORT}"
caddyfile="${caddyfile//@@NODE_ALLOW_FROM@@/$node_allow}"
caddyfile="${caddyfile//@@REGISTRY_AUTH@@/$registry_auth}"
printf '%s\n' "$caddyfile" >"$RUN_DIR/Caddyfile"
# адрес записи в реестр для registry-push.sh (пароль он берёт из secrets.env)
printf 'REGISTRY_BACKEND_PORT=%s\nREGISTRY_USER=%s\n' "$REGISTRY_BACKEND_PORT" "$REGISTRY_USER" >"$RUN_DIR/registry.env"

# ------------------------------------------------------------------ окружение портала
# Значения экранируются printf %q: файл читается командой `. portal.env` (supervisord.conf).
DATABASE_URL="postgresql+asyncpg://portal:${PORTAL_DB_PASSWORD}@127.0.0.1:5432/portal"
# Провайдера входа выбирает оператор: значений по умолчанию нет. Без OIDC_ISSUER и OIDC_CLIENT_ID главный запускается, но войти
# в портал нельзя (кнопка «Войти» отвечает понятной ошибкой), поэтому здесь громкая строка в журнал, а не остановка.
for v in OIDC_ISSUER OIDC_CLIENT_ID; do
  [ -n "${!v:-}" ] || echo "ПРЕДУПРЕЖДЕНИЕ: не задана переменная $v: войти в портал нельзя, пока её нет в .env (install-main.sh спрашивает её; пример для NSU ID в .env.example)." >&2
done
: "${NODE_IMAGE_REPO:=nsu-llm-node}"
: "${NODE_IMAGE_TAG:=0.2.1}"
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
# requirepass Redis (REDIS_PASSWORD, common-25-redis): тот же адрес у LiteLLM (GATEWAY_STATS_REDIS_URL в common-30-litellm)
REDIS_URL="redis://:${REDIS_PASSWORD}@127.0.0.1:6379/0"

portal_vars=(DATABASE_URL LITELLM_MASTER_KEY MAIN_BACKEND_KEY SPLIT_LEADER_ON_MAIN AGENT_TOKEN CLUSTER_TOKEN NODE_LEGACY_TOKEN OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET LOGIN_PROVIDER_NAME OIDC_REDIRECT_URI OIDC_POST_LOGOUT_REDIRECT_URI
  BACKEND_API_KEY PUBLIC_HOSTNAME MAIN_ADVERTISE_IP MAIN_OWN_CERT ADMIN_EMAILS ADMIN_REQUIRE_EMAIL_VERIFIED NODE_IMAGE_REPO NODE_IMAGE_TAG INFER_PORT RPC_PORT HF_TOKEN
  MAX_KEYS_PER_USER KEY_RPM_LIMIT KEY_TPM_LIMIT DEFAULT_MODEL_NAME DEFAULT_MODEL_REPO DEFAULT_MODEL_FILE
  DEFAULT_CTX_PER_REQUEST DEFAULT_PARALLEL WEB_DIST MIGRATIONS_DIR REDIS_URL PORTAL_WORKERS LITELLM_WORKERS QUEUE_FACTOR
  DB_POOL_SIZE DB_MAX_OVERFLOW SESSION_TTL_HOURS LITELLM_STREAM_CONNECTIONS BACKEND_RECENT_WINDOW_S HF_ENDPOINT STATUS_CACHE_TTL_S NODE_NOTREADY_GRACE_S
  SPLIT_REGROUP_GRACE_S SCHEDULE_DRAIN_S GPU_COMPUTE_RESERVE_MIB CONTROLLER_INTERVAL_S STANDALONE_TTL_S STANDALONE_PURGE_S BACKUP_DIR MAIN_GPU HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
  HF_MIRROR_TOKEN HF_MIRROR_ADMIN_TOKEN HF_MIRROR_MAX_GIB HF_MIRROR_PORT HF_MIRROR_ENABLED HF_MIRROR_URL MODEL_LOAD_MBPS RPC_CACHE_MAX_GIB
  HF_MIRROR_DATA_DIR LOCAL_MODELS_MAX_GIB UPLOAD_CHUNK_MIB LOCAL_MODELS_SCAN_S LOCAL_MODELS_SETTLE_S
  WOL_ENABLED WOL_DELAY_S WOL_RETRY_S WOL_MAX_TRIES WOL_UNICAST WOL_BROADCAST_OVERRIDE WOL_VIA_NODE WOL_VIA_NODE_AFTER
  NODE_IMAGE_PIN NODE_AUTO_UPDATE NODE_UPDATE_RETRY_S REGISTRY_PORT REGISTRY_URL NSU_LLM_VERSION NSU_LLM_REVISION MAIN_IMAGE_DIGEST MODEL_CACHE_IDLE_S
  SETTINGS_CACHE_TTL_S NODE_APPROVAL_REQUIRED ADMIN_SUBS OIDC_ALLOWED_DOMAINS OIDC_REQUIRED_CLAIM SESSION_MAX_DAYS TRUSTED_PROXIES
  KEY_CREATE_PER_HOUR PLAYGROUND_MAX_INFLIGHT PLAYGROUND_BODY_BUDGET_MIB NODE_KEY_MIGRATION_DAYS)
: >"$RUN_DIR/portal.env"
# При NODE_LEGACY_TOKEN=false портал не принимает серверный токен k3s, поэтому и не получает его: так он остаётся только у k3s.
# Значения false/0/no/off/f/n (без учёта регистра) читаются так же, как их читает pydantic в настройках портала.
# Не задана в .env: значение по умолчанию этой установки (legacy_default выше: false на новой, true на созданной до неё).
: "${NODE_LEGACY_TOKEN:=${legacy_default:-false}}"
legacy_token=1
case "$NODE_LEGACY_TOKEN" in
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
# Подпись /ca.crt ключом CA k3s: появляется, когда есть оба (CA k3s создаётся при первом старте k3s), и обновляется при смене
# любого из них (sign-ca.sh сам ничего не делает, пока входы не изменились).
if [ -n "$ca_src" ]; then
  # каждые 2 с, пока подписи нет (нода, которую ставят сразу после главного, иначе ждала бы до 30 с), затем раз в 30 с
  ( while :; do "$MAIN_DIR/sign-ca.sh" "$ca_src" "$CA_SIG_DIR" >/dev/null || true
      if [ -s "$CA_SIG_DIR/ca.crt.p7s" ]; then sleep 30; else sleep 2; fi; done ) &
fi

# Поды главного выбирают ноду по имени объекта Node (matchFields metadata.name: его не присвоить себе чужой ноде, а метку
# kubernetes.io/hostname kubelet ставит себе сам) и терпят этот taint; поды портала (только ПК класса) его не терпят.
# Системные поды k3s (coredns, local-path-provisioner) его уже терпят. Новый кластер получает taint при регистрации
# llm-main (--node-taint), кластер, созданный до этой версии, здесь, как только ответит API k3s.
MAIN_TAINT="node-role.kubernetes.io/control-plane=true:NoSchedule"
( for _ in $(seq 1 360); do
    if k3s kubectl taint node llm-main "$MAIN_TAINT" --overwrite >/dev/null 2>&1; then exit 0; fi
    sleep 5
  done
  echo "Ошибка: taint $MAIN_TAINT не поставлен на llm-main за 30 минут: k3s kubectl taint node llm-main $MAIN_TAINT" >&2 ) &

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
  --node-taint "$MAIN_TAINT" \
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
  --write-kubeconfig-mode 0600
