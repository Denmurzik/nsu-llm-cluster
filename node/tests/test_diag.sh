#!/usr/bin/env bash
# Offline tests for node/diag.ps1: the system answers (DNS, TCP, HTTP, firewall, WSL) come from a JSON stub file (-Stub);
# no network, no real firewall, no WSL. Needs PowerShell (Windows PowerShell 5.1 or pwsh); python is used to edit the stub.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILS=$((FAILS + 1)); }
has() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (нет: $3)" "$2" ;; esac; }
lacks() { case "$2" in *"$3"*) fail "$1 (лишнее: $3)" "$2" ;; *) pass "$1" ;; esac; }
eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (ожидалось $3, получено $2)"; fi; }

PS="${PS:-$(command -v powershell || command -v pwsh || true)}"
if [ -z "$PS" ]; then echo "skip - PowerShell не найден"; exit 0; fi
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
DIAG="$(winpath "$NODE/diag.ps1")"
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "skip - python не найден"; exit 0; }

# синтаксис: разбор файла парсером PowerShell
syn="$("$PS" -NoProfile -Command "\$e = \$null; [void][System.Management.Automation.Language.Parser]::ParseFile('$DIAG', [ref]\$null, [ref]\$e); if (\$e) { \$e | ForEach-Object { \$_.Message }; exit 1 }" 2>&1)" \
  && pass "diag.ps1 разбирается без ошибок" || fail "diag.ps1 разбирается без ошибок" "$syn"

# BOM нужен Windows PowerShell 5.1 для кириллицы в UTF-8 файле
[ "$(head -c3 "$NODE/diag.ps1" | od -An -tx1 | tr -d ' \n')" = efbbbf ] && pass "diag.ps1 с BOM" || fail "diag.ps1 с BOM"

# сертификат для проверки отпечатка
CERT=""
if command -v openssl >/dev/null 2>&1; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$TMP/k.pem" -out "$TMP/ca.pem" -days 2 -subj "/CN=nsu-llm-test-ca" >/dev/null 2>&1 && CERT="$(cat "$TMP/ca.pem")"
fi
export CERT

BASE="$TMP/base.json"
"$PY" - "$(winpath "$BASE")" <<'EOF'
import json, os, sys
cert = os.environ.get("CERT") or "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
base = {
  "admin": True, "os_build": 22631, "local_ip": "10.0.1.11",
  "dns": {"llm.nsu.test": ["10.0.0.5"]},
  "tcp": {"10.0.0.5:443": "open", "10.0.0.5:6443": "open"},
  "http": {
    "GET https://llm.nsu.test/ca.crt": {"code": 200, "body": cert},
    "TRUSTED GET https://llm.nsu.test/readyz": {"code": 200},
    "POST https://llm.nsu.test/api/nodes/register": {"code": 401},
  },
  "firewall": {"rules": [{"name": "NSU-LLM-Node", "action": "Allow", "protocol": "TCP", "ports": "18080,18050", "remote": "Any"}]},
  "wsl": {"installed": True, "version": "2.3.26", "networking_mode": "mirrored", "wslconfig": "[wsl2]\nnetworkingMode=mirrored\n",
          "dns": {"llm.nsu.test": "10.0.0.5"}, "tcp": {"10.0.0.5:6443": "open"}},
  "node_runtime": "wsl",
  "hyperv": {"supported": True, "default_inbound": "Block", "rules": [{"name": "llm-inference", "ports": "18080,18050"}]},
}
json.dump(base, open(sys.argv[1], "w", encoding="utf-8"))
EOF

# stub NAME 'python-выражение над словарём d': сохраняет вариант заглушки и печатает путь
stub() {
  "$PY" - "$(winpath "$BASE")" "$(winpath "$TMP/$1.json")" "$2" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
exec(sys.argv[3])
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"))
EOF
  winpath "$TMP/$1.json"
}
CFG="$(winpath "$TMP/diag-config.json")"
# run STUBFILE [args]: код выхода в $rc, вывод в $out
run() {
  local s="$1"; shift
  out="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$DIAG" -Stub "$s" -Config "$CFG" "$@" 2>&1)"; rc=$?
}

