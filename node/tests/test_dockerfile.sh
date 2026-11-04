#!/usr/bin/env bash
# Offline tests of the llama.cpp device-limit patches in node/Dockerfile (no docker, no network).
# The patch script runs against small fixtures copied from b12033; the Dockerfile is checked statically.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
PATCH="$NODE/llama/patch-llama.sh"
DF="$NODE/Dockerfile"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }

# Fixture source tree: the relevant lines of b12033 verbatim.
mksrc() { # dir
  mkdir -p "$1/src" "$1/ggml/src" "$1/common"
  cat > "$1/src/llama.cpp" <<'EOF'
    return result;
}

size_t llama_max_devices(void) {
    return 16;
}

size_t llama_max_tensor_buft_overrides() {
    return 4096;
}
EOF
  cat > "$1/ggml/src/ggml-backend.cpp" <<'EOF'
// scheduler

#ifndef GGML_SCHED_MAX_BACKENDS
#define GGML_SCHED_MAX_BACKENDS 16
#endif

    ggml_backend_t backends[GGML_SCHED_MAX_BACKENDS];
    GGML_ASSERT(n_backends <= GGML_SCHED_MAX_BACKENDS);
EOF
  printf 'float tensor_split[128] = {0};\n' > "$1/common/common.h"
}

[ -f "$PATCH" ] && pass "exists llama/patch-llama.sh" || fail "exists llama/patch-llama.sh"
bash -n "$PATCH" 2>/dev/null && pass "bash -n patch-llama.sh" || fail "bash -n patch-llama.sh"

# --- source patch on b12033 ---
mksrc "$TMP/ok"
if out="$(bash "$PATCH" "$TMP/ok" 2>&1)"; then pass "patch succeeds on b12033 sources"; else fail "patch succeeds on b12033 sources"; echo "$out" | sed 's/^/    | /'; fi
if grep -A1 -x 'size_t llama_max_devices(void) {' "$TMP/ok/src/llama.cpp" | grep -qx '    return 64;'; then
  pass "llama_max_devices returns 64"; else fail "llama_max_devices returns 64"; cat "$TMP/ok/src/llama.cpp"; fi
grep -qx '    return 4096;' "$TMP/ok/src/llama.cpp" && pass "neighbouring function untouched" || fail "neighbouring function untouched"
grep -c 'return 16;' "$TMP/ok/src/llama.cpp" | grep -qx 0 && pass "no 16 left in llama.cpp" || fail "no 16 left in llama.cpp"
grep -qx '#define GGML_SCHED_MAX_BACKENDS 16' "$TMP/ok/ggml/src/ggml-backend.cpp" \
  && pass "scheduler default left in source (raised by -D)" || fail "scheduler default left in source (raised by -D)"

# --- the guards fail the build on unexpected sources ---
mksrc "$TMP/moved"; sed -i 's/return 16;/return 32;/' "$TMP/moved/src/llama.cpp"
bash "$PATCH" "$TMP/moved" >/dev/null 2>&1 && fail "patch fails when llama_max_devices is not 16" || pass "patch fails when llama_max_devices is not 16"

mksrc "$TMP/renamed"; sed -i 's/llama_max_devices(void)/llama_max_devices()/' "$TMP/renamed/src/llama.cpp"
bash "$PATCH" "$TMP/renamed" >/dev/null 2>&1 && fail "patch fails when the function line changed" || pass "patch fails when the function line changed"

mksrc "$TMP/noifndef"; sed -i '/^#ifndef GGML_SCHED_MAX_BACKENDS$/d;/^#endif$/d' "$TMP/noifndef/ggml/src/ggml-backend.cpp"
bash "$PATCH" "$TMP/noifndef" >/dev/null 2>&1 && fail "patch fails when the scheduler define is not #ifndef-guarded" \
  || pass "patch fails when the scheduler define is not #ifndef-guarded"

mksrc "$TMP/cfile"; printf 'int a[GGML_SCHED_MAX_BACKENDS];\n' > "$TMP/cfile/ggml/src/ggml.c"
bash "$PATCH" "$TMP/cfile" >/dev/null 2>&1 && fail "patch fails when the scheduler define leaks into a C or header file" \
  || pass "patch fails when the scheduler define leaks into a C or header file"

mksrc "$TMP/smallts"; printf 'float tensor_split[16] = {0};\n' > "$TMP/smallts/common/common.h"
bash "$PATCH" "$TMP/smallts" >/dev/null 2>&1 && fail "patch fails when common tensor_split is shorter than 64" \
  || pass "patch fails when common tensor_split is shorter than 64"

# --- compile-command check after cmake configure ---
mkdir -p "$TMP/b_ok" "$TMP/b_no"
cat > "$TMP/b_ok/compile_commands.json" <<'EOF'
[
{
  "directory": "/src/build/ggml/src",
  "command": "/usr/bin/c++ -DGGML_BUILD -DGGML_SCHED_MAX_BACKENDS=64 -O3 -DNDEBUG -std=gnu++17 -o CMakeFiles/ggml-base.dir/ggml-backend.cpp.o -c /src/ggml/src/ggml-backend.cpp",
  "file": "/src/ggml/src/ggml-backend.cpp",
  "output": "ggml/src/CMakeFiles/ggml-base.dir/ggml-backend.cpp.o"
}
]
EOF
sed 's/ -DGGML_SCHED_MAX_BACKENDS=64//' "$TMP/b_ok/compile_commands.json" > "$TMP/b_no/compile_commands.json"
bash "$PATCH" --check-build "$TMP/b_ok" >/dev/null 2>&1 && pass "check-build accepts -DGGML_SCHED_MAX_BACKENDS=64" \
  || fail "check-build accepts -DGGML_SCHED_MAX_BACKENDS=64"
