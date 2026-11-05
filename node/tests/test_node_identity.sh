#!/usr/bin/env bash
# Offline tests of the node's own key (task SEC-1, portal/app/nodes/identity.py): registrar.sh keeps the key the portal
# issues, sends it as X-Node-Key with every call (header file, never argv), derives its backend key for the k3s pods,
# drops a key the portal refuses and registers again; node-metrics.sh sends it too; serve-llama.sh reads the backend key
# from LLAMA_API_KEY_FILE. No docker, no network: curl is a fake that answers like the portal.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
BIN="$NODE/rootfs/opt/llm/bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing: $3)"; echo "$2" | sed 's/^/    | /' ;; esac; }
lacks() { case "$2" in *"$3"*) fail "$1 (unexpected: $3)" ;; *) pass "$1" ;; esac; }

KEY="AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcde"   # 43 characters, like secrets.token_urlsafe(32)
KEY2="ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210-_edcba"
BACKEND="$(printf '%s' "nsu-llm-backend:$KEY" | sha256sum | cut -d' ' -f1)"

mkdir -p "$TMP/bin" "$TMP/serve"
# fake curl: logs argv and the header files; answers per path. FAKE_ISSUE: the key register/enroll hands out (empty: none);
# FAKE_INVALID_ONCE: a file whose presence makes the next heartbeat answer 403 node_key_invalid (then it is removed)
cat > "$TMP/bin/curl" <<'EOS'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
prev="" hdr=""
for a in "$@"; do [ "$prev" = -H ] && case "$a" in @*) hdr="$(cat "${a#@}")"; printf '%s\n--\n' "$hdr" >> "$FAKE_LOG.hdr" ;; esac; prev="$a"; done
for a in "$@"; do
  case "$a" in
    *://*/ca.crt) printf '404'; exit 0 ;;
    */api/nodes/register|*/api/nodes/enroll)
      if [ -n "${FAKE_ISSUE:-}" ] && ! grep -q '^X-Node-Key:' <<<"$hdr"; then
        printf '{"ok":true,"heartbeat_interval":1,"approved":%s,"node_key":"%s"}\n200' "${FAKE_APPROVED:-true}" "$FAKE_ISSUE"
      else
        printf '{"ok":true,"heartbeat_interval":1,"approved":%s}\n200' "${FAKE_APPROVED:-true}"
      fi
      exit 0 ;;
    */api/nodes/heartbeat)
      if [ -n "${FAKE_INVALID_ONCE:-}" ] && [ -e "$FAKE_INVALID_ONCE" ]; then
        rm -f "$FAKE_INVALID_ONCE"
        printf '{"detail":{"code":"node_key_invalid","message":"reset"}}\n403'; exit 0
      fi
      printf '{"ok":true,"heartbeat_interval":1,"approved":%s,"desired":null}\n200' "${FAKE_APPROVED:-true}"; exit 0 ;;
    */metrics)
      if [ -n "${FAKE_METRICS_INVALID:-}" ]; then printf '{"detail":{"code":"node_key_invalid","message":"x"}}\n403'; exit 0; fi
      printf '\n204'; exit 0 ;;
    */api/nodes/*) printf '{"ok":true}\n200'; exit 0 ;;
  esac
done
exit 0
EOS
chmod +x "$TMP/bin/curl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/serve/serve-llama.sh"
printf '#!/usr/bin/env bash\n[ "${1:-}" = --usage ] && echo 1\nexit 0\n' > "$TMP/serve/models-cleanup.sh"
chmod +x "$TMP/serve/serve-llama.sh" "$TMP/serve/models-cleanup.sh"

run_reg() { # [registrar args...] -> output; state in $TMP/state, logs in $TMP/curl.log(.hdr)
  : > "$TMP/curl.log"; : > "$TMP/curl.log.hdr"
  FAKE_LOG="$TMP/curl.log" PATH="$TMP/bin:$TMP/serve:$BIN:$PATH" CLUSTER_TOKEN=clustersecret42 NODE_IP=10.0.0.9 \
    NODE_NAME=lab-1 MAIN_HOST=main.example ACCEL=cpu STATE_DIR="$TMP/state" WAKE_RETRY_S=1 ENROLL_RETRY_S=1 \
    timeout "${REG_TIMEOUT:-4}" bash "$BIN/registrar.sh" "$@" 2>&1
}

# --- standalone: the key of the first registration ---------------------------------------------------------------
rm -rf "$TMP/state"
out="$(FAKE_ISSUE="$KEY" run_reg)"
contains "registrar keeps the key the portal issued" "$(cat "$TMP/state/node.key" 2>/dev/null)" "$KEY"
mode="$(stat -c %a "$TMP/state/node.key" 2>/dev/null)"
[ "$mode" = 600 ] && pass "the key file is root only (0600)" || fail "the key file is root only (0600), got $mode"
[ "$(cat "$TMP/state/backend/key" 2>/dev/null)" = "$BACKEND" ] \
  && pass "the backend key is sha256(nsu-llm-backend:<key>), like the portal's" || fail "the backend key is derived from the key"
lacks "the key is not on a curl command line" "$(cat "$TMP/curl.log")" "$KEY"
lacks "the key is not in the log" "$out" "$KEY"
hb="$(awk '/X-Node-Key/' "$TMP/curl.log.hdr")"
contains "later calls carry the key in the header file" "$hb" "X-Node-Key: $KEY"
contains "and the agent token next to it" "$(cat "$TMP/curl.log.hdr")" "Authorization: Bearer clustersecret42"

# a restart: the key on the volume goes with the registration, no new one
out="$(FAKE_ISSUE="$KEY2" run_reg)"
first="$(sed -n '1,/^--$/p' "$TMP/curl.log.hdr")"
contains "a restarted node registers with its key" "$first" "X-Node-Key: $KEY"
[ "$(cat "$TMP/state/node.key")" = "$KEY" ] && pass "a registration with the key keeps it" || fail "a registration with the key keeps it"

# the admin reset the key: 403 node_key_invalid drops it, the node registers again and keeps the new key
touch "$TMP/invalid"
out="$(FAKE_ISSUE="$KEY2" FAKE_INVALID_ONCE="$TMP/invalid" FAKE_APPROVED=false REG_TIMEOUT=5 run_reg)"
contains "a refused key is dropped" "$out" "refused this node's key"
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY2" ] && pass "the node registers again and keeps the new key" \
  || fail "the node registers again and keeps the new key"
[ "$(cat "$TMP/state/backend/key")" = "$(printf '%s' "nsu-llm-backend:$KEY2" | sha256sum | cut -d' ' -f1)" ] \
  && pass "the backend key follows the new key" || fail "the backend key follows the new key"
contains "a node that waits says so" "$out" "waits for the admin's approval"

# a malformed key from the portal is not written
rm -rf "$TMP/state"
out="$(FAKE_ISSUE='bad key;rm -rf /' run_reg)"
[ ! -e "$TMP/state/node.key" ] && pass "a malformed key is ignored" || fail "a malformed key is ignored"

# a lost reply: the registration without a key carries a nonce, the same one until a key is kept, then none
rm -rf "$TMP/state"
out="$(FAKE_ISSUE='bad key;rm -rf /' REG_TIMEOUT=8 run_reg)"
nonces="$(grep -o 'X-Node-Enroll-Nonce: [A-Za-z0-9_-]*' "$TMP/curl.log.hdr" | sort -u)"
[ "$(grep -c . <<<"$nonces")" = 1 ] && [ "$(grep -c 'X-Node-Enroll-Nonce' "$TMP/curl.log.hdr")" -ge 2 ]   && pass "a registration whose key was not kept repeats the same nonce" || { fail "a registration whose key was not kept repeats the same nonce"; echo "$nonces"; }
[ -s "$TMP/state/enroll.nonce" ] && pass "the nonce stays until a key is kept" || fail "the nonce stays until a key is kept"
lacks "the nonce is not on a command line" "$(cat "$TMP/curl.log")" "Enroll-Nonce"
out="$(FAKE_ISSUE="$KEY" run_reg)"
[ ! -e "$TMP/state/enroll.nonce" ] && pass "a kept key removes the nonce" || fail "a kept key removes the nonce"
contains "the registration that got the key sent the same nonce" "$(cat "$TMP/curl.log.hdr")" "$(head -1 <<<"$nonces")"

# --- k3s mode: --report enrolls first, the reports carry the key, the keeper stays --------------------------------
rm -rf "$TMP/state"
out="$(FAKE_ISSUE="$KEY" REG_TIMEOUT=3 run_reg --report)"
log="$(cat "$TMP/curl.log")"
contains "k3s mode enrolls" "$log" "https://main.example/api/nodes/enroll"
[ "$(grep -n 'api/nodes/enroll' "$TMP/curl.log" | head -1 | cut -d: -f1)" -lt "$(grep -n '/image' "$TMP/curl.log" | head -1 | cut -d: -f1)" ] \
  && pass "k3s mode enrolls before the image report" || fail "k3s mode enrolls before the image report"
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY" ] && pass "k3s mode keeps the key" || fail "k3s mode keeps the key"
[ -s "$TMP/state/backend/key" ] && pass "k3s mode writes the backend key for the pods" || fail "k3s mode writes the backend key for the pods"
contains "the image report carries the key" "$(awk '/X-Node-Key/' "$TMP/curl.log.hdr")" "X-Node-Key: $KEY"
# the keeper: a key dropped by another process (node-metrics.sh) is replaced
rm -f "$TMP/state/node.key"
: > "$TMP/curl.log"
(FAKE_LOG="$TMP/curl.log" FAKE_ISSUE="$KEY2" PATH="$TMP/bin:$TMP/serve:$BIN:$PATH" CLUSTER_TOKEN=t NODE_NAME=lab-1 \
  MAIN_HOST=main.example STATE_DIR="$TMP/state" WAKE_RETRY_S=1 ENROLL_RETRY_S=1 timeout 4 bash "$BIN/registrar.sh" --report >/dev/null 2>&1)
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY2" ] && pass "a container start without a key registers again" \
  || fail "a container start without a key registers again"
# an older main (no enroll endpoint): the reports go as before and the process ends
rm -rf "$TMP/state"
cat > "$TMP/bin/curl404" <<'EOS'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
for a in "$@"; do case "$a" in
  *://*/ca.crt) printf '404'; exit 0 ;;
  */api/nodes/enroll) printf '{"detail":"Not Found"}\n404'; exit 0 ;;
  */api/nodes/*) printf '{"ok":true}\n200'; exit 0 ;;
esac; done
EOS
mkdir -p "$TMP/old"; cp "$TMP/bin/curl404" "$TMP/old/curl"; chmod +x "$TMP/old/curl"
: > "$TMP/curl.log"
FAKE_LOG="$TMP/curl.log" PATH="$TMP/old:$TMP/serve:$BIN:$PATH" CLUSTER_TOKEN=t NODE_NAME=lab-1 MAIN_HOST=main.example \
  STATE_DIR="$TMP/state" WAKE_RETRY_S=1 ENROLL_RETRY_S=1 timeout 5 bash "$BIN/registrar.sh" --report >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && pass "an older main: the report process ends as before" || fail "an older main: the report process ends as before (rc=$rc)"
contains "an older main still gets the image report" "$(cat "$TMP/curl.log")" "/api/nodes/lab-1/image"

# --- task B1: a node never calls without its key, the keeper never ends while the container runs -----------------
# A scripted portal: $SP/<kind> (enroll, register, heartbeat, wake, image) holds one answer per line, «code body»; each call
# takes the first line while more than one is left (the last one repeats). $SP/calls logs «kind key=<X-Node-Key or ->».
mkdir -p "$TMP/sp"
cat > "$TMP/sp/curl" <<'EOS'
#!/usr/bin/env bash
prev="" hdr="" url=""
for a in "$@"; do
  [ "$prev" = -H ] && case "$a" in @*) hdr="$(cat "${a#@}")" ;; esac
  prev="$a"
  case "$a" in http*://*) url="$a" ;; esac
done
case "$url" in
  */ca.crt) printf '404'; exit 0 ;;
  */api/nodes/enroll) kind=enroll ;;
  */api/nodes/register) kind=register ;;
  */api/nodes/heartbeat) kind=heartbeat ;;
  */api/nodes/wake) kind=wake ;;
  */image) kind=image ;;
  */stop) printf '\n204'; exit 0 ;;
  *) printf '{"ok":true}\n200'; exit 0 ;;
