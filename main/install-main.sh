#!/usr/bin/env bash
# Установщик главного сервера NSU LLM Cluster на Linux-хосте (мастер; низкоуровневый запуск контейнера: run-main.sh).
# Использование:
#   sudo ./main/install-main.sh [--yes] [--answers FILE] [--env-file FILE] [--config FILE]
#                               [--image-tar FILE] [--timeout SEC] [--dry-run]
#   sudo ./main/install-main.sh --update VERSION [--yes] [--dry-run]
#   sudo ./main/install-main.sh --uninstall [--yes] [--dry-run]
#
#   без параметров  проверяет хост, спрашивает недостающее, показывает сводку и после подтверждения пишет .env,
#                   открывает порты в брандмауэре, готовит образ, запускает run-main.sh и ждёт /readyz
#   --yes           без вопросов: значения из --answers, сохранённых ответов, .env и значений по умолчанию
#   --answers FILE  файл ответов KEY=VALUE: PUBLIC_HOSTNAME, MAIN_ADVERTISE_IP, OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, ADMIN_EMAILS,
#                   TRUSTED_PROXIES (адреса фронт-прокси через запятую, можно пусто), CERT_MODE=internal|own,
#                   CERTS_DIR, AIRGAP=yes|no, FIREWALL=yes|no, HAIRPIN_RULE=yes|no, IMAGE_SOURCE=pull|build|load|keep,
#                   IMAGE_TAR, NODE_IMAGE_TAR_CPU, NODE_IMAGE_TAR_GPU, GHCR_OWNER, IMAGE_REF, NODE_IMAGE_SOURCE, GHCR_TOKEN_FILE,
#                   INSTALL_DOCKER=yes|no;
#                   остальные KEY=VALUE записываются в .env как есть
#   --env-file      переменные главного для контейнера, по умолчанию .env в корне репозитория (права 600)
#   --config        ответы установщика для повторного запуска, по умолчанию install-main.env рядом с --env-file
#   --image-tar     архив образа главного (docker save): загрузить его вместо сборки из исходников (IMAGE_SOURCE=load)
#   --update VER    скачать с ghcr.io образ главного версии VER и образы нод VER-cpu и VER-gpu, положить образы нод в реестр
#                   главного (класс берёт их оттуда); контейнер не перезапускается (docs/release.md, «Обновление»)
#   --timeout       сколько секунд ждать /readyz, по умолчанию 900
#   --dry-run       ничего не менять: проверить хост и напечатать, что было бы сделано
#   --uninstall     удалить контейнер и тома llm-main-* (данные и секреты теряются) и открытые установщиком порты
# Повторный запуск идёт без вопросов и обновляет образ: контейнер пересоздаётся на тех же томах, данные сохраняются.
# IMAGE_SOURCE=pull: образ главного берётся с ghcr.io (публичный, без токена): ghcr.io/$GHCR_OWNER/nsu-llm-main:<версия этого
# чекаута> (другой адрес: IMAGE_REF), а образы нод nsu-llm-node:<версия>-cpu и -gpu (адрес: NODE_IMAGE_SOURCE, по умолчанию
# ghcr.io/$GHCR_OWNER/nsu-llm-node) кладутся в реестр главного. Токен нужен только для закрытого зеркала: GHCR_TOKEN_FILE
# (права 0600). GHCR_OWNER: из ответов или переменной окружения, по умолчанию denmurzik (ghcr.io/denmurzik/nsu-llm-main). Без доступа к ghcr.io: IMAGE_SOURCE=load.
# IMAGE_SOURCE=load: образы нод кладутся в реестр главного из архивов NODE_IMAGE_TAR_CPU и NODE_IMAGE_TAR_GPU (docker save, можно
# .tar.gz; по умолчанию nsu-llm-node-<версия>-cpu.tar.gz и -gpu.tar.gz рядом с IMAGE_TAR, если они есть).
# Код выхода 3: главный запущен, но контейнер не достаёт до MAIN_ADVERTISE_IP:6443 (нет hairpin: логи и exec подов на других
# машинах не работают); итог и команда исправления напечатаны в конце. При активном ufw или firewalld и FIREWALL=no правило для
# подсети Docker на 6443/tcp добавляется само (HAIRPIN_RULE=no отключает).
#
# Вход пользователей идёт через OIDC-провайдера, которого выбирает оператор: значений по умолчанию для PUBLIC_HOSTNAME, OIDC_ISSUER,
# OIDC_CLIENT_ID и OIDC_CLIENT_SECRET нет. С --yes без любого из них установщик останавливается и называет недостающие ключи; мастер
# спрашивает их и показывает redirect URI (https://<PUBLIC_HOSTNAME>/auth/callback или OIDC_REDIRECT_URI), который нужно
# зарегистрировать у провайдера. NSU ID (OIDC_ISSUER=https://sso-ng.nsu.ru/auth/realms/NSU) один из примеров, не умолчание.
#
# Откуда берутся значения (по возрастанию приоритета): значения по умолчанию, переменная окружения OIDC_CLIENT_SECRET (только
# если секрета нет ни в .env, ни в файле ответов), .env, сохранённые ответы установщика, --answers FILE, --image-tar.
# Секрет лучше не передавать в командной строке (он попадёт в историю shell и в список процессов): введите его по запросу,
# положите в файл ответов с правами 600 или передайте переменной окружения через sudo --preserve-env.
# Без терминала на stdin (ssh без -t, curl | sudo bash) вопросы задать нельзя: нужны --yes --answers FILE или ssh -t.
set -euo pipefail
export MSYS_NO_PATHCONV=1
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RUN_MAIN="$HERE/run-main.sh"
IMAGE="nsu-llm-main:0.2.1"
CONTAINER="llm-main"
PORTS=(80 443 6443)
MIN_RAM_GIB=4
MIN_DISK_GIB=20
WARN_DISK_GIB=50
POLL_S=5
# Только для тестов: корень, от которого читаются /etc/os-release, /proc/meminfo и пишется /etc/apt.
SYSROOT="${NSU_LLM_SYSROOT:-}"
ENV_SECRET="${OIDC_CLIENT_SECRET:-}"
INSTALLER_KEYS=" CERT_MODE CERTS_DIR AIRGAP FIREWALL IMAGE_SOURCE IMAGE_TAR NODE_IMAGE_TAR_CPU NODE_IMAGE_TAR_GPU GHCR_OWNER IMAGE_REF NODE_IMAGE_SOURCE GHCR_TOKEN_FILE PENDING_IMAGE_VERSION NODE_IMAGE_TAG_KEEP INSTALL_DOCKER HAIRPIN_RULE HAIRPIN_SUBNET "
ENV_OWNER="${GHCR_OWNER:-}"
GHCR=ghcr.io
DEFAULT_REGISTRY_PORT=5000
ASKED_ENV_KEYS=(PUBLIC_HOSTNAME MAIN_ADVERTISE_IP OIDC_ISSUER OIDC_CLIENT_ID LOGIN_PROVIDER_NAME OIDC_CLIENT_SECRET ADMIN_EMAILS ADMIN_REQUIRE_EMAIL_VERIFIED TRUSTED_PROXIES)
DEFAULT_GHCR_OWNER=denmurzik
# ключи без значения по умолчанию: оператор называет их сам (с --yes их проверяет require_yes_keys)
REQUIRED_YES_KEYS=(PUBLIC_HOSTNAME OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET ADMIN_EMAILS)
# можно оставить пустыми; без .env спрашиваются, с .env (повторный запуск) отсутствие ключа значит «пусто»
OPTIONAL_KEYS=" TRUSTED_PROXIES NODE_IMAGE_TAR_CPU NODE_IMAGE_TAR_GPU "

YES=0
DRY=0
UNINSTALL=0
UPDATE_VERSION=""
ANSWERS=""
ENV_FILE="$REPO/.env"
CONFIG=""
IMAGE_TAR_ARG=""
TIMEOUT=900
ORIG_ARGS="$*"

DRY_DIR=""
GHCR_CFG=""
cleanup() {
  [ -z "$DRY_DIR" ] || rm -rf "$DRY_DIR"
  if [ -n "$GHCR_CFG" ]; then
    DOCKER_CONFIG="$GHCR_CFG" docker logout "$GHCR" >/dev/null 2>&1 || true
    rm -rf "$GHCR_CFG"
  fi
}
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
ok() { say "  [ок] $*"; }
warn() { say "  [!]  $*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1; shift ;;
    --answers) ANSWERS="${2:?}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --config) CONFIG="${2:?}"; shift 2 ;;
    --image-tar) IMAGE_TAR_ARG="${2:?}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --update) UPDATE_VERSION="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout должен быть числом секунд, получено: $TIMEOUT"
[ -z "$UPDATE_VERSION" ] || [[ "$UPDATE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "--update: нужна версия вида 0.2.1, получено: $UPDATE_VERSION"
[ -z "$UPDATE_VERSION" ] || [ "$UNINSTALL" = 0 ] || die "--update и --uninstall вместе не используются"
[ -d "$(dirname "$ENV_FILE")" ] || die "каталог для $ENV_FILE не существует"
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
CONFIG="${CONFIG:-$(dirname "$ENV_FILE")/install-main.env}"
if [ -n "$ANSWERS" ]; then [ -f "$ANSWERS" ] || die "файл ответов $ANSWERS не найден"; fi

# run CMD...: при --dry-run только печатает команду; stdin дочерней команды /dev/null: она не должна съесть ответы мастера
run() {
  if [ "$DRY" = 1 ]; then say "  [dry-run] $(printf '%q ' "$@")"; else "$@" </dev/null; fi
}

# ------------------------------------------------------------------ ввод
# NSU_LLM_STDIN_TTY (только для тестов): 1 или 0 вместо проверки настоящего терминала
stdin_is_tty() {
  if [ -n "${NSU_LLM_STDIN_TTY:-}" ]; then [ "$NSU_LLM_STDIN_TTY" = 1 ]; else [ -t 0 ]; fi
}

read_line() { # read_line VAR [-s]
  local __v
  stdin_is_tty || die "вопросы задать некому: stdin не терминал (ssh без -t или конвейер). Запустите с --yes --answers FILE (README, «Без вопросов») или по ssh -t"
  if [ "${2:-}" = -s ]; then
    IFS= read -rs __v || die "ввод закончился раньше, чем ответы на вопросы (для работы без вопросов: --yes --answers FILE)"
    echo
  else
    IFS= read -r __v || die "ввод закончился раньше, чем ответы на вопросы (для работы без вопросов: --yes --answers FILE)"
  fi
  printf -v "$1" '%s' "${__v%$'\r'}"
}

yesno() { # yesno ВОПРОС ПО_УМОЛЧАНИЮ(y|n): 0 = да
  local ans def_text=нет
  [ "$2" = y ] && def_text=да
  while :; do
    printf '%s (да/нет) [%s]: ' "$1" "$def_text"
    read_line ans
    case "$ans" in
      "") [ "$2" = y ]; return ;;
      y|Y|yes|Yes|YES|д|Д|да|Да|ДА) return 0 ;;
      n|N|no|No|NO|н|Н|нет|Нет|НЕТ) return 1 ;;
      *) say "  ответьте «да» или «нет»" ;;
    esac
  done
}

# ------------------------------------------------------------------ ответы: .env, сохранённые ответы, файл ответов
declare -A ANS=() SRC=()
EXTRA_KEYS=()

load_kv() { # load_kv FILE ИСТОЧНИК all|installer|extra
  local line k v
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    k="${BASH_REMATCH[1]}"
    v="${BASH_REMATCH[2]}"
    if [[ "$v" =~ ^\"(.*)\"$ ]] || [[ "$v" =~ ^\'(.*)\'$ ]]; then v="${BASH_REMATCH[1]}"; fi
    case "$3" in
      installer) [[ "$INSTALLER_KEYS" == *" $k "* ]] || continue ;;
      extra)
        if [[ "$INSTALLER_KEYS" != *" $k "* ]] && [[ " ${ASKED_ENV_KEYS[*]} " != *" $k "* ]]; then EXTRA_KEYS+=("$k"); fi ;;
    esac
    ANS[$k]="$v"
    SRC[$k]="$2"
  done <"$1"
}

