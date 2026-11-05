#!/usr/bin/env bash
# Signature of main's root certificate by the k3s server CA (main/sign-ca.sh) and its check on a node
# (node/rootfs/opt/llm/bin/fetch-main-ca.sh), with openssl and a curl stub that serves files: no network, no docker.
# A k3s-like CA (ECDSA P-256, as k3s makes it), a Caddy-like root, and the attacks: another CA in /cacerts, a tampered
# /ca.crt, a /ca.crt signed by a certificate the k3s CA issued (a node's own serving cert), a raw token, an old main.
set -u
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SIGN="$REPO/main/sign-ca.sh"
FETCH="$REPO/node/rootfs/opt/llm/bin/fetch-main-ca.sh"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
FAILED=0
ok() { echo "ok   - $1"; }
bad() { echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; FAILED=1; }
export MSYS2_ARG_CONV_EXCL="/CN="   # Git Bash: keep -subj as is, convert the file paths

mkca() { # mkca DIR CN: self-signed CA like k3s's server-ca (EC P-256)
  mkdir -p "$1"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$1/server-ca.key" -out "$1/server-ca.crt" \
    -days 3650 -subj "/CN=$2" -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    -addext "basicConstraints=critical,CA:TRUE" >/dev/null 2>&1
}
mkca "$W/k3s" "k3s-server-ca@1700000000"
mkca "$W/evil" "k3s-server-ca@1700000000"
mkca "$W/caddy" "Caddy Local Authority - 2026 ECC Root"
cp "$W/caddy/server-ca.crt" "$W/root.crt"
# a leaf the k3s CA issued (a node's kubelet serving certificate: the node holds its key)
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$W/leaf.key" -out "$W/leaf.csr" -subj "/CN=evil-node" >/dev/null 2>&1
openssl x509 -req -in "$W/leaf.csr" -CA "$W/k3s/server-ca.crt" -CAkey "$W/k3s/server-ca.key" -CAcreateserial -out "$W/leaf.crt" -days 30 >/dev/null 2>&1

# 1. sign-ca.sh: waits for both files, signs once, re-signs on change
out="$(bash "$SIGN" "$W/root.crt" "$W/sig" "$W/none" 2>&1)"; rc=$?
[ "$rc" = 0 ] && [ ! -e "$W/sig/ca.crt.p7s" ] && ok "sign-ca.sh without the k3s CA yet: nothing, exit 0" || bad "sign-ca.sh without k3s CA" "rc=$rc $out"
bash "$SIGN" "$W/root.crt" "$W/sig" "$W/k3s" >/dev/null 2>&1
[ -s "$W/sig/ca.crt.p7s" ] && ok "sign-ca.sh writes ca.crt.p7s" || bad "sign-ca.sh writes ca.crt.p7s"
openssl cms -verify -binary -inform DER -in "$W/sig/ca.crt.p7s" -content "$W/root.crt" -CAfile "$W/k3s/server-ca.crt" -purpose any -out /dev/null >/dev/null 2>&1
[ $? = 0 ] && ok "the signature verifies against the k3s CA" || bad "the signature verifies against the k3s CA"
m1="$(cksum <"$W/sig/ca.crt.p7s")"
out="$(bash "$SIGN" "$W/root.crt" "$W/sig" "$W/k3s" 2>&1)"
[ -z "$out" ] && [ "$(cksum <"$W/sig/ca.crt.p7s")" = "$m1" ] && ok "unchanged inputs: not re-signed" || bad "unchanged inputs: not re-signed" "$out"
cp "$W/root.crt" "$W/root.orig"; echo >>"$W/root.crt"
bash "$SIGN" "$W/root.crt" "$W/sig" "$W/k3s" >/dev/null 2>&1
openssl cms -verify -binary -inform DER -in "$W/sig/ca.crt.p7s" -content "$W/root.crt" -CAfile "$W/k3s/server-ca.crt" -purpose any -out /dev/null >/dev/null 2>&1
[ $? = 0 ] && ok "changed /ca.crt: re-signed" || bad "changed /ca.crt: re-signed"
cp "$W/root.orig" "$W/root.crt"; bash "$SIGN" "$W/root.crt" "$W/sig" "$W/k3s" >/dev/null 2>&1

# 2. fetch-main-ca.sh with a curl stub: URL -> file in $SRV (missing file: 404)
mkdir -p "$W/bin" "$W/srv"
cat >"$W/bin/curl" <<'EOF'
#!/usr/bin/env bash
out="" url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -w|-m|--connect-timeout) shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac; done
f="$SRV/$(printf '%s' "$url" | sed 's#^https://##; s#/#_#g')"
if [ -f "$f" ]; then cp "$f" "$out"; printf 200; else : >"$out"; printf 404; fi
EOF
chmod +x "$W/bin/curl"
serve() { # serve CACERTS CA SIG: files of the main at 10.0.0.10
  rm -f "$W/srv/"*
  [ -z "$1" ] || cp "$1" "$W/srv/10.0.0.10:6443_cacerts"
  [ -z "$2" ] || cp "$2" "$W/srv/10.0.0.10_ca.crt"
  [ -z "$3" ] || cp "$3" "$W/srv/10.0.0.10_ca.crt.p7s"
}
H="$(sha256sum <"$W/k3s/server-ca.crt" | cut -c1-64)"
TOKEN="K10$H::node:0123456789abcdef"
run_fetch() { rm -f "$W/out.crt"; env PATH="$W/bin:$PATH" SRV="$W/srv" "$@" bash "$FETCH" --main 10.0.0.10 --out "$W/out.crt" 2>&1; echo "rc=$?"; }

