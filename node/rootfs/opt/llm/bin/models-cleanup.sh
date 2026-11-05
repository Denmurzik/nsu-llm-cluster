#!/usr/bin/env bash
# Removes model weights from the models volume of a class node ($LLAMA_CACHE, the volume `llm-models`): the cleanup of
# task 2-26. The portal decides what a node needs and passes it as arguments; everything else goes.
#   models-cleanup.sh [--dry-run] [--keep REPO:FILE]... [--keep-rpc] [--keep-vllm]
#   models-cleanup.sh --usage         print the size of the volume in bytes and exit
# --keep REPO:FILE  a file the node's deployment uses (the model, its draft model, the projector), e.g.
#                   unsloth/gemma-4-E4B-it-GGUF:gemma-4-E4B-it-Q4_K_M.gguf or local/my-model:model.gguf. Of a multi-file GGUF
#                   (`-00001-of-0000N.gguf`) every shard stays; so do the sidecar files of a kept file (.etag, .part,
#                   .part.etag). Other files of the same repository (an older quant) go.
# --keep-rpc        the tensor cache of ggml-rpc-server ($LLAMA_CACHE/rpc) stays: the node is in a split group of its
#                   deployment, and a restarted group loads in minutes instead of a quarter of an hour
# --keep-vllm       vLLM's cache ($LLAMA_CACHE/huggingface) stays
# --dry-run         deletes nothing, reports what would go
# Without arguments everything goes: a node without a deployment keeps no weights.
# What is touched: the shard directories <owner>__<repo>, `rpc`, `huggingface`, `models--*` and loose *.gguf files of
# llama.cpp's own cache. Anything else in the volume stays. Entries changed in the last MODELS_CLEANUP_MIN_AGE_S
# seconds (default 60) are skipped: a download or a tensor upload may be under way, a cleanup never pulls the file out
# from under it.
# Output: a line `removed <path> <bytes>` per removal, then one line `RESULT <json>`:
#   {"freed_bytes": N, "models_bytes": N (left on the volume), "removed": [...], "skipped": [...], "dry_run": bool}
set -uo pipefail

CACHE="${LLAMA_CACHE:-/var/lib/llm-models}"
MIN_AGE="${MODELS_CLEANUP_MIN_AGE_S:-60}"
DRY=0
KEEP_RPC=0
KEEP_VLLM=0
USAGE=0
declare -A KEEP=()        # <dir>/<file> that stays
declare -A KEEP_DIR=()    # <dir> that holds a kept file
REMOVED=()
SKIPPED=()
FREED=0

die() { echo "models-cleanup: $*" >&2; exit 2; }

[[ "$MIN_AGE" =~ ^[0-9]+$ ]] || die "MODELS_CLEANUP_MIN_AGE_S must be a whole number (got '$MIN_AGE')"
# The path is the one rm -rf works under: refuse anything that is not an absolute path at least two levels deep.
[[ "$CACHE" =~ ^/[^/]+/[^/]+ ]] && [[ "$CACHE" != *..* ]] || die "LLAMA_CACHE must be an absolute path of at least two levels (got '$CACHE')"
CACHE="${CACHE%/}"

