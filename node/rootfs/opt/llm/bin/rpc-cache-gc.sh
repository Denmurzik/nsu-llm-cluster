#!/usr/bin/env bash
# LRU cleanup of the ggml-rpc-server tensor cache (`-c`: $LLAMA_CACHE/rpc, one file per tensor named by its FNV-1a
# hash). llama.cpp never deletes these files and has no size limit of its own (docs/research/big-models.md, section 4).
#   rpc-cache-gc.sh          one pass
#   rpc-cache-gc.sh --loop   a pass now and then every RPC_CACHE_GC_INTERVAL_S seconds (default 600)
# Settings:
#   RPC_CACHE_MAX_GIB       cache size limit in GiB (default 64, 0 = no limit)
#   RPC_CACHE_MIN_FREE_PCT  keep at least this share of the file system free (default 10)
#   RPC_CACHE_DIR           the cache directory (default $LLAMA_CACHE/rpc)
#   RPC_CACHE_MAX_BYTES     the limit in bytes instead of GiB (tests)
# The least recently used file goes first: the newer of its access and modification times (the server reads a file on a
# cache hit; relatime keeps atime to the day). A removed tensor is sent by the leader again on its next load.
# In WSL2 `df` reports the ext4.vhdx at its maximum size, not the free space of the Windows disk: there only
# RPC_CACHE_MAX_GIB protects the disk.
set -uo pipefail

DIR="${RPC_CACHE_DIR:-${LLAMA_CACHE:-/var/lib/llm-models}/rpc}"
INTERVAL="${RPC_CACHE_GC_INTERVAL_S:-600}"
MAX_GIB="${RPC_CACHE_MAX_GIB:-64}"
MIN_FREE_PCT="${RPC_CACHE_MIN_FREE_PCT:-10}"
for v in MAX_GIB MIN_FREE_PCT INTERVAL; do
  [[ "${!v}" =~ ^[0-9]+$ ]] || { echo "rpc-cache-gc: $v must be a whole number (got '${!v}')" >&2; exit 2; }
done
LIMIT="${RPC_CACHE_MAX_BYTES:-$((MAX_GIB * 1024 * 1024 * 1024))}"

log() { echo "$(date -u +%FT%TZ) rpc-cache-gc: $*"; }

gc_once() {
  [ -d "$DIR" ] || return 0
  local list total=0 size_fs avail need_free removed=0 freed=0 used s p
  list="$(find "$DIR" -maxdepth 1 -type f -printf '%A@ %T@ %s %p\n' 2>/dev/null \
    | awk '{a = int($1); m = int($2); u = (a > m) ? a : m; $1 = ""; $2 = ""; sub(/^  /, ""); print u, $0}' | sort -n)"
  [ -n "$list" ] || return 0
  while read -r _ s _; do total=$((total + s)); done <<<"$list"
  read -r size_fs avail < <(df -P -B1 "$DIR" 2>/dev/null | awk 'NR == 2 {print $2, $4}')
  size_fs="${size_fs:-0}"; avail="${avail:-0}"
  need_free=$((size_fs / 100 * MIN_FREE_PCT))
  while read -r used s p; do
    if { [ "$LIMIT" -gt 0 ] && [ "$total" -gt "$LIMIT" ]; } || [ "$avail" -lt "$need_free" ]; then
      rm -f -- "$p" && { total=$((total - s)); avail=$((avail + s)); removed=$((removed + 1)); freed=$((freed + s)); }
    else
      break
    fi
  done <<<"$list"
  [ "$removed" = 0 ] || log "removed $removed files ($freed bytes), cache now $total bytes (limit $LIMIT)"
}

if [ "${1:-}" = "--loop" ]; then
  trap 'exit 0' TERM INT
  while :; do
    gc_once
    sleep "$INTERVAL" &
    wait $!
  done
fi
gc_once
