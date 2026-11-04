#!/usr/bin/env bash
# Переключение домена double.fresh.nsuts.ru на сервере tsga (10.4.0.105): nginx проксирует весь домен на главный сервер
# NSU LLM Cluster, LMS MTSGA останавливается. Запускается НА tsga от root (sudo).
# Использование:
#   sudo ./switch-front.sh --main <IP или имя главного> [--ca FILE] [--yes] [--dry-run]
#   sudo ./switch-front.sh --rollback [--dry-run]
#
#   --main      адрес главного: nginx ходит на https://<адрес> с SNI double.fresh.nsuts.ru
#   --ca        корневой сертификат главного (PEM), по нему nginx проверяет главный. Без него скачивается
#               https://<главный>/ca.crt (без проверки), печатается отпечаток SHA-256 и нужен ответ «y» (или --yes)
#   --yes       не спрашивать подтверждения отпечатка
#   --dry-run   ничего не менять: проверить главный и показать, что было бы сделано, и новый файл сайта
#   --rollback  вернуть LMS: файл сайта и CA из последней резервной копии, политики перезапуска и контейнеры LMS
#
# Переключение: резервная копия файла сайта и политик перезапуска в /var/backups/nsu-llm-front/<время>/, новый сайт
# (шаблон site.conf.tmpl, пути ssl_certificate из прежнего файла), nginx -t (при ошибке всё возвращается), docker update
# --restart=no и docker stop для контейнеров LMS, systemctl reload nginx, проверка https://double.fresh.nsuts.ru/readyz
# через этот nginx. Повторный запуск на уже переключённом сервере (например, с новым --main) только переписывает сайт и CA.
# Остальные контейнеры (базы deploy-db-1 и mtsga-test-db-1, pgadmin, gitlab-runner) не трогаются; пайплайн gitlab-runner
# при push в репозиторий LMS может создать её контейнеры заново.
set -Eeuo pipefail   # -E: ловушка ERR переключения срабатывает и внутри функций
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# FRONT_ROOT (только для тестов): корень, от которого берутся /etc/nginx и /var/backups
ROOT="${FRONT_ROOT:-}"
FRONT_HOST=double.fresh.nsuts.ru
SITE="$ROOT/etc/nginx/sites-enabled/$FRONT_HOST"
CA_PATH=/etc/nginx/nsu-llm-main-ca.crt          # путь в конфигурации nginx
CA_FILE="$ROOT$CA_PATH"                          # тот же файл на диске
TMP_SITE="$ROOT/etc/nginx/.nsu-llm-front-site.tmp"   # вне sites-enabled: nginx его не читает
BACKUPS="$ROOT/var/backups/nsu-llm-front"
TEMPLATE="$HERE/site.conf.tmpl"
MARKER="# nsu-llm-front: managed by switch-front.sh"
LMS_CONTAINERS=(double.fresh.nsuts double.fresh.nsuts.test daemon_server daemon_server_test)
READY_TRIES="${FRONT_READY_TRIES:-10}"
READY_SLEEP=3

MAIN=""
CA_ARG=""
ORIG_ARGS="$*"
CA_TMP=""
TMP_FILES=()
trap 'rm -f ${TMP_FILES[@]+"${TMP_FILES[@]}"}' EXIT
YES=0
DRY=0
ROLLBACK=0
SWITCH_STEP=""
RELOADED=no

say() { printf '%s\n' "$*"; }
ok() { say "  [ок] $*"; }
warn() { say "  [!]  $*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --main) MAIN="${2:-}"; shift 2 || die "--main: нужен адрес главного" ;;
    --ca) CA_ARG="${2:-}"; shift 2 || die "--ca: нужен файл" ;;
    --yes|-y) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "неизвестный параметр: $1" ;;
  esac
done

# run CMD...: при --dry-run только печатает команду; вывод команды (docker печатает имя контейнера) не нужен
run() {
  if [ "$DRY" = 1 ]; then say "  [dry-run] $(printf '%q ' "$@")"; else "$@" >/dev/null </dev/null; fi
}