load_answers() {
  if [ -f "$ENV_FILE" ]; then load_kv "$ENV_FILE" "$ENV_FILE" all; fi
  if [ -f "$CONFIG" ]; then load_kv "$CONFIG" "$CONFIG" installer; fi
  if [ -n "$ANSWERS" ]; then load_kv "$ANSWERS" "файл ответов" extra; fi
  if [ -n "$IMAGE_TAR_ARG" ]; then
    ANS[IMAGE_SOURCE]=load; SRC[IMAGE_SOURCE]="--image-tar"
    ANS[IMAGE_TAR]="$IMAGE_TAR_ARG"; SRC[IMAGE_TAR]="--image-tar"
  fi
  if [ -z "${ANS[GHCR_OWNER]:-}" ] && [ -n "$ENV_OWNER" ]; then ANS[GHCR_OWNER]="$ENV_OWNER"; SRC[GHCR_OWNER]="переменная окружения"; fi
  if [ -z "${ANS[OIDC_CLIENT_SECRET]:-}" ] && [ -n "$ENV_SECRET" ]; then
    ANS[OIDC_CLIENT_SECRET]="$ENV_SECRET"; SRC[OIDC_CLIENT_SECRET]="переменная окружения"
  elif [ -n "$ENV_SECRET" ] && [ "${ANS[OIDC_CLIENT_SECRET]:-}" != "$ENV_SECRET" ]; then
    warn "OIDC_CLIENT_SECRET из переменной окружения не используется: секрет уже есть в ${SRC[OIDC_CLIENT_SECRET]} и приоритетнее. Чтобы взять другой, поправьте его там."
  fi
}

normalize() { # normalize KEY VALUE
  local v="$2"
  case "$1" in
    ADMIN_EMAILS|TRUSTED_PROXIES) v="${v//[[:space:]]/}" ;;
    AIRGAP|FIREWALL|INSTALL_DOCKER|HAIRPIN_RULE)
      case "$v" in
        y|Y|yes|Yes|YES|д|Д|да|Да|ДА|true|1) v=yes ;;
        n|N|no|No|NO|н|Н|нет|Нет|НЕТ|false|0) v=no ;;
      esac ;;
    ADMIN_REQUIRE_EMAIL_VERIFIED)
      case "$v" in
        y|Y|yes|Yes|YES|д|Д|да|Да|ДА|true|True|TRUE|1) v=true ;;
        n|N|no|No|NO|н|Н|нет|Нет|НЕТ|false|False|FALSE|0) v=false ;;
      esac ;;
    CERT_MODE) case "$v" in 1) v=internal ;; 2) v=own ;; esac ;;
    IMAGE_SOURCE) case "$v" in 1) v=pull ;; 2) v=build ;; 3) v=load ;; 4) v=keep ;; esac ;;
    CERTS_DIR) if [ -d "$v" ]; then v="$(cd "$v" && pwd)"; fi ;;
    IMAGE_TAR|NODE_IMAGE_TAR_CPU|NODE_IMAGE_TAR_GPU|GHCR_TOKEN_FILE) if [ -f "$v" ]; then v="$(cd "$(dirname "$v")" && pwd)/$(basename "$v")"; fi ;;
  esac
  printf '%s' "$v"
}

validate() { # validate KEY VALUE: печатает причину и возвращает 1, если значение не годится
  local v="$2" e o
  case "$1" in
    PUBLIC_HOSTNAME)
      [[ "$v" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] \
        || { echo "«$v» не похоже на имя хоста: допустимы латинские буквы, цифры, точки и дефисы"; return 1; } ;;
    MAIN_ADVERTISE_IP)
      if [[ "$v" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
        for o in "${BASH_REMATCH[@]:1}"; do
          [ "$((10#$o))" -le 255 ] || { echo "нужен адрес IPv4 вида 10.0.0.5, получено: $v"; return 1; }
        done
      else
        echo "нужен адрес IPv4 вида 10.0.0.5, получено: ${v:-пусто}"; return 1
      fi ;;
    OIDC_ISSUER)
      [[ "$v" == https://?* && "$v" != *[[:space:]\"\']* ]] \
        || { echo "нужен адрес провайдера вида https://sso.example.org/realms/main (только https), получено: ${v:-пусто}"; return 1; } ;;
    OIDC_CLIENT_ID)
      [[ -n "$v" && "$v" != *[[:space:]\"\']* ]] \
        || { echo "client id не может быть пустым и содержать пробелы и кавычки: его выдаёт провайдер при регистрации клиента"; return 1; } ;;
    LOGIN_PROVIDER_NAME)
      [[ -n "$v" && ${#v} -le 40 && "$v" != *[[:cntrl:]\"\']* ]] \
        || { echo "название провайдера для кнопки входа: от 1 до 40 символов без кавычек (например SSO или NSU ID), получено: ${v:-пусто}"; return 1; } ;;
    ADMIN_REQUIRE_EMAIL_VERIFIED) [[ "$v" =~ ^(true|false)$ ]] || { echo "ответьте «да» или «нет»"; return 1; } ;;
    OIDC_CLIENT_SECRET)
      [ -n "$v" ] || { echo "секрет не может быть пустым: его показывает провайдер при регистрации клиента (confidential client; в Keycloak: Clients -> Credentials)"; return 1; }
      [[ ! "$v" =~ [[:space:]\"\'] ]] || { echo "секрет не должен содержать пробелов и кавычек"; return 1; } ;;
    ADMIN_EMAILS)
      [ -n "$v" ] || { echo "нужен хотя бы один e-mail администратора"; return 1; }
      local IFS=,
      for e in $v; do
        [[ "$e" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
          || { echo "«$v» не похоже на список e-mail через запятую"; return 1; }
      done ;;
    TRUSTED_PROXIES)
      local IFS=, p a n max
      [[ "$v" != ,* && "$v" != *, && "$v" != *,,* ]] || { echo "«$v»: лишняя запятая"; return 1; }
      for p in $v; do
        a="${p%%/*}"; n=""
        [[ "$p" != */* ]] || n="${p#*/}"
        if [[ "$a" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
          max=32
          for o in "${BASH_REMATCH[@]:1}"; do [ "$((10#$o))" -le 255 ] || max=-1; done
        elif [[ "$a" =~ ^[0-9A-Fa-f:]+$ && "$a" == *:*:* ]]; then
          max=128
        else
          max=-1
        fi
        if [ "$max" -lt 0 ] || { [ -n "$n" ] && ! { [[ "$n" =~ ^[0-9]{1,3}$ ]] && [ "$((10#$n))" -le "$max" ]; }; }; then
          echo "«$v» не похоже на адреса или подсети через запятую (например 10.4.0.105 или 10.4.0.0/24)"; return 1
        fi
      done ;;
    CERT_MODE) [[ "$v" =~ ^(internal|own)$ ]] || { echo "ответьте internal (1) или own (2)"; return 1; } ;;
    CERTS_DIR)
      [ -d "$v" ] || { echo "каталог ${v:-(пусто)} не найден"; return 1; }
      for e in tls.crt tls.key; do [ -f "$v/$e" ] || { echo "в каталоге $v нет файла $e"; return 1; }; done ;;
    AIRGAP|FIREWALL|INSTALL_DOCKER|HAIRPIN_RULE) [[ "$v" =~ ^(yes|no)$ ]] || { echo "ответьте «да» или «нет»"; return 1; } ;;
    HAIRPIN_SUBNET) [[ "$v" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { echo "нужна подсеть IPv4 вида 172.17.0.0/16, получено: ${v:-пусто}"; return 1; } ;;
    IMAGE_SOURCE) [[ "$v" =~ ^(pull|build|load|keep)$ ]] || { echo "ответьте pull (1), build (2), load (3) или keep (4)"; return 1; } ;;
    GHCR_OWNER) [[ "$v" =~ ^[a-z0-9]([a-z0-9-]{0,37}[a-z0-9])?$ ]] \
      || { echo "владелец образов на GitHub (имя пользователя или организации): строчные латинские буквы, цифры и дефис, получено: ${v:-пусто}"; return 1; } ;;
    IMAGE_REF|NODE_IMAGE_SOURCE) [[ "$v" =~ ^[a-z0-9]([a-z0-9._/:@-]*[a-z0-9])?$ ]] \
      || { echo "нужен адрес образа вида ghcr.io/владелец/имя[:тег], получено: ${v:-пусто}"; return 1; } ;;
    GHCR_TOKEN_FILE)
      [ -f "$v" ] || { echo "файл с токеном ${v:-(пусто)} не найден"; return 1; }
      local perm; perm="$(stat -c %a "$v" 2>/dev/null || stat -f %Lp "$v" 2>/dev/null || echo 600)"
      [ "$perm" = 600 ] || [ "$perm" = 400 ] || { echo "права $v: $perm, токен должен читаться только владельцем: chmod 600 $v"; return 1; } ;;
    IMAGE_TAR) [ -f "$v" ] || { echo "файл ${v:-(пусто)} не найден"; return 1; } ;;
    NODE_IMAGE_TAR_CPU|NODE_IMAGE_TAR_GPU) [ -z "$v" ] || [ -f "$v" ] || { echo "файл $v не найден"; return 1; } ;;
  esac
}

need() { # need KEY ВОПРОС ПО_УМОЛЧАНИЮ [-s]: значение из файлов (проверенное), иначе вопрос или значение по умолчанию
  local key="$1" prompt="$2" def="$3" secret="${4:-}" v err ans
  v="${ANS[$key]:-}"
  if [ -z "$v" ] && [[ "$OPTIONAL_KEYS" == *" $key "* ]] && { [ -n "${ANS[$key]+x}" ] || [ -f "$ENV_FILE" ] || [ "$YES" = 1 ]; }; then
    SRC[$key]="${SRC[$key]:-по умолчанию}"
    if [ -z "$def" ] || [ -n "${ANS[$key]+x}" ]; then ANS[$key]=""; return; fi   # ключ с пустым значением в ответах: «не нужно»
    v="$def"   # ключа нет, а умолчание есть: берётся оно (дальше проверяется как обычный ответ)
  fi
  if [ -n "$v" ]; then
    v="$(normalize "$key" "$v")"
    if err="$(validate "$key" "$v")"; then ANS[$key]="$v"; return; fi
    [ "$YES" = 0 ] || die "$key (${SRC[$key]}): $err"
    say "  $key из ${SRC[$key]}: $err"
  elif [ "$YES" = 1 ]; then
    [ -n "$def" ] || die "без вопросов (--yes) нужно значение $key: добавьте строку $key=... в файл ответов (--answers FILE) или в $ENV_FILE"
    v="$(normalize "$key" "$def")"
    err="$(validate "$key" "$v")" || die "$key: $err"
    ANS[$key]="$v"; SRC[$key]="по умолчанию"
    return
  fi
  while :; do
    if [ -n "$def" ]; then printf '%s [%s]: ' "$prompt" "$def"; else printf '%s: ' "$prompt"; fi
    read_line ans $secret
    [ -n "$ans" ] || ans="$def"
    ans="$(normalize "$key" "$ans")"
    if err="$(validate "$key" "$ans")"; then ANS[$key]="$ans"; SRC[$key]="ответ"; return; fi
    say "  $err"
  done
}

# С --yes недостающие ключи без умолчаний называются все сразу, до проверок хоста и до любых действий.
require_yes_keys() {
  local k missing=()
  [ "$YES" = 1 ] || return 0
  for k in "${REQUIRED_YES_KEYS[@]}"; do [ -n "${ANS[$k]:-}" ] || missing+=("$k"); done
  [ "${#missing[@]}" = 0 ] && return 0
  die "без вопросов (--yes) не заданы обязательные значения: $(IFS=,; echo "${missing[*]}" | sed "s/,/, /g"). Добавьте строки KEY=значение в файл ответов (--answers FILE) или в $ENV_FILE. Значений по умолчанию нет: установщик не подставляет ни домен, ни провайдера входа. Пример для NSU ID: OIDC_ISSUER=https://sso-ng.nsu.ru/auth/realms/NSU (README, «Без вопросов»)"
}

# redirect URI, который оператор регистрирует у провайдера (портал: /auth/callback на публичном имени, либо OIDC_REDIRECT_URI)
redirect_uri() { printf '%s' "${ANS[OIDC_REDIRECT_URI]:-https://${ANS[PUBLIC_HOSTNAME]}/auth/callback}"; }
logout_uri() { printf '%s' "${ANS[OIDC_POST_LOGOUT_REDIRECT_URI]:-https://${ANS[PUBLIC_HOSTNAME]}/}"; }
show_redirect_uri() {
  say ""
  say "  Зарегистрируйте у OIDC-провайдера клиент (confidential, Authorization Code + PKCE S256) с адресами:"
  say "    Valid redirect URI:           $(redirect_uri)"
  say "    Valid post logout redirect:   $(logout_uri)"
  say "  Client ID и секрет клиента нужны на следующих шагах (README, «OIDC-провайдер»)."
}

# ------------------------------------------------------------------ ОС
declare -A OS=()
check_os() {
  local kernel arch line k v
  kernel="$(uname -s)"
  [ "$kernel" = Linux ] || die "этот установщик для Linux (обнаружено: $kernel). На Windows запускайте главный через main/run-main.ps1 (README, «Быстрый старт»)."
  arch="$(uname -m)"
  [ "$arch" = x86_64 ] || die "образ главного собран для linux/amd64, а процессор этого хоста $arch"
  if [ -f "$SYSROOT/etc/os-release" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      [[ "$line" =~ ^([A-Z_]+)=\"?([^\"]*)\"?$ ]] || continue
      k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
      OS[$k]="$v"
    done <"$SYSROOT/etc/os-release"
  fi
  OS_FAMILY=other
  case " ${OS[ID]:-} ${OS[ID_LIKE]:-} " in
    *" ubuntu "*|*" debian "*) OS_FAMILY=debian ;;
    *" rhel "*|*" fedora "*|*" centos "*) OS_FAMILY=rhel ;;
  esac
  if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "нужны права root (Docker, брандмауэр, установка пакетов). Запустите: sudo $0 $ORIG_ARGS"
  fi
}

# ------------------------------------------------------------------ Docker
DOCKER_CMDS=()
DOCKER_FIX=""
CONFLICTS=()
REMOVED_PKGS=""
docker_install_cmds() {
  local c keyrings="$SYSROOT/etc/apt/keyrings" lists="$SYSROOT/etc/apt/sources.list.d" dist codename repo
  local pkgs="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
  case "$OS_FAMILY" in
    debian)
      if [ "${OS[ID]:-}" = debian ] || { [ "${OS[ID]:-}" != ubuntu ] && [ -z "${OS[UBUNTU_CODENAME]:-}" ]; }; then
        dist=debian; codename="${OS[VERSION_CODENAME]:-}"
      else
        dist=ubuntu; codename="${OS[UBUNTU_CODENAME]:-${OS[VERSION_CODENAME]:-}}"
      fi
      [ -n "$codename" ] || die "в /etc/os-release нет VERSION_CODENAME: поставьте Docker Engine вручную (https://docs.docker.com/engine/install/)"
      DOCKER_CMDS=(
        "apt-get update"
        "apt-get install -y ca-certificates curl"
        "mkdir -p $keyrings $lists && chmod 0755 $keyrings $lists"
        "curl -fsSL https://download.docker.com/linux/$dist/gpg -o $keyrings/docker.asc"
        "chmod a+r $keyrings/docker.asc"
        "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$dist $codename stable' > $lists/docker.list && chmod 0644 $lists/docker.list"
        "apt-get update"
        "apt-get install -y $pkgs"
        "systemctl enable --now docker"
      )
      DOCKER_FIX="sudo apt-get update && sudo apt-get install -f; журнал: journalctl -xe --no-pager | tail -n 50; затем повторите установщик (или поставьте Docker вручную: https://docs.docker.com/engine/install/$dist/)" ;;
    rhel)
      case "${OS[ID]:-}" in fedora) repo=fedora ;; rhel) repo=rhel ;; *) repo=centos ;; esac
      # пакеты, которые по инструкции docs.docker.com конфликтуют с docker-ce / containerd.io: их сначала удаляют
      CONFLICTS=()
      if command -v rpm >/dev/null 2>&1; then
        for c in docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-engine podman runc; do
          if rpm -q "$c" >/dev/null 2>&1; then CONFLICTS+=("$c"); fi
        done
      fi
      DOCKER_CMDS=()
      [ "${#CONFLICTS[@]}" = 0 ] || DOCKER_CMDS+=("dnf remove -y ${CONFLICTS[*]}")
      DOCKER_CMDS+=(
        "dnf -y install dnf-plugins-core"
        "dnf config-manager --add-repo https://download.docker.com/linux/$repo/docker-ce.repo || dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/$repo/docker-ce.repo"
        "dnf install -y $pkgs"
        "systemctl enable --now docker"
      )
      DOCKER_FIX="если dnf пишет о конфликте пакетов (podman, runc, buildah): sudo dnf remove -y podman runc (список: https://docs.docker.com/engine/install/$repo/), затем повторите установщик; можно и sudo dnf install -y --allowerasing $pkgs" ;;
  esac
}

