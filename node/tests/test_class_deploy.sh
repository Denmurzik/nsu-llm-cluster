#!/usr/bin/env bash
# Offline tests for node/class-deploy.ps1 (mass deployment over WinRM). No network, no real PCs:
# Invoke-Command, New-PSSession, Test-WSMan, Copy-Item -ToSession, New-PSDrive (SMB) and ports are stubbed by
# class-deploy-stubs.ps1; the "remote disks" are directories under $STUB_ROOT/<address>. Needs Windows PowerShell 5.1.
export MSYS_NO_PATHCONV=1
unset PSModulePath   # an inherited PowerShell 7 module path breaks autoloading in Windows PowerShell 5.1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing: $3)"; echo "$2" | sed 's/^/    | /' ;; esac; }
lacks() { case "$2" in *"$3"*) fail "$1 (unexpected: $3)"; echo "$2" | sed 's/^/    | /' ;; *) pass "$1" ;; esac; }

PS="$(command -v powershell || true)"
if [ -z "$PS" ]; then echo "skip - Windows PowerShell not found, class-deploy tests skipped"; exit 0; fi
W() { cygpath -m "$1"; }

SCRIPT="$(W "$NODE/class-deploy.ps1")"
STUBS="$(W "$HERE/class-deploy-stubs.ps1")"
export STUB_LOG="$(W "$TMP/stub.log")"
export STUB_ROOT="$(W "$TMP/remote")"
export NSU_LLM_ADMIN_USER='.\classadmin'
export NSU_LLM_ADMIN_PASSWORD='Pw-Secret-9x'
TOKEN='agent-token-SECRET-123'

# --- syntax
"$PS" -NoProfile -Command '$e=$null;$t=$null;[void][Management.Automation.Language.Parser]::ParseFile("'"$SCRIPT"'",[ref]$t,[ref]$e); if ($e.Count) { $e | % { "$($_.Extent.StartLineNumber): $($_.Message)" }; exit 1 }' \
  && pass "class-deploy.ps1 parses" || fail "class-deploy.ps1 parses"

# --- the shared helper node/secure-dir.ps1 has its own unit checks in test_install_node.sh; here it runs inside the
# class-deploy blocks with the directory SD and the move aside stubbed (class-deploy-stubs.ps1).
# The test tree gets a clean protected ACL (SYSTEM, Administrators, this user): a TEMP that other accounts can write to would
# make every "remote" dir untrusted for Secure-Dir, which then (correctly) refuses it.
ME_SID="$(/c/Windows/System32/whoami.exe /user /fo csv /nh 2>/dev/null | tr -d '\r"' | awk -F, '{print $2}')"
[ -n "$ME_SID" ] && icacls "$(cygpath -w "$TMP")" /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*${ME_SID}:(OI)(CI)F" >/dev/null

# --- fixtures
mkdir -p "$TMP/img" "$TMP/work" "$TMP/scripts"
for f in install-node.ps1 run-node.ps1 node-boot.ps1 wsl-setup.sh pull-image.sh; do echo "# fake $f" > "$TMP/scripts/$f"; done
echo "\$Version = '0.1.0'" >> "$TMP/scripts/install-node.ps1"
echo cpu-image > "$TMP/img/nsu-llm-node-0.1.0-cpu.tar.gz"
echo gpu-image-bigger > "$TMP/img/nsu-llm-node-0.1.0-gpu.tar.gz"
CPU="$(W "$TMP/img/nsu-llm-node-0.1.0-cpu.tar.gz")"; GPU="$(W "$TMP/img/nsu-llm-node-0.1.0-gpu.tar.gz")"
INV="$(W "$TMP/work/class-nodes.csv")"
cat > "$TMP/work/class-nodes.csv" <<'EOF'
name,address,mode,accel
pc01,192.168.3.101,k3s,cpu
pc02,192.168.3.102,k3s,auto
pc03,192.168.3.103,standalone,nvidia
EOF
COMMON="-ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -ImageTarCpu '$CPU' -ImageTarGpu '$GPU' -Main 10.0.0.5 -Token $TOKEN -AllowFrom '192.168.3.0/24,10.0.0.5' -NonInteractive"

scenario() { printf '%s' "$1" > "$TMP/scn.json"; }
reset() { rm -rf "$TMP/remote" "$TMP/work/class-deploy-logs" "$TMP/work/class-answers.json"; : > "$TMP/stub.log"; }
# run <args...>: out in $OUT, exit code in $RC
run() {
  local argline="$1"; local stdin="${2:-}"
  if [ -n "$stdin" ]; then OUT="$(printf '%b' "$stdin" | "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "$argline" 2>&1)"; RC=$?
  else OUT="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "$argline" 2>&1 < /dev/null)"; RC=$?; fi
  LOG="$(cat "$TMP/stub.log" 2>/dev/null)"
}
H3='"192.168.3.101":{%s},"192.168.3.102":{%s},"192.168.3.103":{%s}'

# ------------------------------------------------------------------ reachability and WinRM advice
reset
scenario '{"trusted":"","domain":false,"hosts":{"192.168.3.101":{},"192.168.3.102":{"tcp":false},"192.168.3.103":{"session":false}}}'
run "-Action check $COMMON"
[ $RC -eq 1 ] && pass "check: unreachable hosts -> exit 1" || fail "check exit code ($RC)"
contains "check: closed port reported" "$OUT" "порт 5985 закрыт"
contains "check: failed login reported with the cause" "$OUT" "Access is denied"
contains "check: prints the command that enables WinRM" "$OUT" "Enable-PSRemoting -Force -SkipNetworkProfileCheck"
contains "check: prints the local-account token policy fix" "$OUT" "LocalAccountTokenFilterPolicy"
contains "check: prints the exact TrustedHosts command with all addresses" "$OUT" "Set-Item WSMan:\\localhost\\Client\\TrustedHosts -Value '192.168.3.101,192.168.3.102,192.168.3.103' -Force"
contains "check: reachable host is ok in the table" "$OUT" "WinRM работает"
lacks "check: no remote run" "$LOG" "RUN "
lacks "check: no copy" "$LOG" "Copy-Item"
contains "check: sessions are closed" "$LOG" "Remove-PSSession 192.168.3.101"
contains "session: the user profile is loaded (USERPROFILE, .wslconfig, DPAPI)" "$LOG" "New-PSSession 192.168.3.101 user=.\\classadmin noprofile=False"
lacks "session: no -NoMachineProfile" "$LOG" "noprofile=True"

