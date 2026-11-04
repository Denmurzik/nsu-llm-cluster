#!/usr/bin/env bash
# Offline tests for deploy/front-tsga/switch-front.sh: docker, nginx, systemctl, curl, sleep and hostname are stubs that log
# their calls; /etc/nginx and /var/backups live under a temp root (FRONT_ROOT). openssl is the real one (CA fingerprint).
# Also a static check of site.conf.tmpl (SSE, buffering, timeouts, TLS to the main).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "$HERE/.." && pwd)"
SCRIPT="$DIR/switch-front.sh"
TEMPLATE="$DIR/site.conf.tmpl"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0
H=double.fresh.nsuts.ru

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { # check NAME CONDITION-EXIT-CODE [DETAIL]
  if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi
}

# ------------------------------------------------------------------ static check of the template
tpl_has() { grep -qE "^[[:space:]]*$1[[:space:]]*$" "$TEMPLATE"; }
missing=""
for line in 'proxy_buffering off;' 'proxy_request_buffering off;' 'proxy_read_timeout 3600s;' 'proxy_send_timeout 3600s;' \
  'client_max_body_size 0;' 'gzip off;' 'proxy_http_version 1\.1;' 'proxy_set_header Connection "";' \
  'proxy_set_header Host \$host;' 'proxy_set_header X-Real-IP \$remote_addr;' 'proxy_set_header X-Forwarded-For \$remote_addr;' \
  'proxy_set_header X-Forwarded-Proto https;' 'proxy_pass https://@@MAIN@@;' 'proxy_ssl_server_name on;' \
  'proxy_ssl_name @@HOSTNAME@@;' 'proxy_ssl_verify on;' 'proxy_ssl_verify_depth 2;' 'proxy_ssl_trusted_certificate @@CA@@;' \
  'ssl_certificate @@SSL_CERTIFICATE@@;' 'ssl_certificate_key @@SSL_CERTIFICATE_KEY@@;' 'listen 80;' 'listen 443 ssl http2;' \
  'location /\.well-known/acme-challenge/ \{' 'root /var/www/html;' 'return 301 https://\$host\$request_uri;'; do
  tpl_has "$line" || missing+="$line"$'\n'
done
[ -z "$missing" ]
check "template: SSE/upload without buffering, 3600 s timeouts, no body limit, TLS to the main verified by its CA, ACME and 301 on :80" $? "missing:
$missing"
[ "$(grep -c '^[[:space:]]*proxy_buffering' "$TEMPLATE")" = 1 ] && ! grep -q 'proxy_set_header Upgrade' "$TEMPLATE"
check "template: one proxy_buffering line, no websocket Upgrade passthrough" $?
head -n1 "$TEMPLATE" | grep -qF '# nsu-llm-front: managed by switch-front.sh'
check "template: first line is the marker switch-front.sh recognises" $?

# ------------------------------------------------------------------ stubs
STUBS="$TMP/stubs"; mkdir -p "$STUBS"
cat >"$STUBS/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >>"$FAKE_LOG"
st="$FAKE_STATE/containers"
# FAKE_DOCKER_FAIL="stop:NAME start:NAME ...": that call fails like a docker daemon error
for f in ${FAKE_DOCKER_FAIL:-}; do
  if [ "$f" = "$1:${!#}" ]; then echo "Error response from daemon: injected failure of $1 ${!#}" >&2; exit 1; fi
done
case "$1" in
  inspect)
    if [ "$2" = -f ]; then name="$4"; else name="$2"; fi
    [ -f "$st/$name" ] || { echo "Error: No such object: $name" >&2; exit 1; }
    if [ "$2" = -f ]; then cat "$st/$name"; else echo '[{}]'; fi ;;
  update)
    pol="${2#--restart=}"; name="$3"
    [ -f "$st/$name" ] || exit 1
    read -r _ _ running <"$st/$name"
    case "$pol" in on-failure:*) n="${pol#on-failure:}"; pol=on-failure ;; *) n=0 ;; esac
    echo "$pol $n $running" >"$st/$name"; echo "$name" ;;
  stop|start)
    name="$2"; [ -f "$st/$name" ] || exit 1
    read -r pol n _ <"$st/$name"
    if [ "$1" = stop ]; then echo "$pol $n false" >"$st/$name"; else echo "$pol $n true" >"$st/$name"; fi
    echo "$name" ;;
