#!/usr/bin/env bash
# Standalone mode (PID 1): registers the node in the portal, sends heartbeats and runs
# llama-server according to the "desired" config returned by the heartbeat.
# --report (k3s mode, started by entrypoint.sh in the background; --wake-report is the old name): reports the NIC for
# Wake-on-LAN (WOL_MAC, WOL_IP, WOL_PREFIX from run-node) to POST /api/nodes/wake and the image this container runs
# (NODE_VERSION, NODE_REVISION from the image, NODE_IMAGE_DIGEST and NODE_IMAGE from run-node) to POST /api/nodes/<name>/image,
# retrying until the portal takes them. Standalone mode reports the image right after every registration.
# --models-report (k3s mode, started by entrypoint.sh in the background): reports how many bytes the models volume holds
# to POST /api/nodes/models every MODELS_REPORT_S seconds (default 300) for the «Ноды» page (task 2-26). In standalone
# mode the size travels with the heartbeat, and the heartbeat reply may carry cleanup orders (`purge`, models-cleanup.sh).
#
# Identity (task SEC-1, portal/app/nodes/identity.py): the agent token only joins. The first registration of the name
# (standalone: POST /api/nodes/register, k3s: POST /api/nodes/enroll, which --report does first and then keeps doing
# whenever the key is gone) returns the node's own key once; it is kept root-only in $STATE_DIR/node.key and sent as
# X-Node-Key with every call, besides the token. From it the node derives its backend key (llama-server --api-key):
# sha256("nsu-llm-backend:" + key) in $STATE_DIR/backend/key, which the k3s pods mount. A 403 node_key_invalid (the
# admin reset the key or deleted the node) drops both files and registers again; the node then waits for the admin's
# approval («Ноды», «Подтвердить»): heartbeats answer `approved: false` and no deployment.
set -uo pipefail

