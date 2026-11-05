#!/usr/bin/env bash
# Offline tests of the node update on Windows (task 2-30): run-node.ps1 -Update and the update loop of node-boot.ps1.
# wsl, docker and curl are fake commands first in PATH (.bat files handing their arguments to bash scripts); the node's data
# directory is a temp directory. Needs Windows PowerShell: elsewhere the whole file is skipped.
# Run it through the test lock of the machine (docs: recovery rules), it starts a few PowerShell processes.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
before() { local s="${1%%"$2"*}"; echo ${#s}; }
contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing: $3)"; echo "$2" | sed 's/^/    | /' ;; esac; }
lacks() { case "$2" in *"$3"*) fail "$1 (unexpected: $3)"; echo "$2" | sed 's/^/    | /' ;; *) pass "$1" ;; esac; }

PS="$(command -v powershell || true)"
if [ -z "$PS" ]; then
  echo "skip - Windows PowerShell not found, the update tests are skipped"
  echo; echo "ALL PASSED"; exit 0
fi
BASHW="$(cygpath -w "$(command -v bash)")"
FB="$TMP/fakebin"; LB="$TMP/lbin"; mkdir -p "$FB" "$LB"
for c in wsl docker; do
  printf '@echo off\r\n"%s" "%s" %%*\r\nexit /b %%ERRORLEVEL%%\r\n' "$BASHW" "$(cygpath -w "$FB/$c.sh")" > "$FB/$c.bat"
done
export FAKE_LOG="$(cygpath -w "$TMP/fake.log")" FAKE_DOCKER="$FB/docker.sh" FAKE_LBIN="$LB"

cat > "$FB/docker.sh" <<'EOS'
#!/usr/bin/env bash
# fake docker: FAKE_INSPECT_JSON (file) is `docker inspect llm-node`; FAKE_FAIL=<subcommand> makes that subcommand fail
echo "docker $*" >> "$FAKE_LOG"
[ "$1" = "${FAKE_FAIL:-}" ] && { echo "docker: $1 failed" >&2; exit 3; }
case "$1" in
  inspect) [ -f "${FAKE_INSPECT_JSON:-/nonexistent}" ] && { cat "$FAKE_INSPECT_JSON"; exit 0; }; exit 1 ;;
  image)
    case "$*" in
      *RepoDigests*) echo "[10.9.0.5/nsu-llm-node@sha256:$(printf 'cd%.0s' $(seq 32))]" ;;
      # (the MSYS runtime of bash drops the braces of a Go template on the way from PowerShell: match on the field name)
      *".Id"*) echo "sha256:$(printf '1f%.0s' $(seq 32))" ;;
    esac; exit 0 ;;
  info) echo 17179869184; exit 0 ;;
esac
exit 0
EOS
# curl of the distro (bash); curl.exe of Windows is a PowerShell script below: the MSYS runtime of bash would expand an argument @file
# (the header file) into its content before the fake sees it, and would drop the backslash of the -w format
cat > "$LB/curl" <<'EOS'
#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
prev=""; body=""
for a in "$@"; do
  [ "$prev" = -H ] && case "$a" in @*) printf 'header-file: %s\n' "$(cat "${a#@}" 2>/dev/null)" >> "$FAKE_LOG" ;; esac
  [ "$prev" = -d ] && case "$a" in @*) body="$(cat "${a#@}" 2>/dev/null)"; printf 'body: %s\n' "$body" >> "$FAKE_LOG" ;; esac
  prev="$a"
done
case "${*: -1}" in
  *"/desired?"*) [ "${FAKE_CURL_DOWN:-0}" = 1 ] && { echo "curl: (7) Failed to connect" >&2; exit 7; }; cat "$FAKE_PLAN"; printf '\n200'; exit 0 ;;
  *"/image") printf '{"ok":true}\n200'; exit 0 ;;