esac
key="$(sed -n 's/^X-Node-Key: //p' <<<"$hdr")"
echo "$kind key=${key:--}" >> "$SP/calls"
f="$SP/$kind"
line="$(head -n1 "$f")"
[ "$(grep -c . "$f")" -gt 1 ] && sed -i 1d "$f"
printf '%s\n%s' "${line#* }" "${line%% *}"
EOS
chmod +x "$TMP/sp/curl"
sp_reset() { rm -rf "$TMP/spd" "$TMP/state"; mkdir -p "$TMP/spd"; : > "$TMP/spd/calls"; }
sp_run() { # timeout [registrar args...]
  local t="$1"; shift
  SP="$TMP/spd" PATH="$TMP/sp:$TMP/serve:$BIN:$PATH" CLUSTER_TOKEN=t NODE_IP=10.0.0.9 NODE_NAME=lab-1 MAIN_HOST=main.example \
    ACCEL=cpu STATE_DIR="$TMP/state" WAKE_RETRY_S=1 ENROLL_RETRY_S=1 WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=24 \
    timeout "$t" bash "$BIN/registrar.sh" "$@" 2>&1
}
OK_KEY="200 {\"ok\":true,\"heartbeat_interval\":1,\"approved\":true,\"node_key\":\"$KEY\"}"
OK_APPROVED='200 {"ok":true,"heartbeat_interval":1,"approved":true}'
HB_OK='200 {"ok":true,"heartbeat_interval":1,"approved":true,"desired":null}'
REQUIRED='403 {"detail":{"code":"node_key_required","message":"no key"}}'
PENDING='403 {"detail":{"code":"node_pending","message":"waits"}}'

