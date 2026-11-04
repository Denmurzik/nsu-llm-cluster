#!/usr/bin/env bash
# Offline tests for run-main.sh and run-main.ps1: dry runs and runs against a fake docker; no image, no real container.
# The k3s airgap archive comes from a local fake release (NSU_LLM_K3S_RELEASE_URL=file://...).
set -uo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$(cd "$HERE/.." && pwd)"
RUN_MAIN="${RUN_MAIN:-$MAIN/run-main.sh}"
RUN_MAIN_PS1="${RUN_MAIN_PS1:-$MAIN/run-main.ps1}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { # check NAME CONDITION-EXIT-CODE [DETAIL]
  if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi
}
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
fileurl() { local p; p="$(winpath "$1")"; case "$p" in /*) echo "file://$p" ;; *) echo "file:///$p" ;; esac; }
sha() { sha256sum "$1" | awk '{print $1}'; }

RUN_ENV="$TMP/run.env"; : >"$RUN_ENV"
K3S_FILE=k3s-airgap-images-amd64.tar.zst

# ------------------------------------------------------------------ dry run: documented docker command
out="$(bash "$RUN_MAIN" --dry-run --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 --airgap-dir "$TMP/none" 2>&1)"
for frag in '--name llm-main' '--hostname llm-main' '--privileged' '--restart unless-stopped' '--tmpfs /run' '--tmpfs /var/run' \
  '-p 443:443' '-p 80:80' '-p 6443:6443' 'llm-main-k3s:/var/lib/rancher/k3s' 'llm-main-kubelet:/var/lib/kubelet' \
  'llm-main-data:/var/lib/llm' 'llm-main-hf:/var/lib/llm-hf' 'llm-main-registry:/var/lib/llm-registry' '--env-file' 'MAIN_ADVERTISE_IP=192.168.3.21' 'nsu-llm-main:0.1.0' \
  '--ulimit nofile=1048576:1048576' '--log-driver json-file' '--log-opt max-size=50m' '--log-opt max-file=5'; do
  grep -qF -- "$frag" <<<"$out"
  check "run-main.sh --dry-run contains $frag" $? "$out"
done
! grep -qF -- "$K3S_FILE" <<<"$out"
check "run-main.sh without --airgap downloads nothing" $? "$out"

# NVIDIA GPU of the host: passed on for the status page, nothing when nvidia-smi is not there or fails
mkdir -p "$TMP/fakegpu" "$TMP/fakegpu-bad"
printf '#!/bin/sh
printf "NVIDIA GeForce RTX 4090, 24564 MiB\nNVIDIA GeForce RTX 4090, 24564 MiB\n"
' >"$TMP/fakegpu/nvidia-smi"
printf '#!/bin/sh
echo "NVIDIA-SMI has failed" >&2
exit 9
' >"$TMP/fakegpu-bad/nvidia-smi"
chmod +x "$TMP/fakegpu/nvidia-smi" "$TMP/fakegpu-bad/nvidia-smi"
out="$(PATH="$TMP/fakegpu:$PATH" bash "$RUN_MAIN" --dry-run --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 2>&1)"
grep -qF -- 'MAIN_GPU=NVIDIA GeForce RTX 4090, 24564 MiB; NVIDIA GeForce RTX 4090, 24564 MiB' <<<"$(tr -d '\\' <<<"$out")"
check "run-main.sh passes the host's NVIDIA GPUs as MAIN_GPU" $? "$out"
out="$(PATH="$TMP/fakegpu-bad:$PATH" bash "$RUN_MAIN" --dry-run --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 2>&1)"
! grep -q MAIN_GPU <<<"$out"
check "run-main.sh without a working nvidia-smi passes no MAIN_GPU" $? "$out"
out="$(bash "$RUN_MAIN" --dry-run --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 2>&1)"
! command -v nvidia-smi >/dev/null 2>&1 && { ! grep -q MAIN_GPU <<<"$out"; check "run-main.sh without nvidia-smi passes no MAIN_GPU" $? "$out"; }

# under Git Bash docker.exe gets Windows paths (cygpath -w), not /c/...
mkdir -p "$TMP/fakecyg"
printf '#!/bin/sh\n[ "$1" = "-w" ] && shift\necho "W:${1#/}"\n' >"$TMP/fakecyg/cygpath"
chmod +x "$TMP/fakecyg/cygpath"
out="$(PATH="$TMP/fakecyg:$PATH" bash "$RUN_MAIN" --dry-run --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 --certs "$TMP" 2>&1)"
grep -qE -- "--env-file W:[^ ]*run.env" <<<"$out" && ! grep -qE -- "--env-file /" <<<"$out" && grep -qE -- "-v W:[^ ]*:/certs:ro" <<<"$out"
check "run-main.sh converts host paths with cygpath under Git Bash" $? "$out"
out="$(bash "$RUN_MAIN" --dry-run --uninstall 2>&1)"
grep -q 'docker volume rm llm-main-k3s llm-main-kubelet llm-main-data llm-main-hf llm-main-registry' <<<"$out"
check "run-main.sh --uninstall removes the volumes" $? "$out"

# ------------------------------------------------------------------ airgap: k3s system images of the image's k3s version
want_ver="$(sed -n 's/^ARG K3S_VERSION=//p' "$MAIN/Dockerfile")"
sh_ver="$(sed -n 's/^K3S_VERSION="\(.*\)"$/\1/p' "$RUN_MAIN")"
ps_ver="$(sed -n "s/^\$K3sVersion = '\(.*\)'.*$/\1/p" "$RUN_MAIN_PS1" | tr -d '\r')"
[ -n "$want_ver" ] && [ "$sh_ver" = "$want_ver" ] && [ "$ps_ver" = "$want_ver" ]
check "run-main.sh and run-main.ps1 pin the k3s version of main/Dockerfile ($want_ver)" $? "sh=$sh_ver ps1=$ps_ver"

out="$(bash "$RUN_MAIN" --dry-run --airgap --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 --airgap-dir "$TMP/ag" 2>&1)"
grep -qF "https://github.com/k3s-io/k3s/releases/download/v1.36.5%2Bk3s1/$K3S_FILE" <<<"$out" \
  && grep -qF 'sha256sum-amd64.txt' <<<"$out" && grep -qF 'docker save --platform linux/amd64' <<<"$out" \
  && grep -qF -- ':/var/lib/rancher/k3s/agent/images:ro' <<<"$out"
check "run-main.sh --dry-run --airgap downloads the k3s archive of the pinned release, checks sha256, mounts the dir" $? "$out"

# fake release: archive + sha256sum-amd64.txt (good and bad sums)
REL="$TMP/release"; mkdir -p "$REL/good" "$REL/bad"
head -c 4096 /dev/urandom >"$REL/good/$K3S_FILE"
cp "$REL/good/$K3S_FILE" "$REL/bad/$K3S_FILE"
printf '%s  k3s\n%s  %s\n' "$(sha "$REL/good/$K3S_FILE")" "$(sha "$REL/good/$K3S_FILE")" "$K3S_FILE" >"$REL/good/sha256sum-amd64.txt"
printf '%s  %s\n' "0000000000000000000000000000000000000000000000000000000000000000" "$K3S_FILE" >"$REL/bad/sha256sum-amd64.txt"

# fake docker: logs every call; the container does not exist yet; everything else succeeds
FAKEBIN="$TMP/fakebin"; mkdir -p "$FAKEBIN"
export FAKE_DOCKER_LOG; FAKE_DOCKER_LOG="$(winpath "$TMP/docker.log")"
export FAKE_DOCKER_RC=0
cat >"$FAKEBIN/docker" <<'EOS'
#!/usr/bin/env bash
echo "$*" >>"$FAKE_DOCKER_LOG"
case "$1" in
  inspect) exit 1 ;;
  info) [ "${2:-}" = "--format" ] && echo 17179869184; exit 0 ;;
  pull) exit "$FAKE_DOCKER_RC" ;;
  exec) [ -n "${FAKE_AGENT_TOKEN:-}" ] || { echo "Токен агента ещё не записан: k3s запускается." >&2; exit 1; }; echo "$FAKE_AGENT_TOKEN" ;;
  *) exit 0 ;;
esac
EOS
chmod +x "$FAKEBIN/docker"
printf '@echo off\r\necho %%*>>"%%FAKE_DOCKER_LOG%%"\r\nif "%%1"=="inspect" exit /b 1\r\nif "%%1"=="exec" (if defined FAKE_AGENT_TOKEN (echo %%FAKE_AGENT_TOKEN%%& exit /b 0) else exit /b 1)\r\nif "%%1 %%2"=="info --format" (echo 17179869184& exit /b 0)\r\nif "%%1"=="pull" exit /b %%FAKE_DOCKER_RC%%\r\nexit /b 0\r\n' >"$FAKEBIN/docker.cmd"
[ "$(PATH="$FAKEBIN:$PATH" command -v docker)" = "$FAKEBIN/docker" ] || { echo "fake docker is not first in PATH, stopping"; exit 1; }

rm_run() { # rm_run RELEASE-DIR AIRGAP-DIR: run-main.sh --airgap against the fake docker and the fake release
  : >"$FAKE_DOCKER_LOG"
  PATH="$FAKEBIN:$PATH" NSU_LLM_K3S_RELEASE_URL="$(fileurl "$1")" \
    bash "$RUN_MAIN" --airgap --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 --airgap-dir "$2" 2>&1
}
out="$(rm_run "$REL/good" "$TMP/ag1")"; rc=$?
[ "$rc" = 0 ] && cmp -s "$REL/good/$K3S_FILE" "$TMP/ag1/$K3S_FILE" && ! ls "$TMP/ag1" | grep -q '\.part$' \
  && grep -q '^pull --platform linux/amd64 ghcr.io/berriai/litellm:v1.106.6' "$FAKE_DOCKER_LOG" \
  && grep -q '^run -d --name llm-main .*/var/lib/rancher/k3s/agent/images:ro' "$FAKE_DOCKER_LOG"
check "run-main.sh --airgap: archive with a matching sha256 lands in the images dir, then images and the container" $? "rc=$rc $out
$(cat "$FAKE_DOCKER_LOG")"
out="$(rm_run "$REL/good" "$TMP/ag1")"; rc=$?
[ "$rc" = 0 ] && grep -q 'уже скачаны' <<<"$out"
check "run-main.sh --airgap: an archive already in place with the right sha256 is not downloaded again" $? "rc=$rc $out"
out="$(rm_run "$REL/bad" "$TMP/ag2")"; rc=$?
[ "$rc" != 0 ] && grep -q 'контрольная сумма' <<<"$out" && [ -z "$(ls -A "$TMP/ag2" 2>/dev/null)" ] \
  && ! grep -q '^run ' "$FAKE_DOCKER_LOG"
check "run-main.sh --airgap: sha256 mismatch -> Russian error, nothing kept, no container" $? "rc=$rc $out
$(cat "$FAKE_DOCKER_LOG")"

# ------------------------------------------------------------------ token for nodes: the agent token, from agent-token.sh
AGENT_T='K10fake::node:agent42'
rm_start() { : >"$FAKE_DOCKER_LOG"; PATH="$FAKEBIN:$PATH" NSU_LLM_TOKEN_WAIT_S=0 \
  bash "$RUN_MAIN" --env-file "$RUN_ENV" --advertise-ip 192.168.3.21 --airgap-dir "$TMP/none" 2>&1; }
out="$(FAKE_AGENT_TOKEN="$AGENT_T" rm_start)"; rc=$?
[ "$rc" = 0 ] && grep -qF "$AGENT_T" <<<"$out" && grep -qF -- "--main 192.168.3.21 --token $AGENT_T" <<<"$out" \
  && grep -q '^exec llm-main /opt/llm/main/agent-token.sh$' "$FAKE_DOCKER_LOG"
check "run-main.sh prints the agent token (agent-token.sh) and a node command after the start" $? "rc=$rc $out
$(cat "$FAKE_DOCKER_LOG")"
out="$(rm_start)"; rc=$?
[ "$rc" = 0 ] && grep -qF -- '--print-token' <<<"$out" && grep -q 'Ноды' <<<"$out"
check "run-main.sh: token not ready yet -> start still succeeds, hint to --print-token and the Nodes page" $? "rc=$rc $out"
out="$(FAKE_AGENT_TOKEN="$AGENT_T" PATH="$FAKEBIN:$PATH" NSU_LLM_TOKEN_WAIT_S=0 bash "$RUN_MAIN" --print-token 2>/dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$out" = "$AGENT_T" ]
check "run-main.sh --print-token prints only the agent token" $? "rc=$rc out=$out"
out="$(PATH="$FAKEBIN:$PATH" NSU_LLM_TOKEN_WAIT_S=0 bash "$RUN_MAIN" --print-token 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q 'Ошибка: токен подключения нод' <<<"$out"
check "run-main.sh --print-token before k3s wrote the token -> Russian error, non-zero exit" $? "rc=$rc $out"
out="$(bash "$RUN_MAIN" --dry-run --print-token 2>&1)"
grep -q '^docker exec llm-main /opt/llm/main/agent-token.sh *$' <<<"$out"
check "run-main.sh --dry-run --print-token shows the docker exec command" $? "$out"

