#!/usr/bin/env bash
# Offline tests of the monitoring collector node-metrics.sh and of its start in entrypoint.sh (no docker, no network).
# Needs bash, jq, curl-compatible stubs only: nvidia-smi, curl, k3s and registrar.sh are replaced by fakes.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
BIN="$NODE/rootfs/opt/llm/bin"
SCRIPT="$BIN/node-metrics.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing: $3)"; echo "$2" | sed 's/^/    | /' ;; esac; }
lacks() { case "$2" in *"$3"*) fail "$1 (unexpected: $3)" ;; *) pass "$1" ;; esac; }
eq() { [ "$2" = "$3" ] && pass "$1" || fail "$1 (got '$2', want '$3')"; }
field() { jq -r "$2" <<<"$1"; } # JSON, jq expression

if bash -n "$SCRIPT" 2>"$TMP/syn"; then pass "bash -n node-metrics.sh"; else fail "bash -n node-metrics.sh"; cat "$TMP/syn"; fi
command -v jq >/dev/null 2>&1 || { fail "jq is needed to run these tests"; exit 1; }

# ---- a fake container: /proc, the model cache, nvidia-smi ----
PROC="$TMP/proc"
mkdir -p "$PROC/1" "$PROC/42" "$PROC/77" "$TMP/models/org__repo" "$TMP/fakebin" "$TMP/state"
echo k3s > "$PROC/1/comm"
echo llama-server > "$PROC/42/comm"
echo bash > "$PROC/77/comm"
printf 'cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1\n' > "$PROC/stat"
printf 'MemTotal:       16777216 kB\nMemFree:  100 kB\nMemAvailable:    4194304 kB\n' > "$PROC/meminfo"
head -c 3000 /dev/zero > "$TMP/models/org__repo/m.gguf"
cat > "$TMP/fakebin/nvidia-smi" <<'EOS'
#!/usr/bin/env bash
case "$*" in
  *--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw*) ;;
  *) exit 1 ;;
esac
echo "37, 6001, 12282, 61, 120.50"
echo "5, 100, 12282, 48, [N/A]"
EOS
chmod +x "$TMP/fakebin/nvidia-smi"
printf '#!/usr/bin/env bash\nexit 9\n' > "$TMP/fakebin/nvidia-smi-broken"
export PROC_DIR="$PROC" PROC_STAT="$PROC/stat" PROC_MEMINFO="$PROC/meminfo" CGROUP_MEMORY_MAX="$TMP/absent" \
  CGROUP_MEMORY_CURRENT="$TMP/absent" LLAMA_CACHE="$TMP/models" METRICS_CPU_SAMPLE_S=0

once() { PATH="$TMP/fakebin:$PATH" bash "$SCRIPT" --once 2>"$TMP/once.err"; }

# ---- one sample ----
out="$(once)"
eq "sample is one JSON object" "$(field "$out" 'type')" object
eq "two GPUs" "$(field "$out" '.gpus | length')" 2
eq "gpu 0 utilisation" "$(field "$out" '.gpus[0].util_pct')" 37
eq "gpu 0 vram used" "$(field "$out" '.gpus[0].vram_used_mib')" 6001
eq "gpu 0 vram total" "$(field "$out" '.gpus[0].vram_total_mib')" 12282
eq "gpu 0 temperature" "$(field "$out" '.gpus[0].temp_c')" 61
eq "gpu 0 power" "$(field "$out" '.gpus[0].power_w')" 120.5
eq "a field nvidia-smi cannot give is null" "$(field "$out" '.gpus[1].power_w')" null
eq "ram used is MemTotal - MemAvailable" "$(field "$out" '.ram_used_mib')" 12288
eq "ram total" "$(field "$out" '.ram_total_mib')" 16384
eq "the model cache size is counted" "$(field "$out" '.models_bytes | . >= 3000')" true
eq "the free disk space is there" "$(field "$out" '.disk_free_bytes | . > 0')" true
eq "llama-server is running" "$(field "$out" '.llama_up')" true
eq "no rpc server" "$(field "$out" '.rpc_up')" false
eq "no change of the cpu counters is 0 %" "$(field "$out" '.cpu_pct == 0')" true