# the live case (2026-11-05): the enrolment answered 200 but issued no key -> no report goes without a key, the keeper stays
sp_reset
echo "$OK_APPROVED" > "$TMP/spd/enroll"; echo "$REQUIRED" > "$TMP/spd/wake"; echo "$REQUIRED" > "$TMP/spd/image"
out="$(sp_run 9 --report)"; rc=$?
[ "$rc" = 124 ] && pass "k3s: an enrolment without a key does not end the keeper" || fail "k3s: an enrolment without a key does not end the keeper (rc=$rc)"
contains "k3s: the log says the portal issued no key" "$out" "without issuing a key"
[ "$(grep -c '^enroll' "$TMP/spd/calls")" -ge 2 ] && pass "k3s: the enrolment is repeated" || fail "k3s: the enrolment is repeated: $(cat "$TMP/spd/calls")"
lacks "k3s: no NIC report without a key" "$(cat "$TMP/spd/calls")" "wake key=-"
lacks "k3s: no image report without a key" "$(cat "$TMP/spd/calls")" "image key=-"

# the portal does not answer at first: the reports wait for the key, then carry it
sp_reset
printf '%s\n%s\n%s\n%s\n' '503 {"detail":"down"}' '502 bad gateway' "$OK_KEY" "$OK_APPROVED" > "$TMP/spd/enroll"
echo "$OK_APPROVED" > "$TMP/spd/wake"; echo "$OK_APPROVED" > "$TMP/spd/image"
out="$(sp_run 25 --report)"
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY" ] && pass "k3s: the key is kept once the portal answers" || fail "k3s: the key is kept once the portal answers: $out"
calls="$(cat "$TMP/spd/calls")"
first_report="$(grep -n -m1 -E '^(wake|image)' <<<"$calls" | cut -d: -f1)"
last_nokey_enroll="$(grep -n '^enroll key=-' <<<"$calls" | tail -1 | cut -d: -f1)"
[ -n "$first_report" ] && [ "$first_report" -gt "${last_nokey_enroll:-0}" ] && pass "k3s: the reports go only after the key" \
  || fail "k3s: the reports go only after the key: $calls"
