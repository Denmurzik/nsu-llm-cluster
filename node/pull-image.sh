#!/usr/bin/env bash
# Скачивает образ ноды с главного сервера (реестр главного, https://<главный>/v2/) в Docker этой машины и ставит ему
# локальное имя nsu-llm-node:<версия>-cpu|gpu, под которым его ищут run-node.* и поды кластера. Общий для Linux-ноды
# (run-node.sh --from-main) и для WSL установщика Windows-ноды (install-node.ps1); запускается от root.
# Использование:
#   pull-image.sh --main HOST --image nsu-llm-node:0.1.0-cpu [--digest sha256:...] [--ca FILE] [--record FILE] [--dry-run]
#   --no-tofu: без закреплённого корня и без --ca никогда не брать корень с https://<главный>/ca.crt без проверки (шаг 3 ниже),
#   а завершиться с ошибкой (вызывающий уже проверил корень по токену и передаёт --ca, если он есть).
#   --digest D: тянуть не тег, а манифест с этим digest (его называет главный: /api/node-image, desired ноды; SEC-2 M2).
#   Docker сам сверяет содержимое с digest, поэтому подменённый в реестре тег не доедет до ноды.
#   --record FILE: после загрузки записать в FILE «<id образа> <digest в реестре главного>». run-node передаёт digest контейнеру, нода
#   сообщает его главному, и панель видит, какая нода отстала. Строка верна, пока id совпадает с id образа на ноде (образ из архива
#   или загруженный заново её не унаследует). Без --record ничего не пишется.
#
# Доверие к реестру. Реестр главного отвечает на том же адресе и с тем же сертификатом, что портал. Корневой сертификат
# главного закрепляется ОДИН раз в /etc/docker/certs.d/<главный>/ca.crt (Docker применяет его только к этому реестру и не меняет
# системное хранилище; перезапуск Docker не нужен), и дальше проверяется только по нему:
#   1. закреплённый сертификат уже есть: реестр проверяется по нему; не совпал, то это ошибка, /ca.crt заново не скачивается
#      (подмена CA в сети дала бы нодам чужой образ с правами root). Сменили CA главного намеренно: удалите файл;
#   2. нет закреплённого, но передан --ca FILE (CA, который run-node уже закрепил в state/ca.crt): реестр проверяется по нему,
#      затем он закрепляется; не совпал: ошибка;
#   3. ни того, ни другого: если сертификат проверяется системными корнями (свой сертификат НГУ), ничего не добавляется, иначе
#      корень главного берётся один раз с https://<главный>/ca.crt без проверки (доверие при первом использовании, как у
#      run-node) и закрепляется.
# Использование (добавка): --ca FILE
# Тесты: PULL_IMAGE_CERTS_DIR заменяет /etc/docker/certs.d.
set -euo pipefail

CERTS_DIR="${PULL_IMAGE_CERTS_DIR:-/etc/docker/certs.d}"
MAIN=""
IMAGE=""
CA_ARG=""
RECORD=""
DIGEST=""
NO_TOFU=0
DRY=0

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --main) MAIN="${2:?}"; shift 2 ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --ca) CA_ARG="${2:?}"; shift 2 ;;
    --record) RECORD="${2:?}"; shift 2 ;;
    --digest) DIGEST="${2:?}"; shift 2 ;;
    --no-tofu) NO_TOFU=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done