serve "$W/k3s/server-ca.crt" "$W/root.crt" "$W/sig/ca.crt.p7s"
out="$(run_fetch K3S_TOKEN="$TOKEN")"
grep -q '^rc=0$' <<<"$out" && cmp -s "$W/out.crt" "$W/root.crt" && ok "genuine main: CA verified by the token and saved" || bad "genuine main" "$out"
printf '%s\n' "$TOKEN" >"$W/token"; chmod 600 "$W/token"
out="$(rm -f "$W/out.crt"; env PATH="$W/bin:$PATH" SRV="$W/srv" bash "$FETCH" --main 10.0.0.10 --out "$W/out.crt" --token-file "$W/token" 2>&1; echo "rc=$?")"
grep -q '^rc=0$' <<<"$out" && ok "token from --token-file" || bad "token from --token-file" "$out"

serve "$W/evil/server-ca.crt" "$W/root.crt" "$W/sig/ca.crt.p7s"
out="$(run_fetch K3S_TOKEN="$TOKEN")"
grep -q '^rc=1$' <<<"$out" && [ ! -e "$W/out.crt" ] && grep -q 'хешем в токене' <<<"$out" && ok "another k3s CA (MITM on 6443): refused" || bad "another k3s CA refused" "$out"

cp "$W/root.crt" "$W/tampered.crt"; cat "$W/evil/server-ca.crt" >"$W/tampered.crt"
serve "$W/k3s/server-ca.crt" "$W/tampered.crt" "$W/sig/ca.crt.p7s"
out="$(run_fetch K3S_TOKEN="$TOKEN")"
grep -q '^rc=1$' <<<"$out" && [ ! -e "$W/out.crt" ] && ok "substituted /ca.crt with the genuine signature: refused" || bad "substituted /ca.crt refused" "$out"

cp "$W/evil/server-ca.crt" "$W/fake-root.crt"
openssl cms -sign -binary -nosmimecap -md sha256 -outform DER -in "$W/fake-root.crt" -signer "$W/leaf.crt" -inkey "$W/leaf.key" \
  -certfile "$W/k3s/server-ca.crt" -out "$W/leaf.p7s" >/dev/null 2>&1
serve "$W/k3s/server-ca.crt" "$W/fake-root.crt" "$W/leaf.p7s"
out="$(run_fetch K3S_TOKEN="$TOKEN")"
grep -q '^rc=1$' <<<"$out" && [ ! -e "$W/out.crt" ] && grep -q 'другим сертификатом' <<<"$out" \
  && ok "/ca.crt signed by a certificate the k3s CA issued to a node: refused" || bad "leaf-signed /ca.crt refused" "$out"

serve "$W/k3s/server-ca.crt" "$W/root.crt" ""
out="$(run_fetch K3S_TOKEN="$TOKEN")"
grep -q '^rc=1$' <<<"$out" && [ ! -e "$W/out.crt" ] && grep -q 'ca.crt.p7s нет' <<<"$out" && ok "K10 token, no signature (old main, or a MITM answering 404 on .p7s): exit 1, nothing saved" || bad "K10 + no p7s" "$out"
for flag in --yes --trust-first-use; do
  out="$(rm -f "$W/out.crt"; env PATH="$W/bin:$PATH" SRV="$W/srv" K3S_TOKEN="$TOKEN" bash "$FETCH" --main 10.0.0.10 --out "$W/out.crt" $flag 2>&1; echo "rc=$?")"
  grep -q '^rc=1$' <<<"$out" && [ ! -e "$W/out.crt" ] && ok "K10 token, no signature, $flag: still exit 1 (no downgrade to TOFU)" || bad "K10 + no p7s + $flag" "$out"
done

serve "$W/k3s/server-ca.crt" "$W/root.crt" "$W/sig/ca.crt.p7s"
out="$(run_fetch K3S_TOKEN=0123456789abcdef)"
grep -q '^rc=3$' <<<"$out" && [ ! -e "$W/out.crt" ] && grep -q 'Отпечаток SHA-256' <<<"$out" && ok "raw token: TOFU needs --trust-first-use (exit 3, fingerprint printed)" || bad "raw token TOFU" "$out"
out="$(rm -f "$W/out.crt"; env PATH="$W/bin:$PATH" SRV="$W/srv" K3S_TOKEN=0123456789abcdef bash "$FETCH" --main 10.0.0.10 --out "$W/out.crt" --yes 2>&1; echo "rc=$?")"
grep -q '^rc=3$' <<<"$out" && [ ! -e "$W/out.crt" ] && ok "raw token with the old --yes: not trusted (exit 3)" || bad "raw token --yes must not trust" "$out"
out="$(rm -f "$W/out.crt"; env PATH="$W/bin:$PATH" SRV="$W/srv" K3S_TOKEN=0123456789abcdef bash "$FETCH" --main 10.0.0.10 --out "$W/out.crt" --trust-first-use 2>&1; echo "rc=$?")"
grep -q '^rc=0$' <<<"$out" && cmp -s "$W/out.crt" "$W/root.crt" && ok "raw token with --trust-first-use: saved with the fingerprint shown" || bad "raw token --trust-first-use" "$out"

serve "" "" ""
out="$(run_fetch K3S_TOKEN="$TOKEN")"
grep -q '^rc=4$' <<<"$out" && ok "main without /ca.crt (public certificate): exit 4" || bad "no /ca.crt: exit 4" "$out"

if [ "$FAILED" = 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED"; exit 1; fi