esac
exit 0
EOS
cat > "$FB/curl-fake.ps1" <<'EOS'
# fake curl.exe of Windows: logs its command line (and the header and body files, whose names it was given) to FAKE_LOG
$log = $env:FAKE_LOG
$utf8 = New-Object System.Text.UTF8Encoding $false
function Log([string]$t) { [IO.File]::AppendAllText($log, $t + "`n", $utf8) }
Log ('curl ' + ($args -join ' '))
for ($i = 0; $i -lt $args.Count - 1; $i++) {
    $a = [string]$args[$i + 1]
    if ($args[$i] -eq '-H' -and $a.StartsWith('@')) { Log ('header-file: ' + ([IO.File]::ReadAllText($a.Substring(1)).Trim())) }
    if ($args[$i] -eq '-d' -and $a.StartsWith('@')) { Log ('body: ' + ([IO.File]::ReadAllText($a.Substring(1), $utf8).Trim())) }
}
$url = [string]$args[$args.Count - 1]
if ($url -like '*/desired?*') {
    if ($env:FAKE_CURL_DOWN -eq '1') { [Console]::Error.WriteLine('curl: (7) Failed to connect'); exit 7 }
    [Console]::Out.Write([IO.File]::ReadAllText($env:FAKE_PLAN) + "`n200")
    exit 0
}
if ($url -like '*/image') { [Console]::Out.Write("{`"ok`":true}`n200"); exit 0 }
exit 0
EOS
printf '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%s" %%*\r\nexit /b %%ERRORLEVEL%%\r\n' "$(cygpath -w "$FB/curl-fake.ps1")" > "$FB/curl.bat"
cat > "$FB/wsl.sh" <<'EOS'
#!/usr/bin/env bash
# fake wsl.exe: docker goes to the fake docker, `bash -c SCRIPT` runs here with the fake curl, `bash FILE ...` is pull-image.sh
echo "wsl $*" >> "$FAKE_LOG"
[ "$1" = -d ] && shift 2
[ "$1" = -u ] && shift 2
[ "$1" = -e ] && shift
case "$1" in
  docker) shift; exec bash "$FAKE_DOCKER" "$@" ;;
  systemctl) exit 0 ;;
  sleep) sleep "${FAKE_SLEEP:-0}"; exit 0 ;;
  bash)
    if [ "$2" = -c ]; then shift 2; PATH="$FAKE_LBIN:$PATH" bash -c "$1"; exit $?; fi
    [ "${FAKE_FAIL:-}" = pull ] && { echo "pull-image.sh: failed" >&2; exit 1; }
    exit 0 ;;
