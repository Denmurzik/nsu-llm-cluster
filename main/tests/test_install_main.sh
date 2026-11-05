#!/usr/bin/env bash
# Offline tests for main/install-main.sh: every external command is a stub that logs its call (docker, apt-get, dnf,
# systemctl, ufw, firewall-cmd, curl, nvidia-smi, ip, ss, df, id, uname, nproc, sleep). PATH holds only the stubs and
# wrappers of plain text tools, so the installer can never reach a real docker, package manager or firewall.
# /etc/os-release, /proc/meminfo and /etc/apt live under a fake root (NSU_LLM_SYSROOT).
set -uo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$(cd "$HERE/.." && pwd)"
INSTALL="${INSTALL_MAIN:-$MAIN/install-main.sh}"
BASH_BIN="$(command -v bash)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { # check NAME CONDITION-EXIT-CODE [DETAIL]
  if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi
}

# ------------------------------------------------------------------ safe tools: wrappers around real text utilities
SAFE="$TMP/safe"; mkdir -p "$SAFE"
for t in bash sh sed awk grep cat tr cut head tail mkdir chmod mv rm cp dirname basename date sort wc tee ls env stat \
  mktemp cmp touch printf od gzip gunzip; do
  real="$(command -v "$t" || true)"
  [ -n "$real" ] || continue
  printf '#!%s\nexec "%s" "$@"\n' "$BASH_BIN" "$real" >"$SAFE/$t"
  chmod +x "$SAFE/$t"
done

# ------------------------------------------------------------------ stubs
# Behaviour is driven by FAKE_* variables; state (image built, container created) lives in $FAKE_STATE.
TPL="$TMP/tpl"; mkdir -p "$TPL"
cat >"$TPL/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >>"$FAKE_LOG"
# the token (FAKE_TOKEN_VALUE) must be in no argv, also not inside the command a docker exec runs in the container
if [ -n "${FAKE_TOKEN_VALUE:-}" ]; then case "$*" in *"$FAKE_TOKEN_VALUE"*) echo "token in argv: $*" >>"$FAKE_STATE/leak" ;; esac; fi
case "$*" in *"--user"*|*GHCR_TOKEN*|*"-e GHCR"*) echo "credentials option in argv: $*" >>"$FAKE_STATE/leak" ;; esac
case "$1" in
  info)
    [ "${FAKE_DOCKER_DOWN:-0}" = 1 ] && [ ! -f "$FAKE_STATE/started" ] && exit 1
    if [ "${2:-}" = "--format" ]; then echo "${FAKE_DOCKER_INFO:-27.3.1 2 /var/lib/docker}"; fi
    exit 0 ;;
  image)
    [ "$2" = inspect ] && { [ -f "$FAKE_STATE/image" ] || [ "${FAKE_IMAGE:-0}" = 1 ]; }; exit $? ;;
  inspect)
    [ -f "$FAKE_STATE/container" ] || exit 1
    [ "${2:-}" = "-f" ] && echo "${FAKE_RUNNING:-true}"
    exit 0 ;;
  build) [ "${FAKE_CHILD_READS_STDIN:-0}" = 1 ] && cat >"$FAKE_STATE/child_stdin"; touch "$FAKE_STATE/image"; exit 0 ;;
  load) touch "$FAKE_STATE/image"; exit 0 ;;
  save) echo "TARBALL-OF $*"; exit 0 ;;
  tag|rmi) exit 0 ;;
  logs) printf '%s\n' "${FAKE_ENTRYPOINT_LOG:-}"; exit 0 ;;
  network) [ -n "${FAKE_BRIDGE_SUBNET:-}" ] && echo "${FAKE_BRIDGE_SUBNET} "; exit 0 ;;
  login) cat >"$FAKE_STATE/login_stdin"; echo "login-config ${DOCKER_CONFIG:-none}" >>"$FAKE_LOG"; exit 0 ;;
  logout) echo "logout-config ${DOCKER_CONFIG:-none}" >>"$FAKE_LOG"; exit 0 ;;
  pull) echo "pull-config ${DOCKER_CONFIG:-none}" >>"$FAKE_LOG"; [ "${FAKE_PULL_FAIL:-0}" = 1 ] && { echo "manifest unknown" >&2; exit 1; }
        touch "$FAKE_STATE/image"; exit 0 ;;
  run) [ "${FAKE_CHILD_READS_STDIN:-0}" = 1 ] && cat >>"$FAKE_STATE/child_stdin"; touch "$FAKE_STATE/container"; exit 0 ;;
  rm) rm -f "$FAKE_STATE/container"; exit 0 ;;
  exec)
    case "$*" in
      # agent-token.sh prints K10<CA hash>::node:<agent token> once k3s wrote it, fails before that
      *agent-token.sh*) [ -n "${FAKE_JOIN_TOKEN:-}" ] || exit 1; echo "$FAKE_JOIN_TOKEN" ;;
      *tags/list*) printf '{"name":"nsu-llm-node","tags":["%s-cpu","%s-gpu"]}' "${FAKE_SEED_VER:-0.2.1}" "${FAKE_SEED_VER:-0.2.1}" ;;
      *"images pull"*) [ "${FAKE_CTR_PULL_FAIL:-0}" = 1 ] && exit 1 ;;
      *"images import"*) cat >>"$FAKE_STATE/import-stdin" ;;
      # registry_digest: HEAD of a manifest; a tag listed in FAKE_REGISTRY_TAGS is there (digest FAKE_REGISTRY_DIGEST)
      # the pause image (rancher/mirrored-pause) is in the registry if FAKE_PAUSE_IN_REGISTRY=1 or it was pushed
      *mirrored-pause/manifests/*) if [ "${FAKE_PAUSE_IN_REGISTRY:-0}" = 1 ] || [ -f "$FAKE_STATE/pause-pushed" ]; then printf 'HTTP/1.1 200 OK\r\nDocker-Content-Digest: sha256:9a05e\r\n\r\n'; else exit 22; fi ;;
      # containerd of main (k3s ctr images ls -q): the names in FAKE_CTR_IMAGES (\n separated)
      *"images ls"*) printf '%b' "${FAKE_CTR_IMAGES:-}" ;;
      *manifests/*) all="$*"; tag="${all##*manifests/}"; case " ${FAKE_REGISTRY_TAGS:-} " in *" $tag "*) printf 'HTTP/1.1 200 OK\r\nDocker-Content-Digest: %s\r\n\r\n' "${FAKE_REGISTRY_DIGEST:-sha256:aaaa}" ;; *) exit 22 ;; esac ;;
      *"images tag"*mirrored-pause*) [ "${FAKE_PAUSE_TAG_FAIL:-0}" = 1 ] && exit 1 ;;
      *"images tag"*) [ "${FAKE_CTR_TAG_FAIL:-0}" = 1 ] && exit 1 ;;
      *"images push"*mirrored-pause*) [ "${FAKE_PAUSE_PUSH_FAIL:-0}" = 1 ] && exit 1; touch "$FAKE_STATE/pause-pushed" ;;
      *"images push"*) [ "${FAKE_CTR_PUSH_FAIL:-0}" = 1 ] && exit 1 ;;
      # main of this version has registry-push.sh (write with the password, SEC-2 M2); FAKE_NO_PUSH_HELPER=1: a main from before
      *"test -x /opt/llm/main/registry-push.sh"*) [ "${FAKE_NO_PUSH_HELPER:-0}" = 1 ] && exit 1 ;;
      *registry-push.sh*mirrored-pause*) [ "${FAKE_PAUSE_PUSH_FAIL:-0}" = 1 ] && exit 1; touch "$FAKE_STATE/pause-pushed" ;;
      *registry-push.sh*) [ "${FAKE_CTR_PUSH_FAIL:-0}" = 1 ] && exit 1 ;;
      *127.0.0.1:*/v2/) [ "${FAKE_REGISTRY_DOWN:-0}" = 1 ] && exit 1 ;;
      *secrets.env*) printf '%b' "${FAKE_SECRETS:-CLUSTER_TOKEN=c1\n}" ;;
      *server/token*) [ -n "${FAKE_SERVER_TOKEN:-}" ] && echo "$FAKE_SERVER_TOKEN" ;;
    esac
    exit 0 ;;
  *) exit 0 ;;
esac
EOS
cat >"$TPL/apt-get" <<'EOS'
#!/usr/bin/env bash
echo "apt-get $*" >>"$FAKE_LOG"
echo "pkg-env DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-} NEEDRESTART_MODE=${NEEDRESTART_MODE:-}" >>"$FAKE_LOG"
case "$*" in *install*docker-ce*) cp "$FAKE_TPL/docker" "$FAKE_BIN/docker"; chmod +x "$FAKE_BIN/docker" ;; esac
exit 0
EOS
cat >"$TPL/dnf" <<'EOS'
#!/usr/bin/env bash
echo "dnf $*" >>"$FAKE_LOG"
echo "pkg-env DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-} NEEDRESTART_MODE=${NEEDRESTART_MODE:-}" >>"$FAKE_LOG"
case "$*" in
  remove*) for p in "$@"; do rm -f "$FAKE_STATE/rpm-$p"; done ;;
  *install*docker-ce*)
    [ "${FAKE_DNF_FAIL:-0}" = 1 ] && { echo "Error: some other dnf failure" >&2; exit 1; }
    if [ -f "$FAKE_STATE/rpm-podman" ]; then echo "Error: problem: package docker-ce conflicts with podman (runc)" >&2; exit 1; fi
    cp "$FAKE_TPL/docker" "$FAKE_BIN/docker"; chmod +x "$FAKE_BIN/docker" ;;
esac
exit 0
EOS
cat >"$TPL/rpm" <<'EOS'
#!/usr/bin/env bash
[ "$1" = -q ] && [ -f "$FAKE_STATE/rpm-$2" ]
EOS
cat >"$TPL/systemctl" <<'EOS'
#!/usr/bin/env bash
echo "systemctl $*" >>"$FAKE_LOG"
case "$*" in *"enable --now docker"*|*"start docker"*) touch "$FAKE_STATE/started" ;; esac
exit 0
EOS
cat >"$TPL/ufw" <<'EOS'
#!/usr/bin/env bash
echo "ufw $*" >>"$FAKE_LOG"
[ "$1" = status ] && echo "Status: ${FAKE_UFW:-inactive}"
exit 0
EOS
cat >"$TPL/firewall-cmd" <<'EOS'
#!/usr/bin/env bash
echo "firewall-cmd $*" >>"$FAKE_LOG"
if [ "$1" = --state ]; then [ "${FAKE_FIREWALLD:-0}" = 1 ] && { echo running; exit 0; }; echo "not running"; exit 252; fi
exit 0
EOS
cat >"$TPL/curl" <<'EOS'
#!/usr/bin/env bash
echo "curl $*" >>"$FAKE_LOG"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
case "$*" in
  *readyz*) n=$(( $(cat "$FAKE_STATE/polls" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$FAKE_STATE/polls"
            code="${FAKE_READYZ:-200}"; [ "$n" -lt "${FAKE_READY_AFTER:-1}" ] && code=503
            printf '%s' "$code"; exit 0 ;;
  *ghcr.io/v2*) printf '%s' "${FAKE_GHCR:-000}"; exit 0 ;;
esac
[ -n "$out" ] && [ "$out" != /dev/null ] && echo "fake download" >"$out"
exit 0
EOS
cat >"$TPL/nvidia-smi" <<'EOS'
#!/usr/bin/env bash
echo "nvidia-smi $*" >>"$FAKE_LOG"
echo "NVIDIA A100-SXM4-80GB, 81920 MiB"
EOS
cat >"$TPL/ip" <<'EOS'
#!/usr/bin/env bash
case "$*" in
  *"route get"*) [ "${FAKE_NOROUTE:-0}" = 1 ] && exit 2; echo "1.1.1.1 via 10.20.0.1 dev eth0 src ${FAKE_IP:-10.20.0.5} uid 0" ;;
  *"addr show"*) echo "2: eth0    inet ${FAKE_IP:-10.20.0.5}/24 brd 10.20.0.255 scope global eth0\       valid_lft forever" ;;
