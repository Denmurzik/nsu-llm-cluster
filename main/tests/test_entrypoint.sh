#!/usr/bin/env bash
# Entrypoint and Caddyfile tests against the built image (nsu-llm-main:0.2.0), mostly in dry-run mode: no k3s is started.
# Build first: docker build --provenance=false -f main/Dockerfile -t nsu-llm-main:0.2.0 .
set -uo pipefail
export MSYS_NO_PATHCONV=1

IMAGE="${IMAGE:-nsu-llm-main:0.2.0}"
VOL="nsu-llm-it14-ep-data-$$"
CERTS_DIR="$(mktemp -d)"
CERTS_MOUNT="$CERTS_DIR"; command -v cygpath >/dev/null 2>&1 && CERTS_MOUNT="$(cygpath -m "$CERTS_DIR")"
FAILED=0

cleanup() {
  docker volume rm -f "$VOL" "$VOL-pg" >/dev/null 2>&1
  rm -rf "$CERTS_DIR"
}
trap cleanup EXIT

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "       $2"; FAILED=1; }
check() { # check NAME CONDITION-EXIT-CODE [DETAIL]
  if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi
}

BASE_ENV=(-e MAIN_ADVERTISE_IP=192.168.3.21 -e OIDC_CLIENT_SECRET=test-oidc-secret -e PUBLIC_HOSTNAME=llm.test.local)

