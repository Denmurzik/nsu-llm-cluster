#!/usr/bin/env bash
# Starts llama-server for one model. Configuration comes from the environment (see node/README.md).
#   serve-llama.sh              exec llama-server
#   serve-llama.sh --print-cmd  print the command (and the shard download list) instead of running
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRINT=0
[ "${1:-}" = "--print-cmd" ] && PRINT=1

: "${MODEL_REPO:?MODEL_REPO is required}"
: "${MODEL_FILE:?MODEL_FILE is required}"
HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
HF_ENDPOINT="${HF_ENDPOINT%/}"
LLAMA_CACHE="${LLAMA_CACHE:-/var/lib/llm-models}"
# The Hugging Face commit the deployment is pinned to (the portal resolves it when the model is chosen, task 2-15):
# every file comes from resolve/<sha>, so a quant uploaded again upstream changes nothing until the admin updates the
# deployment. Empty: main (deployments made before the pin). Own model files (local/*) have no commit to pin.
MODEL_REVISION="${MODEL_REVISION:-}"
case "$MODEL_REPO" in local/*) MODEL_REVISION="" ;; esac
if [ -n "$MODEL_REVISION" ] && ! [[ "$MODEL_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "MODEL_REVISION must be a 40-character commit sha (got '$MODEL_REVISION')" >&2
  exit 2
fi
# The mirror of main (https://<main>/hf) has a certificate of main's own CA: the pods get the system CAs plus it from
# the ConfigMap llm-ca. llama-server (OpenSSL) reads SSL_CERT_FILE, curl CURL_CA_BUNDLE. Without the file the system
# CAs are used.
CA_BUNDLE="${LLM_CA_BUNDLE:-/etc/llm-ca/bundle.crt}"
if [ -s "$CA_BUNDLE" ]; then
  export SSL_CERT_FILE="$CA_BUNDLE" CURL_CA_BUNDLE="$CA_BUNDLE"
fi
# A standalone node gets the mirror from the portal and keeps its own endpoint and token as HF_FALLBACK_*: while the
# mirror does not answer, the model comes from there directly (the mirror key is no use there).
if [ -n "${HF_FALLBACK_ENDPOINT:-}" ] && ! curl -sf --connect-timeout 5 -m 15 -o /dev/null "$HF_ENDPOINT/_mirror/health"; then
  echo "HF mirror $HF_ENDPOINT is unreachable, downloading from $HF_FALLBACK_ENDPOINT" >&2
  HF_ENDPOINT="${HF_FALLBACK_ENDPOINT%/}"
  if [ -n "${HF_FALLBACK_TOKEN:-}" ]; then export HF_TOKEN="$HF_FALLBACK_TOKEN"; else unset HF_TOKEN; fi
fi
unset HF_FALLBACK_ENDPOINT HF_FALLBACK_TOKEN
export HF_ENDPOINT
INFER_PORT="${INFER_PORT:-18080}"
CTX_SIZE="${CTX_SIZE:-8192}"
PARALLEL_SLOTS="${PARALLEL_SLOTS:-1}"
# One core is left free: llama.cpp threads spin-wait, so a single busy core stalls all of them.
THREADS="${THREADS:-$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))}"
THINKING="${THINKING:-off}" # on | off | auto
case "$THINKING" in
  on|off|auto) ;;
  *) echo "THINKING must be on, off or auto (got '$THINKING')" >&2; exit 2 ;;
esac
PUBLIC_MODEL_NAME="${PUBLIC_MODEL_NAME:-${MODEL_FILE%.gguf}}"
RPC_SERVERS="${RPC_SERVERS:-}"

# Settings of the deployment (task 2-22b), all optional: the portal sets only those that differ from the defaults, so a
# deployment made before them keeps its pod template. They are checked here again, the node does not trust the portal
# blindly.
KV_CACHE_TYPE="${KV_CACHE_TYPE:-f16}" # f16 | q8_0 | q4_0, for K and V alike
case "$KV_CACHE_TYPE" in
  f16|q8_0|q4_0) ;;
  *) echo "KV_CACHE_TYPE must be f16, q8_0 or q4_0 (got '$KV_CACHE_TYPE')" >&2; exit 2 ;;
esac
# SAMPLING_ARGS: server-side defaults "--temp 0.7 --top-p 0.9 ..." (a request may send its own values)
SAMPLING_ARGS="${SAMPLING_ARGS:-}"
sampling=()
if [ -n "$SAMPLING_ARGS" ]; then
  read -ra _s <<<"$SAMPLING_ARGS"
  if [ $(( ${#_s[@]} % 2 )) -ne 0 ]; then echo "SAMPLING_ARGS must be flag/value pairs (got '$SAMPLING_ARGS')" >&2; exit 2; fi
  for ((i = 0; i < ${#_s[@]}; i += 2)); do
    case "${_s[$i]}" in
      --temp|--top-p|--top-k|--min-p|--repeat-penalty) ;;
      *) echo "SAMPLING_ARGS: ${_s[$i]} is not a sampling flag" >&2; exit 2 ;;
    esac
    if ! [[ "${_s[$((i + 1))]}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      echo "SAMPLING_ARGS: ${_s[$i]} needs a number (got '${_s[$((i + 1))]}')" >&2
      exit 2
    fi
    sampling+=("${_s[$i]}" "${_s[$((i + 1))]}")
  done
fi
DRAFT_REPO="${DRAFT_REPO:-}"
DRAFT_FILE="${DRAFT_FILE:-}"
DRAFT_REVISION="${DRAFT_REVISION:-}"
case "$DRAFT_REPO" in local/*) DRAFT_REVISION="" ;; esac
if [ -n "$DRAFT_REPO$DRAFT_FILE" ] && { [ -z "$DRAFT_REPO" ] || [ -z "$DRAFT_FILE" ]; }; then
  echo "DRAFT_REPO and DRAFT_FILE go together" >&2
  exit 2
fi
if [ -n "$DRAFT_REVISION" ] && ! [[ "$DRAFT_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "DRAFT_REVISION must be a 40-character commit sha (got '$DRAFT_REVISION')" >&2
  exit 2
fi
# MMPROJ_FILE: a file of the model's own repository, or local/<model>/<file> of an own model
MMPROJ_FILE="${MMPROJ_FILE:-}"
MMPROJ_REPO=""
MMPROJ_REV=""
if [ -n "$MMPROJ_FILE" ]; then
  if [[ "$MMPROJ_FILE" =~ ^local/([^/]+)/(.+)$ ]]; then
    MMPROJ_REPO="local/${BASH_REMATCH[1]}"
    MMPROJ_FILE="${BASH_REMATCH[2]}"
    MMPROJ_REV="main"
  else
    MMPROJ_REPO="$MODEL_REPO"
    MMPROJ_REV="${MODEL_REVISION:-main}"
  fi
fi

# The admin's own flags (EXTRA_ARGS, one per line: the portal split them with shlex): checked with the portal's rules
# (portal/app/deployments/llama_args.py) against the same forbidden list, which the image keeps in
# /opt/llm/forbidden-flags (generated from FORBIDDEN_FLAGS, a portal test keeps the two equal). A flag the portal or
# the node sets itself, a path, too many tokens or a missing list stops the server before it starts.
EXTRA_ARGS="${EXTRA_ARGS:-}"
extra=()
if [ -n "$EXTRA_ARGS" ]; then
  FORBIDDEN_FILE="${FORBIDDEN_FLAGS_FILE:-$HERE/../forbidden-flags}"
  CR=$'\r' BS='\'
  if [ ! -s "$FORBIDDEN_FILE" ]; then
    echo "EXTRA_ARGS refused: the list of forbidden flags $FORBIDDEN_FILE is missing" >&2
    exit 2
  fi
  mapfile -t extra <<<"$EXTRA_ARGS"
  if [ "${#extra[@]}" -gt 40 ]; then echo "EXTRA_ARGS refused: more than 40 tokens" >&2; exit 2; fi
  for ((i = 0; i < ${#extra[@]}; i++)); do
    tok="${extra[$i]}"
    if [ -z "$tok" ] || printf '%s' "$tok" | LC_ALL=C grep -q '[^ -~]'; then
      echo "EXTRA_ARGS refused: only printable ASCII is allowed in token $((i + 1))" >&2
      exit 2
    fi
    if [[ "$tok" == -* ]] && [ "${#tok}" -gt 1 ]; then
      name="${tok%%=*}"
      # llama.cpp reads `_` as `-` in a long flag (`--api_key` is `--api-key`): compare the normalised name, in lower
      # case (llama_args.normalize_flag); a short flag stays as typed
      flag="$name"
      if [[ "$name" == --* ]]; then flag="$(printf '%s' "${name//_/-}" | tr 'A-Z' 'a-z')"; fi
      if tr -d "$CR" <"$FORBIDDEN_FILE" | grep -qxF -- "$flag"; then
        echo "EXTRA_ARGS refused: the flag $name is set by the portal" >&2
        exit 2
      fi
      # the portal's generic rule: long flags that name files, paths, directories, URLs or keys
      if [[ "$flag" == --* ]]; then
        case "$flag" in
          *-file|*-path|*-dir|*-url|*cache-static*|*cache-dynamic*|*-key*)
            echo "EXTRA_ARGS refused: flags with files, paths and keys are set by the portal: $name" >&2
            exit 2 ;;
        esac
      fi
      value=""
      [[ "$tok" == *=* ]] && value="${tok#*=}"
    else
      if [ "$i" -eq 0 ]; then echo "EXTRA_ARGS refused: own flags start with '-' ($tok)" >&2; exit 2; fi
      value="$tok"
    fi
    if [[ "$value" == */* || "$value" == *"$BS"* || "$value" == "~"* || "$value" == . || "$value" == .. ]]; then
      echo "EXTRA_ARGS refused: file paths are not allowed ($tok)" >&2
      exit 2
    fi
  done