esac
EOS
cat >"$TPL/ss" <<'EOS'
#!/usr/bin/env bash
printf '%b' "${FAKE_SS:-LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=1,fd=3))\n}"
EOS
cat >"$TPL/df" <<'EOS'
#!/usr/bin/env bash
[ "${FAKE_DF_FAIL:-0}" = 1 ] && exit 1
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/sda1 2000000000 1000 ${FAKE_DF_KB:-900000000} 1% /"
EOS
printf '#!/usr/bin/env bash\necho "${FAKE_UID:-0}"\n' >"$TPL/id"
printf '#!/usr/bin/env bash\ncase "$1" in -m) echo x86_64 ;; *) echo "${FAKE_UNAME:-Linux}" ;; esac\n' >"$TPL/uname"
printf '#!/usr/bin/env bash\necho "${FAKE_NPROC:-64}"\n' >"$TPL/nproc"
printf '#!/usr/bin/env bash\nexit 0\n' >"$TPL/sleep"
chmod +x "$TPL"/*

# new_env NAME: fresh stubs, state, fake root and work dir for one scenario; exports the FAKE_* plumbing.
new_env() {
  E="$TMP/$1"; mkdir -p "$E/bin" "$E/state" "$E/root/etc" "$E/root/proc" "$E/work" "$E/home"
  for s in docker apt-get dnf rpm systemctl ufw firewall-cmd curl ip ss df id uname nproc sleep; do cp "$TPL/$s" "$E/bin/$s"; done
  export FAKE_LOG="$E/log" FAKE_STATE="$E/state" FAKE_BIN="$E/bin" FAKE_TPL="$TPL"
  : >"$FAKE_LOG"
  os_ubuntu
  mem_kb 1056000000
  ENVF="$E/work/.env"; CONF="$E/work/install-main.env"
}
os_ubuntu() { printf 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n' >"$E/root/etc/os-release"; }
os_rocky() { printf 'ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"\nPRETTY_NAME="Rocky Linux 9.4 (Blue Onyx)"\n' >"$E/root/etc/os-release"; }
mem_kb() { printf 'MemTotal:       %s kB\nMemFree:        1000 kB\n' "$1" >"$E/root/proc/meminfo"; }

# inst [ARGS...]: run the installer inside the scenario; stdin is the scripted answers (or /dev/null).
inst() {
  # INST_TTY=0 makes the installer believe its stdin is not a terminal (the scripted answers come from a pipe or a here-string)
  local -a vars=(PATH="$E/bin:$SAFE" HOME="$E/home" TERM=dumb NSU_LLM_SYSROOT="$E/root" NSU_LLM_STDIN_TTY="${INST_TTY:-1}" NSU_LLM_TOKEN_WAIT_S=1)
  local v
  [ -z "${INST_SECRET:-}" ] || vars+=(OIDC_CLIENT_SECRET="$INST_SECRET")
  [ -z "${INST_OWNER:-}" ] || vars+=(GHCR_OWNER="$INST_OWNER")
  # never the real release/<version>.json of the checkout (with it every pull goes by digest): a scenario brings its own or has none
  vars+=(NSU_LLM_RELEASE_DIR="${INST_RELEASE_DIR:-$TMP/no-release}")
  for v in $(compgen -v FAKE_); do vars+=("$v=${!v}"); done
  env -i "${vars[@]}" "$BASH_BIN" "$INSTALL" --env-file "$ENVF" "$@" 2>&1
}
env_val() { sed -n "s/^$1=//p" "$2" | tail -n1; }
log_has() { grep -qF -- "$1" "$FAKE_LOG"; }

[ -f "$INSTALL" ] || { echo "FAIL - $INSTALL does not exist"; echo; echo "SOME FAILED"; exit 1; }

IDP_KV='PUBLIC_HOSTNAME=llm.nsu.ru
OIDC_ISSUER=https://idp.example.org/realms/main
OIDC_CLIENT_ID=nsu-llm
'

# ------------------------------------------------------------------ 1. interactive first install (Ubuntu, ufw active)
new_env first
export FAKE_UFW=active FAKE_SECRETS='CLUSTER_TOKEN=c1\nAGENT_TOKEN=agent-tok-1\n' FAKE_JOIN_TOKEN='K10cafe::node:agent-tok-1'
# answers: hostname, IP, secret (hidden), admins, front proxies, cert mode, airgap, firewall, image source, confirm
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsuper-secret-1\nadmin@g.nsu.ru, second@g.nsu.ru\n\n10.4.0.105, 10.0.0.0/8\n1\nn\ny\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
check "first install exits 0" "$([ "$rc" = 0 ]; echo $?)" "rc=$rc
$out"
[ "$(env_val PUBLIC_HOSTNAME "$ENVF")" = llm.nsu.ru ] && [ "$(env_val MAIN_ADVERTISE_IP "$ENVF")" = 10.20.0.5 ] \
  && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = super-secret-1 ] \
  && [ "$(env_val ADMIN_EMAILS "$ENVF")" = "admin@g.nsu.ru,second@g.nsu.ru" ] \
  && [ "$(env_val ADMIN_REQUIRE_EMAIL_VERIFIED "$ENVF")" = true ] && [ "$(env_val LOGIN_PROVIDER_NAME "$ENVF")" = SSO ] \
  && [ "$(env_val OIDC_ISSUER "$ENVF")" = https://idp.example.org/realms/main ] && [ "$(env_val LITELLM_WORKERS "$ENVF")" = 4 ]
check "answers land in .env (built from .env.example, detected LAN address as default)" $? "$(cat "$ENVF" 2>&1)"
[ "$(env_val TRUSTED_PROXIES "$ENVF")" = "10.4.0.105,10.0.0.0/8" ] && [ "$(grep -c '^TRUSTED_PROXIES=' "$ENVF")" = 1 ] \
  && grep -q 'фронт-прокси: *10.4.0.105,10.0.0.0/8' <<<"$out"
check "TRUSTED_PROXIES: asked, spaces dropped, written once to .env (in place of the .env.example line), shown in the summary" $? "$out
$(cat "$ENVF" 2>&1)"
perm="$(stat -c %a "$ENVF" 2>/dev/null)"
if [ "$(uname -s)" = Linux ]; then
  [ "$perm" = 600 ] && [ "$(stat -c %a "$CONF")" = 600 ]
  check ".env and the answers file are mode 600" $? "env=$perm conf=$(stat -c %a "$CONF")"
else
  echo "skip - mode 600 (no POSIX permissions on $(uname -s))"
fi
! grep -q super-secret-1 <<<"$out" && ! grep -q super-secret-1 "$CONF"
check "the client secret is neither echoed nor saved outside .env" $? "$out"
grep -q '^CERT_MODE=internal' "$CONF" && grep -q '^AIRGAP=no' "$CONF" && grep -q '^FIREWALL=yes' "$CONF" \
  && grep -q '^IMAGE_SOURCE=build' "$CONF"
check "installer choices are saved for the next run" $? "$(cat "$CONF" 2>&1)"
grep -q 'Ubuntu 24.04' <<<"$out" && grep -q '27.3.1' <<<"$out" && grep -q 'cgroup v2' <<<"$out" \
  && grep -q '10.20.0.5/24' <<<"$out" && grep -qi 'GPU.*не найден' <<<"$out"
check "checks report OS, Docker version, cgroup v2, LAN address with prefix, no GPU" $? "$out"
grep -q 'Будет выполнено' <<<"$out" && grep -q 'Продолжить' <<<"$out"
check "a summary is shown and confirmed before any action" $? "$out"
log_has "ufw allow 80/tcp" && log_has "ufw allow 443/tcp" && log_has "ufw allow 6443/tcp"
check "ufw opens 80, 443, 6443/tcp" $? "$(cat "$FAKE_LOG")"
log_has "docker build --provenance=false -f $MAIN/Dockerfile -t nsu-llm-main:0.2.1" \
  && grep -q "^docker run -d --name llm-main .*--env-file $ENVF" "$FAKE_LOG" && log_has "readyz"
check "image is built, run-main.sh starts llm-main with the env file, /readyz is polled" $? "$(cat "$FAKE_LOG")"
grep -q 'https://llm.nsu.ru/' <<<"$out" && grep -q 'http://10.20.0.5/ca.crt' <<<"$out" \
  && grep -qF 'Токен нод:           K10cafe::node:agent-tok-1' <<<"$out" \
  && grep -qF 'run-node.sh --main 10.20.0.5 --token K10cafe::node:agent-tok-1' <<<"$out" \
  && grep -qF 'run-node.ps1 -Main 10.20.0.5 -Token K10cafe::node:agent-tok-1' <<<"$out" && ! grep -q 'c1$' <<<"$out" \
  && log_has "docker exec llm-main /opt/llm/main/agent-token.sh"
check "final summary: portal URL, ca.crt link, the full agent token (K10<CA hash>) from agent-token.sh in the join commands" $? "$out"
! log_has "volume rm"
check "install never removes volumes" $? "$(cat "$FAKE_LOG")"

# ------------------------------------------------------------------ 2. re-run: no questions, image update, data kept
: >"$FAKE_LOG"; rm -f "$FAKE_STATE/polls"
out="$(inst <<<$'y\n')"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'Имя хоста\|Секрет\|Администраторы (\|Фронт-прокси' <<<"$out" && grep -q 'обновлен' <<<"$out" \
  && log_has "docker build" && log_has "docker rm -f llm-main" && grep -q '^docker run -d --name llm-main' "$FAKE_LOG" \
  && ! log_has "volume rm" && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = super-secret-1 ]
check "re-run asks only for confirmation, rebuilds the image and recreates llm-main on the same volumes" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
: >"$FAKE_LOG"
out="$(inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q '^docker run -d --name llm-main' "$FAKE_LOG"
check "re-run with --yes needs no input at all" $? "rc=$rc
$out"
# the user edited .env by hand: the edit survives a re-run
sed -i 's/^KEY_RPM_LIMIT=.*/KEY_RPM_LIMIT=120/' "$ENVF"
out="$(inst --yes </dev/null)"
[ "$(env_val KEY_RPM_LIMIT "$ENVF")" = 120 ]
check "re-run keeps manual edits of .env" $? "$(grep KEY_RPM "$ENVF")"
# .env of an install made before TRUSTED_PROXIES existed: the re-run asks nothing new and keeps no proxy
sed -i '/^TRUSTED_PROXIES=/d' "$ENVF"
out="$(inst <<<$'y\n')"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'Фронт-прокси' <<<"$out" && [ "$(env_val TRUSTED_PROXIES "$ENVF")" = "" ] \
  && [ "$(grep -c '^TRUSTED_PROXIES=' "$ENVF")" = 1 ]
check "re-run on an .env without TRUSTED_PROXIES: no question, written empty" $? "rc=$rc
$out"
unset FAKE_UFW FAKE_SECRETS FAKE_JOIN_TOKEN

# ------------------------------------------------------------------ 3. non-interactive: --yes --answers FILE
new_env answers
export FAKE_SERVER_TOKEN='K10abc::server:srvtok'
cat >"$E/answers.env" <<'EOF'
# answers for an unattended install
PUBLIC_HOSTNAME=llm.example.org
OIDC_ISSUER=https://idp.example.org/realms/main
OIDC_CLIENT_ID=nsu-llm
OIDC_CLIENT_SECRET=s3cr3t
ADMIN_EMAILS=a@g.nsu.ru
AIRGAP=no
IMAGE_SOURCE=keep
HF_TOKEN=hf_x
EOF
export FAKE_IMAGE=1
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val PUBLIC_HOSTNAME "$ENVF")" = llm.example.org ] && [ "$(env_val HF_TOKEN "$ENVF")" = hf_x ] \
  && [ "$(env_val MAIN_ADVERTISE_IP "$ENVF")" = 10.20.0.5 ] && ! log_has "docker build" \
  && grep -q '^docker run -d --name llm-main' "$FAKE_LOG" && ! grep -q 'srvtok\|--token c1\|Токен нод' <<<"$out" \
  && grep -q 'токен нод прочитать не удалось' <<<"$out"
check "--yes --answers: unattended install, extra keys go to .env, image kept; no agent token yet: neither the k3s server token nor CLUSTER_TOKEN is printed" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
grep -q '^TRUSTED_PROXIES=$' "$ENVF" && ! grep -q 'фронт-прокси' <<<"$out"
check "--yes without TRUSTED_PROXIES: no front proxy, the key is written empty" $? "$(cat "$ENVF")"
! grep -q 'Продолжить' <<<"$out" && grep -q 'брандмауэр: ufw и firewalld не активны' <<<"$out" && ! log_has "ufw allow"
check "--yes asks nothing; no active firewall -> nothing configured, said so" $? "$out"
unset FAKE_SERVER_TOKEN FAKE_IMAGE

new_env answers-missing
printf 'PUBLIC_HOSTNAME=llm.example.org\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'OIDC_CLIENT_SECRET' <<<"$out" && [ ! -f "$ENVF" ] && ! log_has "docker run"
check "--yes without OIDC_CLIENT_SECRET: Russian error naming the key, nothing written or started" $? "rc=$rc
$out"
printf "OIDC_ISSUER=https://idp.example.org
OIDC_CLIENT_ID=c
"'PUBLIC_HOSTNAME=bad_host!\nOIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'PUBLIC_HOSTNAME' <<<"$out" && ! log_has "docker run"
check "--yes with an invalid host name: error, nothing started" $? "rc=$rc
$out"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
export FAKE_READYZ=503
: >"$FAKE_LOG"; rm -f "$FAKE_STATE/polls"
out="$(inst --yes --answers "$E/answers.env" --timeout 10 </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'docker logs' <<<"$out" && [ "$(grep -c readyz "$FAKE_LOG")" = 2 ]
check "/readyz never 200: polls until --timeout, then an error with the docker logs command" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
unset FAKE_READYZ

# ------------------------------------------------------------------ 4. interactive input validation
new_env validate
answers=$'bad host\nllm.nsu.ru\n999.1.1.1\n\nhttp://idp.example.org\nhttps://idp.example.org/realms/main\n\nnsu-llm\n\nsec\nnot-an-email\na@g.nsu.ru\n\n10.4.0.105 }\n10.4.0.256,\n\n\n\n\ny\n'
out="$(FAKE_IMAGE=1 inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -q 'не похоже на имя хоста' <<<"$out" && grep -q 'нужен адрес IPv4' <<<"$out" \
  && grep -q 'не похоже на список e-mail' <<<"$out" && [ "$(env_val PUBLIC_HOSTNAME "$ENVF")" = llm.nsu.ru ] \
  && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = sec ] && [ "$(env_val ADMIN_EMAILS "$ENVF")" = a@g.nsu.ru ]