mkdir -p "$PROC/90"; echo ggml-rpc-server > "$PROC/90/comm"
eq "an rpc server is seen" "$(field "$(once)" '.rpc_up')" true
rm -rf "$PROC/42"
eq "llama-server gone" "$(field "$(once)" '.llama_up')" false

# no GPU (a CPU node): nvidia-smi is missing (exit 127) or fails
mkdir -p "$TMP/failbin"; printf '#!/usr/bin/env bash
exit 9
' > "$TMP/failbin/nvidia-smi"; chmod +x "$TMP/failbin/nvidia-smi"
out="$(PATH="$TMP/failbin:$PATH" bash "$SCRIPT" --once 2>/dev/null)"
eq "a failing nvidia-smi gives no GPUs" "$(field "$out" '.gpus | length')" 0
out="$(LLAMA_CACHE="$TMP/no-such-dir" once)"
eq "no model cache dir: null size" "$(field "$out" '.models_bytes')" null
# a hung mount: du does not answer, the sample still comes (size null) within the timeout
mkdir -p "$TMP/hungbin"; printf '#!/usr/bin/env bash
sleep 30
' > "$TMP/hungbin/du"; chmod +x "$TMP/hungbin/du"
t0=$SECONDS
out="$(PATH="$TMP/hungbin:$PATH" DU_TIMEOUT_S=1 once)"
eq "a hung du gives a null size" "$(field "$out" '.models_bytes')" null
eq "a hung du does not hold the sample" "$((SECONDS - t0 < 10))" 1

# the memory limit of the container (cgroup) caps the total and gives the used part
echo 4294967296 > "$TMP/memory.max"; echo 1073741824 > "$TMP/memory.current"
out="$(CGROUP_MEMORY_MAX="$TMP/memory.max" CGROUP_MEMORY_CURRENT="$TMP/memory.current" once)"
eq "cgroup limit is the total" "$(field "$out" '.ram_total_mib')" 4096
eq "cgroup usage is the used part" "$(field "$out" '.ram_used_mib')" 1024
echo max > "$TMP/memory.max"
out="$(CGROUP_MEMORY_MAX="$TMP/memory.max" once)"
eq "no cgroup limit: MemTotal" "$(field "$out" '.ram_total_mib')" 16384

# ---- the CPU share between two readings ----
# shellcheck disable=SC1090
source "$SCRIPT"
eq "cpu: half busy" "$(cpu_pct 1000 500 2000 1000)" 50.0
eq "cpu: idle" "$(cpu_pct 1000 500 2000 1500)" 0.0
eq "cpu: all busy" "$(cpu_pct 1000 500 2000 500)" 100.0
eq "cpu: no time passed" "$(cpu_pct 1000 500 1000 500)" 0.0
eq "cpu: counters reset" "$(cpu_pct 5000 500 1000 100)" 0.0
eq "cpu times: busy and idle" "$(cpu_times)" "1000 800"

# ---- the interval stays within 10-60 s ----
for pair in "5 10" "20 20" "999 60" "abc 20"; do
  set -- $pair
  got="$(METRICS_INTERVAL_S="$1" bash -c 'source "$1"; echo $INTERVAL' x "$SCRIPT")"
  eq "interval '$1' -> $2" "$got" "$2"
done