esac
EOS
cat >"$STUBS/nginx" <<'EOS'
#!/usr/bin/env bash
echo "nginx $*" >>"$FAKE_LOG"
site="$FRONT_ROOT/etc/nginx/sites-enabled/double.fresh.nsuts.ru"
if [ "${FAKE_NGINX_FAIL:-0}" = 1 ]; then
  echo 'nginx: [emerg] unexpected "}" in /etc/nginx/sites-enabled/double.fresh.nsuts.ru:40' >&2; exit 1
fi
# like nginx: the trusted certificate the site names must exist
ca="$(sed -n 's/^[[:space:]]*proxy_ssl_trusted_certificate[[:space:]]*\([^;]*\);/\1/p' "$site")"
if [ -n "$ca" ] && [ ! -f "$FRONT_ROOT$ca" ]; then echo "nginx: [emerg] cannot load $ca" >&2; exit 1; fi
grep -q '@@' "$site" && { echo "nginx: [emerg] placeholder left" >&2; exit 1; }
echo "nginx: configuration file /etc/nginx/nginx.conf test is successful" >&2
EOS
cat >"$STUBS/systemctl" <<'EOS'
#!/usr/bin/env bash
echo "systemctl $*" >>"$FAKE_LOG"
[ "${FAKE_SYSTEMCTL_FAIL:-0}" = 1 ] && { echo "Job for nginx.service failed (injected)" >&2; exit 1; }
exit 0
EOS
cat >"$STUBS/curl" <<'EOS'
#!/usr/bin/env bash
echo "curl $*" >>"$FAKE_LOG"
out=""; cacert=""; args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in -o) out="${args[i+1]}" ;; --cacert) cacert="${args[i+1]}" ;; esac
done
case "$*" in
  */ca.crt*) [ "${FAKE_CA_CODE:-200}" = 200 ] && cp "$FAKE_CA_SRC" "$out"; printf '%s' "${FAKE_CA_CODE:-200}" ;;
  *--connect-to*)
    # the main answers only if the caller verifies it against the main's CA
    if cmp -s "$cacert" "$FAKE_MAIN_CA"; then printf '%s' "${FAKE_MAIN_CODE:-200}"; else printf '000'; exit 60; fi ;;
  *--resolve*) printf '%s' "${FAKE_FRONT_CODE:-200}" ;;
  *) printf '000'; exit 7 ;;
