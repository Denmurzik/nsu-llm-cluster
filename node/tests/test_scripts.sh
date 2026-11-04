#!/usr/bin/env bash
# Offline tests for node scripts (no docker, no network).
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
BIN="$NODE/rootfs/opt/llm/bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
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
printf '#!/usr/bin/env bash
echo "k3s $*"
echo "K3S_TOKEN=${K3S_TOKEN-unset} CLUSTER_TOKEN=${CLUSTER_TOKEN-unset}"
' > "$TMP/fakebin/k3s"
chmod +x "$TMP/fakebin/k3s"
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
out="$(PATH="$TMP/wolbin:$TMP/fakebin:$BIN:$PATH" FAKE_REG_LOG="$TMP/reg-bg.log" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu \
  WOL_MAC=00-1A-2B-3C-4D-5E WOL_IP=10.1.2.3 WOL_PREFIX=24 bash "$BIN/entrypoint.sh" 2>&1)"
sleep 1
contains "entrypoint k3s reports the NIC in the background" "$(cat "$TMP/reg-bg.log")" "registrar --wake-report CLUSTER_TOKEN=tok WOL_MAC=00-1A-2B-3C-4D-5E"
contains "entrypoint k3s still runs k3s" "$out" "k3s agent --server https://main.example:6443"
: > "$TMP/reg-bg.log"
PATH="$TMP/wolbin:$TMP/fakebin:$BIN:$PATH" FAKE_REG_LOG="$TMP/reg-bg.log" MODE=k3s MAIN_HOST=main.example CLUSTER_TOKEN=tok NODE_NAME=foo ACCEL=cpu \
  bash "$BIN/entrypoint.sh" >/dev/null 2>&1
sleep 1
[ ! -s "$TMP/reg-bg.log" ] && pass "entrypoint k3s without a NIC reports nothing" || fail "entrypoint k3s without a NIC reports nothing"
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
contains "run docker save" "$out" "docker save --platform linux/amd64 nsu-llm-node:0.1.0-cpu"
contains "run docker run" "$out" "docker run -d --name llm-node"
contains "run -p 18080" "$out" "-p 18080:18080"
contains "run -p 18050" "$out" "-p 18050:18050"
contains "run privileged" "$out" "--privileged"
contains "run hostname" "$out" "--hostname test-node"
contains "run image" "$out" "nsu-llm-node:0.1.0-cpu"
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

out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel nvidia --network host 2>&1)"
contains "run nvidia image" "$out" "nsu-llm-node:0.1.0-gpu"
contains "run nvidia gpus" "$out" "--gpus all"
contains "run host network" "$out" "--network host"
lacks "run host no -p" "$out" "-p 18080:18080"

out="$(bash "$RN" --dry-run --main 192.168.3.21 --node-ip 192.168.3.200 --name n1 --token T --accel cpu --mode standalone 2>&1)"
contains "run standalone MODE" "$out" "MODE=standalone"
lacks "run standalone no save" "$out" "docker save"
contains "run standalone publishes inference" "$out" "-p 18080:18080"
lacks "run standalone does not publish unauthenticated RPC" "$out" "18050"

# run-node.ps1 follows run-node.sh: 18050 (RPC) only in k3s mode, in the publish list and in the firewall rule
PS="$(command -v powershell || command -v pwsh || true)"
if [ -n "$PS" ]; then
  psnode() { "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$NODE/run-node.ps1" 2>/dev/null || echo "$NODE/run-node.ps1")" \
    -DryRun -Main 192.168.3.21 -Token T -NodeIp 192.168.3.5 -Name n1 -Accel cpu "$@" 2>&1; }
  out="$(psnode -Mode k3s)"
  contains "ps1 token passed through the environment" "$out" "-e CLUSTER_TOKEN "
  lacks "ps1 token not on the docker command line" "$out" "CLUSTER_TOKEN=T"
  contains "ps1 k3s publishes 18050" "$out" "18050:18050"
  contains "ps1 k3s firewall opens 18050" "$out" "-LocalPort 18080,18050"
  out="$(psnode -Mode standalone)"
  contains "ps1 standalone publishes inference" "$out" "18080:18080"
  lacks "ps1 standalone does not publish RPC" "$out" "18050:18050"
  lacks "ps1 standalone firewall leaves 18050 closed" "$out" "18050"
  contains "ps1 firewall rule is recreated with the current ports" "$out" "Remove-NetFirewallRule -DisplayName NSU-LLM-Node"
  lacks "ps1 firewall without -AllowFrom has no address filter" "$out" "-RemoteAddress"
  # -AllowFrom: the rule admits only the class and the main; anything but IPv4/CIDR lists is refused
  out="$(psnode -AllowFrom '10.1.2.0/24, 10.9.0.5')"
  contains "ps1 AllowFrom limits the firewall rule" "$out" "-LocalPort 18080,18050 -RemoteAddress 10.1.2.0/24,10.9.0.5 -Action Allow"
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
  out="$(psun -Network host -Image nsu-llm-node:0.1.0-cpu)"
  contains "ps1 host cleanup runs" "$out" "--entrypoint bash nsu-llm-node:0.1.0-cpu -c"
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
  contains "ps1 gpu probe runs the local gpu image" "$log" "run --rm --pull never --gpus all --entrypoint nvidia-smi nsu-llm-node:0.1.0-gpu"
  lacks "ps1 gpu probe pulls nothing from Docker Hub" "$log" "nvidia/cuda"
  contains "ps1 gpu probe success selects the gpu image" "$log" "save --platform linux/amd64 nsu-llm-node:0.1.0-gpu"
