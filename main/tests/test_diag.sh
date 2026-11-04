#!/usr/bin/env bash
# Offline tests for main/diag.sh: every external command (docker, ss, timeout, getent, curl, ip, ufw) is a stub in PATH.
# No docker, no network, no real ports.
set -uo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$(cd "$HERE/.." && pwd)"
DIAG="${DIAG:-$MAIN/diag.sh}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
has() { # has NAME HAYSTACK NEEDLE
  case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (нет: $3)" "$2" ;; esac
}
lacks() {
  case "$2" in *"$3"*) fail "$1 (лишнее: $3)" "$2" ;; *) pass "$1" ;; esac
}
eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (ожидалось $3, получено $2)"; fi; }

bash -n "$DIAG" 2>"$TMP/syn" && pass "bash -n diag.sh" || fail "bash -n diag.sh" "$(cat "$TMP/syn")"

# ------------------------------------------------------------------ заглушки внешних команд
BIN="$TMP/bin"; mkdir -p "$BIN"
cat >"$BIN/timeout" <<'EOS'
#!/usr/bin/env bash
# timeout SEC bash -c SCRIPT HOST PORT: состояние из $FAKE_TCP (строки "host:port open|refused|blocked", по умолчанию blocked)
host="$5"; port="$6"
echo "$host:$port" >>"${FAKE_TCP_LOG:-/dev/null}"
st="$(awk -v k="$host:$port" '$1 == k {print $2}' "$FAKE_TCP" 2>/dev/null | head -n1)"
case "${st:-blocked}" in
  open) exit 0 ;;
  refused) echo "bash: connect: Connection refused" >&2; exit 1 ;;
  *) exit 124 ;;
esac
EOS
cat >"$BIN/ss" <<'EOS'
#!/usr/bin/env bash
for p in $FAKE_LISTEN; do echo "LISTEN 0 4096 0.0.0.0:$p 0.0.0.0:*"; done
EOS
cat >"$BIN/getent" <<'EOS'
#!/usr/bin/env bash
# getent ahostsv4 NAME: строки "name ip" из $FAKE_DNS
ip="$(awk -v k="$2" '$1 == k {print $2}' "$FAKE_DNS" 2>/dev/null | head -n1)"
[ -n "$ip" ] || exit 2
echo "$ip STREAM $2"
EOS
cat >"$BIN/ip" <<'EOS'
#!/usr/bin/env bash
echo "1.1.1.1 via 10.0.0.1 dev eth0 src ${FAKE_SRC_IP:-10.0.0.5} uid 0"
EOS
cat >"$BIN/curl" <<'EOS'
#!/usr/bin/env bash
# curl ... URL: код ответа из $FAKE_HTTP_CODE; без -k и при FAKE_TRUST=0 код возврата 60 (сертификат не доверенный)
insecure=0; for a in "$@"; do case "$a" in -k|-[a-zA-Z]*k*) insecure=1 ;; esac; done
echo "$*" >>"${FAKE_CURL_LOG:-/dev/null}"
if [ "$insecure" = 0 ] && [ "${FAKE_TRUST:-1}" = 0 ]; then exit 60; fi
printf '%s' "${FAKE_HTTP_CODE:-200}"
EOS
cat >"$BIN/docker" <<'EOS'
#!/usr/bin/env bash
echo "$*" >>"${FAKE_DOCKER_LOG:-/dev/null}"
if [ -n "${FAKE_DOCKER_DENIED:-}" ]; then  # docker есть, к сокету демона нет прав: так отвечает любая команда
  echo "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock" >&2; exit 1
fi
case "$1" in
  inspect)
    case "$*" in
      *State.Running*) echo "${FAKE_RUNNING:-true}"; [ "${FAKE_RUNNING:-true}" != missing ] || exit 1 ;;
      *Config.Env*) [ -z "${FAKE_CONTAINER_ENV:-}" ] || printf '%s\n' $FAKE_CONTAINER_ENV ;;
    esac ;;
  exec)
    case "$*" in
      *"get nodes"*) [ -f "${FAKE_NODES:-/nonexistent}" ] && cat "$FAKE_NODES" ;;
      *"-n kube-system get pods"*) [ -f "${FAKE_PODS:-/nonexistent}" ] && cat "$FAKE_PODS" ;;
    esac ;;
  info) exit 0 ;;
