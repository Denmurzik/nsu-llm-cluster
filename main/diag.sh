#!/usr/bin/env bash
# Диагностика сети главного сервера NSU LLM Cluster. Запускается на главном хосте, ничего не меняет.
# Использование:
#   diag.sh [--hostname ИМЯ] [--ip АДРЕС] [--node ИМЯ=АДРЕС]... [--env-file FILE] [--config FILE]
#           [--container llm-main] [--timeout СЕК] [--yes]
#
# Что проверяется (у каждого провала печатаются причина и готовая команда исправления):
#   1. 443/tcp и 6443/tcp слушаются на этом хосте; контейнер llm-main работает; портал отвечает на /readyz;
#   2. DNS: PUBLIC_HOSTNAME разрешается в адрес главного; сертификат доверенный для этого хоста;
#   3. для каждой ноды кластера: 18080/tcp (инференс);
#   4. системные поды kube-system работают на llm-main, а не на ПК класса;
#   5. локальный брандмауэр (ufw, firewalld), если он включён.
#
# Откуда берутся значения (первое найденное): параметры, --env-file (по умолчанию ./.env), контейнер llm-main,
# файл конфигурации. Недостающее спрашивается (имя главного; адрес предлагается автоопределённый) и записывается в
# файл конфигурации (по умолчанию ~/.config/nsu-llm/diag.conf), повторный запуск идёт без вопросов.
# --yes: без вопросов; если значения нет ни в одном источнике, код выхода 2.
# Коды выхода: 0 провалов нет (предупреждения допустимы), 1 есть провалы, 2 ошибка параметров.
set -uo pipefail
export MSYS_NO_PATHCONV=1

HOSTNAME_ARG=""
IP_ARG=""
ENV_FILE=""
CONFIG="${NSU_LLM_DIAG_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/nsu-llm/diag.conf}"
CONTAINER="llm-main"
PROBE_TIMEOUT=3
YES=0
EXTRA_NODES=()

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }
usage_error() { echo "Ошибка: $*" >&2; usage >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) HOSTNAME_ARG="${2:?}"; shift 2 ;;
    --ip) IP_ARG="${2:?}"; shift 2 ;;
    --node) EXTRA_NODES+=("${2:?}"); shift 2 ;;
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --config) CONFIG="${2:?}"; shift 2 ;;
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --timeout) PROBE_TIMEOUT="${2:?}"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "неизвестный параметр: $1" ;;
  esac
done
[[ "$PROBE_TIMEOUT" =~ ^[0-9]+$ ]] || usage_error "--timeout: целое число секунд"

# ------------------------------------------------------------------ значения: источники и вопросы
# Читается только запрошенный ключ: секреты (OIDC_CLIENT_SECRET и другие) ни читаются, ни печатаются.
file_value() { # file_value FILE KEY
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ]; }
container_value() { # container_value KEY
  command -v docker >/dev/null 2>&1 || return 0
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" 2>/dev/null | sed -n "s/^$1=//p" | tail -n1
}
if [ -z "$ENV_FILE" ] && [ -f .env ]; then ENV_FILE=.env; fi
[ -z "$ENV_FILE" ] || [ -f "$ENV_FILE" ] || usage_error "файл $ENV_FILE не найден"

resolve() { # resolve KEY ARG: параметр, env-файл, контейнер, конфигурация
  local v="$2"
  [ -n "$v" ] || v="$(file_value "$ENV_FILE" "$1")"
  [ -n "$v" ] || v="$(container_value "$1")"
  [ -n "$v" ] || v="$(file_value "$CONFIG" "$1")"
  printf '%s' "$v"
}
PUBLIC_HOSTNAME="$(resolve PUBLIC_HOSTNAME "$HOSTNAME_ARG")"
MAIN_ADVERTISE_IP="$(resolve MAIN_ADVERTISE_IP "$IP_ARG")"

detect_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  fi
  [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "$ip"
}

ASKED=0
ask() { # ask VAR "вопрос" "значение по умолчанию": пустой ответ принимает значение по умолчанию
  local var="$1" q="$2" def="$3" ans=""
  if [ -n "$def" ]; then printf '%s [%s]: ' "$q" "$def" >&2; else printf '%s: ' "$q" >&2; fi
  read -r ans || ans=""
  [ -n "$ans" ] || ans="$def"
  printf -v "$var" '%s' "$ans"
  ASKED=1
}