WAKE_REPORT=0
MODELS_REPORT=0
case "${1:-}" in --report|--wake-report) WAKE_REPORT=1 ;; esac
[ "${1:-}" = --models-report ] && MODELS_REPORT=1
: "${CLUSTER_TOKEN:?CLUSTER_TOKEN is required}"
[ "$WAKE_REPORT" = 1 ] || [ "$MODELS_REPORT" = 1 ] || : "${NODE_IP:?NODE_IP is required}"
NODE_NAME="${NODE_NAME:-$(hostname)}"
NODE_NAME="${NODE_NAME,,}"
MAIN_URL="${MAIN_URL:-https://${MAIN_HOST:?MAIN_URL or MAIN_HOST is required}}"
MAIN_URL="${MAIN_URL%/}"
# fetch-main-ca.sh takes host[:port] (SEC-3 validates it), not a URL
main_hostport() { local h="${MAIN_URL#*://}"; printf %s "${h%%/*}"; }
INFER_PORT="${INFER_PORT:-18080}"
STATE_DIR="${STATE_DIR:-/var/lib/llm-node}"
# baked into the image (node/Dockerfile ARG VERSION, GIT_COMMIT); the digest of the image in main's registry and its local name
# are known to run-node, which pulled it
NODE_VERSION="${NODE_VERSION:-dev}"
NODE_REVISION="${NODE_REVISION:-}"
NODE_IMAGE_DIGEST="${NODE_IMAGE_DIGEST:-}"
[[ "$NODE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || NODE_IMAGE_DIGEST=""
NODE_IMAGE="${NODE_IMAGE:-}"
ACCEL="${ACCEL:-auto}"
if [ "$ACCEL" = "auto" ] || [ -z "$ACCEL" ]; then ACCEL="$(detect-accel.sh)"; fi
export ACCEL INFER_PORT
# The node's own Hugging Face settings: used as they are, or as the fallback when the portal hands out the mirror of main.
OWN_HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
OWN_HF_TOKEN="${HF_TOKEN:-}"
SYSTEM_CA="${SYSTEM_CA:-/etc/ssl/certs/ca-certificates.crt}"
WAKE_RETRY_S="${WAKE_RETRY_S:-30}"
MODELS_REPORT_S="${MODELS_REPORT_S:-300}"
PURGE_RESULT="$STATE_DIR/purge-result.json"  # what the last cleanup did, until a heartbeat took it to the portal
KEY_FILE="$STATE_DIR/node.key"                 # the node's own key (portal/app/nodes/identity.py), root only
NONCE_FILE="$STATE_DIR/enroll.nonce"           # this node's nonce of a registration without a key (a lost reply repeats it)
BACKEND_KEY_FILE="$STATE_DIR/backend/key"      # its backend key, derived; the k3s pods mount $STATE_DIR/backend
ENROLL_RETRY_S="${ENROLL_RETRY_S:-60}"
APPROVED=""          # what the portal said last: true | false
PURGE_PID=""         # background models-cleanup.sh of a portal order
MODELS_BYTES=""      # size of the models volume, refreshed every MODELS_REPORT_S seconds
MODELS_AT=-100000

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
        # main's CA checked against the k3s CA hash of the token (fetch-main-ca.sh of the image, task SEC-2): 0 saved and
        # verified, 4 main serves no /ca.crt (own certificate), anything else: try again
        if command -v fetch-main-ca.sh >/dev/null 2>&1; then
          local rc=0
          CLUSTER_TOKEN="$CLUSTER_TOKEN" fetch-main-ca.sh --main "$(main_hostport)" --out "$STATE_DIR/ca.crt" || rc=$?
          if [ "$rc" = 0 ] && [ -s "$STATE_DIR/ca.crt" ]; then
            log "CA saved to $STATE_DIR/ca.crt (checked by fetch-main-ca.sh)"
            break
          elif [ "$rc" = 4 ]; then
            log "$MAIN_URL/ca.crt not served (own certificate): verifying against the system CA store"
            TLS=()
            return 0
          fi
          log "cannot fetch main's CA (fetch-main-ca.sh exit $rc), retrying"
          nap 5
          continue
        fi
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

# The node's own key, when it has one (a well-formed one only)
node_key() {
  local k=""
  [ -s "$KEY_FILE" ] && IFS= read -r k < "$KEY_FILE"
  [[ "$k" =~ ^[A-Za-z0-9_-]{43}$ ]] && printf '%s' "$k"
}

# Keeps the key the portal issued (once: the portal has only its hash) and the backend key derived from it, root only
save_key() {
  local k="$1"
  [[ "$k" =~ ^[A-Za-z0-9_-]{43}$ ]] || { log "the portal sent a malformed node key, ignored"; return 1; }
  (
    umask 077
    mkdir -p "$STATE_DIR/backend"
    printf '%s\n' "$k" > "$KEY_FILE.tmp" && mv "$KEY_FILE.tmp" "$KEY_FILE"
    printf '%s' "nsu-llm-backend:$k" | sha256sum | cut -d' ' -f1 > "$BACKEND_KEY_FILE.tmp" && mv "$BACKEND_KEY_FILE.tmp" "$BACKEND_KEY_FILE"
  ) || { log "cannot write $KEY_FILE"; return 1; }
  rm -f "$NONCE_FILE"
  log "the portal issued this node its own key ($KEY_FILE)"
}

# The nonce sent with a registration without a key: the same one until a key is kept, so a registration whose reply got
# lost (or whose key could not be written) gets a new key instead of 409 node_name_taken
enroll_nonce() {
  local n=""
  [ -s "$NONCE_FILE" ] && IFS= read -r n < "$NONCE_FILE"
  if ! [[ "$n" =~ ^[A-Za-z0-9_-]{16,64}$ ]]; then
    n="$(head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
    ( umask 077; mkdir -p "$STATE_DIR"; printf '%s\n' "$n" > "$NONCE_FILE" ) 2>/dev/null || true
  fi
  printf '%s' "$n"
}

# The backend key file follows the node key (a state directory restored without it, an older registrar)
ensure_backend_key() {
  local k
  k="$(node_key)" || return 0
  [ -n "$k" ] || return 0
  [ -s "$BACKEND_KEY_FILE" ] && return 0
  ( umask 077; mkdir -p "$STATE_DIR/backend"
    printf '%s' "nsu-llm-backend:$k" | sha256sum | cut -d' ' -f1 > "$BACKEND_KEY_FILE.tmp" && mv "$BACKEND_KEY_FILE.tmp" "$BACKEND_KEY_FILE" )
}

drop_key() {
  log "the portal refused this node's key (reset by the admin or the node was deleted): registering again"
  rm -f "$KEY_FILE" "$BACKEND_KEY_FILE"
}

# detail.code of a 403/409 answer of the node API (node_key_invalid, node_key_required, node_name_taken)
refusal_code() {
  jq -r '.detail.code // empty' <<<"$BODY" 2>/dev/null
}
refusal_text() {
  jq -r '.detail.message // .detail // empty' <<<"$BODY" 2>/dev/null | head -c 300
}

# Takes what a registration answered: the key it issued, whether the node is approved
take_identity() {
  local k a
  k="$(jq -r '.node_key // empty' <<<"$BODY" 2>/dev/null)"
  if [ -n "$k" ] && ! save_key "$k"; then
    return 1  # not kept: the next registration repeats the nonce and gets a new key
  fi
  a="$(jq -r 'if .approved == false then "false" else "true" end' <<<"$BODY" 2>/dev/null)"
  note_approval "${a:-true}"
}

note_approval() {
  [ "$1" = "$APPROVED" ] && return 0
  APPROVED="$1"
  if [ "$1" = false ]; then
    log "this node waits for the admin's approval on the «Ноды» page of the portal; it runs no model until then"
  else
    log "this node is approved by the portal"
  fi
}

# call METHOD PATH [JSON]  ->  HTTP_CODE, BODY
call() {
  local method="$1" path="$2" data="${3:-}" out key nonce
  local args=(-sS --connect-timeout 3 -m 8 -X "$method" -w $'\n%{http_code}')
  [ "${#TLS[@]}" -gt 0 ] && args+=("${TLS[@]}")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  key="$(node_key)" nonce=""
  case "$path" in /api/nodes/register|/api/nodes/enroll) [ -n "$key" ] || nonce="$(enroll_nonce)" ;; esac
  # The token and the node's key reach curl as a header file, never through argv (/proc/<pid>/cmdline is world-readable).
  if ! out="$(curl "${args[@]}" -H @<(printf 'Authorization: Bearer %s\n' "$CLUSTER_TOKEN"; [ -n "$key" ] && printf 'X-Node-Key: %s\n' "$key"; [ -n "$nonce" ] && printf 'X-Node-Enroll-Nonce: %s\n' "$nonce") "$MAIN_URL$path" 2>&1)"; then
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
  refresh_models_bytes
  payload="$(jq -nc --arg name "$NODE_NAME" --arg url "http://$NODE_IP:$INFER_PORT" \
    --arg accel "$ACCEL" --arg vram "$vram" --argjson ram "${ram:-0}" --arg version "$NODE_VERSION" --argjson wake "$WAKE" \
    --arg mb "$MODELS_BYTES" \
    '{name: $name, url: $url, accel: $accel, vram_mib: (if $vram == "" then null else ($vram | tonumber) end),
      ram_mib: $ram, version: $version} + (if $wake == null then {} else {wake: $wake} end)
      + (if $mb == "" then {} else {models_bytes: ($mb | tonumber)} end)')"
  call POST /api/nodes/register "$payload" || return 1
  if [ "$HTTP_CODE" = 403 ] && [ "$(refusal_code)" = node_key_invalid ]; then
    drop_key
    call POST /api/nodes/register "$payload" || return 1
  fi
  if [ "$HTTP_CODE" = 409 ] && [ "$(refusal_code)" = node_name_taken ]; then
    log "$(refusal_text)"
    REGISTER_WAIT="$ENROLL_RETRY_S"
    return 1
  fi
  [ "$HTTP_CODE" = 200 ] || return 1
  take_identity || return 1
  INTERVAL="$(jq -r '.heartbeat_interval // 15' <<<"$BODY" 2>/dev/null || echo 15)"
  return 0
}

