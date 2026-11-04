#!/usr/bin/env bash
# ggml-rpc-server of a split group. Configuration from the environment (set by the portal, see node/README.md):
#   RPC_DEVICES  -d list, e.g. CUDA0,CPU (VRAM and RAM of the node); its order is the order of --tensor-split
#   RPC_CACHE=1  -c: tensor cache in $LLAMA_CACHE/rpc, a restarted group does not resend the weights
#   RPC_CACHE_MAX_GIB  the cache has no limit of its own: rpc-cache-gc.sh drops least recently used files beyond it
#   serve-rpc.sh --print-cmd   print the command instead of running it
args=(ggml-rpc-server -H 0.0.0.0 -p "${RPC_PORT:-18050}" -t "${THREADS:-$(nproc)}")
[ -n "${RPC_DEVICES:-}" ] && args+=(-d "$RPC_DEVICES")
[ "${RPC_CACHE:-0}" = 1 ] && args+=(-c)
if [ "${1:-}" = "--print-cmd" ]; then
  echo "LLAMA_CACHE=${LLAMA_CACHE:-} ${args[*]}"
  exit 0
fi
rpc-cache-gc.sh --loop &
exec "${args[@]}"