# admin PC in a domain, class PCs by name in a workgroup: Kerberos fails, TrustedHosts is offered and the login retried
reset
printf 'name,address,mode,accel\npc01,pc01,k3s,cpu\npc02,pc02,k3s,cpu\n' > "$TMP/work/names.csv"
scenario '{"trusted":"","domain":true,"hosts":{"pc01":{"needtrust":true},"pc02":{}}}'
run "-Action check -Inventory '$(W "$TMP/work/names.csv")'" "д\n"
contains "domain+workgroup: the failed login offers TrustedHosts" "$OUT" "Добавить их в TrustedHosts и повторить вход"
contains "domain+workgroup: only the failed PC is added" "$LOG" "Set-TrustedHosts pc01"
lacks "domain+workgroup: a domain PC by name is not added" "$LOG" "Set-TrustedHosts pc01,pc02"
[ $RC -eq 0 ] && pass "domain+workgroup: retry succeeds" || { fail "domain+workgroup: retry succeeds ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
reset
scenario '{"trusted":"","domain":true,"hosts":{"pc01":{"needtrust":true},"pc02":{}}}'
run "-Action check -Inventory '$(W "$TMP/work/names.csv")' -NonInteractive"
contains "domain+workgroup non-interactive: prints the TrustedHosts command" "$OUT" "Set-Item WSMan:\\localhost\\Client\\TrustedHosts -Value 'pc01' -Force"

# TrustedHosts: interactive yes applies the exact value
reset
scenario '{"trusted":"10.9.9.9","domain":false,"hosts":{"192.168.3.101":{},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action check -Inventory '$INV'" "д\n"
contains "trusted: offers to add the missing addresses" "$OUT" "TrustedHosts на этом ПК не содержит: 192.168.3.101"
contains "trusted: keeps existing entries and adds the new ones" "$LOG" "Set-TrustedHosts 10.9.9.9,192.168.3.101,192.168.3.102,192.168.3.103"
[ $RC -eq 0 ] && pass "trusted: check ok" || { fail "trusted: check ok ($RC)"; echo "$OUT" | sed 's/^/    | /'; }

# no credentials in non-interactive mode
reset
scenario '{"hosts":{}}'
OUT="$(NSU_LLM_ADMIN_USER= NSU_LLM_ADMIN_PASSWORD= "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "-Action status -Inventory '$INV' -NonInteractive" 2>&1 < /dev/null)"; RC=$?
[ $RC -eq 1 ] && pass "no credentials: exit 1" || fail "no credentials exit ($RC)"
contains "no credentials: tells how to pass them" "$OUT" "NSU_LLM_ADMIN_PASSWORD"

# a one-PC inventory is counted (PowerShell 5.1 has no .Count on a single object)
reset
printf 'name,address,mode,accel
pc01,192.168.3.101,k3s,cpu
' > "$TMP/work/one.csv"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{}}}'
run "-Action check -Inventory '$(W "$TMP/work/one.csv")' -NonInteractive"
contains "one-PC inventory: count shown" "$OUT" ": 1 ПК, действие check"

# ------------------------------------------------------------------ inventory: wizard and validation
reset
rm -f "$TMP/work/new.csv"
scenario '{"trusted":"*","hosts":{}}'
run "-Action check -Inventory '$(W "$TMP/work/new.csv")'" "192.168.3.101-103\n\n\n\n"
contains "wizard: announces the missing file" "$OUT" "не найден. Создаю его"
csv="$(cat "$TMP/work/new.csv" 2>/dev/null)"
contains "wizard: header" "$csv" "name"
contains "wizard: first host named by prefix" "$csv" "\"pc01\",\"192.168.3.101\",\"k3s\",\"auto\""
contains "wizard: range expanded to the last host" "$csv" "\"pc03\",\"192.168.3.103\""
contains "wizard: docker column defaults to wsl" "$csv" "\"pc01\",\"192.168.3.101\",\"k3s\",\"auto\",\"wsl\""
contains "wizard: the new inventory is used" "$OUT" "3 ПК"

reset
scenario '{"hosts":{}}'
run "-Action check -Inventory '$(W "$TMP/work/missing.csv")' -NonInteractive"
[ $RC -eq 1 ] && pass "non-interactive without inventory fails" || fail "non-interactive without inventory ($RC)"
contains "non-interactive without inventory: explains the format" "$OUT" "name,address,mode,accel"

printf 'name,address,mode,accel\npc01,10.0.0.1,k3s,auto\npc01,10.0.0.2,k3s,auto\n' > "$TMP/work/dup.csv"
run "-Action check -Inventory '$(W "$TMP/work/dup.csv")' -NonInteractive"
contains "inventory: duplicate name rejected" "$OUT" "имя pc01 повторяется"
printf 'name,address,mode,accel\nPC_1,10.0.0.1,k3s,auto\n' > "$TMP/work/bad.csv"
run "-Action check -Inventory '$(W "$TMP/work/bad.csv")' -NonInteractive"
contains "inventory: bad node name rejected" "$OUT" "недопустимое имя"
printf 'name,address,mode,accel\npc01,10.0.0.1,docker,auto\n' > "$TMP/work/bad.csv"
run "-Action check -Inventory '$(W "$TMP/work/bad.csv")' -NonInteractive"
contains "inventory: bad mode rejected" "$OUT" "режим 'docker'"

# ------------------------------------------------------------------ install
reset
scenario '{"trusted":"*","hosts":{
 "192.168.3.101":{"run":{"install":{"Lines":["[1/9] скопировать скрипты","WSL 2.6.1"],"Code":0,"Output":"Готово.\nВсё в порядке.\n"}}},
 "192.168.3.102":{"probe":{"HasGpu":true},"run":{"install":{"Code":3010,"Output":"Перезагрузите ПК и запустите установку снова\n"}}},
 "192.168.3.103":{"smb":false,"run":{"install":{"Code":1,"Output":"[2/9] x\nОШИБКА: Docker в WSL не запустился\nКак исправить: wsl -d NSU-LLM -u root -e journalctl -u docker -n 50\n"}}}}}'
