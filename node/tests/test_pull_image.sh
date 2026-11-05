#!/usr/bin/env bash
# Offline tests for node/pull-image.sh (the image of the node from the registry of main) and for the --from-main option of
# run-node.sh. docker and curl are stubs that log their calls; /etc/docker/certs.d is a temp directory.
set -uo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
PULL="$NODE/pull-image.sh"
BASH_BIN="$(command -v bash)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi; }

BIN="$TMP/bin"; mkdir -p "$BIN"
cat >"$BIN/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >>"$FAKE_LOG"
case "$1" in
  pull) [ "${FAKE_PULL_FAIL:-0}" = 1 ] && { echo "x509: certificate signed by unknown authority" >&2; exit 1; }; touch "$FAKE_STATE/image"; exit 0 ;;
  image)
    [ -f "$FAKE_STATE/image" ] || exit 1
    case "$*" in
      *RepoDigests*) [ "${FAKE_NO_DIGEST:-0}" = 1 ] || echo "${FAKE_DIGEST_REPO:-10.0.0.5}/nsu-llm-node@sha256:$(printf 'cd%.0s' $(seq 32))" ;;
      *"{{.Id}}"*) echo "sha256:$(printf '1f%.0s' $(seq 32))" ;;
    esac
    exit 0 ;;
  info) exit 0 ;;
  *) exit 0 ;;
esac
EOS
# FAKE_CURL: ok (system trust works), needca (only with --cacert), 403, 404, down, noca (no /ca.crt either)
cat >"$BIN/curl" <<'EOS'
#!/usr/bin/env bash
echo "curl $*" >>"$FAKE_LOG"
out=""; prev=""; cacert=0; cafile=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; [ "$a" = "--cacert" ] && cacert=1; [ "$prev" = "--cacert" ] && cafile="$a"; prev="$a"; done
url="${*: -1}"
case "$url" in
  *"/ca.crt")
    [ "${FAKE_CURL:-ok}" = noca ] && exit 22
    cat "$FAKE_SERVER_CA" >"$out"; exit 0 ;;
  *"/v2/")
    case "${FAKE_CURL:-ok}" in
      ok) printf 200 ;;
      needca) if [ "$cacert" = 1 ] && cmp -s "$cafile" "$FAKE_SERVER_CA"; then printf 200; else printf 000; fi ;;
      403|404) printf '%s' "$FAKE_CURL" ;;
      *) printf 000 ;;
    esac; exit 0 ;;