# ---- the push loop with a fake curl ----
cat > "$TMP/fakebin/curl" <<'EOS'
#!/usr/bin/env bash
# logs the arguments and the header files; answers FAKE_CODE (default 204)
hdr=""
for a in "$@"; do
  case "$a" in @*) hdr="$hdr $(cat "${a#@}" 2>/dev/null | tr -d '\r')" ;; esac
done
echo "curl $* | headers:$hdr" >> "$FAKE_CURL_LOG"
if [ "${FAKE_CURL_FAIL:-0}" = 1 ]; then echo -n 000; exit 7; fi
printf '%s' "${FAKE_CODE:-204}"
EOS
chmod +x "$TMP/fakebin/curl"

run_loop() { # extra env assignments...; leaves LOOP_PID
  : > "$TMP/curl.log"
  env PATH="$TMP/fakebin:$PATH" FAKE_CURL_LOG="$TMP/curl.log" MAIN_URL=http://main.example:8000/ CLUSTER_TOKEN=tok-secret-1 \
    NODE_NAME=Win-07 METRICS_INTERVAL_S=10 STATE_DIR="$TMP/state" "$@" bash "$SCRIPT" >"$TMP/loop.out" 2>&1 &
  LOOP_PID=$!
}
wait_log() { for _ in $(seq 100); do [ "$(grep -c . "$TMP/curl.log")" -ge "$1" ] && return 0; sleep 0.1; done; return 1; }

run_loop
wait_log 1 && pass "the loop pushes a sample" || fail "the loop pushes a sample"
log="$(cat "$TMP/curl.log")"
contains "pushed to the metrics route of the lower-cased node name" "$log" "http://main.example:8000/api/nodes/win-07/metrics"
contains "pushed with POST" "$log" "-X POST"
contains "the body is the sample" "$log" '"ram_total_mib":16384'
contains "the token is sent as a bearer header" "$log" "headers: Authorization: Bearer tok-secret-1"
lacks "the token is not on the command line" "${log%% | headers:*}" "tok-secret-1"
start=$SECONDS
kill -TERM "$LOOP_PID"; wait "$LOOP_PID" 2>/dev/null
[ $((SECONDS - start)) -le 3 ] && pass "TERM stops the loop at once" || fail "TERM stops the loop at once"

# a portal that refuses is logged once
run_loop FAKE_CODE=503
wait_log 1 || fail "the loop pushes against a refusing portal"
sleep 0.5
kill -TERM "$LOOP_PID"; wait "$LOOP_PID" 2>/dev/null
contains "a refusal is logged once with its HTTP code" "$(cat "$TMP/loop.out")" "the portal did not take the sample (HTTP 503)"
run_loop FAKE_CURL_FAIL=1
wait_log 1 || fail "the loop pushes against an unreachable portal"
sleep 0.5
kill -TERM "$LOOP_PID"; wait "$LOOP_PID" 2>/dev/null
contains "an unreachable portal is logged with HTTP 000" "$(cat "$TMP/loop.out")" "HTTP 000"

# https: the portal CA is fetched once and used; MAIN_INSECURE_TLS skips verification
cat > "$TMP/fakebin/curl" <<'EOS'
#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_CURL_LOG"
case "$*" in
  *"/ca.crt"*) for ((i = 1; i <= $#; i++)); do [ "${!i}" = -o ] && { j=$((i + 1)); echo CERT > "${!j}"; }; done; printf 200 ;;
  *) printf 204 ;;
esac
EOS
rm -f "$TMP/state/ca.crt"
run_loop MAIN_URL=https://main.example
wait_log 2 || fail "https: the CA is fetched and the sample pushed"
kill -TERM "$LOOP_PID"; wait "$LOOP_PID" 2>/dev/null
log="$(cat "$TMP/curl.log")"
contains "https: the CA of the portal is fetched without verification (trust on first use)" "$log" "-k"
contains "https: the sample is verified against the saved CA" "$log" "--cacert $TMP/state/ca.crt"
eq "https: the CA is saved for the registrar too" "$(cat "$TMP/state/ca.crt" 2>/dev/null)" CERT
rm -f "$TMP/state/ca.crt"
run_loop MAIN_URL=https://main.example MAIN_INSECURE_TLS=1
wait_log 1 || fail "https insecure: the sample is pushed"
kill -TERM "$LOOP_PID"; wait "$LOOP_PID" 2>/dev/null
log="$(cat "$TMP/curl.log")"
contains "MAIN_INSECURE_TLS pushes with -k" "$log" "-k"
lacks "MAIN_INSECURE_TLS fetches no CA" "$log" "/ca.crt"

# no token: the collector refuses to start
out="$(env -u CLUSTER_TOKEN MAIN_URL=http://m:1 bash "$SCRIPT" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "no token: refuses to start" || fail "no token: refuses to start"
contains "no token: says why" "$out" "CLUSTER_TOKEN is required"

# ---- entrypoint.sh starts the collector in both modes ----
mkdir -p "$TMP/epbin"
printf '#!/usr/bin/env bash\necho "metrics NODE_NAME=${NODE_NAME-unset} CLUSTER_TOKEN=${CLUSTER_TOKEN-unset} MAIN_URL=${MAIN_URL-unset}" >> "$FAKE_EP_LOG"\n' > "$TMP/epbin/node-metrics.sh"
printf '#!/usr/bin/env bash\necho "registrar ran" >> "$FAKE_EP_LOG"\n' > "$TMP/epbin/registrar.sh"
printf '#!/usr/bin/env bash\necho "k3s ran with K3S_TOKEN=${K3S_TOKEN-unset} CLUSTER_TOKEN=${CLUSTER_TOKEN-unset}" >> "$FAKE_EP_LOG"\n' > "$TMP/epbin/k3s"
printf '#!/usr/bin/env bash\necho cpu\n' > "$TMP/epbin/detect-accel.sh"
chmod +x "$TMP/epbin/"*
export K3S_NODE_ETC="$TMP/etc-node" K3S_DATA_DIR="$TMP/k3s-data" K3S_REGISTRIES_FILE="$TMP/etc-k3s/registries.yaml"
ep() { : > "$TMP/ep.log"; env PATH="$TMP/epbin:$PATH" FAKE_EP_LOG="$TMP/ep.log" ACCEL=cpu NODE_NAME=foo "$@" bash "$BIN/entrypoint.sh" >/dev/null 2>&1; sleep 0.5; cat "$TMP/ep.log"; }

out="$(ep MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok)"
contains "k3s mode starts the collector" "$out" "metrics NODE_NAME=foo CLUSTER_TOKEN=tok MAIN_URL=https://main.example"
contains "k3s mode still runs k3s (the token is K3S_TOKEN there)" "$out" "k3s ran with K3S_TOKEN=tok CLUSTER_TOKEN=unset"
out="$(ep MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_METRICS=0)"
lacks "NODE_METRICS=0 switches the collector off (k3s)" "$out" "metrics NODE_NAME"
contains "NODE_METRICS=0 still runs k3s" "$out" "k3s ran"
out="$(ep MODE=standalone MAIN_URL=http://main.example:8000 CLUSTER_TOKEN=tok NODE_IP=10.0.0.5)"
contains "standalone mode starts the collector" "$out" "metrics NODE_NAME=foo CLUSTER_TOKEN=tok MAIN_URL=http://main.example:8000"
contains "standalone mode still runs the registrar" "$out" "registrar ran"
out="$(ep MODE=standalone MAIN_URL=http://main.example:8000 CLUSTER_TOKEN=tok NODE_IP=10.0.0.5 NODE_METRICS=0)"
lacks "NODE_METRICS=0 switches the collector off (standalone)" "$out" "metrics NODE_NAME"

# ---- llama-server exposes /metrics, behind the backend key ----
out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu LLAMA_CACHE="$TMP/models" BACKEND_API_KEY=backend-key-1   bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "llama-server is started with --metrics" "$out" " --metrics "
eq "--metrics appears once" "$(grep -o -- '--metrics' <<<"$out" | wc -l | tr -d ' ')" 1
lacks "the backend key is not on the command line" "$out" "backend-key-1"
contains "the key stays in LLAMA_API_KEY, which guards /metrics too" "$out" "LLAMA_API_KEY is set (not shown)"
grep -qx -- '--metrics' "$NODE/rootfs/opt/llm/forbidden-flags"   && pass "an admin cannot pass --metrics or turn it off himself (forbidden flag)" || fail "--metrics is a forbidden flag"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