mkdir -p "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage"; echo planted > "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage/install-node.ps1"
# "planted by someone else": Everyone may write it, so the stage tree is untrusted and must be reset
icacls "$(cygpath -w "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage/install-node.ps1")" /grant '*S-1-1-0:(M)' >/dev/null
run "-Action install $COMMON -Throttle 2"
[ $RC -eq 1 ] && pass "install: one failure -> exit 1" || fail "install exit code ($RC)"
contains "install: ok host" "$OUT" "pc01"
contains "install: summary counts" "$OUT" "Итого:"
contains "install: failed host shows the installer error and fix" "$OUT" "ОШИБКА: Docker в WSL не запустился"
contains "install: reboot host gets the repeat command" "$OUT" "Restart-Computer -ComputerName 192.168.3.102"
contains "install: reboot host repeat with -Only" "$OUT" "-Action install -Only pc02"
contains "install: status ПЕРЕЗАГРУЗКА" "$OUT" "ПЕРЕЗАГРУЗКА"
# arguments for the node installer
r1="$(echo "$LOG" | grep '^RUN 192.168.3.101')"
contains "install args: main" "$r1" "Main=10.0.0.5"
contains "install args: token passed to the installer" "$r1" "Token=$TOKEN"
contains "install args: node name" "$r1" "Name=pc01"
contains "install args: mode" "$r1" "Mode=k3s"
contains "install args: accel" "$r1" "Accel=cpu"
contains "install args: allow-from" "$r1" "AllowFrom=192.168.3.0/24,10.0.0.5"
contains "install args: non-interactive" "$r1" "NonInteractive=True"
contains "install args: task password is the admin password" "$r1" "TaskPassword=Pw-Secret-9x"
contains "install args: cpu image from the stage dir" "$r1" "ImageTar=C:\\ProgramData\\NSU-LLM-stage\\nsu-llm-node-0.1.0-cpu.tar.gz"
r2="$(echo "$LOG" | grep '^RUN 192.168.3.102')"
contains "install args: auto accel with a GPU gets the gpu image" "$r2" "nsu-llm-node-0.1.0-gpu.tar.gz"
contains "install args: auto accel stays auto for the installer" "$r2" "Accel=auto"
r3="$(echo "$LOG" | grep '^RUN 192.168.3.103')"
contains "install args: standalone mode" "$r3" "Mode=standalone"
contains "install: the default run mode is a scheduled task" "$r1" "mode=task"
contains "install args: nvidia accel gets the gpu image" "$r3" "nsu-llm-node-0.1.0-gpu.tar.gz"
contains "install args: Docker from the inventory, default wsl (no-logon autostart even with Docker Desktop)" "$r1" "Docker=wsl"
# the stage dir is secured on the PC before anything is copied (a student could pre-create it under C:\ProgramData)
contains "secure: stage dir secured on pc01" "$LOG" "SECURE 192.168.3.101 C:\\ProgramData\\NSU-LLM-stage"
contains "secure: stage dir owner Administrators, protected DACL: SYSTEM, Administrators full, Users read" "$LOG" "DirSec owner=S-1-5-32-544 protected=True ids=S-1-5-18:FullControl,S-1-5-32-544:FullControl,S-1-5-32-545:ReadAndExecute"
for a in 192.168.3.101 192.168.3.103; do
  sl="$(echo "$LOG" | grep -n "^SECURE $a " | head -1 | cut -d: -f1)"
  cl="$(echo "$LOG" | grep -n -F -e "New-PSDrive NSUND" -e "Copy-Item -ToSession $a " | grep -F -e "\\\\$a\\" -e "ToSession $a" | head -1 | cut -d: -f1)"
  [ -n "$sl" ] && [ -n "$cl" ] && [ "$sl" -lt "$cl" ] && pass "secure: $a secured before the first copy" || fail "secure: $a secured before the first copy (secure line $sl, copy line $cl)"
done
contains "secure: a planted file is moved aside, not fixed in place (an old handle keeps its rights)" "$LOG" "MoveAside install-node.ps1 -> install-node.ps1"
pq="$(ls -d "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage.untrusted-"* 2>/dev/null | head -1)"
contains "secure: the planted file sits in the side dir" "$(cat "$pq/install-node.ps1" 2>/dev/null)" "planted"
# files
st="$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage"
for f in install-node.ps1 run-node.ps1 node-boot.ps1 wsl-setup.sh pull-image.sh nsu-llm-node-0.1.0-cpu.tar.gz; do
  [ -f "$st/$f" ] && pass "smb copy: $f on pc01" || fail "smb copy: $f on pc01"
done
[ ! -f "$st/nsu-llm-node-0.1.0-gpu.tar.gz" ] && pass "smb copy: only the needed image" || fail "smb copy: only the needed image"
contains "copy: smb used for pc01" "$LOG" "Copy-Item smb nsu-llm-node-0.1.0-cpu.tar.gz"
contains "copy: smb share opened with the admin account" "$LOG" "New-PSDrive NSUND1 \\\\192.168.3.101\\C\$ user=.\\classadmin"
contains "copy: falls back to WinRM when SMB is closed" "$LOG" "Copy-Item -ToSession 192.168.3.103 nsu-llm-node-0.1.0-gpu.tar.gz"
[ -f "$TMP/remote/192.168.3.103/ProgramData/NSU-LLM-stage/nsu-llm-node-0.1.0-gpu.tar.gz" ] && pass "winrm copy: image landed" || fail "winrm copy: image landed"
# parallelism
maxrun="$(echo "$LOG" | grep '^START' | grep -v 'op=probe' | sed 's/.*running=//' | sort -n | tail -1)"
[ "$maxrun" = 2 ] && pass "throttle: at most 2 installs run at once (saw $maxrun)" || fail "throttle: expected max 2 got $maxrun"
# logs and secrets
lg="$TMP/work/class-deploy-logs"
n=$(ls "$lg"/*/pc0*.log 2>/dev/null | wc -l)
[ "$n" = 3 ] && pass "per-host logs written (3)" || fail "per-host logs written ($n)"
contains "log has the installer output" "$(cat "$lg"/*/pc03.log)" "Docker в WSL не запустился"
contains "progress: installer steps are shown live with the PC name" "$OUT" "pc01: [1/9] скопировать скрипты"
lacks "progress: other installer lines stay in the log" "$OUT" "WSL 2.6.1"
contains "progress: streamed lines land in the host log" "$(cat "$lg"/*/pc01.log)" "WSL 2.6.1"
[ -f "$lg"/*/summary.txt ] && pass "summary.txt written" || fail "summary.txt written"
alltext="$OUT$(cat "$lg"/*/* 2>/dev/null)"
lacks "token never in output or logs" "$alltext" "$TOKEN"
lacks "password never in output or logs" "$alltext" "Pw-Secret-9x"
# answers file
ans="$(cat "$TMP/work/class-answers.json" 2>/dev/null)"
contains "answers file: main" "$ans" "\"Main\":  \"10.0.0.5\""
contains "answers file: allow-from" "$ans" "192.168.3.0/24,10.0.0.5"
lacks "answers file: token is not plain" "$ans" "$TOKEN"
contains "answers file: token is DPAPI-protected" "$ans" "AgentTokenProtected"

# second run without parameters: everything comes from the answers file
reset_stub() { : > "$TMP/stub.log"; }
reset_stub
run "-Action status -Inventory '$INV' -NonInteractive"
lacks "status after install: no questions needed" "$OUT" "ОШИБКА"

