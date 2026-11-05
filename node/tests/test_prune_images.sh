#!/usr/bin/env bash
# Offline tests of prune-old-images.sh (the Docker of the machine) and prune-node-images.sh (containerd inside the node
# container): old node images go after an update, the kept one and every other repository stay. Fake docker and ctr, no network.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
HOSTPRUNE="$NODE/prune-old-images.sh"
INNER="$NODE/rootfs/opt/llm/bin/prune-node-images.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing '$3' in: $2)" ;; esac; }
lacks() { case "$2" in *"$3"*) fail "$1 (found '$3' in: $2)" ;; *) pass "$1" ;; esac; }

bash -n "$HOSTPRUNE" && bash -n "$INNER" && pass "bash -n both scripts" || fail "bash -n both scripts"

# ---- host side: fake docker over a plain list of images (repository tag id)
mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'FAKE'
#!/usr/bin/env bash
if [ "$1 $2" = "image inspect" ]; then
  ref="${@: -1}"
  id="$(awk -v r="$ref" '$1":"$2 == r {print "sha256:" $3; exit}' "$DOCKER_IMAGES")"
  [ -n "$id" ] && echo "$id" && exit 0
  exit 1
fi
case "$1" in
  images) cat "$DOCKER_IMAGES" ;;
  rmi) echo "rmi $2" >> "$DOCKER_LOG"; case " $DOCKER_BUSY " in *" $2 "*) exit 1 ;; esac ;;
esac
FAKE
chmod +x "$TMP/bin/docker"
cat > "$TMP/images" <<'LIST'
nsu-llm-node 0.2.1-gpu aaa111
nsu-llm-node 0.2.0-gpu bbb222
nsu-llm-node 0.2.0-cpu ccc333
10.0.0.5/nsu-llm-node <none> ddd444
nsu-llm-node 0.2.1-cpu aaa111
ubuntu 24.04 eee555
other nsu-llm-node-2 fff666
LIST
run_host() { PATH="$TMP/bin:$PATH" DOCKER_IMAGES="$TMP/images" DOCKER_LOG="$TMP/docker.log" DOCKER_BUSY="${BUSY:-}" bash "$HOSTPRUNE" "$@" 2>&1; }
: > "$TMP/docker.log"
out="$(run_host --keep nsu-llm-node:0.2.1-gpu)"; rc=$?
[ "$rc" = 0 ] && pass "host: exit 0" || fail "host: exit 0 (rc $rc)"
log="$(cat "$TMP/docker.log")"
contains "host: old gpu tag removed" "$log" "rmi nsu-llm-node:0.2.0-gpu"
contains "host: old cpu tag removed" "$log" "rmi nsu-llm-node:0.2.0-cpu"
contains "host: untagged leftover removed by id" "$log" "rmi ddd444"
lacks "host: the kept image is not removed" "$log" "aaa111"
lacks "host: another tag of the kept image is not removed" "$log" "0.2.1-cpu"
lacks "host: ubuntu stays" "$log" "ubuntu"
lacks "host: another repository stays" "$log" "other"
: > "$TMP/docker.log"; out="$(run_host --keep nsu-llm-node:0.2.1-gpu --dry-run)"
contains "host: --dry-run lists" "$out" "would remove nsu-llm-node:0.2.0-gpu"
[ ! -s "$TMP/docker.log" ] && pass "host: --dry-run removes nothing" || fail "host: --dry-run removes nothing"
out="$(BUSY="nsu-llm-node:0.2.0-gpu" run_host --keep nsu-llm-node:0.2.1-gpu)"; rc=$?
[ "$rc" = 0 ] && contains "host: an image in use is reported, not fatal" "$out" "kept nsu-llm-node:0.2.0-gpu" || fail "host: an image in use is not fatal (rc $rc)"
out="$(run_host --keep nsu-llm-node:9.9.9-gpu)"; rc=$?
[ "$rc" = 1 ] && pass "host: a missing kept image removes nothing (exit 1)" || fail "host: a missing kept image removes nothing (rc $rc)"
run_host --keep "ubuntu:24.04" >/dev/null; [ $? = 2 ] && pass "host: --keep must be a node image" || fail "host: --keep must be a node image"

# ---- inside the container: fake `ctr images ls` / `ctr images rm`
cat > "$TMP/ctr" <<'FAKE'
#!/usr/bin/env bash
if [ "$1 $2" = "images ls" ]; then
  [ "${3:-}" = -q ] && exit 0
  cat "$CTR_IMAGES"
elif [ "$1 $2" = "containers ls" ]; then
  [ -n "${CTR_NOCONT:-}" ] && exit 1
  cat "$CTR_CONTAINERS"
elif [ "$1 $2" = "images rm" ]; then
  echo "rm $3" >> "$CTR_LOG"