[ -n "$MAIN" ] || { usage >&2; die "--main обязателен"; }
[ -n "$IMAGE" ] || { usage >&2; die "--image обязателен"; }
[[ "$MAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] || die "--main: нужен адрес главного (IP или имя, можно с портом), получено: $MAIN"
[[ "$IMAGE" =~ ^nsu-llm-node:[A-Za-z0-9._-]+$ ]] || die "--image: нужен образ вида nsu-llm-node:0.1.0-cpu, получено: $IMAGE"
[ -z "$DIGEST" ] || [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "--digest: нужен digest вида sha256:<64 hex>, получено: ${DIGEST:0:80}"
REMOTE="$MAIN/$IMAGE"
# с --digest тянется ровно этот манифест: <главный>/nsu-llm-node@sha256:...
[ -z "$DIGEST" ] || REMOTE="$MAIN/${IMAGE%%:*}@$DIGEST"
HOST_ONLY="${MAIN%%:*}"

# run CMD...: при --dry-run только печатает команду
run() {
  if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$(printf '%q ' "$@")"; else "$@"; fi
}

# HTTP-код https://<главный>/v2/ с заданными параметрами curl (000: нет соединения или сертификат не принят)
probe() { curl -sS -m 15 -o /dev/null -w '%{http_code}' "$@" "https://$MAIN/v2/" 2>/dev/null || true; }

say "Образ $IMAGE с главного: $REMOTE"
if [ "$DRY" = 1 ]; then
  say "  [dry-run] проверка https://$MAIN/v2/; корень главного закрепляется один раз ($CERTS_DIR/$MAIN/ca.crt) и дальше только проверяется по нему"
  run docker pull "$REMOTE"
  run docker tag "$REMOTE" "$IMAGE"
  run docker rmi "$REMOTE"
  [ -z "$RECORD" ] || say "  [dry-run] digest образа в реестре главного записывается в $RECORD"
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "docker не найден в PATH (в WSL его ставит install-node.ps1, на Linux установите Docker Engine)"
command -v curl >/dev/null 2>&1 || die "curl не найден в PATH"

PIN="$CERTS_DIR/$MAIN/ca.crt"
BAD_CA="сертификат реестра https://$MAIN/v2/ не совпал с сохранённым корнем главного ($PIN). Либо в сети подмена, либо сеть недоступна, либо CA главного сменили. Заново корень не скачивается. Если CA главного сменён намеренно: удалите $PIN (и state/ca.crt ноды) и повторите. Запасной путь: образ из архива (-ImageTar / docker load)."
pin_ca() { # pin_ca FILE: закрепить корень для этого реестра
  mkdir -p "$CERTS_DIR/$MAIN"
  cp "$1" "$PIN"
  chmod 644 "$PIN"
  say "  корневой сертификат главного закреплён для этого реестра: $PIN"
}
if [ -n "$CA_ARG" ]; then
  [ -s "$CA_ARG" ] && grep -q 'BEGIN CERTIFICATE' "$CA_ARG" || die "--ca $CA_ARG: это не сертификат PEM"
fi
if [ -s "$PIN" ]; then
  if [ -n "$CA_ARG" ] && ! cmp -s "$CA_ARG" "$PIN"; then die "$BAD_CA (сертификат ноды $CA_ARG отличается от закреплённого для Docker)"; fi
  code="$(probe --cacert "$PIN")"
  if [ "$code" = 000 ] || [ -z "$code" ]; then die "$BAD_CA"; fi
elif [ -n "$CA_ARG" ]; then
  code="$(probe --cacert "$CA_ARG")"
  if [ "$code" = 000 ] || [ -z "$code" ]; then die "реестр https://$MAIN/v2/ не принял корень главного $CA_ARG, закреплённый нодой: подмена в сети, сеть недоступна или CA сменён. Запасной путь: образ из архива (-ImageTar / docker load)."; fi
  pin_ca "$CA_ARG"
else
  code="$(probe)"
  if { [ "$code" = 000 ] || [ -z "$code" ]; } && [ "$NO_TOFU" = 1 ]; then
    die "сертификат https://$MAIN/v2/ не проверяется системными корнями, а корень главного не закреплён и не передан (--ca); с --no-tofu он не берётся с /ca.crt без проверки. Проверьте корень по токену подключения (fetch-main-ca.sh) и передайте --ca FILE."
  fi
  if [ "$code" = 000 ] || [ -z "$code" ]; then
    # системные корни сертификат не приняли и корень ещё не закреплён: единственный раз берём его с главного (доверие при первом использовании)
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    if curl -sSf -k --connect-timeout 5 -m 20 -o "$tmp" "https://$MAIN/ca.crt" 2>/dev/null && grep -q 'BEGIN CERTIFICATE' "$tmp"; then
      code="$(probe --cacert "$tmp")"
      if [ "$code" != 000 ] && [ -n "$code" ]; then pin_ca "$tmp"; else code=000; fi
    else
      code=000
    fi
  fi
fi

case "$code" in
  200) say "  реестр главного отвечает (200)" ;;
  401) say "  реестр главного отвечает (401)" ;;
  403) die "реестр главного ответил 403: адрес этой машины не входит в REGISTRY_ALLOW_FROM главного (по умолчанию только частные сети). Добавьте подсеть класса в .env главного (REGISTRY_ALLOW_FROM=10.1.0.0/16,...) и перезапустите главный, либо возьмите образ из архива (-ImageTar / docker load)." ;;
  404) die "на главном нет реестра образов (https://$MAIN/v2/ ответил 404): главный старше версии с реестром. Обновите главный (docs/release.md, «Обновление») или возьмите образ из архива (-ImageTar / docker load)." ;;
  *) die "https://$MAIN/v2/ недоступен (ответ: ${code:-нет}). Проверьте, что главный запущен, порт 443 открыт и $HOST_ONLY разрешается с этой машины (docs/deploy-class.md, «Сеть и DNS»), либо возьмите образ из архива (-ImageTar / docker load)." ;;
esac

docker pull "$REMOTE" || die "docker pull $REMOTE не удался: образа нет в реестре главного (на главном: docker exec llm-main curl -s http://127.0.0.1:5000/v2/nsu-llm-node/tags/list; пополняет его install-main.sh с IMAGE_SOURCE=pull или --update) или сертификат реестра не принят"
# digest манифеста, который отдал реестр главного (его же закрепляют поды кластера); после rmi записи о нём уже нет
digest="$DIGEST"
if [ -n "$RECORD" ] && [ -z "$digest" ]; then
  digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REMOTE" 2>/dev/null | tr -d '\r' | grep -F "$MAIN/nsu-llm-node@" | head -n1 | sed 's/^.*@//' || true)"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || digest=""
fi
docker tag "$REMOTE" "$IMAGE"
docker rmi "$REMOTE" >/dev/null 2>&1 || true
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "после docker pull образа $IMAGE нет"
if [ -n "$RECORD" ]; then
  id="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null | tr -d '\r' || true)"
  if [ -n "$digest" ] && [ -n "$id" ]; then
    mkdir -p "$(dirname "$RECORD")"
    printf '%s %s\n' "$id" "$digest" >"$RECORD.tmp" && mv -f "$RECORD.tmp" "$RECORD"
    say "  digest образа в реестре главного: $digest"
  else
    rm -f "$RECORD"
    printf 'Предупреждение: digest образа не определён, нода не сможет сообщить главному, какую версию получила (%s не записан).\n' "$RECORD" >&2
  fi
fi
say "Образ $IMAGE загружен с главного."