if [ -z "$PUBLIC_HOSTNAME" ]; then
  [ "$YES" = 0 ] || usage_error "не задано имя главного сервера: укажите --hostname (PUBLIC_HOSTNAME из .env), --env-file или запустите без --yes"
  ask PUBLIC_HOSTNAME "Имя главного сервера (PUBLIC_HOSTNAME), под которым к нему обращаются ПК класса" ""
  [ -n "$PUBLIC_HOSTNAME" ] || usage_error "имя главного сервера не задано"
fi
if [ -z "$MAIN_ADVERTISE_IP" ]; then
  MAIN_ADVERTISE_IP="$(detect_ip)"
  if [ "$YES" = 0 ]; then
    ask MAIN_ADVERTISE_IP "LAN-адрес главного сервера (MAIN_ADVERTISE_IP)" "$MAIN_ADVERTISE_IP"
  fi
  [ -n "$MAIN_ADVERTISE_IP" ] || usage_error "не удалось определить адрес этого хоста, укажите --ip"
fi
[[ "$MAIN_ADVERTISE_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || usage_error "адрес главного должен быть IPv4, получено: $MAIN_ADVERTISE_IP"
[[ "$PUBLIC_HOSTNAME" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || usage_error "недопустимое имя главного сервера: $PUBLIC_HOSTNAME"

if [ "$ASKED" = 1 ]; then
  mkdir -p "$(dirname "$CONFIG")"
  (umask 077; printf 'PUBLIC_HOSTNAME=%s\nMAIN_ADVERTISE_IP=%s\n' "$PUBLIC_HOSTNAME" "$MAIN_ADVERTISE_IP" >"$CONFIG")
  echo "Ответы записаны в $CONFIG: повторный запуск пройдёт без вопросов." >&2
fi

# ------------------------------------------------------------------ вывод
N_OK=0; N_FAIL=0; N_WARN=0
detail() { # detail СТРОКА...: продолжение с отступом (причина, команда исправления)
  local l
  for l in "$@"; do printf '       %s\n' "$l"; done
}
ok()   { N_OK=$((N_OK + 1)); printf '[ OK ] %s\n' "$1"; }
info() { printf '[ -- ] %s\n' "$1"; }
bad()  { N_FAIL=$((N_FAIL + 1)); printf '[СБОЙ] %s\n' "$1"; shift; detail "$@"; }
warn() { N_WARN=$((N_WARN + 1)); printf '[ВНИМ] %s\n' "$1"; shift; detail "$@"; }

# tcp_probe HOST PORT: open | refused (сеть доходит, порт никто не слушает) | blocked (таймаут или нет маршрута)
tcp_probe() {
  local out rc
  out="$(timeout "$PROBE_TIMEOUT" bash -c ': </dev/tcp/$0/$1' "$1" "$2" 2>&1)"; rc=$?
  if [ "$rc" = 0 ]; then echo open
  elif [ "$rc" != 124 ] && grep -qiE 'refused|отказ' <<<"$out"; then echo refused
  else echo blocked
  fi
}

declare -A LISTENING=()
listening() { # listening PORT: слушается ли TCP-порт на этом хосте (результат запоминается)
  if [ -z "${LISTENING[$1]:-}" ]; then
    LISTENING[$1]=no
    if command -v ss >/dev/null 2>&1 \
      && ss -ltnH 2>/dev/null | awk -v p=":$1" '{ n = length($4) - length(p) + 1; if (n > 0 && substr($4, n) == p) f = 1 } END { exit !f }'; then
      LISTENING[$1]=yes
    # Docker с userland-proxy=false публикует порт правилом DNAT без сокета, ss его не видит: проверка подключением
    # к LAN-адресу (к 127.0.0.1 это правило не применяется)
    elif [ "$(tcp_probe "$MAIN_ADVERTISE_IP" "$1")" = open ]; then
      LISTENING[$1]=yes
    fi
  fi
  [ "${LISTENING[$1]}" = yes ]
}

echo "NSU LLM Cluster: диагностика сети главного сервера"
echo "Главный: $PUBLIC_HOSTNAME, LAN-адрес $MAIN_ADVERTISE_IP, контейнер $CONTAINER"
echo

# ------------------------------------------------------------------ 1. порты главного
DOCKER_OK=0; command -v docker >/dev/null 2>&1 && DOCKER_OK=1
if [ "$DOCKER_OK" = 1 ] && ! docker_err="$(docker info 2>&1 >/dev/null)" && grep -qi 'permission denied' <<<"$docker_err"; then
  bad "нет доступа к docker (добавьте пользователя в группу docker или запустите через sudo)" \
    "Причина: без docker не проверяются контейнер $CONTAINER, ноды кластера и системные поды" \
    "Исправление: sudo usermod -aG docker \$USER, затем перезайдите в систему; или sudo bash main/diag.sh"
  DOCKER_OK=0
fi
RUNNING=0; if [ "$DOCKER_OK" = 1 ] && container_running; then RUNNING=1; fi
if [ "$DOCKER_OK" = 1 ] && [ "$RUNNING" = 0 ]; then
  info "контейнер $CONTAINER не запущен или не существует"
fi

for p in 443 6443; do
  if listening "$p"; then
    ok "$p/tcp слушается на этом хосте"
  elif [ "$DOCKER_OK" = 1 ] && [ "$RUNNING" = 0 ]; then
    bad "$p/tcp не слушается на этом хосте" \
      "Причина: контейнер $CONTAINER не запущен, поэтому порт никто не слушает" \
      "Исправление: docker ps -a --filter name=$CONTAINER; docker logs --tail 50 $CONTAINER" \
      "Исправление: bash main/run-main.sh --env-file .env" \
      "Если порт занят другим процессом: sudo ss -ltnp 'sport = :$p'"
  else
    bad "$p/tcp не слушается на этом хосте" \
      "Причина: порт $p не опубликован контейнером или занят другим процессом" \
      "Исправление: docker port $CONTAINER   (должны быть 443, 80 и 6443)" \
      "Исправление: sudo ss -ltnp 'sport = :$p'   (кто занял порт); затем bash main/run-main.sh --env-file .env"
  fi
done

if listening 443; then
  code="$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 -m 15 "https://$MAIN_ADVERTISE_IP/readyz" 2>/dev/null)"
  if [ "$code" = 200 ]; then
    ok "портал отвечает: https://$MAIN_ADVERTISE_IP/readyz -> 200"
  else
    bad "портал не готов: /readyz ответил ${code:-нет ответа}" \
      "Причина: Caddy слушает порт, но портал, LiteLLM или Postgres ещё не поднялись (после запуска нужно до минуты)" \
      "Исправление: docker logs --tail 100 $CONTAINER" \
      "Исправление: docker exec $CONTAINER k3s kubectl -n llm get pods"
  fi
fi

# ------------------------------------------------------------------ 2. DNS и сертификат
DNS_OK=0
if [[ "$PUBLIC_HOSTNAME" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  info "PUBLIC_HOSTNAME задан адресом ($PUBLIC_HOSTNAME): проверка DNS пропущена. Для сертификата НГУ и для ПК класса нужно имя"
  DNS_OK=1
else
  resolved="$(getent ahostsv4 "$PUBLIC_HOSTNAME" 2>/dev/null | awk 'NR == 1 {print $1}')"
  if [ -z "$resolved" ]; then
    bad "DNS: $PUBLIC_HOSTNAME не разрешается" \
      "Причина: в DNS кампуса нет записи для этого имени, ПК класса и пользователи не найдут главный" \
      "Исправление: попросите администратора DNS создать A-запись $PUBLIC_HOSTNAME -> $MAIN_ADVERTISE_IP" \
      "Временно (Linux, на этом сервере): echo '$MAIN_ADVERTISE_IP $PUBLIC_HOSTNAME' | sudo tee -a /etc/hosts" \
      "Временно (Windows, ПК класса, PowerShell от администратора): Add-Content C:\\Windows\\System32\\drivers\\etc\\hosts '$MAIN_ADVERTISE_IP $PUBLIC_HOSTNAME'"
  elif [ "$resolved" = "$MAIN_ADVERTISE_IP" ]; then
    ok "DNS: $PUBLIC_HOSTNAME -> $resolved"
    DNS_OK=1
  else
    warn "DNS: $PUBLIC_HOSTNAME -> $resolved, а адрес главного $MAIN_ADVERTISE_IP" \
      "Причина: запись указывает на другой адрес. Если главный стоит за NAT или балансировщиком, это нормально" \
      "Исправление: иначе исправьте A-запись на $MAIN_ADVERTISE_IP или задайте верный MAIN_ADVERTISE_IP"
    DNS_OK=1
  fi
fi

if [ "$DNS_OK" = 1 ] && listening 443; then
  curl -s -o /dev/null --connect-timeout 5 -m 15 "https://$PUBLIC_HOSTNAME/readyz" >/dev/null 2>&1; crc=$?
  case "$crc" in
    0) ok "сертификат: https://$PUBLIC_HOSTNAME принят системой этого хоста без исключений" ;;
    60|51|58|59|77|83)
      info "сертификат: система этого хоста ему не доверяет (так и должно быть для внутреннего CA: ca.crt нужно раздать ПК класса)"
      detail "Если используется сертификат НГУ (--certs), этого быть не должно: проверьте цепочку в tls.crt (нужен сертификат вместе с промежуточными)." \
             "Раздача внутреннего CA и сертификат НГУ: docs/deploy-class.md, раздел «Сеть и DNS»" ;;
    *) info "сертификат не проверен: curl завершился с кодом $crc" ;;
  esac
fi

# ------------------------------------------------------------------ 3. ноды: 18080
NODES=()
if [ "$DOCKER_OK" = 1 ] && [ "$RUNNING" = 1 ]; then
  while read -r name internal external _; do
    [ -n "$name" ] || continue
    [ "$name" != llm-main ] || continue
    NODES+=("$name=${external:-$internal}")
  done < <(docker exec "$CONTAINER" k3s kubectl get nodes -o \
    'jsonpath={range .items[*]}{.metadata.name}{" "}{.status.addresses[?(@.type=="InternalIP")].address}{" "}{.status.addresses[?(@.type=="ExternalIP")].address}{"\n"}{end}' 2>/dev/null)
fi
for n in "${EXTRA_NODES[@]:-}"; do
  [ -n "$n" ] || continue
  [[ "$n" == *=* ]] || usage_error "--node: формат ИМЯ=АДРЕС, получено: $n"
  NODES+=("$n")
done

if [ "${#NODES[@]}" = 0 ]; then
  info "ноды в кластере не найдены: проверка 18080 пропущена (ноды в режиме standalone в k3s не видны: укажите их через --node ИМЯ=АДРЕС)"
fi

declare -A SEEN=()
for entry in "${NODES[@]:-}"; do
  [ -n "$entry" ] || continue
  name="${entry%%=*}"; addr="${entry#*=}"
  [ -z "${SEEN[$addr]:-}" ] || continue
  SEEN[$addr]=1
  label="$name ($addr)"

  case "$(tcp_probe "$addr" 18080)" in
    open) ok "$label: 18080/tcp доступен" ;;
    refused) ok "$label: 18080/tcp сеть доходит, порт никто не слушает (на ноде нет запущенного сервера модели; это нормально, пока нет развёртывания)" ;;
    *) bad "$label: 18080/tcp недоступен с главного" \
         "Причина: брандмауэр ПК, нет маршрута между подсетями или нода выключена. Шлюз не сможет отправлять запросы на эту ноду" \
         "Исправление (Windows, PowerShell от администратора на ноде): New-NetFirewallRule -DisplayName NSU-LLM-Node -Direction Inbound -Protocol TCP -LocalPort 18080 -RemoteAddress $MAIN_ADVERTISE_IP -Action Allow" \
         "Исправление (нода в WSL с mirrored-сетью, там же): New-NetFirewallHyperVRule -Name llm-inference -DisplayName 'LLM inference' -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 18080,18050" \
         "Исправление (Linux-нода): sudo ufw allow from $MAIN_ADVERTISE_IP to any port 18080 proto tcp" \
         "Проверка маршрута: traceroute -n $addr; на самой ноде запустите node/diag.ps1 (Windows): он покажет, какого правила не хватает" ;;
  esac