check "invalid answers are explained and asked again; empty secret is not accepted" $? "rc=$rc
$out"
[ "$(grep -c 'не похоже на адреса или подсети' <<<"$out")" = 1 ] && grep -q 'лишняя запятая' <<<"$out" \
  && grep -q '^TRUSTED_PROXIES=$' "$ENVF"
check "invalid front proxy answers are explained and asked again; empty means none" $? "rc=$rc
$out"
new_env decline
out="$(FAKE_IMAGE=1 inst <<<$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\ns\na@g.nsu.ru\n\n\n\n\n\nn\n')"; rc=$?
[ "$rc" != 0 ] && [ ! -f "$ENVF" ] && ! log_has "docker run"
check "declining the summary changes nothing" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 5. Docker missing
new_env nodocker
rm "$E/bin/docker"
out="$(inst <<<$'n\n')"; rc=$?
[ "$rc" != 0 ] && grep -q 'download.docker.com' <<<"$out" && ! log_has "apt-get"
check "no Docker, installation declined: error with the install commands, nothing run" $? "rc=$rc
$out"
answers=$'y\nllm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\ns\na@g.nsu.ru\n\n\n\n\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && log_has "apt-get install -y ca-certificates curl" && log_has "curl -fsSL https://download.docker.com/linux/ubuntu/gpg" \
  && grep -q 'https://download.docker.com/linux/ubuntu noble stable' "$E/root/etc/apt/sources.list.d/docker.list" \
  && log_has "apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" \
  && log_has "systemctl enable --now docker" && grep -q '^docker run -d --name llm-main' "$FAKE_LOG"
check "no Docker, installation accepted: official apt repository (noble), service enabled, install continues" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
if [ "$(uname -s)" = Linux ]; then
  [ "$(stat -c %a "$E/root/etc/apt/sources.list.d/docker.list")" = 644 ] && [ "$(stat -c %a "$E/root/etc/apt/keyrings/docker.asc")" = 644 ]
  check "the apt source and key are world-readable though the installer runs with umask 077" $? \
    "list=$(stat -c %a "$E/root/etc/apt/sources.list.d/docker.list") key=$(stat -c %a "$E/root/etc/apt/keyrings/docker.asc")"
else
  echo "skip - docker.list mode (no POSIX permissions on $(uname -s))"
fi
! grep -q '^pkg-env DEBIAN_FRONTEND=noninteractive' "$FAKE_LOG" && grep -q '^pkg-env DEBIAN_FRONTEND= NEEDRESTART_MODE=$' "$FAKE_LOG"
check "interactive Docker install leaves the package manager's own prompts alone" $? "$(grep pkg-env "$FAKE_LOG")"
new_env nodocker-yes
rm "$E/bin/docker"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'INSTALL_DOCKER=yes' <<<"$out" && ! log_has "apt-get"
check "--yes without INSTALL_DOCKER=yes never installs Docker on its own" $? "rc=$rc
$out"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nINSTALL_DOCKER=yes\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "apt-get install -y docker-ce" && grep -q '^pkg-env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a$' "$FAKE_LOG" \
  && ! grep -q '^pkg-env DEBIAN_FRONTEND= ' "$FAKE_LOG"
check "--yes Docker install on Ubuntu runs apt non-interactively (DEBIAN_FRONTEND=noninteractive, NEEDRESTART_MODE=a)" $? "rc=$rc
$(grep pkg-env "$FAKE_LOG")
$out"
new_env rocky
os_rocky; rm "$E/bin/docker"
export FAKE_FIREWALLD=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nINSTALL_DOCKER=yes\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo" \
  && log_has "dnf install -y docker-ce" && log_has "firewall-cmd --permanent --add-port=443/tcp" && log_has "firewall-cmd --reload"
check "Rocky Linux: Docker from the official dnf repository, ports opened in firewalld" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
unset FAKE_FIREWALLD
# RHEL family with podman and runc installed: they conflict with containerd.io, so they are removed first (docs.docker.com) and the summary says so
new_env rocky-podman
os_rocky; rm "$E/bin/docker"
touch "$FAKE_STATE/rpm-podman" "$FAKE_STATE/rpm-runc"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nINSTALL_DOCKER=yes\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
rm_line="$(grep -n '^dnf -y remove\|^dnf remove' "$FAKE_LOG" | head -n1 | cut -d: -f1)"; in_line="$(grep -n '^dnf install -y docker-ce' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
[ "$rc" = 0 ] && [ -n "$rm_line" ] && [ -n "$in_line" ] && [ "$rm_line" -lt "$in_line" ] && grep -q '^dnf .*remove.* podman runc' "$FAKE_LOG" \
  && grep -q 'конфликт' <<<"$out" && grep -q 'podman' <<<"$out" && grep -q 'Удалены конфликтующие пакеты: podman runc' <<<"$out" \
  && grep -q '^docker run -d --name llm-main' "$FAKE_LOG"
check "RHEL family: conflicting podman and runc are removed before the Docker install, announced and listed in the summary" $? "rc=$rc rm=$rm_line in=$in_line
$out
$(cat "$FAKE_LOG")"
new_env rocky-clean
os_rocky; rm "$E/bin/docker"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x
ADMIN_EMAILS=a@g.nsu.ru
INSTALL_DOCKER=yes
' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! grep -q '^dnf .*remove' "$FAKE_LOG" && ! grep -q 'Удалены конфликтующие' <<<"$out"
check "RHEL family without conflicting packages: nothing is removed" $? "rc=$rc
$out"
new_env rocky-fail
os_rocky; rm "$E/bin/docker"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x
ADMIN_EMAILS=a@g.nsu.ru
INSTALL_DOCKER=yes
' >"$E/answers.env"
export FAKE_DNF_FAIL=1
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'Исправление:' <<<"$out" && grep -q 'dnf' <<<"$out" && ! log_has "docker run"
check "a failed Docker install prints the failed command and a fix command" $? "rc=$rc
$out"
unset FAKE_DNF_FAIL
new_env docker-down
export FAKE_DOCKER_DOWN=1 FAKE_IMAGE=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "systemctl enable --now docker" && ! log_has "apt-get"
check "Docker installed but stopped: the service is started, nothing is installed" $? "rc=$rc
$out"
unset FAKE_DOCKER_DOWN FAKE_IMAGE

# ------------------------------------------------------------------ 6. preflight failures (all reported at once)
new_env preflight
export FAKE_DOCKER_INFO="20.10.0 1 /var/lib/docker" FAKE_DF_KB=1000000 \
  FAKE_SS='LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
mem_kb 2000000
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'cgroup v1' <<<"$out" && grep -q 'systemd.unified_cgroup_hierarchy=1' <<<"$out" \
  && grep -q '443.*nginx' <<<"$out" && grep -q "ss -ltnp" <<<"$out" && grep -q 'ГиБ' <<<"$out" \
  && grep -q 'памят' <<<"$out" && ! log_has "docker run" && [ ! -f "$ENVF" ]
check "cgroup v1, busy port 443, low disk and RAM: every problem with its fix, nothing done" $? "rc=$rc
$out"
unset FAKE_DOCKER_INFO FAKE_DF_KB FAKE_SS
new_env ours
touch "$FAKE_STATE/container"
export FAKE_SS='LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:(("docker-proxy",pid=9,fd=4))\n' FAKE_IMAGE=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'обновлен' <<<"$out"
check "ports held by the existing llm-main are not a conflict (update)" $? "rc=$rc
$out"
unset FAKE_SS FAKE_IMAGE
# a host without a default route (airgap): the installer says so and goes on instead of dying silently under set -e
new_env noroute
export FAKE_NOROUTE=1 FAKE_IMAGE=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'нет маршрута по умолчанию' <<<"$out" && grep -q 'MAIN_ADVERTISE_IP' <<<"$out" && ! log_has "docker run"
check "no default route, no MAIN_ADVERTISE_IP: warning, then an error that names MAIN_ADVERTISE_IP" $? "rc=$rc
$out"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nMAIN_ADVERTISE_IP=10.99.0.7\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'нет маршрута по умолчанию' <<<"$out" && [ "$(env_val MAIN_ADVERTISE_IP "$ENVF")" = 10.99.0.7 ] && log_has "docker run"
check "no default route but MAIN_ADVERTISE_IP given: the install goes through" $? "rc=$rc
$out"
unset FAKE_NOROUTE
new_env dffail
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x
ADMIN_EMAILS=a@g.nsu.ru
IMAGE_SOURCE=keep
MAIN_ADVERTISE_IP=10.20.0.5
' >"$E/answers.env"
export FAKE_DF_FAIL=1 FAKE_IMAGE=1
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'не удалось узнать свободное место' <<<"$out" && log_has "docker run"
check "df failing is a warning, not a silent exit" $? "rc=$rc
$out"
unset FAKE_DF_FAIL FAKE_IMAGE
new_env mirrordisk
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$E/answers.env"
export FAKE_DF_KB=$((600 * 1048576)) FAKE_IMAGE=1  # 600 GiB free: the default 500 GiB cache is above 80 % of it
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'HF_MIRROR_MAX_GIB=500 ГиБ больше 80 % свободного места' <<<"$out" && log_has "docker run"
check "a mirror cache limit above 80 % of the free disk is a warning" $? "rc=$rc
$out"
printf 'HF_MIRROR_MAX_GIB=400\n' >>"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'HF_MIRROR_MAX_GIB' <<<"$out"
check "a mirror cache limit within 80 % of the free disk: no warning" $? "rc=$rc
$out"
unset FAKE_DF_KB FAKE_IMAGE
new_env nocurl
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x
ADMIN_EMAILS=a@g.nsu.ru
IMAGE_SOURCE=keep
MAIN_ADVERTISE_IP=10.20.0.5
' >"$E/answers.env"
rm "$E/bin/curl"
export FAKE_IMAGE=1
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'curl' <<<"$out" && grep -q 'Исправление' <<<"$out" && ! log_has "docker run"
check "no curl: reported with a fix before anything starts (it polls /readyz)" $? "rc=$rc
$out"
unset FAKE_IMAGE
new_env nonroot
out="$(FAKE_UID=1000 inst --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'sudo' <<<"$out" && ! log_has "docker run"
check "not root: error with the sudo command" $? "rc=$rc
$out"
new_env windows
out="$(FAKE_UNAME=MINGW64_NT-10.0 inst --yes </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'run-main.ps1' <<<"$out"
check "not Linux: points to run-main.ps1" $? "rc=$rc
$out"

# ------------------------------------------------------------------ 7. GPU, own certificate, airgap, dry run
new_env gpu
cp "$TPL/nvidia-smi" "$E/bin/nvidia-smi"
mkdir -p "$E/certs"; touch "$E/certs/tls.crt"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nCERT_MODE=own\nCERTS_DIR=%s\nAIRGAP=yes\nIMAGE_SOURCE=keep\n' "$E/certs" >"$E/answers.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'tls.key' <<<"$out" && ! log_has "docker run"
check "own certificate without tls.key: error before any action" $? "rc=$rc
$out"
touch "$E/certs/tls.key"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" --dry-run </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'NVIDIA A100-SXM4-80GB' <<<"$out" && grep -q -- '--airgap' <<<"$out" \
  && grep -q -- "-v $E/certs:/certs:ro" <<<"$out" && grep -q 'docker run -d --name llm-main' <<<"$out" \
  && [ ! -f "$ENVF" ] && [ ! -f "$CONF" ] && ! log_has "docker run" && ! log_has "docker pull" && ! log_has "ufw allow" \
  && ! grep -qx 'x' <<<"$out" && grep -q 'OIDC_CLIENT_SECRET=\*\*\*' <<<"$out"
check "--dry-run: GPU reported, airgap and certs passed to run-main, commands printed, nothing written or run, secret masked" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# ------------------------------------------------------------------ 8. uninstall
new_env uninstall
touch "$FAKE_STATE/container"
printf 'CERT_MODE=internal\nAIRGAP=no\nFIREWALL=yes\nIMAGE_SOURCE=build\n' >"$CONF"
export FAKE_UFW=active
out="$(inst --uninstall <<<$'n\n')"; rc=$?
[ "$rc" != 0 ] && ! log_has "docker rm" && grep -q 'безвозвратно' <<<"$out" \
  && grep -q 'llm-main-k3s, llm-main-kubelet, llm-main-data, llm-main-hf' <<<"$out" && grep -q 'свои модели' <<<"$out"
check "--uninstall names every volume it removes (llm-main-hf: the mirror cache and own models), asks first and does nothing on no" $? "rc=$rc
$out"
out="$(inst --uninstall <<<$'y\n')"; rc=$?
[ "$rc" = 0 ] && log_has "docker rm -f llm-main" && log_has "docker volume rm llm-main-k3s llm-main-kubelet llm-main-data" \
  && log_has "ufw delete allow 6443/tcp"
check "--uninstall on yes: container, volumes and firewall rules removed" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
: >"$FAKE_LOG"
out="$(inst --uninstall --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker volume rm"
check "--uninstall --yes runs without a prompt" $? "rc=$rc
$out"
unset FAKE_UFW

# ------------------------------------------------------------------ 9. secrets in the output, no terminal, children's stdin, env precedence
new_env mask
printf "$IDP_KV"'OIDC_CLIENT_SECRET=sec-1\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nHF_TOKEN=hf_tok_x\nDB_PASSWORD=pw_x\nMY_SECRET=sc_x\nPLAIN=visible\n' >"$E/answers.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" --dry-run </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'HF_TOKEN=\*\*\*' <<<"$out" && grep -q 'DB_PASSWORD=\*\*\*' <<<"$out" && grep -q 'MY_SECRET=\*\*\*' <<<"$out" \
  && grep -q 'OIDC_CLIENT_SECRET=\*\*\*' <<<"$out" && grep -q 'PLAIN=visible' <<<"$out" \
  && ! grep -q 'hf_tok_x\|pw_x\|sc_x\|sec-1' <<<"$out"