contains "k3s: the NIC report carries the key" "$calls" "wake key=$KEY"
contains "k3s: the image report carries the key" "$calls" "image key=$KEY"
contains "k3s: the keeper enrols again with the key (the portal confirms it)" "$calls" "enroll key=$KEY"
[ "$(grep -c '^wake' <<<"$calls")" = 1 ] && pass "k3s: the NIC is reported once" || fail "k3s: the NIC is reported once: $calls"

# a claim: the reports are refused (node_pending) until the admin approves it, then they go (with the same key)
sp_reset
printf '%s\n%s\n%s\n%s\n' "200 {\"ok\":true,\"approved\":false,\"node_key\":\"$KEY\"}" '200 {"ok":true,"approved":false}' \
  '200 {"ok":true,"approved":false}' "$OK_APPROVED" > "$TMP/spd/enroll"
printf '%s\n%s\n%s\n' "$PENDING" "$PENDING" "$OK_APPROVED" > "$TMP/spd/wake"
printf '%s\n%s\n%s\n' "$PENDING" "$PENDING" "$OK_APPROVED" > "$TMP/spd/image"
out="$(sp_run 8 --report)"
contains "k3s: a waiting node says so" "$out" "waits for the admin's approval"
contains "k3s: after the approval the NIC is reported" "$out" "NIC 00-1A-2B-3C-4D-5E (10.1.2.3/24) reported to the portal"
contains "k3s: after the approval the image is reported" "$out" "(version dev, digest unknown) reported to the portal"
[ "$(grep -c 'NIC report not taken yet' <<<"$out")" = 1 ] && pass "k3s: a refused report is logged once, not every round" \
  || fail "k3s: a refused report is logged once: $out"

