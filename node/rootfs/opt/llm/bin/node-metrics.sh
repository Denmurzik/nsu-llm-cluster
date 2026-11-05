#!/usr/bin/env bash
# Monitoring collector of the node (task 2-33). Runs in the background of the node container (k3s and standalone
# alike, started by entrypoint.sh) and pushes the load of the node to the portal every METRICS_INTERVAL_S seconds:
#   POST $MAIN_URL/api/nodes/$NODE_NAME/metrics   (Bearer agent token, the way the NIC report goes)
# The interval is 20 s (METRICS_INTERVAL_S, kept between 10 and 60): a GPU load changes within seconds, but the portal
# stores one row per minute anyway, so three pushes per minute give the live bars a fresh value and a lost push costs
# nothing. The reply is not used; the portal answers 429 to a push that is less than 5 s after the previous one.
#
# What is sent (see portal/app/metrics/schemas.py): per GPU the utilisation, VRAM used/total, temperature and power
# (nvidia-smi; fields it cannot give are null), RAM used/total inside the memory limit of the container (in WSL: of
# WSL), CPU % since the previous push (/proc/stat), the size of the model cache and the free disk space there, and
# whether a llama-server and a ggml-rpc-server process run (any process of the container: in k3s mode the pods'
# processes live in it too).
#
#   node-metrics.sh           push in a loop until TERM
#   node-metrics.sh --once    print the JSON of one sample (CPU over METRICS_CPU_SAMPLE_S) and exit
# Environment: MAIN_URL, CLUSTER_TOKEN, NODE_NAME (as for registrar.sh); LLAMA_CACHE (the model cache directory);
# MAIN_INSECURE_TLS=1, STATE_DIR (the portal CA); tests: PROC_DIR, PROC_STAT, PROC_MEMINFO, CGROUP_MEMORY_MAX,
# CGROUP_MEMORY_CURRENT.
set -uo pipefail
export LC_ALL=C  # a decimal point in the numbers whatever the locale

INTERVAL="${METRICS_INTERVAL_S:-20}"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] || INTERVAL=20
[ "$INTERVAL" -lt 10 ] && INTERVAL=10
[ "$INTERVAL" -gt 60 ] && INTERVAL=60
CPU_SAMPLE_S="${METRICS_CPU_SAMPLE_S:-1}"
DISK_EVERY_S="${METRICS_DISK_EVERY_S:-300}"  # du of the model cache: not on every push
LLAMA_CACHE="${LLAMA_CACHE:-/var/lib/llm-models}"
STATE_DIR="${STATE_DIR:-/var/lib/llm-node}"
PROC_DIR="${PROC_DIR:-/proc}"
PROC_STAT="${PROC_STAT:-$PROC_DIR/stat}"
PROC_MEMINFO="${PROC_MEMINFO:-$PROC_DIR/meminfo}"
CGROUP_MEMORY_MAX="${CGROUP_MEMORY_MAX:-/sys/fs/cgroup/memory.max}"
CGROUP_MEMORY_CURRENT="${CGROUP_MEMORY_CURRENT:-/sys/fs/cgroup/memory.current}"

log() { echo "$(date -u +%FT%TZ) node-metrics: $*"; }

# "<total> <idle>" jiffies of all CPUs (idle includes iowait)
cpu_times() {
  awk '/^cpu / {print $2 + $3 + $4 + $5 + $6 + $7 + $8 + $9, $5 + $6; exit}' "$PROC_STAT" 2>/dev/null
}

# cpu_pct TOTAL0 IDLE0 TOTAL1 IDLE1 -> busy share in percent, one decimal
cpu_pct() {
  awk -v t0="$1" -v i0="$2" -v t1="$3" -v i1="$4" 'BEGIN {
    dt = t1 - t0; di = i1 - i0
    if (dt <= 0) { print "0.0"; exit }
    p = 100 * (dt - di) / dt
    if (p < 0) p = 0
    if (p > 100) p = 100
    printf "%.1f\n", p
  }'
}

# "<used> <total>" MiB of the memory this container may use: the cgroup limit when there is one (MemTotal otherwise,
# which inside WSL is the memory of WSL), the used part from the cgroup or from MemTotal - MemAvailable
ram_mib() {
  local total avail limit current used
  total="$(awk '/^MemTotal:/ {print int($2 / 1024)}' "$PROC_MEMINFO" 2>/dev/null)"
  avail="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' "$PROC_MEMINFO" 2>/dev/null)"
  total="${total:-0}"
  used=$((total - ${avail:-$total}))
  limit="$(cat "$CGROUP_MEMORY_MAX" 2>/dev/null || true)"
  if [[ "$limit" =~ ^[0-9]+$ ]] && [ $((limit / 1048576)) -lt "$total" ]; then
    total=$((limit / 1048576))
    current="$(cat "$CGROUP_MEMORY_CURRENT" 2>/dev/null || true)"
    [[ "$current" =~ ^[0-9]+$ ]] && used=$((current / 1048576))
  fi
  [ "$used" -lt 0 ] && used=0
  [ "$used" -gt "$total" ] && used="$total"
  echo "$used $total"
}