esac
exit 0
EOS
chmod +x "$FB"/*.sh "$LB/curl"
# Docker Desktop: run-node.ps1 secures its data dir (Secure-Dir); a test run is not elevated and cannot make Administrators the owner
export NSU_LLM_TEST_NO_SD_OWNER=1
export NSU_LLM_CURL="$(cygpath -w "$FB/curl.bat")"

DIG_OLD="sha256:$(printf '11%.0s' $(seq 32))"
DIG_NEW="sha256:$(printf 'cd%.0s' $(seq 32))"
inspect_json() { # NODE_IMAGE_DIGEST network mount-source -> docker inspect output of the node container
  cat > "$TMP/inspect.json" <<EOS
[{"Config":{"Image":"nsu-llm-node:0.2.1-cpu","Env":["MODE=k3s","MAIN_HOST=10.9.0.5","CLUSTER_TOKEN=clustersecret42","NODE_NAME=pc01","NODE_IP=10.1.2.3","ACCEL=cpu","WOL_MAC=00-1A-2B-3C-4D-5E","WOL_IP=10.1.2.3","WOL_PREFIX=24","NODE_IMAGE_DIGEST=${1:-$DIG_OLD}","PATH=/usr/bin"]},
  "HostConfig":{"NetworkMode":"${2:-default}"},
  "Mounts":[{"Destination":"/var/lib/llm-node","Source":"${3:-/run/desktop/mnt/host/c/ProgramData/nsu-llm-node/state}"}]}]
EOS
  export FAKE_INSPECT_JSON="$(cygpath -w "$TMP/inspect.json")"
}
plan() { # go [digest] [reason]
  printf '{"image":{"name":"nsu-llm-node:0.2.1-cpu","digest":"%s","version":"0.2.1","revision":"abc"},"update":{"go":%s,"mode":"requested","when":"now","waiting":false,"reason":"%s"}}' \
    "${2:-$DIG_NEW}" "$1" "${3:-}" > "$TMP/plan.json"
  export FAKE_PLAN="$(cygpath -w "$TMP/plan.json")"
}
RN="$(cygpath -w "$NODE/run-node.ps1")"
DATA="$TMP/desktop-data"; mkdir -p "$DATA/state"
printf 'llm.nsu/accel=cpu' > "$DATA/state/labels"
upd() { # [run-node args...] -> output; every fake call lands in fake.log
  : > "$TMP/fake.log"
  PATH="$FB:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RN" -Update -DataDir "$(cygpath -w "$DATA")" "$@" 2>&1
}
flog() { cat "$TMP/fake.log"; }

# ---- Docker Desktop (no WSL): docker.exe and curl.exe
unset FAKE_INSPECT_JSON
plan 1
out="$(upd)"; rc=$?
[ $rc -eq 0 ] && pass "update without a container exits quietly" || fail "update without a container exits quietly (rc=$rc)"
contains "update without a container has nothing to do" "$out" "Контейнера llm-node нет"
lacks "update without a container asks nobody" "$(flog)" "curl"

inspect_json; plan 0 "$DIG_NEW" "нода уже на актуальном образе"
out="$(upd)"; rc=$?
[ $rc -eq 0 ] && pass "update: main says no, exit 0" || fail "update: main says no, exit 0 (rc=$rc)"
contains "update: main says no and why" "$out" "Обновление не требуется (нода уже на актуальном образе)"
lacks "update: nothing is pulled" "$(flog)" "docker pull"
lacks "update: the container stays" "$(flog)" "docker rm"
contains "update asks main for this node and its accel" "$(flog)" "https://10.9.0.5/api/nodes/pc01/desired?accel=cpu"
contains "update: the token goes in a header file" "$(flog)" "header-file: Authorization: Bearer clustersecret42"
lacks "update: the token is not on the curl command line" "$(flog | grep '^curl ')" "clustersecret42"
lacks "update: no node key yet: no X-Node-Key" "$(flog)" "X-Node-Key"
NK="$(printf 'K%.0s' $(seq 43))"; mkdir -p "$DATA/state"; printf '%s
' "$NK" > "$DATA/state/node.key"
out="$(upd)"
contains "update: with node.key the key goes next to the token" "$(flog)" "X-Node-Key: $NK"
contains "update: and the token stays" "$(flog)" "Authorization: Bearer clustersecret42"
lacks "update: the key is not on the curl command line" "$(flog | grep '^curl ')" "$NK"

plan 1
out="$(upd)"; rc=$?
[ $rc -eq 0 ] && pass "update: main says go, the container is replaced" || { fail "update: main says go, the container is replaced (rc=$rc)"; echo "$out"; }
log="$(flog)"
contains "update pulls the image from main by the digest main named, not by a tag that can be overwritten" "$log" "docker pull 10.9.0.5/nsu-llm-node@$DIG_NEW"
lacks "update does not pull by tag" "$log" "docker pull 10.9.0.5/nsu-llm-node:0.2.1-cpu"
contains "update gives the pulled image its local name" "$log" "docker tag 10.9.0.5/nsu-llm-node@$DIG_NEW nsu-llm-node:0.2.1-cpu"
contains "update drops the remote name" "$log" "docker rmi 10.9.0.5/nsu-llm-node@$DIG_NEW"
[ "$(before "$log" "docker pull")" -lt "$(before "$log" "docker rm -f llm-node")" ] && [ "$(before "$log" "docker rm -f llm-node")" -lt "$(before "$log" "docker run")" ] \
  && pass "update: pull, then remove, then run" || fail "update: pull, then remove, then run"
run="$(grep '^docker run' "$TMP/fake.log")"
for want in "--name llm-node" "--hostname pc01" "--restart unless-stopped" "-e MODE=k3s" "-e MAIN_HOST=10.9.0.5" "-e NODE_NAME=pc01" "-e NODE_IP=10.1.2.3" "-e ACCEL=cpu" \
    "-e WOL_MAC=00-1A-2B-3C-4D-5E -e WOL_IP=10.1.2.3 -e WOL_PREFIX=24" "-e NODE_IMAGE=nsu-llm-node:0.2.1-cpu" "-e CLUSTER_TOKEN "; do
  contains "update re-creates the container with the same parameter: $want" "$run" "$want"
done
lacks "update: the token is not on the docker command line" "$run" "clustersecret42"
lacks "update: the firewall is left alone" "$out" "NetFirewallRule"
contains "update says what happened" "$out" "обновлена и запущена"
rec="$(cat "$DATA/state/image.digest" 2>/dev/null)"
want_rec="sha256:$(printf '1f%.0s' $(seq 32)) $DIG_NEW"
[ "$rec" = "$want_rec" ] && pass "update records the digest main's registry gave the image, with the image id" || fail "update records the digest (got '$rec')"
contains "update hands the digest to the container" "$run" "-e NODE_IMAGE_DIGEST=$DIG_NEW"

inspect_json "$DIG_NEW"; plan 1
out="$(upd)"
contains "update: the container already runs the offered image" "$out" "уже работает на образе"
lacks "update: nothing is pulled when the image is the same" "$(flog)" "docker pull"

inspect_json; plan 1
out="$(FAKE_FAIL=pull upd)"; rc=$?
[ $rc -ne 0 ] && pass "update: a failed pull fails the script" || fail "update: a failed pull fails the script"
lacks "update: a failed pull leaves the container alone" "$(flog)" "docker rm"
contains "update: a failed pull is reported to main" "$(flog)" "https://10.9.0.5/api/nodes/pc01/image"
contains "update: the report says the container was not touched" "$(flog)" "контейнер не тронут"
contains "update: the report carries update_error" "$(flog)" '"update_error":'
lacks "update: the token is not in the report" "$(flog | grep '^body:')" "clustersecret42"

out="$(FAKE_FAIL=run upd)"; rc=$?
[ $rc -ne 0 ] && pass "update: a failed docker run fails the script" || fail "update: a failed docker run fails the script"
contains "update: a failed docker run is reported (the old container is gone)" "$(flog)" "контейнер не удалось создать заново"
lacks "update: a failed run does not say the node runs" "$out" "обновлена и запущена"

out="$(FAKE_CURL_DOWN=1 upd)"; rc=$?
[ $rc -eq 0 ] && pass "update: main does not answer, exit 0" || fail "update: main does not answer, exit 0 (rc=$rc)"
contains "update: main does not answer, nothing changes" "$out" "Главный не ответил"
lacks "update: main does not answer, no docker rm" "$(flog)" "docker rm"

# an image whose digest is not the one main named (the registry answered with something else) is refused before the container is touched
inspect_json; plan 1 "sha256:$(printf 'ee%.0s' $(seq 32))"
out="$(upd)"; rc=$?
[ $rc -ne 0 ] && pass "update: an image of another digest than main named is refused" || fail "update: an image of another digest than main named is refused"
contains "update: the refusal says the digest differs" "$out" "образ отклонён, контейнер не тронут"
lacks "update: a refused image does not replace the container" "$(flog)" "docker rm -f llm-node"
contains "update: the refused remote name is dropped" "$(flog)" "docker rmi 10.9.0.5/nsu-llm-node@sha256:$(printf 'ee%.0s' $(seq 32))"
printf '{"image":{"name":"evil:1","digest":"%s"},"update":{"go":true}}' "$DIG_NEW" > "$TMP/plan.json"
out="$(upd)"; rc=$?
[ $rc -ne 0 ] && pass "update refuses an image name that is not ours" || fail "update refuses an image name that is not ours"
contains "update names the bad image" "$out" "недопустимый образ"
lacks "update: a foreign image is not pulled" "$(flog)" "docker pull"
printf '{"image":{"name":"nsu-llm-node:0.2.1-cpu","digest":"sha256:short"},"update":{"go":true}}' > "$TMP/plan.json"
out="$(upd)"; rc=$?
[ $rc -ne 0 ] && pass "update refuses a malformed digest" || fail "update refuses a malformed digest"

plan 1
out="$(upd -DryRun)"; rc=$?
[ $rc -eq 0 ] && pass "update -DryRun exits 0" || { fail "update -DryRun exits 0 (rc=$rc)"; echo "$out"; }
contains "update -DryRun prints the pull by digest" "$out" "docker pull 10.9.0.5/nsu-llm-node@$DIG_NEW"
contains "update -DryRun prints docker run" "$out" "docker run -d --name llm-node"
lacks "update -DryRun runs no docker pull" "$(flog)" "docker pull"
lacks "update -DryRun removes nothing" "$(flog)" "docker rm"
lacks "update -DryRun sends no failure report" "$(flog)" "/image"

# the old way keeps working: -Main is still required for an install, -Update needs none
out="$(PATH="$FB:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RN" -DryRun -Token T 2>&1)"; rc=$?
[ $rc -ne 0 ] && contains "install without -Main is refused" "$out" "Параметр -Main обязателен"
out="$(PATH="$FB:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RN" -Update -Uninstall 2>&1)"; rc=$?
[ $rc -ne 0 ] && contains "-Update with -Uninstall is refused" "$out" "вместе не используются"

# ---- inside WSL: docker through wsl.exe, the API call and the pull as bash in the distro
WDATA="$TMP/wsl-data"; mkdir -p "$WDATA/state"; echo CA > "$WDATA/state/ca.crt"
inspect_json "$DIG_OLD" host "$WDATA/state"; plan 1
updw() { : > "$TMP/fake.log"; PATH="$FB:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RN" -Update -Wsl NSU-LLM "$@" 2>&1; }
out="$(updw)"; rc=$?
[ $rc -eq 0 ] && pass "wsl update: main says go, the container is replaced" || { fail "wsl update: main says go (rc=$rc)"; echo "$out"; }
log="$(flog)"
contains "wsl update reads the container through wsl as root" "$log" "wsl -d NSU-LLM -u root -e docker inspect llm-node"
contains "wsl update asks main from the distro with the pinned CA" "$log" "--cacert $WDATA/state/ca.crt"
contains "wsl update asks about this node" "$log" "https://10.9.0.5/api/nodes/pc01/desired?accel=cpu"
contains "wsl update: the token goes in a header file in the distro" "$log" "header-file: Authorization: Bearer clustersecret42"
contains "wsl update pulls with pull-image.sh in the distro" "$log" "wsl -d NSU-LLM -u root -e bash /mnt/"
contains "wsl update gives pull-image.sh the registry address and the record file" "$log" "pull-image.sh --main 10.9.0.5 --image nsu-llm-node:0.2.1-cpu --digest $DIG_NEW --no-tofu --record $WDATA/state/image.digest --ca $WDATA/state/ca.crt"
contains "wsl update re-creates the container in the distro" "$log" "wsl -d NSU-LLM -u root -e docker run -d --name llm-node"
contains "wsl update keeps the host network the container had" "$(grep 'docker run' "$TMP/fake.log")" "--network host"
lacks "wsl update: the token is not on the wsl command line" "$(flog | grep '^wsl ')" "clustersecret42"
out="$(FAKE_FAIL=pull updw)"; rc=$?
[ $rc -ne 0 ] && pass "wsl update: a failed pull fails" || fail "wsl update: a failed pull fails"
contains "wsl update: a failed pull is reported with the body in a file (no quotes on the wsl command line)" "$(flog)" 'body: {"version":"","revision":"","digest":"","tag":"","update_error":"образ не удалось скачать'
lacks "wsl update: the wsl command lines carry no double quotes" "$(flog | grep '^wsl ' | grep -v 'bash -c')" '"'

# ---- a fresh run takes the image from main (its NODE_IMAGE_TAG), not from a constant of the script (found on the 122b bed: main ran
# 0.1.1, the script checked the GPU with the image nsu-llm-node:0.2.1-gpu and failed at the last step)
unset FAKE_INSPECT_JSON
FRESH="$TMP/fresh-data"; mkdir -p "$FRESH"
fresh() { # accel -> output of a real (not dry) first run on Docker Desktop
  : > "$TMP/fake.log"
  PATH="$FB:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RN" -Main 10.9.0.5 -Token clustersecret42 -Mode standalone -Accel "$1" \
    -NodeIp 10.1.2.3 -Name pc01 -DataDir "$(cygpath -w "$FRESH")" 2>&1
}
printf '{"name":"nsu-llm-node:0.1.1-gpu","image":null,"update":{"go":false,"mode":null,"when":null,"waiting":false,"reason":""}}' > "$TMP/plan.json"
export FAKE_PLAN="$(cygpath -w "$TMP/plan.json")"
out="$(fresh nvidia)"; rc=$?
[ $rc -eq 0 ] && pass "fresh run on a GPU: the node starts" || { fail "fresh run on a GPU: the node starts (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
log="$(flog)"
contains "fresh run: main is asked for the name of the gpu image" "$log" "/api/nodes/pc01/desired?accel=nvidia"
contains "fresh run: the GPU is checked with the image main names" "$log" "docker run --rm --pull never --gpus all --entrypoint nvidia-smi nsu-llm-node:0.1.1-gpu"
contains "fresh run: that very image is started" "$log" "-e NODE_IMAGE=nsu-llm-node:0.1.1-gpu"
lacks "fresh run: the constant of the script is not used" "$log" "nsu-llm-node:0.2.1"
lacks "fresh run: the token is not on a command line" "$(printf '%s\n' "$log" | grep '^curl')" "clustersecret42"
printf '{"name":"nsu-llm-node:0.1.1-cpu","image":null,"update":{"go":false}}' > "$TMP/plan.json"
out="$(fresh cpu)"
contains "fresh cpu run: the cpu image of main's tag" "$(flog)" "-e NODE_IMAGE=nsu-llm-node:0.1.1-cpu"
printf '{"name":"evil:1"}' > "$TMP/plan.json"
out="$(fresh cpu)"
contains "a name that is not ours is ignored: IMAGE_VERSION of the scripts" "$(flog)" "-e NODE_IMAGE=nsu-llm-node:0.2.1-cpu"
export FAKE_CURL_DOWN=1
out="$(fresh cpu)"
contains "main does not answer: IMAGE_VERSION of the scripts" "$(flog)" "-e NODE_IMAGE=nsu-llm-node:0.2.1-cpu"
unset FAKE_CURL_DOWN

# ---- node-boot.ps1: the loop asks for updates while the distro runs
BD="$TMP/boot"; mkdir -p "$BD"
cp "$NODE/node-boot.ps1" "$BD/node-boot.ps1"
cat > "$BD/run-node.ps1" <<'EOS'
param([switch]$Update, [switch]$SyncFirewall, [string]$Wsl = '')
Add-Content -Path $env:FAKE_RUNNODE_LOG -Value "run-node Update=$Update SyncFirewall=$SyncFirewall Wsl=$Wsl"
if ($SyncFirewall) { Write-Output 'Правило брандмауэра NSU-LLM-Node-RPC: TCP 18050 с 10.9.0.5,10.1.1.7.'; return }
Write-Output 'Обновление не требуется.'
EOS
: > "$TMP/runnode.log"; : > "$TMP/fake.log"
out="$(PATH="$FB:$PATH" FAKE_SLEEP=4 FAKE_RUNNODE_LOG="$(cygpath -w "$TMP/runnode.log")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$BD/node-boot.ps1")" \
  -Distro NSU-LLM -Log "$(cygpath -w "$TMP/boot.log")" -MaxRuns 1 -PauseSec 0 -UpdateEverySec 1 2>&1)"
log="$(flog)"
contains "boot starts docker" "$log" "wsl -d NSU-LLM -u root -e systemctl start docker"
contains "boot keeps the distro alive" "$log" "wsl -d NSU-LLM -u root -e sleep infinity"
contains "boot asks for updates while the distro runs" "$(cat "$TMP/runnode.log")" "run-node Update=True SyncFirewall=False Wsl=NSU-LLM"
contains "boot keeps the RPC firewall in step with the split group" "$(cat "$TMP/runnode.log")" "run-node Update=False SyncFirewall=True Wsl=NSU-LLM"
contains "boot logs a change of the firewall" "$(cat "$TMP/boot.log" 2>/dev/null)" "брандмауэр: Правило брандмауэра NSU-LLM-Node-RPC"
contains "boot logs the first answer" "$(cat "$TMP/boot.log" 2>/dev/null)" "обновление: Обновление не требуется."
[ "$(grep -c 'обновление:' "$TMP/boot.log")" = 1 ] && pass "boot does not repeat the same answer in its log" || fail "boot does not repeat the same answer in its log ($(grep -c 'обновление:' "$TMP/boot.log"))"
contains "boot says when the distro stopped" "$(cat "$TMP/boot.log")" "остановлен (код 0)"
: > "$TMP/runnode.log"
out="$(PATH="$FB:$PATH" FAKE_SLEEP=3 FAKE_RUNNODE_LOG="$(cygpath -w "$TMP/runnode.log")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$BD/node-boot.ps1")" \
  -Distro NSU-LLM -Log "$(cygpath -w "$TMP/boot2.log")" -MaxRuns 1 -PauseSec 0 -UpdateEverySec 0 2>&1)"
[ ! -s "$TMP/runnode.log" ] && pass "boot with -UpdateEverySec 0 never asks for updates" || fail "boot with -UpdateEverySec 0 never asks for updates"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
