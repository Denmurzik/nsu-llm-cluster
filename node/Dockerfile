# syntax=docker/dockerfile:1
# NSU LLM Cluster node image: llama.cpp (CPU/CUDA, RPC) + k3s agent + standalone registrar.
# Build context is this directory (node/).

ARG LLAMA_TAG=b12033
# the commit of that tag (git ls-remote https://github.com/ggml-org/llama.cpp refs/tags/b12033): a moved tag fails the build
ARG LLAMA_COMMIT=785322d17f2d51e5185cf996a1f4e46d87640841
ARG K3S_VERSION=v1.36.5+k3s1
# NVIDIA device plugin for the GPU image: nvcr.io answers 403 from Russia, so the plugin is built from the sources of
# the release tag (github.com, vendored Go modules) and runs from this image (main/manifests/common-45).
ARG DEVICE_PLUGIN_TAG=v0.19.3
ARG DEVICE_PLUGIN_COMMIT=570e776d6813cb9f3e9c8c105cf8d79989db6650
# Base of the GPU image (the lecturer's limit: the whole node footprint fits 16 GB on a class PC):
#   slim (default)  ubuntu:24.04 + only the CUDA libraries the two binaries link (libcudart, libcublas, libcublasLt; NCCL is off)
#   fat             the former nvidia/cuda runtime base (2.8 GB of CUDA libraries). Fallback if a slim image fails on a class PC:
#                   docker build --target gpu --build-arg GPU_BASE=fat --build-arg JOBS=16 ... (docs/deploy-class.md, "GPU-образ ноды: slim и fat")
ARG GPU_BASE=slim

# ---------------------------------------------------------------- build-cpu
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS build-cpu
ARG LLAMA_TAG
ARG LLAMA_COMMIT
ARG JOBS=4
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates libcurl4-openssl-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMA_TAG}" https://github.com/ggml-org/llama.cpp /src \
 && [ "$(git -C /src rev-parse HEAD)" = "${LLAMA_COMMIT}" ] \
 || { echo "llama.cpp ${LLAMA_TAG} is not commit ${LLAMA_COMMIT}" >&2; exit 1; }
WORKDIR /src
RUN grep -n add_executable tools/rpc/CMakeLists.txt
# Device limits 16 -> 64 (split over VRAM + RAM of 15 nodes = 30 RPC devices); fails the build if the sources differ.
COPY llama/patch-llama.sh /usr/local/bin/patch-llama.sh
RUN bash /usr/local/bin/patch-llama.sh /src
# HTTPS model downloads (--hf-repo) use LLAMA_OPENSSL (ON by default in b12033; LLAMA_CURL is deprecated).
RUN cmake -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_RPC=ON -DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON \
      -DBUILD_SHARED_LIBS=OFF -DLLAMA_OPENSSL=ON \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_CXX_FLAGS=-DGGML_SCHED_MAX_BACKENDS=64 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
 && bash /usr/local/bin/patch-llama.sh --check-build build \
 && cmake --build build --config Release -j"${JOBS}" --target llama-server ggml-rpc-server \
 && strip build/bin/llama-server build/bin/ggml-rpc-server

# ---------------------------------------------------------------- build-gpu
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66 AS build-gpu
ARG LLAMA_TAG
ARG LLAMA_COMMIT
ARG JOBS=4
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates libcurl4-openssl-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMA_TAG}" https://github.com/ggml-org/llama.cpp /src \
 && [ "$(git -C /src rev-parse HEAD)" = "${LLAMA_COMMIT}" ] \
 || { echo "llama.cpp ${LLAMA_TAG} is not commit ${LLAMA_COMMIT}" >&2; exit 1; }