# update: image already on the node is not copied again
reset_stub
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"probe":{"Installed":true}},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action update -ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -Only pc01 -NonInteractive -Yes"
[ $RC -eq 0 ] && pass "update: ok" || { fail "update: ok ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
contains "update: image already on the node" "$OUT" "nsu-llm-node-0.1.0-cpu.tar.gz уже на месте"
lacks "update: image not copied again" "$LOG" "Copy-Item smb nsu-llm-node-0.1.0-cpu"
contains "update: scripts refreshed" "$LOG" "Copy-Item smb install-node.ps1"
lacks "update -Only: other hosts untouched" "$LOG" "192.168.3.102"

# a stage dir that cannot be taken over, or a junction in its place: the PC is not touched
reset
mkdir -p "$TMP/remote/192.168.3.102/ProgramData" "$TMP/elsewhere"
cmd /c mklink /J "$(cygpath -w "$TMP/remote/192.168.3.102/ProgramData/NSU-LLM-stage")" "$(cygpath -w "$TMP/elsewhere")" > /dev/null
# pc01: a stage dir someone else may write to (Everyone), and moving it aside fails
mkdir -p "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage"
icacls "$(cygpath -w "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage")" /grant '*S-1-1-0:(OI)(CI)M' >/dev/null
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"aclfail":true},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action install $COMMON"
[ $RC -eq 1 ] && pass "secure refused: exit 1" || fail "secure refused: exit 1 ($RC)"
contains "secure refused: an untrusted stage that cannot be moved aside is reported" "$OUT" "отложить его в сторону не удалось"
contains "secure refused: moving the untrusted stage aside was tried" "$LOG" "MoveAside NSU-LLM-stage -> NSU-LLM-stage.untrusted-"
lacks "secure refused: nothing copied to pc01" "$LOG" "New-PSDrive NSUND1"
lacks "secure refused: installer not run on pc01" "$LOG" "RUN 192.168.3.101"
contains "secure refused: junction in place of the stage dir" "$OUT" "ссылка (junction или symlink)"
lacks "secure refused: nothing copied to pc02" "$LOG" "New-PSDrive NSUND2"
[ -z "$(ls -A "$TMP/elsewhere")" ] && pass "secure refused: junction target untouched" || fail "secure refused: junction target untouched"
contains "secure refused: the third PC still installs" "$LOG" "RUN 192.168.3.103"
cmd /c rmdir "$(cygpath -w "$TMP/remote/192.168.3.102/ProgramData/NSU-LLM-stage")" > /dev/null; rm -rf "$TMP/elsewhere"

# docker column: desktop passed through, an empty value is wsl, a bad value rejected
reset
printf 'name,address,mode,accel,docker\npc01,192.168.3.101,k3s,cpu,desktop\npc02,192.168.3.102,k3s,cpu,\n' > "$TMP/work/dk.csv"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},"192.168.3.102":{}}}'
run "-Action install -ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$(W "$TMP/work/dk.csv")' -ImageTarCpu '$CPU' -Main 10.0.0.5 -Token $TOKEN -AllowFrom 10.0.0.5 -NonInteractive"
contains "docker column: desktop passed to the installer" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "Docker=desktop"
contains "docker column: empty -> wsl" "$(echo "$LOG" | grep '^RUN 192.168.3.102')" "Docker=wsl"
printf 'name,address,mode,accel,docker\npc01,10.0.0.1,k3s,auto,podman\n' > "$TMP/work/bad.csv"
run "-Action check -Inventory '$(W "$TMP/work/bad.csv")' -NonInteractive"
contains "docker column: bad value rejected" "$OUT" "docker 'podman'"

# image already loaded on the PC (marker of the last successful run matches the tar): neither copied nor passed
mark() { "$PS" -NoProfile -Command "\$i = Get-Item -LiteralPath '$CPU'; '{\"Name\":\"' + \$i.Name + '\",\"Length\":' + (\$i.Length + $1) + ',\"Ticks\":' + \$i.LastWriteTimeUtc.Ticks + ',\"Docker\":\"wsl\"}'"; }
MS="$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage"
reset; mkdir -p "$MS"; mark 0 > "$MS/image-loaded.json"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"probe":{"Installed":true}}}}'
run "-Action install $COMMON -Only pc01"
[ $RC -eq 0 ] && pass "image loaded: install ok" || { fail "image loaded: install ok ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
lacks "image loaded: tar not passed to the installer" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "ImageTar="
lacks "image loaded: tar not copied" "$LOG" "Copy-Item smb nsu-llm-node-0.1.0-cpu"
contains "image loaded: scripts still refreshed" "$LOG" "Copy-Item smb install-node.ps1"
contains "image loaded: plan says so" "$OUT" "образ уже загружен"
reset; mkdir -p "$MS"; mark 0 > "$MS/image-loaded.json"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"probe":{"Installed":false}}}}'
run "-Action install $COMMON -Only pc01"
contains "image marker but node not installed: tar passed" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "ImageTar="
reset; mkdir -p "$MS"; mark 7 > "$MS/image-loaded.json"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"probe":{"Installed":true}}}}'
run "-Action install $COMMON -Only pc01"
contains "image marker of another tar: tar copied" "$LOG" "Copy-Item smb nsu-llm-node-0.1.0-cpu"
contains "image marker of another tar: tar passed" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "ImageTar="

# PowerShell 7 module path inherited by Windows PowerShell 5.1: no *-SecureString cmdlets, the saved token still works
P7='C:\Program Files\PowerShell\7\Modules;C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules'
if [ -d "/c/Program Files/PowerShell/7/Modules" ]; then
  reset
  scenario '{"trusted":"*","hosts":{"192.168.3.101":{}}}'
  OUT="$(PSModulePath="$P7" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "-Action install $COMMON -Only pc01" 2>&1 < /dev/null)"; RC=$?
  [ $RC -eq 0 ] && pass "pwsh 7 module path: install ok" || { fail "pwsh 7 module path: install ok ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
  lacks "pwsh 7 module path: no module load failure" "$OUT" "could not be loaded"
  contains "pwsh 7 module path: token saved" "$(cat "$TMP/work/class-answers.json" 2>/dev/null)" "AgentTokenProtected"
  : > "$TMP/stub.log"
  OUT="$(PSModulePath="$P7" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "-Action uninstall -Inventory '$INV' -Only pc01 -NonInteractive" 2>&1 < /dev/null)"; RC=$?
  contains "pwsh 7 module path: the saved token is decrypted" "$(grep '^RUN 192.168.3.101' "$TMP/stub.log")" "Token=$TOKEN"
else
  echo "skip - PowerShell 7 not installed: module path test skipped"
fi
# a token saved by an earlier version (ConvertFrom-SecureString) still decrypts
reset
"$PS" -NoProfile -Command "\$e = ConvertTo-SecureString '$TOKEN' -AsPlainText -Force | ConvertFrom-SecureString; '{\"Main\":\"10.0.0.5\",\"AgentTokenProtected\":\"' + \$e + '\"}'" > "$TMP/work/class-answers.json"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{}}}'
run "-Action uninstall -Inventory '$INV' -Only pc01 -NonInteractive"
contains "old answers file: token decrypted" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "Token=$TOKEN"

