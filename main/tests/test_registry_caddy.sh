#!/usr/bin/env bash
# Functional test of the class registry (task 2-25, SEC-2 M2) with docker: the real registry image pinned in
# main/render_manifests.py with htpasswd auth (as common-50-registry runs it) behind the real Caddy 2.10 with the /v2/ block and
# the loopback read listener of main/Caddyfile.tmpl (substituted as entrypoint.sh does). Checks that the registry refuses a push
# without the password and takes one with it (registry-push.sh), that an image is readable through Caddy without any client
# credentials (Caddy adds them for GET/HEAD only, a client's own Authorization is replaced), that PUT/POST/PATCH/DELETE get 405
# on both listeners and that a client outside REGISTRY_ALLOW_FROM gets 403. Needs docker, curl, python3 and access to Docker
# Hub (registry and caddy images): run it on the build server (newbar), never on the laptop. All containers are named
# nsu-llm-regtest-* on a private network and removed at the end.
set -u
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REG_IMAGE="$(sed -n 's/^DEFAULT_REGISTRY_IMAGE = "\(.*\)"/\1/p' "$REPO/main/render_manifests.py")"
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2.10-alpine}"
W="$(mktemp -d)"
NET=nsu-llm-regtest-net
export DOCKER_CONFIG="$W/docker-config"   # docker login for the push test stays in this directory
mkdir -p "$DOCKER_CONFIG"
cleanup() {
  docker rm -f nsu-llm-regtest-reg nsu-llm-regtest-caddy nsu-llm-regtest-caddy-deny >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker rmi -f 127.0.0.1:15001/nsu-llm-node:0.0.1-cpu 127.0.0.1:15001/nsu-llm-node:0.0.2-cpu >/dev/null 2>&1
  rm -rf "$W"
}
trap cleanup EXIT
FAILED=0
ok() { echo "ok   - $1"; }
bad() { echo "FAIL - $1"; FAILED=1; }
echo "registry image: $REG_IMAGE"

PW="$(openssl rand -hex 24)"
AUTH="Basic $(printf 'nsu-push:%s' "$PW" | base64 -w0)"
# the same hash entrypoint.sh makes (caddy hash-password --bcrypt-cost 6, password on stdin)
HASH="$(printf '%s\n' "$PW" | docker run --rm -i "$CADDY_IMAGE" caddy hash-password --bcrypt-cost 6)"
[[ "$HASH" =~ ^\$2[aby]\$06\$ ]] && ok "caddy hash-password gives a bcrypt hash of cost 6" || bad "caddy hash-password: $HASH"
mkdir -p "$W/auth"; printf 'nsu-push:%s\n' "$HASH" >"$W/auth/htpasswd"

# the /v2/ block and the 127.0.0.1:<REGISTRY_PORT> site of the template, verbatim, with the placeholders substituted
python3 - "$REPO/main/Caddyfile.tmpl" "$W" "$AUTH" <<'PYEOF'
import sys
t = open(sys.argv[1], encoding="utf-8").read()
i = t.index("\thandle /v2/* {")
j = t.index("\n\t}\n", i) + 4
block = t[i:j]
n0 = t.index("\t@nodes_denied {")
nodes = t[n0:t.index("\n\t}\n", t.index("\thandle @nodes_denied {", n0)) + 4]  # the node API filter (SEC-2 L1)
k = t.index("http://127.0.0.1:@@REGISTRY_PORT@@ {")
local = t[k:t.index("\n}\n", k) + 3]
def sub(s, allow):
    return (s.replace("@@REGISTRY_ALLOW_FROM@@", allow).replace("@@REGISTRY_AUTH@@", sys.argv[3])
             .replace("@@REGISTRY_PORT@@", "5000").replace("127.0.0.1:@@REGISTRY_BACKEND_PORT@@", "nsu-llm-regtest-reg:5001")
             .replace("http://127.0.0.1:5000 {\n\tbind 127.0.0.1", "http://:5000 {").replace("@@NODE_ALLOW_FROM@@", allow))