# ep_run [docker run args...] -- [shell snippet run after the entrypoint, same container]
ep_run() {
  local dargs=() snippet="true"
  while [ $# -gt 0 ]; do
    if [ "$1" = "--" ]; then shift; snippet="$*"; break; fi
    dargs+=("$1"); shift
  done
  docker run --rm -v "$VOL:/var/lib/llm" -e LLM_ENTRYPOINT_DRY_RUN=1 "${dargs[@]}" \
    --entrypoint sh "$IMAGE" -c "/opt/llm/main/entrypoint.sh; rc=\$?; echo \"rc=\$rc\"; $snippet" 2>&1
}

# 1. required variables: exit 64 and the message names the variable
out="$(docker run --rm -e LLM_ENTRYPOINT_DRY_RUN=1 -e OIDC_CLIENT_SECRET=x -e PUBLIC_HOSTNAME=h "$IMAGE" 2>&1)"; rc=$?
[ "$rc" = 64 ] && grep -q MAIN_ADVERTISE_IP <<<"$out"
check "missing MAIN_ADVERTISE_IP -> exit 64 naming the variable" $? "rc=$rc out=$out"

out="$(docker run --rm -e LLM_ENTRYPOINT_DRY_RUN=1 -e MAIN_ADVERTISE_IP=1.2.3.4 -e PUBLIC_HOSTNAME=h "$IMAGE" 2>&1)"; rc=$?
[ "$rc" = 64 ] && grep -q OIDC_CLIENT_SECRET <<<"$out"
check "missing OIDC_CLIENT_SECRET -> exit 64 naming the variable" $? "rc=$rc out=$out"

out="$(docker run --rm -e LLM_ENTRYPOINT_DRY_RUN=1 -e MAIN_ADVERTISE_IP=1.2.3.4 -e OIDC_CLIENT_SECRET=x "$IMAGE" 2>&1)"; rc=$?
[ "$rc" = 64 ] && grep -q PUBLIC_HOSTNAME <<<"$out"
check "missing PUBLIC_HOSTNAME -> exit 64 naming the variable" $? "rc=$rc out=$out"

out="$(docker run --rm -e LLM_ENTRYPOINT_DRY_RUN=1 -e MAIN_ADVERTISE_IP=1.2.3.4 -e OIDC_CLIENT_SECRET=x -e 'PUBLIC_HOSTNAME=evil.example { respond 200 }' "$IMAGE" 2>&1)"; rc=$?
[ "$rc" = 64 ] && grep -q PUBLIC_HOSTNAME <<<"$out"
check "unsafe PUBLIC_HOSTNAME is rejected (Caddyfile injection)" $? "rc=$rc out=$out"

# 2. first run: secrets.env created, mode 600, master key sk-<hex32>
out1="$(ep_run "${BASE_ENV[@]}" -- 'stat -c "mode=%a" /var/lib/llm/secrets.env; cat /var/lib/llm/secrets.env')"
grep -q '^rc=0$' <<<"$out1" && grep -q '^mode=600$' <<<"$out1" && grep -Eq '^LITELLM_MASTER_KEY=sk-[0-9a-f]{32}$' <<<"$out1"
check "first run creates secrets.env (600, master key sk-<hex32>)" $? "$out1"
for v in LITELLM_SALT_KEY POSTGRES_PASSWORD LITELLM_DB_PASSWORD PORTAL_DB_PASSWORD CLUSTER_TOKEN AGENT_TOKEN BACKEND_API_KEY; do
  grep -Eq "^$v=[A-Za-z0-9._~-]+$" <<<"$out1"
  check "secrets.env has $v" $? "$out1"
done
grep -Eq '^LITELLM_SALT_KEY=sk-[0-9a-f]{32}$' <<<"$out1"
check "salt key is sk-<hex32>" $?
grep -Eq '^HF_MIRROR_TOKEN=hf_[0-9a-f]{40}$' <<<"$out1"
check "mirror token is hf_<hex40> (the shape of a Hugging Face token: llama.cpp sends only such a token)" $? "$out1"

# 3. second run keeps the values
out2="$(ep_run "${BASE_ENV[@]}" -- 'cat /var/lib/llm/secrets.env')"
[ "$(grep -E '^[A-Z_]+=' <<<"$out1" | grep -v '^mode=' | grep -v '^rc=' | sort)" = "$(grep -E '^[A-Z_]+=' <<<"$out2" | grep -v '^rc=' | sort)" ]
check "second run keeps the same secrets" $?

# 4. env wins over the stored value, and is persisted (no k3s cluster yet: the token is not baked into its data)
out3="$(ep_run "${BASE_ENV[@]}" -e CLUSTER_TOKEN=from-env-token -- 'grep ^CLUSTER_TOKEN= /var/lib/llm/secrets.env; grep CLUSTER_TOKEN /run/llm/portal.env')"
grep -q '^CLUSTER_TOKEN=from-env-token$' <<<"$out3" && grep -q 'CLUSTER_TOKEN=from-env-token' <<<"$out3"
check "env wins before k3s has data: CLUSTER_TOKEN from the environment is used" $? "$out3"

# 4b. agent token (k3s --agent-token): its own hex secret, never the server token, handed to the portal
agent1="$(sed -n 's/^AGENT_TOKEN=//p' <<<"$out1")"; server1="$(sed -n 's/^CLUSTER_TOKEN=//p' <<<"$out1")"
[[ "$agent1" =~ ^[0-9a-f]{64}$ ]] && [ "$agent1" != "$server1" ]
check "AGENT_TOKEN is hex64 and differs from CLUSTER_TOKEN" $? "agent=$agent1 server=$server1"
out3b="$(ep_run "${BASE_ENV[@]}" -- 'grep -E "^(AGENT_TOKEN|CLUSTER_TOKEN|NODE_LEGACY_TOKEN)=" /run/llm/portal.env')"
grep -Eq '^AGENT_TOKEN=[0-9a-f]{64}$' <<<"$out3b" && ! grep -q '^CLUSTER_TOKEN=' <<<"$out3b" \
  && grep -q '^NODE_LEGACY_TOKEN=false$' <<<"$out3b"
check "fresh install: portal.env has AGENT_TOKEN, NODE_LEGACY_TOKEN defaults to false and the server token stays out (SEC-2 L3)" $? "$out3b"
# an install from before this version (secrets.env without NODE_LEGACY_TOKEN_DEFAULT) keeps taking the server token, and
# remembers it; NODE_LEGACY_TOKEN in .env still wins
out3b2="$(ep_run "${BASE_ENV[@]}" -- 'S=/var/lib/llm/secrets.env; sed -i /^NODE_LEGACY_TOKEN_DEFAULT=/d $S;
  /opt/llm/main/entrypoint.sh >/dev/null; grep -E "^(CLUSTER_TOKEN|NODE_LEGACY_TOKEN)=" /run/llm/portal.env | sed "s/^/RUN1 /";
  /opt/llm/main/entrypoint.sh >/dev/null; grep ^NODE_LEGACY_TOKEN= /run/llm/portal.env | sed "s/^/RUN2 /"; grep ^NODE_LEGACY_TOKEN_DEFAULT= $S;
  NODE_LEGACY_TOKEN=false /opt/llm/main/entrypoint.sh >/dev/null; grep -c ^CLUSTER_TOKEN= /run/llm/portal.env | sed "s/^/RUN3 /"')"
grep -q '^RUN1 CLUSTER_TOKEN=from-env-token$' <<<"$out3b2" && grep -q '^RUN1 NODE_LEGACY_TOKEN=true$' <<<"$out3b2" \
  && grep -q '^RUN2 NODE_LEGACY_TOKEN=true$' <<<"$out3b2" && grep -q '^NODE_LEGACY_TOKEN_DEFAULT=true$' <<<"$out3b2" \
  && grep -q '^RUN3 0$' <<<"$out3b2"
check "install from before SEC-2: NODE_LEGACY_TOKEN defaults to true, remembered; .env false still wins" $? "$out3b2"
# SEC-2 L2: the mirror key of an install from before SEC-2 was in llm-node-secrets (every node knows it): replaced once, then kept
out3m="$(ep_run "${BASE_ENV[@]}" -- 'S=/var/lib/llm/secrets.env; sed -i /^HF_MIRROR_TOKEN_MAIN_ONLY=/d $S; sed -n "s/^HF_MIRROR_TOKEN=/OLD /p" $S;
  /opt/llm/main/entrypoint.sh 2>&1 | grep -c "HF_MIRROR_TOKEN заменён" | sed "s/^/MSG /"; sed -n "s/^HF_MIRROR_TOKEN=/NEW /p" $S;
  /opt/llm/main/entrypoint.sh >/dev/null 2>&1; sed -n "s/^HF_MIRROR_TOKEN=/AGAIN /p" $S; grep -c ^HF_MIRROR_TOKEN_MAIN_ONLY=1$ $S')"
m_old="$(sed -n 's/^OLD //p' <<<"$out3m")"; m_new="$(sed -n 's/^NEW //p' <<<"$out3m")"; m_again="$(sed -n 's/^AGAIN //p' <<<"$out3m")"
[[ "$m_new" =~ ^hf_[0-9a-f]{40}$ ]] && [ "$m_old" != "$m_new" ] && [ "$m_new" = "$m_again" ] && grep -q '^MSG 1$' <<<"$out3m" && grep -q '^1$' <<<"$out3m"
check "install from before SEC-2: HF_MIRROR_TOKEN (known to every node) is replaced once and then kept" $? "$out3m"
# the old value set in .env (review I4): refused with a working fix; removing it from .env gives a new key; re-adding it is refused
out3n="$(ep_run "${BASE_ENV[@]}" -- 'S=/var/lib/llm/secrets.env; sed -i -e /^HF_MIRROR_TOKEN_MAIN_ONLY=/d -e /^HF_MIRROR_TOKEN_LEAKED=/d $S;
  old=$(sed -n "s/^HF_MIRROR_TOKEN=//p" $S); echo "OLD $old";
  HF_MIRROR_TOKEN=$old /opt/llm/main/entrypoint.sh >/tmp/o1 2>&1; echo "RC1 $?"; grep -c "уберите строку\|Уберите строку" /tmp/o1 | sed "s/^/MSG /";
  /opt/llm/main/entrypoint.sh >/dev/null 2>&1; echo "RC2 $?"; sed -n "s/^HF_MIRROR_TOKEN=/NEW /p" $S;
  HF_MIRROR_TOKEN=$old /opt/llm/main/entrypoint.sh >/dev/null 2>&1; echo "RC3 $?";
  HF_MIRROR_TOKEN=hf_0123456789abcdef0123456789abcdef01234567 /opt/llm/main/entrypoint.sh >/dev/null 2>&1; echo "RC4 $?"')"
n_old="$(sed -n 's/^OLD //p' <<<"$out3n")"; n_new="$(sed -n 's/^NEW //p' <<<"$out3n")"
grep -q '^RC1 64$' <<<"$out3n" && grep -q '^MSG 1$' <<<"$out3n" && grep -q '^RC2 0$' <<<"$out3n" && [[ "$n_new" =~ ^hf_[0-9a-f]{40}$ ]] \
  && [ "$n_new" != "$n_old" ] && grep -q '^RC3 64$' <<<"$out3n" && grep -q '^RC4 0$' <<<"$out3n"
check "old HF_MIRROR_TOKEN in .env: exit 64 with the fix; without it a new key; the old value is refused later too, a new one accepted" $? "$out3n"
out3c="$(ep_run "${BASE_ENV[@]}" -e NODE_LEGACY_TOKEN=false -- 'grep ^NODE_LEGACY_TOKEN= /run/llm/portal.env')"
grep -q '^NODE_LEGACY_TOKEN=false$' <<<"$out3c"
check "portal.env passes NODE_LEGACY_TOKEN" $? "$out3c"
out3c2="$(ep_run "${BASE_ENV[@]}" -e NODE_LEGACY_TOKEN=false -- 'grep -c CLUSTER_TOKEN /run/llm/portal.env; grep -E "^AGENT_TOKEN=" /run/llm/portal.env; grep -E "^CLUSTER_TOKEN=" /var/lib/llm/secrets.env')"
grep -q '^0$' <<<"$out3c2" && grep -Eq '^AGENT_TOKEN=[0-9a-f]{64}$' <<<"$out3c2" && grep -q '^CLUSTER_TOKEN=' <<<"$out3c2"
check "NODE_LEGACY_TOKEN=false: CLUSTER_TOKEN is not passed to the portal (still in secrets.env for k3s)" $? "$out3c2"
out3d="$(ep_run "${BASE_ENV[@]}" -e AGENT_TOKEN=from-env-token -- 'true')"
grep -q '^rc=64$' <<<"$out3d" && grep -q 'AGENT_TOKEN' <<<"$out3d" && grep -q 'CLUSTER_TOKEN' <<<"$out3d"
check "AGENT_TOKEN equal to CLUSTER_TOKEN -> exit 64" $? "$out3d"
# migration: secrets.env of an install from before the agent token gets one, once; the other secrets stay
out3e="$(ep_run "${BASE_ENV[@]}" -- 'S=/var/lib/llm/secrets.env; sed -i /^AGENT_TOKEN=/d $S; cp $S /tmp/old;
  /opt/llm/main/entrypoint.sh; sed -n "s/^AGENT_TOKEN=/RUN1 /p" $S;
  /opt/llm/main/entrypoint.sh; sed -n "s/^AGENT_TOKEN=/RUN2 /p" $S;
  grep -v ^AGENT_TOKEN= $S | cmp -s - /tmp/old && echo SAME_OTHERS')"
run1="$(sed -n 's/^RUN1 //p' <<<"$out3e")"; run2="$(sed -n 's/^RUN2 //p' <<<"$out3e")"
grep -q '^SAME_OTHERS$' <<<"$out3e" && [[ "$run1" =~ ^[0-9a-f]{64}$ ]] && [ "$run1" != "$agent1" ] && [ "$run1" = "$run2" ]
check "existing install without AGENT_TOKEN: generated once and kept, other secrets unchanged" $? "$out3e"

# 5. rendered manifests, Caddyfile and portal env
out4="$(ep_run "${BASE_ENV[@]}" -- 'ls /var/lib/rancher/k3s/server/manifests; echo ---CADDY; cat /run/llm/Caddyfile; echo ---PORTAL; cat /run/llm/portal.env; stat -c "portal.env mode=%a" /run/llm/portal.env')"
grep -q 'common-30-litellm.yaml' <<<"$out4"
check "manifests contain common-30-litellm.yaml" $?
grep -q 'llm.test.local' <<<"$out4" && grep -q 'flush_interval -1' <<<"$out4" && grep -q 'tls internal' <<<"$out4" && grep -q 'local_certs' <<<"$out4"
check "Caddyfile has hostname, flush_interval -1, tls internal, local_certs" $? "$out4"
grep -q '192.168.3.21' <<<"$out4"
check "Caddyfile has the advertise IP" $?
[ "$(grep -c 'keepalive 4s' <<<"$out4")" -ge 6 ]   # LiteLLM, portal, uploads, Playground, HF mirror, registry (later tasks may add more)
check "Caddyfile: upstream keep-alive 4 s for LiteLLM, the portal, its upload and Playground routes, the HF mirror and the registry (below uvicorn's 5 s)" $? "$out4"
grep -A2 'handle /api/playground/chat' <<<"$out4" | grep -q 'max_size 24MB'
check "Caddyfile: the Playground chat route takes up to 24 MB, the rest of the portal 1 MB" $? "$out4"
[ "$(grep -c 'max_size 1MB' <<<"$out4")" = 1 ]
check "Caddyfile: only the catch-all portal route stays at 1 MB" $? "$out4"
grep -q 'handle /v2/\*' <<<"$out4" && grep -q 'reverse_proxy 127.0.0.1:5001' <<<"$out4" \
  && grep -q 'not client_ip 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 ::1 ::ffff:a00:0/104 ::ffff:ac10:0/108 ::ffff:c0a8:0/112 ::ffff:7f00:0/104$' <<<"$out4" \
  && grep -q '@registry_write not method GET HEAD' <<<"$out4" && grep -q 'common-50-registry.yaml' <<<"$out4" && ! grep -qE '@@[A-Z_]+@@' <<<"$out4"
check "registry: /v2/ proxied to the registry on 127.0.0.1:5001, read-only, limited to the private subnets by default, manifest rendered" $? "$out4"
# SEC-2 M2: the registry needs the nsu-push password; Caddy adds it for reads (/v2/ and the loopback read listener on
# REGISTRY_PORT), the Secret carries only its bcrypt hash, the hash is stable across restarts, registry-push.sh finds the port
out4a="$(ep_run "${BASE_ENV[@]}" -- 'S=/var/lib/llm/secrets.env; pw=$(sed -n s/^REGISTRY_PASSWORD=//p $S); echo "PW=$pw";
  M=/var/lib/rancher/k3s/server/manifests/common-50-registry.yaml; sed -n "s/^  htpasswd: //p" $M | sed "s/^/H1 /";
  grep -c "header_up Authorization \"Basic $(printf nsu-push:%s "$pw" | base64 -w0)\"" /run/llm/Caddyfile | sed "s/^/AUTHS /";
  grep -A1 "^http://127.0.0.1:5000 {" /run/llm/Caddyfile; cat /run/llm/registry.env; grep -c "$pw" $M | sed "s/^/PW_IN_MANIFEST /";
  stat -c "caddyfile mode=%a" /run/llm/Caddyfile;
  /opt/llm/main/entrypoint.sh >/dev/null; sed -n "s/^  htpasswd: //p" $M | sed "s/^/H2 /"')"
pw="$(sed -n 's/^PW=//p' <<<"$out4a")"; h1="$(sed -n 's/^H1 //p' <<<"$out4a")"; h2="$(sed -n 's/^H2 //p' <<<"$out4a")"
[[ "$pw" =~ ^[0-9a-f]{48}$ ]] && [[ "$h1" =~ ^\"nsu-push:\$2a\$06\$ ]] && [ "$h1" = "$h2" ] && grep -q '^AUTHS 2$' <<<"$out4a" \
  && grep -q '^http://127.0.0.1:5000 {' <<<"$out4a" && grep -q 'bind 127.0.0.1' <<<"$out4a" \
  && grep -q '^REGISTRY_BACKEND_PORT=5001$' <<<"$out4a" && grep -q '^REGISTRY_USER=nsu-push$' <<<"$out4a" \
  && grep -q '^PW_IN_MANIFEST 0$' <<<"$out4a" && grep -q 'caddyfile mode=600' <<<"$out4a"
check "registry auth: REGISTRY_PASSWORD generated, Secret has only a stable bcrypt hash, Caddy adds the password for reads on /v2/ and 127.0.0.1:5000" $? "$out4a"
out4r="$(ep_run "${BASE_ENV[@]}" -e REGISTRY_PORT=5111 -e REGISTRY_BACKEND_PORT=5222 -e 'REGISTRY_ALLOW_FROM=10.1.0.0/16, 192.168.5.7' -- 'cat /run/llm/Caddyfile; cat /run/llm/registry.env')"
grep -q 'reverse_proxy 127.0.0.1:5222' <<<"$out4r" && grep -q '^http://127.0.0.1:5111 {' <<<"$out4r" && grep -q 'not client_ip 10.1.0.0/16 192.168.5.7$' <<<"$out4r" \
  && grep -q '^REGISTRY_BACKEND_PORT=5222$' <<<"$out4r"
check "REGISTRY_PORT, REGISTRY_BACKEND_PORT and REGISTRY_ALLOW_FROM reach the Caddyfile" $? "$out4r"
# SEC-2 L1: the node API only from the class subnets (default: private ranges), before the catch-all portal route
p_nodes="$(grep -n 'path /api/nodes /api/nodes/\*' <<<"$out4" | cut -d: -f1)"; p_all="$(grep -n '^	handle {$' <<<"$out4" | head -n1 | cut -d: -f1)"
[ -n "$p_nodes" ] && [ -n "$p_all" ] && [ "$p_nodes" -lt "$p_all" ] && grep -q 'API нод доступен только из сети класса' <<<"$out4"
check "Caddyfile: /api/nodes only from NODE_ALLOW_FROM (private subnets by default), ahead of the portal route" $? "$out4"
out4n="$(ep_run "${BASE_ENV[@]}" -e 'NODE_ALLOW_FROM=10.20.0.0/16, 10.4.1.20' -- 'grep -A2 "path /api/nodes" /run/llm/Caddyfile')"
grep -q 'not client_ip 10.20.0.0/16 10.4.1.20$' <<<"$out4n"
check "NODE_ALLOW_FROM reaches the Caddyfile" $? "$out4n"
out4m="$(ep_run "${BASE_ENV[@]}" -e 'REGISTRY_ALLOW_FROM=203.0.113.0/24' -- 'grep -A2 "path /api/nodes" /run/llm/Caddyfile')"
grep -q 'not client_ip 203.0.113.0/24$' <<<"$out4m"
check "NODE_ALLOW_FROM not set: the subnets of REGISTRY_ALLOW_FROM (a class in a public subnet keeps working after the update)" $? "$out4m"
for bad in 'NODE_ALLOW_FROM=,' 'NODE_ALLOW_FROM=evil.example' 'NODE_ALLOW_FROM=10.0.0.0/8 }'; do
  outb="$(ep_run "${BASE_ENV[@]}" -e "$bad" -- 'true')"
  grep -q '^rc=64$' <<<"$outb" && grep -q "NODE_ALLOW_FROM" <<<"$outb"
  check "$bad -> exit 64 naming the variable" $? "$outb"
done
for bad in REGISTRY_PORT=80 REGISTRY_PORT=x REGISTRY_BACKEND_PORT=80 REGISTRY_BACKEND_PORT=5000 'REGISTRY_ALLOW_FROM=10.0.0.0/8 }' 'REGISTRY_ALLOW_FROM=,' 'REGISTRY_ALLOW_FROM=evil.example'; do
  outb="$(ep_run "${BASE_ENV[@]}" -e "$bad" -- 'true')"
  grep -q '^rc=64$' <<<"$outb" && grep -q "REGISTRY_" <<<"$outb"
  check "$bad -> exit 64 naming the variable" $? "$outb"
done
grep -q 'handle_path /hf/\*' <<<"$out4" && grep -q 'reverse_proxy 127.0.0.1:8090' <<<"$out4" \
  && grep -q 'common-40-hf-mirror.yaml' <<<"$out4" && grep -Eq '^HF_MIRROR_TOKEN=hf_[0-9a-f]{40}$' <<<"$out4" \
  && grep -q '^HF_MIRROR_PORT=8090$' <<<"$out4"
check "HF mirror: /hf/ proxied to 127.0.0.1:8090, manifest rendered, token and port in portal.env" $? "$out4"
! grep -q 'common-15-ca.yaml' <<<"$out4"
check "internal CA not created yet (dry run): no llm-ca ConfigMap written before Caddy made its root" $? "$out4"
out4m="$(ep_run "${BASE_ENV[@]}" -e HF_MIRROR_PORT=8191 -e HF_MIRROR_MAX_GIB=2000 -- 'cat /run/llm/Caddyfile; grep -A1 "name: HF_MIRROR_MAX_GIB" /var/lib/rancher/k3s/server/manifests/common-40-hf-mirror.yaml')"
grep -q 'reverse_proxy 127.0.0.1:8191' <<<"$out4m" && grep -q '"2000"' <<<"$out4m"
check "HF_MIRROR_PORT and HF_MIRROR_MAX_GIB reach Caddy and the mirror manifest" $? "$out4m"
for bad in HF_MIRROR_PORT=80 HF_MIRROR_PORT=x; do
  outb="$(ep_run "${BASE_ENV[@]}" -e "$bad" -- 'true')"
  grep -q '^rc=64$' <<<"$outb" && grep -q "HF_MIRROR_PORT" <<<"$outb"
  check "$bad -> exit 64 naming the variable" $? "$outb"
done
outp="$(ep_run "${BASE_ENV[@]}" -e HTTPS_PROXY=http://proxy.nsu.ru:3128 -e NO_PROXY=.nsu.ru -- 'grep -i "^no_proxy=" /run/llm/portal.env; grep -A1 "name: NO_PROXY" /var/lib/rancher/k3s/server/manifests/common-40-hf-mirror.yaml')"
# portal.env is written with printf %q, which escapes the commas
grep -qxF 'NO_PROXY=.nsu.ru\,127.0.0.1\,localhost\,::1' <<<"$outp" && grep -qxF 'no_proxy=.nsu.ru\,127.0.0.1\,localhost\,::1' <<<"$outp" \
  && grep -qF '{name: NO_PROXY, value: ".nsu.ru,127.0.0.1,localhost,::1"}' <<<"$outp"
check "with a proxy, 127.0.0.1, localhost and ::1 bypass it (portal.env and the mirror pod)" $? "$outp"
outs="$(ep_run "${BASE_ENV[@]}" -e HF_TOKEN=hf_realtoken -- 'cat /var/lib/rancher/k3s/server/manifests/common-10-secrets.yaml')"
node_secret="$(sed -n '/name: llm-node-secrets/,$p' <<<"$outs")"
grep -q '^  HF_TOKEN: ""$' <<<"$node_secret" \
  && ! grep -q 'LITELLM_MASTER_KEY\|SALT\|PASSWORD\|DATABASE_URL\|hf_realtoken\|BACKEND_API_KEY\|HF_MIRROR_TOKEN' <<<"$node_secret"
check "llm-node-secrets (read by the nodes' kubelets) holds no key of main and, with the mirror on, no HF token" $? "$outs"
outb="$(ep_run "${BASE_ENV[@]}" -e HF_MIRROR_MAX_GIB=0 -- 'true')"
grep -q '^rc=64$' <<<"$outb" && grep -q "HF_MIRROR_MAX_GIB" <<<"$outb"
check "HF_MIRROR_MAX_GIB=0 -> exit 64 naming the variable" $? "$outb"
grep -q '^DEFAULT_MODEL_FILE=gemma-4-E4B-it-Q4_K_M.gguf$' <<<"$out4"
check "portal.env has DEFAULT_MODEL_FILE" $? "$out4"
grep -Eq '^DATABASE_URL=postgresql\+asyncpg://portal:[A-Za-z0-9._~-]+@127.0.0.1:5432/portal$' <<<"$out4" \
  && grep -q '^WEB_DIST=/opt/llm/web$' <<<"$out4" && grep -q '^PUBLIC_HOSTNAME=llm.test.local$' <<<"$out4" \
  && grep -q '^MIGRATIONS_DIR=/opt/llm/portal$' <<<"$out4" && grep -q '^OIDC_CLIENT_SECRET=test-oidc-secret$' <<<"$out4"
check "portal.env has database url, web dist, migrations dir, hostname, oidc secret" $? "$out4"
out4b="$(ep_run "${BASE_ENV[@]}" -e ADMIN_REQUIRE_EMAIL_VERIFIED=false -- 'cat /run/llm/portal.env')"
grep -q '^ADMIN_REQUIRE_EMAIL_VERIFIED=false$' <<<"$out4b"
check "portal.env passes ADMIN_REQUIRE_EMAIL_VERIFIED" $? "$out4b"
# no provider defaults: without OIDC_ISSUER / OIDC_CLIENT_ID main starts, says so loudly and never writes a provider of its own
! grep -q '^OIDC_ISSUER=\|^OIDC_CLIENT_ID=\|nsu.ru\|nsuts' <<<"$out4" && grep -q 'не задана переменная OIDC_ISSUER' <<<"$out4" && grep -q 'не задана переменная OIDC_CLIENT_ID' <<<"$out4"
check "no OIDC_ISSUER / OIDC_CLIENT_ID: a warning for each, no NSU value in portal.env" $? "$out4"
out4c="$(ep_run "${BASE_ENV[@]}" -e OIDC_ISSUER=https://sso.example.org/realms/x -e OIDC_CLIENT_ID=cid -e 'LOGIN_PROVIDER_NAME=Campus SSO' -- 'cat /run/llm/portal.env')"
grep -q '^OIDC_ISSUER=https://sso.example.org/realms/x$' <<<"$out4c" && grep -q '^OIDC_CLIENT_ID=cid$' <<<"$out4c"   && grep -qxF 'LOGIN_PROVIDER_NAME=Campus\ SSO' <<<"$out4c" && ! grep -q 'не задана переменная OIDC' <<<"$out4c"
check "portal.env passes OIDC_ISSUER, OIDC_CLIENT_ID and LOGIN_PROVIDER_NAME; no warning when set" $? "$out4c"
grep -q 'portal.env mode=600' <<<"$out4"
check "portal.env is mode 600" $?

# 5b. scale settings: defaults reach the portal, LITELLM_WORKERS reaches the LiteLLM manifest, bad values exit 64
grep -Eq '^REDIS_URL=redis://:[0-9a-f]{48}@127.0.0.1:6379/0$' <<<"$out4" && grep -q '^PORTAL_WORKERS=4$' <<<"$out4" \
  && grep -q '^LITELLM_WORKERS=4$' <<<"$out4" && grep -q '^QUEUE_FACTOR=2$' <<<"$out4" && grep -q 'common-25-redis.yaml' <<<"$out4"
check "portal.env has redis url, workers and queue factor; redis manifest rendered" $? "$out4"
# SEC-2: the portal's own key to the mirror and the signing key reach the portal, differ, and the secrets of Redis and the
# registry stay out of it (the portal reads Redis through REDIS_URL, the registry through Caddy)
adm="$(sed -n 's/^HF_MIRROR_ADMIN_TOKEN=//p' <<<"$out4")"; sig="$(sed -n 's/^HF_MIRROR_TOKEN=//p' <<<"$out4")"
[[ "$adm" =~ ^[0-9a-f]{48}$ ]] && [[ "$sig" =~ ^hf_[0-9a-f]{40}$ ]] && ! grep -q '^REDIS_PASSWORD=\|^REGISTRY_PASSWORD=' <<<"$out4"
check "portal.env: HF_MIRROR_ADMIN_TOKEN and HF_MIRROR_TOKEN (different), no REDIS_PASSWORD or REGISTRY_PASSWORD" $? "adm=$adm sig=$sig"
out4b="$(ep_run "${BASE_ENV[@]}" -e LITELLM_WORKERS=2 -e QUEUE_FACTOR=4 -- 'M=/var/lib/rancher/k3s/server/manifests/common-30-litellm.yaml; grep -A1 -- "- --num_workers" $M; grep "database_connection_pool_limit\|overload_hook" $M; grep QUEUE_FACTOR /run/llm/portal.env')"
grep -q '"2"' <<<"$out4b" && grep -q 'database_connection_pool_limit: 60' <<<"$out4b" \
  && grep -q 'overload_hook.py: |' <<<"$out4b" && grep -q '^QUEUE_FACTOR=4$' <<<"$out4b"
check "LITELLM_WORKERS=2: two LiteLLM workers, pool limit 60, overload hook shipped; QUEUE_FACTOR passed on" $? "$out4b"
grep -q '^DB_POOL_SIZE=10$' <<<"$out4" && grep -q '^DB_MAX_OVERFLOW=20$' <<<"$out4"
check "portal.env has the DB pool defaults (10 + 20)" $? "$out4"
out4c="$(ep_run "${BASE_ENV[@]}" -e DB_POOL_SIZE=20 -e DB_MAX_OVERFLOW=11 -- 'true')"
grep -q '^rc=64$' <<<"$out4c" && grep -q 'DB_POOL_SIZE + DB_MAX_OVERFLOW' <<<"$out4c"
check "DB_POOL_SIZE + DB_MAX_OVERFLOW over 30 -> exit 64" $? "$out4c"
for bad in LITELLM_WORKERS=17 PORTAL_WORKERS=5 QUEUE_FACTOR=0 LITELLM_WORKERS=x DB_POOL_SIZE=0 DB_MAX_OVERFLOW=x; do
  outb="$(ep_run "${BASE_ENV[@]}" -e "$bad" -- 'true')"
  grep -q '^rc=64$' <<<"$outb" && grep -q "${bad%%=*}" <<<"$outb"
  check "$bad -> exit 64 naming the variable" $? "$outb"
done

# 6. custom certificate: no local_certs, tls /certs/tls.crt /certs/tls.key
touch "$CERTS_DIR/tls.crt" "$CERTS_DIR/tls.key"
out5="$(ep_run "${BASE_ENV[@]}" -e TLS_CERT=1 -v "$CERTS_MOUNT:/certs:ro" -- 'cat /run/llm/Caddyfile')"
grep -q 'tls /certs/tls.crt /certs/tls.key' <<<"$out5" && ! grep -q 'local_certs' <<<"$out5" && ! grep -q 'tls internal' <<<"$out5"
check "TLS_CERT: custom certificate, no local_certs" $? "$out5"
# SEC-2 M1: /ca.crt.p7s (the k3s-CA signature of /ca.crt) on both sites; nothing to sign without a /ca.crt
[ "$(grep -A1 'handle /ca.crt.p7s {' <<<"$out5" | grep -c 'respond 404')" = 2 ]
check "TLS_CERT without ca.crt: /ca.crt.p7s answers 404 on both sites" $? "$out5"
[ "$(grep -c 'handle /ca.crt.p7s {' <<<"$out4")" = 2 ] && [ "$(grep -c 'rewrite \* /ca.crt.p7s' <<<"$out4")" = 2 ] \
  && grep -q 'root \* /var/lib/llm/ca-sign' <<<"$out4" && grep -q 'header Content-Type application/pkcs7-signature' <<<"$out4"
check "internal CA: /ca.crt.p7s served from /var/lib/llm/ca-sign on both sites" $? "$out4"
out5s="$(docker run --rm --entrypoint sh "$IMAGE" -c 'test -x /opt/llm/main/sign-ca.sh && openssl version' 2>&1)"
grep -q '^OpenSSL 3' <<<"$out5s"
check "sign-ca.sh is in the image with OpenSSL 3 (cms)" $? "$out5s"
printf -- '-----BEGIN CERTIFICATE-----\nOWNCA\n-----END CERTIFICATE-----\n' >"$CERTS_DIR/ca.crt"
out5c="$(ep_run "${BASE_ENV[@]}" -e TLS_CERT=1 -v "$CERTS_MOUNT:/certs:ro" -- 'cat /var/lib/rancher/k3s/server/manifests/common-15-ca.yaml')"
grep -q 'name: llm-ca' <<<"$out5c" && [ "$(grep -c OWNCA <<<"$out5c")" = 2 ] && grep -q 'bundle.crt: |' <<<"$out5c"
check "own certificate with ca.crt: ConfigMap llm-ca carries it (ca.crt and the system bundle + it)" $? "$out5c"
rm -f "$CERTS_DIR/ca.crt"
out6="$(ep_run "${BASE_ENV[@]}" -e TLS_CERT=1 -- 'true')"
grep -q '^rc=64$' <<<"$out6" && grep -q '/certs/tls.crt' <<<"$out6"
check "TLS_CERT without mounted files -> exit 64" $? "$out6"

# 6b. front proxy (TRUSTED_PROXIES, nginx on tsga): Caddy global option servers { trusted_proxies static ... }
! grep -q '^[[:space:]]*trusted_proxies' <<<"$out4"
check "TRUSTED_PROXIES unset: no trusted_proxies in the Caddyfile" $? "$out4"
out6b="$(ep_run "${BASE_ENV[@]}" -e 'TRUSTED_PROXIES=10.4.0.105, 10.0.0.0/8,fd00::/8' -- \
  'cat /run/llm/Caddyfile; caddy adapt --config /run/llm/Caddyfile --adapter caddyfile >/tmp/adapt.json 2>&1 && echo ADAPT_OK; cat /tmp/adapt.json')"
grep -q '^rc=0$' <<<"$out6b" && [[ "$out6b" == *$'\n\tservers {\n\t\ttrusted_proxies static 10.4.0.105 10.0.0.0/8 fd00::/8\n\t}\n}\n'* ]] \
  && grep -q '^ADAPT_OK$' <<<"$out6b" && grep -qF '"trusted_proxies":{' <<<"$out6b" \
  && grep -qF '"ranges":["10.4.0.105","10.0.0.0/8","fd00::/8"]' <<<"$out6b" && grep -qF '"source":"static"' <<<"$out6b"
check "TRUSTED_PROXIES: list (spaces dropped) lands in servers { trusted_proxies static ... }, caddy adapt accepts it" $? "$out6b"
for bad in '10.4.0.105 }' '10.4.0.105;' 'tsga.nsu.ru' '{env.X}' '10.4.0.105,,10.0.0.1'; do
  outb="$(ep_run "${BASE_ENV[@]}" -e "TRUSTED_PROXIES=$bad" -- 'true')"
  grep -q '^rc=64$' <<<"$outb" && grep -q 'TRUSTED_PROXIES' <<<"$outb"
  check "TRUSTED_PROXIES='$bad' -> exit 64 naming the variable (no Caddyfile injection)" $? "$outb"
done

# 7. API advertise address (main/api-advertise-ip.py): the LAN address only when a nonce handshake on 6443 comes back
#    from inside the container (hairpin), else the container address with a warning.
# aip [VAR=value...] [-- fake-listener python code]: prints "ip=<stdout> rc=<rc> warn=<n> accepted=<n> own=<ip>"
aip() {
  local vars=() fake=""
  while [ $# -gt 0 ]; do
    if [ "$1" = "--" ]; then shift; fake="$*"; break; fi
    vars+=("-e" "$1"); shift
  done
  docker run --rm "${vars[@]}" -e FAKE="$fake" --entrypoint bash "$IMAGE" -c '
    own="$(hostname -i | awk "{print \$1}")"
    [ "${MAIN_ADVERTISE_IP:-}" = own ] && export MAIN_ADVERTISE_IP="$own"
    if [ -n "$FAKE" ]; then python3 -c "$FAKE" & sleep 1; fi
    t0=$(date +%s%N); ip="$(/opt/llm/main/api-advertise-ip.py 2>/tmp/err)"; rc=$?; ms=$(( ($(date +%s%N) - t0) / 1000000 ))
    echo "ip=$ip rc=$rc warn=$(grep -c Предупреждение /tmp/err) accepted=$(grep -c "соединение .* принято" /tmp/err) own=$own ms=$ms"
    cat /tmp/err >&2' 2>&1
}
# a fake service on 0.0.0.0:18444 that accepts and then ANSWERS (python, one connection at a time)
FAKE_SRV='import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", 18444)); s.listen(8)
while True:
    c,_=s.accept(); c.settimeout(2)
    try: line=c.recv(128)
    except OSError: line=b""
    REPLY
    time.sleep(0.1); c.close()'
o="$(aip MAIN_ADVERTISE_IP=own)"
grep -Eq '^ip=([0-9.]+) rc=0 warn=0 .* own=\1 ms=' <<<"$o"
check "advertise ip: own reachable address with the nonce answer -> that address" $? "$o"
o="$(aip MAIN_ADVERTISE_IP=127.0.0.1)"
grep -Eq '^ip=([0-9.]+) rc=0 warn=1 .* own=\1 ms=' <<<"$o" && ! grep -q '^ip=127' <<<"$o"
check "advertise ip: 127.0.0.1 -> container address with a warning" $? "$o"
o="$(aip MAIN_ADVERTISE_IP=192.0.2.1 HAIRPIN_BUDGET_S=3)"
grep -Eq '^ip=([0-9.]+) rc=0 warn=1 .* own=\1 ms=' <<<"$o"
check "advertise ip: unreachable 192.0.2.1 -> container address with a warning (also if a VPN accepts)" $? "$o"
o="$(aip MAIN_ADVERTISE_IP=own HAIRPIN_CONNECT_PORT=18444 HAIRPIN_BUDGET_S=3 -- "${FAKE_SRV/REPLY/c.sendall(b\"garbage\n\")}")"
grep -Eq '^ip=([0-9.]+) rc=0 warn=1 accepted=1 own=\1 ms=' <<<"$o"
check "advertise ip: interceptor answering garbage -> container address, warning says accepted" $? "$o"
o="$(aip MAIN_ADVERTISE_IP=own HAIRPIN_CONNECT_PORT=18444 HAIRPIN_BUDGET_S=3 -- "${FAKE_SRV/REPLY/c.sendall(b\"OK 0123456789abcdef0123456789abcdef\n\")}")"
grep -Eq '^ip=([0-9.]+) rc=0 warn=1 accepted=1 own=\1 ms=' <<<"$o"
check "advertise ip: wrong nonce -> container address" $? "$o"
o="$(aip MAIN_ADVERTISE_IP=own HAIRPIN_CONNECT_PORT=18444 HAIRPIN_BUDGET_S=3 -- "${FAKE_SRV/REPLY/time.sleep(3)}")"
grep -Eq '^ip=([0-9.]+) rc=0 warn=1 accepted=1 own=\1 ms=' <<<"$o"
check "advertise ip: accepts but stays silent -> container address" $? "$o"
ms="$(grep -o 'ms=[0-9]*' <<<"$o" | cut -d= -f2)"
[ -n "$ms" ] && [ "$ms" -le 4000 ]
check "advertise ip: the budget is hard (3 s budget, silent peer) -> done within 4 s" $? "$o"
o="$(aip MAIN_ADVERTISE_IP=own HAIRPIN_LISTEN_PORT=18444 -- "${FAKE_SRV/REPLY/pass}")"
grep -q 'rc=64' <<<"$o" && grep -q 'Ошибка: порт 18444 занят' <<<"$o"
check "advertise ip: listen port taken -> exit 64 with an explicit error" $? "$o"

# 8. run-main.sh and run-main.ps1: main/tests/test_run_main.sh (no image needed)

# 9. backend key: hex, persisted, handed to the portal
grep -Eq '^BACKEND_API_KEY=[0-9a-f]{64}$' <<<"$out1"
check "backend key is 64 hex chars in secrets.env" $? "$out1"
out9="$(ep_run "${BASE_ENV[@]}" -- 'grep ^BACKEND_API_KEY= /var/lib/llm/secrets.env; grep ^BACKEND_API_KEY= /run/llm/portal.env')"
[ "$(grep -c '^BACKEND_API_KEY=' <<<"$out9")" = 2 ] && [ "$(grep '^BACKEND_API_KEY=' <<<"$out9" | sort -u | wc -l)" = 1 ]
check "portal.env carries the same backend key as secrets.env" $? "$out9"
grep -q "$(grep -m1 '^BACKEND_API_KEY=' <<<"$out1")" <<<"$out9"
check "backend key survives a restart" $?

# 10. a changed Postgres password with an existing data volume is refused (initdb does not run again)
PGDIR=/var/lib/rancher/k3s/storage/pvc-x_llm_data-postgres-0
pg_run() { docker run --rm -v "$VOL-pg:/var/lib/llm" -e LLM_ENTRYPOINT_DRY_RUN=1 "${BASE_ENV[@]}" "$@" \
  --entrypoint sh "$IMAGE" -c "mkdir -p $PGDIR; /opt/llm/main/entrypoint.sh; echo rc=\$?" 2>&1; }
pg_run >/dev/null  # stores generated secrets
out10="$(pg_run -e POSTGRES_PASSWORD=changed-pw)"
grep -q '^rc=64$' <<<"$out10" && grep -q 'POSTGRES_PASSWORD' <<<"$out10" && grep -q 'том Postgres уже создан' <<<"$out10"
check "changed POSTGRES_PASSWORD with an existing data volume -> exit 64, Russian message" $? "$out10"
out10b="$(docker run --rm -v "$VOL-pg:/var/lib/llm" -e LLM_ENTRYPOINT_DRY_RUN=1 "${BASE_ENV[@]}" -e POSTGRES_PASSWORD=changed-pw "$IMAGE" 2>&1; echo "rc=$?")"
grep -q '^rc=0$' <<<"$out10b"
check "changed POSTGRES_PASSWORD on a fresh volume (no Postgres data yet) is accepted" $? "$out10b"
out10c="$(pg_run -e LITELLM_MASTER_KEY=sk-rotated-master)"
grep -q '^rc=0$' <<<"$out10c"
check "a changed LITELLM_MASTER_KEY is accepted even with Postgres data" $? "$out10c"
out10d="$(pg_run -e LITELLM_SALT_KEY=sk-other-salt)"
grep -q '^rc=64$' <<<"$out10d" && grep -q 'LITELLM_SALT_KEY' <<<"$out10d" && grep -q 'не расшифрует' <<<"$out10d"
check "changed LITELLM_SALT_KEY with an existing data volume -> exit 64 (stored credentials stay decryptable)" $? "$out10d"

# 11. a changed CLUSTER_TOKEN once k3s has bootstrap data is refused (k3s would restart forever with a token mismatch)
K3S_TOKEN_FILE=/var/lib/rancher/k3s/server/token
k3s_run() { docker run --rm -v "$VOL-pg:/var/lib/llm" -e LLM_ENTRYPOINT_DRY_RUN=1 "${BASE_ENV[@]}" "$@" \
  --entrypoint sh "$IMAGE" -c "mkdir -p ${K3S_TOKEN_FILE%/*}; echo K10abc::server:x >$K3S_TOKEN_FILE; /opt/llm/main/entrypoint.sh; echo rc=\$?" 2>&1; }
out11="$(k3s_run -e CLUSTER_TOKEN=rotated-token)"
grep -q '^rc=64$' <<<"$out11" && grep -q 'CLUSTER_TOKEN' <<<"$out11" && grep -q 'кластер k3s уже создан' <<<"$out11"
check "changed CLUSTER_TOKEN with existing k3s data -> exit 64, Russian message" $? "$out11"
out11b="$(k3s_run)"
grep -q '^rc=0$' <<<"$out11b"
check "unchanged CLUSTER_TOKEN with existing k3s data starts" $? "$out11b"
# k3s takes a new --agent-token on an existing cluster (docs.k3s.io/cli/token: "set before or after the cluster has been started")
out11c="$(k3s_run -e AGENT_TOKEN=rotated-agent-token)"
grep -q '^rc=0$' <<<"$out11c"
check "a changed AGENT_TOKEN with existing k3s data is accepted" $? "$out11c"

# 11b. agent-token.sh (run-main prints its output): the agent token in the secure form k3s writes, never the server token
at_run() { docker run --rm -v "$VOL:/var/lib/llm" --entrypoint sh "$IMAGE" -c "
  S=/var/lib/rancher/k3s/server; mkdir -p \$S; echo K10abc::server:\$(sed -n s/^CLUSTER_TOKEN=//p /var/lib/llm/secrets.env) >\$S/token
  A=\$(sed -n s/^AGENT_TOKEN=//p /var/lib/llm/secrets.env); $1
  /opt/llm/main/agent-token.sh; echo rc=\$?" 2>&1; }
agent_now="$(docker run --rm -v "$VOL:/var/lib/llm" --entrypoint sh "$IMAGE" -c 'sed -n s/^AGENT_TOKEN=//p /var/lib/llm/secrets.env')"
server_now="$(docker run --rm -v "$VOL:/var/lib/llm" --entrypoint sh "$IMAGE" -c 'sed -n s/^CLUSTER_TOKEN=//p /var/lib/llm/secrets.env')"
out11d="$(at_run 'echo K10abc::node:$A >$S/agent-token')"
grep -qx "K10abc::node:$agent_now" <<<"$out11d" && grep -q '^rc=0$' <<<"$out11d"
check "agent-token.sh prints K10<hash>::node:<AGENT_TOKEN>" $? "$out11d"
out11e="$(at_run 'ln -s $S/token $S/agent-token')"
grep -q '^rc=1$' <<<"$out11e" && ! grep -qF "$server_now" <<<"$out11e" && grep -q 'k3s' <<<"$out11e"
check "agent-token.sh refuses the old link to the server token (k3s not yet restarted with the agent token)" $? "$out11e"
out11f="$(at_run 'true')"
grep -q '^rc=1$' <<<"$out11f" && ! grep -qF "$server_now" <<<"$out11f"
check "agent-token.sh before k3s wrote the file -> exit 1, no token" $? "$out11f"

# 12. supervisord (Caddy, portal) starts with a clean environment: no secrets, no .env values
#     (fake supervisord and k3s in PATH; the real entrypoint runs to the exec of k3s)
FB="$CERTS_DIR/fb"; mkdir -p "$FB"
cat >"$FB/supervisord" <<'EOS'
#!/bin/sh
tr '\0' '\n' </proc/self/environ >/tmp/sv.env
sleep 30
EOS
cat >"$FB/k3s" <<'EOS'
#!/bin/sh
for i in 1 2 3 4 5 6 7 8 9 10; do [ -s /tmp/sv.env ] && break; sleep 0.5; done
echo K3S_STARTED
sed 's/^/SV_ENV /' /tmp/sv.env
echo "K3S_ARGS $*"
[ -n "${K3S_TOKEN:-}" ] && grep -qx "CLUSTER_TOKEN=$K3S_TOKEN" /var/lib/llm/secrets.env && echo K3S_TOKEN_FROM_ENV
[ -n "${K3S_AGENT_TOKEN:-}" ] && grep -qx "AGENT_TOKEN=$K3S_AGENT_TOKEN" /var/lib/llm/secrets.env \
  && [ "$K3S_AGENT_TOKEN" != "$K3S_TOKEN" ] && echo K3S_AGENT_TOKEN_FROM_ENV
EOS
out12="$(docker run --rm -v "$VOL:/var/lib/llm" -v "$CERTS_MOUNT/fb:/mnt/fb:ro" "${BASE_ENV[@]}" -e HAIRPIN_BUDGET_S=2 \
  -e HF_TOKEN=hf-secret-token -e PATH=/tmp/fb:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  --entrypoint sh "$IMAGE" -c "cp -r /mnt/fb /tmp/fb && chmod +x /tmp/fb/* && /opt/llm/main/entrypoint.sh" 2>&1)"
grep -q '^K3S_STARTED$' <<<"$out12" && grep -q '^SV_ENV PATH=/tmp/fb:' <<<"$out12" \
  && ! grep -Eq '^SV_ENV (POSTGRES_PASSWORD|LITELLM_DB_PASSWORD|PORTAL_DB_PASSWORD|LITELLM_MASTER_KEY|LITELLM_SALT_KEY|CLUSTER_TOKEN|K3S_TOKEN|AGENT_TOKEN|K3S_AGENT_TOKEN|BACKEND_API_KEY|OIDC_CLIENT_SECRET|HF_TOKEN)=' <<<"$out12"
check "supervisord gets PATH but no secrets (POSTGRES_PASSWORD, master/salt keys, tokens, OIDC secret)" $? "$out12"
grep -q '^K3S_TOKEN_FROM_ENV$' <<<"$out12" && grep -q '^K3S_ARGS server .*--node-name llm-main' <<<"$out12" \
  && ! grep -q '^K3S_ARGS .*--token' <<<"$out12"
check "k3s server gets the cluster token as K3S_TOKEN, not on its command line" $? "$out12"
grep -q '^K3S_AGENT_TOKEN_FROM_ENV$' <<<"$out12" && ! grep -q '^K3S_ARGS .*--agent-token' <<<"$out12"
check "k3s server gets the agent token as K3S_AGENT_TOKEN, not on its command line" $? "$out12"
# 12b. existing cluster: server/cred/passwd is removed before k3s starts, k3s restores it from the datastore and applies
#      the agent token again (kine keeps the passwd of the cluster's creation; a newer one on disk is fatal for k3s)
cat >>"$FB/k3s" <<'EOS'
C=/var/lib/rancher/k3s/server/cred
if [ -e $C/passwd ]; then echo PASSWD_KEPT; else echo PASSWD_REMOVED; fi
[ -e $C/ipsec.psk ] && echo OTHER_CRED_KEPT
EOS
fb_run() { docker run --rm -v "$VOL:/var/lib/llm" -v "$CERTS_MOUNT/fb:/mnt/fb:ro" "${BASE_ENV[@]}" -e HAIRPIN_BUDGET_S=2 \
  -e PATH=/tmp/fb:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin --entrypoint sh "$IMAGE" -c "
  S=/var/lib/rancher/k3s/server; mkdir -p \$S/cred; echo old >\$S/cred/passwd; echo psk >\$S/cred/ipsec.psk; $1
  cp -r /mnt/fb /tmp/fb && chmod +x /tmp/fb/* && /opt/llm/main/entrypoint.sh" 2>&1; }
out12b="$(fb_run 'echo K10abc::server:x >$S/token')"
grep -q '^PASSWD_REMOVED$' <<<"$out12b" && grep -q '^OTHER_CRED_KEPT$' <<<"$out12b"
check "existing k3s cluster: cred/passwd removed before k3s starts, other cred files kept" $? "$out12b"
out12c="$(fb_run 'true')"
grep -q '^PASSWD_KEPT$' <<<"$out12c"
check "no k3s cluster yet (no server/token): cred/passwd is not touched" $? "$out12c"


# 13. Caddy with the rendered Caddyfile (real caddy in the image, fake portal and LiteLLM upstreams):
#     body limits (1 MB portal, 32 MB /v1/*), security headers, retry while LiteLLM restarts, Russian JSON 503 with Retry-After
CT="$CERTS_DIR/caddytest"; mkdir -p "$CT"
cat >"$CT/upstream.py" <<'EOS'
# fake upstream: answers every request with 200 and the number of body bytes received; sets its own X-Frame-Options
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = 0
        while got < n:
            chunk = self.rfile.read(min(65536, n - got))
            if not chunk:
                break
            got += len(chunk)
        path = f" path={self.path} range={self.headers.get('Range')}" if sys.argv[2] == "hf" else ""
        body = f"{sys.argv[2]}{path} bytes={got}".encode()
        self.send_response(200)
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PUT = _answer
    def log_message(self, *a):
        pass

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
EOS
cat >"$CT/run.sh" <<'EOS'
#!/bin/bash
# runs inside the image after the dry-run entrypoint has written /run/llm/Caddyfile
export XDG_DATA_HOME=/tmp/caddy-data XDG_CONFIG_HOME=/tmp/caddy-config
caddy run --config /run/llm/Caddyfile --adapter caddyfile >/tmp/caddy.log 2>&1 &
python3 /mnt/ct/upstream.py 8000 portal &
H=llm.test.local
c() { curl -sk --resolve "$H:443:127.0.0.1" "$@"; }
for i in $(seq 1 50); do c -o /dev/null "https://$H/" && break; sleep 0.2; done
head -c 2097152 /dev/zero | tr '\0' a >/tmp/2m
head -c 524288 /dev/zero | tr '\0' a >/tmp/512k
head -c 131072 /dev/zero | tr '\0' a >/tmp/128k
head -c 34000000 /dev/zero | tr '\0' a >/tmp/33m
head -c 20000000 /dev/zero | tr '\0' a >/tmp/20m
echo "== GET /"; c -D - -o /dev/null "https://$H/"
echo "== portal 2M"; c -w ' status=%{http_code}' --data-binary @/tmp/2m "https://$H/api/keys"; echo
echo "== portal 512K"; c -w ' status=%{http_code}' --data-binary @/tmp/512k "https://$H/api/keys"; echo
echo "== upload 33M"; c -w ' status=%{http_code}' -X PUT --data-binary @/tmp/33m "https://$H/api/admin/local-models/uploads/u1?offset=0"; echo
head -c 90000000 /dev/zero >/tmp/90m
echo "== upload 90M"; c -w ' status=%{http_code}' -X PUT --data-binary @/tmp/90m "https://$H/api/admin/local-models/uploads/u1?offset=0"; echo
head -c 25000000 /dev/zero | tr '\0' a >/tmp/25m
echo "== playground 20M"; c -w ' status=%{http_code}' --data-binary @/tmp/20m "https://$H/api/playground/chat"; echo
echo "== playground 25M"; c -w ' status=%{http_code}' --data-binary @/tmp/25m "https://$H/api/playground/chat"; echo
t0=$(date +%s)
echo "== v1 down"; c -D - --data-binary '{}' "https://$H/v1/chat/completions"; echo; echo "v1 down took $(( $(date +%s) - t0 )) s"
(sleep 3; exec python3 /mnt/ct/upstream.py 4000 litellm) &
echo "== v1 restarting"; c -w ' status=%{http_code}' --data-binary @/tmp/128k "https://$H/v1/chat/completions"; echo
echo "== v1 33M"; c -w ' status=%{http_code}' --data-binary @/tmp/33m "https://$H/v1/chat/completions"; echo
echo "== v1 20M"; c -w ' status=%{http_code}' --data-binary @/tmp/20m "https://$H/v1/chat/completions"; echo
echo "== hf down"; c -D - "https://$H/hf/org/m/resolve/main/f.gguf"; echo
python3 /mnt/ct/upstream.py 8090 hf &
for i in $(seq 1 50); do c -f -o /dev/null "https://$H/hf/x" && break; sleep 0.2; done
echo "== hf get"; c -w ' status=%{http_code}' -H 'Range: bytes=5-' "https://$H/hf/c/node-a/org/m/resolve/main/f.gguf"; echo
echo "== hf 2M"; c -w ' status=%{http_code}' --data-binary @/tmp/2m "https://$H/hf/c/node-a/x"; echo
EOS
out13="$(ep_run "${BASE_ENV[@]}" -v "$CERTS_MOUNT/caddytest:/mnt/ct:ro" -- 'bash /mnt/ct/run.sh; echo "== caddy log"; tail -n 20 /tmp/caddy.log')"
sec() { awk -v s="== $1" '$0 == s {on=1; next} /^== / {on=0} on' <<<"$out13"; }
get="$(sec 'GET /')"
grep -qi '^strict-transport-security: max-age=31536000' <<<"$get" && grep -qi '^x-content-type-options: nosniff' <<<"$get" \
  && grep -qi '^referrer-policy: same-origin' <<<"$get" && [ "$(grep -ci '^x-frame-options:' <<<"$get")" = 1 ] \
  && grep -qi '^x-frame-options: DENY' <<<"$get"
check "Caddy: HSTS, X-Frame-Options DENY (overrides the upstream's), nosniff, Referrer-Policy same-origin" $? "$out13"
grep -q 'status=413' <<<"$(sec 'portal 2M')" && grep -q '"detail":"Запрос больше 1 МБ"' <<<"$(sec 'portal 2M')"
check "Caddy: portal body over 1 MB -> 413 with a Russian JSON detail" $? "$out13"
grep -q 'portal bytes=524288 status=200' <<<"$(sec 'portal 512K')"
check "Caddy: portal body under 1 MB reaches the portal" $? "$out13"
grep -q 'portal bytes=34000000 status=200' <<<"$(sec 'upload 33M')"
check "Caddy: a 33 MB part of an own-model upload reaches the portal" $? "$out13"
grep -q 'status=413' <<<"$(sec 'upload 90M')" && grep -q '"detail":"Часть файла больше 80 МБ"' <<<"$(sec 'upload 90M')"
check "Caddy: an upload part over 80 MB -> 413 with a Russian JSON detail" $? "$out13"
grep -q 'portal bytes=20000000 status=200' <<<"$(sec 'playground 20M')"
check "Caddy: a 20 MB Playground chat request (images, files) reaches the portal" $? "$out13"
grep -q 'status=413' <<<"$(sec 'playground 25M')" && grep -q '"detail":"Запрос больше 24 МБ' <<<"$(sec 'playground 25M')"
check "Caddy: a Playground chat request over 24 MB -> 413 with a Russian JSON detail" $? "$out13"
down="$(sec 'v1 down')"
grep -q '^HTTP/[0-9.]* 503' <<<"$down" && grep -qi '^retry-after: 15' <<<"$down" \
  && grep -q '"message":"Шлюз моделей временно недоступен' <<<"$down" && grep -Eq 'v1 down took 1[4-9] s' <<<"$down"
check "Caddy: LiteLLM down -> retried for 15 s, then 503 JSON in Russian with Retry-After" $? "$out13"
grep -q 'litellm bytes=131072 status=200' <<<"$(sec 'v1 restarting')"
check "Caddy: LiteLLM back within 15 s -> the waiting POST (128 KB body, buffered) succeeds" $? "$out13"
grep -q 'status=413' <<<"$(sec 'v1 33M')" && grep -q 'request_too_large' <<<"$(sec 'v1 33M')"
check "Caddy: /v1 body over 32 MB -> 413 (OpenAI-style error)" $? "$out13"
grep -q 'litellm bytes=20000000 status=200' <<<"$(sec 'v1 20M')"
check "Caddy: /v1 body of 20 MB reaches LiteLLM" $? "$out13"
grep -q '^HTTP/[0-9.]* 503' <<<"$(sec 'hf down')" && grep -q '"error":"Зеркало Hugging Face временно недоступно' <<<"$(sec 'hf down')"
check "Caddy: HF mirror down -> 503 JSON in Russian" $? "$out13"
grep -q 'hf path=/c/node-a/org/m/resolve/main/f.gguf range=bytes=5- bytes=0 status=200' <<<"$(sec 'hf get')"
check "Caddy: /hf/ reaches the mirror without the prefix, Range passed on" $? "$out13"
grep -q 'hf path=/c/node-a/x range=None bytes=2097152 status=200' <<<"$(sec 'hf 2M')"
check "Caddy: /hf/ has no 1 MB body limit" $? "$out13"

# 14. Domain through a front proxy (nginx on tsga, deploy/front-tsga): PUBLIC_HOSTNAME=double.fresh.nsuts.ru, internal CA.
#     nginx connects to https://<main IP> with SNI double.fresh.nsuts.ru and verifies the chain against /ca.crt
#     (proxy_ssl_verify_depth 2); curl --resolve and openssl s_client do the same here. X-Forwarded-For from a trusted
#     proxy reaches the portal, from anyone else it is replaced by the peer address.
cat >"$CT/echo.py" <<'EOS'
# fake portal: answers with the forwarding headers it received
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        h = self.headers
        body = f"host={h.get('Host')} xff={h.get('X-Forwarded-For')} xfp={h.get('X-Forwarded-Proto')} path={self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
EOS
cat >"$CT/front.sh" <<'EOS'
#!/bin/bash
# runs inside the image after the dry-run entrypoint; Caddy keeps its CA where supervisord puts it (/var/lib/llm/caddy-data)
export XDG_DATA_HOME=/var/lib/llm/caddy-data XDG_CONFIG_HOME=/tmp/caddy-config
caddy run --config /run/llm/Caddyfile --adapter caddyfile >/tmp/caddy.log 2>&1 &
python3 /mnt/ct/echo.py &
H=double.fresh.nsuts.ru
ROOT=/var/lib/llm/caddy-data/caddy/pki/authorities/local/root.crt
for i in $(seq 1 50); do curl -sk -o /dev/null --resolve "$H:443:127.0.0.1" "https://$H/readyz" && [ -f "$ROOT" ] && break; sleep 0.2; done
echo "== ca"; curl -s -o /tmp/ca.crt -w 'status=%{http_code}' --resolve "$H:80:127.0.0.1" "http://$H/ca.crt"; echo
cmp -s /tmp/ca.crt "$ROOT" && echo SAME_ROOT
echo "== verified"
curl -sS --cacert /tmp/ca.crt --resolve "$H:443:127.0.0.1" -H 'X-Forwarded-For: 203.0.113.7' -H 'X-Forwarded-Proto: http' \
  -w ' status=%{http_code}' "https://$H/readyz"; echo
echo "== s_client"
openssl s_client -connect 127.0.0.1:443 -servername "$H" -CAfile /tmp/ca.crt -verify_depth 2 -verify_return_error </dev/null 2>/tmp/sc.err >/tmp/sc.out
grep -h 'Verify return code' /tmp/sc.out /tmp/sc.err
openssl x509 -in /tmp/sc.out -noout -text 2>/dev/null | grep -o 'DNS:[^,]*'
EOS
for mode in trusted untrusted; do
  extra=(); [ "$mode" = untrusted ] || extra=(-e TRUSTED_PROXIES=127.0.0.1)
  out14="$(ep_run -e MAIN_ADVERTISE_IP=192.168.3.21 -e OIDC_CLIENT_SECRET=test-oidc-secret -e PUBLIC_HOSTNAME=double.fresh.nsuts.ru \
    "${extra[@]}" -v "$CERTS_MOUNT/caddytest:/mnt/ct:ro" -- 'bash /mnt/ct/front.sh; echo "== caddy log"; tail -n 20 /tmp/caddy.log')"
  sec14() { awk -v s="== $1" '$0 == s {on=1; next} /^== / {on=0} on' <<<"$out14"; }
  if [ "$mode" = trusted ]; then
    grep -q 'status=200' <<<"$(sec14 ca)" && grep -q '^SAME_ROOT$' <<<"$(sec14 ca)"
    check "front proxy: /ca.crt on port 80 is the internal CA root (what switch-front.sh copies to tsga)" $? "$out14"
    grep -q 'Verify return code: 0 (ok)' <<<"$(sec14 s_client)" && grep -qx 'DNS:double.fresh.nsuts.ru' <<<"$(sec14 s_client)"
    check "front proxy: SNI double.fresh.nsuts.ru gets a certificate for that name that verifies against /ca.crt (depth 2)" $? "$out14"
    grep -qx 'host=double.fresh.nsuts.ru xff=203.0.113.7, 127.0.0.1 xfp=http path=/readyz status=200' <<<"$(sec14 verified)"
    check "front proxy: TRUSTED_PROXIES=127.0.0.1 -> the portal gets the client address and scheme the proxy sent" $? "$out14"
  else
    grep -qx 'host=double.fresh.nsuts.ru xff=127.0.0.1 xfp=https path=/readyz status=200' <<<"$(sec14 verified)"
    check "no TRUSTED_PROXIES: X-Forwarded-For and -Proto from the peer are replaced (no spoofing)" $? "$out14"
  fi
done
echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