tmpfile() { # tmpfile VAR: временный файл, удаляется при выходе
  local f
  f="$(mktemp)"
  TMP_FILES+=("$f")
  printf -v "$1" '%s' "$f"
}

is_managed() { [ -f "$SITE" ] && head -n1 "$SITE" | grep -qF "$MARKER"; }

latest_backup() { # каталог последней резервной копии удачного переключения (в том числе уже откаченного)
  local d last=""
  for d in "$BACKUPS"/*/; do
    [ -f "$d/complete" ] || [ -f "$d/rolled-back" ] || continue
    last="${d%/}"
  done
  printf '%s' "$last"
}

policy_str() { # policy_str NAME COUNT: политика перезапуска в виде для docker update --restart=
  case "$1" in
    ""|no) echo no ;;
    on-failure) if [ "${2:-0}" = 0 ]; then echo on-failure; else echo "on-failure:$2"; fi ;;
    *) echo "$1" ;;
  esac
}

container_state() { # container_state NAME: "<политика> <true|false>", пусто, если контейнера нет
  local info name count running
  info="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.HostConfig.RestartPolicy.MaximumRetryCount}} {{.State.Running}}' "$1" 2>/dev/null)" \
    || return 0
  read -r name count running <<<"$info"
  printf '%s %s\n' "$(policy_str "$name" "$count")" "$running"
}

# install_file SRC DEST: замена файла целиком (права 644) через временный файл в /etc/nginx, вне sites-enabled
install_file() {
  cp "$1" "$TMP_SITE"
  chmod 644 "$TMP_SITE"
  mv -f "$TMP_SITE" "$2"
}

nginx_test() {
  local out
  if out="$(nginx -t 2>&1)"; then ok "nginx -t: конфигурация верна"; return 0; fi
  say "$out" | sed 's/^/       /'
  return 1
}

container_exists() { docker inspect "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ откат
do_rollback() {
  local b line name policy running cur cur_pol cur_run fails=0 ngx_ok=1
  local -a lines=()
  b="$(latest_backup)"
  [ -n "$b" ] || die "резервных копий для отката нет в $BACKUPS (переключения не было или оно не удалось и всё вернуло само)"
  # Повторный откат разрешён: прежний сайт уже на месте (cmp с копией) или сайт ещё наш. Чужой изменённый файл не трогаем.
  local site_same=0
  if [ -f "$SITE" ] && cmp -s "$SITE" "$b/site"; then
    site_same=1
  elif ! is_managed; then
    die "файл $SITE не создан switch-front.sh и отличается от резервной копии $b/site (его изменили после отката?): откат его не перезапишет"
  fi
  say "Откат из $b"
  if [ "$site_same" = 1 ]; then
    ok "файл сайта уже прежний"
  elif [ "$DRY" = 1 ]; then
    say "  [dry-run] $SITE <- $b/site"
  else
    install_file "$b/site" "$SITE"
    ok "файл сайта восстановлен"
  fi
  if [ -f "$b/ca.crt" ]; then
    if ! cmp -s "$b/ca.crt" "$CA_FILE" 2>/dev/null; then
      if [ "$DRY" = 1 ]; then say "  [dry-run] $CA_FILE <- $b/ca.crt"; else install_file "$b/ca.crt" "$CA_FILE"; fi
    fi
  elif [ -e "$CA_FILE" ]; then
    if [ "$DRY" = 1 ]; then say "  [dry-run] удалить $CA_FILE"; else rm -f "$CA_FILE"; fi
  fi
  if [ "$DRY" = 0 ] && ! nginx_test; then
    warn "nginx -t не принимает восстановленную конфигурацию (см. выше): nginx не перезагружается"
    ngx_ok=0; fails=$((fails + 1))
  fi
  mapfile -t lines <"$b/containers"
  for line in ${lines[@]+"${lines[@]}"}; do
    read -r name policy running <<<"$line"
    [ -n "$name" ] || continue
    if [ "$policy" = absent ]; then say "  $name: контейнера не было при переключении, пропуск"; continue; fi
    cur="$(container_state "$name")"
    if [ -z "$cur" ]; then
      warn "$name: контейнер не найден (удалён или пересоздан?): политику --restart=$policy и запуск восстановите вручную"
      fails=$((fails + 1)); continue
    fi
    read -r cur_pol cur_run <<<"$cur"
    if [ "$cur_pol" != "$policy" ]; then
      if run docker update "--restart=$policy" "$name"; then ok "$name: --restart=$policy"
      else warn "$name: docker update --restart=$policy не удался"; fails=$((fails + 1)); fi
    fi
    if [ "$running" = true ] && [ "$cur_run" != true ]; then
      if run docker start "$name"; then ok "$name: запущен"
      else warn "$name: docker start не удался"; fails=$((fails + 1)); fi
    elif [ "$cur_pol" = "$policy" ]; then
      ok "$name: уже как до переключения (--restart=$policy, работает: $cur_run)"
    fi
  done
  if [ "$ngx_ok" = 1 ]; then
    if run systemctl reload nginx; then
      [ "$DRY" = 1 ] || ok "nginx перезагружен"
    else
      warn "systemctl reload nginx не удался: nginx всё ещё может проксировать на главный"; fails=$((fails + 1))
    fi
  fi
  if [ "$DRY" = 1 ]; then say "--dry-run: ничего не изменено."; return; fi
  [ "$fails" = 0 ] || die "откат выполнен не полностью: шагов с ошибкой $fails (см. [!] выше). Исправьте причину и повторите: sudo $0 --rollback (повтор безопасен)"
  [ ! -f "$b/complete" ] || mv "$b/complete" "$b/rolled-back"
  ok "домен $FRONT_HOST снова обслуживает LMS"
}

# ------------------------------------------------------------------ переключение
check_main_arg() {
  local o
  [ -n "$MAIN" ] || { usage >&2; die "нужен --main <IP или имя главного>"; }
  if [[ "$MAIN" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    for o in "${BASH_REMATCH[@]:1}"; do [ "$((10#$o))" -le 255 ] || die "--main: неверный адрес IPv4 $MAIN"; done
    [[ "$MAIN" != 127.* && "$MAIN" != 0.* ]] || die "--main $MAIN: это сам tsga, nginx проксировал бы сам на себя"
  else
    [[ "$MAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "--main должен быть адресом IPv4 или именем хоста, получено: $MAIN"
    [ "${MAIN,,}" != "$FRONT_HOST" ] && [ "${MAIN,,}" != localhost ] \
      || die "--main $MAIN указывает на сам tsga: nginx проксировал бы сам на себя. Укажите адрес главного"
  fi
  if command -v hostname >/dev/null 2>&1 && [[ " $(hostname -I 2>/dev/null || true) " == *" $MAIN "* ]]; then
    die "--main $MAIN: это адрес самого tsga, nginx проксировал бы сам на себя"
  fi
}

ca_fingerprint() { { openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null || true; } | sed 's/^[^=]*=//'; }

# ca_normalize SRC DEST WHAT: в DEST ровно один сертификат из SRC, переписанный openssl. nginx доверяет всем сертификатам
# файла, а оператор сверяет отпечаток одного: файл с несколькими блоками PEM или с чем-то кроме сертификата отвергается.
ca_normalize() {
  local src="$1" dest="$2" what="$3" clean n rest
  tmpfile clean
  tr -d '\r' <"$src" >"$clean"
  n="$(grep -c -- '-----BEGIN' "$clean" || true)"
  [ "$n" = 1 ] || die "$what: блоков PEM в файле ${n:-0}, а нужен ровно один сертификат (корень CA главного)"
  grep -qx -- '-----BEGIN CERTIFICATE-----' "$clean" && grep -qx -- '-----END CERTIFICATE-----' "$clean" \
    || die "$what: в файле не сертификат PEM"
  rest="$(sed '/^-----BEGIN CERTIFICATE-----$/,/^-----END CERTIFICATE-----$/d' "$clean" | tr -d '[:space:]')"
  [ -z "$rest" ] || die "$what: кроме сертификата в файле есть другие данные"
  openssl x509 -in "$clean" -out "$dest" 2>/dev/null || die "$what: в файле не сертификат PEM (openssl x509 его не читает)"
  [ "$(grep -c -- '-----BEGIN' "$dest" || true)" = 1 ] || die "$what: openssl x509 выдал не один сертификат"
}

# CA главного во временный файл $CA_TMP: из --ca или с https://<главный>/ca.crt с подтверждением отпечатка
get_ca() {
  local code fp ans raw
  tmpfile CA_TMP
  if [ -n "$CA_ARG" ]; then
    [ -f "$CA_ARG" ] || die "--ca: файл $CA_ARG не найден"
    ca_normalize "$CA_ARG" "$CA_TMP" "--ca $CA_ARG"
    fp="$(ca_fingerprint "$CA_TMP")"
    [ -n "$fp" ] || die "--ca: $CA_ARG не сертификат PEM"
    ok "CA главного из $CA_ARG, SHA-256 $fp"
    return
  fi
  tmpfile raw
  code="$(curl -sk --noproxy '*' -m 15 -o "$raw" -w '%{http_code}' "https://$MAIN/ca.crt" 2>/dev/null || true)"
  [ "$code" = 200 ] || die "главный не отдал https://$MAIN/ca.crt (ответ ${code:-нет}): он не запущен или работает со своим сертификатом без ca.crt. Передайте корневой сертификат: --ca FILE"
  ca_normalize "$raw" "$CA_TMP" "https://$MAIN/ca.crt"
  fp="$(ca_fingerprint "$CA_TMP")"
  [ -n "$fp" ] || die "https://$MAIN/ca.crt не сертификат PEM"
  say "  CA главного скачан с https://$MAIN/ca.crt без проверки. Отпечаток SHA-256:"
  say "      $fp"
  say "  Сверьте его на главном: curl -s http://127.0.0.1/ca.crt | openssl x509 -noout -fingerprint -sha256"
  if [ "$YES" = 1 ] || [ "$DRY" = 1 ]; then return; fi
  printf 'Отпечаток совпадает? (y/N): '
  IFS= read -r ans || ans=""
  [[ "$ans" =~ ^(y|Y|yes|д|Д|да)$ ]] || die "отпечаток не подтверждён, ничего не изменено"
}

# главный отвечает на https://double.fresh.nsuts.ru/readyz по своему адресу и проходит проверку по CA (как у nginx)
check_main() {
  local code
  code="$(curl -s --noproxy '*' -m 15 --cacert "$CA_TMP" --connect-to "$FRONT_HOST:443:$MAIN:443" -o /dev/null -w '%{http_code}' \
    "https://$FRONT_HOST/readyz" 2>/dev/null || true)"
  [ "$code" = 200 ] || die "главный https://$MAIN не готов для $FRONT_HOST (readyz: ${code:-000}; 000 — нет соединения или сертификат не прошёл проверку по CA). Проверьте на главном: PUBLIC_HOSTNAME=$FRONT_HOST, /readyz по IP, брандмауэр 443 для tsga. Ничего не изменено"
  ok "главный https://$MAIN отвечает для $FRONT_HOST, сертификат проверен по CA"
}

render_site() { # render_site OUT
  local cert key
  cert="$(sed -n 's/^[[:space:]]*ssl_certificate[[:space:]]\{1,\}\([^;[:space:]]\{1,\}\)[[:space:]]*;.*/\1/p' "$SITE" | head -n1)"
  key="$(sed -n 's/^[[:space:]]*ssl_certificate_key[[:space:]]\{1,\}\([^;[:space:]]\{1,\}\)[[:space:]]*;.*/\1/p' "$SITE" | head -n1)"
  [[ "$cert" =~ ^/[A-Za-z0-9/._-]+$ ]] && [[ "$key" =~ ^/[A-Za-z0-9/._-]+$ ]] \
    || die "в $SITE не найдены ssl_certificate и ssl_certificate_key (получено: «$cert», «$key»)"
  sed -e "s|@@HOSTNAME@@|$FRONT_HOST|g" -e "s|@@MAIN@@|$MAIN|g" -e "s|@@SSL_CERTIFICATE@@|$cert|g" \
    -e "s|@@SSL_CERTIFICATE_KEY@@|$key|g" -e "s|@@CA@@|$CA_PATH|g" "$TEMPLATE" >"$1"
  ! grep -q '@@' "$1" || die "в шаблоне $TEMPLATE остались незаполненные @@...@@"
}