esac
EOS
chmod +x "$BIN"/*

export FAKE_TCP="$TMP/tcp" FAKE_DNS="$TMP/dns" FAKE_NODES="$TMP/nodes" FAKE_LISTEN="443 6443 80" FAKE_UFW="$TMP/ufw"
export FAKE_PODS="$TMP/pods"
# kube-system: системные поды на главном, device plugin проекта на GPU-ноде (так и должно быть), под без ноды (Pending)
set_pods() {
  printf 'coredns-6d4b llm-main \nlocal-path-provisioner-5c llm-main \nnvidia-device-plugin-daemonset-x1 class02 nsu-llm\nhelper-pod-new  \n' >"$FAKE_PODS"
}
set_pods
printf 'llm.nsu.test 10.0.0.5\n' >"$FAKE_DNS"
set_nodes() { printf 'class01 10.0.1.11 \nclass02 10.0.1.12 \nllm-main 10.0.0.5 10.0.0.5\n' >"$FAKE_NODES"; }
set_nodes
good_tcp() {
  : >"$FAKE_TCP"
  for ip in 10.0.1.11 10.0.1.12; do printf '%s:18080 open\n%s:10250 open\n' "$ip" "$ip" >>"$FAKE_TCP"; done
}
good_tcp
tcp_set() { grep -v "^$1 " "$FAKE_TCP" >"$FAKE_TCP.n"; echo "$1 $2" >>"$FAKE_TCP.n"; mv "$FAKE_TCP.n" "$FAKE_TCP"; }
CFG="$TMP/diag.conf"
mkdir -p "$TMP/work"
d() { (cd "$TMP/work" && PATH="$BIN:$PATH" NSU_LLM_DIAG_CONFIG="$CFG" bash "$DIAG" "$@" 2>&1); }

# ------------------------------------------------------------------ все проверки проходят
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "всё исправно: код 0" "$rc" 0
has "443 слушается" "$out" "443/tcp слушается"
has "6443 слушается" "$out" "6443/tcp слушается"
has "DNS совпал с адресом" "$out" "llm.nsu.test -> 10.0.0.5"
has "нода class01: 18080" "$out" "class01 (10.0.1.11): 18080/tcp доступен"
has "системные поды на главном" "$out" "kube-system: системные поды работают на llm-main"
lacks "главный не проверяется как нода" "$out" "llm-main (10.0.0.5)"
has "итог без провалов" "$out" "провалов: 0"

# ------------------------------------------------------------------ 443 и 6443 не слушаются, контейнер остановлен
out="$(FAKE_LISTEN="" FAKE_RUNNING=false d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "порты не слушаются: код 1" "$rc" 1
has "443 не слушается" "$out" "443/tcp не слушается"
has "6443 не слушается" "$out" "6443/tcp не слушается"
has "контейнер остановлен назван причиной" "$out" "контейнер llm-main не запущен"
has "команда исправления: запуск главного" "$out" "bash main/run-main.sh --env-file .env"
has "команда исправления: кто занял порт" "$out" "sudo ss -ltnp"

# ------------------------------------------------------------------ контейнер работает, но порт не слушается
out="$(FAKE_LISTEN="443" d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "6443 закрыт при работающем контейнере: код 1" "$rc" 1
has "порт занят или не опубликован" "$out" "docker port llm-main"

# ------------------------------------------------------------------ нода: 18080 закрыт брандмауэром / никто не слушает
good_tcp; tcp_set 10.0.1.11:18080 blocked
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "18080 заблокирован: код 1" "$rc" 1
has "18080 заблокирован: нода названа" "$out" "class01 (10.0.1.11): 18080/tcp недоступен"
has "18080: команда для Windows" "$out" "New-NetFirewallRule -DisplayName NSU-LLM-Node"
has "18080: источник в правиле — адрес главного" "$out" "-RemoteAddress 10.0.0.5"
has "18080: Hyper-V" "$out" "New-NetFirewallHyperVRule"
good_tcp; tcp_set 10.0.1.11:18080 refused
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "18080 отказ соединения (нода есть, порт никто не слушает): код 0" "$rc" 0
has "18080 отказ: сеть проходит" "$out" "class01 (10.0.1.11): 18080/tcp сеть доходит, порт никто не слушает"

# ------------------------------------------------------------------ 10250 не проверяется
: >"$TMP/tcplog"
out="$(FAKE_TCP_LOG="$TMP/tcplog" d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "10250 не проверяется: код 0" "$rc" 0
lacks "10250: подключения нет" "$(cat "$TMP/tcplog")" ":10250"
has "10250: одна строка-пояснение" "$out" "10250/tcp (kubelet) не проверяется"
has "10250: только при --network host" "$out" "--network host"
lacks "10250: нет правила брандмауэра для kubelet" "$out" "NSU-LLM-Kubelet"
has "без предупреждений" "$out" "предупреждений: 0"

# ------------------------------------------------------------------ системный под на ПК класса
printf 'coredns-6d4b class01 \nlocal-path-provisioner-5c llm-main \n' >"$FAKE_PODS"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "coredns на ПК класса: код 1" "$rc" 1
has "coredns на ПК класса: под и нода" "$out" "kube-system: под coredns-6d4b работает на ноде class01, а не на llm-main"
has "coredns на ПК класса: причина taint" "$out" "llm.nsu/class"
has "coredns на ПК класса: команда" "$out" "k3s kubectl -n kube-system delete pod coredns-6d4b"
lacks "local-path на главном не назван" "$out" "под local-path-provisioner-5c"
set_pods

# ------------------------------------------------------------------ ss не видит порт (userland-proxy=false)
good_tcp; printf '10.0.0.5:443 open\n10.0.0.5:6443 open\n' >>"$FAKE_TCP"
out="$(FAKE_LISTEN="" d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "порт без сокета, но открыт на LAN-адресе: код 0" "$rc" 0
has "443 по подключению к LAN-адресу" "$out" "443/tcp слушается"
has "6443 по подключению к LAN-адресу" "$out" "6443/tcp слушается"
good_tcp

# ------------------------------------------------------------------ docker есть, прав нет
out="$(FAKE_DOCKER_DENIED=1 d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "нет доступа к docker: код 1" "$rc" 1
has "нет доступа к docker: сообщение" "$out" "нет доступа к docker (добавьте пользователя в группу docker или запустите через sudo)"
lacks "нет доступа к docker: не выдаётся за остановленный контейнер" "$out" "контейнер llm-main не запущен"

# ------------------------------------------------------------------ DNS
out="$(d --yes --hostname missing.nsu.test --ip 10.0.0.5)"; rc=$?
eq "DNS не разрешается: код 1" "$rc" 1
has "DNS: запись не найдена" "$out" "missing.nsu.test не разрешается"
has "DNS: что добавить в DNS кампуса" "$out" "A-запись missing.nsu.test -> 10.0.0.5"
has "DNS: временный /etc/hosts" "$out" "/etc/hosts"
printf 'llm.nsu.test 10.0.0.77\n' >"$FAKE_DNS"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "DNS указывает на другой адрес: предупреждение, код 0" "$rc" 0
has "DNS: другой адрес" "$out" "llm.nsu.test -> 10.0.0.77, а адрес главного 10.0.0.5"
printf 'llm.nsu.test 10.0.0.5\n' >"$FAKE_DNS"
out="$(d --yes --hostname 10.0.0.5 --ip 10.0.0.5)"
has "имя-адрес: проверка DNS пропущена" "$out" "PUBLIC_HOSTNAME задан адресом"

# ------------------------------------------------------------------ портал и сертификат
out="$(FAKE_HTTP_CODE=502 d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "readyz 502: код 1" "$rc" 1
has "readyz: код ответа" "$out" "/readyz ответил 502"
has "readyz: команда логов" "$out" "docker logs --tail 100 llm-main"
out="$(FAKE_TRUST=0 d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "сертификат не доверенный в системе: код 0" "$rc" 0
has "сертификат: про ca.crt и GPO" "$out" "ca.crt"
has "сертификат: ссылка на документацию" "$out" "Сеть и DNS"

# ------------------------------------------------------------------ ноды не найдены / ноды вручную / внешний адрес
: >"$FAKE_NODES"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "нод нет: код 0" "$rc" 0
has "нод нет: подсказка" "$out" "ноды в кластере не найдены"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5 --node pc9=10.0.1.99)"
has "нода из параметра --node проверяется" "$out" "pc9 (10.0.1.99): 18080/tcp недоступен"
printf 'class01 10.0.1.11 10.0.1.61\n' >"$FAKE_NODES"
printf '10.0.1.61:18080 open\n10.0.1.61:10250 open\n' >"$FAKE_TCP"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"
has "у ноды с ExternalIP проверяется внешний адрес (его использует портал)" "$out" "class01 (10.0.1.61): 18080/tcp доступен"
set_nodes; good_tcp

# ------------------------------------------------------------------ брандмауэр хоста
cat >"$BIN/ufw" <<'EOS'
#!/usr/bin/env bash
cat "$FAKE_UFW"
EOS
chmod +x "$BIN/ufw"
printf 'Status: active\n\nTo Action From\n-- ------ ----\n443/tcp ALLOW Anywhere\n80/tcp ALLOW Anywhere\n' >"$FAKE_UFW"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "ufw без 6443: предупреждение, код 0" "$rc" 0
has "ufw: порт назван" "$out" "ufw: нет правила для 6443/tcp"
has "ufw: команда" "$out" "sudo ufw allow proto tcp from <подсеть класса> to any port 6443"
lacks "ufw: для 443 правило есть" "$out" "нет правила для 443/tcp"
printf 'Status: inactive\n' >"$FAKE_UFW"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"
has "ufw выключен: информация" "$out" "ufw выключен"
printf 'ERROR: You need to be root to run this script\n' >"$FAKE_UFW"
out="$(d --yes --hostname llm.nsu.test --ip 10.0.0.5)"
has "ufw без прав root: пропуск" "$out" "ufw status требует root"
rm -f "$BIN/ufw"

cat >"$BIN/firewall-cmd" <<'EOS'
#!/usr/bin/env bash
case "$1" in
  --state) echo running ;;
  --list-ports) echo "${FAKE_FW_PORTS:-}" ;;
  --list-services) echo "${FAKE_FW_SERVICES:-ssh}" ;;
esac
EOS
chmod +x "$BIN/firewall-cmd"
out="$(FAKE_FW_PORTS="6443/tcp" FAKE_FW_SERVICES="ssh https" d --yes --hostname llm.nsu.test --ip 10.0.0.5)"; rc=$?
eq "firewalld: 6443 и https открыты, код 0" "$rc" 0
has "firewalld: 6443 открыт" "$out" "firewalld: порт 6443/tcp открыт"
has "firewalld: 443 открыт сервисом https" "$out" "firewalld: порт 443/tcp открыт"
out="$(FAKE_FW_PORTS="" d --yes --hostname llm.nsu.test --ip 10.0.0.5)"
has "firewalld: 6443 закрыт, команда" "$out" "sudo firewall-cmd --permanent --add-port=6443/tcp"
rm -f "$BIN/firewall-cmd"

# ------------------------------------------------------------------ источники параметров, конфигурация, секреты
out="$(d --yes)"; rc=$?
eq "--yes без имени: код 2" "$rc" 2
has "--yes без имени: подсказка" "$out" "--hostname"

rm -f "$CFG"
printf 'OIDC_CLIENT_SECRET=topsecret-123\nPUBLIC_HOSTNAME=llm.nsu.test\nMAIN_ADVERTISE_IP=10.0.0.5\n' >"$TMP/.env"
out="$(d --yes --env-file "$TMP/.env")"; rc=$?
eq "параметры из .env: код 0" "$rc" 0
has ".env: имя взято из файла" "$out" "llm.nsu.test -> 10.0.0.5"
lacks "секреты из .env не печатаются" "$out" "topsecret-123"

out="$(FAKE_CONTAINER_ENV="PUBLIC_HOSTNAME=llm.nsu.test MAIN_ADVERTISE_IP=10.0.0.5 OIDC_CLIENT_SECRET=topsecret-123" d --yes)"; rc=$?
eq "параметры из контейнера llm-main: код 0" "$rc" 0
lacks "секреты из контейнера не печатаются" "$out" "topsecret-123"

# интерактивный режим: вопросы с подставленным значением, ответы сохраняются, повторный запуск без вопросов
rm -f "$CFG"
out="$(printf 'llm.nsu.test\n\n' | d)"; rc=$?
eq "интерактивный запуск: код 0" "$rc" 0
has "вопрос про имя" "$out" "Имя главного сервера (PUBLIC_HOSTNAME)"
has "вопрос про адрес с подставленным значением" "$out" "[10.0.0.5]"
[ -f "$CFG" ] && pass "ответы записаны в файл конфигурации" || fail "ответы записаны в файл конфигурации"
has "в файле имя" "$(cat "$CFG" 2>/dev/null)" "PUBLIC_HOSTNAME=llm.nsu.test"
has "в файле адрес (значение по умолчанию принято)" "$(cat "$CFG" 2>/dev/null)" "MAIN_ADVERTISE_IP=10.0.0.5"
out="$(d </dev/null)"; rc=$?
eq "повторный запуск без вопросов: код 0" "$rc" 0
lacks "повторный запуск не спрашивает" "$out" "Имя главного сервера (PUBLIC_HOSTNAME)"

out="$(d --bogus 2>&1)"; rc=$?
eq "неизвестный параметр: код 2" "$rc" 2

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
