#!/usr/bin/env bash
# Removes the node images of older versions from the containerd store of the k3s INSIDE this node container. k3s imports
# agent/images/node.tar at every start and pulls pods' images from the registry of main, so after an update the store keeps the old
# image next to the new one (kubelet's own image GC starts at 85 % of the disk, which inside WSL2 never happens: df sees the ext4.vhdx
# at its maximum size). Containerd frees layers nothing refers to.
#   prune-node-images.sh            wait PRUNE_IMAGES_DELAY_S (default 300: the pods of the new version have been pulled), then a pass;
#                                   a pass that had to keep an image because a container still uses it is repeated (3 passes at most)
#   prune-node-images.sh --now      one pass at once
# Kept: (1) the refs of nsu-llm-node whose digest is that of this container's image (NODE_IMAGE, imported from node.tar;
# NODE_IMAGE_DIGEST, the registry's digest, may be empty); (2) every ref a container of containerd uses (`ctr containers ls`, running or
# stopped, and any other ref of the same digest): pods are pinned to the registry digest of main, which an image imported from
# node.tar does not carry, so (1) alone could delete the image the pods run on. With no ref of the own image in the store, or when
# the containers cannot be listed, nothing is removed. Other repositories (pause, coredns, ...) are never touched.
# PRUNE_IMAGES=0 switches it off. PRUNE_IMAGES_CTR replaces `k3s ctr -n k8s.io` (tests).
set -uo pipefail

[ "${PRUNE_IMAGES:-1}" != 0 ] || exit 0
DELAY="${PRUNE_IMAGES_DELAY_S:-300}"
[[ "$DELAY" =~ ^[0-9]+$ ]] || { echo "prune-node-images: PRUNE_IMAGES_DELAY_S must be a whole number" >&2; exit 2; }
IFS=' ' read -r -a CTR <<<"${PRUNE_IMAGES_CTR:-k3s ctr -n k8s.io}"
log() { echo "$(date -u +%FT%TZ) prune-node-images: $*"; }

# one pass; prints nothing, returns 10 when an image was kept only because a container uses it
pass() {
  local rows containers used_refs want_tag name digest keep_digests used_digests found=0 removed=0 busy=0 u
  mapfile -t rows < <("${CTR[@]}" images ls 2>/dev/null | awk 'NR > 1 {print $1, $3}')
  containers="$("${CTR[@]}" containers ls 2>/dev/null)" || { log "cannot list the containers, nothing removed"; return 0; }
  mapfile -t used_refs < <(printf '%s\n' "$containers" | awk 'NR > 1 {print $2}')
  want_tag="${NODE_IMAGE:+docker.io/library/$NODE_IMAGE}"
  keep_digests=" ${NODE_IMAGE_DIGEST:-} "
  used_digests=" "
  for r in "${rows[@]}"; do
    read -r name digest <<<"$r"
    [ -z "$want_tag" ] || [ "$name" != "$want_tag" ] || keep_digests+="$digest "
    for u in "${used_refs[@]}"; do [ "$name" != "$u" ] || used_digests+="$digest "; done
  done
  for r in "${rows[@]}"; do
    read -r name digest <<<"$r"
    [[ "$name" =~ (^|/)nsu-llm-node([:@]|$) ]] || continue
    case "$keep_digests" in *" $digest "*) found=1 ;; esac
  done
  [ "$found" = 1 ] || { log "no ref of this container's image in the store, nothing removed"; return 0; }
  for r in "${rows[@]}"; do
    read -r name digest <<<"$r"
    [[ "$name" =~ (^|/)nsu-llm-node([:@]|$) ]] || continue
    case "$keep_digests" in *" $digest "*) continue ;; esac
    case "$used_digests" in *" $digest "*) log "kept $name: a container uses it"; busy=1; continue ;; esac
    if "${CTR[@]}" images rm "$name" >/dev/null 2>&1; then log "removed $name ($digest)"; removed=$((removed + 1)); else log "could not remove $name"; fi
  done
  log "pass done: $removed old image refs removed"
  [ "$busy" = 0 ] || return 10
  return 0
}

[ "${1:-}" = "--now" ] || sleep "$DELAY"
end=$((SECONDS + 600))
until "${CTR[@]}" images ls -q >/dev/null 2>&1; do
  [ "$SECONDS" -lt "$end" ] || { log "containerd does not answer, nothing removed"; exit 0; }
  sleep 5
done
for attempt in 1 2 3; do
  pass; rc=$?
  [ "$rc" = 10 ] && [ "${1:-}" != "--now" ] && [ "$attempt" -lt 3 ] || break
  sleep "$DELAY"
done
exit 0