# the admin deleted the node (or reset its key): the keeper's next enrolment gets 403 node_key_invalid, drops the key, gets a new one
sp_reset
printf '%s\n%s\n%s\n%s\n' "$OK_KEY" '403 {"detail":{"code":"node_key_invalid","message":"x"}}' \
  "200 {\"ok\":true,\"approved\":false,\"node_key\":\"$KEY2\"}" '200 {"ok":true,"approved":false}' > "$TMP/spd/enroll"
echo "$OK_APPROVED" > "$TMP/spd/wake"; echo "$OK_APPROVED" > "$TMP/spd/image"
out="$(sp_run 6 --report)"
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY2" ] && pass "k3s: a key the portal refuses is replaced by the keeper" \
  || fail "k3s: a key the portal refuses is replaced by the keeper: $(cat "$TMP/spd/calls")"
# the deletion took the NIC and the image from the portal: the new registration sends them again, once
calls="$(cat "$TMP/spd/calls")"
contains "k3s: the NIC goes again with the new key" "$calls" "wake key=$KEY2"
contains "k3s: the image goes again with the new key" "$calls" "image key=$KEY2"
[ "$(grep -c "^wake key=$KEY2" <<<"$calls")" = 1 ] && pass "k3s: the new key reports the NIC once" || fail "k3s: the new key reports the NIC once: $calls"

# standalone: a registration answered without a key is not «registered»; no heartbeat goes without a key
sp_reset
printf '%s\n%s\n' "$OK_APPROVED" "$OK_KEY" > "$TMP/spd/register"
echo "$HB_OK" > "$TMP/spd/heartbeat"; echo "$OK_APPROVED" > "$TMP/spd/image"
out="$(sp_run 12)"
contains "standalone: a registration without a key is a failure" "$out" "without issuing a key"
[ "$(grep -c 'registered as lab-1' <<<"$out")" = 1 ] && pass "standalone: «registered» only once, with the key" || fail "standalone: «registered» only with the key: $out"
lacks "standalone: no heartbeat without a key" "$(cat "$TMP/spd/calls")" "heartbeat key=-"
# an older portal (no identities: no `approved` in the reply, no key) is taken as before; a heartbeat that then wants a key
# the node lacks (403 node_key_required) registers again
sp_reset
printf '%s\n%s\n' '200 {"ok":true,"heartbeat_interval":1}' "$OK_KEY" > "$TMP/spd/register"
printf '%s\n%s\n' "$REQUIRED" "$HB_OK" > "$TMP/spd/heartbeat"
echo "$OK_APPROVED" > "$TMP/spd/image"
out="$(sp_run 12)"
contains "standalone: 403 node_key_required without a key registers again" "$out" "registering again"
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY" ] && pass "standalone: the new registration keeps its key" || fail "standalone: the new registration keeps its key: $(cat "$TMP/spd/calls")"

