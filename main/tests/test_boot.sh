#!/usr/bin/env bash
# Boot test of the main image on a real Docker host (Docker Desktop / Linux).
# Starts one container (it-14-main), waits for k3s + postgres + litellm + portal, checks Caddy
# (TLS, /ca.crt, streaming), restarts the container, removes the container and its volumes.
# Run through test-lock: bash <test-lock> -- bash main/tests/test_boot.sh
set -uo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${IMAGE:-nsu-llm-main:0.2.0}"
NAME="nsu-llm-it14-main"
VOL_K3S="nsu-llm-it14-main-k3s"; VOL_KUBELET="nsu-llm-it14-main-kubelet"; VOL_DATA="nsu-llm-it14-main-data"
BACKEND="nsu-llm-it14-fakebackend"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-600}"
# host ports (a build server whose 80/443 are taken: HTTPS_PORT=18443 HTTP_PORT=18081 K3S_PORT=16443)
HTTPS_PORT="${HTTPS_PORT:-443}"; HTTP_PORT="${HTTP_PORT:-80}"; K3S_PORT="${K3S_PORT:-6443}"
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
    body="$(curl -sk -m 5 -H "Host: $IP" -w '\n%{http_code}' https://127.0.0.1:$HTTPS_PORT/readyz 2>/dev/null)"
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
  -p "$HTTPS_PORT:443" -p "$HTTP_PORT:80" -p "$K3S_PORT:6443" \
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

ca="$(curl -s -m 10 http://127.0.0.1:$HTTP_PORT/ca.crt)"
grep -q 'BEGIN CERTIFICATE' <<<"$ca" && pass "http /ca.crt serves a PEM" || fail "http /ca.crt serves a PEM" "$ca"
loc="$(curl -s -m 10 -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:$HTTP_PORT/anything)"
grep -q '^301 https://' <<<"$loc" && pass "http redirects to https ($loc)" || fail "http redirects to https" "$loc"
cai="$(curl -sk -m 10 -H "Host: $IP" https://127.0.0.1:$HTTPS_PORT/ca.crt)"
grep -q 'BEGIN CERTIFICATE' <<<"$cai" && pass "https /ca.crt serves a PEM" || fail "https /ca.crt serves a PEM" "$cai"
printf '%s\n' "$ca" >"$TMP/ca.pem"
CA_HOST="$TMP/ca.pem"; command -v cygpath >/dev/null 2>&1 && CA_HOST="$(cygpath -m "$TMP/ca.pem")"
# --ssl-no-revoke: schannel (curl on Windows) cannot reach a revocation server for a private CA
verify="$(curl -sS --ssl-no-revoke -m 10 --cacert "$CA_HOST" --resolve "$IP:$HTTPS_PORT:127.0.0.1" "https://$IP:$HTTPS_PORT/healthz" 2>&1)"
grep -q '"status"' <<<"$verify" && pass "https certificate verifies against /ca.crt" || fail "https certificate verifies against /ca.crt" "$verify"
curl -sk -m 10 -H "Host: $IP" https://127.0.0.1:$HTTPS_PORT/ | grep -qi '<html' \
  && pass "SPA index served through Caddy" || fail "SPA index served through Caddy"

nodes="$(kc get nodes --no-headers 2>&1)"
grep -Eq '^llm-main +Ready' <<<"$nodes" && pass "node llm-main Ready" || fail "node llm-main Ready" "$nodes"
[ "$(grep -c . <<<"$nodes")" = 1 ] && pass "no other nodes joined" || fail "no other nodes joined" "$nodes"

# SEC-2 H1: main carries its own taint (only main's pods and k3s system pods tolerate it)
taints="$(kc get node llm-main -o jsonpath='{range .spec.taints[*]}{.key}={.value}:{.effect}{"\n"}{end}' 2>&1)"
grep -qx 'node-role.kubernetes.io/control-plane=true:NoSchedule' <<<"$taints" \
  && pass "llm-main carries the main-only taint" || fail "llm-main carries the main-only taint" "$taints"
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

