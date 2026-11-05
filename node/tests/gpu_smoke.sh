#!/usr/bin/env bash
# GPU smoke test of the gpu image in standalone mode. Meant for the GPU build server (newbar),
# run from the directory that holds the node/ files:   bash tests/gpu_smoke.sh
# Needs only docker, python3 and curl. Uses containers it-t2-gpu-smoke and it-t2-mock, network it-t2-net, host port 28080.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-nsu-llm-node:0.2.0-gpu}"
CTR="it-t2-gpu-smoke"
MOCK="it-t2-mock"
NET="it-t2-net"
MOCK_IMAGE="${MOCK_IMAGE:-python:3.11-slim}"
VOL="it-t2-gpu-models"
HOST_PORT=28080
MOCK_PORT=28090
TMP="$(mktemp -d)"
CALLS="$TMP/calls.json"
DESIRED="$TMP/desired.json"
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
cleanup() {
  docker rm -f "$CTR" "$MOCK" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker volume rm -f "$VOL" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

py() { python3 -c "$@"; }
last_applied() {
  py 'import json,sys
c=[x for x in json.load(open(sys.argv[1])) if x["path"]=="/api/nodes/heartbeat"]
print(c[-1]["body"]["applied_spec_hash"] if c else "none")' "$CALLS" 2>/dev/null
}

cat > "$DESIRED" <<'EOF'
{"deployment":"t2gpu","model_repo":"ggml-org/gemma-3-270m-it-GGUF","model_file":"gemma-3-270m-it-Q8_0.gguf",
 "public_model_name":"test-model","ctx_size":2048,"parallel":2,"spec_hash":"g1"}
EOF
# The host firewall drops container -> host traffic, so the mock portal runs in its own container
# (existing python image, nothing is pulled) on a private network shared with the node container.
docker network create "$NET" >/dev/null || { echo "network create failed"; exit 1; }
docker run -d --name "$MOCK" --network "$NET" --user "$(id -u):$(id -g)" -v "$HERE:/t:ro" -v "$TMP:/w" "$MOCK_IMAGE"   python /t/mock_main.py --port "$MOCK_PORT" --calls /w/calls.json --desired /w/desired.json   --token smoke-token --interval 3 >/dev/null || { echo "mock start failed"; exit 1; }
sleep 2

echo "GPU before: $(nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader)"
docker run -d --name "$CTR" --hostname it-t2-gpu --gpus all --network "$NET" -p "$HOST_PORT:18080"   -v "$VOL:/var/lib/llm-models"   -e MODE=standalone -e "MAIN_URL=http://$MOCK:$MOCK_PORT" -e MAIN_INSECURE_TLS=1   -e NODE_IP=127.0.0.1 -e CLUSTER_TOKEN=smoke-token -e NODE_NAME=it-t2-gpu -e ACCEL=auto   "$IMAGE" >/dev/null || { echo "docker run failed"; exit 1; }

end=$((SECONDS + 300))
until [ "$(last_applied)" = g1 ]; do
  if [ "$SECONDS" -ge "$end" ]; then fail "heartbeat reports g1 (timeout)"; docker logs "$CTR" 2>&1 | tail -40; exit 1; fi
  sleep 2
done
pass "heartbeat reports g1"

py 'import json,sys
r=[x for x in json.load(open(sys.argv[1])) if x["path"]=="/api/nodes/register"][0]["body"]
print("register:", r)
assert r["accel"]=="nvidia", r
assert isinstance(r["vram_mib"], int) and r["vram_mib"] > 1000, r' "$CALLS" \
  && pass "registered as nvidia with vram_mib" || fail "register payload (accel/vram)"

docker logs "$CTR" 2>&1 | grep -iE "offloaded|CUDA|ggml_cuda_init|using device" | head -6

body='{"model":"test-model","max_tokens":64,"messages":[{"role":"user","content":"Write one sentence about the sea."}]}'
# first request warms the CUDA kernels up, the second one is measured
curl -s -m 120 -H 'Content-Type: application/json' -d "$body" "http://127.0.0.1:$HOST_PORT/v1/chat/completions" > "$TMP/chat.json"
curl -s -m 120 -H 'Content-Type: application/json' -d "$body" "http://127.0.0.1:$HOST_PORT/v1/chat/completions" > "$TMP/chat.json"
py 'import json,sys
d=json.load(open(sys.argv[1]))
c=d["choices"][0]["message"]["content"]
t=d.get("timings",{})
print("content:", repr(c[:100]))
print("tokens/s (generation): %.1f, prompt tokens/s: %.1f, predicted_n: %s" % (t.get("predicted_per_second",0), t.get("prompt_per_second",0), t.get("predicted_n")))
assert len(c) > 0' "$TMP/chat.json" && pass "chat completion returns content" || fail "chat completion"

pids="$(docker top "$CTR" -eo pid,comm | awk '$2=="llama-server" {print $1}')"
echo "container llama-server host pids: $pids"
apps="$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader)"
found=0
for p in $pids; do
  line="$(echo "$apps" | awk -F', ' -v p="$p" '$1==p')"
  if [ -n "$line" ]; then echo "nvidia-smi shows: $line"; found=1; fi
done
[ "$found" = 1 ] && pass "nvidia-smi shows the container's llama-server" || { fail "llama-server not in nvidia-smi compute apps"; echo "$apps"; }
echo "GPU during: $(nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader)"

docker stop "$CTR" >/dev/null
py 'import json,sys
c=json.load(open(sys.argv[1]))
assert sum(1 for x in c if x["method"]=="POST" and x["path"]=="/api/nodes/it-t2-gpu/stop")==1
assert not any(x["method"]=="DELETE" for x in c)' "$CALLS" \
  && pass "POST /stop recorded on docker stop, no DELETE (the NIC stays for Wake-on-LAN)" || fail "POST /stop on stop"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