WORKDIR /src
RUN grep -n add_executable tools/rpc/CMakeLists.txt
# Device limits 16 -> 64 (split over VRAM + RAM of 15 nodes = 30 RPC devices); fails the build if the sources differ.
COPY llama/patch-llama.sh /usr/local/bin/patch-llama.sh
RUN bash /usr/local/bin/patch-llama.sh /src
# sm_86 (3090/A-series), sm_89 (4070), sm_120 (RTX 5090, needs CUDA >= 12.8 and driver >= 570).
# GGML_CUDA_NCCL=OFF: NCCL (libnccl, 380 MB) only speeds up several GPUs inside ONE process; a node has one GPU and the split goes
# over RPC, so the library would be dead weight in every copy of the image on a class PC (16 GB limit).
RUN cmake -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_RPC=ON -DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON \
      -DBUILD_SHARED_LIBS=OFF -DLLAMA_OPENSSL=ON \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_CXX_FLAGS=-DGGML_SCHED_MAX_BACKENDS=64 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
      -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="86;89;120" -DGGML_CUDA_NCCL=OFF \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
 && bash /usr/local/bin/patch-llama.sh --check-build build \
 && cmake --build build --config Release -j"${JOBS}" --target llama-server ggml-rpc-server \
 && strip build/bin/llama-server build/bin/ggml-rpc-server

# ---------------------------------------------------------------- device-plugin
# bookworm: the binary links glibc dynamically (cgo, go-nvml), and glibc 2.36 runs on Ubuntu 24.04 (2.39). The link
# flags are those of the release Makefile (go-nvml resolves NVML symbols at run time); vendored modules, no download.
FROM golang:1.26.4-bookworm@sha256:b305420a68d0f229d91eb3b3ed9e519fcf2cf5461da4bef997bf927e8c0bfd2b AS device-plugin
ARG DEVICE_PLUGIN_TAG
ARG DEVICE_PLUGIN_COMMIT
RUN git clone --depth 1 --branch "${DEVICE_PLUGIN_TAG}" https://github.com/NVIDIA/k8s-device-plugin /src \
 && [ "$(git -C /src rev-parse HEAD)" = "${DEVICE_PLUGIN_COMMIT}" ]
WORKDIR /src
RUN CGO_ENABLED=1 GOFLAGS=-mod=vendor GOPROXY=off GOTOOLCHAIN=local go build \
      -ldflags "-s -w '-extldflags=-Wl,--export-dynamic -Wl,--unresolved-symbols=ignore-in-object-files' \
        -X github.com/NVIDIA/k8s-device-plugin/internal/info.version=${DEVICE_PLUGIN_TAG} \
        -X github.com/NVIDIA/k8s-device-plugin/internal/info.gitCommit=${DEVICE_PLUGIN_COMMIT}" \
      -o /out/nvidia-device-plugin ./cmd/nvidia-device-plugin \
 && /out/nvidia-device-plugin --version

# ---------------------------------------------------------------- k3s
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS k3s
ARG K3S_VERSION
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    base="https://github.com/k3s-io/k3s/releases/download/$(echo "${K3S_VERSION}" | sed 's/+/%2B/')"; \
    curl -fsSL "$base/k3s" -o /k3s; \
    curl -fsSL "$base/sha256sum-amd64.txt" -o /sha256sum-amd64.txt; \
    want="$(awk '$2=="k3s" {print $1}' /sha256sum-amd64.txt)"; \
    [ -n "$want" ]; \
    echo "$want  /k3s" | sha256sum -c -; \
    chmod +x /k3s

