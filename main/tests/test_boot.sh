#!/usr/bin/env bash
# Boot test of the main image on a real Docker host (Docker Desktop / Linux).
# Starts one container (it-14-main), waits for k3s + postgres + litellm + portal, checks Caddy
# (TLS, /ca.crt, streaming), restarts the container, removes the container and its volumes.
# Run through test-lock: bash <test-lock> -- bash main/tests/test_boot.sh
set -uo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${IMAGE:-nsu-llm-main:0.1.0}"
NAME="it-14-main"
VOL_K3S="it-14-main-k3s"; VOL_KUBELET="it-14-main-kubelet"; VOL_DATA="it-14-main-data"
BACKEND="it-14-fakebackend"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-600}"
TMP="$(mktemp -d)"
FAILED=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "       $2"; FAILED=1; }

remove_all() {
  docker rm -f "$NAME" "$BACKEND" >/dev/null 2>&1
  docker volume rm -f "$VOL_K3S" "$VOL_KUBELET" "$VOL_DATA" >/dev/null 2>&1
}
cleanup() { remove_all; rm -rf "$TMP"; }
trap cleanup EXIT

lan_ip() {
  if [ -n "${MAIN_IP:-}" ]; then echo "$MAIN_IP"; return; fi
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1
  else
    # Windows: the same detection as run-main.ps1 (physical adapter with a gateway, not the VPN adapter)
    : >"$TMP/detect.env"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$ROOT/main/run-main.ps1")" \
      -DryRun -EnvFile "$(cygpath -w "$TMP/detect.env")" | tr -d '\r' | grep -o 'MAIN_ADVERTISE_IP=[0-9.]*' | cut -d= -f2
  fi
}
IP="$(lan_ip)"
[ -n "$IP" ] || { echo "cannot detect the LAN IP; set MAIN_IP"; exit 2; }

# wait_readyz SECONDS: poll the portal through Caddy. 127.0.0.1 instead of localhost avoids the
# IPv6 fallback delay; the Host header selects the Caddy site.
wait_readyz() {
  local deadline=$((SECONDS + $1)) body=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    body="$(curl -sk -m 5 -H "Host: $IP" -w '\n%{http_code}' https://127.0.0.1/readyz 2>/dev/null)"
    if [ "$(tail -n1 <<<"$body")" = 200 ]; then head -n1 <<<"$body"; return 0; fi
    sleep 5
  done
  echo "timeout; last: $body"; return 1
}
kc() { docker exec "$NAME" k3s kubectl "$@"; }

remove_all
if [ "${SKIP_BUILD:-0}" != 1 ]; then
  echo "== build $IMAGE"
  (cd "$ROOT" && docker build --provenance=false -f main/Dockerfile -t "$IMAGE" .) >"$TMP/build.log" 2>&1 \
    || { tail -30 "$TMP/build.log"; echo "build failed"; exit 1; }
fi

ENV_FILE_HOST="$TMP/test.env"; command -v cygpath >/dev/null 2>&1 && ENV_FILE_HOST="$(cygpath -m "$TMP/test.env")"
cat >"$TMP/test.env" <<ENV
OIDC_ISSUER=http://host.docker.internal:9999
OIDC_CLIENT_ID=boot-test
OIDC_CLIENT_SECRET=boot-test-secret
PUBLIC_HOSTNAME=llm.boot.test
ADMIN_EMAILS=admin@example.test
ENV

echo "== start $NAME (advertise $IP)"
docker run -d --name "$NAME" --hostname llm-main --privileged \
  --tmpfs /run --tmpfs /var/run \
  -p 443:443 -p 80:80 -p 6443:6443 \
  -v "$VOL_K3S:/var/lib/rancher/k3s" -v "$VOL_KUBELET:/var/lib/kubelet" -v "$VOL_DATA:/var/lib/llm" \
  --env-file "$ENV_FILE_HOST" -e "MAIN_ADVERTISE_IP=$IP" "$IMAGE" >/dev/null || { echo "docker run failed"; exit 1; }

start=$SECONDS
body="$(wait_readyz "$BOOT_TIMEOUT")"; rc=$?
if [ "$rc" = 0 ] && [ "$body" = '{"db":true,"litellm":true}' ]; then
  pass "readyz 200 {\"db\":true,\"litellm\":true} after $((SECONDS - start)) s"