check "--dry-run masks every *TOKEN*, *SECRET* and *PASSWORD* value, other values stay" $? "rc=$rc
$out"

new_env notty
out="$(INST_TTY=0 inst <<<$'llm.nsu.ru\n')"; rc=$?
[ "$rc" != 0 ] && grep -q -- '--yes --answers' <<<"$out" && grep -q 'ssh -t' <<<"$out" && ! grep -q 'ввод закончился' <<<"$out" \
  && [ ! -f "$ENVF" ] && ! log_has "docker run"
check "interactive mode without a terminal on stdin: exits at once with the --yes --answers / ssh -t hint" $? "rc=$rc
$out"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(FAKE_IMAGE=1 INST_TTY=0 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ]
check "--yes needs no terminal" $? "rc=$rc
$out"

new_env childin
export FAKE_CHILD_READS_STDIN=1 FAKE_UFW=active
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsecret-1\nadmin@g.nsu.ru\n\n\n1\nn\ny\n\ny\nLEAKED-INTO-A-CHILD\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && ! grep -q LEAKED "$FAKE_STATE/child_stdin" 2>/dev/null && grep -q '^docker run -d --name llm-main' "$FAKE_LOG"
check "commands the installer starts never read its answers (stdin is /dev/null for them)" $? "rc=$rc child=$(cat "$FAKE_STATE/child_stdin" 2>&1)
$out"
unset FAKE_CHILD_READS_STDIN FAKE_UFW

new_env prec
printf "$IDP_KV"'OIDC_CLIENT_SECRET=from-env-file\nADMIN_EMAILS=a@g.nsu.ru\nPUBLIC_HOSTNAME=llm.nsu.ru\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$ENVF"
out="$(FAKE_IMAGE=1 INST_SECRET=from-environment inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'OIDC_CLIENT_SECRET' <<<"$out" && grep -q 'переменной окружения' <<<"$out" && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = from-env-file ] \
  && ! grep -q 'from-env' <<<"$out"
check "a secret in the environment does not override .env: said so, no value printed" $? "rc=$rc
$out"


