# syntax=docker/dockerfile:1
# NSU LLM Cluster node image: llama.cpp (CPU/CUDA, RPC) + k3s agent + standalone registrar.
# Build context is this directory (node/).

ARG LLAMA_TAG=b12033
ARG K3S_VERSION=v1.36.5+k3s1
# NVIDIA device plugin for the GPU image: nvcr.io answers 403 from Russia, so the plugin is built from the sources of
# the release tag (github.com, vendored Go modules) and runs from this image (main/manifests/common-45).
ARG DEVICE_PLUGIN_TAG=v0.19.3
ARG DEVICE_PLUGIN_COMMIT=570e776d6813cb9f3e9c8c105cf8d79989db6650

# ---------------------------------------------------------------- build-cpu
FROM ubuntu:24.04 AS build-cpu
ARG LLAMA_TAG
ARG JOBS=4
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates libcurl4-openssl-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMA_TAG}" https://github.com/ggml-org/llama.cpp /src
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
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04 AS build-gpu
ARG LLAMA_TAG
ARG JOBS=4
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates libcurl4-openssl-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMA_TAG}" https://github.com/ggml-org/llama.cpp /src
WORKDIR /src
RUN grep -n add_executable tools/rpc/CMakeLists.txt
# Device limits 16 -> 64 (split over VRAM + RAM of 15 nodes = 30 RPC devices); fails the build if the sources differ.
COPY llama/patch-llama.sh /usr/local/bin/patch-llama.sh
RUN bash /usr/local/bin/patch-llama.sh /src
# sm_86 (3090/A-series), sm_89 (4070), sm_120 (RTX 5090, needs CUDA >= 12.8 and driver >= 570).
RUN cmake -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_RPC=ON -DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON \
      -DBUILD_SHARED_LIBS=OFF -DLLAMA_OPENSSL=ON \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_CXX_FLAGS=-DGGML_SCHED_MAX_BACKENDS=64 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
      -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="86;89;120" \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
 && bash /usr/local/bin/patch-llama.sh --check-build build \
 && cmake --build build --config Release -j"${JOBS}" --target llama-server ggml-rpc-server \
 && strip build/bin/llama-server build/bin/ggml-rpc-server

# ---------------------------------------------------------------- device-plugin
# bookworm: the binary links glibc dynamically (cgo, go-nvml), and glibc 2.36 runs on Ubuntu 24.04 (2.39). The link
# flags are those of the release Makefile (go-nvml resolves NVML symbols at run time); vendored modules, no download.
FROM golang:1.26.4-bookworm AS device-plugin
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
FROM ubuntu:24.04 AS k3s
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
FROM ubuntu:24.04 AS cpu
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
LABEL org.opencontainers.image.version=0.1.0
ENTRYPOINT ["/opt/llm/bin/entrypoint.sh"]

# ---------------------------------------------------------------- gpu
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04 AS gpu
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
LABEL org.opencontainers.image.version=0.1.0
ENTRYPOINT ["/opt/llm/bin/entrypoint.sh"]