# keep_file REPO FILE: marks the file and, for the first shard of a multi-file GGUF, all its shards
keep_file() {
  local repo="$1" file="$2" dir prefix total i
  dir="${repo//\//__}"
  KEEP_DIR["$dir"]=1
  KEEP["$dir/$file"]=1
  if [[ "$file" =~ ^(.*)-00001-of-([0-9]{5})\.gguf$ ]]; then
    prefix="${BASH_REMATCH[1]}"
    total=$((10#${BASH_REMATCH[2]}))
    for ((i = 1; i <= total && i <= 9999; i++)); do
      KEEP["$dir/$(printf '%s-%05d-of-%05d.gguf' "$prefix" "$i" "$total")"]=1
    done
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --keep-rpc) KEEP_RPC=1 ;;
    --keep-vllm) KEEP_VLLM=1 ;;
    --usage) USAGE=1 ;;
    --keep)
      [ $# -ge 2 ] || die "--keep needs REPO:FILE"
      shift
      spec="$1"
      repo="${spec%%:*}"
      file="${spec#*:}"
      [[ "$spec" == *:* ]] || die "--keep: not REPO:FILE: $spec"
      [[ "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "--keep: not a repository: $repo"
      [[ "$file" =~ ^[A-Za-z0-9._+=@-]+(/[A-Za-z0-9._+=@-]+)*$ ]] && [[ "$file" != *..* ]] || die "--keep: not a file path: $file"
      keep_file "$repo" "$file"
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# size of a file or of the files of a directory tree in bytes (the sum of the file sizes, not the blocks they occupy)
bytes() { find "$1" -type f -printf '%s\n' 2>/dev/null | awk '{s += $1} END {print s + 0}'; }

if [ "$USAGE" = 1 ]; then
  if [ -d "$CACHE" ]; then bytes "$CACHE"; else echo 0; fi
  exit 0
fi

CUTOFF=$(( $(date +%s) - MIN_AGE ))
# recent PATH: a file under PATH (or PATH itself) was written in the last MIN_AGE seconds
recent() { [ -n "$(find "$1" -type f -newermt "@$CUTOFF" -print -quit 2>/dev/null)" ]; }

# drop LABEL PATH SIZE: removes a file or a tree (or only counts it with --dry-run)
drop() {
  local label="$1" path="$2" size="$3"
  if [ "$DRY" = 0 ]; then
    rm -rf --one-file-system -- "$path" || { SKIPPED+=("$label"); return 0; }
  fi
  FREED=$((FREED + size))
  REMOVED+=("$label")
  echo "removed $label $size"
}

# empty_dir LABEL DIR: removes what is inside DIR, DIR stays (a running rpc server keeps writing into its cache directory)
empty_dir() {
  local label="$1" dir="$2" size
  [ -n "$(find "$dir" -mindepth 1 -print -quit 2>/dev/null)" ] || return 0
  if recent "$dir"; then SKIPPED+=("$label"); echo "skipped $label (changed in the last ${MIN_AGE} s)"; return 0; fi
  size="$(bytes "$dir")"
  if [ "$DRY" = 0 ]; then
    find "$dir" -mindepth 1 -delete 2>/dev/null || { SKIPPED+=("$label"); return 0; }
  fi
  FREED=$((FREED + size))
  REMOVED+=("$label")
  echo "removed $label $size"
}

# shard_dir NAME DIR: a shard directory keeps the kept files, the rest goes
shard_dir() {
  local name="$1" dir="$2" rel base f size
  if [ -z "${KEEP_DIR[$name]:-}" ]; then
    if recent "$dir"; then SKIPPED+=("$name"); echo "skipped $name (changed in the last ${MIN_AGE} s)"; return 0; fi
    drop "$name" "$dir" "$(bytes "$dir")"
    return 0
  fi
  while IFS= read -r -d '' f; do
    rel="${f#"$dir"/}"
    base="$rel"
    case "$rel" in
      *.part.etag) base="${rel%.part.etag}" ;;
      *.part) base="${rel%.part}" ;;
      *.etag) base="${rel%.etag}" ;;
    esac
    [ -z "${KEEP[$name/$base]:-}" ] || continue
    if recent "$f"; then SKIPPED+=("$name/$rel"); continue; fi
    size="$(stat -c %s -- "$f" 2>/dev/null || echo 0)"
    drop "$name/$rel" "$f" "$size"
  done < <(find "$dir" -type f -print0 2>/dev/null)
  [ "$DRY" = 1 ] || find "$dir" -mindepth 1 -type d -empty -delete 2>/dev/null
  return 0
}

if [ -d "$CACHE" ]; then
  shopt -s nullglob dotglob
  for p in "$CACHE"/*; do
    name="${p##*/}"
    if [ -L "$p" ]; then continue; fi  # never follow or touch a link
    case "$name" in
      rpc)
        [ "$KEEP_RPC" = 1 ] || { [ -d "$p" ] && empty_dir "$name" "$p"; } ;;
      huggingface)
        [ "$KEEP_VLLM" = 1 ] || { [ -d "$p" ] && empty_dir "$name" "$p"; } ;;
      models--*)
        if [ -d "$p" ]; then shard_dir "$name" "$p"; fi ;;
      *.gguf|*.gguf.*)
        if [ -f "$p" ]; then
          if recent "$p"; then SKIPPED+=("$name"); else drop "$name" "$p" "$(stat -c %s -- "$p" 2>/dev/null || echo 0)"; fi
        fi ;;
      *)
        if [ -d "$p" ] && [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*__[A-Za-z0-9._-]+$ ]]; then shard_dir "$name" "$p"; fi ;;
    esac
  done
fi

LEFT=0
[ -d "$CACHE" ] && LEFT="$(bytes "$CACHE")"
# the output stays short however many files went: the lists are cut
json_list() { if [ $# -eq 0 ]; then echo '[]'; else printf '%s\n' "${@:1:50}" | jq -R . | jq -sc .; fi; }
echo "RESULT $(jq -nc --argjson freed "$FREED" --argjson left "${LEFT:-0}" --argjson dry "$([ "$DRY" = 1 ] && echo true || echo false)" \
  --argjson removed "$(json_list ${REMOVED[@]+"${REMOVED[@]}"})" --argjson skipped "$(json_list ${SKIPPED[@]+"${SKIPPED[@]}"})" \
  '{freed_bytes: $freed, models_bytes: $left, removed: $removed, skipped: $skipped, dry_run: $dry}')"
