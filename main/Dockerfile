# syntax=docker/dockerfile:1
# NSU LLM Cluster main image: k3s server (PID 1) + supervisord (Caddy, FastAPI portal with the web UI).
# Build context is the repository root:
#   docker build --provenance=false -f main/Dockerfile -t nsu-llm-main:0.1.0 .

ARG K3S_VERSION=v1.36.5+k3s1

# ---------------------------------------------------------------- web
FROM node:22-bookworm AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------- portal
# Venv built here and copied as is: the final stage has the same python3.12 at the same path.
FROM ubuntu:24.04 AS portal
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3.12 python3.12-venv \
 && rm -rf /var/lib/apt/lists/*
RUN python3.12 -m venv /opt/llm/portal/.venv
COPY portal/requirements.lock /tmp/requirements.lock
RUN /opt/llm/portal/.venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock
COPY portal/pyproject.toml /src/portal/pyproject.toml
COPY portal/app /src/portal/app
RUN /opt/llm/portal/.venv/bin/pip install --no-cache-dir --no-deps /src/portal \
 && /opt/llm/portal/.venv/bin/pip check
COPY portal/alembic.ini /opt/llm/portal/alembic.ini
COPY portal/migrations /opt/llm/portal/migrations

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

# ---------------------------------------------------------------- caddy
FROM caddy:2.10-alpine AS caddy

# ---------------------------------------------------------------- final
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3.12 supervisor iptables iproute2 ca-certificates curl openssl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=k3s /k3s /usr/local/bin/k3s
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY --from=portal /opt/llm/portal /opt/llm/portal
COPY --from=web /web/dist /opt/llm/web
COPY main/render_manifests.py main/hf_mirror.py /opt/llm/main/
COPY main/manifests /opt/llm/main/manifests
COPY main/litellm /opt/llm/main/litellm
COPY main/backup /opt/llm/main/backup
COPY main/Caddyfile.tmpl /opt/llm/main/Caddyfile.tmpl
COPY main/entrypoint.sh /opt/llm/main/entrypoint.sh
COPY main/api-advertise-ip.py /opt/llm/main/api-advertise-ip.py
COPY main/agent-token.sh /opt/llm/main/agent-token.sh
COPY main/supervisord.conf /etc/llm/supervisord.conf
RUN chmod +x /opt/llm/main/entrypoint.sh /opt/llm/main/api-advertise-ip.py /opt/llm/main/agent-token.sh /opt/llm/main/backup/pg-restore.sh \
 && mkdir -p /var/lib/llm /run/llm
LABEL org.opencontainers.image.version=0.1.0
ENTRYPOINT ["/opt/llm/main/entrypoint.sh"]