GOOD="$(winpath "$BASE")"

# ------------------------------------------------------------------ всё исправно
run "$GOOD" -Main llm.nsu.test -NonInteractive
eq "всё исправно: код 0" "$rc" 0
has "DNS" "$out" "DNS: llm.nsu.test -> 10.0.0.5"
has "443 доступен" "$out" "443/tcp главного (10.0.0.5) доступен"
has "6443 доступен" "$out" "6443/tcp главного (10.0.0.5) доступен"
has "ca.crt скачан" "$out" "ca.crt скачан с https://llm.nsu.test/ca.crt"
[ -n "$CERT" ] && has "ca.crt: отпечаток SHA256 показан" "$out" "SHA256 "
has "сертификат принят" "$out" "сертификат главного принят системой этого ПК"
has "эндпоинт регистрации" "$out" "POST /api/nodes/register -> 401"
has "брандмауэр 18080" "$out" "входящий 18080/tcp разрешён правилом NSU-LLM-Node"
has "брандмауэр 18050" "$out" "входящий 18050/tcp разрешён правилом NSU-LLM-Node"
has "нет запрещающих правил" "$out" "запрещающих входящих правил для 18080 и 18050 нет"
has "WSL mirrored работает" "$out" "WSL: mirrored-сеть работает"
has "Hyper-V правило" "$out" "Hyper-V firewall: входящий 18080/tcp разрешён правилом llm-inference"
has "Hyper-V правило 18050" "$out" "Hyper-V firewall: входящий 18050/tcp разрешён правилом llm-inference"
has "нода в WSL названа" "$out" "нода работает в WSL (Docker CE)"
has "имя главного изнутри WSL" "$out" "WSL: llm.nsu.test -> 10.0.0.5 (резолвер WSL)"
has "6443 изнутри WSL" "$out" "WSL: 6443/tcp главного (10.0.0.5) доступен изнутри WSL"
has "итог без провалов" "$out" "провалов: 0"
lacks "нет провалов в выводе" "$out" "[СБОЙ]"

# ------------------------------------------------------------------ DNS и порты главного
run "$(stub dns 'd["dns"] = {}')" -Main llm.nsu.test -NonInteractive
eq "DNS не разрешается: код 1" "$rc" 1
has "DNS: сообщение" "$out" "DNS: llm.nsu.test не разрешается"
has "DNS: A-запись" "$out" "создать A-запись llm.nsu.test"
has "DNS: hosts" "$out" "drivers\\etc\\hosts"
has "DNS: порты пропущены" "$out" "443/tcp главного не проверен"

run "$(stub p443 'd["tcp"]["10.0.0.5:443"] = "blocked"')" -Main llm.nsu.test -NonInteractive
eq "443 закрыт: код 1" "$rc" 1
has "443: недоступен" "$out" "443/tcp главного (10.0.0.5) недоступен: нет ответа"
has "443: Test-NetConnection" "$out" "Test-NetConnection 10.0.0.5 -Port 443"
has "443: ufw с адресом этого ПК" "$out" "sudo ufw allow proto tcp from 10.0.1.11 to any port 443"
has "443: ca.crt пропущен" "$out" "порт 443 главного недоступен"

run "$(stub p6443 'd["tcp"]["10.0.0.5:6443"] = "refused"')" -Main llm.nsu.test -NonInteractive
eq "6443 отказ: код 1" "$rc" 1
has "6443: отказ" "$out" "6443/tcp главного (10.0.0.5) недоступен: соединение отклонено"
has "6443: main/diag.sh" "$out" "bash main/diag.sh"
has "6443: standalone обходится без него" "$out" "для режима standalone достаточно 443"

run "$GOOD" -Main 10.0.0.5 -NonInteractive
has "главный по адресу: DNS пропущен" "$out" "проверка DNS пропущена"