done
if [ "${#NODES[@]}" != 0 ]; then
  info "10250/tcp (kubelet) не проверяется: главному он не нужен (logs и exec идут через туннель ноды на 6443, metrics-server выключен); на адресе ПК он слушается только у ноды с --network host"
fi

# ------------------------------------------------------------------ 4. системные поды на главном
# ПК класса присоединяются с taint llm.nsu/class=true:NoSchedule; системные поды k3s (coredns, local-path-provisioner)
# его не терпят и остаются на llm-main, иначе DNS и тома главного зависели бы от оверлея 8472/udp между подсетями.
# Поды проекта в kube-system (nvidia-device-plugin, метка app.kubernetes.io/part-of=nsu-llm) работают на GPU-нодах намеренно.
if [ "$DOCKER_OK" = 1 ] && [ "$RUNNING" = 1 ]; then
  if sys_pods="$(docker exec "$CONTAINER" k3s kubectl -n kube-system get pods -o \
      'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{" "}{.metadata.labels.app\.kubernetes\.io/part-of}{"\n"}{end}' 2>/dev/null)"; then
    off_main=0
    while read -r pod node part_of; do
      # без nodeName под ещё не назначен (Pending): на ПК класса он не работает
      [ -n "$pod" ] && [ -n "$node" ] && [ "$node" != llm-main ] && [ "$part_of" != nsu-llm ] || continue
      off_main=1
      bad "kube-system: под $pod работает на ноде $node, а не на llm-main" \
        "Причина: нода присоединилась без taint llm.nsu/class (до этой версии), и системный под k3s попал на ПК класса; DNS и тома главного тогда зависят от сети между подсетями" \
        "Исправление: портал сам добавляет taint таким нодам за один проход контроллера; проверка: docker exec $CONTAINER k3s kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints[*].key" \
        "Исправление: затем перезапустите под, он встанет на главный: docker exec $CONTAINER k3s kubectl -n kube-system delete pod $pod"
    done <<<"$sys_pods"
    [ "$off_main" = 1 ] || ok "kube-system: системные поды работают на llm-main"
  else
    info "kube-system: список подов не получен (k3s ещё запускается?), проверка пропущена"
  fi
