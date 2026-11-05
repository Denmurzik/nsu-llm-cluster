#!/usr/bin/env bash
# Starts (or removes) the NSU LLM node container on this Linux host.
# Usage: run-node.sh --main HOST --token TOKEN [options]      (see node/README.md)
set -euo pipefail

usage() {
  cat <<'EOF'
Использование:
  run-node.sh --main HOST (--token-file FILE | --token-stdin | CLUSTER_TOKEN=... в окружении) [--mode k3s|standalone] [--accel auto|cpu|nvidia]
              [--node-ip IP] [--name NAME] [--network bridge|host] [--image IMAGE]
              [--data-dir DIR] [--insecure-tls] [--from-main] [--dry-run]
              [--ca-sha256 HEX | --accept-ca] [--allow-from ADDR[,ADDR...]] [--no-firewall]
  run-node.sh --uninstall --main HOST --name NAME [--token-file FILE] [--network bridge|host] [--data-dir DIR]
  run-node.sh --update [--dry-run]
  Токен агента не передавайте в командной строке: её читают все локальные пользователи (/proc/<pid>/cmdline) и она остаётся в истории
  оболочки. Берите его из файла (--token-file FILE, права 0600; "-" или --token-stdin: со стандартного ввода) или из окружения
  (CLUSTER_TOKEN=... sudo -E ./run-node.sh ...). --token VALUE работает, как раньше, но печатает предупреждение.
  --ca-sha256  SHA-256 (hex, от DER) корня CA главного (страница «Подключение ноды»): скачанный https://HOST/ca.crt сверяется с ним. Без него
               печатается отпечаток, а корень принимается только по ответу «да» на вопрос (терминал) или с --accept-ca (доверие при
               первом обращении): иначе первый, кто ответил в сети, получил бы токен агента и закрепился корнем доверия ноды.
  --allow-from 18080 (инференс) открыт в iptables только главному; эти адреса (через запятую) добавляются. 18050 (RPC split, без
               аутентификации) открыт только главному и нодам текущей группы split (rpc_peers в ответе главного, обновляется
               по таймеру --update); локальным пользователям, кроме root, он закрыт. --no-firewall: не трогать iptables.
  --sync-firewall  один раз привести правила iptables к группе split, которую называет главный (параметры берутся у работающего контейнера);
               ставится таймером systemd, который создаёт run-node.sh. --restore-firewall: вернуть последний набор правил из state/fw.conf
               (без сети и Docker; юнит nsu-llm-node-firewall.service запускает его ДО docker.service при загрузке).
  --from-main  скачать образ ноды с главного (реестр https://HOST/v2/, node/pull-image.sh, нужен root) вместо docker load
  --update     спросить главного, не пора ли заменить контейнер llm-node: по запросу администратора («Обновить» на странице «Ноды»)
               или при NODE_AUTO_UPDATE на главном, и только когда нода ничего не обслуживает (если администратор не выбрал «сейчас»).
               Параметры (главный, токен, режим, адрес, сеть, каталог данных) берутся у работающего контейнера; если главный велит
               обновиться, образ скачивается с главного (pull-image.sh, нужен root) и контейнер создаётся заново теми же параметрами.
               Запускается раз в минуту по таймеру systemd или cron (docs/release.md, «Обновление нод»); ничего не делает, пока обновлять нечего.
EOF
}

MAIN=""
TOKEN="${CLUSTER_TOKEN:-}"
TOKEN_FILE=""
TOKEN_ARGV=0
MODE="k3s"
ACCEL="auto"
NODE_IP=""
NAME=""
NETWORK="bridge"
IMAGE=""
DATA_DIR="/var/lib/nsu-llm-node"
INSECURE=0
CA_SHA256=""
ACCEPT_CA=0
ALLOW_FROM=""
NO_FIREWALL=0
SYNC_ONLY=0
RESTORE_FW=0
FROM_MAIN=0
DRY=0
UPDATE=0
UNINSTALL=0
CONTAINER="llm-node"

die() { echo "Ошибка: $*" >&2; exit 1; }

# The only constant of the node image version in the scripts is node/IMAGE_VERSION (shared with install-node.ps1 and run-node.ps1,
# release.sh checks it): the fallback when main does not name the image (it names it itself, NODE_IMAGE_TAG, GET /api/node-image).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d ' \r\n' < "$HERE/IMAGE_VERSION" 2>/dev/null)"

while [ $# -gt 0 ]; do
  case "$1" in
    --main) MAIN="${2:?}"; shift 2 ;;
    --token) TOKEN="${2:?}"; TOKEN_ARGV=1; shift 2 ;;
    --token-file) TOKEN_FILE="${2:?}"; shift 2 ;;
    --token-stdin) TOKEN_FILE="-"; shift ;;
    --mode) MODE="${2:?}"; shift 2 ;;
    --accel) ACCEL="${2:?}"; shift 2 ;;
    --node-ip) NODE_IP="${2:?}"; shift 2 ;;
    --name) NAME="${2:?}"; shift 2 ;;
    --network) NETWORK="${2:?}"; shift 2 ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:?}"; shift 2 ;;
    --insecure-tls) INSECURE=1; shift ;;
    --ca-sha256) CA_SHA256="${2:?}"; shift 2 ;;
    --accept-ca) ACCEPT_CA=1; shift ;;
    --allow-from) ALLOW_FROM="${2:?}"; shift 2 ;;
    --no-firewall) NO_FIREWALL=1; shift ;;
    --sync-firewall) UPDATE=1; SYNC_ONLY=1; shift ;;
    --restore-firewall) RESTORE_FW=1; shift ;;
    --from-main) FROM_MAIN=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --update) UPDATE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done

