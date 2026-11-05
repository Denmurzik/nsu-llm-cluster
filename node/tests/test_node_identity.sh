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
contains "registrar asks fetch-main-ca.sh for main's CA" "$(cat "$TMP/fca.log")" "--main https://main.example --out $TMP/state/ca.crt token=set"
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