else
  fail "readyz within ${BOOT_TIMEOUT}s" "$body"
  docker logs --tail 60 "$NAME" 2>&1 | tail -60
  kc get pods -A 2>&1 | tail -20
  exit 1
fi

ca="$(curl -s -m 10 http://127.0.0.1/ca.crt)"
grep -q 'BEGIN CERTIFICATE' <<<"$ca" && pass "http /ca.crt serves a PEM" || fail "http /ca.crt serves a PEM" "$ca"
loc="$(curl -s -m 10 -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1/anything)"
grep -q '^301 https://' <<<"$loc" && pass "http redirects to https ($loc)" || fail "http redirects to https" "$loc"
cai="$(curl -sk -m 10 -H "Host: $IP" https://127.0.0.1/ca.crt)"
grep -q 'BEGIN CERTIFICATE' <<<"$cai" && pass "https /ca.crt serves a PEM" || fail "https /ca.crt serves a PEM" "$cai"
printf '%s\n' "$ca" >"$TMP/ca.pem"
CA_HOST="$TMP/ca.pem"; command -v cygpath >/dev/null 2>&1 && CA_HOST="$(cygpath -m "$TMP/ca.pem")"
# --ssl-no-revoke: schannel (curl on Windows) cannot reach a revocation server for a private CA
verify="$(curl -sS --ssl-no-revoke -m 10 --cacert "$CA_HOST" --resolve "$IP:443:127.0.0.1" "https://$IP/healthz" 2>&1)"
grep -q '"status"' <<<"$verify" && pass "https certificate verifies against /ca.crt" || fail "https certificate verifies against /ca.crt" "$verify"
curl -sk -m 10 -H "Host: $IP" https://127.0.0.1/ | grep -qi '<html' \
  && pass "SPA index served through Caddy" || fail "SPA index served through Caddy"

nodes="$(kc get nodes --no-headers 2>&1)"
grep -Eq '^llm-main +Ready' <<<"$nodes" && pass "node llm-main Ready" || fail "node llm-main Ready" "$nodes"
[ "$(grep -c . <<<"$nodes")" = 1 ] && pass "no other nodes joined" || fail "no other nodes joined" "$nodes"

sys="$(kc -n kube-system get pods --no-headers 2>&1)"
grep -Eq '^coredns-.* Running' <<<"$sys" && grep -Eq '^local-path-provisioner-.* Running' <<<"$sys" \
  && pass "coredns and local-path-provisioner Running without an agent" || fail "coredns and local-path-provisioner Running" "$sys"
for _ in $(seq 1 60); do  # litellm may still be starting its first worker
  llm="$(kc -n llm get pods --no-headers 2>&1)"
  grep -Eq '^litellm-.* 1/1 +Running' <<<"$llm" && break
  sleep 5
done
grep -Eq '^postgres-0 .*Running' <<<"$llm" && grep -Eq '^litellm-.* 1/1 +Running' <<<"$llm" \
  && pass "postgres and litellm Running" || fail "postgres and litellm Running" "$llm"
grep -Eq '^redis-.* 1/1 +Running' <<<"$llm" && pass "redis Running" || fail "redis Running" "$llm"
procs="$(docker exec "$NAME" ps -eo args 2>&1)"
[ "$(grep -c '[a]pp.controllers' <<<"$procs")" = 1 ] && grep -q '[u]vicorn app.main:create_app .*--workers 4' <<<"$procs" \
  && pass "portal API with 4 workers and one controllers process" || fail "portal API workers and controllers process" "$procs"

# k3s must be PID 1 (cgroup v2 evacuation only runs there)
pid1="$(docker exec "$NAME" cat /proc/1/comm)"
[[ "$pid1" == k3s* ]] && pass "k3s is PID 1" || fail "k3s is PID 1" "$pid1"
# The advertise IP is NOT made local inside main: a node on the same host advertising it must stay
# reachable from LiteLLM/portal (hostNetwork) through the host, i.e. IP:18080 leaves the container.
r_inf="$(docker exec "$NAME" ip route get "$IP" ipproto tcp dport 18080 2>&1)"
if ! grep -q 'dev lo' <<<"$r_inf" && ! docker exec "$NAME" ip -4 -o addr show | grep -q " $IP/"; then
  pass "advertise IP $IP is not local inside main"
else
  fail "advertise IP $IP is not local inside main" "$r_inf"