for name, allow in (("allow", "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 ::1 ::ffff:a00:0/104 ::ffff:ac10:0/108 ::ffff:c0a8:0/112 ::ffff:7f00:0/104"), ("deny", "10.99.0.0/16")):
    open(f"{sys.argv[2]}/Caddyfile.{name}", "w", encoding="utf-8").write(
        "{\n\tadmin off\n\tlocal_certs\n\tskip_install_trust\n\tauto_https disable_redirects\n}\nhttps://localhost:8443 {\n\ttls internal\n"
        + sub(block, allow) + sub(nodes, allow) + "\thandle {\n\t\trespond \"portal\" 200\n\t}\n}\n" + sub(local, allow))
PYEOF

docker network create "$NET" >/dev/null || exit 1
docker run -d --name nsu-llm-regtest-reg --network "$NET" -p 127.0.0.1:15001:5001 -v "$W/auth:/auth:ro" \
  -e REGISTRY_HTTP_ADDR=0.0.0.0:5001 -e REGISTRY_STORAGE_DELETE_ENABLED=true \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=nsu-llm-registry -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  "$REG_IMAGE" >/dev/null || { echo "cannot start registry"; exit 1; }
for i in $(seq 1 20); do curl -sS -m 2 -o /dev/null http://127.0.0.1:15001/v2/ >/dev/null 2>&1 && break; sleep 1; done

c="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:15001/v2/)"
[ "$c" = 401 ] && ok "the registry itself answers 401 without the password" || bad "registry without password: $c"
tar cf - --files-from /dev/null | docker import - 127.0.0.1:15001/nsu-llm-node:0.0.1-cpu >/dev/null
docker push 127.0.0.1:15001/nsu-llm-node:0.0.1-cpu >/dev/null 2>&1 && bad "push without the password must be refused" \
  || ok "push to the registry without the password is refused"
printf '%s' "$PW" | docker login -u nsu-push --password-stdin 127.0.0.1:15001 >/dev/null 2>&1 \
  && docker push 127.0.0.1:15001/nsu-llm-node:0.0.1-cpu >/dev/null 2>&1 && ok "push with the nsu-push password works (registry-push.sh)" \
  || bad "push with the password"
docker logout 127.0.0.1:15001 >/dev/null 2>&1

for mode in allow deny; do
  name=nsu-llm-regtest-caddy; [ "$mode" = deny ] && name=nsu-llm-regtest-caddy-deny
  port=18443; lport=15000; [ "$mode" = deny ] && { port=18444; lport=15002; }
  docker run -d --name "$name" --network "$NET" -p 127.0.0.1:$port:8443 -p 127.0.0.1:$lport:5000 \
    -v "$W/Caddyfile.$mode:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null || { echo "cannot start caddy ($mode)"; exit 1; }
done
for p in 18443 18444; do for i in $(seq 1 30); do curl -ksS -m 2 -o /dev/null https://localhost:$p/v2/ >/dev/null 2>&1 && break; sleep 1; done; done

ACC='Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'
code() { curl -ksS -m 10 -o /dev/null -w '%{http_code}' "$@"; }
c="$(code https://localhost:18443/v2/)";                                  [ "$c" = 200 ] && ok "GET /v2/ through Caddy from an allowed subnet, no client credentials: 200" || bad "GET /v2/ allowed: $c"
c="$(code -H 'Authorization: Basic bm9ib2R5Om5vcGU=' https://localhost:18443/v2/)"
[ "$c" = 200 ] && ok "a client's own (wrong) Authorization is replaced by Caddy: 200" || bad "client Authorization: $c"
t="$(curl -ksS -m 10 https://localhost:18443/v2/nsu-llm-node/tags/list)"; [[ "$t" == *'0.0.1-cpu'* ]] && ok "tags list through Caddy shows the pushed image" || bad "tags list: $t"
m="$(curl -ksS -m 10 -H "$ACC" -o /dev/null -w '%{http_code}' https://localhost:18443/v2/nsu-llm-node/manifests/0.0.1-cpu)"
[ "$m" = 200 ] && ok "manifest GET through Caddy: 200" || bad "manifest: $m"
h="$(code -I https://localhost:18443/v2/nsu-llm-node/manifests/0.0.1-cpu -H "$ACC")"
[ "$h" = 200 ] && ok "manifest HEAD through Caddy: 200" || bad "manifest HEAD: $h"
for meth in PUT POST PATCH DELETE; do
  c="$(code -X "$meth" https://localhost:18443/v2/nsu-llm-node/blobs/uploads/)"
  [ "$c" = 405 ] && ok "$meth through Caddy: 405 (read-only)" || bad "$meth through Caddy: $c"