# ------------------------------------------------------------------ skips
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"probe":{"IsAdmin":false}},"192.168.3.102":{"probe":{"FreeGB":9}},"192.168.3.103":{"probe":{"Build":19045}}}}'
run "-Action install $COMMON"
[ $RC -eq 1 ] && pass "skips: exit 1" || fail "skips exit ($RC)"
contains "skip: not an admin" "$OUT" "не администратор ПК"
contains "skip: low disk" "$OUT" "на C: свободно 9"
contains "skip: old Windows" "$OUT" "Windows сборки 19045"
lacks "skips: nothing is copied or run" "$LOG" "RUN "

# a job that dies: the tail of run.log is fetched from the PC into the host log
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"jobfail":true,"tail":"[5/9] загрузить образ ноды\nобрыв на середине"},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action install $COMMON"
contains "dead job: reason shown" "$OUT" "remote session broke"
contains "dead job: other hosts still finish" "$OUT" "[3/3]"
contains "dead job: run.log tail fetched" "$LOG" "TAIL 192.168.3.101 C:\\ProgramData\\NSU-LLM-stage\\run.log"
contains "dead job: tail in the host log" "$(cat "$TMP/work/class-deploy-logs"/*/pc01.log)" "[5/9] загрузить образ ноды"

# a job that hangs: local timeout, the job is stopped, the tail is fetched
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"hang":true,"tail":"[3/9] установить дистрибутив"},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action install $COMMON -Only pc01 -RunMode session -TimeoutMin 0.02"
contains "hang: reported as not finished in time" "$OUT" "не завершилось за"
contains "hang: job stopped" "$LOG" "Stop-Job"
contains "hang: tail in the host log" "$(cat "$TMP/work/class-deploy-logs"/*/pc01.log)" "[3/9] установить дистрибутив"

# ------------------------------------------------------------------ GPU driver older than 570, GPU not visible in WSL
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},
 "192.168.3.102":{"probe":{"HasGpu":true,"Driver":"566.36"}},
 "192.168.3.103":{"probe":{"HasGpu":true},"run":{"install":{"Code":1,"Output":"Внимание: GPU не виден в WSL — нода будет cpu.\nОШИБКА: После загрузки образа nsu-llm-node:0.1.0-cpu нет\n"}}}}}'
run "-Action install $COMMON -Only pc02,pc03"
contains "old driver: plan says the PC goes cpu" "$OUT" "драйвер старее 570: cpu"
contains "old driver: auto host gets the cpu image" "$(echo "$LOG" | grep '^RUN 192.168.3.102')" "nsu-llm-node-0.1.0-cpu.tar.gz"
contains "old driver: the cpu image is copied" "$LOG" "Copy-Item smb nsu-llm-node-0.1.0-cpu.tar.gz"
contains "GPU not in WSL: the error suggests accel=cpu in the CSV" "$OUT" "accel=cpu"
contains "GPU not in WSL: and a rerun for that PC" "$OUT" "-Only pc03"

# free space: an image already staged on the PC is not counted twice
reset
mkdir -p "$TMP/big" "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage"
sparse() { : > "$1"; fsutil sparse setflag "$(cygpath -w "$1")" > /dev/null 2>&1; truncate -s "$2" "$1"; }
sparse "$TMP/big/nsu-llm-node-0.1.0-cpu.tar.gz" 1536M
BIG="$(W "$TMP/big/nsu-llm-node-0.1.0-cpu.tar.gz")"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{"probe":{"FreeGB":17}}}}'
run "-Action install -ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -ImageTarCpu '$BIG' -Main 10.0.0.5 -Token $TOKEN -AllowFrom 10.0.0.5 -NonInteractive -Only pc01 -DryRun"
contains "free space: 1.5 GB image, 17 GB free, nothing staged -> skipped" "$OUT" "нужно около 18"
sparse "$TMP/remote/192.168.3.101/ProgramData/NSU-LLM-stage/nsu-llm-node-0.1.0-cpu.tar.gz" 1536M
run "-Action install -ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -ImageTarCpu '$BIG' -Main 10.0.0.5 -Token $TOKEN -AllowFrom 10.0.0.5 -NonInteractive -Only pc01 -DryRun"
lacks "free space: the same image already staged is subtracted" "$OUT" "ПРОПУЩЕН"
contains "free space: planned" "$OUT" "ПЛАН"
rm -rf "$TMP/big"