# k3s mode: the node's identity (its key; whether it is approved). 0 once the node has a key the portal takes.
enroll() {
  local payload
  ensure_backend_key
  payload="$(jq -nc --arg name "$NODE_NAME" '{name: $name}')"
  call POST /api/nodes/enroll "$payload" || return 1
  if [ "$HTTP_CODE" = 403 ] && [ "$(refusal_code)" = node_key_invalid ]; then
    drop_key
    call POST /api/nodes/enroll "$payload" || return 1
  fi
  [ "$HTTP_CODE" = 200 ] || return 1
  take_identity
}

# One registration attempt of the k3s identity -> ENROLL_STATE: ok (the portal answered), none (an older main without
# identities, 404, or a refused name, 422: nothing to retry) or retry (no answer, 409 name taken, 5xx: the keeper tries again)
ENROLL_STATE=retry
enroll_once() {
  if enroll; then ENROLL_STATE=ok; return 0; fi
  case "$HTTP_CODE" in
    404) log "the portal has no enroll endpoint (older main): this node keeps working without its own key"; ENROLL_STATE=none ;;
    422) log "enroll refused (HTTP 422): ${BODY:0:200}"; ENROLL_STATE=none ;;
    409) log "$(refusal_text)"; ENROLL_STATE=retry ;;
    *) log "enroll failed (HTTP $HTTP_CODE): ${BODY:0:200}; trying again later"; ENROLL_STATE=retry ;;
  esac
  return 1
}

