#!/usr/bin/env bash
# Кладёт образ из containerd главного (k3s, пространство k8s.io) в реестр главного с учёткой записи (SEC-2 M2).
# Использование (внутри контейнера llm-main): registry-push.sh 127.0.0.1:<порт>/<репозиторий>:<тег> [--amd64-manifest]
#   образ с этим именем уже есть в containerd (install-main.sh: ctr images pull / import, затем tag); он пушится в сам реестр
#   (127.0.0.1:REGISTRY_BACKEND_PORT, /run/llm/registry.env) с паролем REGISTRY_PASSWORD из /var/lib/llm/secrets.env. Порт в
#   имени не важен (обычно REGISTRY_PORT: адрес Caddy на чтение), берутся репозиторий и тег.
# --amd64-manifest: образ в containerd главного это индекс из нескольких платформ, а содержимое лежит только для linux/amd64 (так k3s
#   скачивает и загружает pause: rancher/mirrored-pause). ctr images push отправил бы индекс целиком, и реестр отклонил бы его
#   ("manifest blob unknown": манифестов остальных платформ нет). Этот режим кладёт в реестр под тем же тегом только манифест linux/amd64
#   (одним манифестом, не индексом) и его блобы по HTTP API реестра; нода (amd64) берёт его так же, как индекс.
# Пароль не попадает в argv (/proc/<pid>/cmdline виден и с хоста главного): ctr получает его заголовком Authorization из
# hosts.toml во временном каталоге 0700 (ctr --hosts-dir), файл 0600 удаляется сразу после push.
set -euo pipefail

SECRETS_FILE="${SECRETS_FILE:-/var/lib/llm/secrets.env}"
REGISTRY_ENV="${REGISTRY_ENV:-/run/llm/registry.env}"
CTR=(k3s ctr -n k8s.io)
die() { echo "registry-push.sh: $*" >&2; exit 1; }

[ $# = 1 ] || { [ $# = 2 ] && [ "$2" = --amd64-manifest ]; } || die "использование: registry-push.sh 127.0.0.1:<порт>/<репозиторий>:<тег> [--amd64-manifest]"
AMD64_ONLY=0; [ "${2:-}" != --amd64-manifest ] || AMD64_ONLY=1
[[ "$1" =~ ^127\.0\.0\.1:[0-9]{2,5}/([a-z0-9]([a-z0-9._/-]*[a-z0-9])?:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})$ ]] \
  || die "нужно имя образа вида 127.0.0.1:<порт>/<репозиторий>:<тег>, получено: $1"
name="${BASH_REMATCH[1]}"
[ -r "$REGISTRY_ENV" ] || die "$REGISTRY_ENV нет: главный не запущен или запущен образ до SEC-2"
port="$(sed -n 's/^REGISTRY_BACKEND_PORT=\([0-9]*\)$/\1/p' "$REGISTRY_ENV")"
user="$(sed -n 's/^REGISTRY_USER=\([a-z0-9-]*\)$/\1/p' "$REGISTRY_ENV")"
pw="$(sed -n 's/^REGISTRY_PASSWORD=\([A-Za-z0-9._~-]*\)$/\1/p' "$SECRETS_FILE" 2>/dev/null)"
[ -n "$port" ] && [ -n "$user" ] || die "в $REGISTRY_ENV нет REGISTRY_BACKEND_PORT или REGISTRY_USER"
[ -n "$pw" ] || die "в $SECRETS_FILE нет REGISTRY_PASSWORD"

target="127.0.0.1:$port/$name"
if [ "$target" != "$1" ]; then
  "${CTR[@]}" images tag --force "$1" "$target" >/dev/null || die "ctr images tag $1 $target не удался: такого образа нет в containerd"
