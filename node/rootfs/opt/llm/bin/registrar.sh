#!/usr/bin/env bash
# Standalone mode (PID 1): registers the node in the portal, sends heartbeats and runs
# llama-server according to the "desired" config returned by the heartbeat.
# --wake-report (k3s mode, started by entrypoint.sh in the background): only reports the NIC for Wake-on-LAN
# (WOL_MAC, WOL_IP, WOL_PREFIX from run-node.ps1) to POST /api/nodes/wake, retrying until the portal takes it.
set -uo pipefail

WAKE_REPORT=0
[ "${1:-}" = --wake-report ] && WAKE_REPORT=1
: "${CLUSTER_TOKEN:?CLUSTER_TOKEN is required}"
[ "$WAKE_REPORT" = 1 ] || : "${NODE_IP:?NODE_IP is required}"
NODE_NAME="${NODE_NAME:-$(hostname)}"
NODE_NAME="${NODE_NAME,,}"
MAIN_URL="${MAIN_URL:-https://${MAIN_HOST:?MAIN_URL or MAIN_HOST is required}}"
MAIN_URL="${MAIN_URL%/}"
INFER_PORT="${INFER_PORT:-18080}"
STATE_DIR="${STATE_DIR:-/var/lib/llm-node}"
NODE_VERSION="0.1.0"
ACCEL="${ACCEL:-auto}"
if [ "$ACCEL" = "auto" ] || [ -z "$ACCEL" ]; then ACCEL="$(detect-accel.sh)"; fi
export ACCEL INFER_PORT
# The node's own Hugging Face settings: used as they are, or as the fallback when the portal hands out the mirror of main.
OWN_HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
OWN_HF_TOKEN="${HF_TOKEN:-}"
SYSTEM_CA="${SYSTEM_CA:-/etc/ssl/certs/ca-certificates.crt}"
WAKE_RETRY_S="${WAKE_RETRY_S:-30}"

STOPPING=0
SERVER_PID=""
CURRENT_HASH=""      # spec hash of the server we started (or are starting)
APPLIED_HASH=""      # spec hash reported to the portal: set once /health answers 200
LAST_REPORTED=""     # applied hash carried by the last successful heartbeat
CRASH_AT=""
INTERVAL=15
D_REPO="" D_FILE="" D_REV="" D_NAME="" D_CTX="" D_PAR="" D_KEY="" D_HF="" D_HF_TOKEN=""
D_ENV=()             # the llama.cpp settings of the deployment as NAME=value (desired `llm_env`), see apply_desired
# the only variables desired may set for serve-llama.sh (the node does not export whatever the portal sends)
LLM_ENV_NAMES='^(THINKING|KV_CACHE_TYPE|SAMPLING_ARGS|DRAFT_REPO|DRAFT_FILE|DRAFT_REVISION|MMPROJ_FILE|EXTRA_ARGS)$'
HTTP_CODE=000
BODY=""
TLS=()

log() { echo "$(date -u +%FT%TZ) registrar: $*"; }
on_term() { STOPPING=1; }
trap on_term TERM INT

# Sleeps up to $1 seconds; returns early when a trapped signal arrives.
nap() {
  sleep "$1" &
  local p=$!
  wait "$p" 2>/dev/null
  kill "$p" 2>/dev/null
  return 0
}

setup_tls() {
  case "$MAIN_URL" in
    https://*)
      if [ "${MAIN_INSECURE_TLS:-0}" = "1" ]; then
        TLS=(-k)
        return 0
      fi
      mkdir -p "$STATE_DIR"
      # Trust on first use: fetch the portal CA once, then verify against it.
      # A portal started with its own certificate (--certs) serves no /ca.crt (404): the system CA store is used.
      local code
      until [ -s "$STATE_DIR/ca.crt" ]; do
        [ "$STOPPING" = 1 ] && return 1
        code="$(curl -sS -k --connect-timeout 3 -m 10 -o "$STATE_DIR/ca.crt.tmp" -w '%{http_code}' "$MAIN_URL/ca.crt" 2>/dev/null)" || code=000
        if [ "$code" = 200 ] && [ -s "$STATE_DIR/ca.crt.tmp" ]; then
          mv "$STATE_DIR/ca.crt.tmp" "$STATE_DIR/ca.crt"
          log "CA saved to $STATE_DIR/ca.crt"
        elif [ "$code" = 404 ]; then
          rm -f "$STATE_DIR/ca.crt.tmp"
          log "$MAIN_URL/ca.crt not served (own certificate): verifying against the system CA store"
          TLS=()
          return 0
        else
          rm -f "$STATE_DIR/ca.crt.tmp"
          log "cannot fetch $MAIN_URL/ca.crt (HTTP $code), retrying"
          nap 5
        fi
      done
      TLS=(--cacert "$STATE_DIR/ca.crt")
      # the mirror of main (https://<main>/hf) has a certificate of the same CA; Hugging Face needs the system CAs
      { [ -s "$SYSTEM_CA" ] && cat "$SYSTEM_CA"; echo; cat "$STATE_DIR/ca.crt"; } > "$STATE_DIR/bundle.crt.tmp" \
        && mv "$STATE_DIR/bundle.crt.tmp" "$STATE_DIR/bundle.crt"
      ;;
    *) TLS=() ;;
  esac
}