# image discovery picks the version of install-node.ps1, not just the newest tar
reset
mkdir -p "$TMP/scripts2"; cp "$TMP/scripts"/* "$TMP/scripts2/"
echo v010 > "$TMP/scripts2/nsu-llm-node-0.1.0-cpu.tar.gz"
echo v009 > "$TMP/scripts2/nsu-llm-node-0.0.9-cpu.tar.gz"; touch -d '+1 hour' "$TMP/scripts2/nsu-llm-node-0.0.9-cpu.tar.gz"
scenario '{"trusted":"*","hosts":{"192.168.3.101":{}}}'
run "-Action install -ScriptsFrom '$(W "$TMP/scripts2")' -Inventory '$INV' -Main 10.0.0.5 -Token $TOKEN -AllowFrom 10.0.0.5 -NonInteractive -Only pc01 -DryRun"
contains "image discovery: matches \$Version of install-node.ps1" "$OUT" "nsu-llm-node-0.1.0-cpu.tar.gz"
lacks "image discovery: a newer tar of another version is ignored" "$OUT" "0.0.9"

# ------------------------------------------------------------------ status
reset
scenario '{"trusted":"*","hosts":{
 "192.168.3.101":{"run":{"status":{"Code":0,"Output":"[ok] WSL 2.6.1\nВсё в порядке.\n"}}},
 "192.168.3.102":{"run":{"status":{"Code":1,"Output":"[ok] WSL 2.6.1\n[!!] контейнер llm-node не создан\n     исправить: x\n[!!] Нет правила брандмауэра\n"}}},
 "192.168.3.103":{}}}'
run "-Action status -Inventory '$INV' -NonInteractive"
[ $RC -eq 1 ] && pass "status: a problem -> exit 1" || fail "status exit ($RC)"
contains "status: healthy node" "$OUT" "всё в порядке"
contains "status: problems counted and first one shown" "$OUT" "2: контейнер llm-node не создан"
contains "status: runs the installer with -Status" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "Status=True"
lacks "status: does not copy anything" "$LOG" "Copy-Item"
lacks "status: does not need the answers" "$OUT" "токен"

# ------------------------------------------------------------------ uninstall
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action uninstall $COMMON -Only pc01"
[ $RC -eq 0 ] && pass "uninstall: ok" || { fail "uninstall ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
u="$(echo "$LOG" | grep '^RUN 192.168.3.101')"
contains "uninstall args: flag" "$u" "Uninstall=True"
contains "uninstall args: cluster removal needs main and token" "$u" "Main=10.0.0.5"
contains "uninstall: stage dir removed afterwards" "$u" "cleanup=all"
contains "uninstall: result" "$OUT" "удалено"

# interactive confirmation can be declined
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action uninstall -Inventory '$INV' -Main 10.0.0.5 -Token $TOKEN" "н\n"
contains "uninstall: asks for confirmation" "$OUT" "Выполнить на 3 ПК?"
contains "uninstall: declining changes nothing" "$OUT" "Отменено, ничего не изменено"
lacks "uninstall: declined -> no run" "$LOG" "RUN "

# ------------------------------------------------------------------ dry-run
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action install $COMMON -DryRun"
[ $RC -eq 0 ] && pass "dry-run: exit 0" || fail "dry-run exit ($RC)"
contains "dry-run: prints the plan with the installer command" "$OUT" "install-node.ps1 -Main 10.0.0.5"
contains "dry-run: token masked" "$OUT" "-Token ***"
lacks "dry-run: secrets not printed" "$OUT" "$TOKEN"
run "-Action install $COMMON -DryRun -Proxy http://u:Px-Pass-7@proxy.nsu.ru:3128"
lacks "dry-run: proxy password not printed" "$OUT" "Px-Pass-7"
contains "dry-run: proxy shown masked" "$OUT" "http://u:***@proxy.nsu.ru:3128"
lacks "dry-run: password not printed" "$OUT" "Pw-Secret-9x"
lacks "dry-run: nothing copied" "$LOG" "Copy-Item"
lacks "dry-run: nothing run" "$LOG" "RUN "
[ ! -f "$TMP/work/class-answers.json" ] && pass "dry-run: answers file not written" || fail "dry-run: answers file not written"

# ------------------------------------------------------------------ missing answers
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{}}}'
run "-Action install -ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -ImageTarCpu '$CPU' -ImageTarGpu '$GPU' -Main 10.0.0.5 -NonInteractive"
[ $RC -eq 1 ] && pass "install without token fails" || fail "install without token ($RC)"
contains "install without token: names the way to give it" "$OUT" "NSU_LLM_AGENT_TOKEN"
reset
run "-Action install -Inventory '$INV' -Main 10.0.0.5 -Token $TOKEN -ImageTarCpu '$(W "$TMP/img/nope.tar")' -ImageTarGpu '$GPU' -NonInteractive"
contains "install with a missing image file explains" "$OUT" "не найден"

# no archives: the PCs pull the image from main (new default); -ImageSource tar demands archives; -ImageSource main ignores them
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},"192.168.3.102":{}}}'
NOTAR="-ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -Main 10.0.0.5 -Token $TOKEN -AllowFrom 10.0.0.5 -NonInteractive -Only pc01"
run "-Action install $NOTAR"
[ $RC -eq 0 ] && pass "install without archives succeeds: the image comes from main" || { fail "install without archives succeeds ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
r1="$(echo "$LOG" | grep '^RUN 192.168.3.101')"
contains "no archives: the installer is told to pull from main" "$r1" "ImageSource=main"
lacks "no archives: no ImageTar passed" "$r1" "ImageTar="
contains "no archives: says the PCs pull the image from main" "$OUT" "скачают его с главного"
lacks "no archives: no image copied to the PC" "$LOG" "Copy-Item smb nsu-llm-node"
reset
run "-Action install $NOTAR -ImageSource tar"
[ $RC -ne 0 ] && contains "-ImageSource tar without archives: error naming the file" "$OUT" "Не задан файл образа" || fail "-ImageSource tar without archives fails ($RC)"
reset
run "-Action install $NOTAR -ImageSource main -ImageTarCpu '$CPU' -ImageTarGpu '$GPU'"
r1="$(echo "$LOG" | grep '^RUN 192.168.3.101')"
contains "-ImageSource main: pull from main although archives exist" "$r1" "ImageSource=main"
lacks "-ImageSource main: archives are not passed" "$r1" "ImageTar="
lacks "-ImageSource main: archives are not copied" "$LOG" "Copy-Item smb nsu-llm-node"
reset
run "-Action install $NOTAR -ImageTarCpu '$CPU' -ImageTarGpu '$GPU'"
r1="$(echo "$LOG" | grep '^RUN 192.168.3.101')"
contains "with archives the old path stays: the archive is passed" "$r1" "ImageTar=C:\ProgramData\NSU-LLM-stage\nsu-llm-node-0.1.0-cpu.tar.gz"
lacks "with archives the installer is not told to pull from main" "$r1" "ImageSource="

# interactive questions with defaults: main, token, allow-from (default shown), credentials
reset
scenario '{"trusted":"*","hosts":{"192.168.3.101":{},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action install -ScriptsFrom '$(W "$TMP/scripts")' -Inventory '$INV' -ImageTarCpu '$CPU' -ImageTarGpu '$GPU' -Only pc01" "10.0.0.5\n$TOKEN\n\nд\n"
contains "interactive: asks for the main" "$OUT" "Адрес главного сервера"
contains "interactive: allow-from default is built from the inventory and main" "$OUT" "[192.168.3.0/24,10.0.0.5]"
contains "interactive: plan shown before the confirmation" "$OUT" "План (install)"
[ $RC -eq 0 ] && pass "interactive install ok" || { fail "interactive install ($RC)"; echo "$OUT" | sed 's/^/    | /'; }
contains "interactive: default allow-from used" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "AllowFrom=192.168.3.0/24,10.0.0.5"

# ------------------------------------------------------------------ the real remote block runs against a fake installer
# The stub gives every address its own copy of the stage dir ($STUB_ROOT/<address>-stage) and sets the process execution policy to AllSigned,
# as on a fresh PC. Task mode really starts powershell.exe for the "scheduled task" (the runner reads the DPAPI LocalMachine parameters file).
FAKE="$TMP/fakestage"
mkfake() {
  rm -rf "$FAKE" "$TMP/remote"; mkdir -p "$FAKE"
  { printf '\xEF\xBB\xBF'; cat <<'EOF'; } > "$FAKE/install-node.ps1"
param([string]$Main, [string]$Token, [string]$Mode, [string]$Accel, [string]$Name, [string]$AllowFrom, [string]$ImageTar, [string]$ImageSource, [string]$TaskPassword, [string]$Proxy, [string]$Docker,
      [switch]$NonInteractive, [switch]$Yes, [switch]$InsecureTls, [switch]$Status, [switch]$Uninstall)
Write-Host '[1/2] шаг один'
Write-Host "installer got Main=$Main Name=$Name Mode=$Mode Accel=$Accel NI=$NonInteractive Tar=$ImageTar Proxy=$($PSBoundParameters['Proxy']) HTTPS_PROXY=$env:HTTPS_PROXY policy=$(Get-ExecutionPolicy -Scope Process)"
if ($Token -ne 'agent-token-SECRET-123' -or $TaskPassword -ne 'Pw-Secret-9x') { Write-Host 'ОШИБКА: secrets lost'; exit 1 }
if ($Name -eq 'pc02') { Write-Host 'ОШИБКА: boom'; exit 1 }
if ($env:FAKE_NOIMAGE -and -not $ImageTar) { Write-Host 'ОШИБКА: Образ nsu-llm-node:0.1.0-cpu не найден ни в WSL, ни рядом со скриптом'; Write-Host 'Как исправить: Положите nsu-llm-node-0.1.0-cpu.tar.gz рядом с install-node.ps1 или укажите -ImageTar <путь>'; exit 1 }
if ($Name -eq 'pc03' -and $env:FAKE_SLOW) { Start-Sleep -Seconds 8 }
Write-Host '[2/2] шаг два'
exit 0
EOF
  echo cpu-image > "$FAKE/nsu-llm-node-0.1.0-cpu.tar.gz"
}
LOCALRUN="$COMMON -CopyMode none -StageDir '$(W "$FAKE")' -Proxy http://proxy.nsu.ru:3128"
R="$TMP/remote"

# task mode (default)
reset; mkfake
scenario '{"trusted":"*","exec":"local","hosts":{"192.168.3.101":{},"192.168.3.102":{},"192.168.3.103":{}}}'
run "-Action install $LOCALRUN"
lg="$(ls -d "$TMP/work/class-deploy-logs"/* | head -1)"
log1="$(cat "$lg/pc01.log" 2>/dev/null)"
contains "task: installer output captured into the host log" "$log1" "installer got Main=10.0.0.5 Name=pc01 Mode=k3s Accel=cpu NI=True Tar="
contains "task: secrets reach the installer through the protected file" "$log1" "[2/2] шаг два"
lacks "task: secrets not lost" "$log1" "secrets lost"
contains "task: runs with -ExecutionPolicy Bypass" "$log1" "policy=Bypass"
contains "task: proxy passed to an installer that declares -Proxy, environment left to it" "$log1" "Proxy=http://proxy.nsu.ru:3128 HTTPS_PROXY= policy"
contains "task: progress shown live" "$OUT" "pc01: [1/2] шаг один"
reg="$(echo "$LOG" | grep '^Register-ScheduledTask' | head -1)"
contains "task: registered as the class PC admin with its password, elevated" "$reg" "Register-ScheduledTask \\NSU-LLM\\NSU-LLM-Deploy user=$COMPUTERNAME\\classadmin runlevel=Highest pwset=True exe=powershell.exe"
contains "task: action runs powershell with -ExecutionPolicy Bypass" "$reg" "-ExecutionPolicy Bypass"
lacks "task: no token on the task command line" "$reg" "$TOKEN"
lacks "task: no password on the task command line" "$reg" "Pw-Secret-9x"
acl="$(echo "$LOG" | grep -m1 '^AdminOnly run-params\.bin ')"
contains "task: parameters file created new with its restricted ACL (never inherits Users read)" "$acl" "exists=False"
contains "task: parameters file limited to Administrators and SYSTEM, ACL not inherited" "$acl" "ids=S-1-5-18:FullControl,S-1-5-32-544:FullControl protected=True"
contains "task: the stage dir is secured on the PC before anything is written (protected DACL)" "$LOG" "DirSec owner=keep protected=True ids=S-1-5-18:FullControl,S-1-5-32-544:FullControl,S-1-5-32-545:ReadAndExecute"
s1="$(echo "$LOG" | grep -n '^DirSec ' | head -1 | cut -d: -f1)"; p1="$(echo "$LOG" | grep -n '^AdminOnly run-params' | head -1 | cut -d: -f1)"
[ -n "$s1" ] && [ -n "$p1" ] && [ "$s1" -lt "$p1" ] && pass "task: stage secured before the parameters file" || fail "task: stage secured before the parameters file ($s1 $p1)"
contains "task: image marker kept on the PC after a successful install" "$(cat "$R/192.168.3.101-stage/image-loaded.json" 2>/dev/null)" '"Name":"nsu-llm-node-0.1.0-cpu.tar.gz"'
[ ! -f "$R/192.168.3.102-stage/image-loaded.json" ] && pass "task: no image marker after a failure" || fail "task: no image marker after a failure"
contains "task: started" "$LOG" "Start-ScheduledTask \\NSU-LLM\\NSU-LLM-Deploy"
contains "task: removed afterwards" "$LOG" "Unregister-ScheduledTask \\NSU-LLM\\NSU-LLM-Deploy"
contains "task: failed host shows the installer error" "$OUT" "ОШИБКА: boom"
[ -f "$R/192.168.3.101-stage/run.log" ] && pass "task: run.log stays on the PC" || fail "task: run.log stays on the PC"
contains "task: run.log has the installer output" "$(cat "$R/192.168.3.101-stage/run.log" 2>/dev/null)" "[2/2] шаг два"
[ ! -f "$R/192.168.3.101-stage/run-params.bin" ] && pass "task: parameters file deleted" || fail "task: parameters file deleted"
[ ! -f "$R/192.168.3.101-stage/nsu-llm-node-0.1.0-cpu.tar.gz" ] && pass "cleanup: tar removed after a successful install" || fail "cleanup: tar removed after a successful install"
[ ! -f "$R/192.168.3.101-stage/install-node.ps1" ] && pass "cleanup: scripts removed after a successful install" || fail "cleanup: scripts removed after a successful install"
[ -f "$R/192.168.3.102-stage/nsu-llm-node-0.1.0-cpu.tar.gz" ] && pass "cleanup: kept after a failure (rerun needs it)" || fail "cleanup: kept after a failure"
leak="$(grep -rl --exclude=install-node.ps1 "$TOKEN" "$R" 2>/dev/null)"   # the fake installer itself compares against the token
[ -z "$leak" ] && pass "task: token not left in plain text on the PC" || fail "task: token left in plain text on the PC: $leak"

# -KeepStage keeps the image
reset; mkfake
run "-Action install $LOCALRUN -Only pc01 -KeepStage"
[ -f "$R/192.168.3.101-stage/nsu-llm-node-0.1.0-cpu.tar.gz" ] && pass "-KeepStage: tar kept" || fail "-KeepStage: tar kept"
[ ! -f "$R/192.168.3.101-stage/install-node.ps1" ] && pass "-KeepStage: scripts still removed" || fail "-KeepStage: scripts still removed"

# remote deadline: the block stops waiting, reports, and the log tail is in the host log
reset; mkfake
OUT="$(FAKE_SLOW=1 "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "-Action install $LOCALRUN -Only pc03 -TimeoutMin 0.05" 2>&1 < /dev/null)"; RC=$?
contains "task deadline: reported" "$OUT" "не завершилось за"
contains "task deadline: what has run so far is in the host log" "$(cat "$TMP/work/class-deploy-logs"/*/pc03.log 2>/dev/null)" "[1/2] шаг один"
for i in $(seq 1 30); do [ -f "$R/192.168.3.103-stage/run.done" ] && break; sleep 1; done

