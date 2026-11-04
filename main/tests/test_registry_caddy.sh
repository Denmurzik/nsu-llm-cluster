#!/usr/bin/env bash
# Functional test of the class registry (task 2-25) with docker: the real registry image pinned in main/render_manifests.py behind
# the real Caddy 2.10 with the /v2/ block of main/Caddyfile.tmpl (substituted as entrypoint.sh does). Checks that an image pushed to
# the registry on localhost (how install-main.sh seeds it) is readable through Caddy, that PUT/POST/PATCH/DELETE get 405 and that a
# client outside REGISTRY_ALLOW_FROM gets 403. Needs docker, curl, python3 and access to Docker Hub (registry and caddy images):
# run it on the build server (newbar), never on the laptop. All containers are named nsu-llm-regtest-* on a private network and removed at the end.
set -u
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REG_IMAGE="$(sed -n 's/^DEFAULT_REGISTRY_IMAGE = "\(.*\)"/\1/p' "$REPO/main/render_manifests.py")"
W="$(mktemp -d)"
NET=nsu-llm-regtest-net
cleanup() {
  docker rm -f nsu-llm-regtest-reg nsu-llm-regtest-caddy nsu-llm-regtest-caddy-deny >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker rmi -f 127.0.0.1:15000/nsu-llm-node:0.0.1-cpu >/dev/null 2>&1
  rm -rf "$W"
}
trap cleanup EXIT
FAILED=0
ok() { echo "ok   - $1"; }
bad() { echo "FAIL - $1"; FAILED=1; }
echo "registry image: $REG_IMAGE"

# the /v2/ block of the template, verbatim, with the placeholders substituted
python3 - "$REPO/main/Caddyfile.tmpl" "$W" <<'PYEOF'
import re, sys
t = open(sys.argv[1], encoding="utf-8").read()
i = t.index("\thandle /v2/* {")
j = t.index("\n\t}\n", i) + 4
block = t[i:j]
for name, allow in (("allow", "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 ::1"), ("deny", "10.99.0.0/16")):
    b = block.replace("@@REGISTRY_ALLOW_FROM@@", allow).replace("@@REGISTRY_PORT@@", "5000").replace("127.0.0.1:5000", "nsu-llm-regtest-reg:5000")
    open(f"{sys.argv[2]}/Caddyfile.{name}", "w", encoding="utf-8").write(
        "{\n\tadmin off\n\tlocal_certs\n\tskip_install_trust\n\tauto_https disable_redirects\n}\nhttps://localhost:8443 {\n\ttls internal\n" + b + "}\n")
PYEOF

docker network create "$NET" >/dev/null || exit 1
docker run -d --name nsu-llm-regtest-reg --network "$NET" -p 127.0.0.1:15000:5000 \
  -e REGISTRY_HTTP_ADDR=0.0.0.0:5000 -e REGISTRY_STORAGE_DELETE_ENABLED=true "$REG_IMAGE" >/dev/null || { echo "cannot start registry"; exit 1; }
for i in $(seq 1 20); do curl -fsS -m 2 http://127.0.0.1:15000/v2/ >/dev/null 2>&1 && break; sleep 1; done

# a tiny image pushed straight to the registry (this is what install-main does from inside main: localhost, no Caddy)
tar cf - --files-from /dev/null | docker import - 127.0.0.1:15000/nsu-llm-node:0.0.1-cpu >/dev/null
docker push 127.0.0.1:15000/nsu-llm-node:0.0.1-cpu >/dev/null 2>&1 && ok "push to the registry on localhost works" || bad "push to localhost"

for mode in allow deny; do
  name=nsu-llm-regtest-caddy; [ "$mode" = deny ] && name=nsu-llm-regtest-caddy-deny
  port=18443; [ "$mode" = deny ] && port=18444
  docker run -d --name "$name" --network "$NET" -p 127.0.0.1:$port:8443 \
    -v "$W/Caddyfile.$mode:/etc/caddy/Caddyfile:ro" caddy:2.10-alpine >/dev/null || { echo "cannot start caddy ($mode)"; exit 1; }
done
for p in 18443 18444; do for i in $(seq 1 30); do curl -ksS -m 2 -o /dev/null https://localhost:$p/v2/ >/dev/null 2>&1 && break; sleep 1; done; done

ACC='Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'
code() { curl -ksS -m 10 -o /dev/null -w '%{http_code}' "$@"; }
c="$(code https://localhost:18443/v2/)";                                  [ "$c" = 200 ] && ok "GET /v2/ through Caddy from an allowed subnet: 200" || bad "GET /v2/ allowed: $c"
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
# the registry itself is not reachable except through its own port on the private network (main binds 127.0.0.1 in its container)
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