DOCKER_PLANNED=0   # Docker будет установлен или запущен (только при --dry-run): проверки через docker пропускаются
offer_docker_install() {
  local c
  say "  [x]  Docker Engine не найден."
  [ "$OS_FAMILY" != other ] || die "Docker не найден, а дистрибутив ${OS[PRETTY_NAME]:-неизвестен} установщик не знает. Поставьте Docker Engine по инструкции https://docs.docker.com/engine/install/ и запустите установщик снова."
  docker_install_cmds
  if [ "${#CONFLICTS[@]}" -gt 0 ]; then
    warn "конфликтующие пакеты (${CONFLICTS[*]}) мешают docker-ce / containerd.io; по инструкции docs.docker.com они будут удалены."
    say "       Если вы пользуетесь podman, его контейнеры перестанут запускаться (данные в ~/.local/share/containers остаются)."
  fi
  say "       Установка из официального репозитория Docker (download.docker.com):"
  for c in "${DOCKER_CMDS[@]}"; do say "         $c"; done
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] установка Docker пропущена"
    DOCKER_PLANNED=1
    return
  fi
  if [ "$YES" = 1 ]; then
    [ "$(normalize INSTALL_DOCKER "${ANS[INSTALL_DOCKER]:-no}")" = yes ] \
      || die "Docker не найден. Установите его командами выше или добавьте INSTALL_DOCKER=yes в файл ответов."
  else
    yesno "Установить Docker Engine из официального репозитория?" y \
      || die "Docker не найден. Установите его командами выше и запустите установщик снова."
  fi
  local -a envs=()
  # без вопросов пакетный менеджер тоже ничего не спрашивает (диалоги debconf и needrestart на Ubuntu 22.04+ иначе повисают)
  if [ "$YES" = 1 ] && [ "$OS_FAMILY" = debian ]; then envs=(DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a); fi
  for c in "${DOCKER_CMDS[@]}"; do
    say "  > $c"
    env ${envs[@]+"${envs[@]}"} bash -c "set -e; $c" </dev/null || die "команда завершилась ошибкой: $c. Исправление: $DOCKER_FIX"
  done
  docker info >/dev/null 2>&1 || die "Docker установлен, но не отвечает. Журнал службы: journalctl -u docker --no-pager -n 50"
  [ "${#CONFLICTS[@]}" = 0 ] || REMOVED_PKGS="${CONFLICTS[*]}"
  ok "Docker Engine установлен${REMOVED_PKGS:+ (удалены конфликтующие пакеты: $REMOVED_PKGS)}"
}

EXISTING=0
DOCKER_ROOT=/var/lib
check_docker() {
  local info ver cg root
  if ! command -v docker >/dev/null 2>&1; then offer_docker_install; fi
  if [ "$DOCKER_PLANNED" = 0 ] && ! docker info >/dev/null 2>&1; then
    say "  [!]  Docker установлен, но служба не отвечает (docker info)."
    if [ "$DRY" = 1 ]; then
      say "  [dry-run] systemctl enable --now docker"
      DOCKER_PLANNED=1
    elif [ "$YES" = 1 ] || yesno "Запустить службу Docker (systemctl enable --now docker)?" y; then
      systemctl enable --now docker </dev/null || die "служба Docker не запустилась. Журнал: journalctl -u docker --no-pager -n 50"
      docker info >/dev/null 2>&1 || die "Docker не отвечает после запуска службы. Журнал: journalctl -u docker --no-pager -n 50"
      ok "служба Docker запущена"
    else
      die "Docker не запущен. Исправление: sudo systemctl enable --now docker"
    fi
  fi
  [ "$DOCKER_PLANNED" = 0 ] || return 0
  info="$(docker info --format '{{.ServerVersion}} {{.CgroupVersion}} {{.DockerRootDir}}')"
  read -r ver cg root <<<"$info"
  ok "Docker Engine $ver"
  if [ "$cg" = 2 ]; then
    ok "cgroup v2"
  else
    problem "cgroup v${cg:-?}: k3s в контейнере главного работает только с cgroup v2" \
      "добавьте systemd.unified_cgroup_hierarchy=1 в GRUB_CMDLINE_LINUX файла /etc/default/grub, выполните sudo update-grub (RHEL: sudo grub2-mkconfig -o /boot/grub2/grub.cfg) и перезагрузите хост"
  fi
  DOCKER_ROOT="${root:-/var/lib/docker}"
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    EXISTING=1
    ok "найдена установка (контейнер $CONTAINER): она будет обновлена, данные в томах llm-main-* сохранятся"
  fi
}

# ------------------------------------------------------------------ ресурсы хоста
PROBLEMS=()
problem() { # problem ЧТО ИСПРАВЛЕНИЕ
  say "  [x]  $1"
  PROBLEMS+=("$1"$'\n'"    Исправление: $2")
}