fi
FAKE
chmod +x "$TMP/ctr"
cat > "$TMP/ctr.images" <<'LIST'
REF TYPE DIGEST SIZE PLATFORMS LABELS
docker.io/library/nsu-llm-node:0.2.1-gpu application/vnd.oci.image.index.v1+json sha256:new 1.2GiB linux/amd64 -
docker.io/library/nsu-llm-node:0.2.0-gpu application/vnd.oci.image.index.v1+json sha256:old 3.5GiB linux/amd64 -
10.0.0.5/nsu-llm-node@sha256:old application/vnd.oci.image.index.v1+json sha256:old 3.5GiB linux/amd64 -
10.0.0.5/nsu-llm-node@sha256:newreg application/vnd.oci.image.index.v1+json sha256:newreg 1.2GiB linux/amd64 -
docker.io/rancher/mirrored-pause:3.6 application/vnd.oci.image.index.v1+json sha256:pause 300KiB linux/amd64 -
LIST
printf 'CONTAINER IMAGE RUNTIME
abc docker.io/rancher/mirrored-pause:3.6 io.containerd.runc.v2
' > "$TMP/ctr.containers"
run_inner() { CTR_CONTAINERS="${CONT:-$TMP/ctr.containers}" CTR_IMAGES="$TMP/ctr.images" CTR_LOG="$TMP/ctr.log" PRUNE_IMAGES_CTR="$TMP/ctr" PRUNE_IMAGES_DELAY_S=0 bash "$INNER" --now 2>&1; }
: > "$TMP/ctr.log"
out="$(NODE_IMAGE=nsu-llm-node:0.2.1-gpu NODE_IMAGE_DIGEST=sha256:newreg run_inner)"; rc=$?
log="$(cat "$TMP/ctr.log")"
[ "$rc" = 0 ] && pass "inner: exit 0" || fail "inner: exit 0 (rc $rc)"
contains "inner: old tag removed" "$log" "rm docker.io/library/nsu-llm-node:0.2.0-gpu"
contains "inner: old digest ref removed" "$log" "rm 10.0.0.5/nsu-llm-node@sha256:old"
lacks "inner: the imported new tag stays" "$log" "0.2.1-gpu"
lacks "inner: the new registry ref stays" "$log" "newreg"
lacks "inner: pause stays" "$log" "pause"
: > "$TMP/ctr.log"; NODE_IMAGE=nsu-llm-node:0.3.0-gpu NODE_IMAGE_DIGEST=sha256:nothing run_inner >/dev/null
[ ! -s "$TMP/ctr.log" ] && pass "inner: without a ref of the own image nothing is removed" || fail "inner: without a ref of the own image nothing is removed"
: > "$TMP/ctr.log"; NODE_IMAGE=nsu-llm-node:0.2.1-gpu PRUNE_IMAGES=0 run_inner >/dev/null
[ ! -s "$TMP/ctr.log" ] && pass "inner: PRUNE_IMAGES=0 switches it off" || fail "inner: PRUNE_IMAGES=0 switches it off"
: > "$TMP/ctr.log"; NODE_IMAGE=nsu-llm-node:0.2.1-gpu run_inner >/dev/null
log="$(cat "$TMP/ctr.log")"
contains "inner: the tag alone is enough to know the own image" "$log" "rm 10.0.0.5/nsu-llm-node@sha256:old"
lacks "inner: ... and the imported tag stays" "$log" "0.2.1-gpu"

# a container still runs on the OLD image through its registry-pinned ref, NODE_IMAGE_DIGEST is empty (docker load, no record)
printf 'CONTAINER IMAGE RUNTIME
abc 10.0.0.5/nsu-llm-node@sha256:old io.containerd.runc.v2
def docker.io/rancher/mirrored-pause:3.6 io.containerd.runc.v2
' > "$TMP/ctr.busy"
: > "$TMP/ctr.log"; out="$(CONT="$TMP/ctr.busy" NODE_IMAGE=nsu-llm-node:0.2.1-gpu run_inner)"
log="$(cat "$TMP/ctr.log")"
lacks "inner: a ref a container uses stays (digest unknown)" "$log" "@sha256:old"
lacks "inner: ... and so does the other ref of the same digest" "$log" "0.2.0-gpu"
contains "inner: ... and says why" "$out" "kept 10.0.0.5/nsu-llm-node@sha256:old: a container uses it"
: > "$TMP/ctr.log"; CONT="$TMP/ctr.busy" NODE_IMAGE=nsu-llm-node:0.2.1-gpu NODE_IMAGE_DIGEST=sha256:newreg run_inner >/dev/null
lacks "inner: a used ref stays with the digest known too" "$(cat "$TMP/ctr.log")" "@sha256:old"
: > "$TMP/ctr.log"; CTR_NOCONT=1 NODE_IMAGE=nsu-llm-node:0.2.1-gpu run_inner >/dev/null
[ ! -s "$TMP/ctr.log" ] && pass "inner: containers not listable, nothing is removed" || fail "inner: containers not listable, nothing is removed"

# ---- the calls exist (without them nothing is ever pruned: the scripts above would be dead code)
grep -q '^    \[ "${PRUNE_IMAGES:-1}" = 0 \] || prune-node-images.sh &$' "$NODE/rootfs/opt/llm/bin/entrypoint.sh" && pass "entrypoint.sh starts prune-node-images.sh in the k3s branch" || fail "entrypoint.sh starts prune-node-images.sh in the k3s branch"
k3s_part="$(awk '/^ *k3s\)/{on=1} /^ *standalone\)/{on=0} on' "$NODE/rootfs/opt/llm/bin/entrypoint.sh")"
case "$k3s_part" in *"|| prune-node-images.sh &"*) pass "... and it is inside the k3s case" ;; *) fail "... and it is inside the k3s case" ;; esac
grep -q 'bash "$HERE/prune-old-images.sh" --keep "$IMAGE"' "$NODE/run-node.sh" && pass "run-node.sh prunes older images after the container is started" || fail "run-node.sh prunes older images after the container is started"
[ "$(grep -n 'prune-old-images.sh' "$NODE/run-node.sh" | head -1 | cut -d: -f1)" -gt "$(grep -n '^run "${args\[@\]}"$' "$NODE/run-node.sh" | head -1 | cut -d: -f1)" ]   && pass "... after docker run, not before" || fail "... after docker run, not before"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
