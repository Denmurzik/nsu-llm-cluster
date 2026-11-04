#!/usr/bin/env bash
# Raise the llama.cpp device limits from 16 to 64 (big models on the class: CUDA0 + CPU on 15 nodes = 30 RPC devices).
# See docs/research/big-models.md, section 3. Every change is checked; the build fails if the pinned sources differ.
#
#   patch-llama.sh <src>                  patch the sources after `git clone` (llama_max_devices() -> 64)
#   patch-llama.sh --check-build <build>  after `cmake -B <build>`: ggml-backend.cpp is compiled with
#                                         -DGGML_SCHED_MAX_BACKENDS=64 (passed via CMAKE_CXX_FLAGS)
set -euo pipefail
LIMIT=64
die() { echo "patch-llama: $*" >&2; exit 1; }

if [ "${1:-}" = "--check-build" ]; then
  cc="${2:?build dir required}/compile_commands.json"
  [ -f "$cc" ] || die "$cc missing (configure with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)"
  grep -E -- '-c [^ "]*/ggml/src/ggml-backend\.cpp"' "$cc" | grep -q -- "-DGGML_SCHED_MAX_BACKENDS=$LIMIT " \
    || die "ggml-backend.cpp is not compiled with -DGGML_SCHED_MAX_BACKENDS=$LIMIT"
  echo "patch-llama: ggml-backend.cpp compiled with -DGGML_SCHED_MAX_BACKENDS=$LIMIT"
  exit 0
fi

src="${1:?source dir required}"
cd "$src"

# 1. llama_max_devices() returns a literal 16 (src/llama.cpp). It bounds --tensor-split and --fit-target
#    (common/arg.cpp) and the tensor_split values the model keeps (src/llama-model.cpp).
grep -A1 -x 'size_t llama_max_devices(void) {' src/llama.cpp | grep -qx '    return 16;' \
  || die "src/llama.cpp: llama_max_devices() does not return 16 as expected"
sed -i '/^size_t llama_max_devices(void) {$/{n;s/^    return 16;$/    return '"$LIMIT"';/}' src/llama.cpp
grep -A1 -x 'size_t llama_max_devices(void) {' src/llama.cpp | grep -qx "    return $LIMIT;" \
  || die "src/llama.cpp: llama_max_devices() was not patched"
# common copies llama_max_devices() values out of params.tensor_split; that array must hold at least LIMIT.
n="$(sed -n 's/^.*float *tensor_split\[\([0-9]*\)\].*$/\1/p' common/common.h | head -n1)"
[ -n "$n" ] && [ "$n" -ge "$LIMIT" ] || die "common/common.h: tensor_split[${n:-?}] is shorter than $LIMIT"

# 2. The scheduler limit GGML_SCHED_MAX_BACKENDS (default 16) is #ifndef-guarded in ggml-backend.cpp, so the
#    -DGGML_SCHED_MAX_BACKENDS flag in CMAKE_CXX_FLAGS sets it. It must stay guarded and C++-only: C sources and
#    headers do not get CMAKE_CXX_FLAGS.
grep -B1 -x '#define GGML_SCHED_MAX_BACKENDS 16' ggml/src/ggml-backend.cpp | grep -qx '#ifndef GGML_SCHED_MAX_BACKENDS' \
  || die "ggml/src/ggml-backend.cpp: GGML_SCHED_MAX_BACKENDS is not an #ifndef-guarded 16"
users="$(grep -rl --include='*.c' --include='*.h' --include='*.cpp' --include='*.cu' --include='*.cuh' \
  GGML_SCHED_MAX_BACKENDS . | sed 's#^\./##' | sort)"
[ "$users" = "ggml/src/ggml-backend.cpp" ] \
  || die "GGML_SCHED_MAX_BACKENDS is used outside ggml/src/ggml-backend.cpp: $(echo "$users" | tr '\n' ' ')"

echo "patch-llama: llama_max_devices() = $LIMIT; GGML_SCHED_MAX_BACKENDS is set by CMAKE_CXX_FLAGS"
