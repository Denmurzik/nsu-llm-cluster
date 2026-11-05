#!/usr/bin/env bash
# Выпуск образов NSU LLM Cluster в ghcr.io: собрать три образа на сервере сборки (newbar), выложить их, записать
# release/<версия>.json и поставить git-тег v<версия> (локально). Ветки не отправляются: тег и json отправляет оператор.
# Использование:
#   GHCR_OWNER=<владелец на GitHub> release/release.sh [--dry-run] [--skip-build] [--latest] <версия>
#
#   <версия>       например 0.1.0; должна совпадать с версией этого чекаута (main/install-main.sh, node/IMAGE_VERSION)
#   --dry-run      напечатать каждый шаг и ничего не менять: ни docker, ни ghcr.io, ни git, ни файлов
#   --skip-build   не собирать: выложить уже имеющиеся локальные теги ghcr.io/<владелец>/... (после неудачной выкладки)
#   --latest       кроме <версия> выложить и «плавающие» теги main, cpu, gpu
#
# Окружение: GHCR_OWNER (обязательно, строчными буквами), GHCR_TOKEN_FILE (токен с правом write:packages; по умолчанию
# ~/.config/nsu-llm/ghcr-token, права строго 0600), JOBS (параллелизм сборки llama.cpp, по умолчанию 8).
# Токен читается из файла и идёт в docker login через stdin, никогда не попадает в командную строку и в вывод. Вход
# выполняется в отдельный каталог DOCKER_CONFIG, который удаляется при выходе вместе с docker logout ghcr.io: существующий
# вход оператора и его docker config не затрагиваются.
# Сервер сборки обслуживает другие службы: образы собираются под своими именами ghcr.io/<владелец>/nsu-llm-*:<версия>
# (теги nsu-llm-main:0.1.0 и nsu-llm-node:* и чужие контейнеры не трогаются), после выкладки удаляются только эти теги,
# кэш сборки остаётся. Ничего не удаляется командами prune и rm. nice -n 15 снижает приоритет клиента docker; нагрузку
# демона ограничивает JOBS.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
REGISTRY=ghcr.io
DRY=0
SKIP_BUILD=0
LATEST=0
VERSION=""
JOBS="${JOBS:-8}"
OWNER="${GHCR_OWNER:-}"
TOKEN_FILE="${GHCR_TOKEN_FILE:-$HOME/.config/nsu-llm/ghcr-token}"

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --latest) LATEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die "неизвестный параметр: $1" ;;
    *) [ -z "$VERSION" ] || die "версия указана дважды: $VERSION и $1"; VERSION="$1"; shift ;;
  esac