check_ports() {
  local p line proc listeners busy=0
  if [ "$EXISTING" = 1 ]; then ok "порты 80, 443, 6443: их держит текущий $CONTAINER, он будет пересоздан"; return; fi
  if ! command -v ss >/dev/null 2>&1; then warn "нет команды ss: порты 80, 443, 6443 не проверены"; return; fi
  listeners="$(ss -ltnpH 2>/dev/null || true)"
  for p in "${PORTS[@]}"; do
    line="$(awk -v s=":$p" 'length($4) >= length(s) && substr($4, length($4) - length(s) + 1) == s' <<<"$listeners" | head -n1)"
    [ -n "$line" ] || continue
    busy=1
    proc="$(sed -n 's/.*users:(("\([^"]*\)".*/\1/p' <<<"$line")"
    problem "порт $p/tcp занят${proc:+ ($proc)}" "найдите процесс: sudo ss -ltnp 'sport = :$p', остановите его или перенесите на другой порт"
  done
  [ "$busy" = 1 ] || ok "порты 80, 443, 6443 свободны"
}

# curl опрашивает /readyz; установка Docker из репозитория Debian/Ubuntu ставит его сама
check_curl() {
  if command -v curl >/dev/null 2>&1; then ok "curl найден"; return; fi
  if [ "$DOCKER_PLANNED" = 1 ] && [ "$OS_FAMILY" = debian ]; then ok "curl будет установлен вместе с Docker"; return; fi
  local install="apt-get install -y curl"
  [ "$OS_FAMILY" != rhel ] || install="dnf install -y curl"
  problem "нет curl: установщик опрашивает им https://<адрес>/readyz" "sudo $install"
}

check_resources() {
  local kb gib n gpu mirror path="$DOCKER_ROOT"
  [ -d "$path" ] || path=/
  kb="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
  if [[ "$kb" =~ ^[0-9]+$ ]]; then
    gib=$((kb / 1048576))
    if [ "$gib" -lt "$MIN_DISK_GIB" ]; then
      problem "диск: на $path свободно $gib ГиБ, нужно не меньше $MIN_DISK_GIB ГиБ (образы, k3s, база)" \
        "освободите место на этом разделе или перенесите каталог данных Docker (data-root в /etc/docker/daemon.json)"
    elif [ "$gib" -lt "$WARN_DISK_GIB" ]; then
      warn "диск: на $path свободно $gib ГиБ, рекомендуется от $WARN_DISK_GIB ГиБ"
    else
      ok "диск: свободно $gib ГиБ ($path)"
    fi
    # кэш зеркала лежит в томе llm-main-hf на этом же разделе; зеркало оставляет свободными 15 % диска
    mirror="${ANS[HF_MIRROR_MAX_GIB]:-500}"
    if [[ "$mirror" =~ ^[0-9]+$ ]] && [ $((mirror * 5)) -gt $((gib * 4)) ]; then
      warn "кэш зеркала Hugging Face HF_MIRROR_MAX_GIB=$mirror ГиБ больше 80 % свободного места на $path ($gib ГиБ): уменьшите HF_MIRROR_MAX_GIB в $ENV_FILE"
    fi
  else
    warn "не удалось узнать свободное место на $path"
  fi
  kb="$(awk '/^MemTotal:/ {print $2}' "$SYSROOT/proc/meminfo" 2>/dev/null || true)"
  if [[ "$kb" =~ ^[0-9]+$ ]]; then
    gib=$((kb / 1048576))
    if [ "$gib" -lt "$MIN_RAM_GIB" ]; then
      problem "оперативной памяти $gib ГиБ, нужно не меньше $MIN_RAM_GIB ГиБ" "выберите хост с большим объёмом памяти"
    else
      ok "RAM: $gib ГиБ"
    fi
  else
    warn "не удалось узнать объём памяти"
  fi
  n="$(nproc 2>/dev/null || echo 0)"
  if [ "$n" -lt 2 ]; then
    problem "процессор: $n ядер, нужно не меньше 2" "выберите хост с большим числом ядер"
  elif [ "$n" -lt 4 ]; then
    warn "процессор: $n ядра, рекомендуется от 4"
  else
    ok "CPU: логических ядер $n"
  fi
  if command -v nvidia-smi >/dev/null 2>&1 \
    && gpu="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null)" && [ -n "$gpu" ]; then
    while IFS= read -r line; do ok "GPU: $line (главный в инференсе не участвует)"; done <<<"$gpu"
  else
    ok "GPU NVIDIA не найден (главному GPU не нужен)"
  fi
}

DETECTED_IP=""
detect_ip() {
  local cidr others
  command -v ip >/dev/null 2>&1 || { warn "нет команды ip: LAN-адрес не определён, его спросят"; return; }
  # без маршрута по умолчанию (изолированная сеть) ip завершается с кодом 2: под set -e и pipefail это не должно убивать установщик
  DETECTED_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)"
  if [ -z "$DETECTED_IP" ]; then warn "LAN-адрес не определён (нет маршрута по умолчанию): его спросят, а с --yes задайте MAIN_ADVERTISE_IP в файле ответов"; return; fi
  cidr="$(ip -o -4 addr show 2>/dev/null | awk -v ip="$DETECTED_IP" '{for (i = 1; i < NF; i++) if ($i == "inet" && index($(i+1), ip "/") == 1) print $(i+1)}' | head -n1 || true)"
  others="$(ip -o -4 addr show 2>/dev/null | awk -v ip="$DETECTED_IP" '{for (i = 1; i < NF; i++) if ($i == "inet" && index($(i+1), ip "/") != 1 && $(i+1) !~ /^127\./) printf "%s (%s) ", $(i+1), $2}' || true)"
  ok "LAN-адрес: ${cidr:-$DETECTED_IP} (маршрут по умолчанию)"
  [ -z "$others" ] || say "       другие адреса: $others"
}

FW_TOOL=""
detect_fw() {
  if command -v ufw >/dev/null 2>&1 && LC_ALL=C ufw status 2>/dev/null | grep -q '^Status: active'; then
    FW_TOOL=ufw
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    FW_TOOL=firewalld
  fi
}

# ------------------------------------------------------------------ действия
fw_apply() { # fw_apply open|close
  local p
  case "$FW_TOOL" in
    ufw)
      for p in "${PORTS[@]}"; do
        if [ "$1" = open ]; then run ufw allow "$p/tcp" comment "NSU LLM main"; else run ufw delete allow "$p/tcp" || true; fi
      done ;;
    firewalld)
      for p in "${PORTS[@]}"; do
        if [ "$1" = open ]; then run firewall-cmd --permanent "--add-port=$p/tcp"; else run firewall-cmd --permanent "--remove-port=$p/tcp" || true; fi
      done
      run firewall-cmd --reload ;;
  esac
}

# Подсеть bridge Docker, из которой контейнер главного ходит к хосту (run-main.sh не задаёт --network, значит, это сеть bridge);
# если Docker её не назвал, вся область адресов Docker 172.16.0.0/12.
docker_bridge_subnet() {
  local s
  s="$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null </dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' | head -n1 || true)"
  printf '%s' "${s:-172.16.0.0/12}"
}
# Контейнер главного проверяет, достаёт ли он MAIN_ADVERTISE_IP:6443 (hairpin, main/api-advertise-ip.py). Этот адрес принадлежит хосту, поэтому
# пакет идёт через цепочку INPUT, где ufw и firewalld закрывают всё, что не разрешено: без FIREWALL=yes (порт 6443 открыт всем) проверка не
# проходит, API объявляется по адресу контейнера, и логи с exec подов на других машинах (туннели k3s) не работают. Узкое правило: подсеть Docker -> 6443/tcp.
hairpin_rule_text() { # hairpin_rule_text TOOL SUBNET: команда правила как её набрать вручную
  case "$1" in
    ufw) printf "sudo ufw allow from %s to any port 6443 proto tcp comment 'NSU LLM main hairpin'" "$2" ;;
    firewalld) printf "sudo firewall-cmd --permanent --add-rich-rule='rule family=\"ipv4\" source address=\"%s\" port port=\"6443\" protocol=\"tcp\" accept' && sudo firewall-cmd --reload" "$2" ;;
  esac
}
hairpin_apply() { # hairpin_apply TOOL open|close SUBNET
  local rule
  case "$1" in
    ufw)
      if [ "$2" = open ]; then run ufw allow from "$3" to any port 6443 proto tcp comment "NSU LLM main hairpin"
      else run ufw delete allow from "$3" to any port 6443 proto tcp || true; fi ;;
    firewalld)
      rule="rule family=\"ipv4\" source address=\"$3\" port port=\"6443\" protocol=\"tcp\" accept"
      if [ "$2" = open ]; then run firewall-cmd --permanent "--add-rich-rule=$rule"; else run firewall-cmd --permanent "--remove-rich-rule=$rule" || true; fi
      run firewall-cmd --reload ;;
  esac
}
# Нужно ли правило: брандмауэр активен, порты в нём установщик не открывает (иначе 6443 и так открыт всем) и правило не отключено
want_hairpin_rule() { [ -n "$FW_TOOL" ] && [ "${ANS[FIREWALL]:-no}" != yes ] && [ "${ANS[HAIRPIN_RULE]:-yes}" = yes ]; }

# После запуска: если главный не достал свой LAN-адрес, он напечатал «нет hairpin». Установка тогда неполная: портал и ноды работают, логи и exec
# подов на других машинах нет. Это громко сказано в конце, с точной командой, а код выхода 3 (при --yes и без него: автоматизация не должна
# принимать такую установку за успешную; контейнер остаётся запущенным, откатывать нечего, после исправления установщик возвращает 0).
DEGRADED=0
check_hairpin() {
  local line net ip="${ANS[MAIN_ADVERTISE_IP]}"
  line="$(docker logs "$CONTAINER" 2>&1 </dev/null | grep 'нет hairpin' | head -n1 || true)"
  [ -n "$line" ] || return 0
  DEGRADED=1
  net="${ANS[HAIRPIN_SUBNET]:-$(docker_bridge_subnet)}"
  say ""
  warn "главный запущен, но установка неполная: контейнер не достаёт до $ip:6443 (нет hairpin). Ноды присоединяются, а логи и exec подов на других машинах, в том числе логи в портале, работать не будут (502)."
  say "       $line"
  if [ -n "$FW_TOOL" ] && ! want_hairpin_rule && [ "${ANS[FIREWALL]:-no}" != yes ]; then
    say "  Брандмауэр $FW_TOOL закрывает обращение контейнера к адресу самого хоста. Исправление (правило только для подсети Docker, только 6443/tcp):"
    say "    $(hairpin_rule_text "$FW_TOOL" "$net")"
  elif [ -n "$FW_TOOL" ]; then
    say "  Правило для подсети Docker ($net) на 6443/tcp в $FW_TOOL уже есть или порт 6443 открыт всем, значит дело не в нём. Проверьте, что $ip принадлежит этому хосту (ip -4 addr),"
    say "  что VPN или прокси не перехватывает адрес, и что на 6443 нет сторонних правил в iptables (iptables -S INPUT)."
  else
    say "  ufw и firewalld не активны: проверьте, что $ip принадлежит этому хосту (ip -4 addr), правила iptables для INPUT и что VPN или прокси не перехватывает адрес."
  fi
  say "  Адрес API выбирается при старте контейнера, поэтому после исправления перезапустите главный и проверьте журнал:"
  say "    docker restart $CONTAINER && sleep 15 && docker logs $CONTAINER 2>&1 | grep hairpin      # строки «нет hairpin» быть не должно"
  say "  Код выхода установщика: 3 (главный работает; после исправления повторный запуск установщика вернёт 0)."
}

write_env() { # write_env TARGET: .env (или шаблон .env.example) с подставленными ответами, права 600
  local target="$1" src=/dev/null kv k
  if [ -f "$ENV_FILE" ]; then src="$ENV_FILE"; elif [ -f "$REPO/.env.example" ]; then src="$REPO/.env.example"; fi
  kv="$(mktemp "$(dirname "$target")/.install-main.XXXXXX")"
  for k in "${ASKED_ENV_KEYS[@]}" "${EXTRA_KEYS[@]}"; do printf '%s=%s\n' "$k" "${ANS[$k]:-}" >>"$kv"; done
  awk 'NR == FNR { i = index($0, "="); k = substr($0, 1, i - 1); v[k] = substr($0, i + 1); order[++n] = k; next }
       match($0, /^[A-Za-z_][A-Za-z0-9_]*=/) { k = substr($0, 1, RLENGTH - 1)
         if (k in v) { if (!(k in done)) { print k "=" v[k]; done[k] = 1 }; next } }
       { print }
       END { for (j = 1; j <= n; j++) if (!(order[j] in done)) { print order[j] "=" v[order[j]]; done[order[j]] = 1 } }' \
    "$kv" "$src" >"$target.tmp"
  rm -f "$kv"
  chmod 600 "$target.tmp"
  mv "$target.tmp" "$target"
}

