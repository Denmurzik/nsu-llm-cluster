#!/usr/bin/env bash
# SEC-2 M2: Redis of main requires the password from llm-secrets, and every client of it authenticates.
# Runs the redis container exactly as the rendered manifest starts it (command, args, env), then the probe, the
# wait-redis init container, the clients of LiteLLM (router, coordination and auth-cache RedisCache: REDIS_PASSWORD from
# the environment) and the overload hook URL, with the real redis:7-alpine and LiteLLM images. Needs docker.
set -uo pipefail
export MSYS_NO_PATHCONV=1

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LITELLM_IMAGE="${LITELLM_IMAGE:-ghcr.io/berriai/litellm:v1.106.6}"
REDIS_IMAGE="${REDIS_IMAGE:-redis:7-alpine}"
NAME="nsu-llm-redis-auth-$$"
PW="$(openssl rand -hex 24)"
W="$(mktemp -d)"
FAILED=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; FAILED=1; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
REPO_MOUNT="$REPO"; command -v cygpath >/dev/null 2>&1 && REPO_MOUNT="$(cygpath -m "$REPO")"

# render the manifests with the python of the LiteLLM image (pyyaml), print the redis container as JSON
spec="$(docker run --rm -v "$REPO_MOUNT:/repo:ro" --entrypoint python \
  -e POSTGRES_PASSWORD=a -e LITELLM_DB_PASSWORD=b -e PORTAL_DB_PASSWORD=c -e LITELLM_MASTER_KEY=sk-d -e LITELLM_SALT_KEY=sk-e \
  -e BACKEND_API_KEY=f -e HF_MIRROR_TOKEN=hf_0123456789abcdef0123456789abcdef01234567 -e REDIS_PASSWORD="$PW" \
  "$LITELLM_IMAGE" -c '
import json, subprocess, sys, yaml
subprocess.run([sys.executable, "/repo/main/render_manifests.py", "--out", "/tmp/m"], check=True, stdout=subprocess.DEVNULL)
docs = list(yaml.safe_load_all(open("/tmp/m/common-25-redis.yaml")))
c = [d for d in docs if d["kind"] == "Deployment"][0]["spec"]["template"]["spec"]["containers"][0]
lit = [d for d in yaml.safe_load_all(open("/tmp/m/common-30-litellm.yaml")) if d["kind"] == "Deployment"][0]
init = [i for i in lit["spec"]["template"]["spec"]["initContainers"] if i["name"] == "wait-redis"][0]
print(json.dumps({"command": c["command"], "args": c["args"], "probe": c["readinessProbe"]["exec"]["command"],
                  "wait": init["command"]}))
' 2>&1)" || { echo "render failed: $spec"; exit 1; }
field() { python3 -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print("\0".join(v), end="")' "$spec" "$1"; }
mapfile -d '' cmd < <(field command)
mapfile -d '' args < <(field args)
mapfile -d '' probe < <(field probe)
mapfile -d '' wait_cmd < <(field wait)

docker run -d --name "$NAME" -e REDIS_PASSWORD="$PW" --entrypoint "${cmd[0]}" "$REDIS_IMAGE" "${cmd[@]:1}" "${args[@]}" >/dev/null \
  || { echo "redis did not start"; exit 1; }
for _ in $(seq 1 30); do docker exec -e REDISCLI_AUTH="$PW" "$NAME" redis-cli -h 127.0.0.1 ping 2>/dev/null | grep -qx PONG && break; sleep 1; done
on_redis() { docker run --rm --network "container:$NAME" "$@"; }

out="$(docker exec "$NAME" redis-cli -h 127.0.0.1 -p 6379 ping 2>&1)"
grep -q NOAUTH <<<"$out" && pass "redis without the password: NOAUTH" || fail "redis without the password: NOAUTH" "$out"
out="$(docker exec "$NAME" ps -o args 2>&1)"
! grep -q "$PW" <<<"$out" && grep -q 'redis-server' <<<"$out" && pass "the password is not in any argv" || fail "the password is not in any argv" "$out"
docker exec -e REDISCLI_AUTH="$PW" "$NAME" "${probe[@]}" && pass "probe with REDISCLI_AUTH passes" || fail "probe with REDISCLI_AUTH passes"
docker exec "$NAME" "${probe[@]}" && fail "probe without the password must fail" || pass "probe without the password fails (PONG, not the exit code)"
timeout 20 docker run --rm --network "container:$NAME" -e REDISCLI_AUTH="$PW" --entrypoint "${wait_cmd[0]}" "$REDIS_IMAGE" "${wait_cmd[@]:1}" >/dev/null 2>&1 \
  && pass "wait-redis init container passes with the password" || fail "wait-redis init container passes with the password"

client='
import asyncio, os, sys
from litellm.caching.redis_cache import RedisCache
from litellm import Router
async def main():
    c = RedisCache(host="127.0.0.1", port=6379)
    await c.async_set_cache("sec2-k", "v")
    assert await c.async_get_cache("sec2-k") == "v"
    r = Router(model_list=[{"model_name": "m", "litellm_params": {"model": "openai/m", "api_base": "http://127.0.0.1:1", "api_key": "x"}}],
               redis_host="127.0.0.1", redis_port=6379)
    await r.cache.redis_cache.async_set_cache("sec2-r", "w")
    assert await r.cache.redis_cache.async_get_cache("sec2-r") == "w"
    from redis import asyncio as aioredis
    h = aioredis.Redis.from_url(os.environ["GATEWAY_STATS_REDIS_URL"])
    assert await h.incr("sec2-c") >= 1
    print("CLIENTS_OK")
asyncio.run(main())
'
out="$(on_redis -e REDIS_PASSWORD="$PW" -e GATEWAY_STATS_REDIS_URL="redis://:$PW@127.0.0.1:6379/0" --entrypoint python "$LITELLM_IMAGE" -c "$client" 2>&1)"
grep -q CLIENTS_OK <<<"$out" && pass "LiteLLM RedisCache, Router and the overload hook URL authenticate (REDIS_PASSWORD from the environment)" \
  || fail "LiteLLM clients authenticate" "$(tail -5 <<<"$out")"
out="$(on_redis -e GATEWAY_STATS_REDIS_URL="redis://127.0.0.1:6379/0" --entrypoint python "$LITELLM_IMAGE" -c "$client" 2>&1)"
! grep -q CLIENTS_OK <<<"$out" && pass "without REDIS_PASSWORD the LiteLLM clients are refused" || fail "without the password LiteLLM must be refused" "$out"

if [ "$FAILED" = 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED"; exit 1; fi
