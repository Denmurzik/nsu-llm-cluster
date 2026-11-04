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
  mktemp cmp touch printf od; do
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
      *tags/list*) printf '{"name":"nsu-llm-node","tags":["%s-cpu","%s-gpu"]}' "${FAKE_SEED_VER:-0.1.0}" "${FAKE_SEED_VER:-0.1.0}" ;;
      *"images pull"*) [ "${FAKE_CTR_PULL_FAIL:-0}" = 1 ] && exit 1 ;;
      *"images import"*) cat >>"$FAKE_STATE/import-stdin" ;;
      # registry_digest: HEAD of a manifest; a tag listed in FAKE_REGISTRY_TAGS is there (digest FAKE_REGISTRY_DIGEST)
      *manifests/*) all="$*"; tag="${all##*manifests/}"; case " ${FAKE_REGISTRY_TAGS:-} " in *" $tag "*) printf 'HTTP/1.1 200 OK\r\nDocker-Content-Digest: %s\r\n\r\n' "${FAKE_REGISTRY_DIGEST:-sha256:aaaa}" ;; *) exit 22 ;; esac ;;
      *"images push"*) [ "${FAKE_CTR_PUSH_FAIL:-0}" = 1 ] && exit 1 ;;
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
  [ -z "${INST_RELEASE_DIR:-}" ] || vars+=(NSU_LLM_RELEASE_DIR="$INST_RELEASE_DIR")
  for v in $(compgen -v FAKE_); do vars+=("$v=${!v}"); done
  env -i "${vars[@]}" "$BASH_BIN" "$INSTALL" --env-file "$ENVF" "$@" 2>&1
}
env_val() { sed -n "s/^$1=//p" "$2" | tail -n1; }
log_has() { grep -qF -- "$1" "$FAKE_LOG"; }

[ -f "$INSTALL" ] || { echo "FAIL - $INSTALL does not exist"; echo; echo "SOME FAILED"; exit 1; }

# ------------------------------------------------------------------ 1. interactive first install (Ubuntu, ufw active)
new_env first
export FAKE_UFW=active FAKE_SECRETS='CLUSTER_TOKEN=c1\nAGENT_TOKEN=agent-tok-1\n' FAKE_JOIN_TOKEN='K10cafe::node:agent-tok-1'
# answers: hostname, IP, secret (hidden), admins, front proxies, cert mode, airgap, firewall, image source, confirm
answers=$'llm.nsu.ru\n\nsuper-secret-1\nadmin@g.nsu.ru, second@g.nsu.ru\n10.4.0.105, 10.0.0.0/8\n1\nn\ny\n\ny\n'
out="$(inst <<<"$answers")"; rc=$?
check "first install exits 0" "$([ "$rc" = 0 ]; echo $?)" "rc=$rc
$out"
[ "$(env_val PUBLIC_HOSTNAME "$ENVF")" = llm.nsu.ru ] && [ "$(env_val MAIN_ADVERTISE_IP "$ENVF")" = 10.20.0.5 ] \
  && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = super-secret-1 ] \
  && [ "$(env_val ADMIN_EMAILS "$ENVF")" = "admin@g.nsu.ru,second@g.nsu.ru" ] \
  && [ "$(env_val ADMIN_REQUIRE_EMAIL_VERIFIED "$ENVF")" = false ] && [ "$(env_val LITELLM_WORKERS "$ENVF")" = 4 ]
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
log_has "docker build --provenance=false -f $MAIN/Dockerfile -t nsu-llm-main:0.1.0" \
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
printf 'PUBLIC_HOSTNAME=bad_host!\nOIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'PUBLIC_HOSTNAME' <<<"$out" && ! log_has "docker run"
check "--yes with an invalid host name: error, nothing started" $? "rc=$rc
$out"
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
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
answers=$'bad host\nllm.nsu.ru\n999.1.1.1\n\n\nsec\nnot-an-email\na@g.nsu.ru\n10.4.0.105 }\n10.4.0.256,\n\n\n\n\ny\n'
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
out="$(FAKE_IMAGE=1 inst <<<$'llm.nsu.ru\n\ns\na@g.nsu.ru\n\n\n\n\nn\n')"; rc=$?
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
answers=$'y\nllm.nsu.ru\n\ns\na@g.nsu.ru\n\n\n\n\ny\n'
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'INSTALL_DOCKER=yes' <<<"$out" && ! log_has "apt-get"
check "--yes without INSTALL_DOCKER=yes never installs Docker on its own" $? "rc=$rc
$out"
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nINSTALL_DOCKER=yes\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "apt-get install -y docker-ce" && grep -q '^pkg-env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a$' "$FAKE_LOG" \
  && ! grep -q '^pkg-env DEBIAN_FRONTEND= ' "$FAKE_LOG"
check "--yes Docker install on Ubuntu runs apt non-interactively (DEBIAN_FRONTEND=noninteractive, NEEDRESTART_MODE=a)" $? "rc=$rc
$(grep pkg-env "$FAKE_LOG")
$out"
new_env rocky
os_rocky; rm "$E/bin/docker"
export FAKE_FIREWALLD=1
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nINSTALL_DOCKER=yes\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nINSTALL_DOCKER=yes\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=x
ADMIN_EMAILS=a@g.nsu.ru
INSTALL_DOCKER=yes
' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! grep -q '^dnf .*remove' "$FAKE_LOG" && ! grep -q 'Удалены конфликтующие' <<<"$out"
check "RHEL family without conflicting packages: nothing is removed" $? "rc=$rc
$out"
new_env rocky-fail
os_rocky; rm "$E/bin/docker"
printf 'OIDC_CLIENT_SECRET=x
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'обновлен' <<<"$out"
check "ports held by the existing llm-main are not a conflict (update)" $? "rc=$rc
$out"
unset FAKE_SS FAKE_IMAGE
# a host without a default route (airgap): the installer says so and goes on instead of dying silently under set -e
new_env noroute
export FAKE_NOROUTE=1 FAKE_IMAGE=1
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'нет маршрута по умолчанию' <<<"$out" && grep -q 'MAIN_ADVERTISE_IP' <<<"$out" && ! log_has "docker run"
check "no default route, no MAIN_ADVERTISE_IP: warning, then an error that names MAIN_ADVERTISE_IP" $? "rc=$rc
$out"
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nMAIN_ADVERTISE_IP=10.99.0.7\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'нет маршрута по умолчанию' <<<"$out" && [ "$(env_val MAIN_ADVERTISE_IP "$ENVF")" = 10.99.0.7 ] && log_has "docker run"
check "no default route but MAIN_ADVERTISE_IP given: the install goes through" $? "rc=$rc
$out"
unset FAKE_NOROUTE
new_env dffail
printf 'OIDC_CLIENT_SECRET=x
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=x
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nCERT_MODE=own\nCERTS_DIR=%s\nAIRGAP=yes\nIMAGE_SOURCE=keep\n' "$E/certs" >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=sec-1\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nHF_TOKEN=hf_tok_x\nDB_PASSWORD=pw_x\nMY_SECRET=sc_x\nPLAIN=visible\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=x\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\n' >"$E/answers.env"
out="$(FAKE_IMAGE=1 INST_TTY=0 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ]
check "--yes needs no terminal" $? "rc=$rc
$out"

new_env childin
export FAKE_CHILD_READS_STDIN=1 FAKE_UFW=active
answers=$'llm.nsu.ru\n\nsecret-1\nadmin@g.nsu.ru\n\n1\nn\ny\n\ny\nLEAKED-INTO-A-CHILD\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && ! grep -q LEAKED "$FAKE_STATE/child_stdin" 2>/dev/null && grep -q '^docker run -d --name llm-main' "$FAKE_LOG"
check "commands the installer starts never read its answers (stdin is /dev/null for them)" $? "rc=$rc child=$(cat "$FAKE_STATE/child_stdin" 2>&1)
$out"
unset FAKE_CHILD_READS_STDIN FAKE_UFW

new_env prec
printf 'OIDC_CLIENT_SECRET=from-env-file\nADMIN_EMAILS=a@g.nsu.ru\nPUBLIC_HOSTNAME=llm.nsu.ru\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$ENVF"
out="$(FAKE_IMAGE=1 INST_SECRET=from-environment inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'OIDC_CLIENT_SECRET' <<<"$out" && grep -q 'переменной окружения' <<<"$out" && [ "$(env_val OIDC_CLIENT_SECRET "$ENVF")" = from-env-file ] \
  && ! grep -q 'from-env' <<<"$out"
check "a secret in the environment does not override .env: said so, no value printed" $? "rc=$rc
$out"


# ------------------------------------------------------------------ 7. IMAGE_SOURCE=pull: ghcr.io, then the node images into the registry of main
new_env pull
export FAKE_GHCR=401
# answers: hostname, IP, secret, admins, cert mode, airgap, image source 1 (pull), owner, confirm
answers=$'llm.nsu.ru\n\nsec-pull\nadmin@g.nsu.ru\n\n1\nn\n1\nnsu\ny\n'
out="$(inst <<<"$answers")"; rc=$?
[ "$rc" = 0 ] && grep -q '1 — скачать с ghcr.io (pull)' <<<"$out" && grep -q 'ghcr.io доступен' <<<"$out"
check "pull, interactive: ghcr.io is offered first when reachable, then the owner is asked" $? "rc=$rc
$out"
l_pull="$(grep -n '^docker pull ghcr.io/nsu/nsu-llm-main:0.1.0$' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_tag="$(grep -n '^docker tag ghcr.io/nsu/nsu-llm-main:0.1.0 nsu-llm-main:0.1.0$' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_run="$(grep -n '^docker run -d --name llm-main' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
l_seed="$(grep -n 'images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.1.0-cpu' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
[ -n "$l_pull" ] && [ -n "$l_tag" ] && [ -n "$l_run" ] && [ -n "$l_seed" ] && [ "$l_pull" -lt "$l_tag" ] && [ "$l_tag" -lt "$l_run" ] && [ "$l_run" -lt "$l_seed" ] \
  && ! log_has "docker build" && ! log_has "docker load"
check "pull: docker pull + tag as nsu-llm-main:0.1.0 before the container starts, no build; node images only after it is up" $? "$(cat "$FAKE_LOG")"
for v in cpu gpu; do
  log_has "docker exec llm-main k3s ctr -n k8s.io images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.1.0-$v" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images tag --force ghcr.io/nsu/nsu-llm-node:0.1.0-$v 127.0.0.1:5000/nsu-llm-node:0.1.0-$v" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images push --plain-http --platform linux/amd64 127.0.0.1:5000/nsu-llm-node:0.1.0-$v" \
    && log_has "docker exec llm-main k3s ctr -n k8s.io images rm --sync ghcr.io/nsu/nsu-llm-node:0.1.0-$v 127.0.0.1:5000/nsu-llm-node:0.1.0-$v"
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
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.1.0" && log_has "docker rm -f llm-main" && log_has "images push --plain-http"
check "pull: the re-run takes the saved choice without questions, updates the image and the registry content" $? "rc=$rc
$out"

# --yes: answers file with IMAGE_REF and NODE_IMAGE_SOURCE (a private mirror)
new_env pull-mirror
export FAKE_GHCR=401
cat >"$E/answers.env" <<'EOF'
OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
IMAGE_SOURCE=pull
GHCR_OWNER=nsu
IMAGE_REF=ghcr.io/mirror/main-copy:0.1.0
NODE_IMAGE_SOURCE=ghcr.io/mirror/node-copy
EOF
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/mirror/main-copy:0.1.0" && log_has "docker tag ghcr.io/mirror/main-copy:0.1.0 nsu-llm-main:0.1.0" \
  && log_has "images pull --platform linux/amd64 ghcr.io/mirror/node-copy:0.1.0-gpu" && ! grep -q '^GHCR_OWNER=\|^IMAGE_REF=' "$ENVF"
check "--yes: IMAGE_REF and NODE_IMAGE_SOURCE override the defaults; installer keys never leak into .env" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# default: owner known and ghcr.io reachable -> pull is the default; unreachable -> build, and the prompt says why
new_env pull-default
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.1.0" && ! log_has "docker build"
check "--yes with a known owner and a reachable ghcr.io: pull is the default" $? "rc=$rc
$out"
new_env pull-default-down
export FAKE_GHCR=000
printf 'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
GHCR_OWNER=nsu
' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "docker pull" && log_has "docker build"
check "ghcr.io unreachable: the default stays build (no behaviour change for an offline install)" $? "rc=$rc
$out"
new_env pull-prompt-down
out="$(inst <<<$'llm.nsu.ru\n\ns\na@g.nsu.ru\n\n\n\ny\n')"; rc=$?
grep -q 'скачивание с ghcr.io недоступно' <<<"$out" && ! grep -q '1 — скачать с ghcr.io' <<<"$out"
check "interactive question without a reachable ghcr.io does not offer pull and says why" $? "rc=$rc
$out"

# unreachable / blocked ghcr.io with pull chosen explicitly
for code in 000 403; do
  new_env "pull-$code"
  export FAKE_GHCR=$code
  printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
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
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'GHCR_OWNER' <<<"$out" && ! log_has "docker pull"
check "pull without GHCR_OWNER under --yes: error naming the key" $? "rc=$rc
$out"
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=Bad_Owner\n' >"$E/answers.env"
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'GHCR_OWNER' <<<"$out" && ! log_has "docker pull"
check "an invalid GHCR_OWNER is explained" $? "rc=$rc
$out"
new_env pull-fail
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\n' >"$E/answers.env"
out="$(INST_OWNER=nsu inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.1.0"
check "GHCR_OWNER from the environment is accepted" $? "rc=$rc
$out"
new_env pull-fail2
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(FAKE_PULL_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'docker pull ghcr.io/nsu/nsu-llm-main:0.1.0 не удался' <<<"$out" && ! log_has "docker run -d"
check "a failed docker pull stops before the container is touched" $? "rc=$rc
$out"
new_env seed-fail
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(FAKE_CTR_PULL_FAIL=1 inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'не удалось скачать ghcr.io/nsu/nsu-llm-node:0.1.0-cpu' <<<"$out" && grep -q 'HTTPS_PROXY' <<<"$out"
check "a failed pull of a node image inside main: Russian error with the retry command and the proxy hint" $? "rc=$rc
$out"
new_env seed-down
printf 'OIDC_CLIENT_SECRET=s
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
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
out="$(inst --yes --dry-run --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'docker pull ghcr.io/nsu/nsu-llm-main:0.1.0' <<<"$out" && grep -q 'docker tag ghcr.io/nsu/nsu-llm-main:0.1.0 nsu-llm-main:0.1.0' <<<"$out" \
  && grep -q 'k3s ctr -n k8s.io images push --plain-http --platform linux/amd64 127.0.0.1:5000/nsu-llm-node:0.1.0-gpu' <<<"$out" \
  && ! log_has "docker pull" && ! log_has "docker exec llm-main k3s ctr" && ! log_has "docker run -d"
check "--dry-run with pull prints the pull, the tag and the registry seeding and runs none of them" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"

# token file for a private mirror
new_env pull-token
export FAKE_GHCR=401
printf 'fake-private-token-123\n' >"$E/work/ghcr-token"
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\nGHCR_TOKEN_FILE=%s\n' "$E/work/ghcr-token" >"$E/answers.env"
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
  grep -q '^docker pull ghcr.io/nsu/nsu-llm-node:0.1.0-gpu$' "$FAKE_LOG" && grep -q '^docker save 127.0.0.1:5000/nsu-llm-node:0.1.0-gpu$' "$FAKE_LOG" \
    && grep -q '^docker exec -i llm-main k3s ctr -n k8s.io images import --no-unpack -$' "$FAKE_LOG" && grep -q 'TARBALL-OF save 127.0.0.1:5000/nsu-llm-node:0.1.0-gpu' "$FAKE_STATE/import-stdin" \
    && ! grep -q 'images pull' "$FAKE_LOG" && grep -q 'images push --plain-http --platform linux/amd64 127.0.0.1:5000/nsu-llm-node:0.1.0-gpu' "$FAKE_LOG"
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
export FAKE_GHCR=401 FAKE_SEED_VER=0.2.0
touch "$FAKE_STATE/container" "$FAKE_STATE/image"
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nGHCR_OWNER=nsu\nMAIN_ADVERTISE_IP=10.20.0.5\n' >"$E/answers.env"
out="$(inst --update 0.2.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.2.0" && log_has "docker tag ghcr.io/nsu/nsu-llm-main:0.2.0 nsu-llm-main:0.2.0" \
  && log_has "images push --plain-http --platform linux/amd64 127.0.0.1:5000/nsu-llm-node:0.2.0-cpu" \
  && log_has "images push --plain-http --platform linux/amd64 127.0.0.1:5000/nsu-llm-node:0.2.0-gpu"
check "--update 0.2.0: main image of that version pulled and tagged, both node images pushed to the registry" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
! log_has "docker rm" && ! log_has "docker run" && ! log_has "docker restart" && ! log_has "docker stop" && ! log_has "docker kill" && ! log_has "docker build" && [ ! -f "$ENVF" ]
check "--update never restarts or recreates main and does not rewrite .env" $? "$(cat "$FAKE_LOG")"
grep -q 'git checkout v0.2.0' <<<"$out" && grep -q 'ещё работает на прежней версии' <<<"$out"
check "--update says that restarting main is a separate step and how to do it" $? "$out"
out="$(inst --update 0.2.0 --dry-run --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && grep -q 'docker pull ghcr.io/nsu/nsu-llm-main:0.2.0' <<<"$out" && grep -q 'images push --plain-http' <<<"$out"
check "--update --dry-run prints the steps" $? "rc=$rc
$out"
new_env update-none
export FAKE_GHCR=401
printf 'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
GHCR_OWNER=nsu
MAIN_ADVERTISE_IP=10.20.0.5
' >"$E/answers.env"
out="$(inst --update 0.2.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'главный не установлен' <<<"$out" && ! log_has "docker pull"
check "--update without an installed main: error, nothing pulled" $? "rc=$rc
$out"
for bad in 0.2 latest v0.2.0; do
  out="$(inst --update "$bad" --yes </dev/null)"; rc=$?
  [ "$rc" != 0 ] && grep -q -- '--update: нужна версия' <<<"$out"
  check "--update $bad: a malformed version is refused" $? "rc=$rc
$out"
done
new_env update-down
printf 'OIDC_CLIENT_SECRET=s
ADMIN_EMAILS=a@g.nsu.ru
GHCR_OWNER=nsu
MAIN_ADVERTISE_IP=10.20.0.5
' >"$E/answers.env"
touch "$FAKE_STATE/container"
export FAKE_RUNNING=false
out="$(inst --update 0.2.0 --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" != 0 ] && grep -q 'не запущен' <<<"$out" && ! log_has "images pull"
check "--update with a stopped main: error before seeding" $? "rc=$rc
$out"
unset FAKE_RUNNING FAKE_GHCR FAKE_SEED_VER

# ------------------------------------------------------------------ 9. the restart after --update uses the pulled image whatever the first source was
# (the checkout of these scripts is version 0.1.0, so --update 0.1.0 stands for "update, then check out that version")
for src in build load pull; do
  new_env "upd-$src"
  export FAKE_GHCR=401 FAKE_SEED_VER=0.1.0
  echo tar >"$E/work/img.tar"
  printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=%s\nGHCR_OWNER=nsu\nIMAGE_TAR=%s\n' "$src" "$E/work/img.tar" >"$E/answers.env"
  out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
  [ "$rc" = 0 ]; check "$src install: the first install works" $? "rc=$rc $out"
  : >"$FAKE_LOG"
  out="$(inst --update 0.1.0 --yes </dev/null)"; rc=$?
  grep -q "^IMAGE_SOURCE=$src\$" "$CONF" && grep -q '^PENDING_IMAGE_VERSION=0.1.0$' "$CONF" && grep -q 'PENDING_IMAGE_VERSION' <<<"$out" && ! log_has "docker rm" && ! log_has "docker run"
  check "$src install: --update 0.1.0 records the pending version, keeps IMAGE_SOURCE=$src, does not touch the container" $? "rc=$rc
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
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=keep\nGHCR_OWNER=nsu\n' >"$E/answers.env"
FAKE_IMAGE=1 inst --yes --answers "$E/answers.env" </dev/null >/dev/null
updout="$(FAKE_SEED_VER=0.2.0 inst --update 0.2.0 --yes </dev/null)"
: >"$FAKE_LOG"
out="$(FAKE_IMAGE=1 inst --yes </dev/null)"; rc=$?
grep -q 'git checkout v0.2.0' <<<"$out" && log_has "docker run -d" && grep -q '^PENDING_IMAGE_VERSION=0.2.0$' "$CONF"
check "a restart from a checkout of another version warns to check out the pulled one and keeps the pending mark" $? "rc=$rc $updout $out"

# a version that is already in the registry is not downloaded again; a digest that differs from release/<version>.json is
new_env seed-skip
printf 'OIDC_CLIENT_SECRET=s\nADMIN_EMAILS=a@g.nsu.ru\nIMAGE_SOURCE=pull\nGHCR_OWNER=nsu\n' >"$E/answers.env"
export FAKE_GHCR=401 FAKE_REGISTRY_TAGS="0.1.0-cpu 0.1.0-gpu" FAKE_REGISTRY_DIGEST=sha256:aaaa
out="$(inst --yes --answers "$E/answers.env" </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "images pull" && ! log_has "images push" && grep -q 'в реестре уже есть nsu-llm-node:0.1.0-cpu (sha256:aaaa)' <<<"$out" && log_has "docker pull ghcr.io/nsu/nsu-llm-main:0.1.0"
check "pull: both node images already in the registry are skipped (the main image is still pulled); no ctr pull or push" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
mkdir -p "$E/rel"
printf '{"images": {\n    "main": {"ref": "x", "digest": "sha256:cccc", "size_bytes": 1},\n    "node-cpu": {"ref": "x", "digest": "sha256:aaaa", "size_bytes": 1},\n    "node-gpu": {"ref": "x", "digest": "sha256:bbbb", "size_bytes": 1}\n  }\n}\n' >"$E/rel/0.1.0.json"
: >"$FAKE_LOG"
out="$(INST_RELEASE_DIR="$E/rel" inst --yes </dev/null)"; rc=$?
[ "$rc" = 0 ] && ! log_has "images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.1.0-cpu" && log_has "images pull --platform linux/amd64 ghcr.io/nsu/nsu-llm-node:0.1.0-gpu" \
  && grep -q 'расходится с release/0.1.0.json' <<<"$out"
check "a registry digest that equals release/<version>.json is skipped, one that differs is downloaded again with a warning" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
unset FAKE_REGISTRY_TAGS FAKE_REGISTRY_DIGEST FAKE_GHCR FAKE_SEED_VER

bash -n "$INSTALL"
check "install-main.sh parses (bash -n)" $?

echo
[ "$FAILED" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