done
c="$(code -X DELETE https://localhost:18443/v2/nsu-llm-node/manifests/sha256:0000000000000000000000000000000000000000000000000000000000000000)"
[ "$c" = 405 ] && ok "manifest DELETE through Caddy: 405" || bad "manifest DELETE: $c"
curl -ksS -m 10 -X POST https://localhost:18443/v2/nsu-llm-node/blobs/uploads/ | grep -q 'только для чтения' && ok "405 body is the Russian registry error JSON" || bad "405 body"
c="$(code https://localhost:18444/v2/)";                                  [ "$c" = 403 ] && ok "GET /v2/ from a subnet outside REGISTRY_ALLOW_FROM: 403" || bad "GET denied: $c"
curl -ksS -m 10 https://localhost:18444/v2/ | grep -q 'сети класса' && ok "403 body is the Russian registry error JSON" || bad "403 body"

# SEC-2 L1: the node API only from NODE_ALLOW_FROM; the rest of the portal (and /api/admin/nodes) is not affected
b() { curl -ksS -m 10 "$@"; }
[ "$(b https://localhost:18443/api/nodes/register)" = portal ] && ok "node API from an allowed subnet reaches the portal" || bad "node API allowed"
c="$(code https://localhost:18444/api/nodes/pc-01/heartbeat)"; c2="$(code https://localhost:18444/api/nodes)"
[ "$c" = 403 ] && [ "$c2" = 403 ] && ok "node API from outside NODE_ALLOW_FROM: 403" || bad "node API denied: $c $c2"
b https://localhost:18444/api/nodes/register | grep -q 'только из сети класса' && ok "403 body names the class network" || bad "node API 403 body"
[ "$(b https://localhost:18444/api/admin/nodes)" = portal ] && [ "$(b https://localhost:18444/api/nodesx)" = portal ] \
  && ok "admin node API and other paths are not filtered" || bad "other paths filtered"

# the loopback read listener of main (portal, install-main.sh): reads without credentials, no writes
t="$(curl -sS -m 10 http://127.0.0.1:15000/v2/nsu-llm-node/tags/list)"; [[ "$t" == *'0.0.1-cpu'* ]] && ok "127.0.0.1:<REGISTRY_PORT> listener: tags list without credentials" || bad "local tags list: $t"
h="$(curl -sS -m 10 -I -o /dev/null -w '%{http_code}' -H "$ACC" http://127.0.0.1:15000/v2/nsu-llm-node/manifests/0.0.1-cpu)"
[ "$h" = 200 ] && ok "127.0.0.1:<REGISTRY_PORT> listener: manifest HEAD 200 (install-main registry_digest)" || bad "local HEAD: $h"
for meth in PUT POST PATCH DELETE; do
  c="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X "$meth" http://127.0.0.1:15000/v2/nsu-llm-node/blobs/uploads/)"
  [ "$c" = 405 ] && ok "$meth on the loopback read listener: 405" || bad "$meth on the loopback listener: $c"
done
tar cf - --files-from /dev/null | docker import - 127.0.0.1:15000/nsu-llm-node:0.0.2-cpu >/dev/null
docker push 127.0.0.1:15000/nsu-llm-node:0.0.2-cpu >/dev/null 2>&1 && bad "docker push through the read listener must fail" \
  || ok "docker push through the loopback read listener fails"
docker rmi -f 127.0.0.1:15000/nsu-llm-node:0.0.2-cpu >/dev/null 2>&1
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