backup() { # backup DIR: файл сайта, CA (если был) и политики перезапуска контейнеров LMS
  local b="$1" c info
  mkdir -p "$b"
  chmod 700 "$b"
  cp -p "$SITE" "$b/site"
  [ ! -f "$CA_FILE" ] || cp -p "$CA_FILE" "$b/ca.crt"
  : >"$b/containers"
  for c in "${LMS_CONTAINERS[@]}"; do
    info="$(container_state "$c")"
    if [ -n "$info" ]; then
      printf '%s %s\n' "$c" "$info" >>"$b/containers"
    else
      printf '%s absent -\n' "$c" >>"$b/containers"
    fi
  done
  touch "$b/complete"
}

switch_state() { # что уже изменено: для сообщения о прерванном переключении
  local c st pol run_
  say "Состояние сейчас:"
  if is_managed; then say "  сайт $SITE: новый (на главный)"; else say "  сайт $SITE: прежний (LMS)"; fi
  say "  nginx перезагружен с новым сайтом: $RELOADED"
  for c in "${LMS_CONTAINERS[@]}"; do
    st="$(container_state "$c" || true)"
    if [ -z "$st" ]; then say "  $c: контейнера нет"; continue; fi
    read -r pol run_ <<<"$st"
    if [ "$run_" = true ]; then say "  $c: работает, --restart=$pol"; else say "  $c: остановлен, --restart=$pol"; fi
  done
}