# ------------------------------------------------------------------ 7. IMAGE_SOURCE=pull: ghcr.io, then the node images into the registry of main
new_env pull
export FAKE_GHCR=401
# answers: hostname, IP, secret, admins, cert mode, airgap, image source 1 (pull), owner, confirm
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsec-pull\nadmin@g.nsu.ru\n\n\n1\nn\n1\nnsu\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -q '1 — скачать с ghcr.io (pull)' <<<"$out" && grep -q 'ghcr.io доступен' <<<"$out"
check "pull, interactive: ghcr.io is offered first when reachable, then the owner is asked" $? "rc=$rc
$out"
l_pull="$(grep -n '^docker pull ghcr.io/nsu/nsu-llm-main:0.2.1$' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_tag="$(grep -n '^docker tag ghcr.io/nsu/nsu-llm-main:0.2.1 nsu-llm-main:0.2.1$' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_run="$(grep -n '^docker run -d --name llm-main' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_seed="$(grep -n 'images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.2.1-cpu' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
[ -n "$l_pull" ] && [ -n "$l_tag" ] && [ -n "$l_run" ] && [ -n "$l_seed" ] && [ "$l_pull" -lt "$l_tag" ] && [ "$l_tag" -lt "$l_run" ] && [ "$l_run" -lt "$l_seed" ] \
  && ! log_has "docker build" && ! log_has "docker load"
check "pull: docker pull + tag as nsu-llm-main:0.2.1 before the container starts, no build; node images only after it is up" $? "$(cat "$FAKE_LOG")"
for v in cpu gpu; do
  log_has "docker exec llm-main k3s ctr -n k8s.io images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.2.1-$v" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images tag --force ghcr.io/nsu/nsu-llm-node:0.2.1-$v 127.0.0.1:5000/nsu-llm-node:0.2.1-$v" \
    && log_has "docker exec llm-main /opt/llm/main/registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-$v" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images rm --sync ghcr.io/nsu/nsu-llm-node:0.2.1-$v 127.0.0.1:5000/nsu-llm-node:0.2.1-$v"
  check "pull: the $v node image goes ghcr.io -> k3s of main -> 127.0.0.1:5000 (plain http, only from main) and the temporary tags are removed" $? "$(cat "$FAKE_LOG")"
done
grep -q 'Реестр образов нод:  https://10.20.0.5/v2/' <<<"$out" && grep -q 'run-node.sh --main 10.20.0.5 --token .* --from-main' <<<"$out" || grep -q 'Реестр образов нод' <<<"$out"
check "pull: the summary names the registry address of main" $? "$out"
grep -q '^IMAGE_SOURCE=pull' "$CONF" && grep -q '^GHCR_OWNER=nsu' "$CONF" && ! grep -q 'sec-pull' "$CONF"
check "pull: IMAGE_SOURCE and GHCR_OWNER are saved for the next run, the secret is not" $? "$(cat "$CONF")"
! log_has "login" && ! log_has "volume rm"
check "pull of a public image needs no token and no login" $? "$(cat "$FAKE_LOG")"

# re-run: no questions, pulls again, recreates the container, seeds again
: >"$FAKE_LOG"; rm -f "$FAKE_STATE/polls"
out="$(inst <<<$'y\n')"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.1" && log_has "docker rm -f llm-main" && log_has "registry-push.sh 127.0.0.1:5000/"
check "pull: the re-run takes the saved choice without questions, updates the image and the registry content" $? "rc=$rc
$out"

# --yes: answers file with IMAGE_REF and NODE_IMAGE_SOURCE (a private mirror)
new_env pull-mirror
export FAKE_GHCR=401
cat >"$E/answers.env" <<'EOF'
PUBLIC_HOSTNAME=llm.nsu.ru
OIDC_ISSUER=https://idp.example.org/realms/main
OIDC_CLIENT_ID=nsu-llm
OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
IMAGE_SOURCE=pull
GHCR_OWNER=nsu
IMAGE_REF=ghcr.io/mirror/main-copy:0.2.1
NODE_IMAGE_SOURCE=ghcr.io/mirror/node-copy
EOF
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/mirror/main-copy:0.2.1" && log_has "docker tag ghcr.io/mirror/main-copy:0.2.1 nsu-llm-main:0.2.1" \
  && log_has "images pull --platform linux/amd64 ghcr.io/mirror/node-copy:0.2.1-gpu" && ! grep -q '^GHCR_OWNER=\|^IMAGE_REF=' "$ENVF"
check "--yes: IMAGE_REF and NODE_IMAGE_SOURCE override the defaults; installer keys never leak into .env" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# default: owner known and ghcr.io reachable -> pull is the default; unreachable -> build, and the prompt says why
new_env pull-default
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.1" && ! log_has "docker build"
check "--yes with a known owner and a reachable ghcr.io: pull is the default" $? "rc=$rc
$out"
new_env pull-default-down
export FAKE_GHCR=000
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
GHCR_OWNER=nsu
' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "docker pull" && log_has "docker build"
check "ghcr.io unreachable: the default stays build (no behaviour change for an offline install)" $? "rc=$rc
$out"
new_env pull-prompt-down
out="$(inst <<<$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\ns\na@g.nsu.ru\n\n\n\n\ny\n')"; rc=$?
grep -q 'скачивание с ghcr.io недоступно' <<<"$out" && ! grep -q '1 — скачать с ghcr.io' <<<"$out"
check "interactive question without a reachable ghcr.io does not offer pull and says why" $? "rc=$rc
$out"

# unreachable / blocked ghcr.io with pull chosen explicitly
for code in 000 403; do
  new_env "pull-$code"
  export FAKE_GHCR=$code
  printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
  out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'ghcr.io' <<<"$out" && grep -q 'IMAGE_SOURCE=load' <<<"$out" && grep -q 'прокси' <<<"$out" \
    && ! log_has "docker pull" && ! log_has "docker run -d" && ! log_has "docker build"
  check "pull with ghcr.io answering $code: Russian error naming proxy and IMAGE_SOURCE=load, nothing pulled or started" $? "rc=$rc
$out"
done
grep -q '403' <<<"$out"
check "a 403 from ghcr.io is called a block of the network" $? "$out"

# missing / invalid owner, failed pull
new_env pull-noowner
export FAKE_GHCR=401
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/denmurzik/nsu-llm-main:0.2.1" \
  && log_has "images pull --platform linux/amd64 ghcr.io/denmurzik/nsu-llm-node:0.2.1-cpu"
check "GHCR_OWNER defaults to denmurzik: images ghcr.io/denmurzik/nsu-llm-main and .../nsu-llm-node, no error under --yes" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
new_env pull-badowner
export FAKE_GHCR=401
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=Bad_Owner\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'GHCR_OWNER' <<<"$out" && ! log_has "docker pull"
check "an invalid GHCR_OWNER is explained" $? "rc=$rc
$out"
new_env pull-fail
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\n' >"$E/answers.env"
out="$(INST_OWNER=nsu inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.1"
check "GHCR_OWNER from the environment is accepted" $? "rc=$rc
$out"
new_env pull-fail2
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(FAKE_PULL_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'docker pull ghcr.io/nsu/nsu-llm-main:0.2.1 не удался' <<<"$out" && ! log_has "docker run -d"
check "a failed docker pull stops before the container is touched" $? "rc=$rc
$out"
new_env seed-fail
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(FAKE_CTR_PULL_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'не удалось скачать ghcr.io/nsu/nsu-llm-node:0.2.1-cpu' <<<"$out" && grep -q 'HTTPS_PROXY' <<<"$out"
check "a failed pull of a node image inside main: Russian error with the retry command and the proxy hint" $? "rc=$rc
$out"
new_env seed-down
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
IMAGE_SOURCE=pull
GHCR_OWNER=nsu
' >"$E/answers.env"
out="$(FAKE_REGISTRY_DOWN=1 inst --yes --answers "$E/answers.env" --timeout 10 </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'реестр на 127.0.0.1:5000 не ответил' <<<"$out" && ! log_has "images pull"
check "a registry that never answers: error with the pod log command, no pull attempted" $? "rc=$rc
$out"

# --dry-run: every step printed, nothing pulled
new_env pull-dry
export FAKE_GHCR=401
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'docker pull ghcr.io/nsu/nsu-llm-main:0.2.1' <<<"$out" && grep -q 'docker tag ghcr.io/nsu/nsu-llm-main:0.2.1 nsu-llm-main:0.2.1' <<<"$out" \
  && grep -q '/opt/llm/main/registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-gpu' <<<"$out" \
  && ! log_has "docker pull" && ! log_has "docker exec llm-main k3s ctr" && ! log_has "docker run -d"
check "--dry-run with pull prints the pull, the tag and the registry seeding and runs none of them" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# token file for a private mirror
new_env pull-token
export FAKE_GHCR=401
printf 'fake-private-token-123\n' >"$E/work/ghcr-token"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\nGHCR_TOKEN_FILE=%s\n' "$E/work/ghcr-token" >"$E/answers.env"
if [ "$(uname -s)" = Linux ]; then
  chmod 644 "$E/work/ghcr-token"
  out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'chmod 600' <<<"$out" && ! grep -q 'PRIVATETOKEN' <<<"$out" && ! log_has "docker pull"
  check "a token file readable by others is refused (0600 required), the token is not printed" $? "rc=$rc
$out"
  chmod 600 "$E/work/ghcr-token"
  out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  : >"$FAKE_STATE/leak"
  out="$(FAKE_TOKEN_VALUE=fake-private-token-123 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  [ "$rc" = 0 ] && grep -q '^docker login ghcr.io -u nsu --password-stdin$' "$FAKE_LOG" && [ "$(cat "$FAKE_STATE/login_stdin")" = 'fake-private-token-123' ] \
    && ! grep -q 'fake-private-token-123' "$FAKE_LOG" && ! grep -q 'fake-private-token-123' <<<"$out" \
    && ! grep -q 'fake-private-token-123' "$ENVF" "$CONF"
  check "private mirror: the token goes only through the stdin of docker login, never argv, output, .env or the saved answers" $? "rc=$rc
$(cat "$FAKE_LOG")"
  [ ! -s "$FAKE_STATE/leak" ]
  check "private mirror: no argv of any docker call, including the command run inside the container, has the token, --user or GHCR_* options" $? "$(cat "$FAKE_STATE/leak" 2>&1)"
  grep -q '^docker pull ghcr.io/nsu/nsu-llm-node:0.2.1-gpu$' "$FAKE_LOG" && grep -q '^docker save 127.0.0.1:5000/nsu-llm-node:0.2.1-gpu$' "$FAKE_LOG" \
    && grep -q '^docker exec -i llm-main k3s ctr -n k8s.io images import --no-unpack -$' "$FAKE_LOG" && grep -q 'TARBALL-OF save 127.0.0.1:5000/nsu-llm-node:0.2.1-gpu' "$FAKE_STATE/import-stdin" \
    && ! grep -q 'images pull' "$FAKE_LOG" && grep -q 'registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-gpu' "$FAKE_LOG"
  check "private mirror: the node image is pulled by the docker of the host, streamed in with docker save | ctr images import, then pushed; no ctr pull inside main" $? "$(cat "$FAKE_LOG")"
  cfg="$(sed -n 's/^login-config //p' "$FAKE_LOG" | head -n1)"
  [ -n "$cfg" ] && [ "$cfg" != none ] && grep -q "^pull-config $cfg$" "$FAKE_LOG" && grep -q "^logout-config $cfg$" "$FAKE_LOG" && [ ! -d "$cfg" ]
  check "the login lives in a private docker config used for the pull; logout runs at exit and the directory is removed" $? "cfg=$cfg
$(cat "$FAKE_LOG")"
else
  echo "skip - token file mode checks (no POSIX permissions on $(uname -s))"
fi

# ------------------------------------------------------------------ 8. --update VERSION
new_env update
export FAKE_GHCR=401 FAKE_SEED_VER=0.3.0
touch "$FAKE_STATE/container" "$FAKE_STATE/image"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nGHCR_OWNER=nsu\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$E/answers.env"
out="$(inst --update 0.3.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.3.0" && log_has "docker tag ghcr.io/nsu/nsu-llm-main:0.3.0 nsu-llm-main:0.3.0" \
  && log_has "registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.3.0-cpu" \
  && log_has "registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.3.0-gpu"
check "--update 0.3.0: main image of that version pulled and tagged, both node images pushed to the registry" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
! log_has "docker rm" && ! log_has "docker run" && ! log_has "docker restart" && ! log_has "docker stop" && ! log_has "docker kill" && ! log_has "docker build" && [ ! -f "$ENVF" ]
check "--update never restarts or recreates main and does not rewrite .env" $? "$(cat "$FAKE_LOG")"
grep -q 'git checkout v0.3.0' <<<"$out" && grep -q 'ещё работает на прежней версии' <<<"$out"
check "--update says that restarting main is a separate step and how to do it" $? "$out"
out="$(inst --update 0.3.0 --dry-run --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'docker pull ghcr.io/nsu/nsu-llm-main:0.3.0' <<<"$out" && grep -q 'registry-push.sh 127.0.0.1:5000/' <<<"$out"
check "--update --dry-run prints the steps" $? "rc=$rc
$out"
# --update runs against the main that is still up: one from before SEC-2 has no registry-push.sh and a registry without a
# password on 127.0.0.1:<port>, so the push goes there as before
: >"$FAKE_LOG"
out="$(FAKE_NO_PUSH_HELPER=1 inst --update 0.3.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker exec llm-main k3s ctr -n k8s.io images push --plain-http --platform linux/amd64 127.0.0.1:5000/nsu-llm-node:0.3.0-cpu" \
  && ! log_has "docker exec llm-main /opt/llm/main/registry-push.sh"
check "--update against a main from before SEC-2 (no registry-push.sh): pushes without a password as that main expects" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
new_env update-none
export FAKE_GHCR=401
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
GHCR_OWNER=nsu
MAIN_ADVERTISE_IP=10.20.0.5
' >"$E/answers.env"
out="$(inst --update 0.3.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'главный не установлен' <<<"$out" && ! log_has "docker pull"
check "--update without an installed main: error, nothing pulled" $? "rc=$rc
$out"
for bad in 0.2 latest v0.3.0; do
  out="$(inst --update "$bad" --yes </dev/null)"; rc=$?
  [ "$rc" != 0 ] && grep -q -- '--update: нужна версия' <<<"$out"
  check "--update $bad: a malformed version is refused" $? "rc=$rc
$out"
done
new_env update-down
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
GHCR_OWNER=nsu
MAIN_ADVERTISE_IP=10.20.0.5
' >"$E/answers.env"
touch "$FAKE_STATE/container"
export FAKE_RUNNING=false
out="$(inst --update 0.3.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'не запущен' <<<"$out" && ! log_has "images pull"
check "--update with a stopped main: error before seeding" $? "rc=$rc
$out"
unset FAKE_RUNNING FAKE_GHCR FAKE_SEED_VER

# ------------------------------------------------------------------ 9. the restart after --update uses the pulled image whatever the first source was
# (the checkout of these scripts is version 0.2.1, so --update 0.2.1 stands for "update, then check out that version")
for src in build load pull; do
  new_env "upd-$src"
  export FAKE_GHCR=401 FAKE_SEED_VER=0.2.1
  echo tar >"$E/work/img.tar"
  printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=%s\nGHCR_OWNER=nsu\nIMAGE_TAR=%s\n' "$src" "$E/work/img.tar" >"$E/answers.env"
  out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  [ "$rc" = 0 ]; check "$src install: the first install works" $? "rc=$rc $out"
  : >"$FAKE_LOG"
  out="$(inst --update 0.2.1 --yes </dev/null)"; rc=$?
  grep -q "^IMAGE_SOURCE=$src\$" "$CONF" && grep -q '^PENDING_IMAGE_VERSION=0.2.1$' "$CONF" && grep -q 'PENDING_IMAGE_VERSION' <<<"$out" && ! log_has "docker rm" && ! log_has "docker run"
  check "$src install: --update 0.2.1 records the pending version, keeps IMAGE_SOURCE=$src, does not touch the container" $? "rc=$rc
$(cat "$CONF")
$out"
  : >"$FAKE_LOG"; rm -f "$FAKE_STATE/polls"
  out="$(inst --yes </dev/null)"; rc=$?
  [ "$rc" = 0 ] && log_has "docker run -d --name llm-main" && ! log_has "docker build" && ! log_has "docker load" && ! log_has "docker pull" && ! log_has "images pull" \
    && ! grep -q 'ghcr.io/v2' "$FAKE_LOG" && grep -q 'берётся уже скачанный образ' <<<"$out"
  check "$src install: the restart after --update neither builds, loads nor pulls: it recreates main from the image that was pulled" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
  grep -q "^IMAGE_SOURCE=$src\$" "$CONF" && ! grep -q 'PENDING_IMAGE_VERSION' "$CONF"
  check "$src install: after the restart the saved source is still $src and the pending mark is gone" $? "$(cat "$CONF")"
  : >"$FAKE_LOG"; rm -f "$FAKE_STATE/polls"
  out="$(inst --yes </dev/null)"; rc=$?
  case "$src" in build) log_has "docker build" ;; load) log_has "docker load" ;; pull) log_has "docker pull" ;; esac
  check "$src install: the next routine run uses the saved source $src again" $? "rc=$rc $(cat "$FAKE_LOG")"
done
new_env upd-other
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nGHCR_OWNER=nsu\n' >"$E/answers.env"
FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null >/dev/null
updout="$(FAKE_SEED_VER=0.3.0 inst --update 0.3.0 --yes </dev/null)"
: >"$FAKE_LOG"
out="$(FAKE_IMAGE=1 inst --yes </dev/null)"; rc=$?
grep -q 'git checkout v0.3.0' <<<"$out" && log_has "docker run -d" && grep -q '^PENDING_IMAGE_VERSION=0.3.0$' "$CONF"
check "a restart from a checkout of another version warns to check out the pulled one and keeps the pending mark" $? "rc=$rc $updout $out"

# a version that is already in the registry is not downloaded again; a digest that differs from release/<version>.json is
new_env seed-skip
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
export FAKE_GHCR=401 FAKE_REGISTRY_TAGS="0.2.1-cpu 0.2.1-gpu" FAKE_REGISTRY_DIGEST=sha256:aaaa
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "images pull" && ! log_has "registry-push.sh 127.0.0.1" && grep -q 'в реестре уже есть nsu-llm-node:0.2.1-cpu (sha256:aaaa)' <<<"$out" && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.1"
check "pull: both node images already in the registry are skipped (the main image is still pulled); no ctr pull or push" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
mkdir -p "$E/rel"
printf '{"images": {\n    "main": {"ref": "x", "digest": "sha256:cccc", "size_bytes": 1},\n    "node-cpu": {"ref": "x", "digest": "sha256:aaaa", "size_bytes": 1},\n    "node-gpu": {"ref": "x", "digest": "sha256:bbbb", "size_bytes": 1}\n  }\n}\n' >"$E/rel/0.2.1.json"
: >"$FAKE_LOG"
out="$(INST_RELEASE_DIR="$E/rel" inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:" && ! log_has "nsu-llm-node@sha256:aaaa" && log_has "images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node@sha256:bbbb" \
  && grep -q 'расходится с release/0.2.1.json' <<<"$out"
check "a registry digest that equals release/<version>.json is skipped, one that differs is downloaded again (by the release digest) with a warning" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
# SEC-2 M3: with release/<version>.json the main image is pulled by its digest, not by the tag a ghcr.io owner could overwrite
log_has "docker pull ghcr.io/nsu/nsu-llm-main@sha256:cccc" && ! log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.1" \
  && log_has "docker tag ghcr.io/nsu/nsu-llm-main@sha256:cccc nsu-llm-main:0.2.1" && grep -q 'совпал с release/0.2.1.json: sha256:cccc' <<<"$out"
check "release/<version>.json: the main image is pulled by its digest and tagged nsu-llm-main:<version>" $? "$(cat "$FAKE_LOG")"
: >"$FAKE_LOG"
out="$(FAKE_REGISTRY_TAGS="" INST_RELEASE_DIR="$E/rel" inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node@sha256:aaaa" \
  && log_has "images tag --force ghcr.io/nsu/nsu-llm-node@sha256:aaaa 127.0.0.1:5000/nsu-llm-node:0.2.1-cpu"
check "release/<version>.json: node images are pulled into main by their digests and tagged for the registry" $? "rc=$rc
$(cat "$FAKE_LOG")"
: >"$FAKE_LOG"
out="$(inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.1" && grep -q 'нет release/0.2.1.json: digest образа главного не с чем сверить' <<<"$out"
check "without release/<version>.json: pulled by the tag with a warning that the digest is not verified" $? "rc=$rc
$out"
unset FAKE_REGISTRY_TAGS FAKE_REGISTRY_DIGEST FAKE_GHCR FAKE_SEED_VER

# ------------------------------------------------------------------ 7b. the pause image goes into the registry of main (nodes pull it from docker.io, the mirror of main answers 404, and without Docker Hub no pod starts)
PAUSE_IMGS='docker.io/library/nsu-llm-node:0.2.1-cpu\ndocker.io/rancher/mirrored-pause:3.10.2\ndocker.io/rancher/mirrored-pause@sha256:f548e0e8e3dc1896ca956272154dde3314e8cc4fde0a57577ee9fa1c63f5baf4\ndocker.io/rancher/mirrored-coredns-coredns:1.12.0\n'
new_env pause
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
export FAKE_GHCR=401 FAKE_REGISTRY_TAGS="0.2.1-cpu 0.2.1-gpu"
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker exec llm-main k3s ctr -n k8s.io images tag --force docker.io/rancher/mirrored-pause:3.10.2 127.0.0.1:5000/rancher/mirrored-pause:3.10.2" \
  && log_has "docker exec llm-main /opt/llm/main/registry-push.sh 127.0.0.1:5000/rancher/mirrored-pause:3.10.2 --amd64-manifest" \
  && log_has "images rm --sync 127.0.0.1:5000/rancher/mirrored-pause:3.10.2" && ! log_has "images rm --sync docker.io/rancher" \
  && ! log_has "mirrored-pause@" && ! log_has "mirrored-coredns" && ! log_has "rancher/mirrored-pause:3.10.2 --platform" \
  && grep -q 'реестр главного: rancher/mirrored-pause:3.10.2' <<<"$out"
check "pause: the tag the containerd of main holds goes to rancher/mirrored-pause:<tag> in the registry (the amd64 manifest of the multi-platform index), the temporary tag is removed, the pause of k3s itself stays; digests and other images are ignored" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

: >"$FAKE_LOG"; rm -f "$FAKE_STATE/pause-pushed"
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" FAKE_PAUSE_IN_REGISTRY=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "mirrored-pause:3.10.2 127.0.0.1" && ! log_has "registry-push.sh 127.0.0.1:5000/rancher" && grep -q 'в реестре уже есть rancher/mirrored-pause:3.10.2' <<<"$out"
check "pause: already in the registry: skipped, nothing tagged or pushed" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

: >"$FAKE_LOG"; rm -f "$FAKE_STATE/pause-pushed"
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" FAKE_PAUSE_PUSH_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'запись 127.0.0.1:5000/rancher/mirrored-pause:3.10.2 в реестр главного не удалась' <<<"$out" && grep -q 'после записи в реестре нет rancher/mirrored-pause:3.10.2' <<<"$out" \
  && log_has "docker run -d" 
check "pause: a failed push only warns (Docker Hub fallback named), the install goes on" $? "rc=$rc
$out"

: >"$FAKE_LOG"; rm -f "$FAKE_STATE/pause-pushed"
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" FAKE_PAUSE_TAG_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'ctr images tag docker.io/rancher/mirrored-pause:3.10.2 127.0.0.1:5000/rancher/mirrored-pause:3.10.2 не удался' <<<"$out" && ! log_has "registry-push.sh 127.0.0.1:5000/rancher"
check "pause: a failed tag only warns and pushes nothing" $? "rc=$rc
$out"

: >"$FAKE_LOG"; rm -f "$FAKE_STATE/pause-pushed"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'в containerd главного нет образа docker.io/rancher/mirrored-pause' <<<"$out" && ! log_has "registry-push.sh 127.0.0.1:5000/rancher"
check "pause: no pause image in containerd of main: a warning naming Docker Hub, the install goes on" $? "rc=$rc
$out"

: >"$FAKE_LOG"; rm -f "$FAKE_STATE/pause-pushed"
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" FAKE_PAUSE_PUSH_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -qF 'образ главного старше 0.2.1 его не знает' <<<"$out" && ! log_has "images push" && log_has "images rm --sync 127.0.0.1:5000/rancher/mirrored-pause:3.10.2"
check "pause: a main whose registry-push.sh does not know --amd64-manifest (the helper refuses): the warning says to repeat after the update, no ctr push of the whole index, the temporary tag is removed" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

: >"$FAKE_LOG"; rm -f "$FAKE_STATE/pause-pushed"
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'dry-run.*mirrored-pause' <<<"$out" && ! log_has "images ls" && ! log_has "mirrored-pause"
check "pause: --dry-run prints the step and runs none of it" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
unset FAKE_GHCR FAKE_REGISTRY_TAGS

# ------------------------------------------------------------------ 8. IMAGE_SOURCE=load: the node images go into the registry of main from tar archives (airgapped main)
# load_env NAME: a scenario with the main archive and gzipped node archives next to it (what the stubs "import" is their content)
load_env() {
  new_env "$1"
  mkdir -p "$E/pkg"
  echo main-image-tar >"$E/pkg/nsu-llm-main.tar"
  printf 'CPU-IMAGE-BYTES' | gzip -c >"$E/pkg/nsu-llm-node-0.2.1-cpu.tar.gz"
  printf 'GPU-IMAGE-BYTES' | gzip -c >"$E/pkg/nsu-llm-node-0.2.1-gpu.tar.gz"
  printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=load\nIMAGE_TAR=%s\n' "$E/pkg/nsu-llm-main.tar" >"$E/answers.env"
}

# default: archives named nsu-llm-node-<version>-<cpu|gpu>.tar.gz next to IMAGE_TAR are found without any key
load_env load-default
export FAKE_JOIN_TOKEN='K10cafe::node:agent-tok-1'
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker load -i $E/pkg/nsu-llm-main.tar" && ! log_has "docker pull" && ! log_has "images pull"
check "load: the main archive is loaded, nothing is pulled" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
for v in cpu gpu; do
  log_has "docker exec -i llm-main k3s ctr -n k8s.io images import --no-unpack -" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images tag --force docker.io/library/nsu-llm-node:0.2.1-$v 127.0.0.1:5000/nsu-llm-node:0.2.1-$v" \
    && log_has "docker exec llm-main /opt/llm/main/registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-$v" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images rm --sync docker.io/library/nsu-llm-node:0.2.1-$v 127.0.0.1:5000/nsu-llm-node:0.2.1-$v"
  check "load: the $v node archive found next to IMAGE_TAR is imported into k3s of main, tagged for 127.0.0.1:5000, pushed (plain http) and the temporary tags are removed" $? "$(cat "$FAKE_LOG")"
done
[ "$(cat "$FAKE_STATE/import-stdin")" = "CPU-IMAGE-BYTESGPU-IMAGE-BYTES" ]
check "load: the archives reach ctr unpacked (gunzip -c), cpu first" $? "$(cat "$FAKE_STATE/import-stdin" 2>&1)"
l_run="$(grep -n '^docker run -d --name llm-main' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_imp="$(grep -n 'images import' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_tag="$(grep -n 'images tag --force docker.io/library/nsu-llm-node:0.2.1-cpu' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_push="$(grep -n 'registry-push.sh .*nsu-llm-node:0.2.1-cpu' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_list="$(grep -n 'tags/list' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
[ -n "$l_run" ] && [ -n "$l_imp" ] && [ -n "$l_tag" ] && [ -n "$l_push" ] && [ -n "$l_list" ] \
  && [ "$l_run" -lt "$l_imp" ] && [ "$l_imp" -lt "$l_tag" ] && [ "$l_tag" -lt "$l_push" ] && [ "$l_push" -lt "$l_list" ]
check "load: the registry is fed only after main is up: import, tag, push, then the tag list is verified" $? "$(cat "$FAKE_LOG")"
grep -q 'реестр главного: nsu-llm-node:0.2.1-cpu и 0.2.1-gpu' <<<"$out" && grep -qF 'run-node.sh --main 10.20.0.5 --token K10cafe::node:agent-tok-1 --from-main' <<<"$out"
check "load: the result says both images are in the registry and offers run-node.sh --from-main" $? "$out"
grep -q "^NODE_IMAGE_TAR_CPU=$E/pkg/nsu-llm-node-0.2.1-cpu.tar.gz\$" "$CONF" && grep -q "^NODE_IMAGE_TAR_GPU=$E/pkg/nsu-llm-node-0.2.1-gpu.tar.gz\$" "$CONF" \
  && ! grep -q 'NODE_IMAGE_TAR' "$ENVF"
check "load: the archives are saved for the next run and never leak into .env" $? "$(cat "$CONF")
$(cat "$ENVF")"

# re-run: the node images already in the registry are not imported again
: >"$FAKE_LOG"
out="$(FAKE_REGISTRY_TAGS="0.2.1-cpu 0.2.1-gpu" inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "images import" && ! log_has "registry-push.sh 127.0.0.1" && grep -q 'в реестре уже есть nsu-llm-node:0.2.1-gpu' <<<"$out" && log_has "docker load -i"
check "load: the re-run takes the saved archives, imports nothing that the registry already has" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

unset FAKE_JOIN_TOKEN

# keys in the answers file: other places, an uncompressed docker save; an empty key means «not needed»
load_env load-keys
mkdir -p "$E/elsewhere"
printf 'CPU-PLAIN-TAR' >"$E/elsewhere/cpu.tar"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=load\nIMAGE_TAR=%s\nNODE_IMAGE_TAR_CPU=%s\nNODE_IMAGE_TAR_GPU=\n' \
  "$E/pkg/nsu-llm-main.tar" "$E/elsewhere/cpu.tar" >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(cat "$FAKE_STATE/import-stdin")" = "CPU-PLAIN-TAR" ] && log_has "registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-cpu" \
  && ! log_has "nsu-llm-node:0.2.1-gpu 127.0.0.1" && grep -q 'nsu-llm-node:0.2.1-gpu в реестр не кладётся: нет архива (NODE_IMAGE_TAR_GPU' <<<"$out" \
  && grep -q 'реестр главного: nsu-llm-node:0.2.1-cpu$' <<<"$out"
check "load: NODE_IMAGE_TAR_CPU names an uncompressed archive anywhere; an empty NODE_IMAGE_TAR_GPU skips the gpu image with a warning" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# the «do not seed gpu» answer is saved: the re-run without --answers does not pick the gpu archive lying next to IMAGE_TAR
grep -q '^NODE_IMAGE_TAR_GPU=$' "$CONF" && ! grep -q 'NODE_IMAGE_TAR_GPU=.' "$CONF"
check "load: an explicit empty NODE_IMAGE_TAR_GPU is saved in install-main.env" $? "$(cat "$CONF")"
: >"$FAKE_LOG"
out="$(FAKE_REGISTRY_TAGS="0.2.1-cpu" inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "nsu-llm-node:0.2.1-gpu 127.0.0.1" && grep -q 'nsu-llm-node:0.2.1-gpu в реестр не кладётся' <<<"$out"
check "load: the re-run keeps skipping the gpu image although its archive is next to IMAGE_TAR" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# no node archives at all: nothing is imported, the registry is not even waited for
new_env load-none
echo main-image-tar >"$E/main.tar"
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=load\nIMAGE_TAR=%s\n' "$E/main.tar" >"$E/answers.env"
out="$(FAKE_REGISTRY_DOWN=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker load -i" && ! log_has "images import" && ! log_has "127.0.0.1:5000/v2/" \
  && grep -q 'образы нод в реестр главного не кладутся' <<<"$out"
check "load without node archives: the install works as before, the summary says the registry stays empty" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# a missing archive is an error before anything runs
load_env load-missing
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=load\nIMAGE_TAR=%s\nNODE_IMAGE_TAR_GPU=%s\n' "$E/pkg/nsu-llm-main.tar" "$E/pkg/nope.tar.gz" >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'NODE_IMAGE_TAR_GPU' <<<"$out" && grep -q 'nope.tar.gz не найден' <<<"$out" && ! log_has "docker load" && ! log_has "docker run -d"
check "load: a NODE_IMAGE_TAR_GPU that does not exist is refused up front, naming the key" $? "rc=$rc
$out"

# a broken archive and an archive with another image name stop the install with a hint
load_env load-broken
echo 'not a gzip' >"$E/pkg/nsu-llm-node-0.2.1-cpu.tar.gz"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -qF 'nsu-llm-node-0.2.1-cpu.tar.gz в k3s главного (gunzip | ctr images import)' <<<"$out" && ! log_has "registry-push.sh 127.0.0.1"
check "load: a corrupt archive stops before anything is pushed, with the docker save command in the message" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
load_env load-wrongname
out="$(FAKE_CTR_TAG_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'нет образа nsu-llm-node:0.2.1-cpu' <<<"$out" && ! log_has "registry-push.sh 127.0.0.1"
check "load: an archive that does not hold nsu-llm-node:<version>-cpu: error naming the image, nothing pushed" $? "rc=$rc
$out"
load_env load-pushfail
out="$(FAKE_CTR_PUSH_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'запись 127.0.0.1:5000/nsu-llm-node:0.2.1-cpu в реестр главного не удалась' <<<"$out"
check "load: a registry that refuses the push: the same error as for pull" $? "rc=$rc
$out"

# interactive: the question appears after the main archive, the archives next to it are the defaults
load_env load-ask
rm -f "$E/answers.env"
# answers: hostname, IP, secret, admins, proxies, cert mode, airgap, image source 3 (load), main archive, cpu archive (Enter: default), gpu archive, confirm
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsec-load\nadmin@g.nsu.ru\n\n\n1\nn\n3\n'"$E/pkg/nsu-llm-main.tar"$'\n\n'"$E/elsewhere-gpu.tar.gz"$'\n'"$E/pkg/nsu-llm-node-0.2.1-gpu.tar.gz"$'\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -qF "Архив образа ноды cpu для реестра главного" <<<"$out" && grep -qF "[$E/pkg/nsu-llm-node-0.2.1-cpu.tar.gz]" <<<"$out" \
  && grep -qF "elsewhere-gpu.tar.gz не найден" <<<"$out" && [ "$(cat "$FAKE_STATE/import-stdin")" = "CPU-IMAGE-BYTESGPU-IMAGE-BYTES" ]
check "load, interactive: both archives are asked with the files next to the main archive as defaults; a missing file is explained and asked again" $? "rc=$rc
$out"

# --dry-run: the commands are printed, nothing is imported
load_env load-dry
out="$(inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -qF "gunzip -c $E/pkg/nsu-llm-node-0.2.1-cpu.tar.gz | docker exec -i llm-main k3s ctr -n k8s.io images import --no-unpack -" <<<"$out" \
  && grep -q '/opt/llm/main/registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-gpu' <<<"$out" \
  && ! log_has "docker exec" && ! log_has "docker load" && ! log_has "docker run -d"
check "load: --dry-run prints the import, tag and push of the node archives and runs none of them" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# the pause image goes into the registry also when the node images come from archives (the pause of k3s is in containerd of main either way)
load_env load-pause
export FAKE_JOIN_TOKEN='K10cafe::node:agent-tok-1'
out="$(FAKE_CTR_IMAGES="$PAUSE_IMGS" inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker exec llm-main /opt/llm/main/registry-push.sh 127.0.0.1:5000/nsu-llm-node:0.2.1-cpu" \
  && log_has "docker exec llm-main /opt/llm/main/registry-push.sh 127.0.0.1:5000/rancher/mirrored-pause:3.10.2 --amd64-manifest" \
  && grep -q 'реестр главного: rancher/mirrored-pause:3.10.2' <<<"$out"
check "load: the pause image is pushed after the node images" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# --update never uses the saved archives: it pulls from ghcr.io as before
load_env load-update
inst --yes --answers "$E/answers.env" </dev/null >/dev/null
: >"$FAKE_LOG"
out="$(FAKE_GHCR=401 FAKE_SEED_VER=0.3.0 INST_OWNER=nsu inst --update 0.3.0 --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.3.0-cpu" && ! log_has "images import"
check "load: --update still pulls the new node images from ghcr.io, the saved archives are not used" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"


# ------------------------------------------------------------------ 11. ufw/firewalld active, ports not opened (FIREWALL=no): the hairpin of main needs one narrow rule
# The container reaches MAIN_ADVERTISE_IP:6443 through the INPUT chain of the host; without it api-advertise-ip.py logs «нет hairpin», the API is
# advertised on the container address, and kubectl logs/exec for pods on other machines (502 in the portal) do not work.
hp_env() { # hp_env NAME [extra answers...]: a first install with ufw active and the ports left alone
  new_env "$1"; shift
  export FAKE_UFW=active FAKE_BRIDGE_SUBNET=172.17.0.0/16 FAKE_IMAGE=1
  printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nFIREWALL=no\n' >"$E/answers.env"
  [ $# -eq 0 ] || printf '%s\n' "$@" >>"$E/answers.env"
}
hp_env hp-ufw
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
l_rule="$(grep -n '^ufw allow from 172.17.0.0/16 to any port 6443 proto tcp comment NSU LLM main hairpin$' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_run="$(grep -n '^docker run -d --name llm-main' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
[ "$rc" = 0 ] && [ -n "$l_rule" ] && [ -n "$l_run" ] && [ "$l_rule" -lt "$l_run" ] && ! log_has "ufw allow 443/tcp" && ! log_has "ufw allow 6443/tcp" && ! log_has "ufw allow 80/tcp"
check "ufw active, FIREWALL=no: only the hairpin rule (bridge subnet -> 6443/tcp) is added, before the container starts; the ports stay closed" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
grep -q 'подсеть Docker 172.17.0.0/16 -> 6443/tcp' <<<"$out" && grep -q 'не достаёт до 10.20.0.5:6443 (hairpin)' <<<"$out" && grep -q 'брандмауэр ufw: не трогать' <<<"$out"
check "the summary lists the hairpin rule and why it is needed" $? "$out"
grep -q '^HAIRPIN_RULE=yes' "$CONF" && grep -q '^HAIRPIN_SUBNET=172.17.0.0/16$' "$CONF" && ! grep -q 'HAIRPIN' "$ENVF"
check "the rule and its subnet are saved for the next run (and for --uninstall), never in .env" $? "$(cat "$CONF")"

# Docker does not name the subnet: the whole Docker address range
hp_env hp-ufw-fallback; unset FAKE_BRIDGE_SUBNET
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "ufw allow from 172.16.0.0/12 to any port 6443 proto tcp comment NSU LLM main hairpin"
check "no subnet from docker network inspect: 172.16.0.0/12" $? "rc=$rc
$(cat "$FAKE_LOG")"

# not needed / not wanted
hp_env hp-yes; printf 'FIREWALL=yes\n' >>"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "ufw allow 6443/tcp" && ! log_has "to any port 6443"
check "FIREWALL=yes: port 6443 is open to everybody, no extra hairpin rule" $? "rc=$rc
$(cat "$FAKE_LOG")"
hp_env hp-off HAIRPIN_RULE=no
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "to any port 6443" && ! grep -q 'HAIRPIN_SUBNET' "$CONF"
check "HAIRPIN_RULE=no: no rule" $? "rc=$rc
$(cat "$FAKE_LOG")"
new_env hp-nofw
unset FAKE_UFW
export FAKE_BRIDGE_SUBNET=172.17.0.0/16 FAKE_IMAGE=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nFIREWALL=no\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "ufw allow" && ! log_has "firewall-cmd --permanent" && ! grep -q 'hairpin' <<<"$out"
check "no active firewall: no rule, no question" $? "rc=$rc
$out"

# firewalld: a rich rule for the subnet
new_env hp-firewalld
unset FAKE_UFW
export FAKE_FIREWALLD=1 FAKE_BRIDGE_SUBNET=172.17.0.0/16 FAKE_IMAGE=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nFIREWALL=no\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has 'firewall-cmd --permanent --add-rich-rule=rule family="ipv4" source address="172.17.0.0/16" port port="6443" protocol="tcp" accept' && log_has "firewall-cmd --reload" \
  && ! log_has "--add-port"
check "firewalld active, FIREWALL=no: a rich rule for the Docker subnet on 6443/tcp, no ports opened" $? "rc=$rc
$(cat "$FAKE_LOG")"
unset FAKE_FIREWALLD

# interactive: a question after the one about the ports, the default is yes
new_env hp-ask
export FAKE_UFW=active FAKE_BRIDGE_SUBNET=172.17.0.0/16 FAKE_IMAGE=1
# answers: hostname, IP, secret, admins, proxies, cert mode, airgap, firewall NO, hairpin rule (Enter = yes), image source (Enter), confirm
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsec\nadmin@g.nsu.ru\n\n\n1\nn\nn\n\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -q 'Разрешить в ufw подсети Docker доступ к 6443/tcp хоста (hairpin)?' <<<"$out" && grep -q 'до 10.20.0.5:6443' <<<"$out" \
  && log_has "ufw allow from 172.17.0.0/16 to any port 6443 proto tcp comment NSU LLM main hairpin"
check "interactive: the hairpin question follows the firewall question, the default adds the rule" $? "rc=$rc
$out"
new_env hp-ask-no
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsec\nadmin@g.nsu.ru\n\n\n1\nn\nn\nn\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && ! log_has "to any port 6443" && grep -q '^HAIRPIN_RULE=no' "$CONF"
check "interactive: answering no adds nothing and is remembered" $? "rc=$rc
$out"
new_env hp-ask-fw-yes
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsec\nadmin@g.nsu.ru\n\n\n1\nn\ny\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'hairpin' <<<"$out"
check "interactive: with the ports opened there is no hairpin question" $? "rc=$rc
$out"

# --dry-run
hp_env hp-dry
out="$(inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'ufw allow from 172.17.0.0/16 to any port 6443 proto tcp comment' <<<"$out" && ! log_has "ufw allow" && [ ! -f "$CONF" ]
check "--dry-run prints the hairpin rule and applies nothing" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# --uninstall removes what was added, and only that
new_env hp-uninstall
touch "$FAKE_STATE/container"
printf 'CERT_MODE=internal\nAIRGAP=no\nFIREWALL=no\nHAIRPIN_RULE=yes\nHAIRPIN_SUBNET=172.17.0.0/16\nIMAGE_SOURCE=build\n' >"$CONF"
export FAKE_UFW=active
out="$(inst --uninstall --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "ufw delete allow from 172.17.0.0/16 to any port 6443 proto tcp" && ! log_has "ufw delete allow 6443/tcp" && ! log_has "ufw delete allow 443/tcp" \
  && grep -q 'будет удалено' <<<"$out"
check "--uninstall removes the hairpin rule it added and not the ports it never opened" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# ------------------------------------------------------------------ 12. no hairpin after the start: loud, exact command, exit code 3
HPLOG='Предупреждение: нет hairpin — нет соединения с 10.20.0.5:6443. API объявлен по адресу контейнера 172.17.0.3: логи и exec подов на других машинах работать не будут.'
hp_env hp-degraded HAIRPIN_RULE=no
out="$(FAKE_ENTRYPOINT_LOG="$HPLOG" inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 3 ] && grep -q 'установка неполная' <<<"$out" && grep -q 'Главный сервер NSU LLM Cluster готов' <<<"$out" \
  && grep -qF "sudo ufw allow from 172.17.0.0/16 to any port 6443 proto tcp comment 'NSU LLM main hairpin'" <<<"$out" \
  && grep -qF 'docker restart llm-main' <<<"$out" && grep -q 'Код выхода установщика: 3' <<<"$out" && grep -qF "$HPLOG" <<<"$out"
check "no hairpin and the rule declined: exit 3, the result is still printed, the exact ufw command and the restart are named" $? "rc=$rc
$out"
last="$(printf '%s\n' "$out" | grep -v '^$' | tail -n1)"
grep -q 'Код выхода установщика: 3' <<<"$last"
check "the degraded notice is the last thing printed" $? "last: $last"
hp_env hp-degraded-fw
export FAKE_FIREWALLD=1; unset FAKE_UFW
printf 'HAIRPIN_RULE=no\n' >>"$E/answers.env"
out="$(FAKE_ENTRYPOINT_LOG="$HPLOG" inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 3 ] && grep -qF "sudo firewall-cmd --permanent --add-rich-rule='rule family=\"ipv4\" source address=\"172.17.0.0/16\" port port=\"6443\" protocol=\"tcp\" accept' && sudo firewall-cmd --reload" <<<"$out"
check "no hairpin on firewalld: the exact firewall-cmd command" $? "rc=$rc
$out"
unset FAKE_FIREWALLD
# the rule is there and it still does not work: the message does not send the user to the same command
hp_env hp-degraded-rule
out="$(FAKE_ENTRYPOINT_LOG="$HPLOG" inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 3 ] && log_has "ufw allow from 172.17.0.0/16 to any port 6443" && grep -q 'дело не в нём' <<<"$out" && ! grep -qF 'sudo ufw allow from' <<<"$out" && grep -qF 'docker restart llm-main' <<<"$out"
check "no hairpin although the rule was added: exit 3, other causes named, no repeat of the rule" $? "rc=$rc
$out"
# no firewall at all
new_env hp-degraded-nofw
unset FAKE_UFW
export FAKE_IMAGE=1
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(FAKE_ENTRYPOINT_LOG="$HPLOG" inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 3 ] && grep -q 'ufw и firewalld не активны' <<<"$out" && grep -qF 'docker restart llm-main' <<<"$out"
check "no hairpin without a firewall: exit 3 with the general checks" $? "rc=$rc
$out"
# interactive too
hp_env hp-degraded-ask
answers=$'llm.nsu.ru\n\nhttps://idp.example.org/realms/main\nnsu-llm\n\nsec\nadmin@g.nsu.ru\n\n\n1\nn\nn\nn\n\ny\n'
out="$(FAKE_ENTRYPOINT_LOG="$HPLOG" inst <<<"$answers")"; rc=$?
[ "$rc" = 3 ] && grep -q 'установка неполная' <<<"$out"
check "no hairpin in the interactive mode: exit 3 too" $? "rc=$rc
$out"
# healthy
hp_env hp-healthy
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'неполная\|нет hairpin\|Код выхода установщика' <<<"$out"
check "hairpin works: exit 0, no notice" $? "rc=$rc
$out"
unset FAKE_UFW FAKE_BRIDGE_SUBNET FAKE_IMAGE FAKE_FIREWALLD

# ------------------------------------------------------------------ 13. no NSU assumptions: --yes needs the host and the OIDC provider, the wizard asks for them (2-36)
new_env oidc-yes-empty
printf 'IMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'PUBLIC_HOSTNAME, OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, ADMIN_EMAILS' <<<"$out" \
  && [ ! -f "$ENVF" ] && [ ! -f "$CONF" ] && ! log_has "docker run" && ! log_has "ufw allow" && ! log_has "docker build" \
  && ! grep -q 'double.fresh' "$FAKE_LOG"
check "--yes without any of the host / OIDC keys: one error naming all of them, nothing written, started or opened; no NSU default is used" $? "rc=$rc
$out"
for miss in PUBLIC_HOSTNAME OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET; do
  new_env "oidc-yes-no-$miss"
  printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' | grep -v "^$miss=" >"$E/answers.env"
  out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  [ "$rc" != 0 ] && grep -q "обязательные значения: $miss\\." <<<"$out" && [ ! -f "$ENVF" ] && ! log_has "docker run"
  check "--yes without only $miss: the error names exactly that key" $? "rc=$rc
$out"
done
new_env oidc-yes-env-file
# a key only in .env (a re-run, or a hand-edited .env) counts as set
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$ENVF"
out="$(FAKE_IMAGE=1 inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val OIDC_ISSUER "$ENVF")" = https://idp.example.org/realms/main ] && [ "$(env_val OIDC_CLIENT_ID "$ENVF")" = nsu-llm ]
check "--yes with all the keys in .env needs no answers file" $? "rc=$rc
$out"
new_env oidc-yes-http
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nOIDC_ISSUER=http://idp.example.org/realms/main\n' >"$E/answers.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'OIDC_ISSUER' <<<"$out" && grep -q 'только https' <<<"$out" && ! log_has "docker run"
check "an http:// OIDC_ISSUER is rejected (only https), nothing started" $? "rc=$rc
$out"

# the wizard: asks issuer, client id and a hidden secret, shows the redirect URI to register, asks the admins
new_env oidc-wizard
answers=$'idp-wizard.example.org\n\nhttps://sso.example.org/auth/realms/corp\nllm-portal\n\nW1zard-Secret-9\nroot@example.org\n\n\n1\nn\n\n\ny\n'
out="$(FAKE_IMAGE=1 inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -q 'OIDC_ISSUER' <<<"$out" && grep -q 'OIDC_CLIENT_ID' <<<"$out" && grep -q 'OIDC_CLIENT_SECRET, ввод скрыт' <<<"$out" && grep -q 'ADMIN_EMAILS' <<<"$out" \
  && [ "$(env_val PUBLIC_HOSTNAME "$ENVF")" = idp-wizard.example.org ] \
  && [ "$(env_val OIDC_ISSUER "$ENVF")" = https://sso.example.org/auth/realms/corp ] \
  && [ "$(env_val OIDC_CLIENT_ID "$ENVF")" = llm-portal ] \
  && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = W1zard-Secret-9 ] && [ "$(env_val ADMIN_EMAILS "$ENVF")" = root@example.org ]
check "wizard: asks the OIDC issuer, client id, secret and admins, writes them to .env" $? "rc=$rc
$out
$(cat "$ENVF" 2>&1)"
grep -qF 'Valid redirect URI:           https://idp-wizard.example.org/auth/callback' <<<"$out" \
  && grep -qF 'Valid post logout redirect:   https://idp-wizard.example.org/' <<<"$out" \
  && grep -qF 'redirect URI:       https://idp-wizard.example.org/auth/callback' <<<"$out"
check "wizard: shows the exact redirect URI to register (https://<PUBLIC_HOSTNAME>/auth/callback) before the OIDC questions and in the summary" $? "$out"
red_at="$(grep -n 'Valid redirect URI' <<<"$out" | head -n1 | cut -d: -f1)"; iss_at="$(grep -n 'Адрес OIDC-провайдера' <<<"$out" | head -n1 | cut -d: -f1)"
[ -n "$red_at" ] && [ -n "$iss_at" ] && [ "$red_at" -le "$iss_at" ]
check "wizard: the redirect URI is shown before the issuer is asked" $? "red=$red_at iss=$iss_at"
! grep -q 'W1zard-Secret-9' <<<"$out" && ! grep -q 'W1zard-Secret-9' "$FAKE_LOG" && ! grep -q 'W1zard-Secret-9' "$CONF" \
  && ! grep -rq 'W1zard-Secret-9' "$E/state" && grep -q 'клиентский секрет:  задан' <<<"$out"
check "wizard: the secret is not echoed, not in any command line (docker/ufw/curl stubs log argv), not in the saved answers or state" $? "$out
$(cat "$FAKE_LOG")"
grep -q 'пример NSU ID: https://sso-ng.nsu.ru/auth/realms/NSU' <<<"$out" && [ "$(env_val OIDC_ISSUER "$ENVF")" != "https://sso-ng.nsu.ru/auth/realms/NSU" ] \
  && [ "$(env_val PUBLIC_HOSTNAME "$ENVF")" != double.fresh.nsuts.ru ]
check "wizard: NSU ID is only an example in the prompt, not a default" $? "$out"

! grep -q 'ADMIN_REQUIRE_EMAIL_VERIFIED=false' <<<"$out" && [ "$(env_val ADMIN_REQUIRE_EMAIL_VERIFIED "$ENVF")" = true ] \
  && grep -q 'ADMIN_REQUIRE_EMAIL_VERIFIED; для NSU ID ответьте «нет»' <<<"$out" && grep -q 'LOGIN_PROVIDER_NAME: «Войти через' <<<"$out" \
  && grep -q 'кнопка «Войти через SSO»' <<<"$out"
check "wizard: the e-mail verification is asked with default yes (true in .env), the provider name with default SSO, both shown in the summary" $? "$out"
new_env oidc-verified-true
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
IMAGE_SOURCE=keep
ADMIN_REQUIRE_EMAIL_VERIFIED=true
' >"$E/answers.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'ADMIN_REQUIRE_EMAIL_VERIFIED=false' <<<"$out" && [ "$(env_val ADMIN_REQUIRE_EMAIL_VERIFIED "$ENVF")" = true ]
check "ADMIN_REQUIRE_EMAIL_VERIFIED=true from the answers: no warning, written to .env" $? "rc=$rc
$out"

new_env oidc-wizard-reject
answers=$'llm.example.org\n\nhttp://insecure.example.org\n\nhttps://sso.example.org/realms/x\nmy-client\n\nSecretInput-1\na@example.org\n\n\n1\nn\n\n\ny\n'
out="$(FAKE_IMAGE=1 inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -q 'только https' <<<"$out" && [ "$(env_val OIDC_ISSUER "$ENVF")" = https://sso.example.org/realms/x ] && ! grep -q 'SecretInput-1' <<<"$out"
check "wizard: an http or empty issuer is explained and asked again" $? "rc=$rc
$out"

# NSU ID typed by the operator works like any other provider
new_env oidc-nsuid
# provider name: Enter takes the suggestion (NSU ID for the NSU issuer); the e-mail verification answered «нет» as NSU ID needs
answers=$'llm.nsu.example\n\nhttps://sso-ng.nsu.ru/auth/realms/NSU\nmy.nsu.client\n\nnsu-secret-2\nadmin@g.nsu.ru\nнет\n\n1\nn\n\n\ny\n'
out="$(FAKE_IMAGE=1 inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val OIDC_ISSUER "$ENVF")" = https://sso-ng.nsu.ru/auth/realms/NSU ] && [ "$(env_val OIDC_CLIENT_ID "$ENVF")" = my.nsu.client ] \
  && [ "$(env_val LOGIN_PROVIDER_NAME "$ENVF")" = "NSU ID" ] && [ "$(env_val ADMIN_REQUIRE_EMAIL_VERIFIED "$ENVF")" = false ] \
  && grep -q 'ADMIN_REQUIRE_EMAIL_VERIFIED=false: роль администратора определяет только ADMIN_EMAILS' <<<"$out" && grep -q 'кнопка «Войти через NSU ID»' <<<"$out"
check "NSU ID typed as the issuer: provider name suggested as NSU ID, e-mail verification off with a warning, nothing is a default" $? "rc=$rc
$out"
# a custom provider name; --yes takes SSO when none is given
new_env oidc-provider-name
answers=$'llm.example.org\n\nhttps://sso.example.org/realms/x\nmy-client\nCampus SSO\nname-sec\na@example.org\n\n\n1\nn\n\n\ny\n'
out="$(FAKE_IMAGE=1 inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val LOGIN_PROVIDER_NAME "$ENVF")" = "Campus SSO" ] && grep -q 'кнопка «Войти через Campus SSO»' <<<"$out"
check "wizard: a typed provider name goes to .env (with a space) and the summary" $? "rc=$rc
$out"
new_env oidc-provider-yes
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val LOGIN_PROVIDER_NAME "$ENVF")" = SSO ] && [ "$(env_val ADMIN_REQUIRE_EMAIL_VERIFIED "$ENVF")" = true ]
check "--yes: LOGIN_PROVIDER_NAME defaults to SSO, ADMIN_REQUIRE_EMAIL_VERIFIED to true" $? "rc=$rc
$out"
new_env oidc-provider-yes-nsu
printf "PUBLIC_HOSTNAME=llm.nsu.ru\nOIDC_ISSUER=https://sso-ng.nsu.ru/auth/realms/NSU\nOIDC_CLIENT_ID=c\nOIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n" >"$E/answers2.env"
out="$(FAKE_IMAGE=1 inst --yes --answers "$E/answers2.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val LOGIN_PROVIDER_NAME "$ENVF")" = "NSU ID" ]
check "--yes with the NSU issuer: LOGIN_PROVIDER_NAME defaults to NSU ID" $? "rc=$rc
$out"

# OIDC_REDIRECT_URI set (a bridge through another domain): the wizard shows it instead of /auth/callback
new_env oidc-bridge
printf 'OIDC_REDIRECT_URI=https://front.example.org/llm-auth/callback\nOIDC_POST_LOGOUT_REDIRECT_URI=https://front.example.org/llm-auth/logged-out\n' >"$E/answers.env"
answers=$'llm.example.org\n\nhttps://sso.example.org/realms/x\nmy-client\n\nbridge-sec\na@example.org\n\n\n1\nn\n\n\ny\n'
out="$(FAKE_IMAGE=1 inst --answers "$E/answers.env" <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -qF 'Valid redirect URI:           https://front.example.org/llm-auth/callback' <<<"$out" \
  && grep -qF 'Valid post logout redirect:   https://front.example.org/llm-auth/logged-out' <<<"$out" && ! grep -q 'llm.example.org/auth/callback' <<<"$out" \
  && [ "$(env_val OIDC_REDIRECT_URI "$ENVF")" = https://front.example.org/llm-auth/callback ]
check "OIDC_REDIRECT_URI from the answers is the one shown to register (and kept in .env)" $? "rc=$rc
$out"

# GHCR_OWNER: offered with the default in the wizard (Enter keeps it), overridable by the environment and the answers
new_env owner-default-wizard
export FAKE_GHCR=401
answers=$'llm.example.org\n\nhttps://sso.example.org/realms/x\nmy-client\n\nowner-sec\na@example.org\n\n\n1\nn\n1\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -qF 'nsu-llm-main) [denmurzik]' <<<"$out" && grep -q '^docker pull ghcr.io/denmurzik/nsu-llm-main:0.2.1$' "$FAKE_LOG" \
  && grep -q '^GHCR_OWNER=denmurzik' "$CONF"
check "wizard: GHCR_OWNER is offered as [denmurzik] and Enter keeps it (images ghcr.io/denmurzik/nsu-llm-main)" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
new_env owner-override
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=otherorg\n' >"$E/answers.env"
out="$(INST_OWNER=envorg inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/otherorg/nsu-llm-main:0.2.1" && ! log_has "denmurzik"
check "GHCR_OWNER from the answers overrides the default (and the environment)" $? "rc=$rc
$out"
new_env owner-env
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\n' >"$E/answers.env"
out="$(INST_OWNER=envorg inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/envorg/nsu-llm-main:0.2.1" && ! log_has "denmurzik"
check "GHCR_OWNER from the environment overrides the default" $? "rc=$rc
$out"
new_env owner-default-plan
printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\n' >"$E/answers.env"
out="$(inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'ghcr.io/denmurzik/nsu-llm-node:0.2.1-cpu' <<<"$out" && grep -q 'ghcr.io/denmurzik/nsu-llm-main:0.2.1' <<<"$out"
check "the plan names the default images ghcr.io/denmurzik/nsu-llm-main and ghcr.io/denmurzik/nsu-llm-node" $? "rc=$rc
$out"
unset FAKE_GHCR


# ------------------------------------------------------------------ NODE_IMAGE_TAG left behind in .env by an older version
VER="$(tr -d '[:space:]' <"$MAIN/../node/IMAGE_VERSION")"
# tag_env NAME TAG: an installed, running main whose .env says NODE_IMAGE_TAG=TAG
tag_env() {
  new_env "$1"
  printf "$IDP_KV"'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nNODE_IMAGE_TAG=%s\n' "$2" >"$ENVF"
  printf 'IMAGE_SOURCE=keep\nAIRGAP=no\n' >"$E/answers.env"
  touch "$FAKE_STATE/container"
}
log_line() { grep -nF -- "$1" "$FAKE_LOG" | head -n1 | cut -d: -f1; }
export FAKE_IMAGE=1 FAKE_REGISTRY_DIGEST=sha256:cafe

tag_env tag-keep-source 0.1.1
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1 ] && grep -q "NODE_IMAGE_TAG=0.1.1 старше этой версии ($VER), но в реестре главного нет образов $VER-cpu и $VER-gpu: тег оставлен" <<<"$out"
check "keep, no images of this version in the registry: the tag stays, with a warning" $? "rc=$rc
$out"

tag_env tag-half 0.1.1
export FAKE_REGISTRY_TAGS="$VER-gpu"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1 ]
check "only one of the two images in the registry: the tag stays" $? "rc=$rc
$out"