# ------------------------------------------------------------------ run-main.ps1 (Windows PowerShell)
PS="$(command -v powershell.exe || command -v pwsh || true)"
if [ -z "$PS" ]; then
  echo "skip - run-main.ps1: no PowerShell"
else
  ps_main() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(winpath "$RUN_MAIN_PS1")" "$@" 2>&1 | tr -d '\r'; }
  out="$(ps_main -DryRun -EnvFile "$(winpath "$RUN_ENV")" -AdvertiseIp 192.168.3.21 -Airgap -AirgapDir "$(winpath "$TMP/psag")")"
  grep -qF -- '--ulimit nofile=1048576:1048576' <<<"$out" \
    && grep -qF "https://github.com/k3s-io/k3s/releases/download/v1.36.5%2Bk3s1/$K3S_FILE" <<<"$out" \
    && grep -qF 'sha256sum-amd64.txt' <<<"$out" && grep -qF 'docker save --platform linux/amd64' <<<"$out" \
    && grep -qF 'llm-main-hf:/var/lib/llm-hf' <<<"$out"
  check "run-main.ps1 -DryRun -Airgap: k3s archive of the pinned release with sha256, images, nofile limit, mirror volume" $? "$out"

  [ "$(PATH="$FAKEBIN:$PATH" "$PS" -NoProfile -Command '(Get-Command docker).Source' | tr -d '\r' | tr '\\' '/')" = "$(winpath "$FAKEBIN")/docker.cmd" ] \
    || { echo "fake docker.cmd is not first for PowerShell, stopping"; exit 1; }
  ps_run() { # ps_run RELEASE-DIR AIRGAP-DIR
    : >"$FAKE_DOCKER_LOG"
    PATH="$FAKEBIN:$PATH" NSU_LLM_K3S_RELEASE_URL="$(fileurl "$1")" \
      ps_main -EnvFile "$(winpath "$RUN_ENV")" -AdvertiseIp 192.168.3.21 -Airgap -AirgapDir "$(winpath "$2")"
  }
  # the run itself may stop later at the port check (this machine may already serve 443): only the airgap part is checked
  out="$(ps_run "$REL/good" "$TMP/psag1")"
  cmp -s "$REL/good/$K3S_FILE" "$TMP/psag1/$K3S_FILE" && ! ls "$TMP/psag1" | grep -q '\.part$\|\.download$' \
    && grep -q '^pull --platform linux/amd64 ghcr.io/berriai/litellm:v1.106.6' "$FAKE_DOCKER_LOG"
  check "run-main.ps1 -Airgap: archive with a matching sha256 lands in the images dir, then the images" $? "$out