done
[ -n "$VERSION" ] || { usage >&2; die "не указана версия (например: release.sh 0.1.0)"; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "версия должна быть вида 1.2.3 или 1.2.3-rc.1, получено: $VERSION"
[ -n "$OWNER" ] || die "не задан GHCR_OWNER: имя пользователя или организации на GitHub, под которым лежат образы (GHCR_OWNER=<имя> release/release.sh $VERSION)"
[[ "$OWNER" =~ ^[a-z0-9]([a-z0-9-]{0,37}[a-z0-9])?$ ]] || die "GHCR_OWNER=$OWNER: ghcr.io принимает только строчные латинские буквы, цифры и дефис"
[[ "$JOBS" =~ ^[0-9]{1,3}$ ]] && [ "$((10#$JOBS))" -ge 1 ] || die "JOBS должно быть целым числом от 1, получено: $JOBS"

# run CMD...: при --dry-run только печатает команду
run() {
  if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$(printf '%q ' "$@")"; else "$@"; fi
}

# ------------------------------------------------------------------ проверки
say "Проверки:"
[ -z "$(git -C "$REPO" status --porcelain)" ] || die "в $REPO есть незафиксированные изменения (git status): выпускать можно только из чистого дерева, иначе образ не соответствует коммиту"
if git -C "$REPO" rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
  die "тег v$VERSION уже существует: версия выпущена. Возьмите следующую или удалите тег, если выпуск не состоялся (git tag -d v$VERSION)"
fi
COMMIT="$(git -C "$REPO" rev-parse HEAD)"
say "  [ок] дерево чистое, тега v$VERSION нет, коммит ${COMMIT:0:12}"

ver_main="$(sed -n 's/^IMAGE="nsu-llm-main:\([^"]*\)".*/\1/p' "$REPO/main/install-main.sh" | head -n1)"
ver_node="$(tr -d ' \r\n' <"$REPO/node/IMAGE_VERSION" 2>/dev/null)"
for pair in "main/install-main.sh:$ver_main" "node/IMAGE_VERSION:$ver_node"; do
  [ "${pair#*:}" = "$VERSION" ] \
    || die "версия выпуска $VERSION не совпадает с версией чекаута в ${pair%%:*} (${pair#*:}): образы и скрипты ищут друг друга по этой версии. Поднимите версию в main/install-main.sh, main/run-main.sh, node/IMAGE_VERSION (одна запасная версия для install-node.ps1, run-node.ps1, run-node.sh) и NODE_IMAGE_TAG портала, зафиксируйте и повторите"
done
say "  [ок] версия выпуска совпадает с версией скриптов чекаута ($VERSION)"
# остальные места, называющие версию образа (скрипты главного, значения по умолчанию портала и манифестов, .env.example)
if [ -f "$REPO/release/check-versions.sh" ]; then
  bash "$REPO/release/check-versions.sh" "$REPO" "$VERSION" || die "версия образа указана не везде одинаково (см. выше): поднимите её во всех перечисленных файлах, зафиксируйте и повторите"
  say "  [ок] версия совпадает во всех местах, где она названа (release/check-versions.sh)"
fi
K3S_VERSION="$(sed -n 's/^ARG K3S_VERSION=//p' "$REPO/main/Dockerfile" | head -n1)"
LLAMA_TAG="$(sed -n 's/^ARG LLAMA_TAG=//p' "$REPO/node/Dockerfile" | head -n1)"
[ -n "$K3S_VERSION" ] && [ -n "$LLAMA_TAG" ] || die "в Dockerfile не найдены ARG K3S_VERSION и ARG LLAMA_TAG"

if [ "$DRY" = 0 ]; then
  command -v docker >/dev/null 2>&1 || die "docker не найден в PATH"
  [ -f "$TOKEN_FILE" ] || die "нет файла с токеном $TOKEN_FILE: создайте токен GitHub с правом write:packages и сохраните его (docs/release.md, «Токен»): umask 077; cat > $TOKEN_FILE"
  perm="$(stat -c %a "$TOKEN_FILE" 2>/dev/null || stat -f %Lp "$TOKEN_FILE")"
  [ "$perm" = 600 ] || [ "$perm" = 400 ] || die "права $TOKEN_FILE: $perm. Токен должен читаться только владельцем (0600): chmod 600 $TOKEN_FILE"
  [ -s "$TOKEN_FILE" ] || die "файл с токеном $TOKEN_FILE пуст"
  say "  [ок] токен: $TOKEN_FILE (права $perm)"
else
  say "  [dry-run] токен будет прочитан из $TOKEN_FILE (права 0600), в docker login через stdin"
fi

MAIN_REF="$REGISTRY/$OWNER/nsu-llm-main"
NODE_REF="$REGISTRY/$OWNER/nsu-llm-node"
IMG_MAIN="$MAIN_REF:$VERSION"
IMG_CPU="$NODE_REF:$VERSION-cpu"
IMG_GPU="$NODE_REF:$VERSION-gpu"
ALL_TAGS=("$IMG_MAIN" "$IMG_CPU" "$IMG_GPU")
MOVING_TAGS=()
if [ "$LATEST" = 1 ]; then MOVING_TAGS=("$MAIN_REF:main" "$NODE_REF:cpu" "$NODE_REF:gpu"); fi

if [ "$SKIP_BUILD" = 1 ] && [ "$DRY" = 0 ]; then
  for t in "${ALL_TAGS[@]}"; do
    docker image inspect "$t" >/dev/null 2>&1 || die "--skip-build: локального образа $t нет (соберите без --skip-build)"
  done
fi

# ------------------------------------------------------------------ вход и выход
DOCKER_CFG=""
cleanup() {
  local rc=$?
  if [ -n "$DOCKER_CFG" ]; then
    DOCKER_CONFIG="$DOCKER_CFG" docker logout "$REGISTRY" >/dev/null 2>&1 || true
    rm -rf "$DOCKER_CFG"
  fi
  return $rc
}
trap cleanup EXIT

login() {
  say "Вход в $REGISTRY как $OWNER (токен из $TOKEN_FILE через stdin, отдельный docker config)"
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] DOCKER_CONFIG=<временный каталог> docker login $REGISTRY -u $OWNER --password-stdin < $TOKEN_FILE"
    say "  [dry-run] при выходе: docker logout $REGISTRY и удаление временного каталога"
    return
  fi
  DOCKER_CFG="$(mktemp -d)"
  chmod 700 "$DOCKER_CFG"
  # подключаемые модули docker (buildx) лежат в ~/.docker/cli-plugins: без ссылки сборка потеряла бы buildx
  if [ -d "$HOME/.docker/cli-plugins" ]; then ln -s "$HOME/.docker/cli-plugins" "$DOCKER_CFG/cli-plugins"; fi
  export DOCKER_CONFIG="$DOCKER_CFG"
  docker login "$REGISTRY" -u "$OWNER" --password-stdin <"$TOKEN_FILE" >/dev/null \
    || die "docker login $REGISTRY не удался: токен недействителен или без права write:packages, либо владелец $OWNER указан неверно"
}

# ------------------------------------------------------------------ сборка
build_all() {
  say "Сборка (nice -n 15, JOBS=$JOBS; кэш сборки сохраняется):"
  run nice -n 15 docker build --provenance=false -f "$REPO/main/Dockerfile" -t "$IMG_MAIN" --build-arg "VERSION=$VERSION" --build-arg "GIT_COMMIT=$COMMIT" "$REPO"
  run nice -n 15 docker build --provenance=false -f "$REPO/node/Dockerfile" --target cpu --build-arg "JOBS=$JOBS" -t "$IMG_CPU" --build-arg "VERSION=$VERSION" --build-arg "GIT_COMMIT=$COMMIT" "$REPO/node"
  run nice -n 15 docker build --provenance=false -f "$REPO/node/Dockerfile" --target gpu --build-arg "JOBS=$JOBS" -t "$IMG_GPU" --build-arg "VERSION=$VERSION" --build-arg "GIT_COMMIT=$COMMIT" "$REPO/node"
}

declare -A DIGEST=() SIZE=()
push_one() { # push_one REF: выкладывает и запоминает digest
  local ref="$1" out d
  if [ "$DRY" = 1 ]; then say "  [dry-run] docker push $ref"; DIGEST[$ref]="sha256:<digest>"; return; fi
  out="$(docker push "$ref" 2>&1)" || { printf '%s\n' "$out" >&2; die "docker push $ref не удался (токен без write:packages? пакет принадлежит другому владельцу? сеть до ghcr.io?)"; }
  d="$(sed -n 's/.*digest: \(sha256:[0-9a-f]\{64\}\).*/\1/p' <<<"$out" | tail -n1)"
  [ -n "$d" ] || die "docker push $ref: в выводе нет digest"
  DIGEST[$ref]="$d"
  say "  $ref -> $d"
}

# ------------------------------------------------------------------ json
write_json() {
  local out="$HERE/$VERSION.json" ref key first=1
  local -A name=([main]="$IMG_MAIN" [node-cpu]="$IMG_CPU" [node-gpu]="$IMG_GPU")
  say "Запись $out"
  if [ "$DRY" = 1 ]; then say "  [dry-run] версии, digest, llama.cpp $LLAMA_TAG, k3s $K3S_VERSION, размеры образов"; return; fi
  {
    printf '{\n'
    printf '  "version": "%s",\n' "$VERSION"
    printf '  "created": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "git_commit": "%s",\n' "$COMMIT"
    printf '  "ghcr_owner": "%s",\n' "$OWNER"
    printf '  "llama_cpp": "%s",\n' "$LLAMA_TAG"
    printf '  "k3s": "%s",\n' "$K3S_VERSION"
    printf '  "moving_tags": %s,\n' "$([ "$LATEST" = 1 ] && echo true || echo false)"
    printf '  "images": {\n'
    for key in main node-cpu node-gpu; do
      ref="${name[$key]}"
      [ "$first" = 1 ] || printf ',\n'
      first=0
      printf '    "%s": {"ref": "%s", "digest": "%s", "size_bytes": %s}' "$key" "$ref" "${DIGEST[$ref]}" "${SIZE[$ref]:-0}"
    done
    printf '\n  }\n}\n'
  } >"$out.tmp"
  mv "$out.tmp" "$out"
}

# ------------------------------------------------------------------ ход
login
if [ "$SKIP_BUILD" = 1 ]; then say "Сборка пропущена (--skip-build): выкладываются имеющиеся локальные теги."; else build_all; fi

say "Выкладка в $REGISTRY:"
for t in "${ALL_TAGS[@]}"; do push_one "$t"; done
for t in ${MOVING_TAGS[@]+"${MOVING_TAGS[@]}"}; do
  case "$t" in
    "$MAIN_REF:main") src="$IMG_MAIN" ;;
    "$NODE_REF:cpu") src="$IMG_CPU" ;;
    *) src="$IMG_GPU" ;;
  esac
  run docker tag "$src" "$t"
  push_one "$t"
done

for t in "${ALL_TAGS[@]}"; do
  if [ "$DRY" = 1 ]; then SIZE[$t]=0; else SIZE[$t]="$(docker image inspect -f '{{.Size}}' "$t")"; fi
done
write_json

say "Git-тег v$VERSION (локально, на коммите ${COMMIT:0:12}; отправляет оператор):"
run git -C "$REPO" tag "v$VERSION" "$COMMIT"

# убираются только теги, созданные этим выпуском; образы, которые были до него (--skip-build), и кэш сборки остаются
if [ "$SKIP_BUILD" = 0 ]; then
  say "Очистка: удаляются только созданные теги ghcr.io/$OWNER/nsu-llm-* этого выпуска (кэш сборки и чужие образы остаются):"
  for t in ${MOVING_TAGS[@]+"${MOVING_TAGS[@]}"} "${ALL_TAGS[@]}"; do run docker rmi "$t" || true; done
fi

say ""
say "Готово: версия $VERSION."
for t in "${ALL_TAGS[@]}"; do say "  $t  ${DIGEST[$t]}"; done
say "Дальше (оператор): git add release/$VERSION.json && git commit; git push origin <ветка> v$VERSION"