# ------------------------------------------------------------------ ca.crt, доверие, регистрация
run "$(stub ca404 'd["http"]["GET https://llm.nsu.test/ca.crt"] = {"code": 404}')" -Main llm.nsu.test -NonInteractive
eq "ca.crt 404 (сертификат НГУ): код 0" "$rc" 0
has "ca.crt 404: пояснение" "$out" "используется сертификат НГУ"

run "$(stub ca502 'd["http"]["GET https://llm.nsu.test/ca.crt"] = {"code": 502}')" -Main llm.nsu.test -NonInteractive
eq "ca.crt 502: код 1" "$rc" 1
has "ca.crt 502: сообщение" "$out" "ca.crt: https://llm.nsu.test/ca.crt ответил 502"

run "$(stub untrusted 'd["http"]["TRUSTED GET https://llm.nsu.test/readyz"] = {"code": 0, "exit": 60}')" -Main llm.nsu.test -NonInteractive
eq "сертификат не доверенный: предупреждение, код 0" "$rc" 0
has "доверие: предупреждение" "$out" "сертификат главного не доверенный на этом ПК"
has "доверие: Import-Certificate" "$out" "Import-Certificate -FilePath"
has "доверие: GPO" "$out" "групповой политикой"
has "доверие: документация" "$out" "Сеть и DNS"
has "итог: одно предупреждение" "$out" "предупреждений: 1"

run "$(stub reg502 'd["http"]["POST https://llm.nsu.test/api/nodes/register"] = {"code": 502}')" -Main llm.nsu.test -NonInteractive
eq "регистрация 502: код 1" "$rc" 1
has "регистрация: сообщение" "$out" "POST /api/nodes/register ответил 502, ожидался 401"

# ------------------------------------------------------------------ локальный брандмауэр
run "$(stub fw0 'd["firewall"]["rules"] = []')" -Main llm.nsu.test -NonInteractive
eq "правил нет: код 1" "$rc" 1
has "нет правила 18080" "$out" "нет разрешающего правила для входящего 18080/tcp"
has "команда создания правила с подсетью и главным" "$out" "-RemoteAddress 10.0.1.0/24,10.0.0.5"
has "18050 только предупреждение" "$out" "[ВНИМ] брандмауэр: нет разрешающего правила для входящего 18050/tcp"

run "$(stub fw1 'd["firewall"]["rules"][0]["ports"] = "18080"')" -Main llm.nsu.test -NonInteractive
eq "только 18080: код 0" "$rc" 0
has "только 18080: 18050 предупреждение" "$out" "нет разрешающего правила для входящего 18050/tcp"

run "$(stub fwblock 'd["firewall"]["rules"].append({"name": "Block LLM", "action": "Block", "protocol": "TCP", "ports": "18000-19000", "remote": "Any"})')" -Main llm.nsu.test -NonInteractive
eq "запрещающее правило: код 1" "$rc" 1
has "запрещающее правило названо" "$out" "запрещающие входящие правила для 18080 или 18050: Block LLM"
has "команда отключения" "$out" "Disable-NetFirewallRule -DisplayName 'Block LLM'"

run "$(stub fwremote 'd["firewall"]["rules"][0]["remote"] = "10.0.1.0/24"')" -Main llm.nsu.test -NonInteractive
eq "правило не пускает главный: код 1" "$rc" 1
has "RemoteAddress не содержит главный" "$out" "не пускает главный (10.0.0.5)"
has "команда исправления RemoteAddress" "$out" "Set-NetFirewallRule -DisplayName 'NSU-LLM-Node' -RemoteAddress"