fi
# push_amd64_manifest REF NAME: манифест linux/amd64 образа REF (индекс или одиночный манифест) и его блобы в реестр под NAME (репозиторий:тег)
push_amd64_manifest() {
  local ref="$1" repo="${2%:*}" tag="${2##*:}" row mt dig d code loc sep mtype url="http://127.0.0.1:$port/v2/${2%:*}"
  row="$("${CTR[@]}" images ls "name==$ref" | awk 'NR==2 {print $2, $3}')"
  mt="${row% *}"; dig="${row#* }"
  [ -n "$row" ] && [ -n "$dig" ] || die "образа $ref нет в containerd"
  case "$mt" in
    *manifest.list*|*image.index*)
      dig="$("${CTR[@]}" content get "$dig" | tr -d ' \t\r\n' | tr '{' '\n' \
        | awk '/"digest":"sha256:/ {d=$0; sub(/.*"digest":"/, "", d); sub(/".*/, "", d)} /"architecture":"amd64","os":"linux"/ {print d; exit}')"
      [ -n "$dig" ] || die "в индексе $ref нет манифеста linux/amd64" ;;
  esac
  "${CTR[@]}" content get "$dig" >"$hosts/manifest" || die "манифеста $dig нет в containerd (содержимое linux/amd64 не загружено)"
  mtype="$(tr -d ' \t\r\n' <"$hosts/manifest" | grep -o '"mediaType":"[^"]*"' | head -n1 | cut -d'"' -f4)"
  case "$mtype" in *manifest*) ;; *) mtype=application/vnd.oci.image.manifest.v1+json ;; esac
  for d in $(tr -d ' \t\r\n' <"$hosts/manifest" | grep -o '"digest":"sha256:[0-9a-f]*"' | cut -d'"' -f4 | sort -u); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -I -K "$hosts/curl.cfg" "$url/blobs/$d" || true)"
    [ "$code" != 200 ] || continue
    "${CTR[@]}" content get "$d" >"$hosts/blob" || die "блоба $d нет в containerd"
    loc="$(curl -fsS -D - -o /dev/null -X POST -K "$hosts/curl.cfg" "$url/blobs/uploads/" | tr -d '\r' | sed -n 's/^[Ll]ocation: *//p')"
    [ -n "$loc" ] || die "реестр не открыл загрузку блоба $d"
    case "$loc" in /*) loc="http://127.0.0.1:$port$loc" ;; esac
    case "$loc" in *\?*) sep='&' ;; *) sep='?' ;; esac
    curl -fsS -o /dev/null -K "$hosts/curl.cfg" -T "$hosts/blob" -H 'Content-Type: application/octet-stream' "$loc${sep}digest=$d" || die "загрузка блоба $d в реестр не удалась"
  done
  curl -fsS -o /dev/null -K "$hosts/curl.cfg" -T "$hosts/manifest" -H "Content-Type: $mtype" "$url/manifests/$tag" || die "загрузка манифеста $dig в реестр не удалась (журнал реестра: k3s kubectl -n llm logs deploy/registry)"
}

hosts="$(umask 077; mktemp -d)"
trap 'rm -rf "$hosts"' EXIT
( umask 077; printf 'user = "%s:%s"\n' "$user" "$pw" >"$hosts/curl.cfg" )
mkdir -m 700 "$hosts/127.0.0.1:$port"
( umask 077
  printf 'server = "http://127.0.0.1:%s"\n\n[host."http://127.0.0.1:%s"]\n  capabilities = ["pull", "resolve", "push"]\n  [host."http://127.0.0.1:%s".header]\n    Authorization = "Basic %s"\n' \
    "$port" "$port" "$port" "$(printf '%s:%s' "$user" "$pw" | base64 -w0)" >"$hosts/127.0.0.1:$port/hosts.toml" )
rc=0
if [ "$AMD64_ONLY" = 1 ]; then push_amd64_manifest "$target" "$name" || rc=$?
else "${CTR[@]}" images push --plain-http --platform linux/amd64 --hosts-dir "$hosts" "$target" || rc=$?; fi
rm -rf "$hosts"
if [ "$target" != "$1" ]; then "${CTR[@]}" images rm --sync "$target" >/dev/null 2>&1 || true; fi
[ "$rc" = 0 ] || die "ctr images push $target не удался (журнал реестра: k3s kubectl -n llm logs deploy/registry)"
echo "registry-push.sh: $name в реестре главного"