CA_SHA256="$(printf '%s' "$CA_SHA256" | tr -d ': ' | tr 'A-F' 'a-f')"
[ -z "$CA_SHA256" ] || [[ "$CA_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "--ca-sha256: нужны 64 шестнадцатеричные цифры (SHA-256 корня CA главного)"
ALLOW_FROM="${ALLOW_FROM// /}"
[ -z "$ALLOW_FROM" ] || [[ "$ALLOW_FROM" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?(,[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?)*$ ]] || die "--allow-from: адреса IPv4 или подсети через запятую, например 10.0.0.5,192.168.3.0/24"

# The cluster token must not sit in argv (every local user reads /proc/<pid>/cmdline, shell history keeps it): the sources are
# --token-file (or - / --token-stdin for stdin), then CLUSTER_TOKEN of the environment; --token VALUE still works, with a warning.
if [ -n "$TOKEN_FILE" ]; then
  if [ "$TOKEN_FILE" = "-" ]; then IFS= read -r TOKEN || true
  else
    [ -r "$TOKEN_FILE" ] || die "--token-file: файл $TOKEN_FILE не читается"
    IFS= read -r TOKEN < "$TOKEN_FILE" || true
    if [ -n "$(find "$TOKEN_FILE" -maxdepth 0 -perm /077 2>/dev/null)" ]; then
      echo "Предупреждение: файл $TOKEN_FILE доступен другим пользователям: chmod 600 $TOKEN_FILE" >&2
    fi
  fi
  TOKEN="${TOKEN//[[:space:]]/}"
  [ -n "$TOKEN" ] || die "--token-file: токен пуст"
fi
if [ "$TOKEN_ARGV" = 1 ]; then
  echo "Предупреждение: --token VALUE виден всем локальным пользователям в списке процессов (/proc/<pid>/cmdline) и остаётся в истории оболочки. Передайте токен файлом (--token-file FILE, права 0600, или --token-stdin) либо переменной CLUSTER_TOKEN: CLUSTER_TOKEN=... sudo -E $0 ..." >&2
fi

# --update: every parameter of the run is the one the running container was started with (this script stays the only place that
# knows how to start it); the flags given on the command line win. No container: nothing to update.
declare -A CE=()
if [ "$UPDATE" = 1 ]; then
  [ "$UNINSTALL" = 0 ] || die "--update и --uninstall вместе не используются"
  docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "Контейнера $CONTAINER нет: обновлять нечего."; exit 0; }
  while IFS= read -r line; do
    line="${line%$'\r'}"
    case "$line" in *=*) CE["${line%%=*}"]="${line#*=}" ;; esac
  done < <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER")
  CUR_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
  [ -n "$MAIN" ] || MAIN="${CE[MAIN_HOST]:-}"
  [ -n "$TOKEN" ] || TOKEN="${CE[CLUSTER_TOKEN]:-}"
  [ -n "$NAME" ] || NAME="${CE[NODE_NAME]:-}"
  [ -n "$NODE_IP" ] || NODE_IP="${CE[NODE_IP]:-}"
  [ -z "${CE[MODE]:-}" ] || MODE="${CE[MODE]}"
  [ -z "${CE[ACCEL]:-}" ] || ACCEL="${CE[ACCEL]}"
  [ "${CE[MAIN_INSECURE_TLS]:-}" != 1 ] || INSECURE=1
  if [ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$CONTAINER")" = host ]; then NETWORK=host; else NETWORK=bridge; fi
  # the state directory is mounted at /var/lib/llm-node: the data directory is its parent
  state_src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/llm-node"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")"
  [ -z "$state_src" ] || DATA_DIR="$(dirname "$state_src")"
  for v in HF_ENDPOINT HF_TOKEN THREADS; do
    if [ -z "${!v:-}" ] && [ -n "${CE[$v]:-}" ]; then export "$v=${CE[$v]}"; fi
  done
  [ -n "$MAIN" ] && [ -n "$TOKEN" ] || die "--update: у контейнера $CONTAINER нет MAIN_HOST или CLUSTER_TOKEN (запущен не run-node.sh?)"
fi

[ "$RESTORE_FW" = 1 ] || [ -n "$MAIN" ] || { usage >&2; die "--main обязателен"; }
if [ "$UNINSTALL" = 0 ] && [ "$RESTORE_FW" = 0 ] && [ -z "$TOKEN" ]; then die "нужен токен агента: --token-file FILE, --token-stdin или переменная CLUSTER_TOKEN (--token VALUE показывает токен в списке процессов)"; fi
case "$MODE" in k3s|standalone) ;; *) die "--mode: k3s или standalone" ;; esac
case "$ACCEL" in auto|cpu|nvidia) ;; *) die "--accel: auto, cpu или nvidia" ;; esac
case "$NETWORK" in bridge|host) ;; *) die "--network: bridge или host" ;; esac

NAME="${NAME:-$(hostname)}"
NAME="${NAME,,}"
# Same rule as the portal (DNS-1123 label, nodes/routes.py): a name it rejects could never register or be deleted.
[ "$RESTORE_FW" = 1 ] || [[ "$NAME" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]] || die "недопустимое имя ноды: $NAME (латинские буквы, цифры и дефис, до 63 символов)"
MAIN_URL="https://$MAIN"

# run CMD...: print under --dry-run, execute otherwise
run() {
  if [ "$DRY" = 1 ]; then printf '%q ' "$@"; echo; else "$@"; fi
}

# The data directory is for root: state holds the node secret, airgap the archive that k3s imports at every start (a foreign archive there
# would replace the node image), so both are created 0700 and an older, more open directory is tightened.
mk_state() {
  ( umask 077; mkdir -p "$DATA_DIR/state" )
  chmod 700 "$DATA_DIR" "$DATA_DIR/state" 2>/dev/null || true
}

