#!/usr/bin/env bash
# Берёт корневой сертификат главного (https://<главный>/ca.crt) и проверяет его по токену подключения, а не верит первому ответу.
# Использование:
#   fetch-main-ca.sh --main HOST[:PORT] --out FILE [--token-file FILE] [--k3s-port 6443] [--trust-first-use]
#   токен: --token-file FILE, иначе переменная K3S_TOKEN или CLUSTER_TOKEN (в командной строке токен не передаётся)
# Коды выхода: 0 сертификат проверен (или подтверждён) и записан в FILE; 1 ошибка или подмена (в том числе нет подписи при
# токене K10: повторите через минуту или обновите главный); 3 голый токен и нет --trust-first-use; 4 главный не отдаёт /ca.crt.
# --yes прежних версий больше ничего не подтверждает (печатается предупреждение): с токеном K10 доверия без проверки нет никогда.
#
# Проверка (контракт с main/sign-ca.sh):
#   1. токен K10<H>::...: H это sha256 ответа https://<главный>:6443/cacerts (CA сервера k3s, его так же проверяет k3s agent);
#      не совпал: подмена, выход 1;
#   2. https://<главный>/ca.crt.p7s: подпись CMS точных байтов /ca.crt; подпись верна, и подписавший сертификат РАВЕН первому
#      сертификату /cacerts (цепочки к CA k3s мало: он выпускает и сертификаты нод, их ключи лежат на нодах);
#   3. токен без K10 (голый): доверие при первом использовании только с --trust-first-use; печатается
#      отпечаток sha256 сертификата, его можно сверить с главным: docker exec llm-main openssl x509 -noout -fingerprint -sha256 \
#      -in /var/lib/llm/caddy-data/caddy/pki/authorities/local/root.crt
# Нужны curl, openssl, sha256sum. Тесты подменяют curl в PATH.
set -euo pipefail

MAIN="" OUT="" TOKEN_FILE="" K3S_PORT=6443 YES=0

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*" >&2; }
usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --main) MAIN="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:?}"; shift 2 ;;
    --k3s-port) K3S_PORT="${2:?}"; shift 2 ;;
    --trust-first-use) YES=1; shift ;;
    --yes) say "fetch-main-ca.sh: --yes больше не подтверждает корень без проверки; для голого токена есть --trust-first-use"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done
[ -n "$MAIN" ] && [ -n "$OUT" ] || { usage >&2; die "--main и --out обязательны"; }
[[ "$MAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] || die "--main: нужен адрес главного (IP или имя, можно с портом), получено: $MAIN"
[[ "$K3S_PORT" =~ ^[0-9]{1,5}$ ]] || die "--k3s-port: нужен номер порта, получено: $K3S_PORT"
HOST_ONLY="${MAIN%%:*}"

token=""
if [ -n "$TOKEN_FILE" ]; then
  [ -r "$TOKEN_FILE" ] || die "--token-file $TOKEN_FILE не читается"
  token="$(tr -d ' \r\n' <"$TOKEN_FILE")"
else
  token="${K3S_TOKEN:-${CLUSTER_TOKEN:-}}"
fi

W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT

# fetch URL FILE: HTTP-код (000: нет соединения); сертификат здесь ещё не проверяется: проверяется содержимое
fetch() { curl -sS -k --connect-timeout 5 -m 20 -o "$2" -w '%{http_code}' "$1" 2>/dev/null || true; }
fingerprint() { openssl x509 -noout -fingerprint -sha256 -in "$1" 2>/dev/null | sed 's/^.*=//'; }
# der FILE: DER первого сертификата PEM-файла в stdout (для побайтного сравнения)
der() { openssl x509 -outform DER -in "$1" 2>/dev/null; }

code="$(fetch "https://$MAIN/ca.crt" "$W/ca.crt")"
case "$code" in
  200) ;;
  404) say "https://$MAIN/ca.crt: главный не отдаёт свой корень (у него сертификат, который проверяется системными корнями)"; exit 4 ;;
  *) die "https://$MAIN/ca.crt недоступен (HTTP ${code:-нет}): главный не запущен, порт 443 закрыт или адрес неверен" ;;
esac
grep -q 'BEGIN CERTIFICATE' "$W/ca.crt" && [ -n "$(fingerprint "$W/ca.crt")" ] || die "https://$MAIN/ca.crt: это не сертификат PEM"
fp="$(fingerprint "$W/ca.crt")"

save() {
  mkdir -p "$(dirname "$OUT")"
  cp "$W/ca.crt" "$OUT.tmp"
  chmod 644 "$OUT.tmp"
  mv -f "$OUT.tmp" "$OUT"
}

tofu() { # tofu REASON
  say "Проверить корень главного нечем: $1."
  say "  Отпечаток SHA-256 полученного сертификата: $fp"
  say "  Сверьте его на главном: docker exec llm-main openssl x509 -noout -fingerprint -sha256 -in /var/lib/llm/caddy-data/caddy/pki/authorities/local/root.crt"
  if [ "$YES" != 1 ]; then
    say "  Доверие при первом использовании нужно подтвердить (--trust-first-use). Надёжнее: токен вида K10...::node:... со страницы «Подключение ноды»."
    exit 3
  fi
  save
  say "Корень главного принят без проверки по токену (подтверждено --trust-first-use): $OUT"
  exit 0
}

if ! [[ "$token" =~ ^K10([0-9a-f]{64}):: ]]; then
  tofu "токен без хеша CA (не вида K10<sha256>::...)"
fi
want="${BASH_REMATCH[1]}"

code="$(fetch "https://$HOST_ONLY:$K3S_PORT/cacerts" "$W/cacerts")"
[ "$code" = 200 ] || die "https://$HOST_ONLY:$K3S_PORT/cacerts недоступен (HTTP ${code:-нет}): порт k3s $K3S_PORT закрыт или главный не запущен"
got="$(sha256sum <"$W/cacerts" | cut -c1-64)"
[ "$got" = "$want" ] || die "CA k3s главного не совпал с хешем в токене (получен sha256 $got, в токене $want): подмена в сети или токен от другого кластера. Корень не сохранён."

code="$(fetch "https://$MAIN/ca.crt.p7s" "$W/ca.crt.p7s")"
case "$code" in
  200) ;;
  # с проверенным токеном K10 подпись обязательна: MITM на 443 мог бы ответить 404 только на .p7s и пропустить 6443
  404) die "https://$MAIN/ca.crt.p7s нет (главный только запущен и ещё не подписал корень: повторите через минуту; или главный старше SEC-2: обновите главный). Корень не сохранён." ;;
  *) die "https://$MAIN/ca.crt.p7s недоступен (HTTP ${code:-нет})" ;;
esac
openssl cms -verify -binary -inform DER -in "$W/ca.crt.p7s" -content "$W/ca.crt" -CAfile "$W/cacerts" -purpose any \
  -signer "$W/signer.pem" -out /dev/null >/dev/null 2>&1 \
  || die "подпись https://$MAIN/ca.crt не сходится с CA k3s главного: подмена в сети. Корень не сохранён."
der "$W/signer.pem" >"$W/signer.der"
der "$W/cacerts" >"$W/k3s.der"
[ -s "$W/signer.der" ] && cmp -s "$W/signer.der" "$W/k3s.der" \
  || die "https://$MAIN/ca.crt подписан не CA k3s главного, а другим сертификатом: подмена в сети. Корень не сохранён."
save
say "Корень главного проверен по токену (CA k3s sha256 $want), отпечаток SHA-256 $fp: $OUT"
