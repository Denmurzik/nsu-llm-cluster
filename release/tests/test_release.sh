#!/usr/bin/env bash
# Offline tests for release/release.sh: docker and nice are stubs that log their calls; git is real (a throw-away repo).
# The stub docker never builds or pushes anything, so no registry, no daemon and no token are involved.
# Scenarios that need POSIX file modes (token file 0600) run on Linux only.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="$(cd "$HERE/.." && pwd)"
BASH_BIN="$(command -v bash)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0
LINUX=0; [ "$(uname -s)" = Linux ] && LINUX=1

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi; }

TOKEN="fake-release-token-0123456789abcdefghijklmnop"
DIGEST_HEX="$(printf 'a%.0s' $(seq 1 64))"

# ------------------------------------------------------------------ stubs
BIN="$TMP/bin"; mkdir -p "$BIN"
cat >"$BIN/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >>"$FAKE_LOG"
echo "config=${DOCKER_CONFIG:-unset}" >>"$FAKE_LOG.env"
case "$1" in
  login)
    cat >"$FAKE_STATE/login-stdin"
    [ "${FAKE_LOGIN_FAIL:-0}" = 1 ] && { echo "unauthorized" >&2; exit 1; }
    mkdir -p "$DOCKER_CONFIG" && echo '{"auths":{"ghcr.io":{}}}' >"$DOCKER_CONFIG/config.json"; exit 0 ;;
  logout) echo "$DOCKER_CONFIG" >>"$FAKE_STATE/logout-config"; exit 0 ;;
  push)
    [ "${FAKE_PUSH_FAIL:-0}" = 1 ] && { echo "denied: permission_denied" >&2; exit 1; }
    echo "The push refers to repository [${2%%:*}]"
    echo "${2#*:}: digest: sha256:$FAKE_DIGEST size: 1234"; exit 0 ;;
  image) [ "$2" = inspect ] || exit 0
         if [ "${3:-}" = "-f" ]; then echo 123456789; exit 0; fi
         [ "${FAKE_NO_IMAGES:-0}" = 1 ] && exit 1; exit 0 ;;
  *) exit 0 ;;