# the GPUs as a JSON array ([] without nvidia-smi or when it fails); a field nvidia-smi cannot give ([N/A]) is null
gpus_json() {
  local out
  out="$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
    --format=csv,noheader,nounits 2>/dev/null)" || { echo '[]'; return 0; }
  jq -Rsc '
    def num: try (gsub("^ +| +$|\r"; "") | tonumber | . + 0) catch null;
    [split("\n")[] | select(length > 0) | split(",") as $f
     | {util_pct: (($f[0] | num) // 0), vram_used_mib: ((($f[1] | num) // 0) | floor),
        vram_total_mib: ((($f[2] | num) // 0) | floor), temp_c: ($f[3] | num), power_w: ($f[4] | num)}]' <<<"$out" 2>/dev/null \
    || echo '[]'
}

# is a process with this name (comm) running anywhere in the container
proc_up() {
  local c n
  for c in "$PROC_DIR"/[0-9]*/comm; do
    n=""
    { IFS= read -r n < "$c"; } 2>/dev/null
    [ "$n" = "$1" ] && { echo true; return 0; }
  done
  echo false
}

# Free bytes of the Windows disk under the WSL disk: wsl-setup.sh makes the distro write HOST_DISK_FILE (JSON with free_bytes and at, epoch
# seconds) every minute. Inside WSL df shows the virtual disk (up to 1 TB), not the disk that ext4.vhdx fills, so the host number wins when it is
# fresh (HOST_DISK_MAX_AGE_S, 15 min). Prints the number; fails without the file, with a stale or malformed one (Docker Desktop, Linux).
host_free() {
  local f="${HOST_DISK_FILE:-$STATE_DIR/host-disk}" at free
  [ -s "$f" ] || return 1
  at="$(jq -r '.at // empty' <"$f" 2>/dev/null)"
  free="$(jq -r '.free_bytes // empty' <"$f" 2>/dev/null)"
  [[ "$at" =~ ^[0-9]+$ ]] && [[ "$free" =~ ^[0-9]+$ ]] || return 1
  [ $(($(date +%s) - at)) -le "${HOST_DISK_MAX_AGE_S:-900}" ] || return 1
  echo "$free"
}

LAST_DISK_AT=-100000
MODELS_BYTES=null
DISK_FREE=null
# the size of the model cache and the free space under it, refreshed every DISK_EVERY_S seconds
refresh_disk() {
  [ $((SECONDS - LAST_DISK_AT)) -ge "$DISK_EVERY_S" ] || return 0
  LAST_DISK_AT=$SECONDS
  MODELS_BYTES=null
  DISK_FREE=null
  local size free host
  if host="$(host_free)"; then DISK_FREE="$host"; fi
  [ -d "$LLAMA_CACHE" ] || return 0
  size="$(timeout "${DU_TIMEOUT_S:-20}" du -sb "$LLAMA_CACHE" 2>/dev/null | cut -f1)"
  [[ "$size" =~ ^[0-9]+$ ]] && MODELS_BYTES="$size"
  if [ "$DISK_FREE" = null ]; then
    free="$(df -Pk "$LLAMA_CACHE" 2>/dev/null | awk 'NR == 2 {print $4 * 1024}')"
    [[ "$free" =~ ^[0-9]+$ ]] && DISK_FREE="$free"
  fi
}

# sample CPU_PCT -> the JSON of one sample on stdout
sample_json() {
  local cpu="$1" used total
  read -r used total <<<"$(ram_mib)"
  refresh_disk
  jq -nc --argjson gpus "$(gpus_json)" --argjson used "${used:-0}" --argjson total "${total:-0}" --argjson cpu "$cpu" \
    --argjson models "$MODELS_BYTES" --argjson free "$DISK_FREE" \
    --argjson llama "$(proc_up llama-server)" --argjson rpc "$(proc_up ggml-rpc-server)" \
    '{gpus: $gpus, ram_used_mib: $used, ram_total_mib: $total, cpu_pct: $cpu, models_bytes: $models,
      disk_free_bytes: $free, llama_up: $llama, rpc_up: $rpc}'
}

# ---- push to the portal -------------------------------------------------------------------------------

TLS=()
TLS_READY=0
# https: the portal CA saved by the registrar (STATE_DIR/ca.crt), fetched here once if nobody did yet (trust on first use,
# as registrar.sh does); an own certificate of the portal (no /ca.crt, 404) is verified against the system store
setup_tls() {
  TLS_READY=1
  TLS=()
  case "$MAIN_URL" in
    https://*)
      if [ "${MAIN_INSECURE_TLS:-0}" = 1 ]; then TLS=(-k); return 0; fi
      if [ ! -s "$STATE_DIR/ca.crt" ] && command -v fetch-main-ca.sh >/dev/null 2>&1; then
        # main's CA checked against the k3s CA hash of the token (task SEC-2): 0 saved, 4 own certificate, else later
        local rc=0
        fetch-main-ca.sh --main "$MAIN_URL" --out "$STATE_DIR/ca.crt" >/dev/null 2>&1 || rc=$?
        [ "$rc" = 0 ] || [ "$rc" = 4 ] || { TLS_READY=0; return 1; }
      elif [ ! -s "$STATE_DIR/ca.crt" ]; then
        local code
        mkdir -p "$STATE_DIR" 2>/dev/null
        code="$(curl -sS -k --connect-timeout 3 -m 10 -o "$STATE_DIR/ca.crt.metrics" -w '%{http_code}' "$MAIN_URL/ca.crt" 2>/dev/null)" || code=000
        if [ "$code" = 200 ] && [ -s "$STATE_DIR/ca.crt.metrics" ]; then
          mv "$STATE_DIR/ca.crt.metrics" "$STATE_DIR/ca.crt"
        else
          rm -f "$STATE_DIR/ca.crt.metrics"
          [ "$code" = 404 ] || { TLS_READY=0; return 1; }  # the portal does not answer yet: again at the next push
        fi
      fi
      [ -s "$STATE_DIR/ca.crt" ] && TLS=(--cacert "$STATE_DIR/ca.crt")
      ;;
  esac
  return 0
}

HTTP_CODE=000
KEY_FILE="$STATE_DIR/node.key"  # the node's own key, issued to the registrar (portal/app/nodes/identity.py)
# push JSON -> HTTP_CODE (000: no answer)
push() {
  [ "$TLS_READY" = 1 ] || setup_tls || { HTTP_CODE=000; return 1; }
  local args=(-sS --connect-timeout 3 -m 8 -X POST -w $'\n%{http_code}' -H 'Content-Type: application/json' -d "$1")
  [ "${#TLS[@]}" -gt 0 ] && args+=("${TLS[@]}")
  local key="" out
  [ -s "$KEY_FILE" ] && IFS= read -r key < "$KEY_FILE"
  [[ "$key" =~ ^[A-Za-z0-9_-]{43}$ ]] || key=""
  # the token and the node's key reach curl as a header file, never through argv (/proc/<pid>/cmdline is world-readable)
  out="$(curl "${args[@]}" -H @<(printf 'Authorization: Bearer %s\n' "$CLUSTER_TOKEN"; [ -n "$key" ] && printf 'X-Node-Key: %s\n' "$key") \
    "$MAIN_URL/api/nodes/$NODE_NAME/metrics" 2>/dev/null)" || out=$'\n000'
  HTTP_CODE="${out##*$'\n'}"
  # a key the portal refuses (reset by the admin, the node deleted) goes: the registrar registers the node again
  if [ "$HTTP_CODE" = 403 ] && [ -n "$key" ] && [[ "${out%$'\n'*}" == *'"node_key_invalid"'* ]]; then
    # only the key that was refused: the registrar may have kept a new one while this call was on its way (task B1)
    local now=""
    [ -s "$KEY_FILE" ] && IFS= read -r now < "$KEY_FILE"
    if [ "$now" = "$key" ]; then
      log "the portal refused this node's key: it is dropped, the registrar registers the node again"
      rm -f "$KEY_FILE" "$STATE_DIR/backend/key"
    fi
  fi
  [ "$HTTP_CODE" = 204 ]
}

STOPPING=0
on_term() { STOPPING=1; }

nap() {
  sleep "$1" &
  local p=$!
  wait "$p" 2>/dev/null
  kill "$p" 2>/dev/null
  return 0
}

main_loop() {
  : "${CLUSTER_TOKEN:?CLUSTER_TOKEN is required}"
  NODE_NAME="${NODE_NAME:-$(hostname)}"
  NODE_NAME="${NODE_NAME,,}"
  MAIN_URL="${MAIN_URL:-https://${MAIN_HOST:?MAIN_URL or MAIN_HOST is required}}"
  MAIN_URL="${MAIN_URL%/}"
  trap on_term TERM INT
  local failing=0 t0 i0 t1 i1 cpu
  read -r t0 i0 <<<"$(cpu_times)"
  nap 2
  while [ "$STOPPING" = 0 ]; do
    read -r t1 i1 <<<"$(cpu_times)"
    cpu="$(cpu_pct "${t0:-0}" "${i0:-0}" "${t1:-0}" "${i1:-0}")"
    t0="$t1"
    i0="$i1"
    if push "$(sample_json "$cpu")"; then
      [ "$failing" = 1 ] && log "the portal takes the samples again"
      failing=0
    elif [ "$failing" = 0 ]; then
      failing=1
      log "the portal did not take the sample (HTTP $HTTP_CODE); trying again every ${INTERVAL}s, this is logged once"
    fi
    [ "$STOPPING" = 1 ] && break
    nap "$INTERVAL"
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if [ "${1:-}" = "--once" ]; then
    read -r t0 i0 <<<"$(cpu_times)"
    nap "$CPU_SAMPLE_S"
    read -r t1 i1 <<<"$(cpu_times)"
    sample_json "$(cpu_pct "${t0:-0}" "${i0:-0}" "${t1:-0}" "${i1:-0}")"
  else
    main_loop
  fi
fi