# Every call of the node to main carries the agent token (Authorization: Bearer) and, once the node container enrolled, the key main
# issued to this very name (X-Node-Key; the file state/node.key is written by registrar.sh, 0600, 43 chars of [A-Za-z0-9_-]).
# Without the file only a legacy node from its known address is accepted by main.
node_key() {
  local f="$DATA_DIR/state/node.key" k=""
  [ -r "$f" ] && k="$(head -n1 "$f" 2>/dev/null | tr -d ' \r\n')"
  [[ "$k" =~ ^[A-Za-z0-9_-]{43}$ ]] && printf '%s' "$k"
  return 0
}
auth_headers() {
  local k
  printf 'Authorization: Bearer %s\n' "$TOKEN"
  k="$(node_key)"
  [ -z "$k" ] || printf 'X-Node-Key: %s\n' "$k"
}
# the message of a refusal of main ({"detail":{"code":..,"message":".."}}) from a response file
api_message() { sed -n 's/.*"message" *: *"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -n1; }

# SHA-256 of the DER of a PEM certificate (the same value the «Подключение ноды» page shows)
ca_fingerprint() {
  command -v openssl >/dev/null 2>&1 || return 1
  openssl x509 -in "$1" -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1
}

# Download the portal CA once and pin it. The root is not taken on trust: it is checked against --ca-sha256; without it the
# fingerprint is printed and the root is accepted only with --accept-ca or a «yes» at the terminal (an unattended --update never
# downloads it: the first one to answer on the network would get the token).
fetch_ca() {
  [ "$DRY" = 1 ] && return 0
  [ "$INSECURE" = 1 ] && return 0
  [ -s "$DATA_DIR/state/ca.crt" ] && return 0
  [ "$UPDATE" = 1 ] && return 0
  mk_state
  local tmp="$DATA_DIR/state/ca.crt.tmp" fp="" ans="" helper="$HERE/rootfs/opt/llm/bin/fetch-main-ca.sh" rc=0
  # A token K10<sha256 of the CA of k3s>::... checks the root of main by itself (main signs /ca.crt with the CA of k3s, see
  # fetch-main-ca.sh): nobody has to be trusted on first use. An explicit --ca-sha256 wins; no K10 / no signature: the fingerprint below.
  if [ -z "$CA_SHA256" ] && [ -f "$helper" ] && [[ "$TOKEN" =~ ^K10[0-9a-f]{64}:: ]]; then
    CLUSTER_TOKEN="$TOKEN" bash "$helper" --main "$MAIN" --out "$tmp" || rc=$?
    case "$rc" in
      0) mv "$tmp" "$DATA_DIR/state/ca.crt"; return 0 ;;
      4) rm -f "$tmp"; return 0 ;;   # a public certificate of main: the system roots check it
      # with a K10 token there is NO fallback to the fingerprint question: a missing signature (404 on .p7s) can be a man in the middle
      *) rm -f "$tmp"; die "корень CA главного не прошёл проверку по токену K10 (подмена в сети, токен от другого кластера или главный ещё не подписал CA: повторите через минуту или обновите главный); корень не сохранён, токен не отправлялся" ;;
    esac
  fi
  if ! curl -sSf -k --connect-timeout 5 -m 15 -o "$tmp" "$MAIN_URL/ca.crt" 2>/dev/null; then rm -f "$tmp"; return 0; fi
  fp="$(ca_fingerprint "$tmp")" || fp=""
  if [ -n "$CA_SHA256" ]; then
    if [ "$fp" != "$CA_SHA256" ]; then
      rm -f "$tmp"
      die "корень CA, который отдал $MAIN_URL/ca.crt, не совпал с --ca-sha256 (получен ${fp:-не вычислен: нужен openssl}, ожидался $CA_SHA256): возможна подмена в сети (ARP, DNS) или CA главного сменили. Корень не сохранён, токен не отправлялся. Сверьте отпечаток со страницей «Подключение ноды»."
    fi
    echo "Корень CA главного совпал с --ca-sha256 ($fp)."
  else
    [ -n "$fp" ] || { rm -f "$tmp"; die "не удалось прочитать корень CA главного ($MAIN_URL/ca.crt не сертификат PEM или на узле нет openssl): отпечаток не вычислен, корень не сохранён"; }
    echo "Отпечаток корня CA главного (SHA-256): $fp"
    if [ "$ACCEPT_CA" = 1 ]; then
      echo "Предупреждение: --ca-sha256 не задан, корень принят по --accept-ca без сверки (доверие при первом обращении). Сверьте отпечаток со страницей «Подключение ноды»." >&2
    elif [ -t 0 ] && [ -t 1 ]; then
      read -r -p "Сверьте отпечаток со страницей «Подключение ноды» портала. Доверять этому корню? [y/N] " ans || ans=""
      case "$ans" in y|Y|yes|YES|д|Д|да|Да) ;; *) rm -f "$tmp"; die "корень CA главного не принят" ;; esac
    else
      rm -f "$tmp"
      die "корень CA главного ($fp) не подтверждён: нет --ca-sha256 и некому ответить на вопрос. Передайте отпечаток со страницы «Подключение ноды» (--ca-sha256) или, если сеть доверенная, --accept-ca."
    fi
  fi
  mv "$tmp" "$DATA_DIR/state/ca.crt"
}

tls_args() {
  if [ "$INSECURE" = 1 ]; then echo "-k"
  elif [ -s "$DATA_DIR/state/ca.crt" ]; then echo "--cacert $DATA_DIR/state/ca.crt"
  fi
}