# What this container runs, for the «Версии» of the admin pages. Returns 0 when the portal took it.
report_image() {
  local payload
  payload="$(jq -nc --arg v "$NODE_VERSION" --arg r "$NODE_REVISION" --arg d "$NODE_IMAGE_DIGEST" --arg t "$NODE_IMAGE" \
    '{version: $v, revision: $r, digest: $d, tag: $t}')"
  call POST "/api/nodes/$NODE_NAME/image" "$payload" || return 1
  [ "$HTTP_CODE" = 200 ]
}

REGISTER_WAIT=5
register_until_ok() {
  ensure_backend_key
  until register; do
    [ "$STOPPING" = 1 ] && return 1
    log "register failed (HTTP $HTTP_CODE): ${BODY:0:200}; retrying in $REGISTER_WAIT s"
    nap "$REGISTER_WAIT"
    REGISTER_WAIT=5
    [ "$STOPPING" = 1 ] && return 1
  done
  log "registered as $NODE_NAME (heartbeat every ${INTERVAL}s)"
  # best effort: a portal older than this report answers 404, which is no reason to stop the node
  report_image || log "image report failed (HTTP $HTTP_CODE): the portal shows no version for this node"
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

# Size of the models volume in bytes, measured at most every MODELS_REPORT_S seconds (empty until the first measurement)
refresh_models_bytes() {
  [ $((SECONDS - MODELS_AT)) -ge "$MODELS_REPORT_S" ] || return 0
  MODELS_AT=$SECONDS
  local b
  b="$(timeout 30 models-cleanup.sh --usage 2>/dev/null)" && [[ "$b" =~ ^[0-9]+$ ]] && MODELS_BYTES="$b"
  return 0
}

# Cleanup orders of the portal (heartbeat reply `purge`: {id, keep: ["repo:file", ...], keep_rpc, keep_vllm}): models-cleanup.sh
# runs in the background, one at a time, so the heartbeat does not stall; its result goes to the portal with a later
# heartbeat (`purge_result`). What the server of this node runs now stays whatever the order says, and a download that
# has just started is out of reach anyway (models-cleanup.sh skips files written in the last minute).
start_purge() {
  local order="$1" id spec args=() kv
  id="$(jq -r '.id // "" | tostring' <<<"$order" 2>/dev/null)"
  if ! [[ "$id" =~ ^[A-Za-z0-9-]{1,64}$ ]]; then log "cleanup order ignored: bad id"; return 0; fi
  while IFS= read -r spec; do args+=(--keep "$spec"); done < <(jq -r '.keep // [] | .[] | strings' <<<"$order" 2>/dev/null)
  [ "$(jq -r '.keep_rpc // false' <<<"$order" 2>/dev/null)" = true ] && args+=(--keep-rpc)
  [ "$(jq -r '.keep_vllm // false' <<<"$order" 2>/dev/null)" = true ] && args+=(--keep-vllm)
  if [ -n "$CURRENT_HASH" ]; then
    args+=(--keep "$D_REPO:$D_FILE")
    local draft_repo="" draft_file="" mmproj=""
    for kv in ${D_ENV[@]+"${D_ENV[@]}"}; do
      case "$kv" in
        DRAFT_REPO=*) draft_repo="${kv#*=}" ;;
        DRAFT_FILE=*) draft_file="${kv#*=}" ;;
        MMPROJ_FILE=*) mmproj="${kv#*=}" ;;
      esac
    done
    [ -n "$draft_repo" ] && [ -n "$draft_file" ] && args+=(--keep "$draft_repo:$draft_file")
    if [[ "$mmproj" =~ ^local/([^/]+)/(.+)$ ]]; then args+=(--keep "local/${BASH_REMATCH[1]}:${BASH_REMATCH[2]}")
    elif [ -n "$mmproj" ]; then args+=(--keep "$D_REPO:$mmproj"); fi
  fi
  log "cleaning the models volume (order $id)"
  (
    out="$(models-cleanup.sh "${args[@]}" 2>&1)"; rc=$?
    result="$(sed -n 's/^RESULT //p' <<<"$out" | tail -1)"
    if [ "$rc" = 0 ] && [ -n "$result" ]; then
      jq -c --arg id "$id" '. + {id: $id}' <<<"$result" > "$PURGE_RESULT.tmp"
    else
      [ "${#out}" -le 300 ] || out="${out: -300}"  # (a negative offset past the start gives nothing, hence the test)
      jq -nc --arg id "$id" --arg err "${out:-models-cleanup.sh failed (exit $rc)}" '{id: $id, error: $err}' > "$PURGE_RESULT.tmp"
    fi
    mv "$PURGE_RESULT.tmp" "$PURGE_RESULT"
  ) &
  PURGE_PID=$!
}

