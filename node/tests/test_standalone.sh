#!/usr/bin/env bash
# End-to-end test of the node image in standalone mode against a mock portal (CPU image).
# Run through test-lock (it starts containers); builds the cpu image only if it is missing.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
IMAGE="${IMAGE:-nsu-llm-node:0.2.0-cpu}"
CTR="it-t2-standalone"
VOL="it-t2-models"
MOCK_PORT="${MOCK_PORT:-$((29100 + RANDOM % 800))}"
HOST_PORT="${HOST_PORT:-28080}"
NAME="it-t2-node"
TOKEN="test-token"
MOCK_HOST="${MOCK_HOST:-host.docker.internal}"
PY="${PYTHON:-python}"
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
TMP="$(winpath "$(mktemp -d)")"   # mixed-style path works for bash, python, curl and jq on Windows
CALLS="$TMP/calls.json"
DESIRED="$TMP/desired.json"
FAILS=0
MOCK_PID=""

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }

cleanup() {
  docker rm -f "$CTR" >/dev/null 2>&1
  [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null
  [ "${KEEP_CACHE:-0}" = 1 ] || docker volume rm -f "$VOL" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

calls_ok() { jq -e "$1" "$CALLS" >/dev/null 2>&1; }
# wait_for DESC TIMEOUT_S CMD...
wait_for() {
  local desc="$1" limit="$2"; shift 2
  local end=$((SECONDS + limit))
  until "$@"; do
    if [ "$SECONDS" -ge "$end" ]; then fail "$desc (timeout ${limit}s)"; return 1; fi
    sleep 2
  done
  pass "$desc"
}
write_desired() { # hash ctx
  cat > "$DESIRED" <<EOF
{"deployment":"t2","model_repo":"ggml-org/gemma-3-270m-it-GGUF","model_file":"gemma-3-270m-it-Q8_0.gguf",
 "public_model_name":"test-model","ctx_size":$2,"parallel":2,"spec_hash":"$1"}
EOF
}
chat() { # max_tokens content -> prints http code, body in $TMP/chat.out
  jq -nc --arg c "$2" --argjson m "$1" '{model:"test-model",max_tokens:$m,messages:[{role:"user",content:$c}]}' \
    | curl -s -m 120 -o "$TMP/chat.out" -w '%{http_code}' -H 'Content-Type: application/json' \
        -d @- "http://127.0.0.1:$HOST_PORT/v1/chat/completions"
}
slot_ctx() { curl -s -m 10 "http://127.0.0.1:$HOST_PORT/props" | jq -r '.default_generation_settings.n_ctx'; }
hb_reported() { calls_ok "[.[] | select(.path==\"/api/nodes/heartbeat\")] | last | .body.applied_spec_hash == $1"; }
infer_down() { ! curl -s -m 3 -o /dev/null "http://127.0.0.1:$HOST_PORT/health"; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE"
  docker build --provenance=false -f "$NODE/Dockerfile" --target cpu --build-arg JOBS=4 -t "$IMAGE" "$NODE" || { echo "build failed"; exit 1; }
fi

write_desired h1 2048
"$PY" "$(winpath "$HERE/mock_main.py")" --port "$MOCK_PORT" --calls "$(winpath "$CALLS")" \
  --desired "$(winpath "$DESIRED")" --token "$TOKEN" --interval 3 &
MOCK_PID=$!
wait_for "mock portal is up" 20 test -s "$CALLS" || exit 1

docker rm -f "$CTR" >/dev/null 2>&1
docker run -d --name "$CTR" --hostname "$NAME" --add-host host.docker.internal:host-gateway \
  -v "$VOL:/var/lib/llm-models" -p "$HOST_PORT:18080" \
  -e MODE=standalone -e "MAIN_URL=http://$MOCK_HOST:$MOCK_PORT" -e MAIN_INSECURE_TLS=1 \
  -e NODE_IP=127.0.0.1 -e CLUSTER_TOKEN="$TOKEN" -e NODE_NAME="$NAME" -e ACCEL=cpu \
  "$IMAGE" >/dev/null || { echo "docker run failed"; exit 1; }

wait_for "register call recorded" 60 calls_ok '[.[] | select(.path=="/api/nodes/register")] | length >= 1' || { docker logs "$CTR" 2>&1 | tail -20; exit 1; }
calls_ok '[.[] | select(.path=="/api/nodes/register")][0].body | (.name=="it-t2-node" and .accel=="cpu" and .version=="0.2.0" and .vram_mib==null and (.ram_mib|type=="number") and (.url=="http://127.0.0.1:18080"))' \
  && pass "register payload fields" || fail "register payload fields"
calls_ok 'all(.[]; .auth=="Bearer test-token")' && pass "bearer token sent" || fail "bearer token sent"
# the image the container runs goes to the portal after the registration (task 2-30): version and revision come from the image
wait_for "image report recorded" 30 calls_ok '[.[] | select(.method=="POST" and .path=="/api/nodes/it-t2-node/image")] | length >= 1'
calls_ok '[.[] | select(.path=="/api/nodes/it-t2-node/image")][0].body | (.version=="0.2.0" and (.revision|type=="string") and .digest=="" and .tag=="")'   && pass "image report payload (version from the image, no digest or name without run-node)" || fail "image report payload"

# --- desired h1 ---
wait_for "heartbeat reports h1" 300 hb_reported '"h1"' || { docker logs "$CTR" 2>&1 | tail -30; exit 1; }
code="$(chat 16 'Say hello.')"
if [ "$code" = 200 ] && jq -e '.choices[0].message.content | length > 0' "$TMP/chat.out" >/dev/null 2>&1; then
  pass "chat completion returns content (model=test-model)"
else fail "chat completion (HTTP $code)"; head -c 400 "$TMP/chat.out"; echo; fi
jq -e '.model == "test-model"' "$TMP/chat.out" >/dev/null 2>&1 && pass "response carries public model name" || fail "response model name"

n1="$(slot_ctx)"
echo "info - /props n_ctx for ctx_size=2048 parallel=2: $n1"
[ "$n1" = 1024 ] && pass "each slot gets ctx_size/parallel = 1024" || fail "slot n_ctx is $n1, expected 1024"

long="$(python -c 'print("lorem ipsum dolor sit amet " * 700)' 2>/dev/null || "$PY" -c 'print("lorem ipsum dolor sit amet " * 700)')"
code="$(chat 16 "$long")"
if [ "$code" -ge 400 ] 2>/dev/null; then pass "prompt above ctx_size is rejected (HTTP $code)"; else fail "oversized prompt not rejected (HTTP $code)"; fi
mid="$("$PY" -c 'print("lorem ipsum dolor sit amet " * 230)')"
code="$(chat 16 "$mid")"
if [ "$code" -ge 400 ] 2>/dev/null; then pass "prompt above per-slot ctx (1024) is rejected (HTTP $code)"; else fail "per-slot limit not enforced (HTTP $code)"; fi

# --- 404 on heartbeat => re-register ---
calls_ok '[.[] | select(.path=="/api/nodes/register")] | length >= 2' && pass "re-registered after heartbeat 404" || fail "re-register after 404"

# --- desired h2 (ctx 4096) ---
write_desired h2 4096
wait_for "heartbeat reports h2" 300 hb_reported '"h2"' || { docker logs "$CTR" 2>&1 | tail -30; exit 1; }
n2="$(slot_ctx)"
[ "$n2" = 2048 ] && pass "props changed after reconfigure (n_ctx $n1 -> $n2)" || fail "props after reconfigure: $n2"
code="$(chat 16 'Say hello.')"
[ "$code" = 200 ] && pass "chat works after reconfigure" || fail "chat after reconfigure (HTTP $code)"

# --- desired null ---
echo null > "$DESIRED"
wait_for "inference port stops answering" 120 infer_down
wait_for "heartbeat reports null" 60 hb_reported null

# --- desired again (h3), then stop with a running server => POST .../stop (not DELETE) and clean exit ---
write_desired h3 2048
wait_for "heartbeat reports h3" 300 hb_reported '"h3"' || { docker logs "$CTR" 2>&1 | tail -30; exit 1; }
t0=$SECONDS
docker stop "$CTR" >/dev/null
took=$((SECONDS - t0))
echo "info - docker stop took $took s"
[ "$took" -lt 40 ] && pass "docker stop finished within the 45 s grace period" || fail "docker stop took $took s"
calls_ok '[.[] | select(.method=="POST" and .path=="/api/nodes/it-t2-node/stop" and .auth=="Bearer test-token")] | length == 1' \
  && pass "stop recorded on docker stop" || fail "stop recorded on docker stop"
calls_ok '[.[] | select(.method=="DELETE")] | length == 0' \
  && pass "docker stop does not deregister (the NIC stays for Wake-on-LAN)" || fail "docker stop does not deregister"
rc="$(docker inspect -f '{{.State.ExitCode}}' "$CTR" 2>/dev/null)"
[ "$rc" = 0 ] && pass "container exited 0 on TERM" || fail "container exit code $rc"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; docker logs "$CTR" 2>&1 | tail -40; exit 1; fi