esac
EOS
printf '#!/usr/bin/env bash\necho "nice $*" >>"$FAKE_LOG"\n[ "$1" = -n ] && shift 2\nexec "$@"\n' >"$BIN/nice"
chmod +x "$BIN"/*

# new_repo NAME: a clean git repo with release.sh and just enough files for the version checks
new_repo() {
  R="$TMP/$1"; RW="$R"; command -v cygpath >/dev/null 2>&1 && RW="$(cygpath -m "$R")"; W="$R.w"; mkdir -p "$W/state" "$W/home" "$R/release" "$R/main" "$R/node"
  cp "$RELEASE_DIR/release.sh" "$R/release/release.sh"
  printf '#!/usr/bin/env bash\nIMAGE="nsu-llm-main:0.2.0"\n' >"$R/main/install-main.sh"
  printf '0.1.0\n' >"$R/node/IMAGE_VERSION"
  printf 'ARG K3S_VERSION=v1.36.5+k3s1\n' >"$R/main/Dockerfile"
  printf 'ARG LLAMA_TAG=b12033\nARG K3S_VERSION=v1.36.5+k3s1\n' >"$R/node/Dockerfile"
  ( cd "$R" && git init -q && git config user.email t@t && git config user.name t && git config core.autocrlf false \
    && git add -A && git commit -q -m init && git update-index -q --refresh ) >/dev/null 2>&1
  export FAKE_LOG="$W/docker.log" FAKE_STATE="$W/state" FAKE_DIGEST="$DIGEST_HEX"
  : >"$FAKE_LOG"; : >"$FAKE_LOG.env"
  TOKEN_FILE="$W/token"; printf '%s' "$TOKEN" >"$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
}
# rel [ARGS...]: run the copy of release.sh in the scenario repo
rel() {
  ( cd "$R" && env -u GHCR_OWNER PATH="$BIN:$PATH" HOME="$W/home" GHCR_OWNER="${OWNER-nsu}" GHCR_TOKEN_FILE="$TOKEN_FILE" \
      FAKE_LOG="$FAKE_LOG" FAKE_STATE="$FAKE_STATE" FAKE_DIGEST="$FAKE_DIGEST" \
      FAKE_LOGIN_FAIL="${FAKE_LOGIN_FAIL:-0}" FAKE_PUSH_FAIL="${FAKE_PUSH_FAIL:-0}" FAKE_NO_IMAGES="${FAKE_NO_IMAGES:-0}" \
      "$BASH_BIN" release/release.sh "$@" 2>&1 )
}
log_has() { grep -qF -- "$1" "$FAKE_LOG"; }

# ------------------------------------------------------------------ syntax
bash -n "$RELEASE_DIR/release.sh"; check "bash -n release.sh" $?

# ------------------------------------------------------------------ 1. --dry-run prints every step and changes nothing
new_repo dry
out="$(rel --dry-run 0.1.0)"; rc=$?
[ "$rc" = 0 ]; check "dry-run exits 0" $? "rc=$rc
$out"
[ ! -s "$FAKE_LOG" ]; check "dry-run calls docker never (no build, no login, no push, no rmi)" $? "$(cat "$FAKE_LOG")"
grep -q 'nice -n 15 docker build --provenance=false -f .*main/Dockerfile -t ghcr.io/nsu/nsu-llm-main:0.2.0' <<<"$out" \
  && grep -q 'nice -n 15 docker build --provenance=false -f .*node/Dockerfile --target cpu --build-arg JOBS=8 -t ghcr.io/nsu/nsu-llm-node:0.2.0-cpu' <<<"$out" \
  && grep -q 'nice -n 15 docker build --provenance=false -f .*node/Dockerfile --target gpu --build-arg JOBS=8 -t ghcr.io/nsu/nsu-llm-node:0.2.0-gpu' <<<"$out"
check "dry-run prints the three builds: nice -n 15, JOBS=8, own ghcr.io tags" $? "$out"
grep -q 'docker login ghcr.io -u nsu --password-stdin' <<<"$out" && grep -q 'docker logout ghcr.io' <<<"$out" \
  && grep -q 'docker push ghcr.io/nsu/nsu-llm-main:0.2.0$' <<<"$out" && grep -q 'docker push ghcr.io/nsu/nsu-llm-node:0.2.0-cpu$' <<<"$out" \
  && grep -q 'docker push ghcr.io/nsu/nsu-llm-node:0.2.0-gpu$' <<<"$out" && grep -q 'git .* tag v0.1.0' <<<"$out" \
  && grep -q 'release/0.1.0.json\|0.1.0.json' <<<"$out"
check "dry-run prints login (stdin), pushes, json, git tag and logout" $? "$out"
! grep -q ':main\|:latest' <<<"$out" && ! grep -q "$TOKEN" <<<"$out"
check "dry-run without --latest has no moving tags and never shows the token" $? "$out"
[ ! -e "$R/release/0.1.0.json" ] && [ -z "$(git -C "$RW" tag)" ]
check "dry-run writes no json and creates no tag" $? "$(ls "$R/release")"
out="$(rel --dry-run --latest 0.1.0)"
grep -q 'docker tag ghcr.io/nsu/nsu-llm-main:0.2.0 ghcr.io/nsu/nsu-llm-main:main' <<<"$out" \
  && grep -q 'docker tag ghcr.io/nsu/nsu-llm-node:0.2.0-cpu ghcr.io/nsu/nsu-llm-node:cpu' <<<"$out" \
  && grep -q 'docker push ghcr.io/nsu/nsu-llm-node:gpu$' <<<"$out"
check "--latest adds the moving tags main, cpu, gpu" $? "$out"
out="$(rel --dry-run --skip-build 0.1.0)"
! grep -q 'docker build' <<<"$out" && grep -q 'docker push ghcr.io/nsu/nsu-llm-main:0.2.0$' <<<"$out" && ! grep -q 'docker rmi' <<<"$out"
check "--skip-build: no build, pushes the existing tags, removes nothing afterwards" $? "$out"

# ------------------------------------------------------------------ 2. refusals
new_repo refuse
out="$(OWNER="" rel --dry-run 0.1.0)"; rc=$?
[ "$rc" != 0 ] && grep -q 'GHCR_OWNER' <<<"$out"
check "no GHCR_OWNER: Russian error naming it" $? "rc=$rc $out"
out="$(OWNER=Nsu rel --dry-run 0.1.0)"; rc=$?
[ "$rc" != 0 ] && grep -q 'строчные' <<<"$out"
check "upper-case GHCR_OWNER is refused (ghcr.io is lower-case only)" $? "rc=$rc $out"
out="$(rel --dry-run)"; rc=$?
[ "$rc" != 0 ] && grep -q 'не указана версия' <<<"$out"
check "no version: error" $? "rc=$rc $out"
out="$(rel --dry-run v0.1)"; rc=$?
[ "$rc" != 0 ] && grep -q 'версия должна быть' <<<"$out"
check "a malformed version is refused" $? "rc=$rc $out"
out="$(rel --dry-run 0.2.0)"; rc=$?
[ "$rc" != 0 ] && grep -q 'не совпадает с версией чекаута' <<<"$out" && [ ! -s "$FAKE_LOG" ]
check "a version that differs from the checkout's scripts is refused before any docker call" $? "rc=$rc $out"
echo dirty >"$R/main/dirty.txt"; git -C "$RW" status --porcelain >/dev/null   # refresh the index (Windows git can miss a file written right after the commit)
out="$(rel --dry-run 0.1.0)"; rc=$?
[ "$rc" != 0 ] && grep -q 'незафиксированные' <<<"$out" && [ ! -s "$FAKE_LOG" ]
check "a dirty tree is refused" $? "rc=$rc $out"
rm "$R/main/dirty.txt"; git -C "$RW" update-index -q --refresh
git -C "$RW" tag v0.1.0
out="$(rel --dry-run 0.1.0)"; rc=$?
[ "$rc" != 0 ] && grep -q 'тег v0.1.0 уже существует' <<<"$out"
check "an existing version tag is refused" $? "rc=$rc $out"

# ------------------------------------------------------------------ 3. token file (needs POSIX modes)
if [ "$LINUX" = 1 ]; then
  new_repo tok
  rm "$TOKEN_FILE"
  out="$(rel 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'нет файла с токеном' <<<"$out" && [ ! -s "$FAKE_LOG" ]
  check "missing token file: error, no docker call" $? "rc=$rc $out"
  printf '%s' "$TOKEN" >"$TOKEN_FILE"; chmod 644 "$TOKEN_FILE"
  out="$(rel 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'chmod 600' <<<"$out" && [ ! -s "$FAKE_LOG" ] && ! grep -q "$TOKEN" <<<"$out"
  check "token file readable by others (0644) is refused, the token is not echoed" $? "rc=$rc $out"
  chmod 640 "$TOKEN_FILE"
  out="$(rel 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'chmod 600' <<<"$out"
  check "token file readable by the group (0640) is refused" $? "rc=$rc $out"
  : >"$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
  out="$(rel 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'пуст' <<<"$out"
  check "empty token file is refused" $? "rc=$rc $out"
else
  echo "skip - token file permission scenarios (no POSIX modes on $(uname -s)); run on Linux"
fi

# ------------------------------------------------------------------ 4. a real run against the stub docker
if [ "$LINUX" = 1 ]; then
  new_repo real
  out="$(rel --latest 0.1.0)"; rc=$?
  [ "$rc" = 0 ]; check "release run exits 0" $? "rc=$rc
$out
$(cat "$FAKE_LOG")"
  [ "$(cat "$FAKE_STATE/login-stdin")" = "$TOKEN" ]; check "docker login gets the token on stdin" $? "$(cat "$FAKE_STATE/login-stdin")"
  ! grep -q "$TOKEN" "$FAKE_LOG" && ! grep -q "$TOKEN" <<<"$out" && ! grep -qiE 'password=|--password |-p ' "$FAKE_LOG"
  check "the token is on no command line and in no output" $? "$(cat "$FAKE_LOG")"
  grep -q '^docker login ghcr.io -u nsu --password-stdin$' "$FAKE_LOG"; check "login command is docker login ghcr.io -u <owner> --password-stdin" $? "$(cat "$FAKE_LOG")"
  cfg="$(grep -m1 '^config=' "$FAKE_LOG.env" | cut -d= -f2-)"
  [ -n "$cfg" ] && [ "$cfg" != unset ] && [ "$cfg" != "$W/home/.docker" ] && [ ! -d "$cfg" ] && [ -s "$FAKE_STATE/logout-config" ] \
    && [ "$(cat "$FAKE_STATE/logout-config")" = "$cfg" ]
  check "login goes to a private DOCKER_CONFIG, logout runs against it and the directory is gone" $? "cfg=$cfg"
  log_has "docker logout ghcr.io"; check "docker logout ghcr.io runs at exit" $? "$(cat "$FAKE_LOG")"
  b="$(grep -n '^docker build' "$FAKE_LOG" | tail -n1 | cut -d: -f1)"; p="$(grep -n '^docker push' "$FAKE_LOG" | head -n1 | cut -d: -f1)"
  [ "$(grep -c '^docker build --provenance=false' "$FAKE_LOG")" = 3 ] && [ "$(grep -c '^nice -n 15 docker build' "$FAKE_LOG")" = 3 ]     && [ "$(grep -c '^docker build .*--build-arg JOBS=8' "$FAKE_LOG")" = 2 ] && [ "$b" -lt "$p" ]
  check "three builds (nice -n 15, --provenance=false, JOBS=8 for the node images) precede the pushes" $? "$(cat "$FAKE_LOG")"
  [ "$(grep -c '^docker push' "$FAKE_LOG")" = 6 ]; check "six pushes: three versions and three moving tags (--latest)" $? "$(grep push "$FAKE_LOG")"
  J="$R/release/0.1.0.json"
  [ -f "$J" ] && grep -q '"version": "0.1.0"' "$J" && grep -q '"llama_cpp": "b12033"' "$J" && grep -q '"k3s": "v1.36.5+k3s1"' "$J" \
    && grep -q "\"ref\": \"ghcr.io/nsu/nsu-llm-main:0.2.0\", \"digest\": \"sha256:$DIGEST_HEX\", \"size_bytes\": 123456789" "$J" \
    && grep -q "\"node-gpu\": {\"ref\": \"ghcr.io/nsu/nsu-llm-node:0.2.0-gpu\"" "$J" && grep -q "\"git_commit\": \"$(git -C "$RW" rev-parse HEAD)\"" "$J"
  check "release/0.1.0.json has versions, digests, llama.cpp, k3s, sizes and the commit" $? "$(cat "$J" 2>&1)"
  if command -v python3 >/dev/null 2>&1; then python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$J"; check "release json is valid JSON" $? "$(cat "$J")"; fi
  [ "$(git -C "$RW" rev-parse v0.1.0)" = "$(git -C "$RW" rev-parse HEAD)" ] && [ "$(git -C "$RW" tag)" = v0.1.0 ]
  check "local git tag v0.1.0 on the built commit" $? "$(git -C "$RW" tag)"
  ! grep -qE '^docker (rm |system|builder|container|image prune|volume)' "$FAKE_LOG" && ! grep -q 'prune' "$FAKE_LOG"
  check "nothing foreign is touched: no docker rm, prune, system, builder, volume" $? "$(cat "$FAKE_LOG")"
  rmi="$(grep '^docker rmi' "$FAKE_LOG" | sed 's/^docker rmi //' | sort | tr '\n' ' ')"
  [ "$rmi" = "ghcr.io/nsu/nsu-llm-main:0.2.0 ghcr.io/nsu/nsu-llm-main:main ghcr.io/nsu/nsu-llm-node:0.2.0-cpu ghcr.io/nsu/nsu-llm-node:0.2.0-gpu ghcr.io/nsu/nsu-llm-node:cpu ghcr.io/nsu/nsu-llm-node:gpu " ]
  check "only the tags this release created are removed afterwards" $? "$rmi"
  [ -z "$(grep -E '^docker (build|rmi|tag|push)' "$FAKE_LOG" | grep -E ' (-t )?nsu-llm-(main|node)')" ]
  check "the plain nsu-llm-main:0.2.0 / nsu-llm-node tags of other services are never used" $? "$(cat "$FAKE_LOG")"

  # --skip-build pushes existing local tags and removes none
  new_repo skip
  out="$(rel --skip-build 0.1.0)"; rc=$?
  [ "$rc" = 0 ] && ! log_has "docker build" && ! log_has "docker rmi" && [ "$(grep -c '^docker push' "$FAKE_LOG")" = 3 ] && [ -f "$R/release/0.1.0.json" ]
  check "--skip-build: no build, three pushes, no rmi" $? "rc=$rc $out
$(cat "$FAKE_LOG")"
  new_repo skip2
  out="$(FAKE_NO_IMAGES=1 rel --skip-build 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'локального образа' <<<"$out" && ! log_has "docker push"
  check "--skip-build without the local images: error before any push" $? "rc=$rc $out"

  # failures: logout still runs, nothing is tagged
  new_repo loginfail
  out="$(FAKE_LOGIN_FAIL=1 rel 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'docker login ghcr.io не удался' <<<"$out" && ! log_has "docker build" && [ -s "$FAKE_STATE/logout-config" ] && [ -z "$(git -C "$RW" tag)" ]
  check "login failure: Russian error, no build, logout still runs, no tag" $? "rc=$rc $out"
  new_repo pushfail
  out="$(FAKE_PUSH_FAIL=1 rel 0.1.0)"; rc=$?
  [ "$rc" != 0 ] && grep -q 'docker push .* не удался' <<<"$out" && [ ! -e "$R/release/0.1.0.json" ] && [ -z "$(git -C "$RW" tag)" ] \
    && ! log_has "docker rmi" && [ -s "$FAKE_STATE/logout-config" ]
  check "push failure: no json, no tag, the built images stay for --skip-build, logout still runs" $? "rc=$rc $out"
else
  echo "skip - real-run scenarios (token file mode 0600 needs Linux); run on Linux"
fi

# ------------------------------------------------------------------ check-versions.sh: every place that names the image version
# (node/IMAGE_VERSION is the one source; install-main.sh, run-main.*, entrypoint.sh, render_manifests.py, config.py, .env.example follow it)
REPO_ROOT="$(cd "$RELEASE_DIR/.." && pwd)"
out="$(bash "$RELEASE_DIR/check-versions.sh" "$REPO_ROOT" 2>&1)"; rc=$?
check "check-versions: the real checkout names one version everywhere" "$rc" "$out"
CV="$TMP/cv"; mkdir -p "$CV"; ( cd "$REPO_ROOT" && tar -cf - node/IMAGE_VERSION main/install-main.sh main/run-main.sh main/run-main.ps1 main/entrypoint.sh main/render_manifests.py portal/app/config.py .env.example ) | tar -xf - -C "$CV"
out="$(bash "$RELEASE_DIR/check-versions.sh" "$CV" 2>&1)"; rc=$?
check "check-versions: a copy of the files passes" "$rc" "$out"
for f in main/install-main.sh main/run-main.sh main/run-main.ps1 main/entrypoint.sh main/render_manifests.py portal/app/config.py .env.example; do
  cp "$CV/$f" "$CV/$f.orig"; sed -i 's/0\.1\.0/9.9.9/g' "$CV/$f"
  out="$(bash "$RELEASE_DIR/check-versions.sh" "$CV" 2>&1)"; rc=$?
  [ "$rc" != 0 ] && grep -q "^$f: " <<<"$out"
  check "check-versions: $f with another version is named" $? "rc=$rc $out"
  mv "$CV/$f.orig" "$CV/$f"
done
out="$(bash "$RELEASE_DIR/check-versions.sh" "$CV" 9.9.9 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q "версия выпуска 9.9.9" <<<"$out"
check "check-versions: the release version must equal node/IMAGE_VERSION" $? "rc=$rc $out"

echo
if [ "$FAILED" = 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED"; exit 1; fi