run_purge_orders() {
  local order
  if [ -n "$PURGE_PID" ] && ! kill -0 "$PURGE_PID" 2>/dev/null; then wait "$PURGE_PID" 2>/dev/null; PURGE_PID=""; fi
  while IFS= read -r order; do
    [ -n "$order" ] || continue
    if [ -n "$PURGE_PID" ]; then log "cleanup order ignored: the previous one is still running"; continue; fi
    mkdir -p "$STATE_DIR"
    start_purge "$order"
  done < <(jq -c '.purge // [] | .[]' <<<"$BODY" 2>/dev/null)
}

heartbeat() {
  local payload purge=null
  refresh_models_bytes
  [ -s "$PURGE_RESULT" ] && purge="$(jq -c . "$PURGE_RESULT" 2>/dev/null || echo null)"
  payload="$(jq -nc --arg name "$NODE_NAME" --arg h "$APPLIED_HASH" --argjson wake "$WAKE" --arg mb "$MODELS_BYTES" --argjson purge "$purge" \
    '{name: $name, applied_spec_hash: (if $h == "" then null else $h end)} + (if $wake == null then {} else {wake: $wake} end)
      + (if $mb == "" then {} else {models_bytes: ($mb | tonumber)} end)
      + (if $purge == null then {} else {purge_result: $purge} end)')"
  if call POST /api/nodes/heartbeat "$payload" && [ "$HTTP_CODE" = 200 ]; then
    [ "$purge" = null ] || rm -f "$PURGE_RESULT"
    LAST_REPORTED="$APPLIED_HASH"
    INTERVAL="$(jq -r '.heartbeat_interval // 15' <<<"$BODY" 2>/dev/null || echo 15)"
    NEXT_HB=$((SECONDS + INTERVAL))
    note_approval "$(jq -r 'if .approved == false then "false" else "true" end' <<<"$BODY" 2>/dev/null)"
    relay_wake
    apply_desired
    run_purge_orders
  elif [ "$HTTP_CODE" = 404 ]; then
    log "portal does not know this node, registering again"
    register_until_ok
    NEXT_HB=0
  elif [ "$HTTP_CODE" = 403 ] && [ "$(refusal_code)" = node_key_invalid ]; then
    drop_key
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
  [ -z "$PURGE_PID" ] || kill "$PURGE_PID" 2>/dev/null
  stop_server
  exit 0
}