bash "$PATCH" --check-build "$TMP/b_no" >/dev/null 2>&1 && fail "check-build fails without the define" \
  || pass "check-build fails without the define"
bash "$PATCH" --check-build "$TMP/missing" >/dev/null 2>&1 && fail "check-build fails without compile_commands.json" \
  || pass "check-build fails without compile_commands.json"

# --- Dockerfile wiring: both build stages ---
grep -qx 'ARG LLAMA_TAG=b12033' "$DF" && pass "llama.cpp ref stays b12033" || fail "llama.cpp ref stays b12033"
for stage in build-cpu build-gpu; do
  body="$(awk -v s="$stage" '/^FROM /{on = ($NF == s)} on' "$DF")"
  [ -n "$body" ] || { fail "$stage: stage found"; continue; }
  clone="$(printf '%s\n' "$body" | grep -n 'git clone' | head -n1 | cut -d: -f1)"
  patch="$(printf '%s\n' "$body" | grep -n 'patch-llama.sh /src$' | head -n1 | cut -d: -f1)"
  conf="$(printf '%s\n' "$body" | grep -n 'cmake -B build' | head -n1 | cut -d: -f1)"
  printf '%s\n' "$body" | grep -q '^COPY llama/patch-llama.sh /usr/local/bin/patch-llama.sh$' \
    && pass "$stage: copies the patch script" || fail "$stage: copies the patch script"
  [ -n "$clone" ] && [ -n "$patch" ] && [ -n "$conf" ] && [ "$clone" -lt "$patch" ] && [ "$patch" -lt "$conf" ] \
    && pass "$stage: sources patched after clone, before configure" || fail "$stage: sources patched after clone, before configure"
  printf '%s\n' "$body" | grep -q -- '-DCMAKE_CXX_FLAGS=-DGGML_SCHED_MAX_BACKENDS=64' \
    && pass "$stage: scheduler limit 64 via CMAKE_CXX_FLAGS" || fail "$stage: scheduler limit 64 via CMAKE_CXX_FLAGS"
  printf '%s\n' "$body" | grep -q -- '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON' \
    && pass "$stage: exports compile commands" || fail "$stage: exports compile commands"
  printf '%s\n' "$body" | grep -q 'patch-llama.sh --check-build build' \
    && pass "$stage: checks the compile command before building" || fail "$stage: checks the compile command before building"
done

# --- NVIDIA device plugin: built from source into the GPU image (nvcr.io answers 403 from Russia) ---
dp="$(awk '/^FROM /{on = ($NF == "device-plugin")} on' "$DF")"
gpu="$(awk '/^FROM /{on = ($NF == "gpu")} on' "$DF")"
cpu="$(awk '/^FROM /{on = ($NF == "cpu")} on' "$DF")"
has() { printf '%s\n' "$1" | grep -q -- "$2"; }  # has TEXT PATTERN
grep -q 'nvcr\.io/' "$DF" && fail "no nvcr.io image in the node Dockerfile" || pass "no nvcr.io image in the node Dockerfile"
grep -qx 'ARG DEVICE_PLUGIN_TAG=v0.19.3' "$DF" && pass "device plugin pinned to v0.19.3" || fail "device plugin pinned to v0.19.3"
grep -qx 'ARG DEVICE_PLUGIN_COMMIT=570e776d6813cb9f3e9c8c105cf8d79989db6650' "$DF" \
  && pass "device plugin tag checked against its commit" || fail "device plugin tag checked against its commit"
has "$dp" '^FROM golang:[0-9.]*-bookworm AS device-plugin$' \
  && pass "device-plugin: Go on bookworm (glibc older than Ubuntu 24.04's)" || fail "device-plugin: Go on bookworm (glibc older than Ubuntu 24.04's)"
has "$dp" 'git clone --depth 1 --branch "${DEVICE_PLUGIN_TAG}" https://github.com/NVIDIA/k8s-device-plugin /src' \
  && pass "device-plugin: sources of the pinned tag" || fail "device-plugin: sources of the pinned tag"
has "$dp" 'rev-parse HEAD)" = "${DEVICE_PLUGIN_COMMIT}"' \
  && pass "device-plugin: build fails on another commit" || fail "device-plugin: build fails on another commit"
has "$dp" 'GOFLAGS=-mod=vendor' && has "$dp" 'GOPROXY=off' \
  && pass "device-plugin: vendored modules only, no module download" || fail "device-plugin: vendored modules only, no module download"
has "$dp" 'CGO_ENABLED=1' && pass "device-plugin: cgo (go-nvml)" || fail "device-plugin: cgo (go-nvml)"
has "$gpu" '^COPY --from=device-plugin /out/nvidia-device-plugin /opt/llm/bin/nvidia-device-plugin$' \
  && pass "gpu image carries nvidia-device-plugin" || fail "gpu image carries nvidia-device-plugin"
has "$cpu" 'device-plugin' && fail "cpu image has no device plugin" || pass "cpu image has no device plugin"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