# call METHOD PATH [JSON]  ->  HTTP_CODE, BODY
call() {
  local method="$1" path="$2" data="${3:-}" out
  local args=(-sS --connect-timeout 3 -m 8 -X "$method" -w $'\n%{http_code}')
  [ "${#TLS[@]}" -gt 0 ] && args+=("${TLS[@]}")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  # The token (= k3s server token) reaches curl as a header file, never through argv (/proc/<pid>/cmdline is world-readable).
  if ! out="$(curl "${args[@]}" -H @<(printf 'Authorization: Bearer %s\n' "$CLUSTER_TOKEN") "$MAIN_URL$path" 2>&1)"; then
    HTTP_CODE=000
    BODY="$out"
    return 1
  fi
  HTTP_CODE="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
  return 0
}

# The physical NIC of the host for Wake-on-LAN as JSON, or null when run-node passed none (or a malformed one).
wake_json() {
  if [ -n "${WOL_MAC:-}" ] && [ -n "${WOL_IP:-}" ] && [[ "${WOL_PREFIX:-}" =~ ^[0-9]{1,2}$ ]]; then
    jq -nc --arg mac "$WOL_MAC" --arg ip "$WOL_IP" --argjson prefix "$WOL_PREFIX" '{mac: $mac, ip: $ip, prefix: $prefix}'
  else
    echo null
  fi
}
WAKE="$(wake_json)"

register() {
  local ram vram payload
  ram="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)"
  vram="$(detect-accel.sh --vram)"
  payload="$(jq -nc --arg name "$NODE_NAME" --arg url "http://$NODE_IP:$INFER_PORT" \
    --arg accel "$ACCEL" --arg vram "$vram" --argjson ram "${ram:-0}" --arg version "$NODE_VERSION" --argjson wake "$WAKE" \
    '{name: $name, url: $url, accel: $accel, vram_mib: (if $vram == "" then null else ($vram | tonumber) end),
      ram_mib: $ram, version: $version} + (if $wake == null then {} else {wake: $wake} end)')"
  call POST /api/nodes/register "$payload" || return 1
  [ "$HTTP_CODE" = 200 ] || return 1
  INTERVAL="$(jq -r '.heartbeat_interval // 15' <<<"$BODY" 2>/dev/null || echo 15)"
  return 0
}

register_until_ok() {
  until register; do
    [ "$STOPPING" = 1 ] && return 1
    log "register failed (HTTP $HTTP_CODE): ${BODY:0:200}; retrying in 5 s"
    nap 5
    [ "$STOPPING" = 1 ] && return 1
  done
  log "registered as $NODE_NAME (heartbeat every ${INTERVAL}s)"
}

health_ok() {
  curl -sf --connect-timeout 1 -m 3 -o /dev/null "http://127.0.0.1:$INFER_PORT/health"
}