run "$(stub fwcidr 'd["firewall"]["rules"][0]["remote"] = "10.0.0.0/24,10.0.1.0/24"')" -Main llm.nsu.test -NonInteractive
eq "правило с подсетью главного: код 0" "$rc" 0
run "$(stub fwrange 'd["firewall"]["rules"][0]["remote"] = "10.0.0.1-10.0.0.9"')" -Main llm.nsu.test -NonInteractive
eq "правило с диапазоном главного: код 0" "$rc" 0
run "$(stub fwsubnet 'd["firewall"]["rules"][0]["remote"] = "LocalSubnet"')" -Main llm.nsu.test -NonInteractive
eq "LocalSubnet: предупреждение, код 0" "$rc" 0
has "LocalSubnet: объяснение" "$out" "только свою подсеть"

run "$(stub fwunread 'd["firewall"] = {}')" -Main llm.nsu.test -NonInteractive
eq "правила не прочитаны: код 0" "$rc" 0
has "правила не прочитаны: подсказка про администратора" "$out" "правила брандмауэра Windows не прочитаны"

# ------------------------------------------------------------------ WSL и mirrored
run "$(stub nowsl 'd["wsl"] = {"installed": False}')" -Main llm.nsu.test -NonInteractive
eq "WSL нет: код 0" "$rc" 0
has "WSL нет: Docker Desktop" "$out" "WSL не найден"

run "$(stub nat 'd["wsl"]["networking_mode"] = "nat"')" -Main llm.nsu.test -NonInteractive
eq "mirrored в конфиге, но WSL в NAT: код 1" "$rc" 1
has "mirrored не применён" "$out" "в .wslconfig задан mirrored, но WSL работает в режиме nat"
has "wsl --shutdown" "$out" "wsl --shutdown"

run "$(stub noconf 'd["wsl"]["networking_mode"] = "nat"; d["wsl"]["wslconfig"] = "[wsl2]\nmemory=24GB\n"')" -Main llm.nsu.test -NonInteractive
eq "NAT и mirrored не включён: код 1" "$rc" 1
has "NAT: объяснение" "$out" "порт ноды виден только самому ПК"
has "NAT: правка .wslconfig" "$out" "networkingMode=mirrored"

run "$(stub oldwsl 'd["wsl"]["version"] = "1.2.5"')" -Main llm.nsu.test -NonInteractive
eq "старый WSL: код 1" "$rc" 1
has "старый WSL: wsl --update" "$out" "wsl --update"

run "$(stub oldwin 'd["os_build"] = 19045')" -Main llm.nsu.test -NonInteractive
eq "Windows 10: код 1" "$rc" 1
has "Windows 10: объяснение" "$out" "не поддерживает mirrored-сеть"

run "$(stub nomode 'd["wsl"]["networking_mode"] = ""')" -Main llm.nsu.test -NonInteractive
eq "режим WSL не определён, конфиг mirrored: код 0" "$rc" 0
has "режим не определён: подсказка" "$out" "работающий режим определить не удалось"

run "$(stub nomodenoconf 'd["wsl"]["networking_mode"] = ""; d["wsl"]["wslconfig"] = ""')" -Main llm.nsu.test -NonInteractive
eq "конфиг без mirrored и режим не определён: предупреждение, код 0" "$rc" 0
has "конфиг без mirrored: предупреждение" "$out" "в .wslconfig нет networkingMode=mirrored"

run "$(stub hv 'd["hyperv"]["rules"] = []')" -Main llm.nsu.test -NonInteractive
eq "Hyper-V firewall без правила: код 1" "$rc" 1
has "Hyper-V: правило" "$out" "New-NetFirewallHyperVRule -Name llm-inference"
has "Hyper-V: идентификатор WSL" "$out" "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"

run "$(stub hv50 'd["hyperv"]["rules"] = [{"name": "llm-inference", "ports": "18080"}]')" -Main llm.nsu.test -NonInteractive
eq "Hyper-V firewall без правила для 18050: только предупреждение, код 0" "$rc" 0
has "Hyper-V: 18080 разрешён" "$out" "Hyper-V firewall: входящий 18080/tcp разрешён правилом llm-inference"
has "Hyper-V: 18050 предупреждение" "$out" "[ВНИМ] Hyper-V firewall: нет правила для входящего 18050/tcp"
has "Hyper-V: 18050 исправление" "$out" "-LocalPorts 18080,18050"