# ------------------------------------------------------------------ iptables: 18080 only main, 18050 main and the nodes of the split group
# 18050 is ggml-rpc-server of a split group and has no authentication: whoever reaches it changes the weights and reads the KV cache of
# the group. So the node opens it only to main and to the nodes of its current split group (rpc_peers of main's «desired» answer, kept
# fresh by --sync-firewall / --update), and 18080 (inference) only to main (+ --allow-from). Two chains of our own, hooked where the traffic
# of a published port goes (INPUT: host network and standalone; FORWARD and DOCKER-USER: bridge, hostPort pods): a packet is matched by
# the port it was sent to (conntrack --ctorigdstport), so the match works before and after Docker's DNAT. Local traffic (lo) passes: the
# leader of a group talks to its own rpc server over 127.0.0.1. Local users other than root cannot open 18050 at all (OUTPUT, owner match):
# on the Linux host any student with an account would otherwise reach it through the node's own address.
#
# Never «open to all»: a chain is replaced in ONE iptables-restore --noflush transaction (the chain is declared, so its old rules go and the
# new ones, ending with DROP, come at the same COMMIT); a failed transaction changes nothing, the previous set stays. The chain is hooked only
# after it holds its rules. The last applied set is saved in state/fw.conf and restored by a unit that runs BEFORE docker.service at boot
# (--restore-firewall), so a reboot does not leave the published ports open until some timer fires.
FW_RPC=NSU-LLM-RPC
FW_INFER=NSU-LLM-INFER
FW_CONF_NAME=fw.conf
# Match of the packets that arrive for a published port. INPUT: connections to this host (host network, standalone). FORWARD and
# DOCKER-USER only the DNAT'ed ones (Docker's -p, the CNI portmap of a hostPort pod): what a pod or the node container sends out to
# another node's 18050 is not DNAT'ed and must pass, or the leader of a split group could not reach its workers.
fw_match() { # $1 = hook, $2 = port
  if [ "$1" = INPUT ]; then printf '%s\n' -p tcp -m conntrack --ctorigdstport "$2" --ctdir ORIGINAL
  else printf '%s\n' -p tcp -m conntrack --ctstate DNAT --ctorigdstport "$2" --ctdir ORIGINAL; fi
}
# $1 = port, $2 = chain to jump to, hooked in INPUT, FORWARD and (if it exists) DOCKER-USER, once
fw_hook() {
  local port="$1" target="$2" hook
  local -a m
  for hook in INPUT FORWARD DOCKER-USER; do
    mapfile -t m < <(fw_match "$hook" "$port")
    if [ "$DRY" = 1 ]; then printf '%q ' iptables -w -I "$hook" 1 "${m[@]}" -j "$target"; echo; continue; fi
    [ "$hook" = DOCKER-USER ] && ! iptables -w -n -L DOCKER-USER >/dev/null 2>&1 && continue
    iptables -w -C "$hook" "${m[@]}" -j "$target" 2>/dev/null || iptables -w -I "$hook" 1 "${m[@]}" -j "$target" || return 1
  done
}
fw_unhook() {
  local port="$1" target="$2" hook
  local -a m
  for hook in INPUT FORWARD DOCKER-USER; do
    mapfile -t m < <(fw_match "$hook" "$port")
    if [ "$DRY" = 1 ]; then printf '%q ' iptables -w -D "$hook" "${m[@]}" -j "$target"; echo; continue; fi
    while iptables -w -D "$hook" "${m[@]}" -j "$target" 2>/dev/null; do :; done
  done
}
# $1 = chain, rest = allowed sources. The whole replacement is one transaction (see above): no moment with an empty chain.
fw_chain() {
  local chain="$1"; shift
  local body src
  body="*filter"$'\n'":$chain - [0:0]"$'\n'"-A $chain -i lo -j RETURN"$'\n'
  for src in "$@"; do body+="-A $chain -s $src -j RETURN"$'\n'; done
  body+="-A $chain -j DROP"$'\n'"COMMIT"$'\n'
  if [ "$DRY" = 1 ]; then printf 'iptables-restore --noflush <<EOF\n%sEOF\n' "$body"; return 0; fi
  printf '%s' "$body" | iptables-restore --noflush
}
# IPv4 of main: the address itself or the first A record
main_ipv4() {
  local m="${MAIN%%:*}"
  if [[ "$m" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then printf '%s' "$m"; return 0; fi
  { getent ahostsv4 "$m" 2>/dev/null || true; } | awk 'NR == 1 { print $1 }'
}
# Core: $1 = comma list for 18080, $2 = comma list for 18050 (k3s only). Status 1 on any failure (the previous set stays).
apply_firewall_lists() {
  local infer="$1" rpc="$2"
  local -a a=()
  IFS=',' read -r -a a <<<"$infer"
  fw_chain "$FW_INFER" "${a[@]}" || return 1
  fw_hook 18080 "$FW_INFER" || return 1
  if [ "$MODE" = k3s ]; then
    IFS=',' read -r -a a <<<"$rpc"
    fw_chain "$FW_RPC" "${a[@]}" || return 1
    fw_hook 18050 "$FW_RPC" || return 1
    if [ "$DRY" = 1 ]; then
      printf '%q ' iptables -w -I OUTPUT 1 -p tcp --dport 18050 -m owner ! --uid-owner 0 -j REJECT; echo
    else
      iptables -w -C OUTPUT -p tcp --dport 18050 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null \
        || iptables -w -I OUTPUT 1 -p tcp --dport 18050 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null \
        || echo "Предупреждение: iptables не принял правило owner (модуль xt_owner): локальные пользователи не закрыты от 18050." >&2
    fi
  else
    [ "$DRY" = 1 ] || remove_rpc_firewall
  fi
  if [ "$DRY" = 0 ]; then
    # the set is remembered for the boot unit (no DNS, no network, no docker there)
    ( umask 077; mkdir -p "$DATA_DIR/state"
      printf 'MODE=%s\nINFER=%s\nRPC=%s\n' "$MODE" "$infer" "$rpc" > "$DATA_DIR/state/$FW_CONF_NAME.tmp" && mv -f "$DATA_DIR/state/$FW_CONF_NAME.tmp" "$DATA_DIR/state/$FW_CONF_NAME" ) || true
  fi
  return 0
}
# $1 = rpc_peers of main's answer (comma separated, may be empty). Status 1: not applied (and the previous rules stay).
sync_firewall() {
  [ "$NO_FIREWALL" = 1 ] && return 0
  if [ "$DRY" = 0 ] && ! { command -v iptables >/dev/null 2>&1 && command -v iptables-restore >/dev/null 2>&1; }; then
    echo "Ошибка: iptables или iptables-restore не найден: порты 18080 и 18050 этой ноды нечем закрыть. Поставьте iptables, закройте порты своим брандмауэром и запустите с --no-firewall (docs/deploy-class.md, «Безопасность split-режима»)." >&2
    return 1
  fi
  local peers="$1" mip p
  local -a infer=() rpc=() a=()
  mip="$(main_ipv4)"
  [ -z "$mip" ] || { infer+=("$mip"); rpc+=("$mip"); }
  if [ -n "$ALLOW_FROM" ]; then IFS=',' read -r -a a <<<"$ALLOW_FROM"; infer+=("${a[@]}"); fi
  if [ -n "$peers" ]; then
    IFS=',' read -r -a a <<<"$peers"
    for p in "${a[@]}"; do [[ "$p" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && rpc+=("$p"); done
  fi
  if [ ${#infer[@]} -eq 0 ]; then
    echo "Ошибка: не удалось определить IPv4 главного ($MAIN): правила брандмауэра не заданы (укажите --allow-from <адрес главного>)." >&2
    return 1
  fi
  local il rl
  il="$(IFS=,; echo "${infer[*]}")"; rl="$(IFS=,; echo "${rpc[*]:-}")"
  apply_firewall_lists "$il" "$rl" || { echo "Ошибка: правила iptables не применены (предыдущий набор остаётся)." >&2; return 1; }
}
# Boot: the last applied set from state/fw.conf, before docker.service starts the node (no network needed)
restore_firewall() {
  local f="$DATA_DIR/state/$FW_CONF_NAME" k v MODE_SAVED="" INFER="" RPC=""
  [ -r "$f" ] || { echo "Нет $f: нечего восстанавливать."; return 0; }
  while IFS='=' read -r k v; do
    case "$k" in MODE) MODE_SAVED="$v" ;; INFER) INFER="$v" ;; RPC) RPC="$v" ;; esac
  done < "$f"
  [[ "$INFER" =~ ^[0-9./,]+$ ]] && [[ "$RPC" =~ ^[0-9./,]*$ ]] && [[ "$MODE_SAVED" =~ ^(k3s|standalone)$ ]] || die "$f повреждён"
  MODE="$MODE_SAVED"
  apply_firewall_lists "$INFER" "$RPC"
}
remove_rpc_firewall() {
  [ "$NO_FIREWALL" = 1 ] && return 0
  [ "$DRY" = 1 ] || command -v iptables >/dev/null 2>&1 || return 0
  fw_unhook 18050 "$FW_RPC"
  if [ "$DRY" = 1 ]; then
    printf '%q ' iptables -w -D OUTPUT -p tcp --dport 18050 -m owner ! --uid-owner 0 -j REJECT; echo
    printf '%q ' iptables -w -F "$FW_RPC"; echo; printf '%q ' iptables -w -X "$FW_RPC"; echo
    return 0
  fi
  while iptables -w -D OUTPUT -p tcp --dport 18050 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null; do :; done
  iptables -w -F "$FW_RPC" 2>/dev/null && iptables -w -X "$FW_RPC" 2>/dev/null || true
}
remove_firewall() {
  [ "$NO_FIREWALL" = 1 ] && return 0
  [ "$DRY" = 1 ] || command -v iptables >/dev/null 2>&1 || return 0
  remove_rpc_firewall
  fw_unhook 18080 "$FW_INFER"
  if [ "$DRY" = 1 ]; then printf '%q ' iptables -w -F "$FW_INFER"; echo; printf '%q ' iptables -w -X "$FW_INFER"; echo
  else iptables -w -F "$FW_INFER" 2>/dev/null && iptables -w -X "$FW_INFER" 2>/dev/null || true; fi
}
# Units that keep the rules across reboots and fresh: one before docker.service (restore the saved set), one timer (new set from main).
SYSTEMD_DIR="${NSU_LLM_SYSTEMD_DIR:-/etc/systemd/system}"   # NSU_LLM_SYSTEMD_DIR: tests only
install_firewall_units() {
  [ "$NO_FIREWALL" = 1 ] && return 0
  local self; self="$(readlink -f "${BASH_SOURCE[0]}")"
  case "$self$DATA_DIR" in
    *[[:space:]]*|*\"*|*\'*|*\\*|*%*|*\$*)
      echo "Предупреждение: в пути ($self, $DATA_DIR) пробелы или кавычки: юниты systemd для восстановления правил после перезагрузки не созданы. Запускайте --restore-firewall до docker.service сами." >&2
      return 0 ;;
  esac
  if [ "$DRY" = 1 ]; then echo "systemd: $SYSTEMD_DIR/nsu-llm-node-firewall.service (Before=docker.service: $self --restore-firewall), nsu-llm-node-sync.timer (раз в минуту: --sync-firewall)"; return 0; fi
  if ! command -v systemctl >/dev/null 2>&1 || [ "$(id -u)" != 0 ]; then
    echo "Предупреждение: нет systemd или прав root: правила iptables не вернутся после перезагрузки сами. Запускайте $self --restore-firewall --data-dir $DATA_DIR до docker.service и --sync-firewall раз в минуту (docs/deploy-class.md)." >&2
    return 0
  fi
  if ! { mkdir -p "$SYSTEMD_DIR" 2>/dev/null && [ -w "$SYSTEMD_DIR" ]; }; then
    echo "Предупреждение: в $SYSTEMD_DIR писать нельзя: юниты systemd для возврата правил после перезагрузки не созданы." >&2
    return 0
  fi
  cat > "$SYSTEMD_DIR/nsu-llm-node-firewall.service" <<EOF
[Unit]
Description=NSU LLM node: firewall of ports 18080/18050 before Docker starts the node
Before=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$self --restore-firewall --data-dir $DATA_DIR
[Install]
WantedBy=multi-user.target
EOF
  cat > "$SYSTEMD_DIR/nsu-llm-node-sync.service" <<EOF
[Unit]
Description=NSU LLM node: firewall follows the split group named by main
After=docker.service
[Service]
Type=oneshot
ExecStart=$self --sync-firewall
EOF
  cat > "$SYSTEMD_DIR/nsu-llm-node-sync.timer" <<EOF
[Unit]
Description=NSU LLM node: firewall sync every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable nsu-llm-node-firewall.service nsu-llm-node-sync.timer >/dev/null 2>&1 || true
  systemctl start nsu-llm-node-sync.timer >/dev/null 2>&1 || true
}
remove_firewall_units() {
  [ "$DRY" = 1 ] && { echo "systemd: убрать nsu-llm-node-firewall.service, nsu-llm-node-sync.service/.timer"; return 0; }
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now nsu-llm-node-sync.timer >/dev/null 2>&1 || true
    systemctl disable nsu-llm-node-firewall.service >/dev/null 2>&1 || true
  fi
  rm -f "$SYSTEMD_DIR/nsu-llm-node-firewall.service" "$SYSTEMD_DIR/nsu-llm-node-sync.service" "$SYSTEMD_DIR/nsu-llm-node-sync.timer" 2>/dev/null || true
  command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload >/dev/null 2>&1 || true
}
# rpc_peers of a `format=lines` answer; nothing printed and status 1 when main did not name the field (an older main: rules stay)
plan_peers() {
  local line found=1
  while IFS= read -r line; do
    line="${line%$'\r'}"
    case "$line" in rpc_peers=*) printf '%s' "${line#rpc_peers=}"; found=0 ;; esac
  done <<<"$1"
  return $found
}
# pull-image.sh takes /ca.crt of main WITHOUT a check (curl -k) when nothing is pinned and the system roots do not accept the certificate: an
# attacker on the network would get a pinned root and a node image with root rights. Without a pinned root the system roots must accept it.
require_registry_trust() {
  [ "$DRY" = 1 ] && return 0
  [ -s "$DATA_DIR/state/ca.crt" ] && return 0
  [ -s "/etc/docker/certs.d/$MAIN/ca.crt" ] && return 0
  local c
  c="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "https://$MAIN/v2/" 2>/dev/null || true)"
  case "$c" in 000|'') die "реестр https://$MAIN/v2/ не принят системными корнями, а корень главного не закреплён: pull-image.sh принял бы чужой корень без проверки. Образ не качается; закрепите корень проверенным токеном K10 или --ca-sha256 (install: run-node.sh --from-main с токеном K10…) либо используйте docker load." ;; esac
}

if [ "$RESTORE_FW" = 1 ]; then
  MODE="k3s"
  restore_firewall
  exit $?
fi

# ------------------------------------------------------------------ update (the panel's «Обновить», NODE_AUTO_UPDATE)
# The failure of an update goes to the portal (the page shows it and main waits before it asks again); the text is fixed, so it is
# safe inside JSON.
report_update_failure() {
  local msg="$1"
  [ "$DRY" = 1 ] && return 0
  # shellcheck disable=SC2046
  curl -sS -o /dev/null -m 15 $(tls_args) -X POST -H 'Content-Type: application/json' \
    -H @<(auth_headers) \
    -d "{\"version\":\"\",\"revision\":\"\",\"digest\":\"\",\"tag\":\"\",\"update_error\":\"$msg\"}" \
    "$MAIN_URL/api/nodes/$NAME/image" 2>/dev/null || true
}

if [ "$UPDATE" = 1 ]; then
  if [ "$DRY" = 0 ] && command -v flock >/dev/null 2>&1; then  # a timer must not start a second update over a long pull
    mk_state
    exec 9>"$DATA_DIR/state/update.lock"
    flock -n 9 || { echo "Другое обновление ещё идёт."; exit 0; }
  fi
  fetch_ca
  # shellcheck disable=SC2046
  if ! plan="$(curl -sSf -m 15 $(tls_args) -H @<(auth_headers) \
      "$MAIN_URL/api/nodes/$NAME/desired?accel=$ACCEL&format=lines" 2>&1)"; then
    echo "Главный не ответил на вопрос об обновлении (${plan:0:200}): ничего не меняю."
    exit 0
  fi
  p_go="" p_image="" p_digest="" p_mode="" p_when="" p_reason=""
  # the firewall follows the split group named by main (rpc_peers), also when there is nothing to update
  FW_FAILED=0
  if peers="$(plan_peers "$plan")"; then sync_firewall "$peers" || FW_FAILED=1; fi
  if [ "$SYNC_ONLY" = 1 ]; then
    [ "$FW_FAILED" = 0 ] || exit 1
    exit 0
  fi
  while IFS='=' read -r k v; do
    v="${v%$'\r'}"
    case "$k" in go) p_go="$v" ;; image) p_image="$v" ;; digest) p_digest="$v" ;; mode) p_mode="$v" ;; when) p_when="$v" ;; reason) p_reason="$v" ;; esac
  done <<<"$plan"
  if [ "$p_go" != 1 ]; then
    echo "Обновление не требуется${p_reason:+ ($p_reason)}."
    exit 0
  fi
  [[ "$p_image" =~ ^nsu-llm-node:[A-Za-z0-9._-]+$ ]] || die "главный назвал недопустимый образ: ${p_image:0:80}"
  [[ "$p_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "главный назвал недопустимый digest: ${p_digest:0:80}"
  if [ "${CE[NODE_IMAGE_DIGEST]:-}" = "$p_digest" ]; then
    echo "Контейнер уже работает на образе $p_digest: главный узнает об этом при его запуске."
    exit 0
  fi
  echo "Обновление ноды $NAME до $p_image ($p_digest), запрос: ${p_mode:-?}${p_when:+, $p_when}."
  [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "--update: Docker доверяет реестру главного через /etc/docker/certs.d, нужны права root: sudo $0 --update"
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [ -f "$HERE/pull-image.sh" ] || die "нет $HERE/pull-image.sh: скопируйте каталог node целиком"
  ca_args=()
  [ ! -s "$DATA_DIR/state/ca.crt" ] || ca_args=(--ca "$DATA_DIR/state/ca.crt")
  require_registry_trust
  if ! run bash "$HERE/pull-image.sh" --main "$MAIN" --image "$p_image" --digest "$p_digest" --no-tofu ${ca_args[@]+"${ca_args[@]}"} --record "$DATA_DIR/state/image.digest"; then
    report_update_failure "образ $p_image не удалось скачать с главного (pull-image.sh завершился с ошибкой), контейнер не тронут"
    die "образ $p_image не удалось скачать с главного (см. сообщение выше), контейнер не тронут"
  fi
  IMAGE="$p_image"
  FROM_MAIN=0
  # from here the container is replaced: any failure below is reported (the old container is already gone)
  trap 'rc=$?; [ "$rc" = 0 ] || report_update_failure "контейнер не удалось создать заново (run-node.sh --update завершился с кодом $rc), проверьте docker на ноде"' EXIT
fi

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
      body="$(mktemp)"
      code="$(curl -X DELETE -sS -o "$body" -w '%{http_code}' -m 15 $(tls_args) \
        -H @<(auth_headers) "$url" 2>&1)" || true
      msg="$(api_message "$body")"; rm -f "$body"
      # 204: removed, 404: already gone. Anything else (no connection, 401, 5xx) leaves the node object behind.
      case "$code" in
        204|404) ;;
        403|409) echo "Предупреждение: DELETE $url отклонён (ответ: $code)${msg:+: $msg}. Удалите ноду на странице «Ноды» портала (меню строки, «Удалить из кластера…») или на главном сервере (docker exec llm-main k3s kubectl delete node $NAME)." >&2 ;;
        401) echo "Предупреждение: DELETE $url не выполнен (ответ: 401). Главный не принял токен: он мог устареть после переустановки главного или смены токена агента. Повторите с актуальным токеном (--token-file, страница «Подключение ноды») или удалите ноду на странице «Ноды» портала (меню строки, «Удалить из кластера…») или на главном сервере (docker exec llm-main k3s kubectl delete node $NAME)." >&2 ;;
        *) echo "Предупреждение: DELETE $url не выполнен (ответ: ${code:-нет}). Нода останется в кластере: удалите её на странице «Ноды» портала (меню строки, «Удалить из кластера…») или на главном сервере (docker exec llm-main k3s kubectl delete node $NAME)." >&2 ;;
      esac
    fi
  fi
  remove_firewall_units
  remove_firewall
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
# The tag of the node image is the NODE_IMAGE_TAG of main (the portal names the image in its answer to «desired»), not a constant of
# this script: they used to differ (the script said 0.1.0 while main ran 0.1.1) and the node looked for an image nobody had pulled.
# Without --image: ask main; no answer (dry run, older main, no network): the version of this script.
image_from_main() {
  [ "$DRY" = 1 ] && return 0
  local plan name
  fetch_ca
  # shellcheck disable=SC2046
  plan="$(curl -sSf -m 10 $(tls_args) -H @<(auth_headers) \
    "$MAIN_URL/api/nodes/$NAME/desired?accel=$1&format=lines" 2>/dev/null)" || return 0
  name="$(printf '%s\n' "$plan" | tr -d '\r' | sed -n 's/^name=//p' | head -n1)"
  [[ "$name" =~ ^nsu-llm-node:[A-Za-z0-9._-]+$ ]] && printf '%s' "$name"
  return 0
}
# the root of main is pinned (and checked) in this shell: a failed check must stop the run, not a subshell
fetch_ca
# digest of the image main names for this flavour (GET /api/node-image, field digest): the first pull goes by it, not by a tag
digest_from_main() {
  [ "$DRY" = 1 ] && return 0
  local j
  j="$(curl -sSf -m 10 $(tls_args) "$MAIN_URL/api/node-image" 2>/dev/null | tr -d '\n')" || return 0
  printf '%s' "$j" | sed -n "s/.*\"digest\" *: *{[^}]*\"$1\" *: *\"\(sha256:[0-9a-f]\{64\}\)\".*/\1/p"
}
IMAGE_AUTO=0
if [ -z "$IMAGE" ]; then
  IMAGE="$(image_from_main "$([ "$ACCEL" = nvidia ] && echo nvidia || echo cpu)")"
  if [ -z "$IMAGE" ]; then
    # the fallback is needed only here: --uninstall, --update and --image never read the file
    [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "главный не назвал образ ноды, а в $HERE/IMAGE_VERSION нет версии вида 1.2.3: скопируйте каталог node целиком или укажите --image"
    IMAGE="nsu-llm-node:$VERSION-$([ "$ACCEL" = nvidia ] && echo gpu || echo cpu)"
  else
    IMAGE_AUTO=1
  fi
fi
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
    mk_state
    fetch_ca
    [ ! -s "$DATA_DIR/state/ca.crt" ] || ca_args=(--ca "$DATA_DIR/state/ca.crt")
  fi
  require_registry_trust
  dg_args=()
  if [ "$IMAGE_AUTO" = 1 ]; then
    dg="$(digest_from_main "$([ "$ACCEL" = nvidia ] && echo nvidia || echo cpu)")"
    [ -z "$dg" ] || dg_args=(--digest "$dg")
  fi
  run bash "$HERE/pull-image.sh" --main "$MAIN" --image "$IMAGE" --no-tofu ${dg_args[@]+"${dg_args[@]}"} ${ca_args[@]+"${ca_args[@]}"} || die "образ $IMAGE не удалось скачать с главного (см. сообщение выше); запасной путь: docker load -i nsu-llm-node-*.tar.gz без --from-main"
fi
if [ "$DRY" = 0 ]; then
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "образ $IMAGE не найден (--from-main, docker load или docker build)"
  mk_state
  ( umask 077; mkdir -p "$DATA_DIR/airgap" )
  chmod 700 "$DATA_DIR/airgap" 2>/dev/null || true
  # k3s imports every archive of this directory: nothing but what this script writes belongs there
  find "$DATA_DIR/airgap" -mindepth 1 -maxdepth 1 ! -name node.tar ! -name node.id -exec rm -rf {} + 2>/dev/null || true
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
  # 18050 (ggml-rpc-server of split deployments) has no authentication: only a k3s node needs it published, only on the node's own
  # address (not 127.0.0.1: any local user would reach it), and sync_firewall below lets only main and the group in (docs/deploy-class.md).
  args+=(-p 18080:18080)
  [ "$MODE" = k3s ] && args+=(-p "$NODE_IP:18050:18050")
fi
[ "$ACCEL" = nvidia ] && args+=(--gpus all)
# The token reaches docker through the environment (-e NAME), not through the docker command line.
export CLUSTER_TOKEN="$TOKEN"
args+=(-e "MODE=$MODE" -e "MAIN_HOST=$MAIN" -e CLUSTER_TOKEN -e "NODE_NAME=$NAME"
  -e "NODE_IP=$NODE_IP" -e "ACCEL=$ACCEL")
[ "$INSECURE" = 1 ] && args+=(-e MAIN_INSECURE_TLS=1)
# What this container runs, for the «Версии» of the admin pages: the local name of the image and the digest main's registry gave it
# (pull-image.sh --record). The record counts only while its image id is the id of this very image: an image loaded from an archive
# or pulled again does not inherit it.
args+=(-e "NODE_IMAGE=$IMAGE")
if [ "$DRY" = 0 ]; then
  rec_id="" rec_digest=""
  read -r rec_id rec_digest <"$DATA_DIR/state/image.digest" 2>/dev/null || true
  if [ -n "$rec_digest" ] && [ "$rec_id" = "$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null)" ] && [[ "$rec_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    args+=(-e "NODE_IMAGE_DIGEST=$rec_digest")
  fi
fi
# Memory Docker may give the node (inside WSL: the memory of WSL), the label llm.nsu/ram-mib for split placement.
# Not in state/labels: a changed WSL size must not stop the node; the portal never trusts it above the kubelet's value.
if [ -z "${RAM_MIB:-}" ] && [ "$DRY" = 0 ]; then
  RAM_MIB="$(docker info --format '{{.MemTotal}}' 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {print int($1 / 1048576)}' || true)"
fi
[ -n "${RAM_MIB:-}" ] && args+=(-e "RAM_MIB=$RAM_MIB")
for v in HF_ENDPOINT HF_TOKEN THREADS MODELS_REPORT_S; do
  [ -n "${!v:-}" ] && args+=(-e "$v")
done
args+=("$IMAGE")

# The firewall goes up BEFORE the container is started (the published ports are never open without it): 18080 only for main; on 18050 main
# and the nodes of the split group main names (none known at the first start). A set that cannot be applied stops the start (fail closed).
peers=""
if [ "$DRY" = 0 ]; then
  # shellcheck disable=SC2046
  plan="$(curl -sSf -m 10 $(tls_args) -H @<(auth_headers) \
    "$MAIN_URL/api/nodes/$NAME/desired?accel=$ACCEL&format=lines" 2>/dev/null)" && peers="$(plan_peers "$plan")" || peers=""
fi
if ! sync_firewall "$peers"; then
  # a first start with a set that cannot be applied does not happen (fail closed); an update of a node whose rules are already in place, and a dry
  # run, only warn: the previous set stays
  if [ "$UPDATE" = 1 ] || [ "$DRY" = 1 ]; then echo "Предупреждение: правила брандмауэра не обновлены, остаются прежние." >&2
  else die "брандмауэр не применён: контейнер не запущен (порты 18080 и 18050 остались бы открытыми). Исправьте причину выше или запустите с --no-firewall, если порты закрыты вашим брандмауэром."; fi
fi
install_firewall_units
run "${args[@]}"
if [ "$DRY" = 0 ]; then
  echo "Нода $NAME запущена: контейнер $CONTAINER, образ $IMAGE, режим $MODE, адрес $NODE_IP."
fi
