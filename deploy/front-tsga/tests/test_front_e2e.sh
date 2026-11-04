#!/usr/bin/env bash
# End-to-end check of the tsga front with real nginx and the real main image (needs docker; run on a build server):
#   main  = nsu-llm-main image, dry-run entrypoint + Caddy with PUBLIC_HOSTNAME=double.fresh.nsuts.ru, internal CA,
#           TRUSTED_PROXIES=<nginx>, fake portal and LiteLLM upstreams (SSE with 1 s pauses)
#   front = nginx (NGINX_IMAGE, default nginx:1.18, the version on tsga) with the site switch-front.sh renders (--dry-run
#           against the live main: CA fetched from /ca.crt, main checked against it)
#   client = the main image as a curl box
# Checks: nginx -t, SNI and CA verification, X-Forwarded-For, SSE without buffering, an upload over 1 MB, :80 redirect,
# and a 502 when the CA does not match. Everything it creates is named t220e2e-* and removed at exit.
# Usage: IMAGE=nsu-llm-main:0.1.0 bash deploy/front-tsga/tests/test_front_e2e.sh
set -uo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "$HERE/.." && pwd)"
IMAGE="${IMAGE:-nsu-llm-main:0.1.0}"
NGINX_IMAGE="${NGINX_IMAGE:-nginx:1.18}"
P=t220e2e
NET="$P-net"
SUBNET="${E2E_SUBNET:-172.30.220}"
MAIN_IP="$SUBNET.10"; FRONT_IP="$SUBNET.11"; CLIENT_IP="$SUBNET.12"
H=double.fresh.nsuts.ru
TMP="$(mktemp -d)"
FAILED=0

cleanup() {
  docker rm -f "$P-main" "$P-front" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker volume rm -f "$P-vol" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi; }

cleanup; TMP="$(mktemp -d)"
docker network create --subnet "$SUBNET.0/24" "$NET" >/dev/null || { echo "FAIL - docker network $SUBNET.0/24"; exit 1; }