start_server() {
  log "starting llama-server for $D_REPO/$D_FILE (ctx $D_CTX, parallel $D_PAR, hash $CURRENT_HASH)"
  local hf=(HF_ENDPOINT="$OWN_HF_ENDPOINT" HF_TOKEN="$OWN_HF_TOKEN")
  if [ -n "$D_HF" ]; then  # the mirror of main, the node's own settings if it does not answer (serve-llama.sh)
    hf=(HF_ENDPOINT="$D_HF" HF_TOKEN="$D_HF_TOKEN" HF_FALLBACK_ENDPOINT="$OWN_HF_ENDPOINT"
      HF_FALLBACK_TOKEN="$OWN_HF_TOKEN" LLM_CA_BUNDLE="$STATE_DIR/bundle.crt")
  fi
  env "${hf[@]}" MODEL_REPO="$D_REPO" MODEL_FILE="$D_FILE" MODEL_REVISION="$D_REV" PUBLIC_MODEL_NAME="$D_NAME" \
    CTX_SIZE="$D_CTX" PARALLEL_SLOTS="$D_PAR" ${D_ENV[@]+"${D_ENV[@]}"} serve-llama.sh &
  SERVER_PID=$!
  CRASH_AT=""
}

stop_server() {
  [ -n "$SERVER_PID" ] || return 0
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    log "stopping llama-server (pid $SERVER_PID)"
    kill -TERM "$SERVER_PID" 2>/dev/null
    local i=0
    while kill -0 "$SERVER_PID" 2>/dev/null && [ "$i" -lt 60 ]; do
      sleep 0.5
      i=$((i + 1))
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      log "llama-server ignored TERM for 30 s, killing"
      kill -KILL "$SERVER_PID" 2>/dev/null
    fi
  fi
  wait "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
}

# Keeps APPLIED_HASH in step with the real state of the server.
check_server() {
  [ -n "$CURRENT_HASH" ] || return 0
  if [ -z "$SERVER_PID" ] || ! kill -0 "$SERVER_PID" 2>/dev/null; then
    [ -n "$SERVER_PID" ] && wait "$SERVER_PID" 2>/dev/null
    SERVER_PID=""
    APPLIED_HASH=""
    [ -n "$CRASH_AT" ] || { CRASH_AT=$SECONDS; log "llama-server is not running, restarting in 5 s"; }
    if [ $((SECONDS - CRASH_AT)) -ge 5 ]; then start_server; fi
  elif [ "$APPLIED_HASH" != "$CURRENT_HASH" ] && health_ok; then
    APPLIED_HASH="$CURRENT_HASH"
    log "llama-server is healthy, hash $APPLIED_HASH"
  fi
}

apply_desired() {
  local d h k
  d="$(jq -c '.desired' <<<"$BODY" 2>/dev/null)" || return 0
  if [ "$d" = "null" ] || [ -z "$d" ]; then
    if [ -n "$CURRENT_HASH" ] || [ -n "$SERVER_PID" ]; then
      log "desired config is empty, stopping llama-server"
      stop_server
      CURRENT_HASH=""
      APPLIED_HASH=""
    fi
    return 0
  fi
  h="$(jq -r '.spec_hash' <<<"$d")"
  k="$(jq -r '.backend_api_key // ""' <<<"$d")"
  if [ "$h" != "$CURRENT_HASH" ] || [ "$k" != "$D_KEY" ]; then  # a rotated key restarts the server too
    stop_server
    D_REPO="$(jq -r '.model_repo' <<<"$d")"
    D_FILE="$(jq -r '.model_file' <<<"$d")"
    D_REV="$(jq -r '.model_revision // ""' <<<"$d")"  # the pinned commit (in the spec hash); empty: main
    D_NAME="$(jq -r '.public_model_name' <<<"$d")"
    D_CTX="$(jq -r '.ctx_size' <<<"$d")"
    D_PAR="$(jq -r '.parallel' <<<"$d")"
    D_KEY="$k"
    D_HF="$(jq -r '.hf_endpoint // ""' <<<"$d")"
    D_HF_TOKEN="$(jq -r '.hf_token // ""' <<<"$d")"
    # reasoning, KV cache type, sampling, draft, projector, own flags: the same environment the k3s pods get; serve-llama.sh
    # checks the values again (own flags included). NUL-separated, an own-flags value holds newlines.
    D_ENV=()
    local kv
    while IFS= read -r -d '' kv; do
      if [[ "${kv%%=*}" =~ $LLM_ENV_NAMES ]]; then D_ENV+=("$kv"); else log "ignoring ${kv%%=*} from desired"; fi
    done < <(jq -j '(.llm_env // {}) | to_entries[] | select(.value | type == "string") | "\(.key)=\(.value)\u0000"' <<<"$d")
    # llama-server reads the shared key from its environment, never from argv
    if [ -n "$k" ]; then export LLAMA_API_KEY="$k"; else unset LLAMA_API_KEY; fi
    CURRENT_HASH="$h"
    APPLIED_HASH=""
    start_server
  fi
}

