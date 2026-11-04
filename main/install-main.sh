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
#   --answers FILE  файл ответов KEY=VALUE: PUBLIC_HOSTNAME, MAIN_ADVERTISE_IP, OIDC_CLIENT_SECRET, ADMIN_EMAILS,
#                   TRUSTED_PROXIES (адреса фронт-прокси через запятую, можно пусто), CERT_MODE=internal|own,
#                   CERTS_DIR, AIRGAP=yes|no, FIREWALL=yes|no, IMAGE_SOURCE=pull|build|load|keep,
#                   IMAGE_TAR, GHCR_OWNER, IMAGE_REF, NODE_IMAGE_SOURCE, GHCR_TOKEN_FILE, INSTALL_DOCKER=yes|no;
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
# (права 0600). GHCR_OWNER берётся из ответов или из переменной окружения. Без доступа к ghcr.io: IMAGE_SOURCE=load.
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
IMAGE="nsu-llm-main:0.1.0"
CONTAINER="llm-main"
PORTS=(80 443 6443)
MIN_RAM_GIB=4
MIN_DISK_GIB=20
WARN_DISK_GIB=50
POLL_S=5
# Только для тестов: корень, от которого читаются /etc/os-release, /proc/meminfo и пишется /etc/apt.
SYSROOT="${NSU_LLM_SYSROOT:-}"
ENV_SECRET="${OIDC_CLIENT_SECRET:-}"
INSTALLER_KEYS=" CERT_MODE CERTS_DIR AIRGAP FIREWALL IMAGE_SOURCE IMAGE_TAR GHCR_OWNER IMAGE_REF NODE_IMAGE_SOURCE GHCR_TOKEN_FILE PENDING_IMAGE_VERSION INSTALL_DOCKER "
ENV_OWNER="${GHCR_OWNER:-}"
GHCR=ghcr.io
DEFAULT_REGISTRY_PORT=5000
ASKED_ENV_KEYS=(PUBLIC_HOSTNAME MAIN_ADVERTISE_IP OIDC_CLIENT_SECRET ADMIN_EMAILS TRUSTED_PROXIES)
# можно оставить пустыми; без .env спрашиваются, с .env (повторный запуск) отсутствие ключа значит «пусто»
OPTIONAL_KEYS=" TRUSTED_PROXIES "

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
usage() { sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; }

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
[ -z "$UPDATE_VERSION" ] || [[ "$UPDATE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "--update: нужна версия вида 0.2.0, получено: $UPDATE_VERSION"
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
    AIRGAP|FIREWALL|INSTALL_DOCKER)
      case "$v" in
        y|Y|yes|Yes|YES|д|Д|да|Да|ДА|true|1) v=yes ;;
        n|N|no|No|NO|н|Н|нет|Нет|НЕТ|false|0) v=no ;;
      esac ;;
    CERT_MODE) case "$v" in 1) v=internal ;; 2) v=own ;; esac ;;
    IMAGE_SOURCE) case "$v" in 1) v=pull ;; 2) v=build ;; 3) v=load ;; 4) v=keep ;; esac ;;
    CERTS_DIR) if [ -d "$v" ]; then v="$(cd "$v" && pwd)"; fi ;;
    IMAGE_TAR|GHCR_TOKEN_FILE) if [ -f "$v" ]; then v="$(cd "$(dirname "$v")" && pwd)/$(basename "$v")"; fi ;;
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
    OIDC_CLIENT_SECRET)
      [ -n "$v" ] || { echo "секрет не может быть пустым: его выдаёт владелец клиента в Keycloak sso-ng.nsu.ru (Clients -> Credentials)"; return 1; }
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
    AIRGAP|FIREWALL|INSTALL_DOCKER) [[ "$v" =~ ^(yes|no)$ ]] || { echo "ответьте «да» или «нет»"; return 1; } ;;
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
  esac
}

