#!/usr/bin/env bash
# Кладёт образ из containerd главного (k3s, пространство k8s.io) в реестр главного с учёткой записи (SEC-2 M2).
# Использование (внутри контейнера llm-main): registry-push.sh 127.0.0.1:<порт>/<репозиторий>:<тег>
#   образ с этим именем уже есть в containerd (install-main.sh: ctr images pull / import, затем tag); он пушится в сам реестр
#   (127.0.0.1:REGISTRY_BACKEND_PORT, /run/llm/registry.env) с паролем REGISTRY_PASSWORD из /var/lib/llm/secrets.env. Порт в
#   имени не важен (обычно REGISTRY_PORT: адрес Caddy на чтение), берутся репозиторий и тег.
# Пароль не попадает в argv (/proc/<pid>/cmdline виден и с хоста главного): ctr получает его заголовком Authorization из
# hosts.toml во временном каталоге 0700 (ctr --hosts-dir), файл 0600 удаляется сразу после push.
set -euo pipefail

SECRETS_FILE="${SECRETS_FILE:-/var/lib/llm/secrets.env}"
REGISTRY_ENV="${REGISTRY_ENV:-/run/llm/registry.env}"
CTR=(k3s ctr -n k8s.io)
die() { echo "registry-push.sh: $*" >&2; exit 1; }

[ $# = 1 ] || die "использование: registry-push.sh 127.0.0.1:<порт>/<репозиторий>:<тег>"
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
hosts="$(umask 077; mktemp -d)"
trap 'rm -rf "$hosts"' EXIT
mkdir -m 700 "$hosts/127.0.0.1:$port"
( umask 077
  printf 'server = "http://127.0.0.1:%s"\n\n[host."http://127.0.0.1:%s"]\n  capabilities = ["pull", "resolve", "push"]\n  [host."http://127.0.0.1:%s".header]\n    Authorization = "Basic %s"\n' \
    "$port" "$port" "$port" "$(printf '%s:%s' "$user" "$pw" | base64 -w0)" >"$hosts/127.0.0.1:$port/hosts.toml" )
rc=0
"${CTR[@]}" images push --plain-http --platform linux/amd64 --hosts-dir "$hosts" "$target" || rc=$?
rm -rf "$hosts"
if [ "$target" != "$1" ]; then "${CTR[@]}" images rm --sync "$target" >/dev/null 2>&1 || true; fi
[ "$rc" = 0 ] || die "ctr images push $target не удался (журнал реестра: k3s kubectl -n llm logs deploy/registry)"
echo "registry-push.sh: $name в реестре главного"