# --- node-metrics.sh ------------------------------------------------------------------------------------------------
mkdir -p "$TMP/mstate"
printf '%s\n' "$KEY" > "$TMP/mstate/node.key"
mkdir -p "$TMP/mstate/backend"; echo x > "$TMP/mstate/backend/key"
: > "$TMP/curl.log"; : > "$TMP/curl.log.hdr"
FAKE_LOG="$TMP/curl.log" PATH="$TMP/bin:$PATH" STATE_DIR="$TMP/mstate" MAIN_URL=http://main.example CLUSTER_TOKEN=t \
  NODE_NAME=lab-1 bash -c "source '$BIN/node-metrics.sh'; push '{}'" >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && pass "node-metrics push is taken (204)" || fail "node-metrics push is taken (204), rc=$rc"
contains "node-metrics sends the node's key" "$(cat "$TMP/curl.log.hdr")" "X-Node-Key: $KEY"
lacks "node-metrics key not on the command line" "$(cat "$TMP/curl.log")" "$KEY"
FAKE_METRICS_INVALID=1 FAKE_LOG="$TMP/curl.log" PATH="$TMP/bin:$PATH" STATE_DIR="$TMP/mstate" MAIN_URL=http://main.example \
  CLUSTER_TOKEN=t NODE_NAME=lab-1 bash -c "source '$BIN/node-metrics.sh'; push '{}'" >/dev/null 2>&1
[ ! -e "$TMP/mstate/node.key" ] && [ ! -e "$TMP/mstate/backend/key" ] && pass "node-metrics drops a key the portal refuses" \
  || fail "node-metrics drops a key the portal refuses"

# the race that left the node without a key (task B1): the refusal of the OLD key arrives after the registrar kept a new one
mkdir -p "$TMP/race"
cat > "$TMP/race/curl" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$NEW_KEY" > "$KEY_PATH"
printf '{"detail":{"code":"node_key_invalid","message":"x"}}\n403'
EOS
chmod +x "$TMP/race/curl"
printf '%s\n' "$KEY" > "$TMP/mstate/node.key"
NEW_KEY="$KEY2" KEY_PATH="$TMP/mstate/node.key" PATH="$TMP/race:$PATH" STATE_DIR="$TMP/mstate" MAIN_URL=http://main.example \
  CLUSTER_TOKEN=t NODE_NAME=lab-1 bash -c "source '$BIN/node-metrics.sh'; push '{}'" >/dev/null 2>&1
[ "$(cat "$TMP/mstate/node.key" 2>/dev/null)" = "$KEY2" ] && pass "node-metrics keeps a key the registrar kept while the call was on its way" \
  || fail "node-metrics keeps a key the registrar kept while the call was on its way"
rm -rf "$TMP/state"; mkdir -p "$TMP/state"; printf '%s\n' "$KEY" > "$TMP/state/node.key"
out="$(NEW_KEY="$KEY2" KEY_PATH="$TMP/state/node.key" PATH="$TMP/race:$TMP/serve:$BIN:$PATH" CLUSTER_TOKEN=t MAIN_URL=http://main.example \
  NODE_NAME=lab-1 STATE_DIR="$TMP/state" MODELS_REPORT_S=300 WAKE_RETRY_S=1 timeout 3 bash "$BIN/registrar.sh" --models-report 2>&1)"
[ "$(cat "$TMP/state/node.key" 2>/dev/null)" = "$KEY2" ] && pass "the models report keeps a key kept while the call was on its way" \
  || { fail "the models report keeps a key kept while the call was on its way"; echo "$out"; }

# --- serve-llama.sh: the backend key file of the k3s pods ----------------------------------------------------------
mkdir -p "$TMP/llama"
cat > "$TMP/llama/llama-server" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$LLAMA_ARGS_OUT"
printf 'LLAMA_API_KEY=%s\nLLAMA_API_KEY_FILE=%s\n' "${LLAMA_API_KEY-unset}" "${LLAMA_API_KEY_FILE-unset}" > "$LLAMA_ENV_OUT"
EOS
chmod +x "$TMP/llama/llama-server"
printf '%s\n' "$BACKEND" > "$TMP/bkey"
LLAMA_CACHE="$TMP/models" LLAMA_ARGS_OUT="$TMP/largs" LLAMA_ENV_OUT="$TMP/lenv" PATH="$TMP/llama:$PATH" MODEL_REPO=org/repo \
  MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu LLAMA_API_KEY_FILE="$TMP/bkey" bash "$BIN/serve-llama.sh" >/dev/null 2>&1