run "$(stub hvallow 'd["hyperv"]["rules"] = []; d["hyperv"]["default_inbound"] = "Allow"')" -Main llm.nsu.test -NonInteractive
eq "Hyper-V firewall по умолчанию разрешает: код 0" "$rc" 0
has "Hyper-V: по умолчанию разрешено" "$out" "входящие в WSL разрешены по умолчанию"

run "$(stub hvunsup 'd["hyperv"] = {"supported": False}')" -Main llm.nsu.test -NonInteractive
eq "Hyper-V firewall не читается: код 0" "$rc" 0
has "Hyper-V: не прочитано" "$out" "состояние прочитать не удалось"

# ------------------------------------------------------------------ связь с главным изнутри WSL
run "$(stub wsldns 'd["wsl"]["dns"] = {}')" -Main llm.nsu.test -NonInteractive
eq "имя не разрешается в WSL: код 1" "$rc" 1
has "имя в WSL: сообщение" "$out" "WSL: llm.nsu.test не разрешается внутри WSL"
has "имя в WSL: Windows разрешает" "$out" "Windows разрешает имя (10.0.0.5)"
has "имя в WSL: команда проверки" "$out" "wsl -e getent hosts llm.nsu.test"
has "имя в WSL: dnsTunneling" "$out" "dnsTunneling"

run "$(stub wsldiff 'd["wsl"]["dns"] = {"llm.nsu.test": "10.0.0.77"}; d["wsl"]["tcp"] = {"10.0.0.77:6443": "open"}')" -Main llm.nsu.test -NonInteractive
eq "в WSL другой адрес: предупреждение, код 0" "$rc" 0
has "в WSL другой адрес: оба адреса" "$out" "WSL: llm.nsu.test внутри WSL -> 10.0.0.77, а в Windows -> 10.0.0.5"

run "$(stub wsl6443 'd["wsl"]["tcp"] = {}')" -Main llm.nsu.test -NonInteractive
eq "6443 закрыт изнутри WSL: код 1" "$rc" 1
has "6443 изнутри WSL: сообщение" "$out" "WSL: 6443/tcp главного (10.0.0.5) недоступен изнутри WSL: нет ответа"
has "6443 изнутри WSL: из Windows доступен" "$out" "из Windows порт доступен, а из WSL нет"

run "$GOOD" -Main 10.0.0.5 -NonInteractive
has "главный по адресу: в WSL проверяется сам адрес" "$out" "WSL: 6443/tcp главного (10.0.0.5) доступен изнутри WSL"

# ------------------------------------------------------------------ нода в Docker Desktop (запасной путь) и нода не найдена
run "$(stub desktop 'd["node_runtime"] = "desktop"; d["wsl"]["networking_mode"] = "nat"; d["wsl"]["wslconfig"] = ""; d["hyperv"]["rules"] = []; d["wsl"]["dns"] = {}')" -Main llm.nsu.test -NonInteractive
eq "Docker Desktop с WSL в NAT: код 0" "$rc" 0
has "Docker Desktop: информация" "$out" "нода работает в Docker Desktop (запасной путь)"
has "Docker Desktop: NAT нормален" "$out" "WSL работает в режиме nat: для ноды в Docker Desktop это нормально"
lacks "Docker Desktop: без проверки Hyper-V" "$out" "Hyper-V firewall: входящие в WSL запрещены"
lacks "Docker Desktop: без проверок изнутри WSL" "$out" "внутри WSL"
has "Docker Desktop: без предупреждений" "$out" "предупреждений: 0"

run "$(stub desktopwin10 'd["node_runtime"] = "desktop"; d["os_build"] = 19045')" -Main llm.nsu.test -NonInteractive
eq "Docker Desktop на Windows 10: код 0" "$rc" 0