fi

ACCEL="${ACCEL:-auto}"
if [ "$ACCEL" = "auto" ] || [ -z "$ACCEL" ]; then
  ACCEL="$("$HERE/detect-accel.sh")"
fi

# Layers reach RPC devices only through -ngl, so an RPC split always offloads everything.
if [ -n "${NGL:-}" ]; then
  :
elif [ -n "$RPC_SERVERS" ] || [ "$ACCEL" = "nvidia" ]; then
  NGL=99
else
  NGL=0
fi

cmd=(llama-server
  --host 0.0.0.0 --port "$INFER_PORT"
  --jinja --metrics --cache-ram 512 -fa auto
  --ctx-size "$CTX_SIZE" --parallel "$PARALLEL_SLOTS" --no-kv-unified
  -ngl "$NGL" -t "$THREADS" --alias "$PUBLIC_MODEL_NAME")
if [ -n "$RPC_SERVERS" ]; then
  # --fit would probe every rpc device in a dry load first, and it reads 16 entries only (more devices with 64 backends)
  cmd+=(--rpc "$RPC_SERVERS" --fit off)
  # whole layers per rpc device in --rpc order, inside a server in its -d order (computed by the portal)
  [ -n "${TENSOR_SPLIT:-}" ] && cmd+=(--tensor-split "$TENSOR_SPLIT")