main_running() { [ "$EXISTING" = 1 ] && [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" = true ]; }
# Образы нод этого запуска окажутся в реестре: pull или архивы обоих видов.
source_seeds_both() {
  [ "${ANS[IMAGE_SOURCE]:-}" = pull ] || { [ "${ANS[IMAGE_SOURCE]:-}" = load ] && [ -n "${ANS[NODE_IMAGE_TAR_CPU]:-}" ] && [ -n "${ANS[NODE_IMAGE_TAR_GPU]:-}" ]; }
}
# NODE_IMAGE_TAG в .env переживает обновление: на стенде 0.2.0 он так и остался 0.1.1, и главный велел нодам запускать старый образ.
# Значение старше версии чекаута заменяется ею, но только когда образы новой версии уже в реестре: иначе поды всех ПК разом уйдут в
# ImagePullBackOff, а старый тег работал (docs/release.md: сначала реестр, потом перезапуск). Поэтому вызов идёт после наполнения
# реестра и до run-main. Главный не запущен: старый тег сейчас никто не использует, реестр наполнится сразу после старта.
# Только простая версия (x.y.z): свой тег вроде <версия>-fat и версия новее чекаута заданы нарочно. Нарочно старая версия задаётся
# строкой NODE_IMAGE_TAG в файле ответов и запоминается в NODE_IMAGE_TAG_KEEP: следующий запуск без --answers её тоже не тронет.
refresh_node_tag() {
  local tag="${ANS[NODE_IMAGE_TAG]:-}" ready=0
  [[ "$tag" =~ ^[0-9]+(\.[0-9]+)+$ ]] && [ "$tag" != "$CHECKOUT_VERSION" ] || return 0
  [ "$(printf '%s\n%s\n' "$tag" "$CHECKOUT_VERSION" | sort -V | head -n1)" = "$tag" ] || return 0
  if [ "${SRC[NODE_IMAGE_TAG]:-}" = "файл ответов" ] || [ "${ANS[NODE_IMAGE_TAG_KEEP]:-}" = "$tag" ]; then
    warn "NODE_IMAGE_TAG=$tag задан вами и старше этой версии ($CHECKOUT_VERSION): оставлен, ноды будут запускать образ $tag"
    ANS[NODE_IMAGE_TAG_KEEP]="$tag"
    [ "$DRY" = 1 ] || write_config
    return 0
  fi
  if [ "$DRY" = 0 ] && main_running; then
    [ -z "$(registry_digest "$CHECKOUT_VERSION-cpu")" ] || [ -z "$(registry_digest "$CHECKOUT_VERSION-gpu")" ] || ready=1
  elif source_seeds_both; then
    ready=1
  fi
  if [ "$ready" = 0 ]; then
    warn "NODE_IMAGE_TAG=$tag старше этой версии ($CHECKOUT_VERSION), но в реестре главного нет образов $CHECKOUT_VERSION-cpu и $CHECKOUT_VERSION-gpu: тег оставлен, иначе поды не запустятся. Положите образы (install-main.sh --update $CHECKOUT_VERSION или IMAGE_SOURCE=pull) и запустите установщик снова."
    return 0
  fi
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] NODE_IMAGE_TAG в $ENV_FILE: $tag заменился бы на $CHECKOUT_VERSION, когда образы окажутся в реестре"
    return 0
  fi
  say "NODE_IMAGE_TAG в $ENV_FILE был $tag, это старая версия: записан $CHECKOUT_VERSION. Оставить старый тег нарочно: строка NODE_IMAGE_TAG в файле ответов (--answers)."
  ANS[NODE_IMAGE_TAG]="$CHECKOUT_VERSION"
  EXTRA_KEYS+=(NODE_IMAGE_TAG)
  write_env "$ENV_FILE"
}

# После --update VERSION запуск на чекауте этой версии берёт скачанный образ (keep) один раз: сохранённый способ (build, load, pull)
# при этом не меняется, а метка PENDING_IMAGE_VERSION снимается после успешного запуска.
PENDING_USED=0
PENDING_DONE=0
ORIG_IMAGE_SOURCE=""
write_config() {
  local k v
  {
    echo "# Ответы install-main.sh: повторный запуск берёт их отсюда и ничего не спрашивает."
    echo "# Переменные главного (имя хоста, адрес, администраторы, секрет) хранятся в $ENV_FILE."
    for k in $INSTALLER_KEYS; do
      [ "$k" = INSTALL_DOCKER ] && continue
      v="${ANS[$k]:-}"
      if [ "$k" = IMAGE_SOURCE ] && [ "$PENDING_USED" = 1 ]; then v="$ORIG_IMAGE_SOURCE"; fi
      if [ "$k" = PENDING_IMAGE_VERSION ] && [ "$PENDING_DONE" = 1 ]; then v=""; fi
      if [ -n "$v" ]; then printf '%s=%s\n' "$k" "$v"
      else
        # «этот образ не класть», сказанное при архиве рядом с IMAGE_TAR: пустой ключ сохраняется, иначе повторный запуск возьмёт умолчание
        case "$k" in NODE_IMAGE_TAR_CPU|NODE_IMAGE_TAR_GPU)
          [ -z "${ANS[$k]+x}" ] || [ -z "$(node_tar_default "$(printf '%s' "${k##*_}" | tr A-Z a-z)")" ] || printf '%s=\n' "$k" ;;
        esac
      fi
    done
  } >"$CONFIG.tmp"
  chmod 600 "$CONFIG.tmp"
  mv "$CONFIG.tmp" "$CONFIG"
}

prepare_image() {
  case "${ANS[IMAGE_SOURCE]}" in
    pull) pull_main_image "$CHECKOUT_VERSION" ;;
    build)
      say "Сборка образа $IMAGE (повторная сборка без изменений в исходниках идёт из кэша)"
      run docker build --provenance=false -f "$HERE/Dockerfile" -t "$IMAGE" "$REPO" \
        || die "сборка образа завершилась ошибкой (см. вывод выше). Повторите: docker build --provenance=false -f $HERE/Dockerfile -t $IMAGE $REPO" ;;
    load)
      run docker load -i "${ANS[IMAGE_TAR]}" || die "docker load -i ${ANS[IMAGE_TAR]} завершился ошибкой: архив повреждён?"
      [ "$DRY" = 1 ] || docker image inspect "$IMAGE" >/dev/null 2>&1 \
        || die "в архиве ${ANS[IMAGE_TAR]} нет образа $IMAGE (сохраните его: docker save -o nsu-llm-main.tar $IMAGE)" ;;
    keep) : ;;
  esac
}

wait_ready() {
  local ip="${ANS[MAIN_ADVERTISE_IP]}" attempts i code running
  attempts=$(((TIMEOUT + POLL_S - 1) / POLL_S))
  [ "$attempts" -ge 1 ] || attempts=1
  say "Жду готовности https://$ip/readyz (до $TIMEOUT с; первый запуск обычно 2–3 мин, обновление ~30 с)"
  for ((i = 1; i <= attempts; i++)); do
    code="$(curl -sk --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' "https://$ip/readyz" 2>/dev/null || true)"
    if [ "$code" = 200 ]; then ok "главный готов ($((i * POLL_S - POLL_S)) с)"; return 0; fi
    running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
    [ "$running" = true ] || die "контейнер $CONTAINER остановился. Журнал: docker logs --tail 100 $CONTAINER"
    if ((i % 6 == 0)); then say "  ещё не готов: $((i * POLL_S)) с, ответ ${code:-нет}"; fi
    sleep "$POLL_S"
  done
  die "главный не ответил 200 на https://$ip/readyz за $TIMEOUT с. Журнал: docker logs --tail 100 $CONTAINER; поды: docker exec $CONTAINER k3s kubectl -n llm get pods"
}

# Токен подключения нод в полном виде K10<хэш CA>::node:<токен агента> (agent-token.sh в контейнере, как у run-main
# --print-token): по хэшу нода проверяет CA главного. Серверный токен k3s и CLUSTER_TOKEN не печатаются никогда.
node_token() {
  docker exec "$CONTAINER" /opt/llm/main/agent-token.sh 2>/dev/null | tr -d '\r' | head -n1 || true
}

print_result() {
  local host="${ANS[PUBLIC_HOSTNAME]}" ip="${ANS[MAIN_ADVERTISE_IP]}" token
  token="$(node_token)"
  say ""
  say "Главный сервер NSU LLM Cluster готов."
  say "  Портал:              https://$host/"
  if [ "${ANS[CERT_MODE]}" = internal ] || [ -f "${ANS[CERTS_DIR]:-/nonexistent}/ca.crt" ]; then
    say "  Корневой сертификат: http://$ip/ca.crt (установите в доверенные на устройствах пользователей и ПК класса)"
  else
    say "  Сертификат:          свой из ${ANS[CERTS_DIR]}, ca.crt главный не раздаёт"
  fi
  if [ -n "$token" ]; then
    say "  Токен нод:           $token"
    say "  Подключение ноды:"
    local fm=""
    if [ "$SEEDED" = 1 ]; then fm=" --from-main"; fi
    say "    Linux:   sudo ./node/run-node.sh --main $ip --token $token$fm"
    say "    Windows: powershell -ExecutionPolicy Bypass -File .\\node\\run-node.ps1 -Main $ip -Token $token"
  else
    warn "токен нод прочитать не удалось (k3s ещё не записал токен агента): его напечатает main/run-main.sh --print-token, команду подключения покажет портал"
  fi
  if [ "$SEEDED" = 1 ]; then
    say "  Реестр образов нод:  https://$ip/v2/ (только чтение, из сети класса): ноды берут образ с главного, не с ghcr.io (docs/release.md)"
  fi
  say "  Команду подключения с токеном показывает и портал: раздел «Ноды»."
  [ -z "$REMOVED_PKGS" ] || say "  Удалены конфликтующие пакеты: $REMOVED_PKGS (по инструкции docs.docker.com для docker-ce)."
  say "  Пока имени $host нет в DNS, на устройствах пользователей нужна строка в hosts: $ip  $host"
  local rerun="sudo $0"
  [ "$ENV_FILE" = "$REPO/.env" ] || rerun+=" --env-file $ENV_FILE"
  [ "$CONFIG" = "$(dirname "$ENV_FILE")/install-main.env" ] || rerun+=" --config $CONFIG"
  say "  Ответы сохранены в $CONFIG и $ENV_FILE: повторный запуск ($rerun) обновит образ без вопросов, данные сохранятся."
}

# ------------------------------------------------------------------ образы с ghcr.io и реестр главного
CHECKOUT_VERSION="${IMAGE#*:}"
SEEDED=0

# HTTP-код https://ghcr.io/v2/: 401 (нужен токен-посетитель) и 200 значат, что реестр достижим; 000 - соединения нет
ghcr_probe() { curl -sS -m 10 -o /dev/null -w '%{http_code}' "https://$GHCR/v2/" 2>/dev/null || true; }
ghcr_reachable() { local c; c="$(ghcr_probe)"; [ "$c" = 200 ] || [ "$c" = 401 ]; }

check_ghcr() {
  local code hint msg
  code="$(ghcr_probe)"
  case "$code" in
    200|401) ok "ghcr.io доступен (https://$GHCR/v2/ ответил $code)"; return 0 ;;
    403) hint="ghcr.io отвечает 403: сеть закрыта для него (как nvcr.io из России)" ;;
    000|"") hint="соединения с ghcr.io нет (кампусный прокси? закрыт порт 443? нет DNS?)" ;;
    *) hint="ghcr.io ответил HTTP $code" ;;
  esac
  msg="https://$GHCR/v2/ с этого хоста недоступен: $hint. Если нужен прокси, задайте HTTPS_PROXY для установщика (sudo --preserve-env) и для демона Docker (docs/release.md, «Проверка ghcr.io из кампуса»). Без доступа к ghcr.io возьмите архив образа с другой машины и запустите с IMAGE_SOURCE=load (--image-tar nsu-llm-main.tar)."
  if [ "$DRY" = 1 ]; then warn "$msg"; return 0; fi
  die "$msg"
}

# вход в закрытое зеркало (GHCR_TOKEN_FILE): токен идёт в docker login через stdin, в отдельный docker config; выход при завершении
ghcr_login() {
  local f="${ANS[GHCR_TOKEN_FILE]:-}"
  [ -n "$f" ] || return 0
  [ -z "$GHCR_CFG" ] || return 0
  say "Вход в $GHCR как ${ANS[GHCR_OWNER]} (закрытое зеркало, токен из $f)"
  if [ "$DRY" = 1 ]; then say "  [dry-run] docker login $GHCR -u ${ANS[GHCR_OWNER]} --password-stdin < $f"; return 0; fi
  GHCR_CFG="$(mktemp -d)"
  DOCKER_CONFIG="$GHCR_CFG" docker login "$GHCR" -u "${ANS[GHCR_OWNER]}" --password-stdin <"$f" >/dev/null \
    || die "docker login $GHCR не удался: токен из $f недействителен или без права read:packages"
}
# docker с входом в закрытое зеркало (если он был); как run: при --dry-run только печатает команду
ghcr_docker() {
  if [ "$DRY" = 1 ] || [ -z "$GHCR_CFG" ]; then run docker "$@"; else DOCKER_CONFIG="$GHCR_CFG" docker "$@" </dev/null; fi
}

# ref_with_version REF VERSION: тот же образ другой версии (тег после последней «/» заменяется)
ref_with_version() {
  local ref="$1" v="$2" last="${1##*/}"
  if [[ "$last" == *:* ]]; then ref="${ref%:*}"; fi
  printf '%s:%s' "$ref" "$v"
}
main_ref() { # main_ref VERSION
  if [ -n "${ANS[IMAGE_REF]:-}" ]; then ref_with_version "${ANS[IMAGE_REF]}" "$1"; else printf '%s/%s/nsu-llm-main:%s' "$GHCR" "${ANS[GHCR_OWNER]}" "$1"; fi
}
node_repo() { printf '%s' "${ANS[NODE_IMAGE_SOURCE]:-$GHCR/${ANS[GHCR_OWNER]}/nsu-llm-node}"; }
registry_port() { printf '%s' "${ANS[REGISTRY_PORT]:-$DEFAULT_REGISTRY_PORT}"; }