$(cat "$FAKE_DOCKER_LOG")"
  out="$(ps_run "$REL/bad" "$TMP/psag2")"
  grep -q 'Контрольная сумма' <<<"$out" && [ -z "$(ls -A "$TMP/psag2" 2>/dev/null)" ] && ! grep -q '^pull ' "$FAKE_DOCKER_LOG"
  check "run-main.ps1 -Airgap: sha256 mismatch -> Russian error, nothing kept, no image pulled" $? "$out
$(cat "$FAKE_DOCKER_LOG")"
  out="$(FAKE_DOCKER_RC=1 ps_run "$REL/good" "$TMP/psag3")"
  grep -q 'docker pull завершилась с кодом 1' <<<"$out" && ! grep -q '^save ' "$FAKE_DOCKER_LOG" && ! grep -q '^run ' "$FAKE_DOCKER_LOG"
  check "run-main.ps1: a failed docker pull stops the script (no save, no run)" $? "$out
$(cat "$FAKE_DOCKER_LOG")"

  out="$(FAKE_AGENT_TOKEN="$AGENT_T" PATH="$FAKEBIN:$PATH" NSU_LLM_TOKEN_WAIT_S=0 ps_main -PrintToken)"; rc=$?
  [ "$rc" = 0 ] && [ "$out" = "$AGENT_T" ]
  check "run-main.ps1 -PrintToken prints only the agent token" $? "rc=$rc out=$out"
  out="$(PATH="$FAKEBIN:$PATH" NSU_LLM_TOKEN_WAIT_S=0 ps_main -PrintToken)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'Токен подключения нод' <<<"$out"
  check "run-main.ps1 -PrintToken before k3s wrote the token -> Russian error, non-zero exit" $? "rc=$rc $out"
  out="$(ps_main -DryRun -PrintToken)"
  grep -q '^docker exec llm-main /opt/llm/main/agent-token.sh *$' <<<"$out"
  check "run-main.ps1 -DryRun -PrintToken shows the docker exec command" $? "$out"
  # the start itself prints the agent token (the full run stops at the port check when this machine serves 443)
  grep -q 'Write-AgentToken' "$RUN_MAIN_PS1" && [ "$(grep -c 'Write-AgentToken' "$RUN_MAIN_PS1")" -ge 2 ]
  check "run-main.ps1 prints the agent token after the start" $?
fi

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