# k3s mode: the NIC once per container start; a refusal (422: data the portal does not accept) is not retried
report_wake() {
  local payload
  payload="$(jq -nc --arg name "$NODE_NAME" --argjson wake "$WAKE" '{name: $name} + $wake')"
  until call POST /api/nodes/wake "$payload" && [ "$HTTP_CODE" = 200 ]; do
    [ "$STOPPING" = 1 ] && return 0
    if [ "$HTTP_CODE" = 422 ] || [ "$HTTP_CODE" = 403 ]; then
      log "wake-on-lan: the portal refused the NIC $WOL_MAC ($WOL_IP/$WOL_PREFIX): ${BODY:0:200}"
      return 1
    fi
    log "wake-on-lan: NIC report failed (HTTP $HTTP_CODE): ${BODY:0:200}; retrying in $WAKE_RETRY_S s"
    nap "$WAKE_RETRY_S"
  done
  log "wake-on-lan: NIC $WOL_MAC ($WOL_IP/$WOL_PREFIX) reported to the portal"
}

# k3s mode: the image once per container start; a portal without the endpoint (404) or that refuses it (422) is not retried
report_image_until_ok() {
  until report_image; do
    [ "$STOPPING" = 1 ] && return 0
    case "$HTTP_CODE" in
      404|422) log "image report not taken by the portal (HTTP $HTTP_CODE): the portal shows no version for this node"; return 1 ;;
      403) log "image report refused: $(refusal_text)"; return 1 ;;
    esac
    log "image report failed (HTTP $HTTP_CODE): ${BODY:0:200}; retrying in $WAKE_RETRY_S s"
    nap "$WAKE_RETRY_S"
  done
  log "image ${NODE_IMAGE:-?} (version $NODE_VERSION, digest ${NODE_IMAGE_DIGEST:-unknown}) reported to the portal"
}

# k3s mode: the size of the models volume every MODELS_REPORT_S seconds; a refusal (422) or an old portal (404) is not retried
report_models() {
  local payload bytes
  [ "$MODELS_REPORT_S" != 0 ] || return 0
  while [ "$STOPPING" = 0 ]; do
    bytes="$(timeout 60 models-cleanup.sh --usage 2>/dev/null)"
    if [[ "$bytes" =~ ^[0-9]+$ ]]; then
      payload="$(jq -nc --arg name "$NODE_NAME" --argjson b "$bytes" '{name: $name, models_bytes: $b}')"
      if call POST /api/nodes/models "$payload" && [ "$HTTP_CODE" = 200 ]; then
        nap "$MODELS_REPORT_S"
        continue
      fi
      if [ "$HTTP_CODE" = 404 ] || [ "$HTTP_CODE" = 422 ]; then
        log "models report: the portal does not take it (HTTP $HTTP_CODE): ${BODY:0:200}"
        return 1
      fi
      [ "$HTTP_CODE" = 403 ] && [ "$(refusal_code)" = node_key_invalid ] && drop_key
      log "models report failed (HTTP $HTTP_CODE): ${BODY:0:200}; retrying in $WAKE_RETRY_S s"
      nap "$WAKE_RETRY_S"
    else
      nap "$MODELS_REPORT_S"
    fi
  done
}

if [ "$MODELS_REPORT" = 1 ]; then
  setup_tls || exit 0
  report_models
  exit $?
fi

if [ "$WAKE_REPORT" = 1 ]; then
  setup_tls || exit 0
  # the identity first (its key goes with the reports); a portal that does not answer yet does not hold the reports up
  enroll_once
  rc=0
  if [ "$WAKE" != null ]; then report_wake || rc=$?; else log "wake-on-lan: no NIC given (WOL_MAC, WOL_IP, WOL_PREFIX), nothing to report"; fi
  report_image_until_ok || true
  # The identity keeper of the container: while the node has a key (or could not register yet) it stays, and a key the
  # portal refused (node-metrics.sh or the models report drop it) is replaced by a new registration, which then waits for
  # the admin's approval. A new registration reports the NIC and the image again (refused while the name was unknown).
  if [ -n "$(node_key)" ] || [ "$ENROLL_STATE" = retry ]; then
    while [ "$STOPPING" = 0 ]; do
      nap "$ENROLL_RETRY_S"
      [ "$STOPPING" = 1 ] && break
      [ -z "$(node_key)" ] || continue
      if enroll_once && [ -n "$(node_key)" ]; then
        [ "$WAKE" = null ] || report_wake || true
        report_image_until_ok || true
      fi
    done
    exit 0
  fi
  exit $rc
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