fi
# pods reach the apiserver at the address k3s publishes for them (the container IP)
ep="$(kc -n default get endpointslices -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[0].endpoints[0].addresses[0]}' 2>/dev/null)"
cip="$(docker exec "$NAME" ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
if [ -n "$ep" ] && [ "$ep" = "$cip" ]; then pass "apiserver endpoint for pods is the container IP ($ep)"
else fail "apiserver endpoint for pods is the container IP" "endpoint=$ep container=$cip"; fi
tls="$(docker exec "$NAME" sh -c "echo | openssl s_client -connect 127.0.0.1:6443 2>/dev/null | openssl x509 -noout -ext subjectAltName" 2>&1)"
grep -q "$IP" <<<"$tls" && pass "apiserver certificate has the LAN IP in its SAN" || fail "apiserver certificate has the LAN IP in its SAN" "$tls"

SECRETS_SUM="$(docker exec "$NAME" sha256sum /var/lib/llm/secrets.env | cut -d' ' -f1)"

# Streaming through Caddy. The fake OpenAI backend runs in a sibling container on the default
# bridge (reachable from llm-main without host firewall rules).
echo "== streaming through Caddy"
docker run -d --name "$BACKEND" -v "$ROOT/portal/tests/fakes:/fakes:ro" python:3.12-slim sh -c \
  'pip install -q starlette uvicorn >/dev/null 2>&1 && exec python /fakes/openai_backend.py --host 0.0.0.0 --port 18099 --model-id boot-fake' >/dev/null
BIP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$BACKEND")"
for _ in $(seq 1 60); do
  docker exec "$NAME" curl -sf -m 3 "http://$BIP:18099/health" >/dev/null 2>&1 && break
  sleep 2
done
MK="$(docker exec "$NAME" sh -c '. /var/lib/llm/secrets.env; echo $LITELLM_MASTER_KEY')"
docker exec "$NAME" curl -s -m 20 -X POST http://127.0.0.1:4000/model/new \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  -d "{\"model_name\":\"boot-fake\",\"litellm_params\":{\"model\":\"openai/boot-fake\",\"api_base\":\"http://$BIP:18099/v1\",\"api_key\":\"none\"},\"model_info\":{\"id\":\"it14-fake\"}}" \
  >"$TMP/model.json"
KEY="$(docker exec "$NAME" curl -s -m 20 -X POST http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' -d '{"models":[]}' | sed -n 's/.*"key":"\([^"]*\)".*/\1/p')"
if [ -z "$KEY" ]; then
  fail "LiteLLM key generated" "$(cat "$TMP/model.json")"
else
  curl -sk -N -m 60 -H "Host: $IP" https://127.0.0.1/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model":"boot-fake","stream":true,"messages":[{"role":"user","content":"hi"}]}' >"$TMP/stream.txt"
  n="$(grep -c '^data: {' "$TMP/stream.txt")"
  if [ "$n" -ge 2 ] && grep -q '^data: \[DONE\]' "$TMP/stream.txt"; then
    pass "stream through Caddy: $n data chunks, then [DONE]"
  else
    fail "stream through Caddy" "$(head -c 600 "$TMP/stream.txt")"
  fi
  models="$(curl -sk -m 30 -H "Host: $IP" https://127.0.0.1/v1/models -H "Authorization: Bearer $KEY")"
  grep -q boot-fake <<<"$models" && pass "/v1/models through Caddy lists the model" || fail "/v1/models through Caddy" "$models"
fi

echo "== docker restart"
docker restart "$NAME" >/dev/null
start=$SECONDS
body="$(wait_readyz "$BOOT_TIMEOUT")"; rc=$?
if [ "$rc" = 0 ]; then pass "readyz 200 again after restart ($((SECONDS - start)) s)"; else fail "readyz after restart" "$body"; docker logs --tail 40 "$NAME" 2>&1 | tail -40; fi
[ "$(docker exec "$NAME" sha256sum /var/lib/llm/secrets.env | cut -d' ' -f1)" = "$SECRETS_SUM" ] \
  && pass "secrets.env unchanged after restart" || fail "secrets.env unchanged after restart"
[ "$(curl -s -m 10 http://127.0.0.1/ca.crt)" = "$ca" ] && pass "internal CA unchanged after restart" || fail "internal CA unchanged after restart"

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