fi
# Backend key: every /v1 request needs it, and so does /metrics (--metrics, scraped by the portal's monitoring
# controller, task 2-33); llama-server leaves only /health open for probes.
# llama-server reads it from LLAMA_API_KEY (common/arg.cpp, `--api-key` .set_env), so it never appears in argv.
# k3s pods get the node's own key as a file (LLAMA_API_KEY_FILE, the node container's $STATE_DIR/backend/key written by
# registrar.sh, task SEC-1); a pod that starts before the node container wrote it waits for it, at most
# BACKEND_KEY_WAIT_S seconds. Standalone mode: registrar.sh exports LLAMA_API_KEY itself.
if [ -n "${BACKEND_API_KEY:-}" ]; then
  export LLAMA_API_KEY="$BACKEND_API_KEY"
fi
unset BACKEND_API_KEY
if [ -n "${LLAMA_API_KEY_FILE:-}" ] && [ "$PRINT" != 1 ]; then
  waited=0
  until [ -s "$LLAMA_API_KEY_FILE" ] || [ "$waited" -ge "${BACKEND_KEY_WAIT_S:-300}" ]; do
    [ "$waited" = 0 ] && echo "waiting for the backend key of this node in $LLAMA_API_KEY_FILE" >&2
    sleep 1
    waited=$((waited + 1))
  done
  if ! [ -s "$LLAMA_API_KEY_FILE" ]; then
    echo "no backend key in $LLAMA_API_KEY_FILE: the node container has not registered with the portal (or waits for the admin's approval)" >&2
    exit 1
  fi
  LLAMA_API_KEY="$(tr -d '\r\n' < "$LLAMA_API_KEY_FILE")"
  export LLAMA_API_KEY