# a task still running from an earlier run is not started twice
# (covered by the remote block: Get-ScheduledTask reports Running -> error with Stop-ScheduledTask advice)

# the image was removed from the node meanwhile: the installer asks for -ImageTar, the marker is dropped, the next run copies the tar
reset; mkfake
FT="$CPU"   # the marker names the tar on the admin PC
FM="$("$PS" -NoProfile -Command "\$i = Get-Item -LiteralPath '$FT'; '{\"Name\":\"' + \$i.Name + '\",\"Length\":' + \$i.Length + ',\"Ticks\":' + \$i.LastWriteTimeUtc.Ticks + ',\"Docker\":\"wsl\"}'")"
printf '%s' "$FM" > "$FAKE/image-loaded.json"
FMJ="$(printf '%s' "$FM" | sed 's/"/\\"/g')"
scenario '{"trusted":"*","exec":"local","hosts":{"192.168.3.101":{"probe":{"Installed":true,"ImageMark":"'"$FMJ"'"}}}}'
OUT="$(FAKE_NOIMAGE=1 "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "-Action install $LOCALRUN -Only pc01" 2>&1 < /dev/null)"; RC=$?
LOG="$(cat "$TMP/stub.log")"
lacks "image gone: run without -ImageTar" "$(echo "$LOG" | grep '^RUN 192.168.3.101')" "ImageTar="
[ ! -f "$R/192.168.3.101-stage/image-loaded.json" ] && pass "image gone: marker dropped" || fail "image gone: marker dropped"
contains "image gone: the next run copies the tar again" "$OUT" "архив будет скопирован заново"