grep -qx "LLAMA_API_KEY=$BACKEND" "$TMP/lenv" && pass "serve reads the node's backend key from its file" \
  || { fail "serve reads the node's backend key from its file"; cat "$TMP/lenv"; }
grep -qx "LLAMA_API_KEY_FILE=unset" "$TMP/lenv" && pass "serve does not hand the file name on" || fail "serve does not hand the file name on"
! grep -q "$BACKEND" "$TMP/largs" && pass "the backend key is not in llama-server's argv" || fail "the backend key is not in llama-server's argv"
out="$(LLAMA_CACHE="$TMP/models" LLAMA_ARGS_OUT="$TMP/largs" LLAMA_ENV_OUT="$TMP/lenv" PATH="$TMP/llama:$PATH" MODEL_REPO=org/repo \
  MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu LLAMA_API_KEY_FILE="$TMP/missing" BACKEND_KEY_WAIT_S=2 bash "$BIN/serve-llama.sh" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && pass "serve without the key file stops after the wait" || fail "serve without the key file stops after the wait"
contains "serve says why" "$out" "has not registered with the portal"

# --- main's CA through fetch-main-ca.sh (task SEC-2) when the image has it ---------------------------------------------
mkdir -p "$TMP/fca"
cat > "$TMP/fca/fetch-main-ca.sh" <<'EOS'
#!/usr/bin/env bash
echo "fetch-main-ca $* token=${CLUSTER_TOKEN:+set}" >> "$FAKE_FCA_LOG"
out=""; prev=""
for a in "$@"; do [ "$prev" = --out ] && out="$a"; prev="$a"; done
[ "${FAKE_FCA_RC:-0}" = 0 ] && echo CERT > "$out"
exit "${FAKE_FCA_RC:-0}"
EOS
chmod +x "$TMP/fca/fetch-main-ca.sh"
rm -rf "$TMP/state"; : > "$TMP/fca.log"; : > "$TMP/curl.log"
FAKE_FCA_LOG="$TMP/fca.log" FAKE_LOG="$TMP/curl.log" FAKE_ISSUE="$KEY" PATH="$TMP/fca:$TMP/bin:$TMP/serve:$BIN:$PATH"   CLUSTER_TOKEN=t NODE_IP=10.0.0.9 NODE_NAME=lab-1 MAIN_HOST=main.example ACCEL=cpu STATE_DIR="$TMP/state" timeout 3   bash "$BIN/registrar.sh" >/dev/null 2>&1
contains "registrar asks fetch-main-ca.sh for main's CA" "$(cat "$TMP/fca.log")" "--main main.example --out $TMP/state/ca.crt token=set"
lacks "registrar never asks for trust on first use" "$(cat "$TMP/fca.log")" "-yes"
lacks "registrar never asks for trust on first use (flag)" "$(cat "$TMP/fca.log")" "trust-first-use"
lacks "registrar does not fetch the CA with curl -k then" "$(cat "$TMP/curl.log")" "https://main.example/ca.crt"
contains "registrar verifies against the CA it got" "$(grep 'api/nodes/register' "$TMP/curl.log")" "--cacert $TMP/state/ca.crt"
rm -rf "$TMP/state"; : > "$TMP/curl.log"
out="$(FAKE_FCA_RC=4 FAKE_FCA_LOG="$TMP/fca.log" FAKE_LOG="$TMP/curl.log" PATH="$TMP/fca:$TMP/bin:$TMP/serve:$BIN:$PATH"   CLUSTER_TOKEN=t NODE_IP=10.0.0.9 NODE_NAME=lab-1 MAIN_HOST=main.example ACCEL=cpu STATE_DIR="$TMP/state" timeout 3   bash "$BIN/registrar.sh" 2>&1)"
contains "exit 4 of fetch-main-ca.sh: the system CA store" "$out" "system CA store"
lacks "exit 4: no --cacert" "$(grep 'api/nodes/register' "$TMP/curl.log")" "--cacert"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