# by_digest REF DIGEST: REF без тега, с @DIGEST (тянется ровно этот манифест: Docker и containerd сверяют содержимое)
by_digest() { local r="$1"; r="${r%@*}"; case "${r##*/}" in *:*) r="${r%:*}" ;; esac; printf '%s@%s' "$r" "$2"; }

pull_main_image() { # pull_main_image VERSION: образ главного с ghcr.io под именем nsu-llm-main:VERSION
  local ver="$1" ref want got src
  ref="$(main_ref "$ver")"
  check_ghcr
  ghcr_login
  # release/<версия>.json (release.sh) называет digest каждого образа: тег в ghcr.io можно перезаписать, digest нет (SEC-2 M3)
  want="$(release_digest "$ver" main)"
  src="$ref"
  if [ -n "$want" ]; then src="$(by_digest "$ref" "$want")"; fi
  say "Загрузка образа главного $src (около 190 МБ)"
  ghcr_docker pull "$src" \
    || die "docker pull $src не удался: образа нет (версия $ver не выпущена? GHCR_OWNER=${ANS[GHCR_OWNER]} указан неверно? пакет закрытый: нужен GHCR_TOKEN_FILE) или сеть до ghcr.io. Без сети: IMAGE_SOURCE=load"
  if [ -n "$want" ]; then
    ok "образ главного совпал с release/$ver.json: $want"
  elif [ "$DRY" = 0 ]; then
    got="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" 2>/dev/null | tr -d '\r' | sed -n 's/^.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' | head -n1 || true)"
    warn "нет release/$ver.json: digest образа главного не с чем сверить (получен ${got:-неизвестный digest}); сверьте его с выпуском на GitHub"
  fi
  run docker tag "$src" "nsu-llm-main:$ver"
}

ctr() { run docker exec "$CONTAINER" k3s ctr -n k8s.io "$@"; }

# registry_digest VERSION-VARIANT: digest манифеста в реестре главного (пусто: тега нет)
registry_digest() { registry_repo_digest nsu-llm-node "$1"; }
# registry_repo_digest РЕПОЗИТОРИЙ ТЕГ: то же для любого репозитория реестра (pause: rancher/mirrored-pause)
registry_repo_digest() {
  docker exec "$CONTAINER" curl -fsSI -m 10 \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "http://127.0.0.1:$(registry_port)/v2/$1/manifests/$2" </dev/null 2>/dev/null \
    | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: *//p' | head -n1 || true
}
# release_digest VERSION KEY: digest образа из release/<версия>.json (его пишет release.sh): KEY main, cpu или gpu (образ ноды);
# пусто, если файла нет
release_digest() {
  local key="node-$2"; [ "$2" != main ] || key=main
  sed -n "s/.*\"$key\": {[^}]*\"digest\": \"\\(sha256:[0-9a-f]*\\)\".*/\\1/p" "${NSU_LLM_RELEASE_DIR:-$REPO/release}/$1.json" 2>/dev/null | head -n1 || true
}

# node_tar_default VARIANT: nsu-llm-node-<версия чекаута>-<cpu|gpu>.tar.gz (или .tar) рядом с архивом образа главного; пусто, если файла нет
node_tar_default() {
  local d f
  d="$(dirname "${ANS[IMAGE_TAR]:-.}")"
  for f in "$d/nsu-llm-node-$CHECKOUT_VERSION-$1.tar.gz" "$d/nsu-llm-node-$CHECKOUT_VERSION-$1.tar"; do
    if [ -f "$f" ]; then printf '%s' "$f"; return; fi
  done
}
# tar_stream FILE: архив образа в stdout (.gz распаковывается)
tar_stream() { case "$1" in *.gz|*.tgz) gunzip -c "$1" ;; *) cat "$1" ;; esac; }

# Образ pause (песочница каждого пода) нода k3s берёт с docker.io: зеркало главного для docker.io отвечает 404 на всё, кроме nsu-llm-node,
# и k3s идёт на Docker Hub. Если из подсети класса Hub не достать, ни один под не стартует (ContainerCreating, failed to pull sandbox
# image). Поэтому pause из containerd главного кладётся в реестр главного под тем же путём rancher/mirrored-pause:<тег>. Тег берётся
# из списка образов главного, не из константы: версия k3s у главного и у ноды одна (K3S_VERSION в обоих Dockerfile), а тег pause
# зашит в k3s. Уже лежащий пропускается; любая неудача только предупреждает, установка из-за неё не останавливается.
# Pause это индекс из нескольких платформ, а в containerd главного лежит содержимое только linux/amd64: ctr images push отправил бы индекс
# целиком, и реестр отклонил бы его (manifest blob unknown). registry-push.sh --amd64-manifest кладёт манифест linux/amd64 под тем же тегом.
seed_pause() {
  local port tags tag src dst
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] образ pause из containerd главного (docker.io/rancher/mirrored-pause:<тег>) в реестр главного (rancher/mirrored-pause:<тег>)"
    return 0
  fi
  port="$(registry_port)"
  tags="$(docker exec "$CONTAINER" k3s ctr -n k8s.io images ls -q </dev/null 2>/dev/null \
    | tr -d '\r' | sed -n 's#^docker\.io/rancher/mirrored-pause:\([A-Za-z0-9_][A-Za-z0-9._-]*\)$#\1#p' | sort -u || true)"
  if [ -z "$tags" ]; then
    warn "в containerd главного нет образа docker.io/rancher/mirrored-pause: в реестр он не кладётся, ноды возьмут pause с Docker Hub, и если подсеть класса его не достаёт, поды не стартуют (проверка: docker exec $CONTAINER k3s ctr -n k8s.io images ls -q | grep pause)"
    return 0
  fi
  for tag in $tags; do
    src="docker.io/rancher/mirrored-pause:$tag"; dst="127.0.0.1:$port/rancher/mirrored-pause:$tag"
    if [ -n "$(registry_repo_digest rancher/mirrored-pause "$tag")" ]; then
      ok "в реестре уже есть rancher/mirrored-pause:$tag: не кладётся заново"
      continue
    fi
    say "  $src -> $dst"
    if ! docker exec "$CONTAINER" k3s ctr -n k8s.io images tag --force "$src" "$dst" </dev/null >/dev/null; then
      warn "k3s ctr images tag $src $dst не удался: pause в реестр не положен, ноды возьмут его с Docker Hub"
      continue
    fi
    # образ главного старше 0.2.1 не знает --amd64-manifest (registry-push.sh отвечает «использование»): запись не удаётся, это то же предупреждение
    docker exec "$CONTAINER" /opt/llm/main/registry-push.sh "$dst" --amd64-manifest </dev/null >/dev/null \
      || warn "запись $dst в реестр главного не удалась (registry-push.sh --amd64-manifest; образ главного старше 0.2.1 его не знает: после --update и перезапуска повторите установщик): pause не положен, ноды возьмут его с Docker Hub; журнал реестра: docker exec $CONTAINER k3s kubectl -n llm logs deploy/registry"
    docker exec "$CONTAINER" k3s ctr -n k8s.io images rm --sync "$dst" </dev/null >/dev/null 2>&1 || true
    if [ -n "$(registry_repo_digest rancher/mirrored-pause "$tag")" ]; then ok "реестр главного: rancher/mirrored-pause:$tag"
    else warn "после записи в реестре нет rancher/mirrored-pause:$tag: ноды возьмут pause с Docker Hub"; fi
  done
}

# Реестр главного (под registry в k3s, том llm-main-registry). Образы нод попадают в него с 127.0.0.1:<порт> этого же контейнера:
# писать в реестр может только главный, класс читает через Caddy. Публичные образы пуллятся прямо в k3s главного (ctr, digest
# сохраняется). Закрытое зеркало (GHCR_TOKEN_FILE): образ скачивает docker хоста (вход в отдельный docker config, токен через stdin) и
# передаётся в k3s потоком docker save | ctr images import: токен не попадает ни в одну командную строку, ни внутрь контейнера.
# Версия, которая уже есть в реестре (и не расходится с release/<версия>.json), пропускается: повторный запуск ничего не качает.
# IMAGE_SOURCE=load (seed_registry VERSION load): ghcr.io недоступен, образы нод берутся из архивов docker save (NODE_IMAGE_TAR_CPU и
# NODE_IMAGE_TAR_GPU) и передаются в k3s главного потоком gunzip | ctr images import, дальше всё как с ghcr.io. Образ в архиве называется
# nsu-llm-node:<версия>-<cpu|gpu> (docker save nsu-llm-node:<версия>-<cpu|gpu>); в containerd он появляется как docker.io/library/nsu-llm-node:<тег>.
seed_registry() { # seed_registry VERSION [load]
  local ver="$1" from_tar=0 repo port variant src dst archive key attempts i have want tags
  local seeded=()
  [ "${2:-}" != load ] || from_tar=1
  port="$(registry_port)"
  [ "$from_tar" = 1 ] || repo="$(node_repo)"   # у load нет ghcr.io, а с ним и GHCR_OWNER
  if [ "$from_tar" = 1 ]; then
    say "Образы нод из архивов в реестр главного (127.0.0.1:$port внутри контейнера $CONTAINER; класс берёт их по https://${ANS[MAIN_ADVERTISE_IP]}/v2/):"
  else
    say "Образы нод в реестр главного (127.0.0.1:$port внутри контейнера $CONTAINER; класс берёт их по https://${ANS[MAIN_ADVERTISE_IP]}/v2/):"
  fi
  if [ "$DRY" = 0 ]; then
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" = true ] \
      || die "контейнер $CONTAINER не запущен: реестру некуда принять образы. Запустите установщик без --update или docker start $CONTAINER"
    attempts=$(((TIMEOUT + POLL_S - 1) / POLL_S)); [ "$attempts" -ge 1 ] || attempts=1
    for ((i = 1; i <= attempts; i++)); do
      if docker exec "$CONTAINER" curl -fsS -m 5 -o /dev/null "http://127.0.0.1:$port/v2/" </dev/null >/dev/null 2>&1; then break; fi
      [ "$i" -lt "$attempts" ] || die "реестр на 127.0.0.1:$port не ответил за $TIMEOUT с. Поды: docker exec $CONTAINER k3s kubectl -n llm get pods; журнал: docker exec $CONTAINER k3s kubectl -n llm logs deploy/registry"
      sleep "$POLL_S"
    done
  fi
  for variant in cpu gpu; do
    dst="127.0.0.1:$port/nsu-llm-node:$ver-$variant"
    archive=""; key=""
    if [ "$from_tar" = 1 ]; then
      src="docker.io/library/nsu-llm-node:$ver-$variant"
      key="NODE_IMAGE_TAR_$(printf '%s' "$variant" | tr a-z A-Z)"
      archive="${ANS[$key]:-}"
    else
      src="$repo:$ver-$variant"
      # с release/<версия>.json образ ноды тянется по digest оттуда (SEC-2 M3): перезаписанный тег в ghcr.io не доедет до класса
      want="$(release_digest "$ver" "$variant")"
      [ -z "$want" ] || src="$(by_digest "$src" "$want")"
    fi
    if [ "$DRY" = 0 ]; then
      have="$(registry_digest "$ver-$variant")"
      want="$(release_digest "$ver" "$variant")"
      if [ -n "$have" ] && { [ -z "$want" ] || [ "$have" = "$want" ]; }; then
        ok "в реестре уже есть nsu-llm-node:$ver-$variant ($have): не скачивается заново"
        seeded+=("$variant")
        continue
      fi
      [ -z "$have" ] || warn "nsu-llm-node:$ver-$variant в реестре ($have) расходится с release/$ver.json ($want): образ загружается заново"
    fi
    if [ "$from_tar" = 1 ] && [ -z "$archive" ]; then
      warn "nsu-llm-node:$ver-$variant в реестр не кладётся: нет архива ($key; по умолчанию nsu-llm-node-$ver-$variant.tar.gz рядом с архивом образа главного). Ноды возьмут этот образ из архива (install-node.ps1 -ImageTar) или после его добавления: повторный запуск установщика"
      continue
    fi
    if [ "$from_tar" = 1 ]; then
      say "  $archive -> $dst"
      if [ "$DRY" = 1 ]; then
        say "  [dry-run] $(case "$archive" in *.gz|*.tgz) printf 'gunzip -c' ;; *) printf 'cat' ;; esac) $archive | docker exec -i $CONTAINER k3s ctr -n k8s.io images import --no-unpack -"
      else
        tar_stream "$archive" | docker exec -i "$CONTAINER" k3s ctr -n k8s.io images import --no-unpack - \
          || die "загрузка образа из $archive в k3s главного (gunzip | ctr images import) не удалась: архив повреждён или это не docker save (сохраните: docker save --platform linux/amd64 nsu-llm-node:$ver-$variant | gzip > nsu-llm-node-$ver-$variant.tar.gz)"
      fi
      ctr images tag --force "$src" "$dst" \
        || die "в архиве $archive нет образа nsu-llm-node:$ver-$variant (k3s ctr images tag $src $dst не удался): архив собран из образа с другим именем или версией"
    else
      say "  $src -> $dst"
      if [ -n "${ANS[GHCR_TOKEN_FILE]:-}" ]; then
        ghcr_login
        ghcr_docker pull "$src" || die "docker pull $src не удался: образа нет, закрытый пакет без права read:packages у токена или нет доступа к ${src%%/*}"
        run docker tag "$src" "$dst"
        if [ "$DRY" = 1 ]; then
          say "  [dry-run] docker save $dst | docker exec -i $CONTAINER k3s ctr -n k8s.io images import --no-unpack -"
        else
          docker save "$dst" | docker exec -i "$CONTAINER" k3s ctr -n k8s.io images import --no-unpack - \
            || die "передача образа $dst в k3s главного (docker save | ctr images import) не удалась"
        fi
        run docker rmi "$dst" >/dev/null || true
      else
        ctr images pull --platform linux/amd64 "$src" \
          || die "не удалось скачать $src внутри $CONTAINER (k3s ctr): образа нет, нет доступа к ${src%%/*} из контейнера (прокси: HTTPS_PROXY в .env главного) или закончилось место. Повтор: docker exec $CONTAINER k3s ctr -n k8s.io images pull --platform linux/amd64 $src"
        ctr images tag --force "$src" "$dst" || die "k3s ctr images tag $src $dst не удался"
      fi
    fi
    # реестр главного принимает запись только с паролем (SEC-2 M2): registry-push.sh внутри контейнера берёт его из secrets.env;
    # главный до этой версии (без registry-push.sh, --update из старой установки) ещё пишет без пароля на 127.0.0.1:<порт>
    if [ "$DRY" = 1 ] || docker exec "$CONTAINER" test -x /opt/llm/main/registry-push.sh </dev/null >/dev/null 2>&1; then
      run docker exec "$CONTAINER" /opt/llm/main/registry-push.sh "$dst" \
        || die "запись $dst в реестр главного не удалась (registry-push.sh): журнал реестра: docker exec $CONTAINER k3s kubectl -n llm logs deploy/registry"
    else
      ctr images push --plain-http --platform linux/amd64 "$dst" || die "k3s ctr images push $dst не удался: реестр не принимает запись (журнал: docker exec $CONTAINER k3s kubectl -n llm logs deploy/registry)"
    fi
    if [ "$from_tar" = 0 ] && [ -n "${ANS[GHCR_TOKEN_FILE]:-}" ]; then ctr images rm --sync "$dst" >/dev/null || warn "не удалось убрать временный тег $dst из containerd главного"
    else ctr images rm --sync "$src" "$dst" >/dev/null || warn "не удалось убрать временные теги $src и $dst из containerd главного (на работу реестра не влияет)"; fi
    seeded+=("$variant")
  done
  if [ "$DRY" = 0 ] && [ "${#seeded[@]}" -gt 0 ]; then
    tags="$(docker exec "$CONTAINER" curl -fsS -m 10 "http://127.0.0.1:$port/v2/nsu-llm-node/tags/list" </dev/null 2>/dev/null || true)"
    for variant in "${seeded[@]}"; do
      grep -q "\"$ver-$variant\"" <<<"$tags" \
        || die "после загрузки в реестре нет nsu-llm-node:$ver-$variant (docker exec $CONTAINER curl -s http://127.0.0.1:$port/v2/nsu-llm-node/tags/list)"
    done
    if [ "${#seeded[@]}" = 2 ]; then ok "реестр главного: nsu-llm-node:$ver-cpu и $ver-gpu"; else ok "реестр главного: nsu-llm-node:$ver-${seeded[0]}"; fi
    SEEDED=1
  fi
  seed_pause
}