fi

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
    */api/nodes/*) printf '{"ok":true,"heartbeat_interval":15,"desired":%s,"wake":%s}\n%s' "${FAKE_DESIRED:-null}" "${FAKE_WAKE:-[]}" "${FAKE_API_CODE:-200}"; exit 0 ;;
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
out="$(FAKE_CURL_LOG="$TMP/curl.log" PATH="$TMP/regbin:$BIN:$PATH" CLUSTER_TOKEN=t MAIN_HOST=main.example STATE_DIR="$TMP/regstate" timeout 5 bash "$BIN/registrar.sh" --wake-report 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass "wake report without a NIC exits quietly" || fail "wake report without a NIC exits quietly (rc=$rc)"
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

out="$(FAKE_INSPECT=nsu-llm-node:0.1.0-gpu FAKE_IMAGES="nsu-llm-node:0.1.0-cpu" unin --network host)"
contains "uninstall host chains" "$out" "KUBE-"
contains "uninstall host links" "$out" "flannel.1"
contains "uninstall host image from container" "$out" "--entrypoint bash nsu-llm-node:0.1.0-gpu"
contains "uninstall host never pulls" "$out" "--pull never"
out="$(FAKE_INSPECT=nsu-llm-node:0.1.0-gpu unin --network host --image my/img:1)"
contains "uninstall host --image wins" "$out" "--entrypoint bash my/img:1"
lacks "uninstall host --image wins (no gpu)" "$out" "nsu-llm-node:0.1.0-gpu"
out="$(FAKE_INSPECT= FAKE_IMAGES="other:1
nsu-llm-node:0.1.0-gpu
nsu-llm-node:0.1.0-cpu" unin --network host)"
contains "uninstall host image from local images" "$out" "--entrypoint bash nsu-llm-node:0.1.0-gpu"
out="$(FAKE_INSPECT= FAKE_IMAGES="other:1" unin --network host)"
contains "uninstall host without image warns" "$out" "Предупреждение: образ ноды не найден локально"
lacks "uninstall host without image skips cleanup" "$out" "--entrypoint"
contains "uninstall host without image still removes container" "$out" "docker rm -f llm-node"
out="$(FAKE_INSPECT=nsu-llm-node:0.1.0-gpu unin)"
lacks "uninstall bridge has no host cleanup" "$out" "--entrypoint"

# a data dir with root-owned files (rm without root fails) is removed through the node image
mkdir -p "$TMP/rootbin" "$TMP/rootdata/state"
cat > "$TMP/rootbin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
case "$1" in inspect) echo nsu-llm-node:0.1.0-cpu; exit 0 ;; esac
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
contains "uninstall removes a root-owned data dir through the node image" "$log" "run --rm --pull never -v $TMP:/p --entrypoint rm nsu-llm-node:0.1.0-cpu -rf /p/rootdata"
contains "uninstall warns when the data dir is still there" "$out" "sudo rm -rf $TMP/rootdata"
rm -rf "$TMP/rootdata"

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
unreal() { FAKE_CODE="$1" PATH="$TMP/delbin:$TMP/dockerbin:$PATH" bash "$RN" --uninstall --main 192.168.3.21 --name test-node \
  --token T --insecure-tls --data-dir "$TMP/undata" 2>&1; }
out="$(unreal 401)"
contains "uninstall warns when DELETE fails" "$out" "Предупреждение: DELETE https://192.168.3.21/api/nodes/test-node не выполнен (ответ: 401)"
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

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