fi

# ------------------------------------------------------------------ 5. брандмауэр этого хоста
# Порты, опубликованные Docker (-p), обходят ufw и firewalld через собственные цепочки iptables: отсутствие правила
# не доказывает блокировки, поэтому это предупреждение. Решает проверка с ПК класса (node/diag.ps1).
if command -v ufw >/dev/null 2>&1; then
  ufw_out="$(ufw status 2>&1)"
  if grep -q 'Status: active' <<<"$ufw_out"; then
    for p in 443 6443; do
      if grep -Eq "^$p(/tcp)?[[:space:]]" <<<"$ufw_out"; then
        ok "ufw: правило для $p/tcp есть"
      else
        warn "ufw: нет правила для $p/tcp" \
          "Причина: ufw включён и не разрешает порт. Порты, опубликованные Docker, ufw обходит, поэтому это может не мешать; проверяйте доступность с ПК класса (node/diag.ps1)" \
          "Исправление: sudo ufw allow proto tcp from <подсеть класса> to any port $p"
      fi
    done
  elif grep -q 'Status: inactive' <<<"$ufw_out"; then
    info "ufw выключен: ограничений на этом хосте нет"
  else
    info "ufw status требует root: правила не проверены (запустите diag.sh через sudo)"
  fi
fi
if command -v firewall-cmd >/dev/null 2>&1 && [ "$(firewall-cmd --state 2>/dev/null)" = running ]; then
  fw_ports="$(firewall-cmd --list-ports 2>/dev/null) $(firewall-cmd --list-services 2>/dev/null)"
  for p in 443 6443; do
    svc=""; [ "$p" = 443 ] && svc="https"
    if grep -Eq "(^|[[:space:]])$p/tcp([[:space:]]|$)" <<<"$fw_ports" || { [ -n "$svc" ] && grep -Eq "(^|[[:space:]])$svc([[:space:]]|$)" <<<"$fw_ports"; }; then
      ok "firewalld: порт $p/tcp открыт"
    else
      warn "firewalld: порт $p/tcp не открыт в зоне по умолчанию" \
        "Причина: порты Docker могут обходить firewalld, но если ПК класса порт не видят, причина здесь" \
        "Исправление: sudo firewall-cmd --permanent --add-port=$p/tcp && sudo firewall-cmd --reload"
    fi
  done
fi

echo
echo "Итог: проверок пройдено: $N_OK, провалов: $N_FAIL, предупреждений: $N_WARN"
if [ "$N_FAIL" -gt 0 ]; then
  echo "Исправьте провалы выше и запустите diag.sh ещё раз. Связность в обратную сторону (ПК класса -> главный) проверяет node/diag.ps1."
  exit 1
fi
echo "Связность в обратную сторону (ПК класса -> главный) проверяет node/diag.ps1 на ПК класса."
exit 0
