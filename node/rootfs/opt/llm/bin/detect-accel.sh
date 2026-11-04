#!/usr/bin/env bash
# Prints "nvidia" if an NVIDIA GPU is usable, else "cpu".
# With --vram prints total VRAM of GPU 0 in MiB (nothing when there is no GPU).
# With --ram prints the memory this container may use in MiB: MemTotal (inside WSL that is the memory of WSL),
# lower when the container has a cgroup memory limit.
if [ "${1:-}" = "--ram" ]; then
  ram="$(awk '/^MemTotal:/ {print int($2 / 1024)}' "${PROC_MEMINFO:-/proc/meminfo}" 2>/dev/null)"
  limit="$(cat "${CGROUP_MEMORY_MAX:-/sys/fs/cgroup/memory.max}" 2>/dev/null || true)"
  if [[ "$limit" =~ ^[0-9]+$ ]] && [ -n "$ram" ] && [ $((limit / 1048576)) -lt "$ram" ]; then
    ram=$((limit / 1048576))
  fi
  [ -n "$ram" ] && echo "$ram"
  exit 0
fi
if [ "${1:-}" = "--vram" ]; then
  if nvidia-smi -L >/dev/null 2>&1; then
    nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i 0 2>/dev/null | head -n1 | tr -d ' \r'
  fi
  exit 0
fi
if nvidia-smi -L >/dev/null 2>&1; then echo nvidia; else echo cpu; fi