# ---- SEC-2 on a real k3s
# H1: every pod of main runs on llm-main
wrong="$(kc -n llm get pods -o jsonpath='{range .items[*]}{.metadata.name} {.spec.nodeName}{"\n"}{end}' 2>&1 | awk '$2 != "llm-main"')"
[ -z "$wrong" ] && pass "every pod in llm runs on llm-main" || fail "every pod in llm runs on llm-main" "$wrong"
# M2 Redis: no access without the password, the portal's REDIS_URL authenticates
r_noauth="$(docker exec "$NAME" sh -c 'k3s kubectl -n llm exec deploy/redis -- sh -c "REDISCLI_AUTH= redis-cli -h 127.0.0.1 ping"' 2>&1)"
grep -q NOAUTH <<<"$r_noauth" && pass "redis refuses a client without the password" || fail "redis refuses a client without the password" "$r_noauth"
r_portal="$(docker exec "$NAME" sh -c '. /run/llm/portal.env; /opt/llm/portal/.venv/bin/python -c "import asyncio,redis.asyncio as r; print(asyncio.run(r.from_url(\"$REDIS_URL\").ping()))"' 2>&1)"
[ "$r_portal" = True ] && pass "the portal's REDIS_URL authenticates" || fail "the portal's REDIS_URL authenticates" "$r_portal"
# M2 registry: reads through Caddy without credentials, no write without the password, registry-push.sh writes
for _ in $(seq 1 30); do grep -Eq '^registry-.* 1/1 +Running' <<<"$(kc -n llm get pods --no-headers 2>&1)" && break; sleep 5; done
# a single-manifest image like the node images (the k3s pause image is a multi-arch index: ctr --platform pushes one arch of it)
tar cf - --files-from /dev/null | docker import - nsu-llm-it14-tiny:1 >/dev/null
docker save nsu-llm-it14-tiny:1 | docker exec -i "$NAME" k3s ctr -n k8s.io images import --no-unpack - >/dev/null
docker rmi nsu-llm-it14-tiny:1 >/dev/null 2>&1
tiny=docker.io/library/nsu-llm-it14-tiny:1
reg_out="$(docker exec "$NAME" sh -c "k3s ctr -n k8s.io images tag --force '$tiny' 127.0.0.1:5000/nsu-llm-node:boot-sec2 >/dev/null &&
  k3s ctr -n k8s.io images tag --force '$tiny' 127.0.0.1:5001/nsu-llm-node:boot-anon >/dev/null &&
  (k3s ctr -n k8s.io images push --plain-http 127.0.0.1:5001/nsu-llm-node:boot-anon >/dev/null 2>&1 && echo ANON_PUSHED || echo ANON_REFUSED);
  (k3s ctr -n k8s.io images push --plain-http 127.0.0.1:5000/nsu-llm-node:boot-anon >/dev/null 2>&1 && echo READ_LISTENER_PUSHED || echo READ_LISTENER_REFUSED);
  /opt/llm/main/registry-push.sh 127.0.0.1:5000/nsu-llm-node:boot-sec2 >/dev/null && echo HELPER_PUSHED;
  curl -fsS -m 10 http://127.0.0.1:5000/v2/nsu-llm-node/tags/list" 2>&1)"
grep -q ANON_REFUSED <<<"$reg_out" && grep -q READ_LISTENER_REFUSED <<<"$reg_out" && grep -q HELPER_PUSHED <<<"$reg_out" \
  && grep -q '"boot-sec2"' <<<"$reg_out" && ! grep -q '"boot-anon"' <<<"$reg_out" \
  && pass "registry: no push without the password, registry-push.sh pushes, 127.0.0.1:5000 reads" || fail "registry auth on k3s" "$reg_out"
tags="$(curl -sk -m 10 -H "Host: $IP" "https://127.0.0.1:$HTTPS_PORT/v2/nsu-llm-node/tags/list")"
grep -q '"boot-sec2"' <<<"$tags" && pass "registry readable through https /v2/ without credentials" || fail "registry through /v2/" "$tags"
# M1: /ca.crt.p7s signed by the k3s CA; the node-side check accepts the genuine main with the join token
for _ in $(seq 1 30); do [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/ca.crt.p7s")" = 200 ] && break; sleep 3; done
join="$(docker exec "$NAME" /opt/llm/main/agent-token.sh 2>/dev/null)"
fetch="$(K3S_TOKEN="$join" bash "$ROOT/node/rootfs/opt/llm/bin/fetch-main-ca.sh" --main "$IP:$HTTPS_PORT" --k3s-port "$K3S_PORT" --out "$TMP/fetched-ca.crt" 2>&1)"; frc=$?
[ "$frc" = 0 ] && [ "$(openssl x509 -noout -fingerprint -sha256 -in "$TMP/fetched-ca.crt")" = "$(openssl x509 -noout -fingerprint -sha256 -in "$TMP/ca.pem")" ] && pass "fetch-main-ca.sh verifies the real /ca.crt by the K10 token (CA hash) and its k3s-CA signature" \
  || fail "fetch-main-ca.sh against the real main" "rc=$frc $fetch"
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
  curl -sk -N -m 60 -H "Host: $IP" https://127.0.0.1:$HTTPS_PORT/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model":"boot-fake","stream":true,"messages":[{"role":"user","content":"hi"}]}' >"$TMP/stream.txt"
  n="$(grep -c '^data: {' "$TMP/stream.txt")"
  if [ "$n" -ge 2 ] && grep -q '^data: \[DONE\]' "$TMP/stream.txt"; then
    pass "stream through Caddy: $n data chunks, then [DONE]"
  else
    fail "stream through Caddy" "$(head -c 600 "$TMP/stream.txt")"
  fi
  models="$(curl -sk -m 30 -H "Host: $IP" https://127.0.0.1:$HTTPS_PORT/v1/models -H "Authorization: Bearer $KEY")"
  grep -q boot-fake <<<"$models" && pass "/v1/models through Caddy lists the model" || fail "/v1/models through Caddy" "$models"
fi

echo "== docker restart"
docker restart "$NAME" >/dev/null
start=$SECONDS
body="$(wait_readyz "$BOOT_TIMEOUT")"; rc=$?
if [ "$rc" = 0 ]; then pass "readyz 200 again after restart ($((SECONDS - start)) s)"; else fail "readyz after restart" "$body"; docker logs --tail 40 "$NAME" 2>&1 | tail -40; fi
[ "$(docker exec "$NAME" sha256sum /var/lib/llm/secrets.env | cut -d' ' -f1)" = "$SECRETS_SUM" ] \
  && pass "secrets.env unchanged after restart" || fail "secrets.env unchanged after restart"
[ "$(curl -s -m 10 http://127.0.0.1:$HTTP_PORT/ca.crt)" = "$ca" ] && pass "internal CA unchanged after restart" || fail "internal CA unchanged after restart"

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