esac
EOS
printf '#!/usr/bin/env bash\necho "sleep $*" >>"$FAKE_LOG"\n' >"$STUBS/sleep"
printf '#!/usr/bin/env bash\necho "10.4.0.105 172.17.0.1 "\n' >"$STUBS/hostname"
chmod +x "$STUBS"/*

# a real CA for the fingerprint, and a second one (another main)
mkca() { openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$TMP/$1.key" -out "$TMP/$1.crt" \
  -subj "/CN=$1" -days 2 >/dev/null 2>&1; }
mkca ca1; mkca ca2
FP1="$(openssl x509 -in "$TMP/ca1.crt" -noout -fingerprint -sha256 | sed 's/^[^=]*=//')"

ORIG_SITE='server {
    listen 80;
    server_name double.fresh.nsuts.ru;
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    location / {
        return 301 https://$host$request_uri;
    }
}
server {
    listen 443 ssl http2;
    server_name double.fresh.nsuts.ru;
    ssl_certificate /etc/nginx/ssl/selfsigned.crt;
    ssl_certificate_key /etc/nginx/ssl/selfsigned.key;
    client_max_body_size 100M;
    location /test/ { proxy_pass http://127.0.0.1:5002; }
    location / { proxy_pass http://127.0.0.1:5001; }
}'

# new_root NAME: fresh tsga root, container state and log
new_root() {
  R="$TMP/$1"; mkdir -p "$R/etc/nginx/sites-enabled" "$R/etc/nginx/ssl" "$R/state/containers"
  printf '%s\n' "$ORIG_SITE" >"$R/etc/nginx/sites-enabled/$H"
  printf '%s\n' "$ORIG_SITE" >"$R/etc/nginx/sites-enabled/$H.bak"
  SITE="$R/etc/nginx/sites-enabled/$H"; CA="$R/etc/nginx/nsu-llm-main-ca.crt"; BK="$R/var/backups/nsu-llm-front"
  for c in double.fresh.nsuts double.fresh.nsuts.test daemon_server daemon_server_test deploy-db-1 gitlab-runner; do
    echo "always 0 true" >"$R/state/containers/$c"
  done
  export FRONT_ROOT="$R" FAKE_STATE="$R/state" FAKE_LOG="$R/log" FAKE_CA_SRC="$TMP/ca1.crt" FAKE_MAIN_CA="$TMP/ca1.crt"
  : >"$FAKE_LOG"
}
sw() { env PATH="$STUBS:$PATH" FRONT_READY_TRIES="${TRIES:-3}" bash "$SCRIPT" "$@" 2>&1; }
log_has() { grep -qF -- "$1" "$FAKE_LOG"; }
state() { cat "$FAKE_STATE/containers/$1"; }
same_as_orig() { [ "$(cat "$SITE")" = "$ORIG_SITE" ]; }
nbackups() { find "$BK" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '; }

# ------------------------------------------------------------------ 1. dry run: nothing changes
new_root dry
out="$(sw --main 10.9.8.7 --dry-run </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && [ ! -e "$CA" ] && [ ! -e "$BK" ] && ! log_has "docker update" && ! log_has "docker stop" \
  && ! log_has "systemctl" && ! log_has "nginx -t" && [ "$(state double.fresh.nsuts)" = "always 0 true" ] \
  && grep -q 'proxy_pass https://10.9.8.7;' <<<"$out" && grep -q 'ssl_certificate /etc/nginx/ssl/selfsigned.crt;' <<<"$out" \
  && grep -q 'docker update --restart=no daemon_server_test' <<<"$out" && grep -qF "$FP1" <<<"$out"
check "--dry-run: shows the fingerprint, the new site and the steps; no file, container or nginx change" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# ------------------------------------------------------------------ 2. fingerprint not confirmed
new_root decline
out="$(sw --main 10.9.8.7 <<<$'n\n')"; rc=$?
[ "$rc" != 0 ] && same_as_orig && [ ! -e "$CA" ] && [ ! -e "$BK" ] && ! log_has "docker update" && grep -qF "$FP1" <<<"$out" \
  && log_has "https://10.9.8.7/ca.crt"
check "CA fetched from https://<main>/ca.crt: SHA-256 printed, 'n' changes nothing" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 3. switch
new_root switch
echo "on-failure 3 true" >"$R/state/containers/daemon_server"
echo "unless-stopped 0 false" >"$R/state/containers/daemon_server_test"
out="$(sw --main 10.9.8.7 <<<$'y\n')"; rc=$?
b="$(find "$BK" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ "$rc" = 0 ] && [ "$(nbackups)" = 1 ] && [ "$(cat "$b/site")" = "$ORIG_SITE" ] && [ -f "$b/complete" ] && [ ! -e "$b/ca.crt" ] \
  && grep -qx 'double.fresh.nsuts always true' "$b/containers" && grep -qx 'daemon_server on-failure:3 true' "$b/containers" \
  && grep -qx 'daemon_server_test unless-stopped false' "$b/containers" && grep -qx 'double.fresh.nsuts.test always true' "$b/containers"
check "switch: backup of the site and of the restart policies (on-failure:N, unless-stopped, running state)" $? "rc=$rc
$out
$(ls -la "$b" 2>&1; cat "$b/containers" 2>&1)"
head -n1 "$SITE" | grep -qF '# nsu-llm-front: managed by switch-front.sh' && grep -q 'proxy_pass https://10.9.8.7;' "$SITE" \
  && grep -q 'ssl_certificate /etc/nginx/ssl/selfsigned.crt;' "$SITE" && grep -q 'ssl_certificate_key /etc/nginx/ssl/selfsigned.key;' "$SITE" \
  && grep -q "proxy_ssl_name $H;" "$SITE" && grep -q 'proxy_ssl_trusted_certificate /etc/nginx/nsu-llm-main-ca.crt;' "$SITE" \
  && ! grep -q '@@\|5001\|5002' "$SITE" && cmp -s "$CA" "$TMP/ca1.crt" && [ ! -e "$R/etc/nginx/.nsu-llm-front-site.tmp" ] \
  && [ "$(cat "$R/etc/nginx/sites-enabled/$H.bak")" = "$ORIG_SITE" ]
check "switch: new site from the template (old ssl_certificate paths kept), main's CA installed, other site files untouched" $? "$(cat "$SITE")"
for c in double.fresh.nsuts double.fresh.nsuts.test daemon_server daemon_server_test; do
  log_has "docker update --restart=no $c" && log_has "docker stop $c" && grep -q '^no 0 false$' "$FAKE_STATE/containers/$c" || { false; break; }
done
check "switch: the four LMS containers get --restart=no and are stopped" $? "$(cat "$FAKE_LOG")"
! grep -q 'deploy-db-1\|gitlab-runner' "$FAKE_LOG" && [ "$(state gitlab-runner)" = "always 0 true" ] && [ "$(state deploy-db-1)" = "always 0 true" ]
check "switch: databases and gitlab-runner are not touched" $? "$(cat "$FAKE_LOG")"
order="$(grep -n 'nginx -t\|docker stop double.fresh.nsuts$\|systemctl reload nginx\|--resolve' "$FAKE_LOG" | cut -d: -f1 | tr '\n' ' ')"
grep -q "curl .*--cacert .* --connect-to $H:443:10.9.8.7:443 .*https://$H/readyz" "$FAKE_LOG" \
  && grep -q "curl .*--resolve $H:443:127.0.0.1 https://$H/readyz" "$FAKE_LOG" && [ "$(grep -c 'systemctl reload nginx' "$FAKE_LOG")" = 1 ] \
  && read -r a1 a2 a3 a4 <<<"$order" && [ "$a1" -lt "$a2" ] && [ "$a2" -lt "$a3" ] && [ "$a3" -lt "$a4" ]
check "switch: main checked against its CA first, then nginx -t, docker stop, reload, /readyz through the local nginx" $? "order=$order
$(cat "$FAKE_LOG")"
grep -q 'conflicting server name' <<<"$out" && grep -q -- '--rollback' <<<"$out"
check "switch: warns about the .bak site declaring the same name, prints the rollback command" $? "$out"

# ------------------------------------------------------------------ 4. second switch: new main, only the site is rewritten
: >"$FAKE_LOG"
out="$(sw --main 10.9.8.8 --ca "$TMP/ca2.crt" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'сертификат не прошёл проверку' <<<"$out" && grep -q 'proxy_pass https://10.9.8.7;' "$SITE"
check "re-switch: a main that does not verify against the given CA is refused before any change" $? "rc=$rc
$out"
export FAKE_MAIN_CA="$TMP/ca2.crt"
: >"$FAKE_LOG"
out="$(sw --main 10.9.8.8 --ca "$TMP/ca2.crt" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'proxy_pass https://10.9.8.8;' "$SITE" && ! grep -q '10.9.8.7' "$SITE" && cmp -s "$CA" "$TMP/ca2.crt" \
  && [ "$(nbackups)" = 1 ] && ! log_has "docker update" && ! log_has "docker stop" && ! log_has "/ca.crt" \
  && log_has "systemctl reload nginx" && grep -q 'переписываются только' <<<"$out"
check "re-switch with a new --main and --ca: site and CA rewritten, no new backup, containers not touched, no prompt" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# ------------------------------------------------------------------ 5. nginx -t fails on a re-switch: the previous site comes back
export FAKE_MAIN_CA="$TMP/ca1.crt"
: >"$FAKE_LOG"
out="$(FAKE_NGINX_FAIL=1 sw --main 10.9.8.9 --ca "$TMP/ca1.crt" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'proxy_pass https://10.9.8.8;' "$SITE" && cmp -s "$CA" "$TMP/ca2.crt" && ! log_has "systemctl" \
  && grep -q 'unexpected' <<<"$out"
check "re-switch, nginx -t fails: previous site and CA restored, no reload, nginx's error shown" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 6. rollback
: >"$FAKE_LOG"
out="$(sw --rollback --dry-run </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'proxy_pass https://10.9.8.8;' "$SITE" && [ -f "$b/complete" ] && ! log_has "docker update" \
  && ! log_has "systemctl" && grep -q 'docker update --restart=on-failure:3 daemon_server' <<<"$out"
check "rollback --dry-run: shows the steps, changes nothing" $? "rc=$rc
$out"
: >"$FAKE_LOG"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && [ ! -e "$CA" ] && [ -f "$b/rolled-back" ] && [ ! -f "$b/complete" ] \
  && [ "$(state double.fresh.nsuts)" = "always 0 true" ] && [ "$(state daemon_server)" = "on-failure 3 true" ] \
  && [ "$(state daemon_server_test)" = "unless-stopped 0 false" ] && ! log_has "docker start daemon_server_test" \
  && log_has "docker update --restart=on-failure:3 daemon_server" && log_has "docker start double.fresh.nsuts.test" \
  && ! grep -q 'deploy-db-1\|gitlab-runner' "$FAKE_LOG"
check "rollback: original site back, CA removed, restart policies and running state restored" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
order="$(grep -n 'nginx -t\|docker start double.fresh.nsuts$\|systemctl reload nginx' "$FAKE_LOG" | cut -d: -f1 | tr '\n' ' ')"
read -r a1 a2 a3 <<<"$order"; [ -n "${a3:-}" ] && [ "$a1" -lt "$a2" ] && [ "$a2" -lt "$a3" ]
check "rollback: nginx -t, containers started, then nginx reloaded" $? "order=$order"
: >"$FAKE_LOG"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && ! log_has "docker update" && ! log_has "docker start" && grep -q 'файл сайта уже прежний' <<<"$out" \
  && grep -q 'double.fresh.nsuts: уже как до переключения' <<<"$out"
check "second rollback is idempotent: nothing to change, exit 0" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# ------------------------------------------------------------------ 7. switch again after a rollback: a new backup, LMS stopped again
: >"$FAKE_LOG"
out="$(sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(nbackups)" = 2 ] && log_has "docker stop double.fresh.nsuts" && grep -q 'proxy_pass https://10.9.8.7;' "$SITE"
check "switch after a rollback (--yes, no prompt): a second backup, LMS stopped again" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 8. nginx -t fails on the first switch
new_root ngfail
out="$(FAKE_NGINX_FAIL=1 sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
b="$(find "$BK" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ "$rc" != 0 ] && same_as_orig && [ ! -e "$CA" ] && ! log_has "docker update" && ! log_has "docker stop" && ! log_has "systemctl" \
  && [ -f "$b/failed" ] && [ ! -f "$b/complete" ] && [ "$(state double.fresh.nsuts)" = "always 0 true" ]
check "first switch, nginx -t fails: site restored, CA removed, LMS keeps running, no reload" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" != 0 ] && same_as_orig && ! log_has "docker start"
check "rollback after a failed switch has nothing to do" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 9. main not ready / no ca.crt: nothing changes
new_root notready
out="$(FAKE_MAIN_CODE=502 sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && same_as_orig && [ ! -e "$BK" ] && [ ! -e "$CA" ] && ! log_has "docker update" && grep -q 'readyz: 502' <<<"$out"
check "main's /readyz not 200: refused before any change" $? "rc=$rc
$out"
out="$(FAKE_CA_CODE=404 sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && same_as_orig && [ ! -e "$BK" ] && grep -q -- '--ca FILE' <<<"$out"
check "main without /ca.crt (own certificate): asks for --ca, nothing changed" $? "rc=$rc
$out"
printf 'not a certificate\n' >"$TMP/bad.crt"
out="$(sw --main 10.9.8.7 --ca "$TMP/bad.crt" </dev/null)"; rc=$?
[ "$rc" != 0 ] && same_as_orig && grep -q 'блоков PEM в файле 0' <<<"$out"
check "--ca that is not a PEM certificate is refused" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 10. /readyz through the local nginx fails after the switch
new_root frontfail
out="$(FAKE_FRONT_CODE=502 TRIES=2 sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q -- '--rollback' <<<"$out" && grep -q 'readyz через nginx tsga отвечает 502' <<<"$out" \
  && [ "$(grep -c -- '--resolve' "$FAKE_LOG")" = 2 ] && log_has "sleep 3"
check "/readyz through tsga's nginx not 200 after retries: non-zero exit with the rollback command" $? "rc=$rc
$out"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && [ "$(state double.fresh.nsuts)" = "always 0 true" ]
check "... and the rollback brings the LMS back" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 11. a missing LMS container is skipped both ways
new_root absent
rm "$R/state/containers/daemon_server_test"
out="$(sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
b="$(find "$BK" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ "$rc" = 0 ] && grep -qx 'daemon_server_test absent -' "$b/containers" && ! log_has "docker stop daemon_server_test" \
  && log_has "docker stop daemon_server"
check "switch: an LMS container that does not exist is skipped and recorded as absent" $? "rc=$rc
$out"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "docker start daemon_server_test" && same_as_orig
check "rollback: the absent container is skipped" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 12. bad --main values
new_root badmain
for bad in '10.9.8.7;' '10.9.8.7 }' 'https://10.9.8.7' "$H" '127.0.0.1' '10.4.0.105' '300.1.1.1' ''; do
  out="$(sw --main "$bad" --yes </dev/null)"; rc=$?
  [ "$rc" != 0 ] && same_as_orig && [ ! -e "$BK" ] && ! log_has "curl"
  check "--main '$bad' is refused before anything is fetched" $? "rc=$rc
$out"
done
out="$(sw --rollback --main 10.9.8.7 </dev/null)"; rc=$?
[ "$rc" != 0 ]
check "--rollback with --main is refused" $? "$out"
out="$(sw --frobnicate </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'неизвестный параметр' <<<"$out"
check "unknown option is refused" $? "$out"

# ------------------------------------------------------------------ 13. CA file: exactly one certificate
new_root cafile
cat "$TMP/ca1.crt" "$TMP/ca2.crt" >"$TMP/two.crt"
out="$(sw --main 10.9.8.7 --ca "$TMP/two.crt" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'блоков PEM в файле 2' <<<"$out" && same_as_orig && [ ! -e "$CA" ] && ! log_has "connect-to"
check "--ca with two certificates is refused (nginx would trust both, only one fingerprint is shown)" $? "rc=$rc
$out"
{ cat "$TMP/ca1.crt"; echo "trailing garbage"; } >"$TMP/garbage.crt"
out="$(sw --main 10.9.8.7 --ca "$TMP/garbage.crt" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'кроме сертификата в файле есть другие данные' <<<"$out" && same_as_orig
check "--ca with trailing garbage is refused" $? "rc=$rc
$out"
{ cat "$TMP/ca1.crt"; openssl ec -in "$TMP/ca1.key" 2>/dev/null; } >"$TMP/withkey.crt"
out="$(sw --main 10.9.8.7 --ca "$TMP/withkey.crt" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'блоков PEM в файле 2' <<<"$out" && same_as_orig
check "--ca with a private key next to the certificate is refused" $? "rc=$rc
$out"
out="$(FAKE_CA_SRC="$TMP/two.crt" sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'https://10.9.8.7/ca.crt: блоков PEM в файле 2' <<<"$out" && same_as_orig && ! grep -q 'Отпечаток' <<<"$out"
check "fetched /ca.crt with two certificates is refused before the fingerprint is shown" $? "rc=$rc
$out"
sed 's/$/\r/' "$TMP/ca1.crt" >"$TMP/crlf.crt"
out="$(sw --main 10.9.8.7 --ca "$TMP/crlf.crt" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(grep -c -- '-----BEGIN' "$CA")" = 1 ] && cmp -s "$CA" <(openssl x509 -in "$TMP/ca1.crt") && ! grep -q $'\r' "$CA"
check "a single certificate (CRLF too) is accepted and written to nginx's file as rewritten by openssl x509" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 14. switch interrupted at docker stop: state and rollback command
new_root stopfail
out="$(FAKE_DOCKER_FAIL="stop:daemon_server" sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'переключение прервано на шаге «docker stop daemon_server»' <<<"$out" \
  && grep -q 'Вернуть LMS: sudo .*switch-front.sh --rollback' <<<"$out" && grep -q 'double.fresh.nsuts: остановлен, --restart=no' <<<"$out" \
  && grep -q 'daemon_server_test: работает, --restart=always' <<<"$out" && grep -q 'перезагружен с новым сайтом: no' <<<"$out" \
  && ! log_has "systemctl reload"
check "switch: docker stop fails -> step, current state and the rollback command are printed, no reload" $? "rc=$rc
$out"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && [ ! -e "$CA" ] && [ "$(state double.fresh.nsuts)" = "always 0 true" ] \
  && [ "$(state daemon_server)" = "always 0 true" ] && [ "$(state daemon_server_test)" = "always 0 true" ]
check "... and --rollback brings everything back" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 15. reload fails after the LMS was stopped
new_root reloadfail
out="$(FAKE_SYSTEMCTL_FAIL=1 sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'прервано на шаге «systemctl reload nginx»' <<<"$out" && grep -q -- '--rollback' <<<"$out" \
  && grep -q 'daemon_server_test: остановлен, --restart=no' <<<"$out"
check "switch: reload fails after the LMS stopped -> step, state and the rollback command" $? "rc=$rc
$out"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && [ "$(state daemon_server_test)" = "always 0 true" ] && log_has "systemctl reload nginx"
check "... and --rollback brings everything back" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 16. rollback: one docker start fails, the rest goes on, a second rollback finishes
new_root startfail
out="$(sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
b="$(find "$BK" -mindepth 1 -maxdepth 1 -type d | head -n1)"
: >"$FAKE_LOG"
out="$(FAKE_DOCKER_FAIL="start:double.fresh.nsuts" sw --rollback </dev/null)"; rc=$?
[ "$rc" != 0 ] && same_as_orig && grep -q 'double.fresh.nsuts: docker start не удался' <<<"$out" \
  && grep -q 'шагов с ошибкой 1' <<<"$out" && [ "$(state double.fresh.nsuts)" = "always 0 false" ] \
  && [ "$(state daemon_server)" = "always 0 true" ] && [ "$(state daemon_server_test)" = "always 0 true" ] \
  && log_has "systemctl reload nginx" && [ -f "$b/complete" ]
check "rollback: a failed docker start is a warning, the other containers and the reload still happen, exit non-zero" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
: >"$FAKE_LOG"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" = 0 ] && same_as_orig && [ "$(state double.fresh.nsuts)" = "always 0 true" ] && log_has "docker start double.fresh.nsuts" \
  && ! log_has "docker start daemon_server" && ! log_has "docker update" && [ -f "$b/rolled-back" ] && grep -q 'файл сайта уже прежний' <<<"$out"
check "second rollback after the partial one: starts only what is still down, finishes, exit 0" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
new_root foreign
out="$(sw --main 10.9.8.7 --yes </dev/null)"; rc=$?
out="$(sw --rollback </dev/null)"
echo "# edited by the LMS owner" >>"$SITE"
out="$(sw --rollback </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'отличается от резервной копии' <<<"$out" && grep -q 'edited by the LMS owner' "$SITE"
check "rollback never overwrites a site file someone changed after the rollback" $? "rc=$rc
$out"

bash -n "$SCRIPT"
check "switch-front.sh parses (bash -n)" $?

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