on_switch_err() { # ловушка ERR: шаг, состояние и команда отката вместо молчаливого выхода по set -e
  local rc=$? line="$1"
  trap - ERR
  set +e
  {
    say ""
    say "Ошибка: переключение прервано на шаге «$SWITCH_STEP» (код $rc, строка $line)."
    switch_state
    say "Вернуть LMS: sudo $0 --rollback"
  } >&2
  exit "$rc"
}

do_switch() {
  local first=1 b="" prev_site="" prev_ca="" c new
  check_main_arg
  [ -f "$SITE" ] || die "нет файла сайта $SITE"
  [ -f "$TEMPLATE" ] || die "нет шаблона $TEMPLATE"
  if is_managed; then first=0; fi
  say "Проверка главного $MAIN:"
  get_ca
  check_main
  tmpfile new
  render_site "$new"
  if [ "$first" = 0 ]; then
    say "Сайт уже переключён switch-front.sh: переписываются только файл сайта и CA (контейнеры LMS не трогаются)."
  fi
  for c in "$ROOT"/etc/nginx/sites-enabled/*; do
    [ "$c" != "$SITE" ] && [ -f "$c" ] || continue
    if grep -qE "server_name[[:space:]].*\b${FRONT_HOST//./\\.}\b" "$c"; then
      warn "$c тоже объявляет $FRONT_HOST: nginx берёт первый по алфавиту файл ($SITE), этот игнорирует с предупреждением «conflicting server name»"
    fi
  done

  if [ "$DRY" = 1 ]; then
    say ""
    say "Было бы выполнено (--dry-run, ничего не изменено):"
    if [ "$first" = 1 ]; then say "  1. резервная копия $SITE, CA и политик перезапуска в $BACKUPS/<время>/"; fi
    say "  2. $CA_FILE <- CA главного; $SITE <- новый сайт (ниже); nginx -t"
    if [ "$first" = 1 ]; then
      for c in "${LMS_CONTAINERS[@]}"; do
        if container_exists "$c"; then say "  3. docker update --restart=no $c; docker stop $c"; else say "  3. $c: контейнера нет, пропуск"; fi
      done
    fi
    say "  4. systemctl reload nginx"
    say "  5. проверка https://$FRONT_HOST/readyz через nginx tsga"
    say ""
    say "Новый файл сайта:"
    sed 's/^/    /' "$new"
    return
  fi

  if [ "$first" = 1 ]; then
    b="$BACKUPS/$(date +%Y%m%d-%H%M%S)"
    [ ! -e "$b" ] || b="$b-$$"
    backup "$b"
    ok "резервная копия: $b"
  else
    tmpfile prev_site; cp -p "$SITE" "$prev_site"
    if [ -f "$CA_FILE" ]; then tmpfile prev_ca; cp -p "$CA_FILE" "$prev_ca"; fi
  fi
  SWITCH_STEP="запись CA и сайта"
  trap 'on_switch_err $LINENO' ERR
  install_file "$CA_TMP" "$CA_FILE"
  install_file "$new" "$SITE"
  SWITCH_STEP="nginx -t"
  if ! nginx_test; then
    trap - ERR
    if [ "$first" = 1 ]; then
      install_file "$b/site" "$SITE"
      if [ -f "$b/ca.crt" ]; then install_file "$b/ca.crt" "$CA_FILE"; else rm -f "$CA_FILE"; fi
      mv "$b/complete" "$b/failed"
    else
      install_file "$prev_site" "$SITE"
      if [ -n "$prev_ca" ]; then install_file "$prev_ca" "$CA_FILE"; else rm -f "$CA_FILE"; fi
    fi
    die "nginx -t не принял новый сайт (см. выше): прежние файлы возвращены, nginx не перезагружен, LMS не остановлена"
  fi
  ok "новый сайт записан: $SITE -> https://$MAIN"

  if [ "$first" = 1 ]; then
    for c in "${LMS_CONTAINERS[@]}"; do
      if ! container_exists "$c"; then warn "контейнера $c нет: пропуск"; continue; fi
      SWITCH_STEP="docker update --restart=no $c"
      run docker update --restart=no "$c"
      SWITCH_STEP="docker stop $c"
      run docker stop "$c"
      ok "$c остановлен, --restart=no"
    done
  fi
  SWITCH_STEP="systemctl reload nginx"
  run systemctl reload nginx
  RELOADED=yes
  ok "nginx перезагружен"
  SWITCH_STEP="проверка /readyz через nginx tsga"

  local i code=""
  for ((i = 1; i <= READY_TRIES; i++)); do
    code="$(curl -sk --noproxy '*' -m 10 -o /dev/null -w '%{http_code}' --resolve "$FRONT_HOST:443:127.0.0.1" \
      "https://$FRONT_HOST/readyz" 2>/dev/null || true)"
    [ "$code" != 200 ] || break
    [ "$i" = "$READY_TRIES" ] || sleep "$READY_SLEEP"
  done
  [ "$code" = 200 ] || die "https://$FRONT_HOST/readyz через nginx tsga отвечает ${code:-нет ответа}. Журнал: tail -n 50 /var/log/nginx/error.log. Вернуть LMS: sudo $0 --rollback"
  ok "https://$FRONT_HOST/readyz через nginx tsga: 200"
  trap - ERR
  say ""
  say "Домен $FRONT_HOST переключён на главный https://$MAIN."
  say "Проверьте вход через NSU ID в браузере. Вернуть LMS: sudo $0 --rollback"
}

if [ -z "$ROOT" ] && [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
  die "нужны права root (nginx, docker): sudo $0 $ORIG_ARGS"
fi
if [ "$ROLLBACK" = 1 ]; then
  [ -z "$MAIN" ] || die "--rollback не сочетается с --main"
  do_rollback
else
  do_switch
fi
