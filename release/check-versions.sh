#!/usr/bin/env bash
# Сверка версии образа: node/IMAGE_VERSION единственный источник, а всё остальное, что называет версию, должно с ним совпадать.
# Использование: release/check-versions.sh [КОРЕНЬ_РЕПОЗИТОРИЯ] [ВЕРСИЯ]   (ВЕРСИЯ: ещё и версия выпуска; ее проверяет release.sh)
# Выход 0: все места совпали; 1: печатает каждое расхождение и что поправить.
# Места, которые читают файл сами (install-node.ps1, run-node.ps1, run-node.sh, class-deploy.ps1), здесь не перечислены.
set -u
REPO="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
WANT_RELEASE="${2:-}"
SRC="$REPO/node/IMAGE_VERSION"
[ -f "$SRC" ] || { echo "нет $SRC" >&2; exit 1; }
SRCV="$(tr -d ' \r\n' <"$SRC")"
[[ "$SRCV" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || { echo "node/IMAGE_VERSION: не версия вида 1.2.3: $SRCV" >&2; exit 1; }
[ -z "$WANT_RELEASE" ] || [ "$WANT_RELEASE" = "$SRCV" ] || { echo "версия выпуска $WANT_RELEASE не совпадает с node/IMAGE_VERSION ($SRCV)" >&2; exit 1; }

bad=0
# файл | регулярное выражение sed с одной группой \(...\) для версии
check() {
  local f="$1" re="$2" got
  if [ ! -f "$REPO/$f" ]; then echo "нет файла $f" >&2; bad=1; return; fi
  got="$(sed -n "s/$re/\1/p" "$REPO/$f" | head -n1 | tr -d '\r')"
  if [ "$got" != "$SRCV" ]; then
    echo "$f: версия «${got:-не найдена}», а в node/IMAGE_VERSION $SRCV (поправьте $f)" >&2
    bad=1
  fi
}
check main/install-main.sh '^IMAGE="nsu-llm-main:\([^"]*\)".*'
check main/run-main.sh '^VERSION="\([^"]*\)".*'
check main/run-main.ps1 '^[$]Version = '"'"'\([^'"'"']*\)'"'"'.*'
check main/entrypoint.sh '.*NODE_IMAGE_TAG:=\([^}]*\)}.*'
check main/render_manifests.py '^DEFAULT_NODE_IMAGE_TAG = "\([^"]*\)".*'
check portal/app/config.py '^ *node_image_tag: str = "\([^"]*\)".*'
check .env.example '^NODE_IMAGE_TAG=\(.*\)$'
exit $bad