# Wake-on-LAN relay: magic packets the portal asks this node to send for down PCs of its subnet (heartbeat `wake`)
relay_wake() {
  local mac addr
  while IFS=$'\t' read -r mac addr; do
    addr="${addr%$'\r'}"
    if ! [[ "$mac" =~ ^[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}$ && "$addr" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      log "wake-on-lan: relay command ignored (MAC ${mac:0:40}, address ${addr:0:40})"
      continue
    fi
    log "wake-on-lan: magic packet for $mac to $addr (relay for a PC of this subnet)"
    wol-send.sh "$mac" "$addr" || log "wake-on-lan: wol-send.sh failed for $mac"
  done < <(jq -r '.wake // [] | .[] | [(.mac // "" | tostring), (.broadcast // "" | tostring)] | @tsv' <<<"$BODY" 2>/dev/null)
}

NEXT_HB=0
BLOCK_UNTIL=0

heartbeat() {
  local payload
  payload="$(jq -nc --arg name "$NODE_NAME" --arg h "$APPLIED_HASH" --argjson wake "$WAKE" \
    '{name: $name, applied_spec_hash: (if $h == "" then null else $h end)} + (if $wake == null then {} else {wake: $wake} end)')"
  if call POST /api/nodes/heartbeat "$payload" && [ "$HTTP_CODE" = 200 ]; then
    LAST_REPORTED="$APPLIED_HASH"
    INTERVAL="$(jq -r '.heartbeat_interval // 15' <<<"$BODY" 2>/dev/null || echo 15)"
    NEXT_HB=$((SECONDS + INTERVAL))
    relay_wake
    apply_desired
  elif [ "$HTTP_CODE" = 404 ]; then
    log "portal does not know this node, registering again"
    register_until_ok
    NEXT_HB=0
  else
    log "heartbeat failed (HTTP $HTTP_CODE): ${BODY:0:200}"
    NEXT_HB=$((SECONDS + 5))
  fi
  BLOCK_UNTIL=$((SECONDS + 2))
}

shutdown() {
  log "shutting down"
  # stopped, not uninstalled: the portal takes the node out of routing and keeps its NIC, so Wake-on-LAN can switch
  # the PC on again; only run-node -Uninstall sends DELETE (forgets the node)
  call POST "/api/nodes/$NODE_NAME/stop" || true
  log "marked stopped (HTTP $HTTP_CODE)"
  stop_server
  exit 0
}

# k3s mode: the NIC once per container start; a refusal (422: data the portal does not accept) is not retried
report_wake() {
  local payload
  payload="$(jq -nc --arg name "$NODE_NAME" --argjson wake "$WAKE" '{name: $name} + $wake')"
  until call POST /api/nodes/wake "$payload" && [ "$HTTP_CODE" = 200 ]; do
    [ "$STOPPING" = 1 ] && return 0
    if [ "$HTTP_CODE" = 422 ]; then
      log "wake-on-lan: the portal refused the NIC $WOL_MAC ($WOL_IP/$WOL_PREFIX): ${BODY:0:200}"
      return 1
    fi
    log "wake-on-lan: NIC report failed (HTTP $HTTP_CODE): ${BODY:0:200}; retrying in $WAKE_RETRY_S s"
    nap "$WAKE_RETRY_S"
  done
  log "wake-on-lan: NIC $WOL_MAC ($WOL_IP/$WOL_PREFIX) reported to the portal"
}

if [ "$WAKE_REPORT" = 1 ]; then
  [ "$WAKE" != null ] || { log "wake-on-lan: no NIC given (WOL_MAC, WOL_IP, WOL_PREFIX), nothing to report"; exit 0; }
  setup_tls || exit 0
  report_wake
  exit $?
fi

setup_tls || shutdown
register_until_ok || shutdown

while [ "$STOPPING" = 0 ]; do
  check_server
  if [ "$SECONDS" -ge "$NEXT_HB" ] || { [ "$APPLIED_HASH" != "$LAST_REPORTED" ] && [ "$SECONDS" -ge "$BLOCK_UNTIL" ]; }; then
    heartbeat
  fi
  [ "$STOPPING" = 1 ] && break
  nap 1
done
shutdown