fi
unset LLAMA_API_KEY_FILE
# Thinking models (Gemma 4) otherwise spend short max_tokens on reasoning_content and return empty content.
# b12033 deprecates --chat-template-kwargs '{"enable_thinking":false}' in favour of --reasoning.
cmd+=(--reasoning "$THINKING")
# b12033 turns flash attention on by itself for a quantized V cache (llama-context.cpp: "enabling flash_attn since it
# is required for quantized V cache"), so `-fa auto` stays; only an explicit `-fa off` would refuse it.
[ "$KV_CACHE_TYPE" != f16 ] && cmd+=(--cache-type-k "$KV_CACHE_TYPE" --cache-type-v "$KV_CACHE_TYPE")
[ "${#sampling[@]}" -gt 0 ] && cmd+=("${sampling[@]}")

# Multi-file GGUF, and any file of a pinned commit: download it ourselves and point -m at the (first) file. llama.cpp
# b12033 has no revision flag (--hf-repo always resolves main through /api/models/<repo>/refs). The files come from
# resolve/<MODEL_REVISION>, or resolve/main without a pin: then a repository that changes while the nodes download may
# leave them with different versions, which the content hash kept per file (.etag) catches on the next start only.
SHARD_URLS=()
SHARD_PATHS=()
FIRST_PATH=""
# add_download REPO FILE REV: the files of REPO/FILE (every shard of a multi-file GGUF) from resolve/REV into the cache;
# FIRST_PATH is the file to point llama-server at
add_download() {
  local repo="$1" file="$2" rev="$3" d name i prefix total
  d="$LLAMA_CACHE/${repo//\//__}"
  if [[ "$file" =~ ^(.*)-00001-of-([0-9]{5})\.gguf$ ]]; then
    prefix="${BASH_REMATCH[1]}"
    total=$((10#${BASH_REMATCH[2]}))
    for ((i = 1; i <= total; i++)); do
      name="$(printf '%s-%05d-of-%05d.gguf' "$prefix" "$i" "$total")"
      SHARD_URLS+=("$HF_ENDPOINT/$repo/resolve/$rev/$name")
      SHARD_PATHS+=("$d/$name")
    done
  else
    SHARD_URLS+=("$HF_ENDPOINT/$repo/resolve/$rev/$file")
    SHARD_PATHS+=("$d/$file")
  fi
  FIRST_PATH="$d/$file"
}
REV="${MODEL_REVISION:-main}"
if [[ "$MODEL_FILE" =~ ^(.*)-00001-of-([0-9]{5})\.gguf$ ]] || [ -n "$MODEL_REVISION" ]; then
  add_download "$MODEL_REPO" "$MODEL_FILE" "$REV"
  cmd+=(-m "$FIRST_PATH")
else
  cmd+=(--hf-repo "$MODEL_REPO" --hf-file "$MODEL_FILE")
fi
# The image projector and the draft model are downloaded the same way (the leader's pre-download, with the retries and
# the ETag check below) and given as files: b12033 has no --hf-file-draft (-hfd takes a repo[:quant] only).
if [ -n "$MMPROJ_FILE" ]; then
  add_download "$MMPROJ_REPO" "$MMPROJ_FILE" "$MMPROJ_REV"
  cmd+=(--mmproj "$FIRST_PATH")
  # b12033 offloads the projector to a GPU-type device by default, and on a split leader the only such devices are the
  # rpc servers (the first one is this node's own GPU, already filled with layers): keep it on the leader, where the
  # portal's estimate puts it
  [ -n "$RPC_SERVERS" ] && cmd+=(--no-mmproj-offload)
else
  cmd+=(--no-mmproj)
fi
if [ -n "$DRAFT_REPO" ]; then
  add_download "$DRAFT_REPO" "$DRAFT_FILE" "${DRAFT_REVISION:-main}"
  # b12033 loads -md but drafts nothing unless the type is named (it infers only MTP/DFlash heads from the GGUF);
  # n-max 4 measured best on a 4070 (Qwen3-8B + 0.6B: x1.4-1.5; 16 was no gain at 21-36% acceptance)
  cmd+=(-md "$FIRST_PATH" --spec-type draft-simple --spec-draft-n-max 4 --spec-draft-n-min 1)
  # the draft rides the GPU of a replica; the split leader has none (its layers are on the rpc servers): it keeps the
  # draft in its own RAM, where the portal's estimate counts it
  if [ -z "$RPC_SERVERS" ] && [ "$ACCEL" = "nvidia" ]; then cmd+=(-ngld 99); else cmd+=(-ngld 0); fi
fi
[ "${#extra[@]}" -gt 0 ] && cmd+=("${extra[@]}")

if [ "$PRINT" = 1 ]; then
  for i in "${!SHARD_URLS[@]}"; do
    echo "download ${SHARD_URLS[$i]} -> ${SHARD_PATHS[$i]}"
  done
  echo "${cmd[*]}"
  [ -n "${LLAMA_API_KEY:-}" ] && echo "LLAMA_API_KEY is set (not shown)"
  [ -n "${SSL_CERT_FILE:-}" ] && echo "SSL_CERT_FILE=$SSL_CERT_FILE"
  echo "HF_ENDPOINT=$HF_ENDPOINT"
  exit 0
fi

# Cached shards of models no deployment uses any more: KEEP_MODEL_REPOS (owner/repo,...) lists the repos of every
# deployment of the portal (ConfigMap llm-model-repos, read when the container starts). The shard dirs of other repos
# ($LLAMA_CACHE/<owner>__<repo>) are deleted; this pod's own repos (the model, its draft and projector) always stay. A copy of a model that is still deployed
# stays too, also on a node that serves another deployment now. Own model files (local__*), the rpc tensor cache (rpc)
# and vLLM's cache (huggingface) are never touched. Without the list nothing is deleted.
prune_models() {
  [ -n "${KEEP_MODEL_REPOS:-}" ] || return 0
  local keep=",${KEEP_MODEL_REPOS//\//__},${MODEL_REPO//\//__},${DRAFT_REPO//\//__},${MMPROJ_REPO//\//__}," d name
  for d in "$LLAMA_CACHE"/*__*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*__[A-Za-z0-9._-]+$ ]] || continue
    case "$name" in local__*) continue ;; esac
    case "$keep" in *",$name,"*) continue ;; esac
    echo "removing cached model $name ($(du -sh "$d" 2>/dev/null | cut -f1)): no deployment uses it"
    rm -rf -- "$d"
  done
}
prune_models

# The optional HF token reaches curl as a header file (mktemp: mode 0600), never through argv.
auth=()
HDR=""
trap '[ -z "$HDR" ] || rm -f "$HDR"' EXIT
if [ -n "${HF_TOKEN:-}" ]; then
  HDR="$(mktemp)"
  printf 'Authorization: Bearer %s\n' "$HF_TOKEN" > "$HDR"
  auth=(-H "@$HDR")
fi

DL_PID=""
on_term() {
  if [ -n "$DL_PID" ]; then
    kill -TERM "$DL_PID" 2>/dev/null
    wait "$DL_PID" 2>/dev/null
  fi
  exit 143
}
trap on_term TERM INT

# url -> "<size> <etag>" of the final (redirect-followed) answer, "- -" unless that answer is a 200 (the length of an
# error page is no shard size); either may be unknown ("-"). Every hop starts afresh: a redirect's Content-Length is
# that of its own body. The ETag is the content hash in X-Linked-Etag when a hop carries one: Hugging Face sends it on
# the redirect to its CDN, whose own ETag (md5-like) differs, and the mirror on main sends both, so a shard fetched
# through one and checked through the other is not "changed".
remote_meta() {
  curl -sSIL --max-time 60 "${auth[@]}" "$1" 2>/dev/null | tr -d '\r' | awk '
    function clean(e) { sub(/^W\//, "", e); gsub(/"/, "", e); return e }
    /^HTTP\// {status = $2; size = ""; etag = ""; next}
    tolower($1) == "content-length:" {size = $2}
    tolower($1) == "etag:" {etag = clean($2)}
    tolower($1) == "x-linked-etag:" {linked = clean($2)}
    END {
      if (status != "200") { print "- -"; exit }
      if (linked != "") etag = linked
      print (size == "" ? "-" : size), (etag == "" ? "-" : etag)
    }'
}

# download URL PART: curl into PART with resume (-C -). A stream that breaks (a flaky link to the mirror, curl exit
# 18/56) is not a failure: curl retries every error itself (--retry-all-errors, bounded by --retry-max-time so that a
# 404 or a wrong key ends within minutes), and when it gives up having added bytes it is started again, so a large
# file arrives in pieces. An attempt that adds nothing ends the download with curl's exit code.
# curl runs in the background: TERM (registrar's stop_server, pod deletion) reaches this script, which stops curl
# before exiting. A curl left running would keep appending to PART while the next start resumes it (-C -).
download() {
  local url="$1" part="$2" before after rc
  while :; do
    before="$(stat -c %s "$part" 2>/dev/null || echo 0)"
    curl -fL --retry 100 --retry-all-errors --retry-delay 2 --retry-max-time 300 -C - "${auth[@]}" -o "$part" "$url" &
    DL_PID=$!
    rc=0
    wait "$DL_PID" || rc=$?
    DL_PID=""
    [ "$rc" = 0 ] && return 0
    after="$(stat -c %s "$part" 2>/dev/null || echo 0)"
    if [ "$after" -le "$before" ]; then
      echo "download of $url failed (curl exit $rc), no progress" >&2
      return "$rc"
    fi
    echo "download of $url interrupted (curl exit $rc) at $after bytes, resuming" >&2
    sleep 2
  done
}

# verified PART ETAG: when the ETag is a sha256 (64 hex: the LFS oid of Hugging Face, the sha256 the mirror reports for an
# own model file), the downloaded bytes must have it (SEC-2 L5): a substituted or corrupted file is not loaded. Another
# ETag (md5-like, size and mtime) names no content hash: nothing to check.
verified() {
  local part="$1" want="${2,,}" got
  [[ "$want" =~ ^[0-9a-f]{64}$ ]] || return 0
  got="$(sha256sum "$part" | cut -c1-64)"
  [ "$got" = "$want" ] && return 0
  echo "sha256 of $part is $got, the source reports $want: the file is deleted" >&2
  return 1
}

# The ETag of every downloaded shard is kept in <shard>.etag: a file of the same name and size but another version (an
# own model file uploaded again, a file changed on Hugging Face) is downloaded again, and a .part of another version is
# not resumed. A shard without a recorded ETag (downloaded before) is kept while its size matches.
for i in "${!SHARD_URLS[@]}"; do
  url="${SHARD_URLS[$i]}"
  path="${SHARD_PATHS[$i]}"
  mkdir -p "$(dirname "$path")"
  read -r want etag <<<"$(remote_meta "$url" || true)"
  [ "${want:--}" = - ] && want=""
  [ "${etag:--}" = - ] && etag=""
  if [ -f "$path" ]; then
    have="$(stat -c %s "$path")"
    old="$(cat "$path.etag" 2>/dev/null || true)"
    if { [ -z "$want" ] || [ "$have" = "$want" ]; } && { [ -z "$etag" ] || [ -z "$old" ] || [ "$old" = "$etag" ]; }; then
      echo "shard ok: $path"
      continue
    fi
    [ -n "$etag" ] && [ -n "$old" ] && [ "$old" != "$etag" ] && echo "shard changed upstream ($old -> $etag): $path"
  fi
  if [ -f "$path.part" ] && [ -n "$etag" ] && [ "$(cat "$path.part.etag" 2>/dev/null || true)" != "$etag" ]; then
    rm -f "$path.part"
  fi
  if [ -n "$etag" ]; then printf '%s\n' "$etag" > "$path.part.etag"; else rm -f "$path.part.etag"; fi
  echo "downloading $url"
  download "$url" "$path.part" || exit $?
  # a mismatch is downloaded once more from the start (a broken resume); twice in a row: the source serves other bytes
  if ! verified "$path.part" "$etag"; then
    rm -f "$path.part"
    echo "downloading $url again"
    download "$url" "$path.part" || exit $?
    if ! verified "$path.part" "$etag"; then
      rm -f "$path.part" "$path.part.etag"
      echo "the file $url does not match its sha256 twice: not loaded" >&2
      exit 1
    fi
  fi
  mv "$path.part" "$path"
  if [ -f "$path.part.etag" ]; then mv "$path.part.etag" "$path.etag"; else rm -f "$path.etag"; fi
done

# Wait for every RPC peer to accept TCP (up to 300 s).
if [ -n "$RPC_SERVERS" ]; then
  IFS=',' read -ra peers <<< "$RPC_SERVERS"
  deadline=$((SECONDS + 300))
  for peer in "${peers[@]}"; do
    host="${peer%:*}"
    port="${peer##*:}"
    until (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; do
      if [ "$SECONDS" -ge "$deadline" ]; then
        echo "RPC server $peer did not become reachable within 300 s" >&2
        exit 1
      fi
      sleep 2
    done
    echo "rpc peer reachable: $peer"
  done
fi

[ -z "$HDR" ] || rm -f "$HDR"   # EXIT traps do not run on exec
exec "${cmd[@]}"