# IMAGE_SOURCE=load и есть хотя бы один архив образа ноды: образы нод кладутся в реестр главного из архивов
load_seeds_registry() { [ "${ANS[IMAGE_SOURCE]:-}" = load ] && { [ -n "${ANS[NODE_IMAGE_TAR_CPU]:-}" ] || [ -n "${ANS[NODE_IMAGE_TAR_GPU]:-}" ]; }; }

# --update VERSION: образ главного и образы нод новой версии; контейнер не перезапускается
do_update() {
  local ver="$UPDATE_VERSION" ref step=1 k
  say "Проверка хоста:"
  ok "ОС: ${OS[PRETTY_NAME]:-${OS[ID]:-неизвестна}}"
  check_docker
  [ "$EXISTING" = 1 ] || die "главный не установлен (контейнера $CONTAINER нет): --update обновляет существующую установку. Для новой запустите установщик без --update."
  need GHCR_OWNER "Владелец образов на GitHub (GHCR_OWNER; образы лежат в ghcr.io/<владелец>/nsu-llm-main)" "$DEFAULT_GHCR_OWNER"
  for k in IMAGE_REF NODE_IMAGE_SOURCE GHCR_TOKEN_FILE; do [ -z "${ANS[$k]:-}" ] || need "$k" "$k" ""; done
  ANS[MAIN_ADVERTISE_IP]="${ANS[MAIN_ADVERTISE_IP]:-<MAIN_ADVERTISE_IP>}"
  ref="$(main_ref "$ver")"
  say ""
  say "Будет выполнено:"
  say "  $((step++)). проверить доступность https://$GHCR/v2/"
  say "  $((step++)). скачать образ главного $ref и пометить его nsu-llm-main:$ver"
  say "  $((step++)). положить образы нод $(node_repo):$ver-cpu и $ver-gpu в реестр главного (то, что там уже есть, пропускается)"
  say "  $((step++)). записать в $CONFIG метку PENDING_IMAGE_VERSION=$ver: запуск установщика на чекауте v$ver возьмёт скачанный образ и не будет ни собирать, ни загружать, ни скачивать его заново (способ образа в $CONFIG не меняется)"
  say "  контейнер $CONTAINER не перезапускается: это отдельный шаг (ниже)"
  if [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then yesno "Продолжить?" y || die "отменено, ничего не изменено"; fi
  say ""
  pull_main_image "$ver"
  seed_registry "$ver"
  if [ "$DRY" = 1 ]; then say "  [dry-run] в $CONFIG было бы записано: PENDING_IMAGE_VERSION=$ver"
  else ANS[PENDING_IMAGE_VERSION]="$ver"; ORIG_IMAGE_SOURCE="${ANS[IMAGE_SOURCE]:-}"; PENDING_USED=1; write_config; fi
  say ""
  say "Версия $ver загружена: образ nsu-llm-main:$ver и образы нод в реестре главного. Главный ещё работает на прежней версии."
  say "Чтобы перейти на неё (короткий перерыв ~30 с, данные в томах llm-main-* сохраняются):"
  say "  1. git fetch --tags && git checkout v$ver        (скрипты и образы должны быть одной версии)"
  say "  2. sudo $0 --yes                                   (возьмёт скачанный образ nsu-llm-main:$ver как есть, без сборки и без обращения к ghcr.io, и пересоздаст $CONTAINER)"
  say "Ноды берут образы новой версии с главного: run-node.sh --from-main, install-node.ps1 (по умолчанию с главного) после шага 2."
}

# ------------------------------------------------------------------ удаление
do_uninstall() {
  [ -f "$CONFIG" ] && load_kv "$CONFIG" "$CONFIG" installer
  detect_fw
  say "Будут удалены безвозвратно: контейнер $CONTAINER и тома llm-main-k3s, llm-main-kubelet, llm-main-data, llm-main-hf, llm-main-registry"
  say "(база портала и LiteLLM, ключи пользователей, секреты, CA главного, кэш зеркала Hugging Face, свои модели и реестр образов нод)."
  local hp_tool="" hp_net="${ANS[HAIRPIN_SUBNET]:-}"
  if [ -n "$hp_net" ] && [ -n "$FW_TOOL" ]; then
    hp_tool="$FW_TOOL"
    say "Правило $hp_tool для подсети Docker $hp_net на 6443/tcp (hairpin), добавленное установщиком, будет удалено."
  fi
  if [ "${ANS[FIREWALL]:-no}" = yes ] && [ -n "$FW_TOOL" ]; then
    say "Правила $FW_TOOL для портов 80, 443, 6443/tcp будут удалены."
  else
    FW_TOOL=""
  fi
  if [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    yesno "Удалить главный сервер?" n || die "отменено, ничего не удалено"
  fi
  local args=(--uninstall)
  [ "$DRY" = 0 ] || args+=(--dry-run)
  "$BASH" "$RUN_MAIN" "${args[@]}" </dev/null || die "run-main.sh --uninstall завершился ошибкой (см. вывод выше)"
  fw_apply close
  if [ -n "$hp_tool" ]; then hairpin_apply "$hp_tool" close "$hp_net"; fi
  say "Файлы $ENV_FILE и $CONFIG оставлены (в .env секрет клиента OIDC): удалите их, если установка больше не нужна."
}

# ------------------------------------------------------------------ основной сценарий
main() {
  local yn
  check_os
  if [ "$UNINSTALL" = 1 ]; then do_uninstall; return; fi
  load_answers
  if [ -n "$UPDATE_VERSION" ]; then do_update; return; fi
  require_yes_keys
  if [ -n "${ANS[PENDING_IMAGE_VERSION]:-}" ]; then
    if [ "${ANS[PENDING_IMAGE_VERSION]}" = "$CHECKOUT_VERSION" ]; then
      ORIG_IMAGE_SOURCE="${ANS[IMAGE_SOURCE]:-}"
      ANS[IMAGE_SOURCE]=keep; SRC[IMAGE_SOURCE]="--update ${ANS[PENDING_IMAGE_VERSION]}"
      PENDING_USED=1
      ok "после --update ${ANS[PENDING_IMAGE_VERSION]} берётся уже скачанный образ $IMAGE (без сборки, загрузки и обращения к ghcr.io)"
    else
      warn "после --update ${ANS[PENDING_IMAGE_VERSION]} скачан образ другой версии, а этот чекаут $CHECKOUT_VERSION: для перехода выполните git checkout v${ANS[PENDING_IMAGE_VERSION]}; иначе будет установлена версия $CHECKOUT_VERSION обычным способом"
    fi
  fi

  say "Проверка хоста:"
  ok "ОС: ${OS[PRETTY_NAME]:-${OS[ID]:-неизвестна}}"
  [ "$OS_FAMILY" != other ] || warn "дистрибутив не из семейств Debian/Ubuntu и RHEL: Docker при необходимости ставится вручную"
  check_docker
  check_ports
  check_curl
  check_resources
  detect_ip
  if [ "${#PROBLEMS[@]}" -gt 0 ]; then
    say ""
    say "Установка невозможна, исправьте:"
    local p
    for p in "${PROBLEMS[@]}"; do say "  - $p"; done
    die "хост не готов (${#PROBLEMS[@]} проблем(ы), см. выше)"
  fi
  detect_fw

  say ""
  need PUBLIC_HOSTNAME "Имя хоста портала (PUBLIC_HOSTNAME; по нему пользователи открывают портал и на него провайдер возвращает их после входа, например llm.example.org)" ""
  need MAIN_ADVERTISE_IP "LAN-адрес главного для нод (MAIN_ADVERTISE_IP)" "$DETECTED_IP"
  [ "$YES" = 1 ] || show_redirect_uri
  need OIDC_ISSUER "Адрес OIDC-провайдера (OIDC_ISSUER, только https; Keycloak: https://<хост>/realms/<realm>; пример NSU ID: https://sso-ng.nsu.ru/auth/realms/NSU)" ""
  need OIDC_CLIENT_ID "Client ID клиента, зарегистрированного у провайдера (OIDC_CLIENT_ID)" ""
  local provider_def=SSO
  [[ "${ANS[OIDC_ISSUER]}" != *sso-ng.nsu.ru* ]] || provider_def="NSU ID"
  need LOGIN_PROVIDER_NAME "Как назвать провайдера на кнопке входа (LOGIN_PROVIDER_NAME: «Войти через ...»)" "$provider_def"
  need OIDC_CLIENT_SECRET "Секрет клиента (OIDC_CLIENT_SECRET, ввод скрыт)" "" -s
  need ADMIN_EMAILS "Администраторы (e-mail через запятую, ADMIN_EMAILS)" ""
  need ADMIN_REQUIRE_EMAIL_VERIFIED "Требовать подтверждённый e-mail для роли администратора (ADMIN_REQUIRE_EMAIL_VERIFIED; для NSU ID ответьте «нет»: он присылает email_verified=false) (да/нет)" да
  need TRUSTED_PROXIES "Фронт-прокси перед главным, адреса через запятую (TRUSTED_PROXIES; например 10.0.0.10 — nginx перед главным; Enter — нет)" ""
  need CERT_MODE "Сертификат HTTPS: 1 — внутренний CA главного (internal), 2 — свой из каталога с tls.crt и tls.key (own)" internal
  if [ "${ANS[CERT_MODE]}" = own ]; then
    need CERTS_DIR "Каталог с tls.crt и tls.key (и необязательно ca.crt)" ""
  fi
  need AIRGAP "Режим без интернета: заранее положить системные образы k3s, Postgres, Redis и LiteLLM (airgap)? (да/нет)" нет
  if [ -n "$FW_TOOL" ]; then
    need FIREWALL "Открыть порты 80, 443, 6443/tcp в $FW_TOOL? (да/нет)" да
    if [ "${ANS[FIREWALL]}" != yes ]; then
      need HAIRPIN_RULE "Разрешить в $FW_TOOL подсети Docker доступ к 6443/tcp хоста (hairpin)? Без этого контейнер главного не достанет до ${ANS[MAIN_ADVERTISE_IP]}:6443 и логи подов на других машинах не заработают (да/нет)" да
    fi
  fi
  local img_def=keep img_prompt
  if [ -f "$HERE/Dockerfile" ]; then img_def=build; fi
  img_prompt="Образ главного: 2 — собрать из исходников (build), 3 — загрузить архив docker save (load), 4 — взять имеющийся (keep)"
  # ghcr.io проверяется, только если способ ещё не задан: «с ghcr.io» (pull) предлагается первым, когда сеть его пропускает
  if [ -z "${ANS[IMAGE_SOURCE]:-}" ]; then
    if ghcr_reachable; then
      img_prompt="Образ главного: 1 — скачать с ghcr.io (pull), 2 — собрать из исходников (build), 3 — загрузить архив docker save (load), 4 — взять имеющийся (keep)"
      img_def=pull
    else
      img_prompt="$img_prompt (скачивание с ghcr.io недоступно: ghcr.io с этого хоста не отвечает)"
    fi
  fi
  need IMAGE_SOURCE "$img_prompt" "$img_def"
  if [ "${ANS[IMAGE_SOURCE]}" = load ]; then
    need IMAGE_TAR "Путь к архиву образа (docker save)" ""
    local variant
    for variant in cpu gpu; do
      need "NODE_IMAGE_TAR_$(printf '%s' "$variant" | tr a-z A-Z)" \
        "Архив образа ноды $variant для реестра главного (docker save, nsu-llm-node-<версия>-$variant.tar.gz; Enter — не класть)" "$(node_tar_default "$variant")"
    done
  fi
  if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then
    need GHCR_OWNER "Владелец образов на GitHub (GHCR_OWNER; образы лежат в ghcr.io/<владелец>/nsu-llm-main)" "$DEFAULT_GHCR_OWNER"
    local k
    for k in IMAGE_REF NODE_IMAGE_SOURCE GHCR_TOKEN_FILE; do [ -z "${ANS[$k]:-}" ] || need "$k" "$k" ""; done
  fi
  if [ "${ANS[IMAGE_SOURCE]}" = keep ] && [ "$DOCKER_PLANNED" = 0 ] && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    die "образа $IMAGE нет в Docker: выберите сборку (IMAGE_SOURCE=build) или загрузку архива (--image-tar FILE)"
  fi

  local ip="${ANS[MAIN_ADVERTISE_IP]}" host="${ANS[PUBLIC_HOSTNAME]}" step=1
  local rm_args=(--env-file "$ENV_FILE" --image "$IMAGE")
  [ "${ANS[AIRGAP]}" = no ] || rm_args+=(--airgap)
  [ "${ANS[CERT_MODE]}" = internal ] || rm_args+=(--certs "${ANS[CERTS_DIR]}")

  say ""
  say "Будет выполнено:"
  say "  портал:             https://$host/  (адрес для нод $ip)"
  say "  администраторы:     ${ANS[ADMIN_EMAILS]}"
  say "  вход:               ${ANS[OIDC_ISSUER]} (client id ${ANS[OIDC_CLIENT_ID]}, кнопка «Войти через ${ANS[LOGIN_PROVIDER_NAME]}»)"
  say "  redirect URI:       $(redirect_uri)  (его нужно зарегистрировать у провайдера)"
  local arev="${ANS[ADMIN_REQUIRE_EMAIL_VERIFIED]:-}"
  [ -n "$arev" ] || arev="$(sed -n 's/^ADMIN_REQUIRE_EMAIL_VERIFIED=//p' "$REPO/.env.example" 2>/dev/null | tail -n1)"
  if [ "$arev" = false ]; then
    warn "ADMIN_REQUIRE_EMAIL_VERIFIED=false: роль администратора определяет только ADMIN_EMAILS, подтверждение e-mail не проверяется (так нужно для NSU ID). Если ваш провайдер подтверждает почту, поставьте true в $ENV_FILE и повторите установку."
  fi
  [ -z "${ANS[TRUSTED_PROXIES]}" ] || say "  фронт-прокси:       ${ANS[TRUSTED_PROXIES]} (им главный верит X-Forwarded-For)"
  say "  клиентский секрет:  задан (${SRC[OIDC_CLIENT_SECRET]:-ответ})"
  if [ "${ANS[CERT_MODE]}" = internal ]; then say "  сертификат:         внутренний CA главного"; else say "  сертификат:         свой из ${ANS[CERTS_DIR]}"; fi
  if [ "${ANS[AIRGAP]}" = yes ]; then say "  airgap:             да"; else say "  airgap:             нет"; fi
  [ "$EXISTING" = 0 ] || say "  установка уже есть: контейнер $CONTAINER будет обновлён, данные в томах llm-main-* сохранятся"
  say "  $((step++)). записать $ENV_FILE (права 600) и ответы в $CONFIG"
  if [ -z "$FW_TOOL" ]; then
    say "  $((step++)). брандмауэр: ufw и firewalld не активны, порты не настраиваются"
  elif [ "${ANS[FIREWALL]}" = yes ]; then
    say "  $((step++)). брандмауэр $FW_TOOL: открыть 80, 443, 6443/tcp"
  else
    say "  $((step++)). брандмауэр $FW_TOOL: не трогать (откройте 80, 443, 6443/tcp сами)"
    if want_hairpin_rule; then
      HP_SUBNET="$(docker_bridge_subnet)"
      say "  $((step++)). брандмауэр $FW_TOOL: добавить одно узкое правило, подсеть Docker $HP_SUBNET -> 6443/tcp: без него контейнер главного не достаёт до $ip:6443 (hairpin), API объявляется по адресу контейнера, и логи и exec подов на других машинах не работают"
    fi
  fi
  case "${ANS[IMAGE_SOURCE]}" in
    pull) say "  $((step++)). скачать образ главного $(main_ref "$CHECKOUT_VERSION") с ghcr.io (docker pull, доступность проверяется заранее) и пометить его $IMAGE" ;;
    build) say "  $((step++)). собрать образ $IMAGE из $REPO" ;;
    load) say "  $((step++)). загрузить образ из ${ANS[IMAGE_TAR]}" ;;
    keep) say "  $((step++)). взять имеющийся образ $IMAGE" ;;
  esac
  say "  $((step++)). main/run-main.sh ${rm_args[*]}: (пере)создать контейнер $CONTAINER"
  say "  $((step++)). дождаться https://$ip/readyz (до $TIMEOUT с)"
  if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then
    say "  $((step++)). скачать образы нод $(node_repo):$CHECKOUT_VERSION-cpu и -gpu внутри $CONTAINER и положить в реестр главного (https://$ip/v2/ для класса)"
  fi
  if load_seeds_registry; then
    say "  $((step++)). положить образы нод из архивов (${ANS[NODE_IMAGE_TAR_CPU]:-нет} и ${ANS[NODE_IMAGE_TAR_GPU]:-нет}) в реестр главного (https://$ip/v2/ для класса)"
  elif [ "${ANS[IMAGE_SOURCE]}" = load ]; then
    say "  образы нод в реестр главного не кладутся: архивов NODE_IMAGE_TAR_CPU и NODE_IMAGE_TAR_GPU нет (ноды берут образ из архива: install-node.ps1 -ImageTar)"
  fi
  if [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    yesno "Продолжить?" y || die "отменено, ничего не изменено"
  fi
  say ""

  local env_target="$ENV_FILE"
  if [ "$DRY" = 1 ]; then
    DRY_DIR="$(mktemp -d)"
    env_target="$DRY_DIR/main.env"
    rm_args[1]="$env_target"
    say "  [dry-run] в $ENV_FILE были бы записаны (права 600):"
    for yn in "${ASKED_ENV_KEYS[@]}" "${EXTRA_KEYS[@]}"; do
      case "$yn" in
        *TOKEN* | *SECRET* | *PASSWORD*) say "    $yn=***" ;;
        *) say "    $yn=${ANS[$yn]:-}" ;;
      esac
    done
    say "  [dry-run] ответы установщика: $CONFIG"
  fi
  write_env "$env_target"
  [ "$DRY" = 1 ] || { write_config; ok "записаны $ENV_FILE и $CONFIG"; }

  if [ -n "$FW_TOOL" ] && [ "${ANS[FIREWALL]}" = yes ]; then fw_apply open; fi
  if want_hairpin_rule; then
    HP_SUBNET="${HP_SUBNET:-$(docker_bridge_subnet)}"
    hairpin_apply "$FW_TOOL" open "$HP_SUBNET"
    if [ "$DRY" = 0 ]; then ANS[HAIRPIN_SUBNET]="$HP_SUBNET"; write_config; fi
  fi
  prepare_image
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] main/run-main.sh ${rm_args[*]} --dry-run:"
    "$BASH" "$RUN_MAIN" "${rm_args[@]}" --dry-run </dev/null
    if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then seed_registry "$CHECKOUT_VERSION"; fi
    if load_seeds_registry; then seed_registry "$CHECKOUT_VERSION" load; fi
    refresh_node_tag
    return
  fi
  # Работающий главный: образы новой версии в реестр до его перезапуска, как --update. Иначе новый портал минуты ждёт образы и поды
  # переходят на недоступный тег, а потом на digest: модели грузятся дважды. Сбой здесь оставляет главный работать как работал.
  local seeded_first=0
  if main_running; then
    if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then seed_registry "$CHECKOUT_VERSION"; seeded_first=1; fi
    if load_seeds_registry; then seed_registry "$CHECKOUT_VERSION" load; seeded_first=1; fi
  fi
  refresh_node_tag
  "$BASH" "$RUN_MAIN" "${rm_args[@]}" </dev/null || die "run-main.sh завершился с ошибкой (см. вывод выше); повторите установщик после исправления"
  wait_ready
  if [ "$seeded_first" = 0 ]; then
    if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then seed_registry "$CHECKOUT_VERSION"; fi
    if load_seeds_registry; then seed_registry "$CHECKOUT_VERSION" load; fi
  fi
  if [ "$PENDING_USED" = 1 ]; then SEEDED=1; PENDING_DONE=1; write_config; fi   # образы нод --update уже положил в реестр
  print_result
  check_hairpin
  [ "$DEGRADED" = 0 ] || return 3
}

main
