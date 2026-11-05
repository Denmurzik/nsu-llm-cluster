#!/usr/bin/env bash
# Offline tests for node scripts (no docker, no network).
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
BIN="$NODE/rootfs/opt/llm/bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# the monitoring collector (node-metrics.sh, started by entrypoint.sh) would run on in the background of every entrypoint test
# below: it has its own tests (test_node_metrics.sh)
export NODE_METRICS=0
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
contains() { # name haystack needle
  case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing: $3)"; echo "$2" | sed 's/^/    | /' ;; esac
}
lacks() {
  case "$2" in *"$3"*) fail "$1 (unexpected: $3)" ;; *) pass "$1" ;; esac
}
# position of first occurrence (length of text before it), or empty if absent
before() { local s="${1%%"$2"*}"; echo ${#s}; }

# --- syntax ---
for f in "$BIN"/*.sh "$NODE/run-node.sh" "$HERE"/*.sh; do
  if [ -f "$f" ] && bash -n "$f" 2>"$TMP/syn"; then pass "bash -n $(basename "$f")"; else fail "bash -n $f"; cat "$TMP/syn" 2>/dev/null; fi
done
for s in entrypoint.sh serve-llama.sh serve-rpc.sh registrar.sh detect-accel.sh; do
  [ -f "$BIN/$s" ] && pass "exists $s" || fail "exists $s"
done
[ -f "$NODE/run-node.sh" ] && pass "exists run-node.sh" || fail "exists run-node.sh"

# --- serve-llama.sh ---
export LLAMA_CACHE="$TMP/models"
out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf CTX_SIZE=16384 PARALLEL_SLOTS=2 \
  RPC_SERVERS=127.0.0.1:18050,10.0.0.2:18050 ACCEL=cpu PUBLIC_MODEL_NAME=pub \
  bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve hf-repo" "$out" "--hf-repo org/repo"
contains "serve hf-file" "$out" "--hf-file m-Q4_K_M.gguf"
contains "serve ctx" "$out" "--ctx-size 16384"
contains "serve parallel" "$out" "--parallel 2"
contains "serve rpc" "$out" "--rpc 127.0.0.1:18050,10.0.0.2:18050"
contains "serve alias" "$out" "--alias pub"
contains "serve ngl rpc" "$out" "-ngl 99"
contains "serve rpc leader skips the --fit probe" "$out" "--fit off"
lacks "serve rpc without placement has no tensor split" "$out" "--tensor-split"
out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf RPC_SERVERS=127.0.0.1:18050,10.0.0.2:18050 ACCEL=cpu NGL=999 \
  TENSOR_SPLIT=10,17,10,0 bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve rpc tensor split from the portal" "$out" "--tensor-split 10,17,10,0"
contains "serve rpc ngl from the portal" "$out" "-ngl 999"
contains "serve no-mmproj" "$out" "--no-mmproj"
contains "serve no-kv-unified" "$out" "--no-kv-unified"
contains "serve jinja" "$out" "--jinja"
contains "serve cache-ram" "$out" "--cache-ram 512"
contains "serve fa" "$out" "-fa auto"
contains "serve host" "$out" "--host 0.0.0.0"
contains "serve port" "$out" "--port 18080"

out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf CTX_SIZE=4096 PARALLEL_SLOTS=1 ACCEL=cpu \
  bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve ngl cpu" "$out" "-ngl 0"
lacks "serve no rpc flag" "$out" "--rpc"
lacks "serve replica keeps --fit" "$out" "--fit"
contains "serve thinking off by default" "$out" "--reasoning off"
mkdir -p "$TMP/nproc4" "$TMP/nproc1"
printf '#!/usr/bin/env bash\necho 4\n' > "$TMP/nproc4/nproc"; printf '#!/usr/bin/env bash\necho 1\n' > "$TMP/nproc1/nproc"
chmod +x "$TMP/nproc4/nproc" "$TMP/nproc1/nproc"
out="$(PATH="$TMP/nproc4:$PATH" MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve threads default: 4 cores -> -t 3" "$out" "-t 3 "
out="$(PATH="$TMP/nproc1:$PATH" MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve threads default: 1 core -> -t 1" "$out" "-t 1 "

out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf CTX_SIZE=4096 PARALLEL_SLOTS=1 ACCEL=cpu THINKING=on THREADS=7 \
  bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve thinking on" "$out" "--reasoning on"
contains "serve threads override" "$out" "-t 7 "
out="$(MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu THINKING=yes bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "serve rejects THINKING=yes" || fail "serve rejects THINKING=yes"
contains "serve THINKING error names the values" "$out" "on, off or auto"

out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf CTX_SIZE=4096 PARALLEL_SLOTS=1 ACCEL=nvidia \
  bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve ngl nvidia" "$out" "-ngl 99"

out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf CTX_SIZE=4096 PARALLEL_SLOTS=1 ACCEL=nvidia NGL=12 \
  bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve ngl override" "$out" "-ngl 12"

out="$(MODEL_REPO=org/repo MODEL_FILE=big-00001-of-00003.gguf CTX_SIZE=4096 PARALLEL_SLOTS=1 ACCEL=cpu \
  HF_ENDPOINT=https://hf.example bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
for i in 1 2 3; do
  contains "serve shard $i" "$out" "https://hf.example/org/repo/resolve/main/big-0000${i}-of-00003.gguf"
done
contains "serve -m first shard" "$out" "-m $TMP/models/org__repo/big-00001-of-00003.gguf"
lacks "serve shards no hf-repo" "$out" "--hf-repo"

# A pinned commit (MODEL_REVISION, task 2-15): every file comes from resolve/<sha>. llama.cpp b12033 has no revision
# flag (--hf-repo always resolves main through /refs), so a single file is downloaded here too and passed with -m.
SHA=0123456789abcdef0123456789abcdef01234567
out="$(MODEL_REPO=org/repo MODEL_FILE=big-00001-of-00002.gguf MODEL_REVISION=$SHA ACCEL=cpu \
  HF_ENDPOINT=https://hf.example bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve pinned shard 2" "$out" "https://hf.example/org/repo/resolve/$SHA/big-00002-of-00002.gguf"
lacks "serve pinned shards not from main" "$out" "resolve/main"
out="$(MODEL_REPO=org/repo MODEL_FILE=sub/m-Q4_K_M.gguf MODEL_REVISION=$SHA ACCEL=cpu \
  HF_ENDPOINT=https://hf.example bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve pinned single file downloaded" "$out" \
  "download https://hf.example/org/repo/resolve/$SHA/sub/m-Q4_K_M.gguf -> $TMP/models/org__repo/sub/m-Q4_K_M.gguf"
contains "serve pinned single file -m" "$out" "-m $TMP/models/org__repo/sub/m-Q4_K_M.gguf"
lacks "serve pinned single file no hf-repo" "$out" "--hf-repo"
out="$(MODEL_REPO=local/own MODEL_FILE=m-Q4_K_M.gguf MODEL_REVISION=$SHA ACCEL=cpu \
  HF_ENDPOINT=https://hf.example bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve own model ignores a revision" "$out" "--hf-repo local/own --hf-file m-Q4_K_M.gguf"
out="$(MODEL_REPO=org/repo MODEL_FILE=m.gguf MODEL_REVISION='main;x' ACCEL=cpu bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"; rc=$?
[ "$rc" = 2 ] && pass "serve refuses a revision that is no commit" || fail "serve refuses a revision that is no commit (rc $rc)"

# backend api key: reaches llama-server through LLAMA_API_KEY only, never through the command line
out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu BACKEND_API_KEY=hexsecret123 \
  bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
lacks "serve print-cmd has no --api-key flag" "$out" "--api-key"
lacks "serve print-cmd hides the key" "$out" "hexsecret123"
contains "serve print-cmd says the key is set" "$out" "LLAMA_API_KEY is set"
out="$(MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
lacks "serve no key marker without secret" "$out" "LLAMA_API_KEY"
mkdir -p "$TMP/llamabin"
cat > "$TMP/llamabin/llama-server" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$LLAMA_ARGS_OUT"
printf 'LLAMA_API_KEY=%s\nBACKEND_API_KEY=%s\nSSL_CERT_FILE=%s\nCURL_CA_BUNDLE=%s\n' "${LLAMA_API_KEY-unset}" \
  "${BACKEND_API_KEY-unset}" "${SSL_CERT_FILE-unset}" "${CURL_CA_BUNDLE-unset}" > "$LLAMA_ENV_OUT"
EOS
chmod +x "$TMP/llamabin/llama-server"
LLAMA_ARGS_OUT="$TMP/llama-args" LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/llamabin:$PATH" MODEL_REPO=org/repo \
  MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu BACKEND_API_KEY=hexsecret123 bash "$BIN/serve-llama.sh" >/dev/null 2>&1
grep -q "^LLAMA_API_KEY=hexsecret123$" "$TMP/llama-env" \
  && pass "serve exports the key as LLAMA_API_KEY for llama-server" || fail "serve exports the key as LLAMA_API_KEY for llama-server"
grep -q "^BACKEND_API_KEY=unset$" "$TMP/llama-env" \
  && pass "serve does not leak BACKEND_API_KEY to llama-server" || fail "serve does not leak BACKEND_API_KEY to llama-server"
! grep -q "hexsecret123\|--api-key" "$TMP/llama-args" \
  && pass "llama-server argv has no key" || fail "llama-server argv has no key"
LLAMA_ARGS_OUT="$TMP/llama-args" LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/llamabin:$PATH" MODEL_REPO=org/repo \
  MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu bash "$BIN/serve-llama.sh" >/dev/null 2>&1
grep -q "^LLAMA_API_KEY=unset$" "$TMP/llama-env" \
  && pass "serve sets no LLAMA_API_KEY without a secret" || fail "serve sets no LLAMA_API_KEY without a secret"

# TERM during a shard download stops curl too: a leftover curl would keep appending to the .part file
mkdir -p "$TMP/dlbin"
cat > "$TMP/dlbin/curl" <<'EOS'
#!/usr/bin/env bash
case " $* " in *" -sSIL "*) printf 'content-length: 10\r\n'; exit 0 ;; esac
echo $$ > "$FAKE_CURL_PID"
exec sleep 30
EOS
chmod +x "$TMP/dlbin/curl"
FAKE_CURL_PID="$TMP/dl.pid" PATH="$TMP/dlbin:$PATH" MODEL_REPO=org/repo MODEL_FILE=big-00001-of-00002.gguf ACCEL=cpu \
  LLAMA_CACHE="$TMP/dlcache" bash "$BIN/serve-llama.sh" >/dev/null 2>&1 &
sp=$!
for _ in $(seq 50); do [ -s "$TMP/dl.pid" ] && break; sleep 0.1; done
cpid="$(cat "$TMP/dl.pid" 2>/dev/null)"
kill -TERM "$sp" 2>/dev/null
for _ in $(seq 50); do kill -0 "$sp" 2>/dev/null || break; sleep 0.1; done
wait "$sp" 2>/dev/null; src=$?
[ -n "$cpid" ] && ! kill -0 "$cpid" 2>/dev/null && pass "serve TERM during download stops curl" \
  || { fail "serve TERM during download stops curl (curl pid '$cpid' still running)"; kill "$cpid" 2>/dev/null; }
[ "$src" = 143 ] && pass "serve TERM during download exits 143" || fail "serve TERM during download exits 143 (got $src)"

# a shard of the same name and size but a new version (own model file uploaded again, or changed on Hugging Face) is
# downloaded again: the ETag of the download is kept next to the shard; a .part of another version is not resumed
mkdir -p "$TMP/etagbin"
cat > "$TMP/etagbin/curl" <<'EOS'
#!/usr/bin/env bash
case " $* " in *" -sSIL "*) printf 'HTTP/1.1 200 OK\r\ncontent-length: 10\r\netag: "%s"\r\n\r\n' "$FAKE_ETAG"; exit 0 ;; esac
prev=""
for a in "$@"; do
  if [ "$prev" = -o ]; then
    [ -e "$a" ] && echo "resumed $(basename "$a")" >> "$FAKE_CURL_LOG"
    echo "download $(basename "$a")" >> "$FAKE_CURL_LOG"
    printf 'newbytes!!' > "$a"
  fi
  prev="$a"
done
exit 0
EOS
chmod +x "$TMP/etagbin/curl"
ec="$TMP/etagcache/local__own"; mkdir -p "$ec"
etag_run() { : > "$TMP/etag.log"; FAKE_ETAG="$1" FAKE_CURL_LOG="$TMP/etag.log" LLAMA_ARGS_OUT="$TMP/llama-args" \
  LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/etagbin:$TMP/llamabin:$PATH" MODEL_REPO=local/own \
  MODEL_FILE=big-00001-of-00002.gguf ACCEL=cpu LLAMA_CACHE="$TMP/etagcache" bash "$BIN/serve-llama.sh" >/dev/null 2>&1; }
printf 'oldbytes!!' > "$ec/big-00001-of-00002.gguf"; printf 'oldbytes!!' > "$ec/big-00002-of-00002.gguf"
etag_run v1  # shards from before the ETag was kept: same size, nothing known, kept
lacks "serve keeps a shard without a recorded ETag" "$(cat "$TMP/etag.log")" "download big-00001"
printf 'v1\n' > "$ec/big-00001-of-00002.gguf.etag"; printf 'v2\n' > "$ec/big-00002-of-00002.gguf.etag"
printf 'partial' > "$ec/big-00001-of-00002.gguf.part"; printf 'v1\n' > "$ec/big-00001-of-00002.gguf.part.etag"
etag_run v2
contains "serve downloads a shard whose ETag changed" "$(cat "$TMP/etag.log")" "download big-00001-of-00002.gguf.part"
lacks "serve does not resume a .part of another version" "$(cat "$TMP/etag.log")" "resumed big-00001"
lacks "serve keeps a shard with the same ETag" "$(cat "$TMP/etag.log")" "download big-00002"
[ "$(cat "$ec/big-00001-of-00002.gguf")" = 'newbytes!!' ] && [ "$(cat "$ec/big-00001-of-00002.gguf.etag")" = v2 ] \
  && pass "serve records the ETag of the new shard" || fail "serve records the ETag of the new shard"
etag_run v2
lacks "serve keeps a shard downloaded with the current ETag" "$(cat "$TMP/etag.log")" "download"

# SEC-2 L5: a sha256 ETag (LFS oid, the mirror's hash of an own model) is checked after the download; a mismatch is
# downloaded once more, a second mismatch deletes the file and fails the start
good="$(printf 'newbytes!!' | sha256sum | cut -c1-64)"; bad="$(printf 'otherbytes' | sha256sum | cut -c1-64)"
rm -f "$ec"/big-0000[12]-of-00002.gguf*
etag_run "$good"; rc=$?
[ "$rc" = 0 ] && [ "$(cat "$ec/big-00001-of-00002.gguf")" = 'newbytes!!' ] && [ "$(grep -c '^download big-00001' "$TMP/etag.log")" = 1 ] \
  && pass "serve accepts a shard whose sha256 matches the ETag" || fail "serve accepts a shard whose sha256 matches (rc=$rc)"
rm -f "$ec"/big-0000[12]-of-00002.gguf*
rm -f "$TMP/llama-args"; etag_run "$bad"; rc=$?
[ "$rc" != 0 ] && [ ! -e "$ec/big-00001-of-00002.gguf" ] && [ ! -e "$ec/big-00001-of-00002.gguf.part" ] \
  && [ "$(grep -c '^download big-00001' "$TMP/etag.log")" = 2 ] && [ ! -s "$TMP/llama-args" ] \
  && pass "serve downloads a sha256 mismatch once more, then deletes it and does not start llama-server" \
  || fail "serve on a sha256 mismatch (rc=$rc, $(cat "$TMP/etag.log" | tr '\n' ' '))"


# The size and ETag of a shard come from the final answer of the redirect chain, and only when that is a 200: the
# Content-Length of an error page (or of a redirect hop) is no shard size. The ETag is the content hash in
# X-Linked-Etag when an answer has one (Hugging Face puts it on the redirect, the CDN answers with its own ETag; the
# mirror sends both): switching between the mirror and Hugging Face does not count as a changed shard.
mkdir -p "$TMP/metabin"
cat > "$TMP/metabin/curl" <<'EOS'
#!/usr/bin/env bash
case " $* " in *" -sSIL "*) printf '%b' "$FAKE_HEAD"; exit 0 ;; esac
prev=""
for a in "$@"; do
  [ "$prev" = -o ] && { echo "download $(basename "$a")" >> "$FAKE_CURL_LOG"; printf 'newbytes!!' > "$a"; }
  prev="$a"
done
exit 0
EOS
chmod +x "$TMP/metabin/curl"
mc="$TMP/metacache/org__repo"; mkdir -p "$mc"
meta_run() { : > "$TMP/meta.log"; rm -f "$mc"/*.part*  # every run starts from the shard of v1 on disk
  printf 'oldbytes!!' > "$mc/big-00001-of-00001.gguf"; printf 'sha-of-v1\n' > "$mc/big-00001-of-00001.gguf.etag"
  FAKE_HEAD="$1" FAKE_CURL_LOG="$TMP/meta.log" LLAMA_ARGS_OUT="$TMP/llama-args" \
  LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/metabin:$TMP/llamabin:$PATH" MODEL_REPO=org/repo \
  MODEL_FILE=big-00001-of-00001.gguf ACCEL=cpu LLAMA_CACHE="$TMP/metacache" bash "$BIN/serve-llama.sh" >/dev/null 2>&1; }
meta_run 'HTTP/1.1 302 Found\r\ncontent-length: 0\r\nlocation: /x\r\n\r\nHTTP/1.1 500 Internal Server Error\r\ncontent-length: 42\r\n\r\n'
lacks "serve keeps a shard when the final answer is an error" "$(cat "$TMP/meta.log")" "download"
meta_run 'HTTP/1.1 503 Service Unavailable\r\ncontent-length: 7\r\netag: "busy"\r\n\r\n'
lacks "serve takes no size or ETag from an error answer" "$(cat "$TMP/meta.log")" "download"
meta_run 'HTTP/1.1 302 Found\r\ncontent-length: 1234\r\netag: "hop"\r\nlocation: /x\r\n\r\nHTTP/1.1 200 OK\r\n\r\n'
lacks "serve takes no size or ETag from a redirect hop" "$(cat "$TMP/meta.log")" "download"
meta_run 'HTTP/1.1 302 Found\r\nx-linked-etag: "sha-of-v1"\r\netag: "pointer"\r\nlocation: /x\r\n\r\nHTTP/1.1 200 OK\r\ncontent-length: 10\r\netag: "md5-cdn-3"\r\n\r\n'
lacks "serve compares the X-Linked-Etag of Hugging Face, not the CDN's ETag" "$(cat "$TMP/meta.log")" "download"
meta_run 'HTTP/1.1 200 OK\r\ncontent-length: 10\r\netag: "sha-of-v1"\r\nx-linked-etag: "sha-of-v1"\r\n\r\n'
lacks "serve keeps the shard behind the mirror (same hash)" "$(cat "$TMP/meta.log")" "download"
meta_run 'HTTP/1.1 302 Found\r\nx-linked-etag: "sha-of-v2"\r\nlocation: /x\r\n\r\nHTTP/1.1 200 OK\r\ncontent-length: 10\r\netag: "md5-cdn-3"\r\n\r\n'
contains "serve downloads a shard whose content hash changed" "$(cat "$TMP/meta.log")" "download big-00001-of-00001.gguf.part"
[ "$(cat "$mc/big-00001-of-00001.gguf.etag")" = sha-of-v2 ] && pass "serve records the content hash of the new shard" \
  || fail "serve records the content hash of the new shard (got '$(cat "$mc/big-00001-of-00001.gguf.etag")')"

# A broken stream (the mirror over a flaky link, curl exit 18/56) does not end the download: curl itself retries every
# error with -C - (--retry-all-errors), and when it gives up after making progress the script starts it again, so a
# 5 GB shard arrives in pieces. An attempt that adds nothing ends it (a 404 does not loop forever).
mkdir -p "$TMP/flakybin"
cat > "$TMP/flakybin/curl" <<'EOS'
#!/usr/bin/env bash
case " $* " in *" -sSIL "*) printf 'HTTP/1.1 200 OK\r\ncontent-length: 12\r\netag: "v1"\r\n\r\n'; exit 0 ;; esac
echo "$*" >> "$FAKE_CURL_LOG"
prev=""
for a in "$@"; do
  if [ "$prev" = -o ]; then
    have="$(cat "$a" 2>/dev/null || true)"
    [ "${FAKE_DEAD:-}" = 1 ] && exit 22
    want=abcdefghijkl
    printf '%s' "${want:${#have}:4}" >> "$a"   # four more bytes per attempt, then the stream breaks
    [ "$(cat "$a")" = "$want" ] && exit 0
    exit 18
  fi
  prev="$a"
done
exit 0
EOS
chmod +x "$TMP/flakybin/curl"
flaky_run() { rm -rf "$TMP/flakycache"; : > "$TMP/flaky.log"; FAKE_CURL_LOG="$TMP/flaky.log" LLAMA_ARGS_OUT="$TMP/llama-args" \
  LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/flakybin:$TMP/llamabin:$PATH" MODEL_REPO=org/repo MODEL_FILE=m.gguf \
  MODEL_REVISION=0123456789abcdef0123456789abcdef01234567 ACCEL=cpu LLAMA_CACHE="$TMP/flakycache" \
  HF_ENDPOINT=https://main.example/hf/c/n1 "$@" bash "$BIN/serve-llama.sh" >"$TMP/flaky.out" 2>&1; }
flaky_run; rc=$?
[ "$rc" = 0 ] && [ "$(cat "$TMP/flakycache/org__repo/m.gguf" 2>/dev/null)" = abcdefghijkl ] \
  && pass "serve resumes a download whose stream breaks until the file is whole" \
  || { fail "serve resumes a download whose stream breaks until the file is whole (rc=$rc)"; sed 's/^/    | /' "$TMP/flaky.out"; }
[ "$(grep -c . "$TMP/flaky.log")" = 3 ] && pass "serve starts curl again only after progress (3 attempts)" \
  || fail "serve starts curl again only after progress (got $(grep -c . "$TMP/flaky.log") attempts)"
contains "serve lets curl retry every error with resume" "$(head -n1 "$TMP/flaky.log")" "--retry 100 --retry-all-errors --retry-delay 2"
contains "serve resumes with -C -" "$(head -n1 "$TMP/flaky.log")" "-C -"
grep -q -- '--retry-max-time' "$TMP/flaky.log" && pass "serve bounds curl's own retries in time" || fail "serve bounds curl's own retries in time"
rm -f "$TMP/llama-args"
flaky_run env FAKE_DEAD=1; rc=$?
[ "$rc" != 0 ] && [ "$(grep -c . "$TMP/flaky.log")" = 1 ] && pass "serve gives up after an attempt without progress" \
  || fail "serve gives up after an attempt without progress (rc=$rc, $(grep -c . "$TMP/flaky.log") attempts)"
[ -e "$TMP/llama-args" ] && fail "serve does not start llama-server without the model" \
  || pass "serve does not start llama-server without the model"

# Cached shards of repos no deployment uses any more (KEEP_MODEL_REPOS, from the portal's ConfigMap llm-model-repos) are
# deleted when a model starts; own model files, the rpc tensor cache and vLLM's cache are never touched, and without
# the list nothing is deleted
pc="$TMP/prunecache"
prune_setup() {
  rm -rf "$pc"
  for d in org__old org__repo org__kept local__own rpc huggingface "odd dir"; do mkdir -p "$pc/$d"; echo x > "$pc/$d/f"; done
}
prune_run() { LLAMA_ARGS_OUT="$TMP/llama-args" LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/llamabin:$PATH" \
  MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu LLAMA_CACHE="$pc" bash "$BIN/serve-llama.sh" "$@" 2>&1; }
prune_setup
out="$(KEEP_MODEL_REPOS=org/kept,local/other prune_run)"
[ ! -e "$pc/org__old" ] && pass "serve deletes the cache of a repo no deployment uses" \
  || fail "serve deletes the cache of a repo no deployment uses"
contains "serve logs the deleted cache" "$out" "org__old"
for d in org__repo org__kept local__own rpc huggingface "odd dir"; do
  [ -f "$pc/$d/f" ] && pass "serve keeps $d" || fail "serve keeps $d"
done
prune_setup
out="$(KEEP_MODEL_REPOS= prune_run)"
[ -f "$pc/org__old/f" ] && pass "serve deletes nothing without the repo list" || fail "serve deletes nothing without the repo list"
out="$(KEEP_MODEL_REPOS=org/kept prune_run --print-cmd)"
[ -f "$pc/org__old/f" ] && pass "serve --print-cmd deletes nothing" || fail "serve --print-cmd deletes nothing"
# HF token reaches curl as a header file, never through argv
mkdir -p "$TMP/hfbin"
cat > "$TMP/hfbin/curl" <<'EOS'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_CURL_LOG"
prev=""
for a in "$@"; do
  [ "$prev" = -H ] && case "$a" in @*) cat "${a#@}" >> "$FAKE_CURL_LOG.hdr" ;; esac
  [ "$prev" = -o ] && printf '0123456789' > "$a"
  prev="$a"
done
case " $* " in *" -sSIL "*) printf 'content-length: 10
' ;; esac
exit 0
EOS
chmod +x "$TMP/hfbin/curl"
: > "$TMP/hf.log"; : > "$TMP/hf.log.hdr"
FAKE_CURL_LOG="$TMP/hf.log" LLAMA_ARGS_OUT="$TMP/llama-args" LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/hfbin:$TMP/llamabin:$PATH"   MODEL_REPO=org/repo MODEL_FILE=big-00001-of-00002.gguf ACCEL=cpu HF_TOKEN=hfsecret77 LLAMA_CACHE="$TMP/hfcache"   bash "$BIN/serve-llama.sh" >/dev/null 2>&1
contains "serve downloads shards" "$(cat "$TMP/hf.log")" "big-00002-of-00002.gguf"
lacks "serve HF token not on the curl command line" "$(cat "$TMP/hf.log")" "hfsecret77"
contains "serve HF token sent from a header file" "$(cat "$TMP/hf.log.hdr")" "Authorization: Bearer hfsecret77"
hdr="$(grep -o -- '-H @[^ ]*' "$TMP/hf.log" | head -n1)"; hdr="${hdr#-H @}"
[ -n "$hdr" ] && [ ! -e "$hdr" ] && pass "serve removes the HF header file before llama-server starts" \
  || fail "serve removes the HF header file before llama-server starts ('$hdr')"

# CA of main for the HF mirror (ConfigMap llm-ca): llama-server and curl verify https://<main>/hf against the bundle
printf 'pem\n' > "$TMP/bundle.crt"
out="$(LLM_CA_BUNDLE="$TMP/bundle.crt" MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu \
  HF_ENDPOINT=https://main.example/hf/c/n1/ bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve CA bundle exported" "$out" "SSL_CERT_FILE=$TMP/bundle.crt"
contains "serve HF endpoint without the trailing slash" "$out" "HF_ENDPOINT=https://main.example/hf/c/n1"
out="$(LLM_CA_BUNDLE="$TMP/none.crt" MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
lacks "serve no CA bundle without the file" "$out" "SSL_CERT_FILE"
LLM_CA_BUNDLE="$TMP/bundle.crt" LLAMA_ARGS_OUT="$TMP/llama-args" LLAMA_ENV_OUT="$TMP/llama-env" PATH="$TMP/llamabin:$PATH" \
  MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu bash "$BIN/serve-llama.sh" >/dev/null 2>&1
grep -q "^SSL_CERT_FILE=$TMP/bundle.crt$" "$TMP/llama-env" && grep -q "^CURL_CA_BUNDLE=$TMP/bundle.crt$" "$TMP/llama-env" \
  && pass "llama-server gets the CA bundle (SSL_CERT_FILE, CURL_CA_BUNDLE)" || fail "llama-server gets the CA bundle"

# mirror unreachable (standalone node with HF_FALLBACK_ENDPOINT): Hugging Face directly with the node's own token
mkdir -p "$TMP/mirrordown" "$TMP/mirrorup"
printf '#!/usr/bin/env bash\necho "$*" >> "$FAKE_CURL_LOG"\nexit 7\n' > "$TMP/mirrordown/curl"
printf '#!/usr/bin/env bash\necho "$*" >> "$FAKE_CURL_LOG"\nexit 0\n' > "$TMP/mirrorup/curl"
chmod +x "$TMP/mirrordown/curl" "$TMP/mirrorup/curl"
: > "$TMP/mcurl.log"
out="$(FAKE_CURL_LOG="$TMP/mcurl.log" PATH="$TMP/mirrordown:$PATH" MODEL_REPO=org/repo MODEL_FILE=big-00001-of-00002.gguf ACCEL=cpu \
  HF_ENDPOINT=https://main.example/hf/c/n1 HF_TOKEN=hf_mirrorkey HF_FALLBACK_ENDPOINT=https://huggingface.co/ \
  HF_FALLBACK_TOKEN=hf_own bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve checks the mirror" "$(cat "$TMP/mcurl.log")" "https://main.example/hf/c/n1/_mirror/health"
contains "serve falls back when the mirror is down" "$out" "HF_ENDPOINT=https://huggingface.co"
contains "serve fallback shard url" "$out" "https://huggingface.co/org/repo/resolve/main/big-00001-of-00002.gguf"
contains "serve says why" "$out" "mirror https://main.example/hf/c/n1 is unreachable"
out="$(FAKE_CURL_LOG="$TMP/mcurl.log" PATH="$TMP/mirrorup:$PATH" MODEL_REPO=org/repo MODEL_FILE=m.gguf ACCEL=cpu \
  HF_ENDPOINT=https://main.example/hf/c/n1 HF_FALLBACK_ENDPOINT=https://huggingface.co bash "$BIN/serve-llama.sh" --print-cmd 2>&1)"
contains "serve keeps a reachable mirror" "$out" "HF_ENDPOINT=https://main.example/hf/c/n1"
mkdir -p "$TMP/fbllama"
cat > "$TMP/fbllama/llama-server" <<'EOS'
#!/usr/bin/env bash
printf 'HF_ENDPOINT=%s\nHF_TOKEN=%s\n' "${HF_ENDPOINT-unset}" "${HF_TOKEN-unset}" > "$LLAMA_ENV_OUT"
EOS
chmod +x "$TMP/fbllama/llama-server"
FAKE_CURL_LOG="$TMP/mcurl.log" LLAMA_ENV_OUT="$TMP/fb-env" PATH="$TMP/mirrordown:$TMP/fbllama:$PATH" MODEL_REPO=org/repo \
  MODEL_FILE=m.gguf ACCEL=cpu HF_ENDPOINT=https://main.example/hf/c/n1 HF_TOKEN=hf_mirrorkey \
  HF_FALLBACK_ENDPOINT=https://huggingface.co HF_FALLBACK_TOKEN= bash "$BIN/serve-llama.sh" >/dev/null 2>&1
grep -q "^HF_ENDPOINT=https://huggingface.co$" "$TMP/fb-env" && grep -q "^HF_TOKEN=unset$" "$TMP/fb-env" \
  && pass "llama-server falls back without the mirror key" || { fail "llama-server falls back without the mirror key"; cat "$TMP/fb-env"; }

# --- serve-llama.sh: settings of the deployment (task 2-22b) ---
SHA40="$(printf 'a%.0s' {1..40})"
sv() { env MODEL_REPO=org/repo MODEL_FILE=m-Q4_K_M.gguf ACCEL=cpu "$@" bash "$BIN/serve-llama.sh" --print-cmd 2>&1; }
out="$(sv)"
lacks "serve defaults: no KV flags" "$out" "--cache-type"
lacks "serve defaults: no sampling" "$out" "--temp"
lacks "serve defaults: no draft" "$out" "-md "
lacks "serve defaults: no mmproj file" "$out" "--mmproj "
contains "serve defaults: --no-mmproj" "$out" "--no-mmproj"
contains "serve defaults: reasoning off" "$out" "--reasoning off"
out="$(sv THINKING=auto)"
contains "serve reasoning from THINKING" "$out" "--reasoning auto"
out="$(sv KV_CACHE_TYPE=q8_0)"
contains "serve KV q8_0 for K" "$out" "--cache-type-k q8_0"
contains "serve KV q8_0 for V" "$out" "--cache-type-v q8_0"
contains "serve keeps -fa auto with a quantized V cache" "$out" "-fa auto"
out="$(sv KV_CACHE_TYPE=f16)"
lacks "serve KV f16 sets no flag" "$out" "--cache-type"
out="$(sv KV_CACHE_TYPE=q5_1)"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses an unknown KV type" || fail "serve refuses an unknown KV type"
contains "serve names the bad KV type" "$out" "KV_CACHE_TYPE must be"
out="$(sv SAMPLING_ARGS='--temp 0.7 --top-p 0.95 --top-k 40 --min-p 0.05 --repeat-penalty 1.1')"
contains "serve sampling flags" "$out" "--temp 0.7 --top-p 0.95 --top-k 40 --min-p 0.05 --repeat-penalty 1.1"
out="$(sv SAMPLING_ARGS='--seed 5')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a flag that is no sampling flag" || fail "serve refuses a flag that is no sampling flag"
out="$(sv SAMPLING_ARGS='--temp hot')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a sampling value that is no number" || fail "serve refuses a sampling value that is no number"
# draft: downloaded like the main file and given as -md; the draft rides the GPU of a replica only
out="$(sv DRAFT_REPO=org/draft DRAFT_FILE=d-Q8_0.gguf DRAFT_REVISION=$SHA40 HF_ENDPOINT=https://hf.example)"
contains "serve draft download from the commit" "$out" "download https://hf.example/org/draft/resolve/$SHA40/d-Q8_0.gguf -> $LLAMA_CACHE/org__draft/d-Q8_0.gguf"
contains "serve draft file" "$out" "-md $LLAMA_CACHE/org__draft/d-Q8_0.gguf --spec-type draft-simple --spec-draft-n-max 4 --spec-draft-n-min 1"
contains "serve draft stays off the GPU on a cpu node" "$out" "-ngld 0"
out="$(sv ACCEL=nvidia DRAFT_REPO=org/draft DRAFT_FILE=d-Q8_0.gguf)"
contains "serve draft on the GPU of a GPU replica" "$out" "-ngld 99"
out="$(sv ACCEL=nvidia RPC_SERVERS=127.0.0.1:18050,10.0.0.2:18050 NGL=999 DRAFT_REPO=org/draft DRAFT_FILE=d-Q8_0.gguf)"
contains "serve draft stays in the RAM of the split leader" "$out" "-ngld 0"
out="$(sv DRAFT_REPO=local/own DRAFT_FILE=sub/d.gguf DRAFT_REVISION=$SHA40)"
contains "serve own draft file comes from the mirror's main" "$out" "local/own/resolve/main/sub/d.gguf"
out="$(sv DRAFT_REPO=org/draft)"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a draft repo without a file" || fail "serve refuses a draft repo without a file"
out="$(sv DRAFT_REPO=org/draft DRAFT_FILE=d.gguf DRAFT_REVISION=main)"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a draft revision that is no commit" || fail "serve refuses a draft revision that is no commit"
out="$(sv DRAFT_REPO=org/draft DRAFT_FILE=d-00001-of-00002.gguf)"
contains "serve draft shards" "$out" "d-00002-of-00002.gguf"
# mmproj: a file of the model's repo (the pinned commit), or an own model's
out="$(sv MMPROJ_FILE=mmproj-F16.gguf MODEL_REVISION=$SHA40)"
contains "serve mmproj download from the model's commit" "$out" "org/repo/resolve/$SHA40/mmproj-F16.gguf"
contains "serve mmproj flag" "$out" "--mmproj $LLAMA_CACHE/org__repo/mmproj-F16.gguf"
lacks "serve mmproj replaces --no-mmproj" "$out" "--no-mmproj"
# the projector stays on the split leader: b12033 offloads it to a GPU-type device by default, and a leader's only ones are rpc
out="$(sv MMPROJ_FILE=mmproj-F16.gguf RPC_SERVERS=127.0.0.1:18050,10.0.0.2:18050 NGL=999)"
contains "serve split: the projector is not offloaded to an rpc device" "$out" "--no-mmproj-offload"
out="$(sv MMPROJ_FILE=mmproj-F16.gguf)"
lacks "serve replica: the projector may be offloaded" "$out" "--no-mmproj-offload"
out="$(sv RPC_SERVERS=127.0.0.1:18050,10.0.0.2:18050 NGL=999)"
lacks "serve split without a projector: no flag" "$out" "--no-mmproj-offload"
out="$(sv MMPROJ_FILE=local/vision/mmproj.gguf)"
contains "serve own mmproj file" "$out" "--mmproj $LLAMA_CACHE/local__vision/mmproj.gguf"
contains "serve own mmproj from the mirror's main" "$out" "local/vision/resolve/main/mmproj.gguf"
# own flags: last on the command line, after the node checks them with the portal's rules
out="$(sv EXTRA_ARGS=$'--no-warmup\n--seed\n42')"
contains "serve own flags" "$out" "--no-warmup --seed 42"
cmdline="$(printf '%s\n' "$out" | grep '^llama-server ')"
case "$cmdline" in *"--no-warmup --seed 42") pass "serve own flags come last" ;; *) fail "serve own flags come last"; echo "$cmdline" ;; esac
for bad in $'--port\n80' $'--api-key=x' $'--hf-repo\norg/x' $'-ngl\n0' $'--cache-type-k\nq4_0' $'--no-mmproj' $'--reasoning\non' $'--rpc\nx:1'; do
  out="$(sv EXTRA_ARGS="$bad")"; rc=$?
  [ "$rc" -ne 0 ] && pass "serve refuses own flag ${bad%%$'\n'*}" || fail "serve refuses own flag ${bad%%$'\n'*}"
done
# the same normalisation and generic rule as the portal (llama_args.normalize_flag): `_` is `-`, case is ignored
for bad in $'--API_KEY=x' $'--Api-Key
x' $'--n_gpu_layers
1' $'--PORT=1' $'--my-model-file
x' $'--some-path=x' $'--any-dir' $'--x-url' $'--my-key' $'-dev
CPU' $'--seed
..'; do
  out="$(sv EXTRA_ARGS="$bad")"; rc=$?
  [ "$rc" -ne 0 ] && pass "serve refuses own flag ${bad%%$'
'*} (normalised)" || fail "serve refuses own flag ${bad%%$'
'*} (normalised)"
done
out="$(sv EXTRA_ARGS=$'--Seed
5
--no_warmup')"; rc=$?
[ "$rc" -eq 0 ] && pass "serve keeps harmless own flags in any spelling" || fail "serve keeps harmless own flags in any spelling"
out="$(sv EXTRA_ARGS=$'--API_KEY=x')"
contains "serve names the flag as typed" "$out" "the flag --API_KEY is set by the portal"
out="$(sv EXTRA_ARGS=$'--port=80')"
contains "serve names the forbidden flag" "$out" "the flag --port is set by the portal"
out="$(sv EXTRA_ARGS=$'--my-opt\n/etc/passwd')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a path as a flag value" || fail "serve refuses a path as a flag value"
out="$(sv EXTRA_ARGS=$'--my-opt=~/x')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a home path in a flag" || fail "serve refuses a home path in a flag"
out="$(sv EXTRA_ARGS=$'--seed\nC:\\x')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses a backslash path" || fail "serve refuses a backslash path"
out="$(sv EXTRA_ARGS=$'42')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses own flags that start with a value" || fail "serve refuses own flags that start with a value"
many="$(printf -- '--x%s\n' $(seq 1 41))"
out="$(sv EXTRA_ARGS="$many")"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses more than 40 own tokens" || fail "serve refuses more than 40 own tokens"
out="$(sv EXTRA_ARGS=$'--note\nпривет')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses non-ASCII own flags" || fail "serve refuses non-ASCII own flags"
out="$(sv FORBIDDEN_FLAGS_FILE="$TMP/none" EXTRA_ARGS=$'--no-warmup')"; rc=$?
[ "$rc" -ne 0 ] && pass "serve refuses own flags without the forbidden list" || fail "serve refuses own flags without the forbidden list"
contains "serve says the list is missing" "$out" "forbidden flags"
# the image keeps the list the portal checks with
[ -s "$NODE/rootfs/opt/llm/forbidden-flags" ] && pass "forbidden-flags is in the image tree" || fail "forbidden-flags is in the image tree"
# the draft and the projector are downloaded with the same curl path as the model
mkdir -p "$TMP/dpbin" "$TMP/dpllama"
cat > "$TMP/dpbin/curl" <<'EOT'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_CURL_LOG"
prev=""
for a in "$@"; do
  [ "$prev" = -o ] && printf '0123456789' > "$a"
  prev="$a"
done
case " $* " in *" -sSIL "*) printf 'HTTP/1.1 200 OK\ncontent-length: 10\n' ;; esac
exit 0
EOT
cat > "$TMP/dpllama/llama-server" <<'EOT'
#!/usr/bin/env bash
echo "$*" > "$LLAMA_ARGS_OUT"
EOT
chmod +x "$TMP/dpbin/curl" "$TMP/dpllama/llama-server"
: > "$TMP/dp.log"
FAKE_CURL_LOG="$TMP/dp.log" LLAMA_ARGS_OUT="$TMP/dp-args" PATH="$TMP/dpbin:$TMP/dpllama:$PATH" MODEL_REPO=org/repo MODEL_FILE=m.gguf \
  MODEL_REVISION=$SHA40 MMPROJ_FILE=mmproj.gguf DRAFT_REPO=org/draft DRAFT_FILE=d.gguf DRAFT_REVISION=$SHA40 ACCEL=cpu \
  LLAMA_CACHE="$TMP/dpcache" bash "$BIN/serve-llama.sh" >/dev/null 2>&1
grep -q "org/repo/resolve/$SHA40/mmproj.gguf" "$TMP/dp.log" && grep -q "org/draft/resolve/$SHA40/d.gguf" "$TMP/dp.log" \
  && pass "serve downloads the draft and the projector" || { fail "serve downloads the draft and the projector"; cat "$TMP/dp.log"; }
[ -f "$TMP/dpcache/org__draft/d.gguf" ] && [ -f "$TMP/dpcache/org__repo/mmproj.gguf" ] \
  && pass "serve keeps the draft and the projector in the cache" || fail "serve keeps the draft and the projector in the cache"
grep -q -- "-md $TMP/dpcache/org__draft/d.gguf" "$TMP/dp-args" && grep -q -- "--mmproj $TMP/dpcache/org__repo/mmproj.gguf" "$TMP/dp-args" \
  && pass "llama-server is started with the draft and the projector" || { fail "llama-server is started with the draft and the projector"; cat "$TMP/dp-args"; }
# the cache cleanup keeps the repos of this pod's draft and projector
mkdir -p "$TMP/pk/org__draft" "$TMP/pk/org__gone"
: > "$TMP/pk/org__draft/f"; : > "$TMP/pk/org__gone/f"
FAKE_CURL_LOG="$TMP/dp.log" LLAMA_ARGS_OUT="$TMP/dp-args" PATH="$TMP/dpbin:$TMP/dpllama:$PATH" MODEL_REPO=org/repo MODEL_FILE=m.gguf \
  DRAFT_REPO=org/draft DRAFT_FILE=d.gguf KEEP_MODEL_REPOS=org/other ACCEL=cpu LLAMA_CACHE="$TMP/pk" bash "$BIN/serve-llama.sh" >/dev/null 2>&1
[ -f "$TMP/pk/org__draft/f" ] && [ ! -e "$TMP/pk/org__gone/f" ] \
  && pass "serve cache cleanup keeps the draft's repo" || fail "serve cache cleanup keeps the draft's repo"

# --- serve-rpc.sh ---
out="$(RPC_PORT=18050 THREADS=6 bash "$BIN/serve-rpc.sh" --print-cmd 2>&1)"
contains "serve-rpc default command" "$out" "ggml-rpc-server -H 0.0.0.0 -p 18050 -t 6"
lacks "serve-rpc no devices by default" "$out" " -d "
lacks "serve-rpc no cache by default" "$out" " -c"
out="$(RPC_PORT=18050 THREADS=6 RPC_DEVICES=CUDA0,CPU RPC_CACHE=1 LLAMA_CACHE=/var/lib/llm-models bash "$BIN/serve-rpc.sh" --print-cmd 2>&1)"
contains "serve-rpc VRAM and RAM of the node" "$out" "-d CUDA0,CPU"
contains "serve-rpc tensor cache" "$out" "-t 6 -d CUDA0,CPU -c"
contains "serve-rpc cache on the models volume" "$out" "LLAMA_CACHE=/var/lib/llm-models ggml-rpc-server"
grep -qxF 'exec "${args[@]}"' "$BIN/serve-rpc.sh" && pass "serve-rpc execs the server" || fail "serve-rpc execs the server"
grep -q '^rpc-cache-gc.sh --loop &$' "$BIN/serve-rpc.sh" && pass "serve-rpc starts the cache cleanup loop" \
  || fail "serve-rpc starts the cache cleanup loop"

# --- rpc-cache-gc.sh: LRU of the -c tensor cache ---
G="$TMP/rpccache/rpc"
now=$(date +%s)
mkrpc() { # name size mtime-age atime-age
  head -c "$2" /dev/zero > "$G/$1"; touch -m -d "@$((now - $3))" "$G/$1"; touch -a -d "@$((now - $4))" "$G/$1"
}
reset_rpc() {
  rm -rf "$G"; mkdir -p "$G"
  mkrpc 00000000000000aa 1000 4000 4000   # oldest
  mkrpc 00000000000000bb 1000 3000 100    # written long ago, read recently (a cache hit)
  mkrpc 00000000000000cc 1000 2000 2000
}
left() { (cd "$G" && ls | tr '\n' ' '); }
reset_rpc
out="$(RPC_CACHE_DIR="$G" RPC_CACHE_MAX_BYTES=2500 RPC_CACHE_MIN_FREE_PCT=0 bash "$BIN/rpc-cache-gc.sh" 2>&1)"
[ "$(left)" = "00000000000000bb 00000000000000cc " ] && pass "rpc gc removes the least recently used file over the limit" \
  || fail "rpc gc removes the least recently used file over the limit (left: $(left))"
contains "rpc gc logs what it removed" "$out" "removed 1 files (1000 bytes)"
reset_rpc
out="$(RPC_CACHE_DIR="$G" RPC_CACHE_MAX_GIB=0 RPC_CACHE_MIN_FREE_PCT=0 bash "$BIN/rpc-cache-gc.sh" 2>&1)"
[ "$(left)" = "00000000000000aa 00000000000000bb 00000000000000cc " ] && [ -z "$out" ] \
  && pass "rpc gc: limit 0 keeps everything" || fail "rpc gc: limit 0 keeps everything (left: $(left), out: $out)"
RPC_CACHE_DIR="$G" RPC_CACHE_MAX_GIB=64 RPC_CACHE_MIN_FREE_PCT=100 bash "$BIN/rpc-cache-gc.sh" >/dev/null 2>&1
[ -z "$(left)" ] && pass "rpc gc frees the file system down to the minimum free share" \
  || fail "rpc gc frees the file system down to the minimum free share (left: $(left))"
RPC_CACHE_DIR="$TMP/rpccache/missing" bash "$BIN/rpc-cache-gc.sh" && pass "rpc gc without a cache dir is a no-op" \
  || fail "rpc gc without a cache dir is a no-op"
RPC_CACHE_DIR="$G" RPC_CACHE_MAX_GIB=lots bash "$BIN/rpc-cache-gc.sh" >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] && pass "rpc gc rejects a bad limit" || fail "rpc gc rejects a bad limit (rc $rc)"
reset_rpc
RPC_CACHE_DIR="$G" RPC_CACHE_MAX_BYTES=1500 RPC_CACHE_MIN_FREE_PCT=0 RPC_CACHE_GC_INTERVAL_S=1 \
  bash "$BIN/rpc-cache-gc.sh" --loop >/dev/null 2>&1 &
gp=$!
for _ in $(seq 30); do [ "$(left)" = "00000000000000bb " ] && break; sleep 0.1; done
mkrpc 00000000000000dd 1000 -1000 -1000   # in the future: a virus scanner may read bb meanwhile (atime now)
for _ in $(seq 40); do [ "$(left)" = "00000000000000dd " ] && break; sleep 0.1; done
[ "$(left)" = "00000000000000dd " ] && pass "rpc gc --loop keeps cleaning as the cache grows" \
  || fail "rpc gc --loop keeps cleaning as the cache grows (left: $(left))"
kill -TERM "$gp" 2>/dev/null; wait "$gp" 2>/dev/null

# --- models-cleanup.sh: model weights of a node (task 2-26) ---
MC="$TMP/mc/models"
mcnow=$(date +%s)
mcfile() { # path size [age in seconds, default a day]
  mkdir -p "$(dirname "$MC/$1")"; head -c "$2" /dev/zero > "$MC/$1"; touch -m -d "@$((mcnow - ${3:-86400}))" "$MC/$1"
}
mcreset() {
  rm -rf "$TMP/mc"; mkdir -p "$MC"
  mcfile org__keep/model-Q4_K_M.gguf 1000
  mcfile org__keep/model-Q4_K_M.gguf.etag 10
  mcfile org__keep/model-Q8_0.gguf 2000            # an older quant of the same repository
  mcfile org__keep/model-Q8_0.gguf.etag 10
  mcfile org__keep/sub/mmproj-F16.gguf 300         # a projector in a subdirectory
  mcfile org__split/big-00001-of-00003.gguf 100
  mcfile org__split/big-00002-of-00003.gguf 100
  mcfile org__split/big-00003-of-00003.gguf 100
  mcfile org__split/big-00003-of-00003.gguf.part.etag 5
  mcfile org__old/other.gguf 4000                   # a model nobody uses
  mcfile local__own/own.gguf 500
  mcfile rpc/00000000000000aa 700
  mcfile rpc/00000000000000bb 700
  mcfile huggingface/hub/blob 800
  mcfile notes.txt 7                                # not a model: never touched
}
mcrun() { LLAMA_CACHE="$MC" bash "$BIN/models-cleanup.sh" "$@" 2>&1; }
mctree() { (cd "$MC" && find . -type f | sort | tr '\n' ' '); }
mcresult() { echo "$1" | sed -n 's/^RESULT //p' | tail -1; }
[ -f "$BIN/models-cleanup.sh" ] && pass "exists models-cleanup.sh" || fail "exists models-cleanup.sh"
mcreset
out="$(mcrun --keep org/keep:model-Q4_K_M.gguf --keep org/keep:sub/mmproj-F16.gguf --keep org/split:big-00001-of-00003.gguf --keep-rpc)"
tree="$(mctree)"
contains "cleanup keeps the model file" "$tree" "./org__keep/model-Q4_K_M.gguf "
contains "cleanup keeps the etag of the kept file" "$tree" "./org__keep/model-Q4_K_M.gguf.etag "
contains "cleanup keeps a kept file in a subdirectory" "$tree" "./org__keep/sub/mmproj-F16.gguf "
contains "cleanup keeps every shard of a multi-file model" "$tree" "./org__split/big-00003-of-00003.gguf "
contains "cleanup keeps the sidecar of a kept shard" "$tree" "./org__split/big-00003-of-00003.gguf.part.etag "
contains "cleanup keeps the rpc tensors with --keep-rpc" "$tree" "./rpc/00000000000000bb "
contains "cleanup leaves what is not a model" "$tree" "./notes.txt "
lacks "cleanup removes an older quant of a kept repository" "$tree" "model-Q8_0.gguf"
lacks "cleanup removes the sidecar of a removed file" "$tree" "model-Q8_0.gguf.etag"
lacks "cleanup removes a model no deployment uses" "$tree" "org__old"
lacks "cleanup removes an own model that is not kept" "$tree" "local__own"
lacks "cleanup removes vLLM's cache without --keep-vllm" "$tree" "huggingface"
[ -d "$MC/rpc" ] && pass "cleanup leaves the rpc directory itself" || fail "cleanup leaves the rpc directory itself"
r="$(mcresult "$out")"
[ "$(echo "$r" | jq -r .freed_bytes)" = 7310 ] && pass "cleanup reports the freed bytes" || fail "cleanup reports the freed bytes ($r)"
[ "$(echo "$r" | jq -r '.removed | index("org__old") != null')" = true ] && pass "cleanup lists what it removed" || fail "cleanup lists what it removed ($r)"
contains "cleanup logs each removal" "$out" "removed org__old 4000"
mcreset
out="$(mcrun --keep org/keep:model-Q4_K_M.gguf --keep-vllm)"
tree="$(mctree)"
contains "cleanup keeps vLLM's cache with --keep-vllm" "$tree" "./huggingface/hub/blob "
lacks "cleanup removes the rpc tensors without --keep-rpc" "$tree" "./rpc/"
[ -d "$MC/rpc" ] && pass "cleanup leaves the rpc directory when it empties it" || fail "cleanup leaves the rpc directory when it empties it"
mcreset
out="$(mcrun)"
tree="$(mctree)"
[ "$tree" = "./notes.txt " ] && pass "cleanup without arguments removes every model and the rpc cache" || fail "cleanup without arguments removes every model and the rpc cache (left: $tree)"
[ "$(echo "$(mcresult "$out")" | jq -r .models_bytes)" -lt 1000000 ] && pass "cleanup reports what is left" || fail "cleanup reports what is left"
mcreset
before="$(mctree)"
out="$(mcrun --dry-run)"
[ "$(mctree)" = "$before" ] && pass "cleanup --dry-run deletes nothing" || fail "cleanup --dry-run deletes nothing"
[ "$(mcresult "$out" | jq -r '.dry_run')" = true ] && [ "$(mcresult "$out" | jq -r .freed_bytes)" = 10325 ] && pass "cleanup --dry-run reports what would go" || fail "cleanup --dry-run reports what would go ($(mcresult "$out"))"
# something changed a moment ago (a download or a tensor upload under way) stays
mcreset
mcfile org__old/fresh.part 50 5
mcfile rpc/00000000000000cc 700 5
out="$(mcrun)"
tree="$(mctree)"
contains "cleanup skips a directory with a fresh file" "$tree" "./org__old/fresh.part "
contains "cleanup skips a fresh rpc cache" "$tree" "./rpc/00000000000000cc "
contains "cleanup names what it skipped" "$(mcresult "$out")" '"skipped":["org__old"'
out="$(MODELS_CLEANUP_MIN_AGE_S=0 mcrun)"
[ "$(mctree)" = "./notes.txt " ] && pass "cleanup MODELS_CLEANUP_MIN_AGE_S=0 takes fresh files too" || fail "cleanup MODELS_CLEANUP_MIN_AGE_S=0 takes fresh files too ($(mctree))"
# a missing volume is nothing to do; bad arguments and an unsafe path stop it before anything is deleted
out="$(LLAMA_CACHE="$TMP/mc/none" bash "$BIN/models-cleanup.sh" 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ "$(mcresult "$out" | jq -r .freed_bytes)" = 0 ] && pass "cleanup without a volume is a no-op" || fail "cleanup without a volume is a no-op (rc $rc)"
for bad in "--keep" "--keep org/x" "--keep org:x.gguf" "--keep org/x:../etc/passwd" "--keep org/x:/abs.gguf" "--keep org/x:a;b.gguf" "--bogus"; do
  mcreset
  # shellcheck disable=SC2086
  mcrun $bad >/dev/null; rc=$?
  [ $rc -eq 2 ] && [ -d "$MC/org__old" ] && pass "cleanup refuses '$bad'" || fail "cleanup refuses '$bad' (rc $rc)"
done
for bad in / /var relative/path /var/lib/../etc; do
  LLAMA_CACHE="$bad" bash "$BIN/models-cleanup.sh" >/dev/null 2>&1; rc=$?
  [ $rc -eq 2 ] && pass "cleanup refuses LLAMA_CACHE='$bad'" || fail "cleanup refuses LLAMA_CACHE='$bad' (rc $rc)"
done
mcreset
mkdir -p "$TMP/linktarget"; echo precious > "$TMP/linktarget/file"
ln -s "$TMP/linktarget" "$MC/org__link"
mcrun >/dev/null
[ -f "$TMP/linktarget/file" ] && pass "cleanup does not follow a link" || fail "cleanup does not follow a link"
mcreset
[ "$(LLAMA_CACHE="$MC" bash "$BIN/models-cleanup.sh" --usage)" -gt 10000 ] && pass "cleanup --usage prints the size of the volume" || fail "cleanup --usage prints the size of the volume"
out="$(LLAMA_CACHE="$TMP/mc/none" bash "$BIN/models-cleanup.sh" --usage)"
[ "$out" = 0 ] && pass "cleanup --usage of a missing volume is 0" || fail "cleanup --usage of a missing volume is 0 ($out)"

# --- detect-accel.sh ---
mkdir -p "$TMP/fakebin" "$TMP/failbin"
cat > "$TMP/fakebin/nvidia-smi" <<'EOS'
#!/usr/bin/env bash
case "$*" in
  *-L*) echo "GPU 0: Fake RTX (UUID: GPU-x)"; exit 0 ;;
  *--query-gpu=memory.total*) echo "32607"; exit 0 ;;
esac
exit 1
EOS
chmod +x "$TMP/fakebin/nvidia-smi"
out="$(PATH="$TMP/fakebin:$PATH" bash "$BIN/detect-accel.sh")"
[ "$out" = "nvidia" ] && pass "detect nvidia" || fail "detect nvidia (got '$out')"
out="$(PATH="$TMP/fakebin:$PATH" bash "$BIN/detect-accel.sh" --vram)"
[ "$out" = "32607" ] && pass "detect vram" || fail "detect vram (got '$out')"
printf '#!/usr/bin/env bash\nexit 9\n' > "$TMP/failbin/nvidia-smi"
chmod +x "$TMP/failbin/nvidia-smi"
out="$(PATH="$TMP/failbin:$PATH" bash "$BIN/detect-accel.sh")"
[ "$out" = "cpu" ] && pass "detect cpu" || fail "detect cpu (got '$out')"
out="$(PATH="$TMP/failbin:$PATH" bash "$BIN/detect-accel.sh" --vram)"
[ -z "$out" ] && pass "detect vram empty" || fail "detect vram empty (got '$out')"
printf 'MemTotal:       29360128 kB\nMemFree:  100 kB\n' > "$TMP/meminfo"
echo max > "$TMP/memory.max"
out="$(PROC_MEMINFO="$TMP/meminfo" CGROUP_MEMORY_MAX="$TMP/memory.max" bash "$BIN/detect-accel.sh" --ram)"
[ "$out" = "28672" ] && pass "detect ram from MemTotal" || fail "detect ram from MemTotal (got '$out')"
echo 8589934592 > "$TMP/memory.max"
out="$(PROC_MEMINFO="$TMP/meminfo" CGROUP_MEMORY_MAX="$TMP/memory.max" bash "$BIN/detect-accel.sh" --ram)"
[ "$out" = "8192" ] && pass "detect ram capped by the cgroup limit" || fail "detect ram capped by the cgroup limit (got '$out')"
export PROC_MEMINFO="$TMP/meminfo" CGROUP_MEMORY_MAX="$TMP/absent"

# --- entrypoint.sh ---
# the background report of the size of the models volume would run on for ever in these tests; the two tests of it start it
export MODELS_REPORT_S=0
printf '#!/usr/bin/env bash
echo "k3s $*"
echo "K3S_TOKEN=${K3S_TOKEN-unset} CLUSTER_TOKEN=${CLUSTER_TOKEN-unset}"
' > "$TMP/fakebin/k3s"
chmod +x "$TMP/fakebin/k3s"
# entrypoint.sh starts `registrar.sh --report` in the background in k3s mode (task 2-30); there is no portal here, and the real one
# would keep the command substitution of the test open while it retries: a stub (the wake tests below put their own first in PATH)
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/fakebin/registrar.sh"
chmod +x "$TMP/fakebin/registrar.sh"
# The node password must not land in the developer's /etc; the k3s volume is a temp dir too.
export K3S_NODE_ETC="$TMP/etc-rancher-node" K3S_DATA_DIR="$TMP/k3s-data"
# registries.yaml (the mirror of the registry of main) and the state dir with the CA: temp dirs too
export K3S_REGISTRIES_FILE="$TMP/etc-rancher-k3s/registries.yaml"
NODE_STATE="$TMP/node-state"; mkdir -p "$NODE_STATE"
out="$(PATH="$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=Foo NODE_IP=1.2.3.4 ACCEL=auto   bash "$BIN/entrypoint.sh" 2>&1)"
contains "entrypoint k3s agent" "$out" "k3s agent --server https://main.example:6443 --node-name foo"
lacks "entrypoint token not on the k3s command line" "$out" "--token"
contains "entrypoint token reaches k3s as K3S_TOKEN" "$out" "K3S_TOKEN=tok CLUSTER_TOKEN=unset"
contains "entrypoint accel label" "$out" "--node-label llm.nsu/accel=nvidia"
contains "entrypoint vram label" "$out" "--node-label llm.nsu/vram-mib=32607"
contains "entrypoint ram label detected" "$out" "--node-label llm.nsu/ram-mib=28672"
contains "entrypoint external ip" "$out" "--node-external-ip 1.2.3.4"
contains "entrypoint class taint keeps system pods on main" "$out" "--node-taint llm.nsu/class=true:NoSchedule"
# Wake-on-LAN: with a NIC from run-node the registrar reports it in the background, k3s stays PID 1 (exec)
mkdir -p "$TMP/wolbin"
printf '#!/usr/bin/env bash\necho "registrar $* CLUSTER_TOKEN=${CLUSTER_TOKEN-unset} WOL_MAC=${WOL_MAC-unset}" >> "$FAKE_REG_LOG"\n' > "$TMP/wolbin/registrar.sh"
chmod +x "$TMP/wolbin/registrar.sh"
: > "$TMP/reg-bg.log"
out="$(PATH="$TMP/wolbin:$TMP/fakebin:$BIN:$PATH" FAKE_REG_LOG="$TMP/reg-bg.log" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu MODELS_REPORT_S=300 \
  WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=24 bash "$BIN/entrypoint.sh" 2>&1)"
sleep 1
contains "entrypoint k3s reports the NIC in the background" "$(cat "$TMP/reg-bg.log")" "registrar --report CLUSTER_TOKEN=tok WOL_MAC=00-1A-2B-3C-4D-5E"
contains "entrypoint k3s reports the size of the models volume in the background" "$(cat "$TMP/reg-bg.log")" "registrar --models-report CLUSTER_TOKEN=tok"
contains "entrypoint k3s still runs k3s" "$out" "k3s agent --server https://main.example:6443"
# the image the container runs is reported in the background with or without a NIC (task 2-30); the token still not in argv
: > "$TMP/reg-bg.log"
PATH="$TMP/wolbin:$TMP/fakebin:$BIN:$PATH" FAKE_REG_LOG="$TMP/reg-bg.log" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu MODELS_REPORT_S=300 \
  bash "$BIN/entrypoint.sh" >/dev/null 2>&1
sleep 1
[ "$(grep -c . "$TMP/reg-bg.log")" = 2 ] && grep -qx "registrar --report CLUSTER_TOKEN=tok WOL_MAC=unset" "$TMP/reg-bg.log" \
  && grep -qx "registrar --models-report CLUSTER_TOKEN=tok WOL_MAC=unset" "$TMP/reg-bg.log" \
  && pass "entrypoint k3s without a NIC still reports the image and the models volume" \
  || fail "entrypoint k3s without a NIC still reports the image and the models volume: $(cat "$TMP/reg-bg.log")"
out="$(PATH="$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo RAM_MIB=27000 ACCEL=cpu bash "$BIN/entrypoint.sh" 2>&1)"
contains "entrypoint ram label from run-node" "$out" "--node-label llm.nsu/ram-mib=27000"
out="$(PATH="$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo RAM_MIB=junk ACCEL=cpu bash "$BIN/entrypoint.sh" 2>&1)"
lacks "entrypoint ignores a malformed RAM_MIB" "$out" "ram-mib"
out="$(PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu bash "$BIN/entrypoint.sh" 2>&1)"
contains "entrypoint cpu label" "$out" "--node-label llm.nsu/accel=cpu"
lacks "entrypoint cpu no vram" "$out" "vram-mib"
lacks "entrypoint no ip no external-ip" "$out" "--node-external-ip"
pw1="$(cat "$K3S_NODE_ETC/password" 2>/dev/null)"
[ -n "$pw1" ] && pass "entrypoint node password created" || fail "entrypoint node password created"
[ "$(cat "$K3S_DATA_DIR/node-password" 2>/dev/null)" = "$pw1" ] && pass "entrypoint node password kept on k3s volume"   || fail "entrypoint node password kept on k3s volume"
if [ "$(uname -s)" = Linux ]; then # Git Bash on Windows has no real file modes
  [ "$(stat -c %a "$K3S_DATA_DIR/node-password" 2>/dev/null)" = 600 ] && [ "$(stat -c %a "$K3S_NODE_ETC/password" 2>/dev/null)" = 600 ] \
    && pass "entrypoint node password files are 0600" || fail "entrypoint node password files are 0600"
fi
[ "${#pw1}" = 32 ] && pass "entrypoint node password is 32 hex chars" || fail "entrypoint node password is 32 hex chars (got ${#pw1})"
rm -rf "$K3S_NODE_ETC"   # a re-created container starts with an empty /etc/rancher/node
PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu   bash "$BIN/entrypoint.sh" >/dev/null 2>&1
[ "$(cat "$K3S_NODE_ETC/password" 2>/dev/null)" = "$pw1" ] && pass "entrypoint re-created container keeps node password"   || fail "entrypoint re-created container keeps node password"
rm -rf "$K3S_NODE_ETC" "$K3S_DATA_DIR"; mkdir -p "$K3S_NODE_ETC"; printf 'old-pass' > "$K3S_NODE_ETC/password"
PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu   bash "$BIN/entrypoint.sh" >/dev/null 2>&1
[ "$(cat "$K3S_DATA_DIR/node-password" 2>/dev/null)" = "old-pass" ] && pass "entrypoint adopts an existing node password"   || fail "entrypoint adopts an existing node password"
# registries.yaml: pods ask for nsu-llm-node (docker.io/library) or ghcr.io/<owner>/nsu-llm-node; main serves it as nsu-llm-node
rm -f "$K3S_REGISTRIES_FILE" "$NODE_STATE/ca.crt"
STATE_DIR="$NODE_STATE" PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=10.0.0.5 CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu bash "$BIN/entrypoint.sh" >/dev/null 2>&1
reg="$(cat "$K3S_REGISTRIES_FILE" 2>&1)"
contains "registries.yaml mirrors docker.io to main" "$reg" $'  docker.io:
    endpoint:
      - "https://10.0.0.5"'
contains "registries.yaml: the bare nsu-llm-node (docker.io/library) is the repository nsu-llm-node on main" "$reg" '"^library/(nsu-llm-node)$": "$1"'
contains "registries.yaml mirrors ghcr.io/<owner>/nsu-llm-node to main" "$reg" $'  ghcr.io:
    endpoint:
      - "https://10.0.0.5"'
contains "registries.yaml: ghcr.io owner is stripped" "$reg" '"^[^/]+/(nsu-llm-node)$": "$1"'
contains "registries.yaml without a saved CA relies on the system store" "$reg" 'insecure_skip_verify: false'
lacks "registries.yaml: other images are not rewritten" "$reg" 'rancher'
printf -- '-----BEGIN CERTIFICATE-----
x
-----END CERTIFICATE-----
' > "$NODE_STATE/ca.crt"
STATE_DIR="$NODE_STATE" PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=10.0.0.5 CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu bash "$BIN/entrypoint.sh" >/dev/null 2>&1
reg="$(cat "$K3S_REGISTRIES_FILE" 2>&1)"
contains "registries.yaml trusts the CA of main for its registry only" "$reg" $'configs:
  "10.0.0.5":
    tls:
      ca_file: "'"$NODE_STATE"$'/ca.crt"'
if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' 2>/dev/null; then
  python3 - "$K3S_REGISTRIES_FILE" <<'PYEOF2' && pass "registries.yaml is valid YAML with the k3s keys" || fail "registries.yaml is valid YAML with the k3s keys"
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
assert d["mirrors"]["docker.io"]["endpoint"] == ["https://10.0.0.5"]
assert d["mirrors"]["docker.io"]["rewrite"] == {"^library/(nsu-llm-node)$": "$1"}
assert d["mirrors"]["ghcr.io"]["rewrite"] == {"^[^/]+/(nsu-llm-node)$": "$1"}
assert d["configs"]["10.0.0.5"]["tls"]["ca_file"].endswith("/ca.crt")
PYEOF2
fi
STATE_DIR="$NODE_STATE" PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=10.0.0.5 CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu MAIN_INSECURE_TLS=1 bash "$BIN/entrypoint.sh" >/dev/null 2>&1
contains "registries.yaml with --insecure-tls skips verification for the registry of main" "$(cat "$K3S_REGISTRIES_FILE")" 'insecure_skip_verify: true'
rm -f "$K3S_REGISTRIES_FILE"
STATE_DIR="$NODE_STATE" PATH="$TMP/failbin:$TMP/fakebin:$BIN:$PATH" MODE=k3s MAIN_HOST=10.0.0.5 CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu REGISTRY_MIRROR=0 bash "$BIN/entrypoint.sh" >/dev/null 2>&1
[ ! -e "$K3S_REGISTRIES_FILE" ] && pass "REGISTRY_MIRROR=0 writes no registries.yaml" || fail "REGISTRY_MIRROR=0 writes no registries.yaml"
out="$(PATH="$BIN:$PATH" bash "$BIN/entrypoint.sh" echo passthrough 2>&1)"
contains "entrypoint exec args" "$out" "passthrough"
out="$(PATH="$BIN:$PATH" MODE=bogus bash "$BIN/entrypoint.sh" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "entrypoint rejects unknown mode" || fail "entrypoint rejects unknown mode"

# --- run-node.sh ---
RN="$NODE/run-node.sh"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name Test-Node --token T --accel cpu 2>&1)"
contains "run docker save" "$out" "docker save --platform linux/amd64 nsu-llm-node:0.2.0-cpu"
contains "run docker run" "$out" "docker run -d --name llm-node"
contains "run -p 18080" "$out" "-p 18080:18080"
contains "run -p 18050 only on the node address" "$out" "-p 192.168.3.200:18050:18050"
lacks "run 18050 is not published on every address (127.0.0.1 stays closed)" "$out" "-p 18050:18050"
contains "run privileged" "$out" "--privileged"
contains "run hostname" "$out" "--hostname test-node"
contains "run image" "$out" "nsu-llm-node:0.2.0-cpu"
contains "run MODE" "$out" "MODE=k3s"
contains "run restart" "$out" "--restart unless-stopped"
contains "run volume models" "$out" "-v llm-models:/var/lib/llm-models"
contains "run volume state" "$out" "/state:/var/lib/llm-node"
lacks "run cpu no gpus" "$out" "--gpus"
contains "run token passed through the environment" "$out" "-e CLUSTER_TOKEN "
lacks "run token not on the docker command line" "$out" "CLUSTER_TOKEN=T"
[ "$(before "$out" "docker save")" -lt "$(before "$out" "docker run")" ] && pass "save before run" || fail "save before run"
lacks "run dry run asks docker for no memory" "$out" "RAM_MIB"
out="$(RAM_MIB=27000 bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu 2>&1)"
contains "run passes the node memory" "$out" "-e RAM_MIB=27000"
out="$(env -u MODELS_REPORT_S bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu 2>&1)"
lacks "run MODELS_REPORT_S only when set" "$out" "MODELS_REPORT_S"
out="$(MODELS_REPORT_S=0 bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu 2>&1)"
contains "run passes MODELS_REPORT_S through" "$out" "-e MODELS_REPORT_S"

out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel nvidia --network host 2>&1)"
contains "run nvidia image" "$out" "nsu-llm-node:0.2.0-gpu"
contains "run nvidia gpus" "$out" "--gpus all"
contains "run host network" "$out" "--network host"
lacks "run host no -p" "$out" "-p 18080:18080"

out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu --mode standalone 2>&1)"
contains "run standalone MODE" "$out" "MODE=standalone"
lacks "run standalone no save" "$out" "docker save"
contains "run standalone publishes inference" "$out" "-p 18080:18080"
lacks "run standalone does not publish unauthenticated RPC" "$out" "18050"
contains "run standalone: inference port only for main (one transaction)" "$out" "-A NSU-LLM-INFER -s 192.168.3.21 -j RETURN"

# run-node.ps1 follows run-node.sh: 18050 (RPC) only in k3s mode, in the publish list and in the firewall rule
PS="$(command -v powershell || command -v pwsh || true)"
if [ -n "$PS" ]; then
  export NSU_LLM_TEST_NO_SD_OWNER=1   # Docker Desktop: run-node.ps1 secures its data dir; a test run is not elevated
  psnode() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$NODE/run-node.ps1" 2>/dev/null || echo "$NODE/run-node.ps1")" \
    -DryRun -Main 192.168.3.21 -Token T -NodeIp 192.168.3.5 -Name n1 -Accel cpu "$@" 2>&1; }
  out="$(psnode -Mode k3s)"
  contains "ps1 token passed through the environment" "$out" "-e CLUSTER_TOKEN "
  lacks "ps1 token not on the docker command line" "$out" "CLUSTER_TOKEN=T"
  contains "ps1 k3s publishes 18050 only on the node address" "$out" "192.168.3.5:18050:18050"
  lacks "ps1 k3s does not publish 18050 on every address" "$out" " 18050:18050"
  contains "ps1 inference port: only main" "$out" "New-NetFirewallRule -DisplayName NSU-LLM-Node -Direction Inbound -Protocol TCP -LocalPort 18080 -RemoteAddress 192.168.3.21 -Action Allow"
  contains "ps1 RPC port has a rule of its own: main only until the group is known" "$out" "New-NetFirewallRule -DisplayName NSU-LLM-Node-RPC -Direction Inbound -Protocol TCP -LocalPort 18050 -RemoteAddress 192.168.3.21 -Action Allow"
  lacks "ps1 the two ports never share a rule" "$out" "-LocalPort 18080,18050"
  contains "ps1 Hyper-V rule of the RPC port" "$out" "New-NetFirewallHyperVRule -Name NSU-LLM-Node-RPC"
  out="$(psnode -Mode k3s -RpcPeers '192.168.3.7,192.168.3.8')"
  contains "ps1 the nodes of the split group may reach 18050" "$out" "-LocalPort 18050 -RemoteAddress 192.168.3.21,192.168.3.7,192.168.3.8 -Action Allow"
  contains "ps1 the group does not widen 18080" "$out" "-LocalPort 18080 -RemoteAddress 192.168.3.21 -Action Allow"
  out="$(psnode -Mode k3s -RpcPeers '192.168.3.7;calc')"; rc=$?
  [ $rc -ne 0 ] && pass "ps1 RpcPeers with junk fails" || fail "ps1 RpcPeers with junk fails"
  contains "ps1 RpcPeers junk explained" "$out" "Недопустимый -RpcPeers"
  out="$(psnode -Mode standalone)"
  contains "ps1 standalone publishes inference" "$out" "18080:18080"
  lacks "ps1 standalone does not publish RPC" "$out" "18050:18050"
  lacks "ps1 standalone firewall leaves 18050 closed" "$out" "18050"
  contains "ps1 firewall rule is recreated with the current ports" "$out" "Remove-NetFirewallRule -DisplayName NSU-LLM-Node"
  contains "ps1 firewall without -AllowFrom is never open to everyone: main only" "$out" "-LocalPort 18080 -RemoteAddress 192.168.3.21 "
  # -AllowFrom: the rule admits only the class and the main; anything but IPv4/CIDR lists is refused
  out="$(psnode -AllowFrom '10.1.2.0/24, 10.9.0.5')"
  contains "ps1 AllowFrom limits the inference rule" "$out" "-LocalPort 18080 -RemoteAddress 10.1.2.0/24,10.9.0.5 -Action Allow"
  contains "ps1 AllowFrom does not widen the RPC rule: the subnet of the class stays out" "$out" "-LocalPort 18050 -RemoteAddress 192.168.3.21 -Action Allow"
  out="$(psnode -AllowFrom '10.1.2.0/24;calc')"; rc=$?
  [ $rc -ne 0 ] && pass "ps1 AllowFrom with junk fails" || fail "ps1 AllowFrom with junk fails"
  contains "ps1 AllowFrom junk explained" "$out" "Недопустимый -AllowFrom"
  # Wake-on-LAN: the NIC install-node.ps1 found reaches the container (not a secret: plain -e NAME=value)
  out="$(psnode -WolMac 00-1A-2B-3C-4D-5E -WolIp 10.1.2.3 -WolPrefix 24)"
  contains "ps1 wol NIC to the container" "$out" "-e WOL_MAC=00-1A-2B-3C-4D-5E -e WOL_IP=10.1.2.3 -e WOL_PREFIX=24"
  out="$(psnode)"
  lacks "ps1 without a NIC passes none" "$out" "WOL_"
  out="$(psnode -WolMac 'x;calc' -WolIp 10.1.2.3 -WolPrefix 24)"; rc=$?
  [ $rc -ne 0 ] && pass "ps1 wol junk MAC fails" || fail "ps1 wol junk MAC fails"
  contains "ps1 wol junk explained" "$out" "Недопустимый -WolMac"
  out="$(psnode -WolMac 00-1A-2B-3C-4D-5E -WolIp 10.1.2 -WolPrefix 24)"; rc=$?
  [ $rc -ne 0 ] && pass "ps1 wol junk address fails" || fail "ps1 wol junk address fails"

  # -Wsl: docker and shell commands run as root inside the distro
  out="$(psnode -Wsl Ubuntu)"
  contains "ps1 wsl docker as root" "$out" "wsl -d Ubuntu -u root -e docker save"
  contains "ps1 wsl docker run as root" "$out" "wsl -d Ubuntu -u root -e docker run"
  out="$(psnode -Wsl default)"
  contains "ps1 wsl default distro as root" "$out" "wsl -u root -e docker run"

  psun() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$NODE/run-node.ps1" 2>/dev/null || echo "$NODE/run-node.ps1")" \
    -DryRun -Uninstall -Main 192.168.3.21 -Token T -Name n1 "$@" 2>&1; }
  out="$(psun -Wsl Ubuntu -Network bridge)"
  contains "ps1 wsl uninstall DELETE runs inside WSL" "$out" "wsl -d Ubuntu -u root -e bash -c 'set --;"
  contains "ps1 wsl uninstall DELETE reads the token from a header file" "$out" "-H @\$h"
  contains "ps1 wsl uninstall removes the header file" "$out" "trap ''rm -f \$h'' EXIT"
  lacks "ps1 wsl uninstall token not on the command line" "$out" "Bearer"
  contains "ps1 wsl uninstall DELETE verifies the saved CA" "$out" "set -- --cacert ''/var/lib/nsu-llm-node/state/ca.crt''"
  lacks "ps1 wsl uninstall DELETE has no command substitution of curl options" "$out" "echo --cacert"
  lacks "ps1 wsl uninstall DELETE carries no double quotes (PowerShell 5.1)" "$out" '"'

  # The script wsl.exe gets, run by bash with a fake curl: the main address, the CA (only when saved) and the header
  # reach curl as separate words. Live 4070 test: the old $(test ... && echo --cacert ...) form made curl look up «echo».
  mkdir -p "$TMP/wsldel/bin" "$TMP/wsldel/data/state"
  cat > "$TMP/wsldel/bin/curl" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$WSLDEL_ARGS"
prev=""; for a in "$@"; do [ "$prev" = -H ] && cat "${a#@}" > "$WSLDEL_HDR"; prev="$a"; done
printf 204
EOS
  chmod +x "$TMP/wsldel/bin/curl"
  wsldel() { # wsldel [run-node.ps1 args...]: run the DELETE script of the dry run as WSL would, print curl's argv
    local line script
    line="$(psun -Wsl Ubuntu -Network bridge -DataDir "$TMP/wsldel/data" "$@" | grep "^wsl -d Ubuntu -u root -e bash -c '.*api/nodes/n1'")"
    script="$(printf '%s' "$line" | sed "s/^wsl -d Ubuntu -u root -e bash -c '\(.*\)'\$/\1/; s/''/'/g")"
    : > "$TMP/wsldel/args"
    printf '%bAuthorization: Bearer T\r\n' "${WSLDEL_BOM:-}" | WSLDEL_ARGS="$TMP/wsldel/args" WSLDEL_HDR="$TMP/wsldel/hdr" \
      PATH="$TMP/wsldel/bin:$PATH" bash -c "$script" > "$TMP/wsldel/out"
    tr '\n' ' ' < "$TMP/wsldel/args"
  }
  echo CA > "$TMP/wsldel/data/state/ca.crt"
  wout="$(wsldel)"
  contains "ps1 wsl DELETE: the saved CA as two words" "$wout" "--cacert $TMP/wsldel/data/state/ca.crt -H"
  contains "ps1 wsl DELETE: the main address is the URL" "$wout" "https://192.168.3.21/api/nodes/n1"
  lacks "ps1 wsl DELETE: no stray echo" "$wout" "echo"
  [ "$(cat "$TMP/wsldel/hdr")" = "Authorization: Bearer T" ] && pass "ps1 wsl DELETE: the token arrives in the header file" \
    || fail "ps1 wsl DELETE: the token arrives in the header file (got '$(cat "$TMP/wsldel/hdr")')"
  [ "$(cat "$TMP/wsldel/out")" = 204 ] && pass "ps1 wsl DELETE: curl's status reaches PowerShell" || fail "ps1 wsl DELETE: curl's status reaches PowerShell"
  rm -f "$TMP/wsldel/data/state/ca.crt"
  wout="$(wsldel)"
  lacks "ps1 wsl DELETE: no CA saved, no --cacert" "$wout" "--cacert"
  contains "ps1 wsl DELETE: without the CA the header and URL still follow" "$wout" "-m 15 -H @"
  wout="$(wsldel -InsecureTls)"
  contains "ps1 wsl DELETE: -InsecureTls passes -k" "$wout" "-m 15 -k -H @"
  WSLDEL_BOM='\xef\xbb\xbf' wsldel >/dev/null  # Windows PowerShell may write stdin as UTF-8 with a BOM
  [ "$(cat "$TMP/wsldel/hdr")" = "Authorization: Bearer T" ] && pass "ps1 wsl DELETE: a BOM before the header is dropped" \
    || fail "ps1 wsl DELETE: a BOM before the header is dropped (got '$(od -c "$TMP/wsldel/hdr" | head -1)')"
  lacks "ps1 wsl uninstall does not use curl.exe" "$out" "curl.exe"
  contains "ps1 uninstall removes the firewall rule" "$out" "Remove-NetFirewallRule -DisplayName NSU-LLM-Node"
  out="$(psun -Network bridge)"
  contains "ps1 uninstall DELETE reads the token from a header file" "$out" "-H '@<файл заголовков>'"
  lacks "ps1 uninstall token not on the command line" "$out" "Bearer"
  out="$(psun -Network host -Image nsu-llm-node:0.2.0-cpu)"
  contains "ps1 host cleanup runs" "$out" "--entrypoint bash nsu-llm-node:0.2.0-cpu -c"
  lacks "ps1 native arguments carry no double quotes" "$out" '"'
  grep -qF "'--format=csv,noheader,nounits'" "$NODE/run-node.ps1" \
    && pass "ps1 nvidia-smi --format is quoted (PowerShell splits on commas)" || fail "ps1 nvidia-smi --format is quoted"

  # docker failures stop the script: no node.id after a failed save, no «запущена» after a failed run
  mkdir -p "$TMP/psdocker"
  printf '@echo off\r\nif defined FAKE_LOG echo %%*>> "%%FAKE_LOG%%"\r\nif "%%1"=="%%FAKE_FAIL%%" exit /b 3\r\nif "%%1"=="image" echo sha256:fake\r\nif "%%1"=="info" echo 30000000000\r\nexit /b 0\r\n' > "$TMP/psdocker/docker.bat"
  psreal() { PATH="$TMP/psdocker:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$NODE/run-node.ps1")" \
    -Main 192.168.3.21 -Token T -NodeIp 192.168.3.5 -Name n1 -InsecureTls -DataDir "$(cygpath -w "$TMP/${PSDATA:-psdata}")" \
    -Accel "${PSACCEL:-cpu}" 2>&1; }
  out="$(FAKE_FAIL=save psreal)"; rc=$?
  [ $rc -ne 0 ] && pass "ps1 failed docker save fails the script" || fail "ps1 failed docker save fails the script"
  contains "ps1 failed save names the step" "$out" "docker save"
  [ ! -e "$TMP/psdata/airgap/node.id" ] && pass "ps1 failed save writes no node.id" || fail "ps1 failed save writes no node.id"
  : > "$TMP/psdocker-run.log"
  out="$(FAKE_FAIL=run FAKE_LOG="$(cygpath -w "$TMP/psdocker-run.log")" psreal)"; rc=$?
  contains "ps1 passes the memory Docker reports" "$(cat "$TMP/psdocker-run.log")" "-e RAM_MIB=28610"
  [ $rc -ne 0 ] && pass "ps1 failed docker run fails the script" || fail "ps1 failed docker run fails the script"
  lacks "ps1 failed run does not report a running node" "$out" "запущена"
  lacks "ps1 failed run does not print the token" "$out" "CLUSTER_TOKEN=T"
  # GPU probe uses the local node image, never a registry image
  : > "$TMP/psdocker.log"
  out="$(FAKE_FAIL=save FAKE_LOG="$(cygpath -w "$TMP/psdocker.log")" PSDATA=psdata-gpu PSACCEL=auto psreal)"
  log="$(cat "$TMP/psdocker.log")"
  contains "ps1 gpu probe runs the local gpu image" "$log" "run --rm --pull never --gpus all --entrypoint nvidia-smi nsu-llm-node:0.2.0-gpu"
  lacks "ps1 gpu probe pulls nothing from Docker Hub" "$log" "nvidia/cuda"
  contains "ps1 gpu probe success selects the gpu image" "$log" "save --platform linux/amd64 nsu-llm-node:0.2.0-gpu"
fi

unset MODELS_REPORT_S
# --- registrar.sh ---
mkdir -p "$TMP/regbin" "$TMP/regstate" "$TMP/fakeserve"
cat > "$TMP/regbin/curl" <<'EOS'
#!/usr/bin/env bash
# fake curl: /ca.crt answers $FAKE_CA_CODE; the portal API answers 200 and hands out $FAKE_DESIRED
echo "$*" >> "$FAKE_CURL_LOG"
prev=""
for a in "$@"; do [ "$prev" = -H ] && case "$a" in @*) cat "${a#@}" >> "$FAKE_CURL_LOG.hdr" ;; esac; prev="$a"; done
for a in "$@"; do
  case "$a" in
    *://*/ca.crt) if [ "$FAKE_CA_CODE" = 200 ]; then  # the URL, not the --cacert path
                for ((i = 1; i <= $#; i++)); do [ "${!i}" = "-o" ] && { j=$((i + 1)); echo CERT > "${!j}"; }; done
              fi
              printf '%s' "$FAKE_CA_CODE"; exit 0 ;;
    */api/nodes/*/image) printf '{"ok":true}\n%s' "${FAKE_IMAGE_CODE:-${FAKE_API_CODE:-200}}"; exit 0 ;;
    */api/nodes/*) printf '{"ok":true,"heartbeat_interval":%s,"desired":%s,"wake":%s,"purge":%s}\n%s' "${FAKE_INTERVAL:-15}" "${FAKE_DESIRED:-null}" "${FAKE_WAKE:-[]}" "${FAKE_PURGE:-[]}" "${FAKE_API_CODE:-200}"; exit 0 ;;
  esac
done
exit 0
EOS
chmod +x "$TMP/regbin/curl"
# fake serve-llama.sh: records what the registrar's child sees
cat > "$TMP/fakeserve/serve-llama.sh" <<'EOS'
#!/usr/bin/env bash
{
  echo "ARGS=$*"
  echo "LLAMA_API_KEY=${LLAMA_API_KEY-unset}"
  echo "BACKEND_API_KEY=${BACKEND_API_KEY-unset}"
  echo "HF_ENDPOINT=${HF_ENDPOINT-unset}"
  echo "HF_TOKEN=${HF_TOKEN-unset}"
  echo "HF_FALLBACK_ENDPOINT=${HF_FALLBACK_ENDPOINT-unset}"
  echo "HF_FALLBACK_TOKEN=${HF_FALLBACK_TOKEN-unset}"
  echo "LLM_CA_BUNDLE=${LLM_CA_BUNDLE-unset}"
  echo "MODEL_REVISION=${MODEL_REVISION-unset}"
  echo "THINKING=${THINKING-unset}"
  echo "KV_CACHE_TYPE=${KV_CACHE_TYPE-unset}"
  echo "SAMPLING_ARGS=${SAMPLING_ARGS-unset}"
  echo "DRAFT_REPO=${DRAFT_REPO-unset}"
  echo "DRAFT_FILE=${DRAFT_FILE-unset}"
  echo "DRAFT_REVISION=${DRAFT_REVISION-unset}"
  echo "MMPROJ_FILE=${MMPROJ_FILE-unset}"
  echo "EXTRA_ARGS_LINES=$(printf '%s' "${EXTRA_ARGS-unset}" | tr '
' '|')"
  echo "LD_PRELOAD=${LD_PRELOAD-unset}"
  echo "SOMETHING_ELSE=${SOMETHING_ELSE-unset}"
} >> "$FAKE_SERVE_LOG"
exec sleep 30
EOS
chmod +x "$TMP/fakeserve/serve-llama.sh"
# fake models-cleanup.sh: --usage answers $FAKE_USAGE, anything else is logged and ends like the real one
cat > "$TMP/fakeserve/models-cleanup.sh" <<'EOS'
#!/usr/bin/env bash
if [ "${1:-}" = --usage ]; then echo "${FAKE_USAGE:-4242}"; exit 0; fi
echo "ARGS=$*" >> "$FAKE_CLEAN_LOG"
if [ -n "${FAKE_CLEAN_FAIL:-}" ]; then echo "models-cleanup: boom" >&2; exit 2; fi
echo "removed org__old 100"
echo 'RESULT {"freed_bytes":100,"models_bytes":42,"removed":["org__old"],"skipped":[],"dry_run":false}'
EOS
chmod +x "$TMP/fakeserve/models-cleanup.sh"
reg() { # CA code [desired json] -> registrar output after 4 s
  : > "$TMP/curl.log"
  : > "$TMP/curl.log.hdr"
  : > "$TMP/serve.log"
  rm -f "$TMP/regstate/ca.crt"
  FAKE_CA_CODE="$1" FAKE_DESIRED="${2:-null}" FAKE_CURL_LOG="$TMP/curl.log" FAKE_SERVE_LOG="$TMP/serve.log" \
    PATH="$TMP/regbin:$TMP/fakeserve:$BIN:$PATH" CLUSTER_TOKEN=clustersecret42 NODE_IP=10.0.0.9 NODE_NAME=reg-test \
    MAIN_HOST=main.example ACCEL=cpu STATE_DIR="$TMP/regstate" timeout "${REG_TIMEOUT:-4}" bash "$BIN/registrar.sh" 2>&1
}
out="$(reg 404)"
contains "registrar 404 on ca.crt uses the system CA store" "$out" "system CA store"
contains "registrar registers after a 404 on ca.crt" "$out" "registered as reg-test"
lacks "registrar does not retry ca.crt after 404" "$(grep -c '/ca.crt' "$TMP/curl.log")" "2"
case "$(grep '/api/nodes/register' "$TMP/curl.log")" in
  *--cacert*|*" -k"*) fail "registrar 404: register must use the system store" ;;
  *) pass "registrar 404: register uses the system store" ;;
esac
lacks "registrar token not on the curl command line" "$(cat "$TMP/curl.log")" "clustersecret42"
contains "registrar token sent from a header file" "$(cat "$TMP/curl.log.hdr")" "Authorization: Bearer clustersecret42"
# a clean stop (TERM) only marks the node stopped: the portal keeps its NIC for Wake-on-LAN; DELETE is -Uninstall's
contains "registrar stop calls /stop" "$(grep -- '/stop' "$TMP/curl.log")" "https://main.example/api/nodes/reg-test/stop"
lacks "registrar stop does not deregister" "$(cat "$TMP/curl.log")" "-X DELETE"
out="$(reg 200)"
contains "registrar saves the served CA" "$out" "CA saved"
case "$(grep '/api/nodes/register' "$TMP/curl.log")" in
  *"--cacert $TMP/regstate/ca.crt"*) pass "registrar 200: register verifies against the saved CA" ;;
  *) fail "registrar 200: register verifies against the saved CA" ;;
esac
out="$(reg 502)"
contains "registrar retries ca.crt on other errors" "$out" "cannot fetch"
[ ! -s "$TMP/regstate/ca.crt" ] && pass "registrar 502 saves no CA" || fail "registrar 502 saves no CA"
DESIRED='{"deployment":"d","model_repo":"o/r","model_file":"m.gguf","public_model_name":"d","ctx_size":2048,"parallel":2,"spec_hash":"h1","backend_api_key":"hexsecret123"}'
out="$(reg 404 "$DESIRED")"
grep -q "^LLAMA_API_KEY=hexsecret123$" "$TMP/serve.log" \
  && pass "registrar hands the backend key to the server as LLAMA_API_KEY" || { fail "registrar hands the backend key to the server as LLAMA_API_KEY"; cat "$TMP/serve.log"; }
grep -q "^ARGS=" "$TMP/serve.log" && ! grep -q "hexsecret123" <(grep "^ARGS=" "$TMP/serve.log") \
  && pass "registrar passes no key on the server command line" || fail "registrar passes no key on the server command line"
lacks "registrar does not log the key" "$out" "hexsecret123"
DESIRED_NOKEY='{"deployment":"d","model_repo":"o/r","model_file":"m.gguf","public_model_name":"d","ctx_size":2048,"parallel":2,"spec_hash":"h1","backend_api_key":""}'
out="$(reg 404 "$DESIRED_NOKEY")"
grep -q "^LLAMA_API_KEY=unset$" "$TMP/serve.log" \
  && pass "registrar sets no LLAMA_API_KEY for an empty key" || fail "registrar sets no LLAMA_API_KEY for an empty key"
# without a mirror in desired the node's own HF settings are used, as before
grep -q "^HF_ENDPOINT=https://huggingface.co$" "$TMP/serve.log" && grep -q "^HF_FALLBACK_ENDPOINT=unset$" "$TMP/serve.log" \
  && pass "registrar without a mirror: Hugging Face directly" || { fail "registrar without a mirror: Hugging Face directly"; cat "$TMP/serve.log"; }
# the HF mirror of main: its address and key from desired, the node's own endpoint and token as the fallback,
# main's CA (saved by the registrar) plus the system CAs as the bundle
DESIRED_MIRROR='{"deployment":"d","model_repo":"o/r","model_file":"m.gguf","public_model_name":"d","ctx_size":2048,"parallel":2,"spec_hash":"h1","backend_api_key":"","hf_endpoint":"https://main.example/hf/c/reg-test","hf_token":"hf_mirrorkey"}'
out="$(HF_TOKEN=hf_owntoken reg 200 "$DESIRED_MIRROR")"
s="$(cat "$TMP/serve.log")"
contains "registrar mirror endpoint" "$s" "HF_ENDPOINT=https://main.example/hf/c/reg-test"
contains "registrar mirror key" "$s" "HF_TOKEN=hf_mirrorkey"
contains "registrar fallback endpoint" "$s" "HF_FALLBACK_ENDPOINT=https://huggingface.co"
contains "registrar fallback token" "$s" "HF_FALLBACK_TOKEN=hf_owntoken"
contains "registrar CA bundle" "$s" "LLM_CA_BUNDLE=$TMP/regstate/bundle.crt"
grep -q CERT "$TMP/regstate/bundle.crt" 2>/dev/null && pass "registrar bundle holds main's CA" || fail "registrar bundle holds main's CA"
lacks "registrar does not log the mirror key" "$out" "hf_mirrorkey"
grep -q "^MODEL_REVISION=$" "$TMP/serve.log"   && pass "registrar without a pin: no revision (main)" || fail "registrar without a pin: no revision (main)"
DESIRED_PIN='{"deployment":"d","model_repo":"o/r","model_file":"m.gguf","public_model_name":"d","ctx_size":2048,"parallel":2,"spec_hash":"h1","model_revision":"0123456789abcdef0123456789abcdef01234567","backend_api_key":""}'
out="$(reg 404 "$DESIRED_PIN")"
grep -q "^MODEL_REVISION=0123456789abcdef0123456789abcdef01234567$" "$TMP/serve.log"   && pass "registrar hands the pinned commit to the server" || { fail "registrar hands the pinned commit to the server"; cat "$TMP/serve.log"; }

# the llama.cpp settings (task 2-22b): desired carries llm_env, the registrar hands exactly the known names to serve-llama.sh
DESIRED_LLM='{"deployment":"d","model_repo":"o/r","model_file":"m.gguf","public_model_name":"d","ctx_size":2048,"parallel":2,"spec_hash":"h1","backend_api_key":"","llm_env":{"THINKING":"auto","KV_CACHE_TYPE":"q4_0","SAMPLING_ARGS":"--temp 0.5 --top-k 30","DRAFT_REPO":"org/d","DRAFT_FILE":"d.gguf","DRAFT_REVISION":"0123456789abcdef0123456789abcdef01234567","MMPROJ_FILE":"mmproj-F16.gguf","EXTRA_ARGS":"--no-warmup\n--seed\n3","LD_PRELOAD":"/tmp/evil.so","SOMETHING_ELSE":"x"}}'
out="$(REG_TIMEOUT=10 reg 404 "$DESIRED_LLM")"
s="$(cat "$TMP/serve.log")"
s="${s//$'\r'/}"  # a jq of Windows ends its lines with CR; irrelevant in the node image
contains "registrar passes the reasoning" "$s" "THINKING=auto"
contains "registrar passes the KV cache type" "$s" "KV_CACHE_TYPE=q4_0"
contains "registrar passes the sampling" "$s" "SAMPLING_ARGS=--temp 0.5 --top-k 30"
contains "registrar passes the draft model" "$s" "DRAFT_REPO=org/d"
contains "registrar passes the draft file" "$s" "DRAFT_FILE=d.gguf"
contains "registrar passes the draft commit" "$s" "DRAFT_REVISION=0123456789abcdef0123456789abcdef01234567"
contains "registrar passes the projector" "$s" "MMPROJ_FILE=mmproj-F16.gguf"
contains "registrar passes the own flags one per line" "$s" "EXTRA_ARGS_LINES=--no-warmup|--seed|3"
contains "registrar ignores a variable that is not a setting" "$s" "SOMETHING_ELSE=unset"
contains "registrar does not let desired set LD_PRELOAD" "$s" "LD_PRELOAD=unset"
contains "registrar says it ignored them" "$out" "ignoring LD_PRELOAD from desired"
out="$(reg 404 "$DESIRED_NOKEY")"
s="$(cat "$TMP/serve.log")"
contains "registrar without llm_env sets no settings" "$s" "KV_CACHE_TYPE=unset"
contains "registrar without llm_env sets no flags" "$s" "EXTRA_ARGS_LINES=unset"

# Wake-on-LAN (task 2-18): the NIC run-node.ps1 passes (WOL_MAC, WOL_IP, WOL_PREFIX) goes into register and heartbeat
out="$(WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=24 reg 404)"
contains "registrar register carries the NIC" "$(grep -- '/api/nodes/register' "$TMP/curl.log")" '"wake":{"mac":"00-1A-2B-3C-4D-5E","ip":"10.1.2.3","prefix":24}'
contains "registrar heartbeat carries the NIC" "$(grep -- '/api/nodes/heartbeat' "$TMP/curl.log" | head -1)" '"wake":{"mac":"00-1A-2B-3C-4D-5E","ip":"10.1.2.3","prefix":24}'
out="$(reg 404)"
lacks "registrar without a NIC sends no wake" "$(cat "$TMP/curl.log")" '"wake"'
out="$(WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=junk reg 404)"
lacks "registrar ignores a malformed prefix" "$(cat "$TMP/curl.log")" '"wake"'
# k3s mode: --wake-report posts the NIC once (no NODE_IP needed, no register), the token from a header file
wr() {
  : > "$TMP/curl.log"; : > "$TMP/curl.log.hdr"; rm -f "$TMP/regstate/ca.crt"
  FAKE_CA_CODE=404 FAKE_API_CODE="${1:-200}" FAKE_CURL_LOG="$TMP/curl.log" PATH="$TMP/regbin:$BIN:$PATH" CLUSTER_TOKEN=clustersecret42 \
    NODE_NAME=PC01 MAIN_HOST=main.example STATE_DIR="$TMP/regstate" WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=24 \
    WAKE_RETRY_S=1 timeout 5 bash "$BIN/registrar.sh" --wake-report 2>&1
}
out="$(wr)"; rc=$?
[ $rc -eq 0 ] && pass "wake report exits once the portal took it" || { fail "wake report exits once the portal took it (rc=$rc)"; echo "$out"; }
log="$(cat "$TMP/curl.log")"
contains "wake report posts to /api/nodes/wake" "$log" "https://main.example/api/nodes/wake"
contains "wake report payload" "$log" '{"name":"pc01","mac":"00-1A-2B-3C-4D-5E","ip":"10.1.2.3","prefix":24}'
lacks "wake report does not register" "$log" "/api/nodes/register"
lacks "wake report token not on the command line" "$log" "clustersecret42"
contains "wake report token from a header file" "$(cat "$TMP/curl.log.hdr")" "Authorization: Bearer clustersecret42"
contains "wake report says it is done" "$out" "reported"
out="$(wr 422)"; rc=$?
[ $rc -ne 0 ] && pass "wake report refused data: stops" || fail "wake report refused data: stops"
[ "$(grep -c '/api/nodes/wake' "$TMP/curl.log")" = 1 ] && pass "wake report refused data: no retry" || fail "wake report refused data: no retry"
out="$(wr 503)"
[ "$(grep -c '/api/nodes/wake' "$TMP/curl.log")" -ge 2 ] && pass "wake report retries while the portal is down" || fail "wake report retries while the portal is down"
: > "$TMP/curl.log"; rm -f "$TMP/regstate/ca.crt"
out="$(FAKE_CA_CODE=404 FAKE_CURL_LOG="$TMP/curl.log" PATH="$TMP/regbin:$BIN:$PATH" CLUSTER_TOKEN=t MAIN_HOST=main.example STATE_DIR="$TMP/regstate" timeout 5 bash "$BIN/registrar.sh" --wake-report 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass "wake report without a NIC exits quietly" || fail "wake report without a NIC exits quietly (rc=$rc)"
lacks "wake report without a NIC posts no NIC" "$(cat "$TMP/curl.log")" "/api/nodes/wake"
contains "wake report without a NIC still reports the image (the old flag is the same mode)" "$(cat "$TMP/curl.log")" "/image"
# Versions (task 2-30): the image this container runs goes to POST /api/nodes/<name>/image, in both modes
DIGEST_A="sha256:$(printf 'ab%.0s' $(seq 32))"
out="$(NODE_VERSION=0.2.0 NODE_REVISION=abc1234 NODE_IMAGE=nsu-llm-node:0.2.0-gpu NODE_IMAGE_DIGEST="$DIGEST_A" reg 404)"
img="$(grep -- '/api/nodes/reg-test/image' "$TMP/curl.log" | head -n1)"
contains "registrar reports the image after registering" "$img" '"version":"0.2.0","revision":"abc1234","digest":"'"$DIGEST_A"'","tag":"nsu-llm-node:0.2.0-gpu"'
[ "$(grep -n -- '/api/nodes/register' "$TMP/curl.log" | head -n1 | cut -d: -f1)" -lt "$(grep -n -- '/image' "$TMP/curl.log" | head -n1 | cut -d: -f1)" ] \
  && pass "registrar reports the image after the registration, not before" || fail "registrar reports the image after the registration, not before"
contains "registrar registers with the version of the image" "$(grep -- '/api/nodes/register' "$TMP/curl.log")" '"version":"0.2.0"'
out="$(NODE_IMAGE_DIGEST='sha256:junk' reg 404)"
contains "registrar drops a malformed digest instead of sending it" "$(grep -- '/image' "$TMP/curl.log" | head -n1)" '"digest":""'
out="$(reg 404)"
contains "registrar without NODE_VERSION says dev (the image always bakes its own)" "$(grep -- '/api/nodes/register' "$TMP/curl.log")" '"version":"dev"'
out="$(FAKE_IMAGE_CODE=404 reg 404 2>&1)"
contains "registrar goes on when the portal knows no image report" "$out" "registered as reg-test"
contains "registrar says that the report was not taken" "$out" "image report failed (HTTP 404)"
wr() {
  : > "$TMP/curl.log"; : > "$TMP/curl.log.hdr"; rm -f "$TMP/regstate/ca.crt"
  FAKE_CA_CODE=404 FAKE_API_CODE="${1:-200}" FAKE_CURL_LOG="$TMP/curl.log" PATH="$TMP/regbin:$BIN:$PATH" CLUSTER_TOKEN=clustersecret42 \
    NODE_NAME=PC01 MAIN_HOST=main.example STATE_DIR="$TMP/regstate" WAKE_RETRY_S=1 NODE_VERSION=0.2.0 NODE_REVISION=abc1234 \
    NODE_IMAGE=nsu-llm-node:0.2.0-cpu NODE_IMAGE_DIGEST="$DIGEST_A" timeout 5 bash "$BIN/registrar.sh" --report 2>&1
}
out="$(wr)"; rc=$?
[ $rc -eq 0 ] && pass "image report exits once the portal took it" || { fail "image report exits once the portal took it (rc=$rc)"; echo "$out"; }
log="$(cat "$TMP/curl.log")"
contains "image report posts to /api/nodes/<name>/image" "$log" "https://main.example/api/nodes/pc01/image"
contains "image report payload" "$log" '{"version":"0.2.0","revision":"abc1234","digest":"'"$DIGEST_A"'","tag":"nsu-llm-node:0.2.0-cpu"}'
lacks "image report does not register" "$log" "/api/nodes/register"
lacks "image report without a NIC posts no NIC" "$log" "/api/nodes/wake"
lacks "image report token not on the command line" "$log" "clustersecret42"
contains "image report token from a header file" "$(cat "$TMP/curl.log.hdr")" "Authorization: Bearer clustersecret42"
contains "image report says it is done" "$out" "reported to the portal"
out="$(FAKE_IMAGE_CODE=404 wr)"; rc=$?
[ "$(grep -c '/pc01/image' "$TMP/curl.log")" = 1 ] && pass "image report: a portal without the endpoint is not asked again" || fail "image report: a portal without the endpoint is not asked again"
out="$(FAKE_IMAGE_CODE=503 wr)"
[ "$(grep -c '/pc01/image' "$TMP/curl.log")" -ge 2 ] && pass "image report retries while the portal is down" || fail "image report retries while the portal is down"
out="$(WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=24 wr)"
contains "image report goes with the NIC report" "$(cat "$TMP/curl.log")" "/api/nodes/wake"
contains "image report goes with the NIC report (image)" "$(cat "$TMP/curl.log")" "/api/nodes/pc01/image"
# relay: the heartbeat reply asks this node to send magic packets for a down PC of its subnet (wol-send.sh)
printf '#!/usr/bin/env bash\necho "wol-send $*" >> "$FAKE_WOL_LOG"\n' > "$TMP/fakeserve/wol-send.sh"; chmod +x "$TMP/fakeserve/wol-send.sh"
: > "$TMP/wol.log"
out="$(FAKE_WOL_LOG="$TMP/wol.log" FAKE_WAKE='[{"mac":"00:1a:2b:3c:4d:5e","broadcast":"10.0.0.255"},{"mac":"00:1a:2b:3c:4d:5f","broadcast":"10.0.0.255"}]' reg 404)"
contains "registrar relay sends the first packet" "$(cat "$TMP/wol.log")" "wol-send 00:1a:2b:3c:4d:5e 10.0.0.255"
contains "registrar relay sends the second packet" "$(cat "$TMP/wol.log")" "wol-send 00:1a:2b:3c:4d:5f 10.0.0.255"
contains "registrar relay logs it" "$out" "wake-on-lan: magic packet for 00:1a:2b:3c:4d:5e"
: > "$TMP/wol.log"
out="$(FAKE_WOL_LOG="$TMP/wol.log" FAKE_WAKE='[{"mac":"x;reboot","broadcast":"10.0.0.255"}]' reg 404)"
lacks "registrar relay refuses junk" "$(cat "$TMP/wol.log")" "reboot"

# Cleanup of the models volume (task 2-26): the size travels with register and heartbeat, orders come back in the reply
out="$(reg 404)"
contains "registrar register carries the size of the models volume" "$(grep -- '/api/nodes/register' "$TMP/curl.log")" '"models_bytes":4242'
contains "registrar heartbeat carries the size of the models volume" "$(grep -- '/api/nodes/heartbeat' "$TMP/curl.log" | head -1)" '"models_bytes":4242'
: > "$TMP/clean.log"
rm -f "$TMP/regstate/purge-result.json"
ORDER='[{"id":"abc123","keep":["org/m:m.gguf","org/d:d.gguf"],"keep_rpc":true,"keep_vllm":false}]'
out="$(REG_TIMEOUT=6 FAKE_INTERVAL=1 FAKE_PURGE="$ORDER" FAKE_CLEAN_LOG="$TMP/clean.log" reg 404)"
contains "registrar runs a cleanup order with its keep list" "$(head -1 "$TMP/clean.log")" "ARGS=--keep org/m:m.gguf --keep org/d:d.gguf --keep-rpc"
lacks "registrar passes no keep-vllm that was not ordered" "$(head -1 "$TMP/clean.log")" "--keep-vllm"
contains "registrar logs the cleanup" "$out" "cleaning the models volume (order abc123)"
sent="$(grep -- '/api/nodes/heartbeat' "$TMP/curl.log" | grep purge_result | head -1)"
contains "registrar reports the result with a later heartbeat" "$sent" '"purge_result":{"freed_bytes":100,"models_bytes":42,"removed":["org__old"],"skipped":[],"dry_run":false,"id":"abc123"}'
# what the server of this node runs now stays whatever the order says
: > "$TMP/clean.log"
DESIRED_RUN='{"deployment":"d","model_repo":"o/r","model_file":"m-00001-of-00002.gguf","public_model_name":"d","ctx_size":2048,"parallel":2,"spec_hash":"h1","backend_api_key":"","llm_env":{"DRAFT_REPO":"org/d","DRAFT_FILE":"d.gguf","MMPROJ_FILE":"local/vis/proj.gguf"}}'
out="$(REG_TIMEOUT=6 FAKE_INTERVAL=1 FAKE_PURGE='[{"id":"run1","keep":[]}]' FAKE_CLEAN_LOG="$TMP/clean.log" reg 404 "$DESIRED_RUN")"
c="$(head -1 "$TMP/clean.log")"
contains "registrar keeps the model the server runs" "$c" "--keep o/r:m-00001-of-00002.gguf"
contains "registrar keeps the draft model the server runs" "$c" "--keep org/d:d.gguf"
contains "registrar keeps an own projector the server runs" "$c" "--keep local/vis:proj.gguf"
# junk orders and a failing script
: > "$TMP/clean.log"
out="$(REG_TIMEOUT=4 FAKE_PURGE='[{"id":"x;rm -rf /","keep":[]}]' FAKE_CLEAN_LOG="$TMP/clean.log" reg 404)"
[ ! -s "$TMP/clean.log" ] && pass "registrar ignores an order with a bad id" || fail "registrar ignores an order with a bad id"
contains "registrar says it ignored it" "$out" "cleanup order ignored: bad id"
out="$(REG_TIMEOUT=6 FAKE_INTERVAL=1 FAKE_PURGE='[{"id":"bad1","keep":[]}]' FAKE_CLEAN_FAIL=1 FAKE_CLEAN_LOG="$TMP/clean.log" reg 404)"
contains "registrar reports a failed cleanup as an error" "$(grep -- '/api/nodes/heartbeat' "$TMP/curl.log" | grep purge_result | head -1)" '"purge_result":{"id":"bad1","error":"models-cleanup: boom'
# the real script behind the registrar: the order removes the files it does not keep
mkdir -p "$TMP/real/models/org__keep" "$TMP/real/models/org__old" "$TMP/real/models/rpc"
head -c 100 /dev/zero > "$TMP/real/models/org__keep/m.gguf"; head -c 300 /dev/zero > "$TMP/real/models/org__old/o.gguf"; head -c 50 /dev/zero > "$TMP/real/models/rpc/aa"
touch -d '1 hour ago' "$TMP/real/models/org__keep/m.gguf" "$TMP/real/models/org__old/o.gguf" "$TMP/real/models/rpc/aa"
: > "$TMP/curl.log"; : > "$TMP/curl.log.hdr"; rm -f "$TMP/regstate/ca.crt" "$TMP/regstate/purge-result.json"
out="$(LLAMA_CACHE="$TMP/real/models" FAKE_CA_CODE=404 FAKE_INTERVAL=1 FAKE_PURGE='[{"id":"real1","keep":["org/keep:m.gguf"],"keep_rpc":false}]' FAKE_CURL_LOG="$TMP/curl.log" \
  PATH="$TMP/regbin:$BIN:$PATH" CLUSTER_TOKEN=t NODE_IP=10.0.0.9 NODE_NAME=reg-real MAIN_HOST=main.example ACCEL=cpu STATE_DIR="$TMP/regstate" timeout 7 bash "$BIN/registrar.sh" 2>&1)"
[ -f "$TMP/real/models/org__keep/m.gguf" ] && [ ! -d "$TMP/real/models/org__old" ] && [ -z "$(ls "$TMP/real/models/rpc")" ] \
  && pass "registrar with the real script removes what the order does not keep" || fail "registrar with the real script removes what the order does not keep"
contains "registrar reports the real result" "$(grep purge_result "$TMP/curl.log" | head -1)" '"freed_bytes":350'

# k3s mode: --models-report posts the size of the volume (no NODE_IP, no register), the token from a header file
mr() { # [api code] [extra env...]
  : > "$TMP/curl.log"; : > "$TMP/curl.log.hdr"; rm -f "$TMP/regstate/ca.crt"
  local code="${1:-200}"; shift
  env FAKE_CA_CODE=404 FAKE_API_CODE="$code" FAKE_CURL_LOG="$TMP/curl.log" PATH="$TMP/regbin:$TMP/fakeserve:$BIN:$PATH" CLUSTER_TOKEN=clustersecret42 \
    NODE_NAME=PC01 MAIN_HOST=main.example STATE_DIR="$TMP/regstate" WAKE_RETRY_S=1 "$@" timeout "${MR_TIMEOUT:-4}" bash "$BIN/registrar.sh" --models-report 2>&1
}
out="$(MODELS_REPORT_S=1 mr 200)"
[ "$(grep -c '/api/nodes/models' "$TMP/curl.log")" -ge 2 ] && pass "models report repeats" || fail "models report repeats"
log="$(cat "$TMP/curl.log")"
contains "models report posts to /api/nodes/models" "$log" "https://main.example/api/nodes/models"
contains "models report payload" "$log" '{"name":"pc01","models_bytes":4242}'
lacks "models report does not register" "$log" "/api/nodes/register"
lacks "models report token not on the command line" "$log" "clustersecret42"
contains "models report token from a header file" "$(cat "$TMP/curl.log.hdr")" "Authorization: Bearer clustersecret42"
out="$(MODELS_REPORT_S=1 mr 422)"; rc=$?
[ $rc -eq 1 ] && [ "$(grep -c '/api/nodes/models' "$TMP/curl.log")" = 1 ] && pass "models report refused: stops, no retry" || fail "models report refused: stops, no retry (rc=$rc)"
out="$(MODELS_REPORT_S=1 mr 404)"; rc=$?
[ $rc -eq 1 ] && pass "models report: an old portal without the endpoint stops it" || fail "models report: an old portal without the endpoint stops it (rc=$rc)"
out="$(MODELS_REPORT_S=300 mr 503)"
[ "$(grep -c '/api/nodes/models' "$TMP/curl.log")" -ge 2 ] && pass "models report retries while the portal is down" || fail "models report retries while the portal is down"
out="$(MODELS_REPORT_S=0 mr 200)"; rc=$?
[ $rc -eq 0 ] && ! grep -q '/api/nodes/models' "$TMP/curl.log" && pass "models report MODELS_REPORT_S=0 does nothing" || fail "models report MODELS_REPORT_S=0 does nothing (rc=$rc)"
out="$(MODELS_REPORT_S=1 FAKE_USAGE=junk mr 200)"
lacks "models report sends no size that is not a number" "$(cat "$TMP/curl.log")" "/api/nodes/models"

# --- wol-send.sh: the relay's sender (perl-base only), checked against a UDP listener on loopback ---
WS="$BIN/wol-send.sh"
[ -f "$WS" ] && bash -n "$WS" && pass "wol-send.sh exists" || fail "wol-send.sh exists"
if command -v perl >/dev/null 2>&1 && [ -f "$WS" ]; then
  port=$((29000 + RANDOM % 900))
  perl -MSocket -e 'socket(my $s, PF_INET, SOCK_DGRAM, 17) or die; bind($s, sockaddr_in($ARGV[0], inet_aton("127.0.0.1"))) or die "bind: $!";
    my $r = ""; my $rin = ""; vec($rin, fileno($s), 1) = 1;
    if (select(my $rout = $rin, undef, undef, 5)) { recv($s, $r, 2000, 0) }
    print unpack("H*", $r)' "$port" > "$TMP/udp.hex" &
  lp=$!
  sleep 1
  out="$(WOL_PORTS="$port" bash "$WS" 00-1A-2B-3C-4D-5E 127.0.0.1 2>&1)"; rc=$?
  wait "$lp"
  [ $rc -eq 0 ] && pass "wol-send.sh sends" || { fail "wol-send.sh sends (rc=$rc)"; echo "$out"; }
  want="ffffffffffff$(printf '001a2b3c4d5e%.0s' $(seq 16))"
  [ "$(cat "$TMP/udp.hex")" = "$want" ] && pass "wol-send.sh magic packet: 6 x ff + 16 x MAC" || fail "wol-send.sh magic packet (got $(cat "$TMP/udp.hex"))"
  contains "wol-send.sh says what it sent" "$out" "magic packet for 001a2b3c4d5e to 127.0.0.1"
  out="$(bash "$WS" 'x;id' 127.0.0.1 2>&1)"; rc=$?
  [ $rc -eq 2 ] && pass "wol-send.sh refuses a bad MAC" || fail "wol-send.sh refuses a bad MAC (rc=$rc)"
  out="$(bash "$WS" 00-1A-2B-3C-4D-5E 'nsu.ru' 2>&1)"; rc=$?
  [ $rc -eq 2 ] && pass "wol-send.sh refuses a name instead of an address" || fail "wol-send.sh refuses a name instead of an address (rc=$rc)"
else
  echo "skip - perl not found, wol-send.sh not exercised"
fi

out="$(bash "$RN" --dry-run --uninstall --main 192.168.3.21 --name test-node --token T 2>&1)"
contains "uninstall rm" "$out" "docker rm -f llm-node"
contains "uninstall delete" "$out" "curl -X DELETE"
contains "uninstall volumes" "$out" "docker volume rm"
[ "$(before "$out" "docker rm -f llm-node")" -lt "$(before "$out" "curl -X DELETE")" ] && pass "rm before DELETE" || fail "rm before DELETE"

# host-network cleanup runs from the image of the existing container, --image, or any local node image
mkdir -p "$TMP/dockerbin"
cat > "$TMP/dockerbin/docker" <<'EOS'
#!/usr/bin/env bash
case "$1" in
  inspect) [ -n "${FAKE_INSPECT:-}" ] && { echo "$FAKE_INSPECT"; exit 0; }; exit 1 ;;
  images) printf '%b
' "${FAKE_IMAGES:-}"; exit 0 ;;
esac
exit 0
EOS
chmod +x "$TMP/dockerbin/docker"
unin() { PATH="$TMP/dockerbin:$PATH" bash "$RN" --dry-run --uninstall --main 192.168.3.21 --name test-node --token T "$@" 2>&1; }

out="$(FAKE_INSPECT=nsu-llm-node:0.2.0-gpu FAKE_IMAGES="nsu-llm-node:0.2.0-cpu" unin --network host)"
contains "uninstall host chains" "$out" "KUBE-"
contains "uninstall host links" "$out" "flannel.1"
contains "uninstall host image from container" "$out" "--entrypoint bash nsu-llm-node:0.2.0-gpu"
contains "uninstall host never pulls" "$out" "--pull never"
out="$(FAKE_INSPECT=nsu-llm-node:0.2.0-gpu unin --network host --image my/img:1)"
contains "uninstall host --image wins" "$out" "--entrypoint bash my/img:1"
lacks "uninstall host --image wins (no gpu)" "$out" "nsu-llm-node:0.2.0-gpu"
out="$(FAKE_INSPECT= FAKE_IMAGES="other:1
nsu-llm-node:0.2.0-gpu
nsu-llm-node:0.2.0-cpu" unin --network host)"
contains "uninstall host image from local images" "$out" "--entrypoint bash nsu-llm-node:0.2.0-gpu"
out="$(FAKE_INSPECT= FAKE_IMAGES="other:1" unin --network host)"
contains "uninstall host without image warns" "$out" "Предупреждение: образ ноды не найден локально"
lacks "uninstall host without image skips cleanup" "$out" "--entrypoint"
contains "uninstall host without image still removes container" "$out" "docker rm -f llm-node"
out="$(FAKE_INSPECT=nsu-llm-node:0.2.0-gpu unin)"
lacks "uninstall bridge has no host cleanup" "$out" "--entrypoint"

# a data dir with root-owned files (rm without root fails) is removed through the node image
mkdir -p "$TMP/rootbin" "$TMP/rootdata/state"
cat > "$TMP/rootbin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
case "$1" in inspect) echo nsu-llm-node:0.2.0-cpu; exit 0 ;; esac
exit 0
EOS
cat > "$TMP/rootbin/rm" <<'EOS'
#!/usr/bin/env bash
echo "rm: cannot remove: Permission denied" >&2; exit 1
EOS
chmod +x "$TMP/rootbin/docker" "$TMP/rootbin/rm"
: > "$TMP/rootdocker.log"
out="$(DOCKER_LOG="$TMP/rootdocker.log" PATH="$TMP/rootbin:$PATH" bash "$RN" --uninstall --main 192.168.3.21 --name test-node --data-dir "$TMP/rootdata" 2>&1)"
log="$(cat "$TMP/rootdocker.log")"
contains "uninstall removes a root-owned data dir through the node image" "$log" "run --rm --pull never -v $TMP:/p --entrypoint rm nsu-llm-node:0.2.0-cpu -rf /p/rootdata"
contains "uninstall warns when the data dir is still there" "$out" "sudo rm -rf $TMP/rootdata"
rm -rf "$TMP/rootdata"

# --- run-node.sh --update (task 2-30): the node asks main whether to replace its container; main decides ---
UN="$TMP/upd-node"; cp -r "$NODE" "$UN"; mkdir -p "$TMP/updbin" "$TMP/upddata/state"
cat > "$UN/pull-image.sh" <<'EOS2'
#!/usr/bin/env bash
echo "pull-image $*" >> "$DOCKER_LOG"
[ "${FAKE_PULL_FAIL:-0}" = 1 ] && { echo "pull failed" >&2; exit 1; }
rec=""; prev=""; for a in "$@"; do [ "$prev" = --record ] && rec="$a"; prev="$a"; done
[ -z "$rec" ] || printf 'sha256:%s sha256:%s\n' "$(printf '1f%.0s' $(seq 32))" "$(printf 'cd%.0s' $(seq 32))" > "$rec"
exit 0
EOS2
cat > "$TMP/updbin/docker" <<'EOS2'
#!/usr/bin/env bash
echo "docker $* [CLUSTER_TOKEN=${CLUSTER_TOKEN-unset}]" >> "$DOCKER_LOG"
case "$1" in
  inspect)
    [ -n "${FAKE_CONTAINER:-}" ] || exit 1
    case "$*" in
      *"range .Config.Env"*) cat "$FAKE_ENV" ;;
      *".Config.Image"*) echo nsu-llm-node:0.2.0-cpu ;;
      *NetworkMode*) echo host ;;
      *Mounts*) echo "$FAKE_STATE_SRC" ;;
    esac; exit 0 ;;
  image) case "$*" in *"{{.Id}}"*) echo "sha256:$(printf '1f%.0s' $(seq 32))" ;; esac; exit 0 ;;
  info) echo 17179869184; exit 0 ;;
  run) [ "${FAKE_RUN_FAIL:-0}" = 1 ] && { echo "docker: run failed" >&2; exit 125; }; exit 0 ;;
esac
exit 0
EOS2
cat > "$TMP/updbin/curl" <<'EOS2'
#!/usr/bin/env bash
echo "curl $*" >> "$DOCKER_LOG"
prev=""; for a in "$@"; do [ "$prev" = -H ] && case "$a" in @*) cat "${a#@}" >> "$DOCKER_LOG.hdr" ;; esac; prev="$a"; done
case "${*: -1}" in
  *"/desired?"*) [ "${FAKE_CURL_FAIL:-0}" = 1 ] && { echo "curl: (7) Failed to connect" >&2; exit 7; }; cat "$FAKE_PLAN"; exit 0 ;;
  *"/v2/") printf '%s' "${FAKE_V2_CODE-200}"; exit 0 ;;
  *"/api/node-image") [ -n "${FAKE_NODE_IMAGE:-}" ] && { cat "$FAKE_NODE_IMAGE"; exit 0; }; exit 22 ;;
  *"/api/nodes/pc01") if [ -n "${FAKE_DELETE_CODE:-}" ]; then for ((i = 1; i <= $#; i++)); do [ "${!i}" = "-o" ] && { j=$((i + 1)); printf '%s' "${FAKE_DELETE_BODY:-}" > "${!j}"; }; done; printf '%s' "$FAKE_DELETE_CODE"; fi; exit 0 ;;
  *"/ca.crt") for ((i = 1; i <= $#; i++)); do [ "${!i}" = "-o" ] && { j=$((i + 1)); cp "${FAKE_CA:-/dev/null}" "${!j}" 2>/dev/null || echo CERT > "${!j}"; }; done; exit 0 ;;
esac
exit 0
EOS2
printf '#!/usr/bin/env bash\n[ "$1" = -u ] && echo "${FAKE_UID:-0}" || /usr/bin/id "$@"\n' > "$TMP/updbin/id"
export NSU_LLM_SYSTEMD_DIR="$TMP/sysd-default" SYSTEMCTL_LOG="$TMP/systemctl-default.log"   # no test writes to the real /etc/systemd
# iptables: logs every call; -C (rule exists?) says no, so that the hooks are inserted; DOCKER-USER exists
cat > "$TMP/updbin/iptables" <<'EOS2'
#!/usr/bin/env bash
echo "iptables $*" >> "${IPT_LOG:-/dev/null}"
case "$*" in *" -C "*) exit 1 ;; *" -D "*) exit 1 ;; esac
exit 0
EOS2
cat > "$TMP/updbin/iptables-restore" <<'EOS2'
#!/usr/bin/env bash
# the whole transaction is read first: nothing is "applied" when FAKE_RESTORE_FAIL=1 (as with a real failed COMMIT)
body="$(cat)"
echo "iptables-restore $*" >> "${IPT_LOG:-/dev/null}"
[ "${FAKE_RESTORE_FAIL:-0}" = 1 ] && { echo "iptables-restore: failed" >&2; exit 1; }
printf '%s\n' "$body" | sed 's/^/restore: /' >> "${IPT_LOG:-/dev/null}"
exit 0
EOS2
printf '#!/usr/bin/env bash\necho "systemctl $*" >> "${SYSTEMCTL_LOG:-/dev/null}"\nexit 0\n' > "$TMP/updbin/systemctl"
chmod +x "$TMP/updbin/iptables" "$TMP/updbin/iptables-restore" "$TMP/updbin/systemctl"
# a real CA certificate: the node checks its fingerprint
if command -v openssl >/dev/null 2>&1; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$TMP/ca.key" -out "$TMP/ca.pem" -subj "/CN=test-main" -days 2 >/dev/null 2>&1
  CA_FP="$(openssl x509 -in "$TMP/ca.pem" -outform DER | sha256sum | cut -d' ' -f1)"
  export FAKE_CA="$TMP/ca.pem"
fi
chmod +x "$UN/pull-image.sh" "$TMP/updbin/docker" "$TMP/updbin/curl" "$TMP/updbin/id"
OLD_DIGEST="sha256:$(printf '11%.0s' $(seq 32))"
NEW_DIGEST="sha256:$(printf 'cd%.0s' $(seq 32))"
cat > "$TMP/upd.env" <<EOS2
MODE=k3s
MAIN_HOST=main.example
CLUSTER_TOKEN=clustersecret42
NODE_NAME=pc01
NODE_IP=10.1.2.3
ACCEL=cpu
NODE_IMAGE_DIGEST=$OLD_DIGEST
PATH=/usr/local/sbin:/usr/bin
EOS2
printf 'llm.nsu/accel=cpu' > "$TMP/upddata/state/labels"; echo CERT > "$TMP/upddata/state/ca.crt"
plan() { printf 'go=%s\nimage=nsu-llm-node:0.2.0-cpu\ndigest=%s\nmode=requested\nwhen=now\nreason=%s\n' "$1" "${2:-$NEW_DIGEST}" "${3:-}" > "$TMP/upd.plan"; }
upd() { # [VAR=value ...] -> output of run-node.sh --update [args]; the docker/curl calls land in $TMP/upd.log
  : > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"
  env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_CONTAINER="${FAKE_CONTAINER-1}" FAKE_ENV="$TMP/upd.env" FAKE_STATE_SRC="$TMP/upddata/state" \
    FAKE_PLAN="$TMP/upd.plan" "$@" bash "$UN/run-node.sh" --update $UPD_ARGS 2>&1
}
updlog() { cat "$TMP/upd.log"; }
UPD_ARGS=""

plan 1
out="$(FAKE_CONTAINER= upd)"; rc=$?
if [ $rc -eq 0 ]; then pass "update without a container exits quietly"; else fail "update without a container exits quietly (rc=$rc)"; fi
contains "update without a container says there is nothing to update" "$out" "Контейнера llm-node нет"
lacks "update without a container asks nobody" "$(updlog)" "curl"
plan 0 "" "нода уже на актуальном образе"
out="$(upd)"; rc=$?
if [ $rc -eq 0 ]; then pass "update: main says no, exit 0"; else fail "update: main says no, exit 0 (rc=$rc) $out"; fi
contains "update: main says no" "$out" "Обновление не требуется (нода уже на актуальном образе)"
lacks "update: main says no, nothing is pulled" "$(updlog)" "pull-image"
lacks "update: main says no, the container stays" "$(updlog)" "docker rm"
contains "update asks main for this node and this accel, in lines" "$(updlog)" "https://main.example/api/nodes/pc01/desired?accel=cpu&format=lines"
contains "update verifies main against the pinned CA" "$(updlog)" "--cacert $TMP/upddata/state/ca.crt"
lacks "update: the token is not on the curl command line" "$(updlog)" "clustersecret42"
contains "update: the token goes in a header file" "$(cat "$TMP/upd.log.hdr")" "Authorization: Bearer clustersecret42"

plan 1
out="$(upd)"; rc=$?
if [ $rc -eq 0 ]; then pass "update: main says go, the container is replaced"; else fail "update: main says go, the container is replaced (rc=$rc)"; echo "$out"; fi
log="$(updlog)"
contains "update pulls from main with the pinned CA and records the digest" "$log" "pull-image --main main.example --image nsu-llm-node:0.2.0-cpu --digest $NEW_DIGEST --no-tofu --ca $TMP/upddata/state/ca.crt --record $TMP/upddata/state/image.digest"
[ "$(before "$log" "pull-image")" -lt "$(before "$log" "docker rm -f llm-node")" ] && [ "$(before "$log" "docker rm -f llm-node")" -lt "$(before "$log" "docker run")" ] \
  && pass "update: pull, then remove, then run" || fail "update: pull, then remove, then run"
run="$(grep 'docker run' "$TMP/upd.log")"
for want in "--hostname pc01" "--restart unless-stopped" "--network host" "-e MODE=k3s" "-e MAIN_HOST=main.example" "-e NODE_NAME=pc01" "-e NODE_IP=10.1.2.3" "-e ACCEL=cpu" \
    "-e NODE_IMAGE=nsu-llm-node:0.2.0-cpu" "-e NODE_IMAGE_DIGEST=$NEW_DIGEST" "-v $TMP/upddata/state:/var/lib/llm-node" "[CLUSTER_TOKEN=clustersecret42]"; do
  contains "update re-creates the container with the same parameter: $want" "$run" "$want"
done
lacks "update: the token is not on the docker command line" "${run%\[CLUSTER_TOKEN*}" "clustersecret42"
contains "update: the new image id is saved for the k3s agent" "$log" "docker save --platform linux/amd64 nsu-llm-node:0.2.0-cpu -o $TMP/upddata/airgap/node.tar"
contains "update says what happened" "$out" "Нода pc01 запущена"

# a record that belongs to another image id is not handed to the container (an archive loaded over the tag)
rm -f "$TMP/upddata/state/image.digest"
printf 'sha256:%s sha256:%s\n' "$(printf '99%.0s' $(seq 32))" "$NEW_DIGEST" > "$TMP/upd.stale"
cp "$UN/pull-image.sh" "$UN/pull-image.sh.keep"
cat > "$UN/pull-image.sh" <<'EOS2'
#!/usr/bin/env bash
echo "pull-image $*" >> "$DOCKER_LOG"
rec=""; prev=""; for a in "$@"; do [ "$prev" = --record ] && rec="$a"; prev="$a"; done
cp "$FAKE_RECORD" "$rec"
exit 0
EOS2
chmod +x "$UN/pull-image.sh"
plan 1
out="$(upd FAKE_RECORD="$TMP/upd.stale")"
lacks "update: a record of another image id gives the container no digest" "$(updlog | grep 'docker run')" "NODE_IMAGE_DIGEST"
mv "$UN/pull-image.sh.keep" "$UN/pull-image.sh"; rm -f "$TMP/upddata/state/image.digest"

plan 1 "$OLD_DIGEST"
out="$(upd)"; rc=$?
if [ $rc -eq 0 ]; then pass "update: the container already runs the image, exit 0"; else fail "update: the container already runs the image, exit 0 (rc=$rc)"; fi
contains "update: the container already runs the image" "$out" "уже работает на образе"
lacks "update: nothing is pulled when the image is the same" "$(updlog)" "pull-image"

plan 1
out="$(upd FAKE_PULL_FAIL=1)"; rc=$?
if [ $rc -ne 0 ]; then pass "update: a failed pull fails"; else fail "update: a failed pull fails"; fi
lacks "update: a failed pull leaves the container alone" "$(updlog)" "docker rm"
contains "update: a failed pull is reported to main" "$(updlog)" "-X POST"
contains "update: the report goes to the image endpoint" "$(updlog)" "/api/nodes/pc01/image"
contains "update: the reason is in Russian and says the container was not touched" "$(updlog)" 'update_error":"образ nsu-llm-node:0.2.0-cpu не удалось скачать с главного'

out="$(upd FAKE_RUN_FAIL=1)"; rc=$?
if [ $rc -ne 0 ]; then pass "update: a failed docker run fails"; else fail "update: a failed docker run fails"; fi
contains "update: a failed docker run is reported too (the old container is gone)" "$(updlog)" 'update_error":"контейнер не удалось создать заново'

out="$(upd FAKE_CURL_FAIL=1)"; rc=$?
if [ $rc -eq 0 ]; then pass "update: main does not answer, exit 0"; else fail "update: main does not answer, exit 0 (rc=$rc)"; fi
contains "update: main does not answer, nothing changes" "$out" "Главный не ответил"
lacks "update: main does not answer, no docker rm" "$(updlog)" "docker rm"

printf 'go=1\nimage=evil:1\ndigest=%s\n' "$NEW_DIGEST" > "$TMP/upd.plan"
out="$(upd)"; rc=$?
if [ $rc -ne 0 ]; then pass "update refuses an image name that is not ours"; else fail "update refuses an image name that is not ours"; fi
contains "update names the bad image" "$out" "недопустимый образ"
lacks "update: a foreign image is not pulled" "$(updlog)" "pull-image"
printf 'go=1\nimage=nsu-llm-node:0.2.0-cpu\ndigest=sha256:short\n' > "$TMP/upd.plan"
out="$(upd)"; rc=$?
if [ $rc -ne 0 ]; then pass "update refuses a malformed digest"; else fail "update refuses a malformed digest"; fi
contains "update names the bad digest" "$out" "недопустимый digest"

plan 1
out="$(upd FAKE_UID=1000)"; rc=$?
if [ $rc -ne 0 ]; then pass "update needs root"; else fail "update needs root"; fi
contains "update without root says why" "$out" "нужны права root"
lacks "update without root pulls nothing" "$(updlog)" "pull-image"

plan 1
UPD_ARGS="--dry-run"
out="$(upd)"; rc=$?
UPD_ARGS=""
if [ $rc -eq 0 ]; then pass "update --dry-run exits 0"; else fail "update --dry-run exits 0 (rc=$rc)"; echo "$out"; fi
contains "update --dry-run prints the pull" "$out" "pull-image.sh --main main.example --image nsu-llm-node:0.2.0-cpu"
contains "update --dry-run prints docker run" "$out" "docker run -d --name llm-node"
lacks "update --dry-run pulls nothing" "$(updlog)" "pull-image"
lacks "update --dry-run removes nothing" "$(updlog)" "docker rm"

# --- the image of a fresh run comes from main (NODE_IMAGE_TAG), not from VERSION of the script (task 2-30) ---
# (found on the 122b bed: the script said 0.1.0 while main ran 0.1.1, and the node looked for an image nobody had pulled)
imgrun() { # [VAR=value ...] -> output of run-node.sh (standalone, no dry run); every docker and curl call lands in $TMP/upd.log
  : > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"; rm -rf "$TMP/imgdata"
  env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_PLAN="$TMP/upd.plan" "$@" bash "$UN/run-node.sh" --main main.example \
    --token clustersecret42 --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca --no-firewall $IMG_ARGS 2>&1
}
runline() { grep '^docker run' "$TMP/upd.log"; }
printf 'name=nsu-llm-node:0.1.1-cpu\ngo=0\nimage=\ndigest=\nmode=\nwhen=\nreason=\n' > "$TMP/upd.plan"
IMG_ARGS="--accel cpu"
out="$(imgrun)"; rc=$?
if [ $rc -eq 0 ]; then pass "fresh run: the node starts"; else fail "fresh run: the node starts (rc=$rc)"; echo "$out"; fi
contains "fresh run: the image is the one main names, not VERSION of the script" "$(runline)" " nsu-llm-node:0.1.1-cpu"
contains "fresh run: the container knows its image name" "$(runline)" "-e NODE_IMAGE=nsu-llm-node:0.1.1-cpu"
contains "fresh run: main is asked for this node and this accel" "$(cat "$TMP/upd.log")" "https://main.example/api/nodes/pc01/desired?accel=cpu&format=lines"
lacks "fresh run: the token is not on the curl command line" "$(grep '^curl' "$TMP/upd.log")" "clustersecret42"
contains "fresh run: the token goes in a header file" "$(cat "$TMP/upd.log.hdr")" "Authorization: Bearer clustersecret42"
contains "fresh run: the image is checked under that name" "$(cat "$TMP/upd.log")" "docker image inspect nsu-llm-node:0.1.1-cpu"
printf 'name=nsu-llm-node:0.1.1-gpu\ngo=0\nimage=\ndigest=\nmode=\nwhen=\nreason=\n' > "$TMP/upd.plan"
printf '#!/usr/bin/env bash\ncase "$*" in *--query-gpu*) echo 12282 ;; *) echo "GPU 0: NVIDIA GeForce RTX 4070 (UUID: GPU-x)" ;; esac\n' > "$TMP/updbin/nvidia-smi"; chmod +x "$TMP/updbin/nvidia-smi"
IMG_ARGS="--accel nvidia"
out="$(imgrun)"
contains "fresh run on a GPU asks for the gpu image of main's tag" "$(cat "$TMP/upd.log")" "/desired?accel=nvidia&format=lines"
contains "fresh run on a GPU runs the gpu image of main's tag" "$(runline)" " nsu-llm-node:0.1.1-gpu"
IMG_ARGS="--accel cpu --image nsu-llm-node:custom"
out="$(imgrun)"
contains "--image wins" "$(runline)" " nsu-llm-node:custom"
lacks "--image wins: main is not asked which image to run" "$(cat "$TMP/upd.log")" "docker image inspect nsu-llm-node:0.1.1"
IMG_ARGS="--accel cpu"
out="$(imgrun FAKE_CURL_FAIL=1)"
contains "main does not answer: the version of the script" "$(runline)" " nsu-llm-node:0.2.0-cpu"
printf 'name=evil:1\n' > "$TMP/upd.plan"
out="$(imgrun)"
contains "a name that is not ours is ignored: the version of the script" "$(runline)" " nsu-llm-node:0.2.0-cpu"
printf 'name=nsu-llm-node:0.1.1-cpu\n' > "$TMP/upd.plan"
out="$(env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_PLAN="$TMP/upd.plan" bash "$UN/run-node.sh" --dry-run --main main.example --token T --mode standalone --accel cpu --node-ip 10.1.2.3 --name pc01 2>&1)"
contains "--dry-run asks nobody and uses the version of the script" "$out" " nsu-llm-node:0.2.0-cpu"

out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "missing token fails" || fail "missing token fails"
out="$(bash "$RN" --dry-run --token T 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "missing main fails" || fail "missing main fails"

# node names follow the portal rule (DNS-1123 label, nodes/routes.py)
n63="$(printf 'a%.0s' $(seq 63))"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --token T --accel cpu --name "$n63" 2>&1)" \
  && pass "run accepts a 63-char name" || fail "run accepts a 63-char name"
for bad in "a.b" "${n63}a" "-ab" "a_b"; do
  out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --token T --accel cpu --name "$bad" 2>&1)"; rc=$?
  [ $rc -ne 0 ] && pass "run rejects name '$bad'" || fail "run rejects name '$bad'"
done
contains "run name error explains the rule" "$out" "до 63 символов"

# uninstall reports a DELETE the portal did not accept
mkdir -p "$TMP/delbin"
printf '#!/usr/bin/env bash\nprintf %%s "$FAKE_CODE"\n' > "$TMP/delbin/curl"
chmod +x "$TMP/delbin/curl"
unreal() { CLUSTER_TOKEN=T FAKE_CODE="$1" PATH="$TMP/delbin:$TMP/dockerbin:$PATH" bash "$RN" --uninstall --main 192.168.3.21 --name test-node \
  --insecure-tls --no-firewall --data-dir "$TMP/undata" 2>&1; }
out="$(unreal 401)"
contains "uninstall warns when DELETE fails" "$out" "Предупреждение: DELETE https://192.168.3.21/api/nodes/test-node не выполнен (ответ: 401)"
contains "uninstall names the panel route as the other way out" "$out" "«Удалить из кластера…»"
contains "uninstall 401 says the token may be outdated" "$out" "Главный не принял токен"
contains "uninstall 401 names the fix of the token" "$out" "--token"
contains "uninstall still prints the exact fix on the main" "$out" "docker exec llm-main k3s kubectl delete node test-node"
# the same words in the Windows scripts (their DELETE needs curl.exe or WSL and is exercised by the dry runs above)
contains "run-node.ps1 warning names the panel route" "$(cat "$NODE/run-node.ps1")" "«Удалить из кластера…»"
contains "run-node.ps1 warns about an outdated token on 401" "$(cat "$NODE/run-node.ps1")" "Главный не принял токен"
contains "run-node.ps1 warning keeps the kubectl fix" "$(cat "$NODE/run-node.ps1")" 'k3s kubectl delete node $Name'
out="$(unreal 204)"
lacks "uninstall 204 is fine" "$out" "Предупреждение"
out="$(unreal 404)"
lacks "uninstall 404 is fine" "$out" "Предупреждение"

if [ -n "$PS" ]; then
  out="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$NODE/run-node.ps1")" -DryRun -Main 192.168.3.21 -Token T \
    -NodeIp 192.168.3.5 -Accel cpu -Name a.b 2>&1)"
  contains "ps1 rejects a name the portal rejects" "$out" "Недопустимое имя ноды"
  out="$(HF_TOKEN=hfsecret9 THREADS=3 psnode -Mode standalone)"
  contains "ps1 passes HF_TOKEN through" "$out" "-e HF_TOKEN "
  contains "ps1 passes THREADS through" "$out" "-e THREADS "
  out="$(MODELS_REPORT_S=0 psnode -Mode standalone)"
  contains "ps1 passes MODELS_REPORT_S through" "$out" "-e MODELS_REPORT_S "
  lacks "ps1 HF_TOKEN value not on the command line" "$out" "hfsecret9"
  lacks "ps1 HF_ENDPOINT only when set" "$out" "HF_ENDPOINT"
fi

# labels changed -> refuse
mkdir -p "$TMP/data/state"
echo "llm.nsu/accel=nvidia" > "$TMP/data/state/labels"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --name n1 --token T --accel cpu --data-dir "$TMP/data" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "labels changed refuses" || fail "labels changed refuses"
contains "labels changed msg" "$out" "Метки ноды изменились — выполните --uninstall и запустите снова"
echo "llm.nsu/accel=cpu" > "$TMP/data/state/labels"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --name n1 --token T --accel cpu --data-dir "$TMP/data" 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass "labels same ok" || { fail "labels same ok"; echo "$out"; }

# ===================================================================== SEC-3: hardening of the node (token, CA, node secret, data dir, RPC firewall)
# --- M6: the cluster token is not taken from argv --------------------------------------------------------------------------------
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu 2>&1)"
contains "token: --token still works but warns about argv" "$out" "--token VALUE виден всем локальным пользователям"
contains "token: the warning names the safe ways" "$out" "--token-file"
printf 'filetoken77\n' > "$TMP/tokfile"; chmod 600 "$TMP/tokfile"
IMG_ARGS="--accel cpu --accept-ca"
imgrun_tok() { # args for the token; the docker stub logs the CLUSTER_TOKEN it saw at docker run
  : > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"; rm -rf "$TMP/imgdata"
  env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_PLAN="$TMP/upd.plan" IPT_LOG="$TMP/ipt.log" "$@" 2>&1
}
printf 'name=nsu-llm-node:0.1.1-cpu\ngo=0\n' > "$TMP/upd.plan"
out="$(imgrun_tok bash "$UN/run-node.sh" --main main.example --token-file "$TMP/tokfile" --accel cpu --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca --no-firewall)"; rc=$?
if [ $rc -eq 0 ]; then pass "token-file: the node starts"; else fail "token-file: the node starts (rc=$rc)"; echo "$out"; fi
contains "token-file: the token reaches docker through the environment" "$(grep '^docker run' "$TMP/upd.log")" "[CLUSTER_TOKEN=filetoken77]"
lacks "token-file: no argv warning" "$out" "виден всем локальным пользователям"
lacks "token-file: the token is not on the curl command line" "$(grep '^curl' "$TMP/upd.log")" "filetoken77"
out="$(printf 'stdintok88\n' | imgrun_tok bash "$UN/run-node.sh" --main main.example --token-stdin --accel cpu --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca --no-firewall)"
contains "token from stdin" "$(grep '^docker run' "$TMP/upd.log")" "[CLUSTER_TOKEN=stdintok88]"
out="$(CLUSTER_TOKEN=envtok99 imgrun_tok bash "$UN/run-node.sh" --main main.example --accel cpu --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca --no-firewall)"
contains "token from CLUSTER_TOKEN of the environment" "$(grep '^docker run' "$TMP/upd.log")" "[CLUSTER_TOKEN=envtok99]"
lacks "token from the environment: no argv warning" "$out" "виден всем локальным пользователям"
out="$(env -u CLUSTER_TOKEN bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --accel cpu 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "no token anywhere fails" || fail "no token anywhere fails"
contains "no token: the message names the three sources" "$out" "--token-file FILE, --token-stdin или переменная CLUSTER_TOKEN"
: > "$TMP/emptytok"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --accel cpu --token-file "$TMP/emptytok" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "empty token file fails" || fail "empty token file fails"
chmod 644 "$TMP/tokfile"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --accel cpu --token-file "$TMP/tokfile" 2>&1)"
contains "a token file other users can read is flagged" "$out" "доступен другим пользователям"
chmod 600 "$TMP/tokfile"
# the docs promised the opposite of what the script did
lacks "docs: no false claim about the command line" "$(cat "$NODE/../docs/deploy-class.md")" "токен не попадает в командные строки"
contains "docs: the token comes from a file or the environment" "$(cat "$NODE/../docs/deploy-class.md")" "--token-file"

# --- M1/rule 5: the CA of main is checked against a fingerprint, never taken on trust --------------------------------------------
if [ -n "${CA_FP:-}" ]; then
  ca_run() { # [run-node args] -> output; $TMP/imgdata/state/ca.crt appears only when the root was accepted
    imgrun_tok bash "$UN/run-node.sh" --main main.example --token clustersecret42 --accel cpu --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --no-firewall "$@"
  }
  out="$(ca_run --ca-sha256 "$(printf '%064d' 7)")"; rc=$?
  [ $rc -ne 0 ] && pass "CA with a wrong fingerprint stops the run" || fail "CA with a wrong fingerprint stops the run"
  contains "CA mismatch: says it may be a spoof" "$out" "не совпал с --ca-sha256"
  [ ! -e "$TMP/imgdata/state/ca.crt" ] && pass "CA mismatch: the root is not saved" || fail "CA mismatch: the root is not saved"
  lacks "CA mismatch: the token was never sent" "$(cat "$TMP/upd.log.hdr")" "clustersecret42"
  out="$(ca_run --ca-sha256 "$CA_FP")"; rc=$?
  if [ $rc -eq 0 ]; then pass "CA with the right fingerprint: the run goes on"; else fail "CA with the right fingerprint (rc=$rc)"; echo "$out"; fi
  contains "CA match is reported" "$out" "совпал с --ca-sha256"
  [ -s "$TMP/imgdata/state/ca.crt" ] && pass "CA match: the root is pinned" || fail "CA match: the root is pinned"
  out="$(ca_run --ca-sha256 "$(printf '%s' "$CA_FP" | tr 'a-f' 'A-F' | sed 's/../&:/g; s/:$//')")"; rc=$?
  [ $rc -eq 0 ] && pass "CA fingerprint in the colon/upper-case form of openssl is accepted" || fail "CA fingerprint in the colon form (rc=$rc)"
  out="$(ca_run < /dev/null)"; rc=$?
  [ $rc -ne 0 ] && pass "CA without a fingerprint and nobody to ask stops the run" || fail "CA without a fingerprint and nobody to ask stops the run"
  contains "CA without a fingerprint: the fingerprint is printed" "$out" "Отпечаток корня CA главного (SHA-256): $CA_FP"
  contains "CA without a fingerprint: names both ways out" "$out" "--accept-ca"
  [ ! -e "$TMP/imgdata/state/ca.crt" ] && pass "CA without a fingerprint: the root is not saved" || fail "CA without a fingerprint: the root is not saved"
  out="$(ca_run --accept-ca)"; rc=$?
  if [ $rc -eq 0 ]; then pass "--accept-ca takes the root"; else fail "--accept-ca takes the root (rc=$rc)"; echo "$out"; fi
  contains "--accept-ca warns that nothing was compared" "$out" "принят по --accept-ca без сверки"
  [ -s "$TMP/imgdata/state/ca.crt" ] && pass "--accept-ca: the root is pinned" || fail "--accept-ca: the root is pinned"
  out="$(ca_run --ca-sha256 xyz)"; rc=$?
  [ $rc -ne 0 ] && pass "a malformed --ca-sha256 is refused" || fail "a malformed --ca-sha256 is refused"
  # the unattended update never downloads a root (it would hand the token to whoever answers first)
  rm -f "$TMP/upddata/state/ca.crt"; plan 0 "" "нода уже на актуальном образе"
  out="$(upd)"
  lacks "update does not fetch a CA by itself" "$(updlog)" "/ca.crt"
  echo CERT > "$TMP/upddata/state/ca.crt"
else
  echo "skip - openssl not found, CA fingerprint tests skipped"
fi

# --- rule 6: the node signs its calls with its own secret; the data dir is for root ------------------------------------------------
NK="$(printf 'K%.0s' $(seq 43))"
echo "$NK" > "$TMP/upddata/state/node.key"
plan 0 "" "нода уже на актуальном образе"
out="$(upd)"
contains "node key: the update question carries the token as before" "$(cat "$TMP/upd.log.hdr")" "Authorization: Bearer clustersecret42"
contains "node key: and the key of this node next to it" "$(cat "$TMP/upd.log.hdr")" "X-Node-Key: $NK"
lacks "node key: the key is not on the curl command line" "$(grep '^curl' "$TMP/upd.log")" "$NK"
echo "short" > "$TMP/upddata/state/node.key"
out="$(upd)"
lacks "a malformed node.key is not sent" "$(cat "$TMP/upd.log.hdr")" "X-Node-Key"
rm -f "$TMP/upddata/state/node.key"
out="$(upd)"
contains "no node key yet: the token alone, as before" "$(cat "$TMP/upd.log.hdr")" "Authorization: Bearer clustersecret42"
lacks "no node key yet: no X-Node-Key" "$(cat "$TMP/upd.log.hdr")" "X-Node-Key"
mkdir -p "$TMP/unidata/state"; echo "$NK" > "$TMP/unidata/state/node.key"
: > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"
out="$(env CLUSTER_TOKEN=clustersecret42 PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_CONTAINER=1 FAKE_ENV="$TMP/upd.env" FAKE_STATE_SRC="$TMP/unidata/state" IPT_LOG="$TMP/ipt.log" \
  bash "$UN/run-node.sh" --uninstall --main main.example --name pc01 --data-dir "$TMP/unidata" --insecure-tls --no-firewall 2>&1)"
contains "node key: the DELETE of the uninstall carries it" "$(cat "$TMP/upd.log.hdr")" "X-Node-Key: $NK"
contains "node key: and the token" "$(cat "$TMP/upd.log.hdr")" "Authorization: Bearer clustersecret42"
[ ! -e "$TMP/unidata" ] && pass "uninstall removes the data dir with node.key and backend/" || fail "uninstall removes the data dir"
# a refusal of main is shown with its own words
mkdir -p "$TMP/unidata/state"; echo "$NK" > "$TMP/unidata/state/node.key"
out="$(FAKE_DELETE_CODE=403 FAKE_DELETE_BODY='{"detail":{"code":"node_key_invalid","message":"ключ ноды сброшен администратором"}}' env CLUSTER_TOKEN=clustersecret42 PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_CONTAINER=1 FAKE_ENV="$TMP/upd.env" FAKE_STATE_SRC="$TMP/unidata/state" IPT_LOG="$TMP/ipt.log" FAKE_DELETE_CODE=403 FAKE_DELETE_BODY='{"detail":{"code":"node_key_invalid","message":"ключ ноды сброшен администратором"}}' \
  bash "$UN/run-node.sh" --uninstall --main main.example --name pc01 --data-dir "$TMP/unidata" --insecure-tls --no-firewall 2>&1)"
contains "a 403 of main on DELETE shows its message" "$out" "ключ ноды сброшен администратором"
contains "a 403 of main on DELETE still names the panel route" "$out" "«Удалить из кластера…»"
# a fresh run creates state and airgap for root only
printf 'name=nsu-llm-node:0.1.1-cpu\ngo=0\n' > "$TMP/upd.plan"
out="$(imgrun_tok bash "$UN/run-node.sh" --main main.example --token clustersecret42 --accel cpu --mode k3s --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca --no-firewall)"
for d in "" /state /airgap; do
  m="$(stat -c %a "$TMP/imgdata$d" 2>/dev/null)"
  [ "$m" = 700 ] && pass "data dir ${d:-root}: mode 0700" || fail "data dir ${d:-root}: mode 0700 (got $m)"
done
mkdir -p "$TMP/imgdata2/state" "$TMP/imgdata2/airgap"; chmod 755 "$TMP/imgdata2" "$TMP/imgdata2/state"
echo evil > "$TMP/imgdata2/airgap/evil.tar"; echo ok > "$TMP/imgdata2/airgap/node.id"
out="$(imgrun_tok bash "$UN/run-node.sh" --main main.example --token clustersecret42 --accel cpu --mode k3s --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata2" --accept-ca --no-firewall)"
[ "$(stat -c %a "$TMP/imgdata2" 2>/dev/null)" = 700 ] && pass "an older open data dir is tightened" || fail "an older open data dir is tightened"
[ ! -e "$TMP/imgdata2/airgap/evil.tar" ] && pass "a foreign archive in airgap is removed (k3s would import it)" || fail "a foreign archive in airgap is removed"

# --- H2: the firewall of the Linux node -------------------------------------------------------------------------------------------
printf 'MODE=k3s\nMAIN_HOST=10.9.0.5\nCLUSTER_TOKEN=clustersecret42\nNODE_NAME=pc01\nNODE_IP=10.1.2.3\nACCEL=cpu\nNODE_IMAGE_DIGEST=%s\n' "$OLD_DIGEST" > "$TMP/upd2.env"
planp() { printf 'go=0\nreason=нода уже на актуальном образе\nrpc_peers=%s\n' "$1" > "$TMP/upd.plan"; }
fwupd() { : > "$TMP/ipt.log"; upd FAKE_ENV="$TMP/upd2.env" IPT_LOG="$TMP/ipt.log" "$@"; }
ipt() { cat "$TMP/ipt.log"; }
planp "10.1.2.7,10.1.2.8"
out="$(fwupd)"
contains "firewall: main may reach the RPC port" "$(ipt)" "restore: -A NSU-LLM-RPC -s 10.9.0.5 -j RETURN"
contains "firewall: a node of the group may reach the RPC port" "$(ipt)" "restore: -A NSU-LLM-RPC -s 10.1.2.7 -j RETURN"
contains "firewall: the other node of the group too" "$(ipt)" "restore: -A NSU-LLM-RPC -s 10.1.2.8 -j RETURN"
contains "firewall: everybody else is dropped on the RPC port" "$(ipt)" "restore: -A NSU-LLM-RPC -j DROP"
contains "firewall: main may reach the inference port" "$(ipt)" "restore: -A NSU-LLM-INFER -s 10.9.0.5 -j RETURN"
lacks "firewall: the group does not get the inference port" "$(grep 'NSU-LLM-INFER' "$TMP/ipt.log")" "10.1.2.7"
contains "firewall: the RPC chain is hooked on INPUT (host network)" "$(ipt)" "iptables -w -I INPUT 1 -p tcp -m conntrack --ctorigdstport 18050 --ctdir ORIGINAL -j NSU-LLM-RPC"
contains "firewall: and on FORWARD for what Docker and hostPort DNAT" "$(ipt)" "iptables -w -I FORWARD 1 -p tcp -m conntrack --ctstate DNAT --ctorigdstport 18050 --ctdir ORIGINAL -j NSU-LLM-RPC"
contains "firewall: and on DOCKER-USER" "$(ipt)" "iptables -w -I DOCKER-USER 1 -p tcp -m conntrack --ctstate DNAT --ctorigdstport 18050 --ctdir ORIGINAL -j NSU-LLM-RPC"
contains "firewall: local users other than root cannot open the RPC port" "$(ipt)" "-m owner ! --uid-owner 0 -j REJECT"
planp "10.1.2.7;calc,300.1.1.1x,10.1.2.9"
out="$(fwupd)"
contains "firewall: a valid peer is taken" "$(ipt)" "-A NSU-LLM-RPC -s 10.1.2.9 -j RETURN"
lacks "firewall: junk in rpc_peers never reaches iptables" "$(ipt)" "calc"
planp ""
out="$(fwupd)"
contains "firewall: no peers: main only" "$(ipt)" "restore: -A NSU-LLM-RPC -s 10.9.0.5 -j RETURN"
lacks "firewall: no peers: nobody else" "$(grep 'NSU-LLM-RPC -s' "$TMP/ipt.log")" "10.1.2"
printf 'go=0\nreason=x\n' > "$TMP/upd.plan"
out="$(fwupd)"
[ ! -s "$TMP/ipt.log" ] && pass "firewall: an older main that names no rpc_peers: the rules are left alone" || fail "firewall: older main, rules untouched"
planp "10.1.2.7"
out="$(UPD_ARGS=--no-firewall fwupd)"
[ ! -s "$TMP/ipt.log" ] && pass "--no-firewall leaves iptables alone" || fail "--no-firewall leaves iptables alone"
printf 'MODE=standalone\nMAIN_HOST=10.9.0.5\nCLUSTER_TOKEN=clustersecret42\nNODE_NAME=pc01\nNODE_IP=10.1.2.3\nACCEL=cpu\nNODE_IMAGE_DIGEST=%s\n' "$OLD_DIGEST" > "$TMP/upd3.env"
out="$(fwupd FAKE_ENV="$TMP/upd3.env")"
lacks "firewall: a standalone node gets no RPC chain" "$(ipt)" "-A NSU-LLM-RPC"
contains "firewall: but its inference port is still main-only" "$(ipt)" "restore: -A NSU-LLM-INFER -s 10.9.0.5 -j RETURN"
out="$(UPD_ARGS="--allow-from 10.5.5.5" fwupd)"
contains "--allow-from adds an address for the inference port" "$(ipt)" "restore: -A NSU-LLM-INFER -s 10.5.5.5 -j RETURN"
lacks "--allow-from does not open the RPC port" "$(grep 'NSU-LLM-RPC -s' "$TMP/ipt.log")" "10.5.5.5"
out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 1.2.3.4 --token T --accel cpu --allow-from '10.0.0.0/8;calc' 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass "--allow-from with junk fails" || fail "--allow-from with junk fails"
: > "$TMP/ipt.log"
mkdir -p "$TMP/unidata2/state"
out="$(env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_CONTAINER=1 FAKE_ENV="$TMP/upd2.env" FAKE_STATE_SRC="$TMP/unidata2/state" IPT_LOG="$TMP/ipt.log" \
  bash "$UN/run-node.sh" --uninstall --main 10.9.0.5 --name pc01 --data-dir "$TMP/unidata2" --insecure-tls 2>&1)"
contains "uninstall takes the RPC chain away" "$(ipt)" "iptables -w -X NSU-LLM-RPC"
contains "uninstall takes the inference chain away" "$(ipt)" "iptables -w -X NSU-LLM-INFER"
contains "uninstall takes the owner rule away" "$(ipt)" "-D OUTPUT -p tcp --dport 18050 -m owner ! --uid-owner 0 -j REJECT"
# the update pulls by the digest main named
printf 'go=1\nimage=nsu-llm-node:0.2.0-cpu\ndigest=%s\nmode=requested\nwhen=now\nreason=\nrpc_peers=10.1.2.7\n' "$NEW_DIGEST" > "$TMP/upd.plan"
out="$(UPD_ARGS=--no-firewall fwupd)"
contains "update pulls by the digest main named" "$(updlog)" "--image nsu-llm-node:0.2.0-cpu --digest $NEW_DIGEST"

# --- CA of main by the join token K10<sha256 of the CA of k3s>::... (fetch-main-ca.sh of main checks the signed /ca.crt) ----------------------
if [ -n "${CA_FP:-}" ]; then
  mkdir -p "$UN/rootfs/opt/llm/bin"
  cat > "$UN/rootfs/opt/llm/bin/fetch-main-ca.sh" <<'EOS2'
#!/usr/bin/env bash
# stand-in for the helper of main: logs the call, never the token; FAKE_HELPER_RC is the verdict
out=""; prev=""; for a in "$@"; do [ "$prev" = --out ] && out="$a"; prev="$a"; done
echo "helper $* token_in_env=$([ -n "${CLUSTER_TOKEN:-}" ] && echo yes || echo no)" >> "$DOCKER_LOG"
rc="${FAKE_HELPER_RC:-0}"
[ "$rc" = 0 ] && cp "$FAKE_CA" "$out"
exit "$rc"
EOS2
  K10="K10$(printf 'ab%.0s' $(seq 32))::node:secret"
  k10_run() { # [FAKE_HELPER_RC=n] [run-node args] -> output
    local rcv="$1"; shift
    : > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"; rm -rf "$TMP/imgdata"
    env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_PLAN="$TMP/upd.plan" IPT_LOG="$TMP/ipt.log" FAKE_HELPER_RC="$rcv" CLUSTER_TOKEN="$K10" \
      bash "$UN/run-node.sh" --main main.example --accel cpu --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --no-firewall "$@" 2>&1 < /dev/null
  }
  out="$(k10_run 0)"; rc=$?
  if [ $rc -eq 0 ]; then pass "K10 token, root verified by the helper: the node starts without any question"; else fail "K10 token, verified root (rc=$rc)"; echo "$out"; fi
  [ -s "$TMP/imgdata/state/ca.crt" ] && pass "K10 token: the verified root is pinned" || fail "K10 token: the verified root is pinned"
  contains "K10 token: the helper is called for this main" "$(cat "$TMP/upd.log")" "helper --main main.example --out $TMP/imgdata/state/ca.crt.tmp"
  contains "K10 token: the token reaches the helper through the environment only" "$(cat "$TMP/upd.log")" "token_in_env=yes"
  lacks "K10 token: nothing was asked (no fingerprint prompt, no --accept-ca needed)" "$out" "Отпечаток"
  out="$(k10_run 1)"; rc=$?
  [ $rc -ne 0 ] && pass "K10 token, helper says mismatch: the run stops" || fail "K10 token, helper says mismatch: the run stops"
  [ ! -e "$TMP/imgdata/state/ca.crt" ] && pass "K10 token, mismatch: the root is not saved" || fail "K10 token, mismatch: the root is not saved"
  lacks "K10 token, mismatch: the token was never sent to main" "$(cat "$TMP/upd.log.hdr")" "Bearer"
  # a K10 token never falls back to a fingerprint question or TOFU (a man in the middle can answer 404 on the signature only)
  out="$(k10_run 3)"; rc=$?
  [ $rc -ne 0 ] && pass "K10 token, helper cannot verify: the run stops" || fail "K10 token, helper cannot verify: the run stops"
  lacks "K10 token, cannot verify: no fingerprint question" "$out" "Отпечаток корня CA главного"
  [ ! -e "$TMP/imgdata/state/ca.crt" ] && pass "K10 token, cannot verify: nothing saved" || fail "K10 token, cannot verify: nothing saved"
  out="$(k10_run 3 --accept-ca)"; rc=$?
  [ $rc -ne 0 ] && [ ! -e "$TMP/imgdata/state/ca.crt" ] && pass "K10 token, cannot verify: --accept-ca does not help" || fail "K10 token, cannot verify: --accept-ca does not help (rc=$rc)"
  out="$(k10_run 4)"; rc=$?
  [ $rc -eq 0 ] && [ ! -e "$TMP/imgdata/state/ca.crt" ] && pass "K10 token, main with a public certificate: nothing pinned, the system roots work" || fail "K10 token, public certificate (rc=$rc)"
  k10_run 0 --ca-sha256 "$CA_FP" > /dev/null
  lacks "an explicit --ca-sha256 wins: the helper is not called" "$(cat "$TMP/upd.log")" "helper "
  out="$(CLUSTER_TOKEN=clustersecret42 FAKE_HELPER_RC=0 k10_run 0 --accept-ca)"
  : > /dev/null
fi

# --- the first pull goes by the digest main names (GET /api/node-image, field digest) ------------------------------------------------
DGX="sha256:$(printf 'ab%.0s' $(seq 32))"
printf '{"tag":"0.1.1","cpu":"nsu-llm-node:0.1.1-cpu","nvidia":"nsu-llm-node:0.1.1-gpu","digest":{"cpu":"%s","nvidia":""}}' "$DGX" > "$TMP/node-image.json"
printf 'name=nsu-llm-node:0.1.1-cpu\ngo=0\n' > "$TMP/upd.plan"
fm_run() {
  : > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"; rm -rf "$TMP/imgdata"
  env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" FAKE_PLAN="$TMP/upd.plan" FAKE_NODE_IMAGE="$TMP/node-image.json" IPT_LOG="$TMP/ipt.log" \
    bash "$UN/run-node.sh" --main main.example --token clustersecret42 --accel cpu --mode standalone --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca --no-firewall --from-main "$@" 2>&1
}
out="$(fm_run)"
contains "from-main: the first pull passes the digest main named" "$(cat "$TMP/upd.log")" "pull-image --main main.example --image nsu-llm-node:0.1.1-cpu --no-tofu --digest $DGX"
out="$(fm_run --image nsu-llm-node:0.1.1-cpu)"
lacks "from-main with --image of the admin: no digest (it names main's image, not his)" "$(cat "$TMP/upd.log")" "--digest"
printf '{"tag":"0.1.1","cpu":"nsu-llm-node:0.1.1-cpu","digest":{"cpu":""}}' > "$TMP/node-image.json"
out="$(fm_run)"
lacks "from-main: main names no digest: the pull is by tag, as before" "$(cat "$TMP/upd.log")" "--digest"

# --- fix round 1: atomic rebuild, fail closed, rules before the container and after a reboot, registry trust -----------------------------------
planp "10.1.2.7"
out="$(fwupd)"
[ "$(grep -c '^iptables-restore --noflush' "$TMP/ipt.log")" = 2 ] && pass "firewall: each chain is replaced by ONE iptables-restore transaction" || fail "firewall: one transaction per chain"
lacks "firewall: no flush of a chain that is in use (that would be a window open to all)" "$(ipt)" "iptables -w -F"
lacks "firewall: no -N/-F dance either" "$(ipt)" "iptables -w -N"
contains "firewall: the chain is declared in the transaction (old rules go with the same COMMIT)" "$(ipt)" "restore: :NSU-LLM-RPC - [0:0]"
contains "firewall: DROP and the allow list arrive at one COMMIT (DROP is the last rule, COMMIT follows)" "$(grep -A1 'restore: -A NSU-LLM-RPC -j DROP' "$TMP/ipt.log")" "restore: COMMIT"
[ "$(before "$(ipt)" 'restore: -A NSU-LLM-RPC -j DROP')" -lt "$(before "$(ipt)" 'iptables -w -I INPUT 1 -p tcp -m conntrack --ctorigdstport 18050')" ] && pass "firewall: the chain is hooked only after it holds its rules" || fail "firewall: hook after rules"
out="$(fwupd FAKE_RESTORE_FAIL=1)"; rc=$?
contains "firewall: a failed transaction is reported, the previous set stays" "$out" "правила iptables не применены (предыдущий набор остаётся)"
lacks "firewall: a failed transaction hooks nothing new" "$(ipt)" "-I INPUT"
# the fresh start: the firewall first, the container after, a set that cannot be applied stops the start
fresh_fw() { # extra env VAR=value ...
  : > "$TMP/upd.log"; : > "$TMP/upd.log.hdr"; rm -rf "$TMP/imgdata"
  env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" IPT_LOG="$TMP/upd.log" FAKE_PLAN="$TMP/upd.plan" "$@" \
    bash "$UN/run-node.sh" --main 10.9.0.5 --token clustersecret42 --accel cpu --mode k3s --node-ip 10.1.2.3 --name pc01 --data-dir "$TMP/imgdata" --accept-ca 2>&1
}
printf 'name=nsu-llm-node:0.1.1-cpu\ngo=0\nrpc_peers=10.1.2.7\n' > "$TMP/upd.plan"
out="$(fresh_fw NSU_LLM_SYSTEMD_DIR="$TMP/sysd" SYSTEMCTL_LOG="$TMP/systemctl.log")"; rc=$?
if [ $rc -eq 0 ]; then pass "fresh k3s node with a firewall: starts"; else fail "fresh k3s node with a firewall (rc=$rc)"; echo "$out"; fi
[ "$(before "$(cat "$TMP/upd.log")" 'iptables-restore --noflush')" -lt "$(before "$(cat "$TMP/upd.log")" 'docker run')" ] && pass "firewall: the rules are set BEFORE the container starts" || fail "firewall before docker run"
contains "the first set already names the group main gave" "$(cat "$TMP/upd.log")" "restore: -A NSU-LLM-RPC -s 10.1.2.7 -j RETURN"
out="$(fresh_fw NSU_LLM_SYSTEMD_DIR="$TMP/sysd" FAKE_RESTORE_FAIL=1)"; rc=$?
[ $rc -ne 0 ] && pass "firewall that cannot be applied: the node does not start" || fail "firewall that cannot be applied: the node does not start"
lacks "firewall that cannot be applied: no container" "$(cat "$TMP/upd.log")" "docker run"
contains "firewall that cannot be applied: says why" "$out" "контейнер не запущен"
# reboot: the saved set comes back before docker.service, without the network
fresh_fw NSU_LLM_SYSTEMD_DIR="$TMP/sysd" SYSTEMCTL_LOG="$TMP/systemctl.log" > /dev/null
contains "state/fw.conf keeps the applied set" "$(cat "$TMP/imgdata/state/fw.conf" 2>/dev/null)" "RPC=10.9.0.5,10.1.2.7"
[ -f "$TMP/sysd/nsu-llm-node-firewall.service" ] && pass "a unit restores the rules at boot" || fail "a unit restores the rules at boot"
contains "the boot unit runs BEFORE docker.service" "$(cat "$TMP/sysd/nsu-llm-node-firewall.service" 2>/dev/null)" "Before=docker.service"
contains "the boot unit calls --restore-firewall for this data dir" "$(cat "$TMP/sysd/nsu-llm-node-firewall.service" 2>/dev/null)" "--restore-firewall --data-dir $TMP/imgdata"
contains "a timer re-syncs the group every minute" "$(cat "$TMP/sysd/nsu-llm-node-sync.timer" 2>/dev/null)" "OnUnitActiveSec=1min"
contains "the timer service runs --sync-firewall" "$(cat "$TMP/sysd/nsu-llm-node-sync.service" 2>/dev/null)" "--sync-firewall"
contains "the units are enabled" "$(cat "$TMP/systemctl.log" 2>/dev/null)" "systemctl enable nsu-llm-node-firewall.service nsu-llm-node-sync.timer"
: > "$TMP/ipt.log"; : > "$TMP/upd.log"
out="$(env PATH="$TMP/updbin:$PATH" DOCKER_LOG="$TMP/upd.log" IPT_LOG="$TMP/ipt.log" bash "$UN/run-node.sh" --restore-firewall --data-dir "$TMP/imgdata" 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass "--restore-firewall works without a token, main, docker or network" || { fail "--restore-firewall (rc=$rc)"; echo "$out"; }
contains "--restore-firewall brings the saved RPC set back" "$(cat "$TMP/ipt.log")" "restore: -A NSU-LLM-RPC -s 10.1.2.7 -j RETURN"
lacks "--restore-firewall does not touch docker or curl" "$(cat "$TMP/upd.log")" "docker"
out="$(bash "$UN/run-node.sh" --restore-firewall --data-dir "$TMP/nonexistent" 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass "--restore-firewall with nothing saved is a no-op" || fail "--restore-firewall with nothing saved"
# --sync-firewall: once, from the container, no update
planp "10.1.2.9"
out="$(UPD_ARGS=--sync-firewall fwupd)"; rc=$?
contains "--sync-firewall puts the group of main into the rules" "$(ipt)" "restore: -A NSU-LLM-RPC -s 10.1.2.9 -j RETURN"
lacks "--sync-firewall never pulls or replaces the container" "$(updlog)" "pull-image"
printf 'go=1\nimage=nsu-llm-node:0.2.0-cpu\ndigest=%s\nmode=requested\nwhen=now\nreason=\nrpc_peers=10.1.2.9\n' "$NEW_DIGEST" > "$TMP/upd.plan"
out="$(UPD_ARGS=--sync-firewall fwupd)"
lacks "--sync-firewall ignores 'go': no pull even when an update is due" "$(updlog)" "pull-image"
# uninstall takes the units away
mkdir -p "$TMP/unidata3/state"
out="$(env PATH="$TMP/updbin:$PATH" NSU_LLM_SYSTEMD_DIR="$TMP/sysd" SYSTEMCTL_LOG="$TMP/systemctl.log" DOCKER_LOG="$TMP/upd.log" IPT_LOG="$TMP/ipt.log" FAKE_CONTAINER=1 FAKE_ENV="$TMP/upd2.env" FAKE_STATE_SRC="$TMP/unidata3/state" \
  bash "$UN/run-node.sh" --uninstall --main 10.9.0.5 --name pc01 --data-dir "$TMP/unidata3" --insecure-tls 2>&1)"
[ ! -e "$TMP/sysd/nsu-llm-node-firewall.service" ] && [ ! -e "$TMP/sysd/nsu-llm-node-sync.timer" ] && pass "uninstall removes the systemd units" || fail "uninstall removes the systemd units"
# registry trust: no pinned root and the system roots reject the certificate: pull-image.sh must not be called (it would take /ca.crt with -k)
rm -f "$TMP/upddata/state/ca.crt"
printf 'go=1\nimage=nsu-llm-node:0.2.0-cpu\ndigest=%s\nmode=requested\nwhen=now\nreason=\n' "$NEW_DIGEST" > "$TMP/upd.plan"
out="$(UPD_ARGS=--no-firewall upd FAKE_V2_CODE=000)"; rc=$?
[ $rc -ne 0 ] && pass "update without a pinned root, system roots reject main: refused" || fail "update without a pinned root, system roots reject main"
lacks "...and pull-image.sh is never called (no silent TOFU)" "$(updlog)" "pull-image"
contains "...with the reason" "$out" "принял бы чужой корень без проверки"
out="$(UPD_ARGS=--no-firewall upd FAKE_V2_CODE=200)"
contains "update without a pinned root but with a trusted public certificate: pulls" "$(updlog)" "pull-image --main main.example"
echo CERT > "$TMP/upddata/state/ca.crt"

# --- run-node.ps1: the CA fingerprint, the data dir of Docker Desktop, the node secret, -SyncFirewall ------------------------------
if [ -n "$PS" ]; then
  RNW="$(cygpath -w "$NODE/run-node.ps1" 2>/dev/null || echo "$NODE/run-node.ps1")"
  # Docker Desktop: the data dir is created for SYSTEM and Administrators only, never inherited from ProgramData
  DD="$TMP/ddnode"; rm -rf "$DD"
  out="$(NSU_LLM_DATA_DIR="$(cygpath -w "$DD" 2>/dev/null || echo "$DD")" psnode -Mode k3s)"
  contains "ps1 Docker Desktop: the data dir is secured before the first write" "$out" "[dry-run] создать каталог"
  contains "ps1 Docker Desktop: SYSTEM and Administrators only (no Users)" "$out" "Users без доступа"
  contains "ps1 Docker Desktop: foreign archives leave airgap" "$out" "удалить из"
  out="$(psnode -Mode k3s -Wsl NSU-LLM)"
  contains "ps1 WSL: the data dir is root 0700" "$out" "chmod 700 '/var/lib/nsu-llm-node'"
  contains "ps1 WSL mirrored: the RPC port is closed for the loopback of Windows (it arrives on loopback0)" "$out" "iptables -w -I INPUT 1 -i loopback0 -p tcp -m conntrack --ctorigdstport 18050 --ctdir ORIGINAL -j DROP"
  contains "ps1 WSL mirrored: also for what a hostPort pod receives through DNAT" "$out" "iptables -w -I FORWARD 1 -i loopback0"
  out="$(psnode -Mode standalone -Wsl NSU-LLM)"
  lacks "ps1 WSL standalone: no RPC, no loopback0 rule is added" "$out" "-I INPUT 1 -i loopback0"
  out="$(psnode -Mode k3s)"
  lacks "ps1 Docker Desktop: no WSL rule" "$out" "loopback0"
  # the CA check is a function of run-node.ps1: take it out with the parser and run it with a fingerprint
  CAT="$TMP/ca-check.ps1"
  cat > "$CAT" <<'EOS2'
param([string]$Script, [string]$Mode = 'none')
$ErrorActionPreference = 'Stop'
$t = $null; $e = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$t, [ref]$e)
$MainUrl = 'https://main.example'
foreach ($n in 'Get-PemSha256', 'Confirm-CaFingerprint') {
    $f = $ast.Find({ param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true)
    . ([scriptblock]::Create($f.Extent.Text))
}
# a throw-away root certificate made by .NET: its hash is taken from the certificate object, the script's from the PEM text
$rsa = [Security.Cryptography.RSA]::Create(2048)
$req = New-Object Security.Cryptography.X509Certificates.CertificateRequest('CN=test-main', $rsa, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
$cert = $req.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddDays(2))
$pem = "-----BEGIN CERTIFICATE-----`n" + [Convert]::ToBase64String($cert.RawData, 'InsertLineBreaks') + "`n-----END CERTIFICATE-----`n"
$want = (([Security.Cryptography.SHA256]::Create().ComputeHash($cert.RawData) | ForEach-Object { $_.ToString('x2') }) -join '')
$fp = Get-PemSha256 $pem
"fp-matches-der=$($fp -eq $want)"
$CaSha256 = ''; $AcceptCa = [switch]$false
if ($Mode -eq 'right') { $CaSha256 = $want }
if ($Mode -eq 'wrong') { $CaSha256 = ('0' * 64) }
if ($Mode -eq 'accept') { $AcceptCa = [switch]$true }
try { "accepted=$(Confirm-CaFingerprint $fp)" } catch { "error=$($_.Exception.Message)" }
EOS2
  capsh() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$CAT")" -Script "$RNW" -Mode "$1" 2>&1 < /dev/null; }
  out="$(capsh none)"
  contains "ps1 CA: the fingerprint is the SHA-256 of the DER of the certificate" "$out" "fp-matches-der=True"
  contains "ps1 CA: without a fingerprint and nobody to ask the root is not accepted" "$out" "не подтверждён"
  out="$(capsh right)"
  contains "ps1 CA: the right fingerprint accepts the root" "$out" "accepted=True"
  out="$(capsh wrong)"
  contains "ps1 CA: a wrong fingerprint is a spoof" "$out" "не совпал с -CaSha256"
  lacks "ps1 CA: a wrong fingerprint accepts nothing" "$out" "accepted=True"
  out="$(capsh accept)"
  contains "ps1 CA: -AcceptCa takes the root" "$out" "accepted=True"
  # the root of main by the join token: hash of /cacerts in K10<H>, the signature of /ca.crt by the CA of k3s (Test-MainCaByToken, secure-dir.ps1)
  CMT="$TMP/ca-token.ps1"
  cat > "$CMT" <<'EOS2'
param([string]$Lib, [string]$Mode)
$ErrorActionPreference = 'Stop'
. $Lib
Add-Type -AssemblyName System.Security
function New-TestCert([string]$cn) {
    $rsa = [Security.Cryptography.RSA]::Create(2048)
    $req = New-Object Security.Cryptography.X509Certificates.CertificateRequest("CN=$cn", $rsa, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $c = $req.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddDays(2))
    return (New-Object Security.Cryptography.X509Certificates.X509Certificate2($c.Export('Pfx', 'x'), 'x', 'Exportable'))
}
function ToPem($c) { return "-----BEGIN CERTIFICATE-----`n" + [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks') + "`n-----END CERTIFICATE-----`n" }
$k3s = New-TestCert 'k3s-server-ca'; $other = New-TestCert 'other-ca'; $root = New-TestCert 'caddy-root'
$caBytes = [Text.Encoding]::UTF8.GetBytes((ToPem $root)); $cacerts = [Text.Encoding]::UTF8.GetBytes((ToPem $k3s))
function Sign($bytes, $cert) {
    $cms = New-Object Security.Cryptography.Pkcs.SignedCms((New-Object Security.Cryptography.Pkcs.ContentInfo(,$bytes)), $true)
    $cms.ComputeSignature((New-Object Security.Cryptography.Pkcs.CmsSigner($cert)))
    return $cms.Encode()
}
$H = ((([Security.Cryptography.SHA256]::Create().ComputeHash($cacerts)) | ForEach-Object { $_.ToString('x2') }) -join '')
$signer = $(if ($Mode -eq 'othersigner') { $other } else { $k3s })
$p7s = Sign $caBytes $signer
if ($Mode -eq 'tampered') { $caBytes = [Text.Encoding]::UTF8.GetBytes((ToPem $other)) }   # /ca.crt differs from what was signed
$token = "K10$H::node:secret"
if ($Mode -eq 'wronghash') { $token = 'K10' + ('0' * 64) + '::node:secret' }
if ($Mode -eq 'rawtoken') { $token = 'secret-agent-token' }
$fetch = {
    param($url)
    if ($url -like '*/ca.crt.p7s') { if ($Mode -eq 'nosig') { return [pscustomobject]@{ Code = '404'; Bytes = [byte[]]@() } }; return [pscustomobject]@{ Code = '200'; Bytes = $p7s } }
    if ($url -like '*/ca.crt') { if ($Mode -eq 'public') { return [pscustomobject]@{ Code = '404'; Bytes = [byte[]]@() } }; if ($Mode -eq 'down') { return [pscustomobject]@{ Code = '000'; Bytes = [byte[]]@() } }; return [pscustomobject]@{ Code = '200'; Bytes = $caBytes } }
    if ($url -like '*:6443/cacerts') { return [pscustomobject]@{ Code = '200'; Bytes = $cacerts } }
    return [pscustomobject]@{ Code = '404'; Bytes = [byte[]]@() }
}
$r = Test-MainCaByToken 'main.example' $token 'curl.exe' $fetch
"status=$($r.Status) pem=$([bool]$r.Pem) fp-ok=$($r.Fp -eq (Get-CaPemSha256 ([Text.Encoding]::UTF8.GetString($caBytes)))) why=$($r.Why)"
EOS2
  LIBW="$(cygpath -w "$NODE/secure-dir.ps1" 2>/dev/null || echo "$NODE/secure-dir.ps1")"
  cat_mode() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$CMT")" -Lib "$LIBW" -Mode "$1" 2>&1 < /dev/null; }
  out="$(cat_mode good)";        contains "ps1 K10: root signed by the CA of k3s and hash right: verified" "$out" "status=verified pem=True fp-ok=True"
  out="$(cat_mode wronghash)";   contains "ps1 K10: /cacerts does not match the hash in the token: mismatch, nothing returned" "$out" "status=mismatch pem=False"
  out="$(cat_mode tampered)";    contains "ps1 K10: /ca.crt is not what was signed: mismatch" "$out" "status=mismatch pem=False"
  out="$(cat_mode othersigner)"; contains "ps1 K10: signed by another certificate than the CA of k3s: mismatch (a chain would not be enough)" "$out" "status=mismatch pem=False"
  out="$(cat_mode nosig)";       contains "ps1 K10: a K10 token and no /ca.crt.p7s is a hard failure (a man in the middle can answer 404 on the signature only), never a fingerprint question" "$out" "status=mismatch pem=False"
  out="$(cat_mode rawtoken)";    contains "ps1 K10: a raw token has no hash: unverifiable" "$out" "status=unverifiable pem=True"
  out="$(cat_mode public)";      contains "ps1 K10: main with a public certificate serves no /ca.crt: public" "$out" "status=public"
  out="$(cat_mode down)";        contains "ps1 K10: main does not answer: unreachable" "$out" "status=unreachable"
  # Docker Desktop: its own program rule allows any address on the ports it publishes; a Block rule (stronger than Allow) for everyone else
  out="$(psnode -Mode k3s -RpcPeers 192.168.3.7)"
  contains "ps1 Docker Desktop: a Block rule for 18080 for everybody except main (the program rule of Docker Desktop cannot override it)" "$out" "New-NetFirewallRule -DisplayName NSU-LLM-Node-Block -Direction Inbound -Protocol TCP -LocalPort 18080 -RemoteAddress 0.0.0.0-126.255.255.255,128.0.0.0-192.168.3.20,192.168.3.22-255.255.255.255 -Action Block"
  contains "ps1 Docker Desktop: and for 18050 for everybody except main and the group" "$out" "-DisplayName NSU-LLM-Node-RPC-Block -Direction Inbound -Protocol TCP -LocalPort 18050 -RemoteAddress 0.0.0.0-126.255.255.255,128.0.0.0-192.168.3.6,192.168.3.8-192.168.3.20,192.168.3.22-255.255.255.255 -Action Block"
  out="$(psnode -Mode standalone)"
  lacks "ps1 Docker Desktop standalone: no RPC Block rule is created" "$out" "-DisplayName NSU-LLM-Node-RPC-Block -Direction"
  contains "ps1 Docker Desktop standalone: the stale RPC Block rule is removed" "$out" "Remove-NetFirewallRule -DisplayName NSU-LLM-Node-RPC-Block"
  out="$(psnode -Mode k3s -Wsl NSU-LLM)"
  lacks "ps1 WSL: no Windows Block rule (the Hyper-V firewall guards it)" "$out" "-Action Block"
  contains "ps1 WSL: DefaultInboundAction of the Hyper-V firewall is checked and set to Block" "$out" "Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Block"
  lacks "ps1 WSL: a failure of the loopback0 rule is not swallowed (no '; true' after the rules)" "$out" "DROP; true"
  # the complement of the allowed addresses (Get-ComplementRanges) is exact
  CRT="$TMP/ranges.ps1"
  cat > "$CRT" <<'EOS2'
param([string]$Script, [string]$List)
$t = $null; $e = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$t, [ref]$e)
foreach ($n in 'ConvertTo-IpNum', 'ConvertFrom-IpNum', 'Get-ComplementRanges') {
    $f = $ast.Find({ param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true)
    . ([scriptblock]::Create($f.Extent.Text))
}
Get-ComplementRanges $List
EOS2
  ranges() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$CRT")" -Script "$RNW" -List "$1" 2>&1 < /dev/null; }
  contains "ps1 ranges: a subnet is carved out of the whole IPv4 space" "$(ranges '10.0.0.0/8,192.168.3.21')" "0.0.0.0-9.255.255.255,11.0.0.0-126.255.255.255,128.0.0.0-192.168.3.20,192.168.3.22-255.255.255.255"
  contains "ps1 ranges: adjacent addresses merge" "$(ranges '192.168.3.21,192.168.3.22')" "128.0.0.0-192.168.3.20,192.168.3.23-255.255.255.255"
  contains "ps1 ranges: the top of the space is covered" "$(ranges '255.255.255.255')" "128.0.0.0-255.255.255.254"
  contains "ps1 ranges: loopback is never in the Block rule" "$(ranges '10.0.0.1')" "0.0.0.0-10.0.0.0,10.0.0.2-126.255.255.255,128.0.0.0-255.255.255.255"
  # the data dir of Docker Desktop is opened exactly for the user who runs it (Administrators alone is deny-only for a non-elevated admin)
  SDX="$TMP/sdx.ps1"
  cat > "$SDX" <<'EOS2'