tag_env tag-old 0.1.1
export FAKE_REGISTRY_TAGS="$VER-cpu $VER-gpu"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = "$VER" ] && grep -q "NODE_IMAGE_TAG в .* был 0.1.1, это старая версия: записан $VER" <<<"$out"
check "both images in the registry: an older tag becomes the checkout version, and the installer says so" $? "rc=$rc
$out"
[ "$(grep -c '^NODE_IMAGE_TAG=' "$ENVF")" = 1 ]
check "the line is replaced, not added" $? "$(cat "$ENVF")"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! grep -q 'NODE_IMAGE_TAG' <<<"$out"
check "a current NODE_IMAGE_TAG: not a word about it" $? "rc=$rc
$out"

tag_env tag-pull 0.1.1
export INST_OWNER=nsu
printf 'IMAGE_SOURCE=pull\nAIRGAP=no\n' >"$E/answers.env"
export FAKE_GHCR=200 FAKE_REGISTRY_TAGS="$VER-cpu $VER-gpu"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
seed="$(log_line "manifests/$VER-cpu")"; start="$(log_line 'docker run ')"
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = "$VER" ] && [ -n "$seed" ] && [ -n "$start" ] && [ "$seed" -lt "$start" ]
check "pull on a running main: the registry first, then the tag, then the restart" $? "rc=$rc seed=$seed start=$start
$out
$(cat "$FAKE_LOG")"