# the one-time task runs node/class-run.ps1 (the single runner), not an embedded copy
reset; mkfake
rm -rf "$TMP/cd"; mkdir -p "$TMP/cd"; cp "$NODE/class-deploy.ps1" "$NODE/secure-dir.ps1" "$NODE/class-run.ps1" "$TMP/cd/"
sed -i 's/^\$code = 1$/$code = 1; [IO.File]::AppendAllText($Log, "runner=file`r`n", $enc)/' "$TMP/cd/class-run.ps1"
scenario '{"trusted":"*","exec":"local","hosts":{"192.168.3.101":{}}}'
OUT="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$(W "$TMP/cd/class-deploy.ps1")" -ArgLine "-Action install $LOCALRUN -Only pc01" 2>&1 < /dev/null)"; RC=$?
contains "runner: node/class-run.ps1 is what the task runs" "$(cat "$R/192.168.3.101-stage/run.log" 2>/dev/null)" "runner=file"
rm -rf "$TMP/cd"

# session mode: in-process, execution policy set for the process
reset; mkfake
run "-Action install $LOCALRUN -RunMode session"
log1="$(cat "$TMP/work/class-deploy-logs"/*/pc01.log 2>/dev/null)"
contains "session: installer runs despite AllSigned (process policy Bypass)" "$log1" "policy=Bypass"
contains "session: installer output captured into the host log" "$log1" "installer got Main=10.0.0.5 Name=pc01 Mode=k3s Accel=cpu NI=True Tar="
contains "session: proxy passed to the installer" "$log1" "Proxy=http://proxy.nsu.ru:3128 HTTPS_PROXY= policy"
contains "session: progress shown live" "$OUT" "pc01: [2/2] шаг два"
contains "session: exit code 1 becomes a failure with the message" "$(cat "$TMP/work/class-deploy-logs"/*/pc02.log)" "ОШИБКА: boom"
lacks "session: no scheduled task" "$LOG" "Register-ScheduledTask"
contains "session: run.log written on the PC too" "$(cat "$R/192.168.3.101-stage/run.log" 2>/dev/null)" "[2/2] шаг два"

# an installer without -Proxy still gets the proxy through HTTP(S)_PROXY, in both modes
for m in task session; do
  reset; mkfake; sed -i 's/\[string\]\$Proxy, *//' "$FAKE/install-node.ps1"
  run "-Action install $LOCALRUN -Only pc01 -RunMode $m"
  log1="$(cat "$TMP/work/class-deploy-logs"/*/pc01.log 2>/dev/null)"
  contains "$m: old installer: proxy through the environment" "$log1" "Proxy= HTTPS_PROXY=http://proxy.nsu.ru:3128 policy"
done

reset; mkfake; rm -f "$FAKE/install-node.ps1"
run "-Action install $LOCALRUN"
contains "remote block: no scripts on the node is explained" "$OUT" "файлы не раздавались"

# the installed copy C:\ProgramData\NSU-LLM\scripts\install-node.ps1 runs as admin only from a trusted NSU-LLM dir
PD="$TMP/pd"; mkdir -p "$PD/NSU-LLM/scripts"
cp "$NODE/tests/../install-node.ps1" "$PD/NSU-LLM/scripts/install-node.ps1" 2>/dev/null || echo '# installed' > "$PD/NSU-LLM/scripts/install-node.ps1"
icacls "$(cygpath -w "$PD/NSU-LLM/scripts/install-node.ps1")" /grant '*S-1-1-0:(M)' >/dev/null
reset; mkfake; rm -f "$FAKE/install-node.ps1"
OUT="$(ProgramData="$(cygpath -w "$PD")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$STUBS" -Scenario "$(W "$TMP/scn.json")" -Script "$SCRIPT" -ArgLine "-Action install $LOCALRUN" 2>&1 < /dev/null)"
contains "remote block: an installed copy in an untrusted NSU-LLM dir is not run" "$OUT" "установленную копию install-node.ps1 запускать нельзя"
rm -rf "$PD"

# ------------------------------------------------------------------ interface agreement with install-node.ps1 (task 2-5, written in parallel)
if [ -f "$NODE/install-node.ps1" ]; then
  names="$("$PS" -NoProfile -Command '$e=$null;$t=$null;$a=[Management.Automation.Language.Parser]::ParseFile("'"$(W "$NODE/install-node.ps1")"'",[ref]$t,[ref]$e); ($a.ParamBlock.Parameters | % { $_.Name.VariablePath.UserPath }) -join " "')"
  for p in Main Token Mode Accel Docker Name AllowFrom ImageTar ImageSource TaskPassword NonInteractive Yes InsecureTls Status Uninstall; do
    case " $names " in *" $p "*) pass "install-node.ps1 has -$p" ;; *) fail "install-node.ps1 has no -$p (class-deploy passes it)" ;; esac
  done
else
  echo "skip - node/install-node.ps1 not present in this tree (task 2-5 is parallel); interface check runs once it lands"
fi

echo
if [ $FAILS -eq 0 ]; then echo "all class-deploy tests passed"; else echo "$FAILS class-deploy test(s) failed"; exit 1; fi