param([string]$Lib)
. $Lib
$sec = New-SdDirSecurity -NoUsers -NoOwner -ExtraSids @('S-1-5-21-1-2-3-1001')
$ids = @($sec.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value }) | Sort-Object
"sids=$($ids -join ',')"
"trusted-before=$(Test-SdTrustedSid 'S-1-5-21-1-2-3-1001')"
$global:SdExtraSids = @('S-1-5-21-1-2-3-1001')
"trusted-after=$(Test-SdTrustedSid 'S-1-5-21-1-2-3-1001')"
"stranger=$(Test-SdTrustedSid 'S-1-5-21-1-2-3-1002')"
EOS2
  out="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$SDX")" -Lib "$LIBW" 2>&1 < /dev/null)"
  contains "ps1 Secure-Dir: the Docker Desktop user gets an ACE of his own next to SYSTEM and Administrators, Users get none" "$out" "sids=S-1-5-18,S-1-5-21-1-2-3-1001,S-1-5-32-544"
  contains "ps1 Secure-Dir: that user is not trusted unless the installer named him" "$out" "trusted-before=False"
  contains "ps1 Secure-Dir: once named he is trusted (the dir is not moved aside by the next run)" "$out" "trusted-after=True"
  contains "ps1 Secure-Dir: another account is not" "$out" "stranger=False"
  out="$(psnode -Mode k3s -DockerUserSid 'S-1-5-21-1-2-3-1001')"
  contains "ps1 the data dir plan names the Docker Desktop user" "$out" "полный доступ S-1-5-21-1-2-3-1001"
  out="$(psnode -Mode k3s -DockerUserSid 'junk;calc')"; rc=$?
  [ $rc -ne 0 ] && pass "ps1 -DockerUserSid with junk fails" || fail "ps1 -DockerUserSid with junk fails"
  # -SyncFirewall: main names the group, the rules follow it
  mkdir -p "$TMP/syncbin"
  printf '@echo off\r\necho {"name":"nsu-llm-node:0.2.0-cpu","rpc_peers":["10.1.1.7","evil","10.1.1.8","10.1.1.7"]}\r\necho 200\r\n' > "$TMP/syncbin/curl-peers.bat"
  printf '@echo off\r\necho {"name":"nsu-llm-node:0.2.0-cpu"}\r\necho 200\r\n' > "$TMP/syncbin/curl-nopeers.bat"
  printf '@echo off\r\nexit /b 7\r\n' > "$TMP/syncbin/curl-down.bat"
  syncps() { NSU_LLM_CURL="$(cygpath -w "$TMP/syncbin/$1")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RNW" -SyncFirewall -DryRun -Main 10.9.0.5 -Name pc01 -Token T -Mode k3s 2>&1 < /dev/null; }
  out="$(syncps curl-peers.bat)"
  contains "ps1 sync: the RPC rule follows the group main names" "$out" "-LocalPort 18050 -RemoteAddress 10.9.0.5,10.1.1.7,10.1.1.8 -Action Allow"
  contains "ps1 sync: the inference rule stays main-only" "$out" "-LocalPort 18080 -RemoteAddress 10.9.0.5 -Action Allow"
  lacks "ps1 sync: a junk peer is dropped" "$out" "evil"
  out="$(syncps curl-nopeers.bat)"
  contains "ps1 sync: an older main without rpc_peers leaves the rules alone" "$out" "не назвал rpc_peers"
  lacks "ps1 sync: no rule is touched then" "$out" "New-NetFirewallRule"
  out="$(syncps curl-down.bat)"
  contains "ps1 sync: main down: the rules stay" "$out" "правила брандмауэра не менялись"
  lacks "ps1 sync: main down: nothing is rewritten" "$out" "New-NetFirewallRule"
  # without parameters the answers of install-node are used
  printf '{"Main":"10.9.0.5","Name":"pc07","Mode":"k3s","AllowFrom":"","Docker":"desktop"}' > "$TMP/install.json"
  out="$(NSU_LLM_INSTALL_JSON="$(cygpath -w "$TMP/install.json")" NSU_LLM_CURL="$(cygpath -w "$TMP/syncbin/curl-peers.bat")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RNW" -SyncFirewall -DryRun -Token T 2>&1 < /dev/null)"
  contains "ps1 sync: main and mode come from install.json" "$out" "-LocalPort 18050 -RemoteAddress 10.9.0.5,10.1.1.7,10.1.1.8"
  out="$(NSU_LLM_INSTALL_JSON="$(cygpath -w "$TMP/install.json")" NSU_LLM_CURL="$(cygpath -w "$TMP/syncbin/curl-peers.bat")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$RNW" -SyncFirewall -Update -DryRun -Token T 2>&1 < /dev/null)"
  contains "ps1 -SyncFirewall excludes -Update" "$out" "не сочетается"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