need() { # need KEY ВОПРОС ПО_УМОЛЧАНИЮ [-s]: значение из файлов (проверенное), иначе вопрос или значение по умолчанию
  local key="$1" prompt="$2" def="$3" secret="${4:-}" v err ans
  v="${ANS[$key]:-}"
  if [ -z "$v" ] && [[ "$OPTIONAL_KEYS" == *" $key "* ]] && { [ -n "${ANS[$key]+x}" ] || [ -f "$ENV_FILE" ] || [ "$YES" = 1 ]; }; then
    ANS[$key]=""; SRC[$key]="${SRC[$key]:-по умолчанию}"; return
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
      [ -z "$v" ] || printf '%s=%s\n' "$k" "$v"
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

pull_main_image() { # pull_main_image VERSION: образ главного с ghcr.io под именем nsu-llm-main:VERSION
  local ver="$1" ref
  ref="$(main_ref "$ver")"
  check_ghcr
  ghcr_login
  say "Загрузка образа главного $ref (около 190 МБ)"
  ghcr_docker pull "$ref" \
    || die "docker pull $ref не удался: образа нет (версия $ver не выпущена? GHCR_OWNER=${ANS[GHCR_OWNER]} указан неверно? пакет закрытый: нужен GHCR_TOKEN_FILE) или сеть до ghcr.io. Без сети: IMAGE_SOURCE=load"
  run docker tag "$ref" "nsu-llm-main:$ver"
}

ctr() { run docker exec "$CONTAINER" k3s ctr -n k8s.io "$@"; }

# registry_digest VERSION-VARIANT: digest манифеста в реестре главного (пусто: тега нет)
registry_digest() {
  docker exec "$CONTAINER" curl -fsSI -m 10 \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "http://127.0.0.1:$(registry_port)/v2/nsu-llm-node/manifests/$1" </dev/null 2>/dev/null \
    | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: *//p' | head -n1 || true
}
# release_digest VERSION VARIANT: digest образа ноды из release/<версия>.json (его пишет release.sh), пусто если файла нет
release_digest() {
  sed -n "s/.*\"node-$2\": {[^}]*\"digest\": \"\\(sha256:[0-9a-f]*\\)\".*/\\1/p" "${NSU_LLM_RELEASE_DIR:-$REPO/release}/$1.json" 2>/dev/null | head -n1 || true
}

# Реестр главного (под registry в k3s, том llm-main-registry). Образы нод попадают в него с 127.0.0.1:<порт> этого же контейнера:
# писать в реестр может только главный, класс читает через Caddy. Публичные образы пуллятся прямо в k3s главного (ctr, digest
# сохраняется). Закрытое зеркало (GHCR_TOKEN_FILE): образ скачивает docker хоста (вход в отдельный docker config, токен через stdin) и
# передаётся в k3s потоком docker save | ctr images import: токен не попадает ни в одну командную строку, ни внутрь контейнера.
# Версия, которая уже есть в реестре (и не расходится с release/<версия>.json), пропускается: повторный запуск ничего не качает.
seed_registry() { # seed_registry VERSION
  local ver="$1" repo port variant src dst attempts i have want todo=0
  repo="$(node_repo)"; port="$(registry_port)"
  say "Образы нод в реестр главного (127.0.0.1:$port внутри контейнера $CONTAINER; класс берёт их по https://${ANS[MAIN_ADVERTISE_IP]}/v2/):"
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
    src="$repo:$ver-$variant"
    dst="127.0.0.1:$port/nsu-llm-node:$ver-$variant"
    if [ "$DRY" = 0 ]; then
      have="$(registry_digest "$ver-$variant")"
      want="$(release_digest "$ver" "$variant")"
      if [ -n "$have" ] && { [ -z "$want" ] || [ "$have" = "$want" ]; }; then
        ok "в реестре уже есть nsu-llm-node:$ver-$variant ($have): не скачивается заново"
        continue
      fi
      [ -z "$have" ] || warn "nsu-llm-node:$ver-$variant в реестре ($have) расходится с release/$ver.json ($want): образ загружается заново"
    fi
    todo=1
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
    ctr images push --plain-http --platform linux/amd64 "$dst" || die "k3s ctr images push $dst не удался: реестр не принимает запись (журнал: docker exec $CONTAINER k3s kubectl -n llm logs deploy/registry)"
    if [ -n "${ANS[GHCR_TOKEN_FILE]:-}" ]; then ctr images rm --sync "$dst" >/dev/null || warn "не удалось убрать временный тег $dst из containerd главного"
    else ctr images rm --sync "$src" "$dst" >/dev/null || warn "не удалось убрать временные теги $src и $dst из containerd главного (на работу реестра не влияет)"; fi
  done
  if [ "$DRY" = 0 ]; then
    docker exec "$CONTAINER" curl -fsS -m 10 "http://127.0.0.1:$port/v2/nsu-llm-node/tags/list" </dev/null 2>/dev/null | grep -q "\"$ver-gpu\"" \
      || die "после загрузки в реестре нет nsu-llm-node:$ver-gpu (docker exec $CONTAINER curl -s http://127.0.0.1:$port/v2/nsu-llm-node/tags/list)"
    ok "реестр главного: nsu-llm-node:$ver-cpu и $ver-gpu"
    SEEDED=1
  fi
}

# --update VERSION: образ главного и образы нод новой версии; контейнер не перезапускается
do_update() {
  local ver="$UPDATE_VERSION" ref step=1 k
  say "Проверка хоста:"
  ok "ОС: ${OS[PRETTY_NAME]:-${OS[ID]:-неизвестна}}"
  check_docker
  [ "$EXISTING" = 1 ] || die "главный не установлен (контейнера $CONTAINER нет): --update обновляет существующую установку. Для новой запустите установщик без --update."
  need GHCR_OWNER "Владелец образов на GitHub (GHCR_OWNER; образы лежат в ghcr.io/<владелец>/nsu-llm-main)" ""
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
  say "Файлы $ENV_FILE и $CONFIG оставлены (в .env секрет NSU ID): удалите их, если установка больше не нужна."
}

# ------------------------------------------------------------------ основной сценарий
main() {
  local yn
  check_os
  if [ "$UNINSTALL" = 1 ]; then do_uninstall; return; fi
  load_answers
  if [ -n "$UPDATE_VERSION" ]; then do_update; return; fi
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
  need PUBLIC_HOSTNAME "Имя хоста портала (PUBLIC_HOSTNAME; на него NSU ID возвращает пользователя)" double.fresh.nsuts.ru
  need MAIN_ADVERTISE_IP "LAN-адрес главного для нод (MAIN_ADVERTISE_IP)" "$DETECTED_IP"
  need OIDC_CLIENT_SECRET "Секрет клиента NSU ID (OIDC_CLIENT_SECRET, ввод скрыт)" "" -s
  need ADMIN_EMAILS "Администраторы (e-mail через запятую, ADMIN_EMAILS)" ""
  need TRUSTED_PROXIES "Фронт-прокси перед главным, адреса через запятую (TRUSTED_PROXIES; например 10.4.0.105 — nginx на tsga; Enter — нет)" ""
  need CERT_MODE "Сертификат HTTPS: 1 — внутренний CA главного (internal), 2 — свой из каталога с tls.crt и tls.key (own)" internal
  if [ "${ANS[CERT_MODE]}" = own ]; then
    need CERTS_DIR "Каталог с tls.crt и tls.key (и необязательно ca.crt)" ""
  fi
  need AIRGAP "Режим без интернета: заранее положить системные образы k3s, Postgres, Redis и LiteLLM (airgap)? (да/нет)" нет
  if [ -n "$FW_TOOL" ]; then
    need FIREWALL "Открыть порты 80, 443, 6443/tcp в $FW_TOOL? (да/нет)" да
  fi
  local img_def=keep img_prompt
  if [ -f "$HERE/Dockerfile" ]; then img_def=build; fi
  img_prompt="Образ главного: 2 — собрать из исходников (build), 3 — загрузить архив docker save (load), 4 — взять имеющийся (keep)"
  # ghcr.io проверяется, только если способ ещё не задан: «с ghcr.io» (pull) предлагается первым, когда сеть его пропускает
  if [ -z "${ANS[IMAGE_SOURCE]:-}" ]; then
    if ghcr_reachable; then
      img_prompt="Образ главного: 1 — скачать с ghcr.io (pull), 2 — собрать из исходников (build), 3 — загрузить архив docker save (load), 4 — взять имеющийся (keep)"
      [ -z "${ANS[GHCR_OWNER]:-}" ] || img_def=pull
    else
      img_prompt="$img_prompt (скачивание с ghcr.io недоступно: ghcr.io с этого хоста не отвечает)"
    fi
  fi
  need IMAGE_SOURCE "$img_prompt" "$img_def"
  if [ "${ANS[IMAGE_SOURCE]}" = load ]; then
    need IMAGE_TAR "Путь к архиву образа (docker save)" ""
  fi
  if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then
    need GHCR_OWNER "Владелец образов на GitHub (GHCR_OWNER; образы лежат в ghcr.io/<владелец>/nsu-llm-main)" ""
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
  prepare_image
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] main/run-main.sh ${rm_args[*]} --dry-run:"
    "$BASH" "$RUN_MAIN" "${rm_args[@]}" --dry-run </dev/null
    if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then seed_registry "$CHECKOUT_VERSION"; fi
    return
  fi
  "$BASH" "$RUN_MAIN" "${rm_args[@]}" </dev/null || die "run-main.sh завершился с ошибкой (см. вывод выше); повторите установщик после исправления"
  wait_ready
  if [ "${ANS[IMAGE_SOURCE]}" = pull ]; then seed_registry "$CHECKOUT_VERSION"; fi
  if [ "$PENDING_USED" = 1 ]; then SEEDED=1; PENDING_DONE=1; write_config; fi   # образы нод --update уже положил в реестр
  print_result
}

main