# ---------------------------------------------------------------- cpu
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS cpu
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl jq iptables iproute2 libcurl4 libssl3 libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build-cpu /src/build/bin/llama-server /opt/llm/bin/llama-server
COPY --from=build-cpu /src/build/bin/ggml-rpc-server /opt/llm/bin/ggml-rpc-server
COPY --from=k3s /k3s /opt/llm/bin/k3s
COPY rootfs/ /
RUN chmod +x /opt/llm/bin/*
ENV PATH=/opt/llm/bin:$PATH LLAMA_CACHE=/var/lib/llm-models
# Build arguments given last (another commit does not rebuild the layers above): the node reports them to the portal
# (env NODE_VERSION, NODE_REVISION; registrar.sh, task 2-30); release/release.sh passes both.
ARG VERSION=dev
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.version=$VERSION org.opencontainers.image.revision=$GIT_COMMIT
ENV NODE_VERSION=$VERSION NODE_REVISION=$GIT_COMMIT
ENTRYPOINT ["/opt/llm/bin/entrypoint.sh"]

# ---------------------------------------------------------------- gpu-libs
# The CUDA libraries the binaries really need, read from `ldd` of the freshly built llama-server and ggml-rpc-server (not a
# hand-kept list: a library that a llama.cpp bump starts to link comes along, a missing one fails the build). libcuda.so.1 is the
# driver: the nvidia runtime mounts it from the host, so it is the only library allowed to stay "not found". Each file is copied
# dereferenced under its soname.
FROM build-gpu AS gpu-libs
RUN set -eu; mkdir -p /out; \
    for b in llama-server ggml-rpc-server; do \
      ldd "/src/build/bin/$b" > "/tmp/ldd-$b"; \
      while read -r name arrow path _; do \
        [ "$arrow" = "=>" ] || continue; \
        if [ "$path" = not ]; then \
          [ "$name" = libcuda.so.1 ] || { echo "$b needs $name, which is not in the build image" >&2; exit 1; }; \
          continue; \
        fi; \
        case "$path" in \
          /usr/local/cuda*|*/libnccl.so.*) cp -L "$path" "/out/$name" ;; \
        esac; \
      done < "/tmp/ldd-$b"; \
    done; \
    for l in libcudart.so.12 libcublas.so.12 libcublasLt.so.12; do \
      [ -s "/out/$l" ] || { echo "$l missing from ldd of the binaries" >&2; exit 1; }; \
    done; \
    ls -l /out; du -sh /out

# ---------------------------------------------------------------- gpu-base-slim / gpu-base-fat
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS gpu-base-slim
COPY --from=gpu-libs /out/ /usr/local/cuda/lib64/
# What the nvidia/cuda base sets and the nvidia runtime and the device plugin rely on (checked on the 12.8.1 runtime image):
# visible devices and capabilities (compute for libcuda, utility for NVML/nvidia-smi), the library path, the CUDA requirement.
RUN printf '/usr/local/cuda/lib64\n' > /etc/ld.so.conf.d/000_cuda.conf && ldconfig
ENV NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility NVIDIA_REQUIRE_CUDA="cuda>=12.8" \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64

FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04@sha256:ebef3c171eeef0298e4eb2e4be843105edf3b8b0ac45e0b43acee358e8046867 AS gpu-base-fat

# ---------------------------------------------------------------- gpu
FROM gpu-base-${GPU_BASE} AS gpu
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl jq iptables iproute2 libcurl4 libssl3 libgomp1 gnupg \
 && curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
 && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nvidia-container-toolkit \
 && v="$(dpkg-query -W -f='${Version}' nvidia-container-toolkit)" \
 && echo "nvidia-container-toolkit ${v}" \
 && [ "$(printf '%s\n1.20.1\n' "${v%%-*}" | sort -V | head -n1)" = "1.20.1" ] \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build-gpu /src/build/bin/llama-server /opt/llm/bin/llama-server
COPY --from=build-gpu /src/build/bin/ggml-rpc-server /opt/llm/bin/ggml-rpc-server
COPY --from=device-plugin /out/nvidia-device-plugin /opt/llm/bin/nvidia-device-plugin
COPY --from=k3s /k3s /opt/llm/bin/k3s
COPY rootfs/ /
RUN chmod +x /opt/llm/bin/*
ENV PATH=/opt/llm/bin:$PATH LLAMA_CACHE=/var/lib/llm-models
# Build arguments given last (another commit does not rebuild the layers above): the node reports them to the portal
# (env NODE_VERSION, NODE_REVISION; registrar.sh, task 2-30); release/release.sh passes both.
ARG VERSION=dev
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.version=$VERSION org.opencontainers.image.revision=$GIT_COMMIT
ENV NODE_VERSION=$VERSION NODE_REVISION=$GIT_COMMIT
ENTRYPOINT ["/opt/llm/bin/entrypoint.sh"]