esac
exit 0
EOS
chmod +x "$BIN"/*

# SEC-2 M2: --digest pulls exactly the manifest main named, never the tag (docker checks the content against the digest)
dg="sha256:$(printf 'ab%.0s' $(seq 32))"
run_digest() { : >"$TMP/dg.log"; rm -f "$TMP/dg-state/image"; mkdir -p "$TMP/dg-state" "$TMP/dg-certs";
  FAKE_LOG="$TMP/dg.log" FAKE_STATE="$TMP/dg-state" FAKE_CURL=ok PULL_IMAGE_CERTS_DIR="$TMP/dg-certs" PATH="$BIN:$PATH" \
    "$BASH_BIN" "$PULL" --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu "$@" 2>&1; }
out="$(run_digest --digest "$dg" --record "$TMP/dg-record")"; rc=$?
[ "$rc" = 0 ] && grep -qx "docker pull 10.0.0.5/nsu-llm-node@$dg" "$TMP/dg.log" && ! grep -q 'docker pull 10.0.0.5/nsu-llm-node:0.2.1-cpu' "$TMP/dg.log" \
  && grep -qx "docker tag 10.0.0.5/nsu-llm-node@$dg nsu-llm-node:0.2.1-cpu" "$TMP/dg.log" && grep -q " $dg\$" "$TMP/dg-record"
check "--digest: pulls <main>/nsu-llm-node@<digest>, tags it, records that digest" $? "rc=$rc $out
$(cat "$TMP/dg.log")"
out="$(run_digest --digest sha256:xyz)"; rc=$?
[ "$rc" != 0 ] && grep -q -- '--digest' <<<"$out" && ! grep -q 'docker pull' "$TMP/dg.log"
check "--digest with a malformed value: refused before any pull" $? "rc=$rc $out"

# --no-tofu: no pinned root, no --ca, system roots fail -> refuse instead of taking /ca.crt unverified
out="$(: >"$TMP/dg.log"; mkdir -p "$TMP/nt-state" "$TMP/nt-certs"; FAKE_LOG="$TMP/dg.log" FAKE_STATE="$TMP/nt-state" FAKE_CURL=needca \
  FAKE_SERVER_CA="$TMP/nt-ca.pem" _x="$(printf -- "-----BEGIN CERTIFICATE-----
X
-----END CERTIFICATE-----
" >"$TMP/nt-ca.pem")" PULL_IMAGE_CERTS_DIR="$TMP/nt-certs" PATH="$BIN:$PATH" \
  "$BASH_BIN" "$PULL" --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --no-tofu 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q -- '--no-tofu' <<<"$out" && ! grep -q '/ca.crt$' "$TMP/dg.log" && ! grep -q 'docker pull' "$TMP/dg.log" \
  && [ -z "$(ls -A "$TMP/nt-certs")" ]
check "--no-tofu: without a pinned root or --ca the root is never fetched from /ca.crt, nothing pulled or pinned" $? "rc=$rc $out
$(cat "$TMP/dg.log")"

new_case() {
  C="$TMP/$1"; mkdir -p "$C/state" "$C/certs"
  export FAKE_LOG="$C/log" FAKE_STATE="$C/state" FAKE_SERVER_CA="$C/server-ca.pem"; : >"$FAKE_LOG"
  printf -- '-----BEGIN CERTIFICATE-----\nMIIBserverCA1\n-----END CERTIFICATE-----\n' >"$FAKE_SERVER_CA"
  unset FAKE_PULL_FAIL
}
pull() { env PATH="$BIN:$PATH" PULL_IMAGE_CERTS_DIR="$C/certs" FAKE_LOG="$FAKE_LOG" FAKE_STATE="$FAKE_STATE" FAKE_SERVER_CA="$FAKE_SERVER_CA" FAKE_CURL="${FAKE_CURL:-ok}" \
  FAKE_PULL_FAIL="${FAKE_PULL_FAIL:-0}" "$BASH_BIN" "$PULL" "$@" 2>&1; }
log_has() { grep -qF -- "$1" "$FAKE_LOG"; }

bash -n "$PULL"; check "bash -n pull-image.sh" $?

# 1. the system trusts the certificate: no CA is installed, the image is pulled, retagged and the remote tag removed
new_case trusted
FAKE_CURL=ok out="$(FAKE_CURL=ok pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull 10.0.0.5/nsu-llm-node:0.2.1-cpu" && log_has "docker tag 10.0.0.5/nsu-llm-node:0.2.1-cpu nsu-llm-node:0.2.1-cpu" \
  && log_has "docker rmi 10.0.0.5/nsu-llm-node:0.2.1-cpu" && [ -z "$(ls -A "$C/certs")" ] && ! log_has "https://10.0.0.5/ca.crt"
check "trusted certificate: pull from the registry of main, local name nsu-llm-node:<ver>-cpu, no CA written" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# 2. an internal CA: the CA of main is fetched once and stored for this registry only
new_case needca
out="$(FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-gpu)"; rc=$?
[ "$rc" = 0 ] && grep -q 'BEGIN CERTIFICATE' "$C/certs/10.0.0.5/ca.crt" && log_has "curl -sSf -k --connect-timeout 5 -m 20 -o" && log_has "https://10.0.0.5/ca.crt" \
  && log_has "docker pull 10.0.0.5/nsu-llm-node:0.2.1-gpu" && grep -q 'certs.d\|certs' <<<"$out"
check "internal CA: the root certificate of main lands in certs.d/<main>/ca.crt (only for this registry), then the pull" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
[ "$(stat -c %a "$C/certs/10.0.0.5/ca.crt" 2>/dev/null || echo 644)" = 644 ]
check "the stored CA is world-readable (dockerd reads it)" $? "$(stat -c %a "$C/certs/10.0.0.5/ca.crt" 2>&1)"
! grep -q 'system\|update-ca-certificates' "$FAKE_LOG"
check "the system trust store is never modified" $? "$(cat "$FAKE_LOG")"

# 2b. the CA is pinned once: a later pull never fetches /ca.crt again; a changed server CA is an error and the pin stays
new_case pin
FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu >/dev/null
pinned="$(cat "$C/certs/10.0.0.5/ca.crt")"
: >"$FAKE_LOG"; rm -f "$FAKE_STATE/image"
out="$(FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
[ "$rc" = 0 ] && ! log_has "https://10.0.0.5/ca.crt" && log_has "docker pull 10.0.0.5/nsu-llm-node:0.2.1-cpu" && [ "$(cat "$C/certs/10.0.0.5/ca.crt")" = "$pinned" ]
check "second pull with the pinned CA: no /ca.crt fetch, the pull works, the pin is untouched" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
: >"$FAKE_LOG"; rm -f "$FAKE_STATE/image"
printf -- '-----BEGIN CERTIFICATE-----\nMIIBattackerCA\n-----END CERTIFICATE-----\n' >"$FAKE_SERVER_CA"
out="$(FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
[ "$rc" != 0 ] && ! log_has "docker pull" && ! log_has "https://10.0.0.5/ca.crt" && [ "$(cat "$C/certs/10.0.0.5/ca.crt")" = "$pinned" ] \
  && grep -q 'не совпал' <<<"$out" && grep -q 'удалите' <<<"$out" && grep -q -- '-ImageTar' <<<"$out"
check "the server CA changed: the pull FAILS in Russian, /ca.crt is not fetched again, the saved CA is not overwritten" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
# --ca FILE (the CA run-node pinned in state/ca.crt): used instead of fetching, then pinned for Docker
new_case ca-arg
cp "$FAKE_SERVER_CA" "$C/node-ca.crt"
out="$(FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --ca "$C/node-ca.crt")"; rc=$?
[ "$rc" = 0 ] && ! log_has "https://10.0.0.5/ca.crt" && cmp -s "$C/node-ca.crt" "$C/certs/10.0.0.5/ca.crt" && log_has "docker pull"
check "--ca FILE: the CA the node pinned is used and pinned for Docker, /ca.crt is not fetched" $? "rc=$rc $out"
new_case ca-arg-bad
printf -- '-----BEGIN CERTIFICATE-----\nMIIBsomeoneelse\n-----END CERTIFICATE-----\n' >"$C/node-ca.crt"
out="$(FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --ca "$C/node-ca.crt")"; rc=$?
[ "$rc" != 0 ] && ! log_has "docker pull" && ! log_has "https://10.0.0.5/ca.crt" && [ -z "$(ls -A "$C/certs")" ]
check "--ca FILE the registry does not match: error, nothing fetched, nothing pinned, nothing pulled" $? "rc=$rc $out"
new_case ca-diff
FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu >/dev/null
printf -- '-----BEGIN CERTIFICATE-----\nMIIBsomeoneelse\n-----END CERTIFICATE-----\n' >"$C/node-ca.crt"; : >"$FAKE_LOG"
out="$(FAKE_CURL=needca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --ca "$C/node-ca.crt")"; rc=$?
[ "$rc" != 0 ] && ! log_has "docker pull"
check "a node CA that differs from the pinned Docker CA is refused" $? "rc=$rc $out"

# 3. registry answers
for code in 403 404; do
  new_case "code$code"
  out="$(FAKE_CURL=$code pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
  [ "$rc" != 0 ] && ! log_has "docker pull"
  check "registry answers $code: error, nothing pulled" $? "rc=$rc $out"
done
grep -q 'REGISTRY_ALLOW_FROM' <<<"$(FAKE_CURL=403 pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"
check "403 is explained with REGISTRY_ALLOW_FROM" $?
grep -q 'нет реестра' <<<"$(FAKE_CURL=404 pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"
check "404 says that main has no registry (older main)" $?
new_case down
out="$(FAKE_CURL=down pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
[ "$rc" != 0 ] && grep -q 'недоступен' <<<"$out" && grep -q -- '-ImageTar' <<<"$out" && ! log_has "docker pull" && [ -z "$(ls -A "$C/certs")" ]
check "main unreachable: Russian error pointing to the archive fallback, no CA stored, nothing pulled" $? "rc=$rc $out"
new_case noca
out="$(FAKE_CURL=noca pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
[ "$rc" != 0 ] && ! log_has "docker pull"
check "no /ca.crt and no trusted certificate: error" $? "rc=$rc $out"
new_case failpull
out="$(FAKE_CURL=ok FAKE_PULL_FAIL=1 pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"; rc=$?
[ "$rc" != 0 ] && grep -q 'docker pull 10.0.0.5/nsu-llm-node:0.2.1-cpu не удался' <<<"$out" && ! log_has "docker tag"
check "a failed docker pull: Russian error, no retag" $? "rc=$rc $out"

# 3b. --record FILE (task 2-30): the digest of the registry of main and the id of the local image, for run-node and the panel
new_case record
REC="$C/state/data/image.digest"
out="$(FAKE_CURL=ok pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-gpu --record "$REC")"; rc=$?
want="sha256:$(printf '1f%.0s' $(seq 32)) sha256:$(printf 'cd%.0s' $(seq 32))"
[ "$rc" = 0 ] && [ "$(cat "$REC" 2>/dev/null)" = "$want" ] && grep -q 'digest образа в реестре главного: sha256:' <<<"$out"
check "--record: '<image id> <registry digest>' lands in the file (its directory is created)" $? "rc=$rc rec=$(cat "$REC" 2>&1)
$out"
# the digest is read before the remote tag is removed
[ "$(grep -n 'docker image inspect' "$FAKE_LOG" | head -n1 | cut -d: -f1)" -lt "$(grep -n 'docker rmi' "$FAKE_LOG" | head -n1 | cut -d: -f1)" ]
check "--record: the digest is taken before docker rmi drops the remote name" $? "$(cat "$FAKE_LOG")"
new_case record-none
REC="$C/state/image.digest"; echo "stale" >"$REC"
out="$(FAKE_NO_DIGEST=1 FAKE_CURL=ok pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --record "$REC")"; rc=$?
[ "$rc" = 0 ] && [ ! -e "$REC" ] && grep -q 'digest образа не определён' <<<"$out"
check "--record: no digest to be found: the pull succeeds, the stale record is removed, a warning is printed" $? "rc=$rc $out"
new_case record-off
out="$(FAKE_CURL=ok pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu)"
! grep -q -- "--format" "$FAKE_LOG" && [ ! -e "$C/state/image.digest" ]
check "without --record the registry digest is not even asked for and nothing is written" $? "$(cat "$FAKE_LOG")"
new_case record-dry
out="$(pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --record "$C/state/r" --dry-run)"; rc=$?
[ "$rc" = 0 ] && grep -q "записывается в $C/state/r" <<<"$out" && [ ! -e "$C/state/r" ]
check "--dry-run with --record says where the digest would go and writes nothing" $? "rc=$rc $out"

# 4. dry run and argument checks
new_case dry
out="$(pull --main 10.0.0.5 --image nsu-llm-node:0.2.1-cpu --dry-run)"; rc=$?
[ "$rc" = 0 ] && grep -q 'docker pull 10.0.0.5/nsu-llm-node:0.2.1-cpu' <<<"$out" && [ ! -s "$FAKE_LOG" ]
check "--dry-run prints the pull, the tag and the removal and runs nothing" $? "rc=$rc $out"
for bad in "--image nsu-llm-node:0.2.1-cpu" "--main 10.0.0.5" "--main 10.0.0.5;id --image nsu-llm-node:0.2.1-cpu" "--main 10.0.0.5 --image evil:1" "--main 10.0.0.5 --image nsu-llm-node:1;rm"; do
  out="$(pull $bad)"; rc=$?
  [ "$rc" != 0 ]
  check "bad arguments are refused: $bad" $? "$out"
done

# 5. run-node.sh --from-main
RN="$NODE/run-node.sh"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu --from-main 2>&1)"
pl="$(grep -n 'pull-image.sh --main 192.168.3.21 --image nsu-llm-node:0.2.1-cpu' <<<"$out" | head -n1 | cut -d: -f1)"
sl="$(grep -n 'docker save --platform linux/amd64 nsu-llm-node:0.2.1-cpu' <<<"$out" | head -n1 | cut -d: -f1)"
[ -n "$pl" ] && [ -n "$sl" ] && [ "$pl" -lt "$sl" ]
check "run-node.sh --from-main: pull-image.sh runs before docker save and docker run" $? "$out"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel nvidia --from-main 2>&1)"
grep -q 'pull-image.sh --main 192.168.3.21 --image nsu-llm-node:0.2.1-gpu' <<<"$out"
check "run-node.sh --from-main with a GPU takes the -gpu image" $? "$out"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu --image my/own:1 --from-main 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q -- '--from-main' <<<"$out"
check "run-node.sh --from-main with a foreign --image is refused" $? "rc=$rc $out"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu 2>&1)"
! grep -q 'pull-image' <<<"$out"
check "run-node.sh without --from-main never pulls (the archive path is unchanged)" $? "$out"

echo
if [ "$FAILED" = 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED"; exit 1; fi
