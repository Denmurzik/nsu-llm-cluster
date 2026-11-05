#!/usr/bin/env bash
# Removes the node images of OLDER versions from the Docker of this machine (the host or the WSL distro of a class PC), after
# an update: pull-image.sh only adds the new image, so every update would keep the old one (up to 3 GB on a PC with a 16 GB limit).
#   prune-old-images.sh --keep nsu-llm-node:0.2.1-gpu [--dry-run]
# Every local image of the repository nsu-llm-node (also <main>/nsu-llm-node and untagged leftovers of a re-pulled tag) whose id is
# not the id of --keep goes. An image of a running container is not removed (docker refuses). Other repositories are never touched.
# Always exits 0 when the kept image exists: a failed removal costs disk, not the node. The inside of the container (the images of
# k3s) is pruned by prune-node-images.sh.
set -uo pipefail

KEEP=""
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "prune-old-images: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$KEEP" =~ ^nsu-llm-node:[A-Za-z0-9._-]+$ ]] || { echo "prune-old-images: --keep needs an image like nsu-llm-node:0.2.1-gpu (got '$KEEP')" >&2; exit 2; }

keep_id="$(docker image inspect -f '{{.Id}}' "$KEEP" 2>/dev/null)" || { echo "prune-old-images: $KEEP is not on this machine, nothing is removed" >&2; exit 1; }
removed=0
while read -r repo tag id; do
  [[ "$repo" =~ (^|/)nsu-llm-node$ ]] || continue
  [ "${id#sha256:}" != "${keep_id#sha256:}" ] || continue
  # the id with a tag is removed by name (other tags of the same image stay if they exist), an untagged one by id
  ref="$repo:$tag"; [ "$tag" != "<none>" ] || ref="$id"
  if [ "$DRY" = 1 ]; then echo "would remove $ref"; continue; fi
  if docker rmi "$ref" >/dev/null 2>&1; then echo "removed old image $ref"; removed=$((removed + 1)); else echo "kept $ref (in use or not removable)"; fi
done < <(docker images --no-trunc --format '{{.Repository}} {{.Tag}} {{.ID}}' 2>/dev/null)
echo "prune-old-images: $removed removed, kept $KEEP"
exit 0