tag_env tag-pull-fail 0.1.1
printf 'IMAGE_SOURCE=pull\nAIRGAP=no\n' >"$E/answers.env"
unset FAKE_REGISTRY_TAGS
export FAKE_CTR_PULL_FAIL=1
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1 ] && ! log_has "docker run "
check "the images do not reach the registry: main is not restarted, the tag stays" $? "rc=$rc
$out"
unset FAKE_CTR_PULL_FAIL FAKE_GHCR INST_OWNER

tag_env tag-fat "$VER-fat"
export FAKE_REGISTRY_TAGS="$VER-cpu $VER-gpu"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = "$VER-fat" ] && ! grep -q 'NODE_IMAGE_TAG' <<<"$out"
check "the fallback tag <version>-fat survives" $? "rc=$rc
$out"
tag_env tag-oldfat 0.1.1-fat
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1-fat ]
check "any tag that is no plain version survives" $? "rc=$rc
$out"
tag_env tag-new 99.0.0
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 99.0.0 ]
check "a version newer than the checkout survives" $? "rc=$rc
$out"

tag_env tag-pinned 0.1.1
echo 'NODE_IMAGE_TAG=0.1.1' >>"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1 ] && grep -q 'NODE_IMAGE_TAG=0.1.1 задан вами' <<<"$out" && [ "$(env_val NODE_IMAGE_TAG_KEEP "$CONF")" = 0.1.1 ]
check "an older tag named in the answers file is the operator's: kept, with a warning, and remembered" $? "rc=$rc
$out"
out="$(inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1 ] && ! grep -q '^NODE_IMAGE_TAG_KEEP' "$ENVF"
check "the next run without --answers keeps the operator's tag too" $? "rc=$rc
$out"

tag_env tag-dry 0.1.1
out="$(inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && [ "$(env_val NODE_IMAGE_TAG "$ENVF")" = 0.1.1 ] && grep -q 'NODE_IMAGE_TAG=0.1.1 старше этой версии' <<<"$out"
check "--dry-run with keep says the tag stays and changes nothing" $? "rc=$rc
$out"
unset FAKE_IMAGE FAKE_REGISTRY_TAGS FAKE_REGISTRY_DIGEST

bash -n "$INSTALL"
check "install-main.sh parses (bash -n)" $?

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