run "$(stub nonode 'd["node_runtime"] = ""; d["wsl"]["networking_mode"] = "nat"; d["wsl"]["wslconfig"] = ""')" -Main llm.nsu.test -NonInteractive
eq "контейнер не найден, WSL в NAT: предупреждение, код 0" "$rc" 0
has "контейнер не найден: пояснение" "$out" "контейнер llm-node на этом ПК не найден"
has "контейнер не найден: NAT предупреждением" "$out" "[ВНИМ] WSL: сеть работает в режиме nat, mirrored не включена"
has "контейнер не найден: про Docker Desktop" "$out" "Если нода запускается через Docker Desktop"

# ------------------------------------------------------------------ брандмауэр: правила, которые могут пропускать порт
run "$(stub fwprog 'd["firewall"]["rules"] = [{"name": "Docker Desktop Backend", "action": "Allow", "protocol": "Any", "ports": "Any", "program": "C:/Program Files/Docker/Docker/resources/com.docker.backend.exe"}]')" -Main llm.nsu.test -NonInteractive
eq "только правило программы: предупреждение, код 0" "$rc" 0
has "правило программы: предупреждение" "$out" "[ВНИМ] брандмауэр: отдельного правила для входящего 18080/tcp"
has "правило программы: названо" "$out" "Docker Desktop Backend (любые порты для программы"
has "правило программы: команда" "$out" "New-NetFirewallRule -DisplayName NSU-LLM-Node"

run "$(stub fwprofile 'd["firewall"]["rules"][0]["profile"] = "Domain"; d["firewall"]["active_profiles"] = ["Private"]')" -Main llm.nsu.test -NonInteractive
eq "правило в другом профиле: предупреждение, код 0" "$rc" 0
has "другой профиль: пояснение" "$out" "только профиль Domain, а сеть сейчас: Private"

run "$(stub fwprofileok 'd["firewall"]["rules"][0]["profile"] = "Domain, Private"; d["firewall"]["active_profiles"] = ["Private"]')" -Main llm.nsu.test -NonInteractive
eq "правило в текущем профиле: код 0" "$rc" 0
has "текущий профиль: правило принято" "$out" "входящий 18080/tcp разрешён правилом NSU-LLM-Node"

# ------------------------------------------------------------------ значения: источники, вопросы, конфигурация
run "$GOOD" -NonInteractive
eq "-NonInteractive без -Main: код 2" "$rc" 2
has "-NonInteractive без -Main: подсказка" "$out" "Укажите -Main"

run "$(stub nodeenv 'd["node_env"] = {"MAIN_HOST": "llm.nsu.test"}')" -NonInteractive
eq "-Main из контейнера llm-node: код 0" "$rc" 0
has "-Main из контейнера: проверка идёт" "$out" "DNS: llm.nsu.test -> 10.0.0.5"

rm -f "$TMP/diag-config.json"
out="$(printf 'llm.nsu.test\n' | "$PS" -NoProfile -ExecutionPolicy Bypass -File "$DIAG" -Stub "$GOOD" -Config "$CFG" 2>&1)"; rc=$?
eq "интерактивный запуск: код 0" "$rc" 0
has "вопрос про имя" "$out" "Имя или адрес главного сервера (PUBLIC_HOSTNAME)"
[ -f "$TMP/diag-config.json" ] && pass "ответ записан в файл конфигурации" || fail "ответ записан в файл конфигурации"
has "в файле имя" "$(cat "$TMP/diag-config.json" 2>/dev/null)" "llm.nsu.test"
out="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$DIAG" -Stub "$GOOD" -Config "$CFG" -NonInteractive </dev/null 2>&1)"; rc=$?
eq "повторный запуск без вопросов: код 0" "$rc" 0
lacks "повторный запуск не спрашивает" "$out" "PUBLIC_HOSTNAME)"

out="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$DIAG" -Stub "$GOOD" -Config "$CFG" -Main 'bad name!' -NonInteractive 2>&1)"; rc=$?
eq "недопустимое имя: код 2" "$rc" 2

echo
if [ "$FAILS" = 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED ($FAILS)"; exit 1; fi