# ------------------------------------------------------------------ main
cat >"$TMP/upstream.py" <<'EOS'
import sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = 0
        while got < n:
            c = self.rfile.read(min(65536, n - got))
            if not c:
                break
            got += len(c)
        return got
    def _answer(self):
        got = self._body()
        if self.path.startswith("/v1/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(4):
                data = f"data: {{\"n\": {i}, \"pad\": \"{'x' * 300}\"}}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data)); self.wfile.flush()
                time.sleep(1)
            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            return
        h = self.headers
        body = f"host={h.get('Host')} xff={h.get('X-Forwarded-For')} xfp={h.get('X-Forwarded-Proto')} bytes={got}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PUT = _answer
    def log_message(self, *a):
        pass

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
EOS
docker volume create "$P-vol" >/dev/null
docker run -d --name "$P-main" --network "$NET" --ip "$MAIN_IP" -v "$P-vol:/var/lib/llm" -v "$TMP:/mnt/t:ro" \
  -e LLM_ENTRYPOINT_DRY_RUN=1 -e MAIN_ADVERTISE_IP="$MAIN_IP" -e OIDC_CLIENT_SECRET=test -e PUBLIC_HOSTNAME="$H" \
  -e TRUSTED_PROXIES="$FRONT_IP" --entrypoint sh "$IMAGE" -c '
    /opt/llm/main/entrypoint.sh >/tmp/ep.log 2>&1 || { cat /tmp/ep.log; exit 1; }
    export XDG_DATA_HOME=/var/lib/llm/caddy-data XDG_CONFIG_HOME=/tmp/caddy-config
    python3 /mnt/t/upstream.py 8000 & python3 /mnt/t/upstream.py 4000 &
    exec caddy run --config /run/llm/Caddyfile --adapter caddyfile' >/dev/null
c() { docker run --rm --network "$NET" --ip "$CLIENT_IP" -v "$TMP:/mnt/t" --entrypoint sh "$IMAGE" -c "$1" 2>&1; }
for i in $(seq 1 60); do
  [ "$(c "curl -sk -o /dev/null -w '%{http_code}' https://$MAIN_IP/readyz")" = 200 ] && break
  sleep 1
done

# ------------------------------------------------------------------ site rendered by switch-front.sh --dry-run (real curl)
R="$TMP/root"; mkdir -p "$R/etc/nginx/sites-enabled" "$TMP/stubs"
printf 'server {\n    listen 443 ssl http2;\n    server_name %s;\n    ssl_certificate /etc/nginx/ssl/selfsigned.crt;\n    ssl_certificate_key /etc/nginx/ssl/selfsigned.key;\n    location / { proxy_pass http://127.0.0.1:5001; }\n}\n' "$H" \
  >"$R/etc/nginx/sites-enabled/$H"
for s in docker systemctl nginx; do printf '#!/bin/sh\nexit 1\n' >"$TMP/stubs/$s"; chmod +x "$TMP/stubs/$s"; done
# curl runs in a container on the test network (the host may not route to it); the script's temp files live in $TMP
# (TMPDIR), mounted at the same path, and the container runs as this user so they stay ours
printf '#!/bin/sh\nexec %s run --rm --user %s --network %s -v %s:%s --entrypoint curl %s "$@"\n' \
  "$(command -v docker)" "$(id -u):$(id -g)" "$NET" "$TMP" "$TMP" "$IMAGE" >"$TMP/stubs/curl"
chmod +x "$TMP/stubs/curl"
dry="$(PATH="$TMP/stubs:$PATH" TMPDIR="$TMP" FRONT_ROOT="$R" bash "$DIR/switch-front.sh" --main "$MAIN_IP" --dry-run 2>&1)"; rc=$?
mkdir -p "$TMP/conf.d"
sed -n '/^Новый файл сайта:$/,$p' <<<"$dry" | tail -n +2 | sed 's/^    //' >"$TMP/conf.d/default.conf"
[ "$rc" = 0 ] && grep -q "proxy_pass https://$MAIN_IP;" "$TMP/conf.d/default.conf"
check "switch-front.sh --dry-run against the live main: CA fetched, main verified, site rendered" $? "rc=$rc
$dry"

# ------------------------------------------------------------------ front (nginx)
c "curl -s http://$MAIN_IP/ca.crt" >"$TMP/ca.crt"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$TMP/selfsigned.key" -out "$TMP/selfsigned.crt" -subj "/CN=$H" -days 2 >/dev/null 2>&1
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$TMP/other.key" -out "$TMP/other.crt" \
  -subj /CN=other -days 2 >/dev/null 2>&1
# like conf.d/gzip.conf on tsga, plus text/event-stream: the site's gzip off must win
cat >"$TMP/conf.d/gzip.conf" <<'EOS'
gzip on;
gzip_proxied any;
gzip_types text/plain text/css application/json application/javascript text/event-stream;
EOS
chmod 755 "$TMP/conf.d"; chmod 644 "$TMP"/*.crt "$TMP"/*.key "$TMP/conf.d"/*
docker run -d --name "$P-front" --network "$NET" --ip "$FRONT_IP" -v "$TMP/conf.d:/etc/nginx/conf.d:ro" \
  -v "$TMP/ca.crt:/etc/nginx/nsu-llm-main-ca.crt:ro" -v "$TMP/other.crt:/etc/nginx/other-ca.crt:ro" \
  -v "$TMP/selfsigned.crt:/etc/nginx/ssl/selfsigned.crt:ro" -v "$TMP/selfsigned.key:/etc/nginx/ssl/selfsigned.key:ro" \
  "$NGINX_IMAGE" >/dev/null
sleep 2
out="$(docker exec "$P-front" nginx -t 2>&1)"
grep -q 'test is successful' <<<"$out"
check "nginx $(docker exec "$P-front" nginx -v 2>&1 | sed 's/.*\///') accepts the rendered site (nginx -t)" $? "$out
$(docker logs "$P-front" 2>&1 | tail -5)"

F="--resolve $H:443:$FRONT_IP --resolve $H:80:$FRONT_IP"
out="$(c "curl -sk $F -H 'X-Forwarded-For: 6.6.6.6' -w ' status=%{http_code}' https://$H/readyz")"
[ "$out" = "host=$H xff=$CLIENT_IP, $FRONT_IP xfp=https bytes=0 status=200" ]
check "through nginx: Host kept, X-Forwarded-For = the client (spoofed value dropped), then nginx; proto https" $? "$out"
out="$(c "curl -s $F -o /dev/null -w '%{http_code} %{redirect_url}' http://$H/v1/models")"
[ "$out" = "301 https://$H/v1/models" ]
check "port 80: 301 to https" $? "$out"
out="$(c "head -c 52428800 /dev/zero >/tmp/50m; curl -sk $F -X PUT --data-binary @/tmp/50m -w ' status=%{http_code}' https://$H/api/admin/local-models/uploads/u1")"
grep -q 'bytes=52428800 status=200' <<<"$out"
check "50 MB upload part passes nginx (no body limit there) and reaches the portal" $? "$out"
out="$(c "curl -skN $F -H 'Accept-Encoding: gzip' --data '{}' https://$H/v1/chat/completions | while IFS= read -r l; do [ -n \"\$l\" ] && echo \"\$(date +%s.%N | cut -c1-14) \$(printf %s \"\$l\" | cut -c1-12)\"; done")"
first="$(head -n1 <<<"$out" | cut -d' ' -f1)"; last="$(tail -n1 <<<"$out" | cut -d' ' -f1)"
[ "$(grep -c 'data: {"n"' <<<"$out")" = 4 ] && awk -v a="$first" -v b="$last" 'BEGIN { exit !(b - a >= 2.5) }'
check "SSE through nginx arrives event by event (not buffered, not gzipped)" $? "$out"

# wrong CA on the front: nginx must refuse the main
sed -i 's|/etc/nginx/nsu-llm-main-ca.crt|/etc/nginx/other-ca.crt|' "$TMP/conf.d/default.conf"
docker exec "$P-front" nginx -s reload >/dev/null 2>&1
sleep 1
out="$(c "curl -sk $F -o /dev/null -w '%{http_code}' https://$H/readyz")"
[ "$out" = 502 ] && docker logs "$P-front" 2>&1 | grep -q 'upstream SSL certificate verify error'
check "front with a CA that did not sign the main: 502, nginx logs the certificate verify error" $? "$out
$(docker logs "$P-front" 2>&1 | tail -3)"

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
