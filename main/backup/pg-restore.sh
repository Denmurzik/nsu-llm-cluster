#!/usr/bin/env bash
# Восстановление баз portal и litellm из набора копий pg-backup.sh. Запускается в контейнере главного сервера:
#   docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh --list
#   docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh [--yes] latest|<время набора>
#
#   --list   показать наборы копий (время, размер) и выйти
#   --yes    не спрашивать подтверждение
#   latest   самый новый набор; иначе время из имени файла, например 20261103T211700Z
#
# Что делает: проверяет, что набор полный и что каждый файл читается (pg_restore --list); загружает копии во временные
# базы <база>_restore_new, пока LiteLLM и портал ещё работают (любая ошибка здесь не трогает рабочие базы и ничего не
# останавливает); останавливает LiteLLM и портал; снимает страховочную копию текущих баз в $BACKUP_DIR/pre-restore-<время>/
# (проверяет её и оставляет PRE_RESTORE_KEEP последних, по умолчанию 3); меняет базы местами переименованием (старая база
# становится <база>_replaced и удаляется после обмена); запускает LiteLLM и портал. Если обмен не удался, уже
# переименованные базы возвращаются назад; то же при SIGINT/SIGTERM посреди обмена. Если вернуть не удалось, LiteLLM и
# портал остаются остановленными, старые базы остаются как <база>_replaced, а скрипт называет страховочную копию.
# Следующий запуск при таких остатках отказывается работать и говорит, как их посмотреть и удалить. Нужно место под вторую
# копию баз. Миграции портала при старте приводят схему к текущей версии.
# Роли и пароли Postgres не восстанавливаются: они создаются при первом запуске из secrets.env (его надо хранить отдельно).
#
# Окружение: PRE_RESTORE_KEEP. Для тестов: KUBECTL ("k3s kubectl"), SUPERVISORCTL, BACKUP_DIR, DATABASES, PG_POD, NAMESPACE, SKIP_WAIT=1.
set -euo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/lib/llm/backups}"
KUBECTL="${KUBECTL:-k3s kubectl}"
SUPERVISORCTL="${SUPERVISORCTL:-supervisorctl -c /etc/llm/supervisord.conf}"
NAMESPACE="${NAMESPACE:-llm}"
PG_POD="${PG_POD:-postgres-0}"
DATABASES="${DATABASES:-portal litellm}"
PG_USER=postgres
PRE_KEEP="${PRE_RESTORE_KEEP:-3}"
case "$PRE_KEEP" in '' | *[!0-9]* | 0) echo "PRE_RESTORE_KEEP: нужно целое число не меньше 1, получено: $PRE_KEEP" >&2; exit 2 ;; esac

die() { echo "Ошибка: $*" >&2; exit 1; }
usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

LIST=0 YES=0 TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --yes) YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) usage >&2; die "неизвестный параметр: $1" ;;
    *) [ -z "$TARGET" ] || die "набор указан дважды: $TARGET и $1"; TARGET="$1"; shift ;;
  esac
done

# наборы, в которых есть копия каждой базы, от новых к старым
sets() {
  local ts db ok
  [ -d "$BACKUP_DIR" ] || return 0
  for ts in $(ls -1 "$BACKUP_DIR" | sed -n 's/^[a-z][a-z0-9_]*-\([0-9]\{8\}T[0-9]\{6\}Z\)\.dump$/\1/p' | sort -ru); do
    ok=1
    for db in $DATABASES; do [ -f "$BACKUP_DIR/$db-$ts.dump" ] || ok=0; done
    [ "$ok" = 1 ] && echo "$ts"
  done
  return 0
}

if [ "$LIST" = 1 ]; then
  found=0
  for ts in $(sets); do
    size=0
    for db in $DATABASES; do size=$((size + $(stat -c %s "$BACKUP_DIR/$db-$ts.dump"))); done
    printf '%s  %s байт\n' "$ts" "$size"
    found=1
  done
  [ "$found" = 1 ] || echo "В $BACKUP_DIR нет полных наборов копий."
  exit 0
fi

[ -n "$TARGET" ] || { usage >&2; die "укажите набор: latest или время из списка (--list)"; }
if [ "$TARGET" = latest ]; then
  TARGET="$(sets | sed -n 1p)"
  [ -n "$TARGET" ] || die "в $BACKUP_DIR нет полных наборов копий"
fi
[[ "$TARGET" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "время набора вида 20261103T211700Z, получено: $TARGET"
for db in $DATABASES; do
  [ -f "$BACKUP_DIR/$db-$TARGET.dump" ] || die "нет файла $BACKUP_DIR/$db-$TARGET.dump: набор неполный или его нет (--list)"
done

if [ "$YES" != 1 ]; then
  echo "Базы $DATABASES будут заменены копией $TARGET. Всё, что записано после неё, пропадёт."
  echo "Перед заменой снимается страховочная копия текущих баз, LiteLLM и портал на время остановятся."
  read -r -p "Продолжить? [y/N] " answer
  [ "$answer" = y ] || [ "$answer" = Y ] || { echo "Отменено."; exit 1; }
fi

# shellcheck disable=SC2086
kube() { $KUBECTL -n "$NAMESPACE" "$@"; }
pg() { kube exec -i "$PG_POD" -- "$@"; }
psql_pg() { pg psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 "$@"; }
list_ok() { pg pg_restore --list <"$1" >/dev/null 2>&1; } # архив читается: оглавление разбирается

WRITERS_STOPPED=0 KEEP_STOPPED=0
start_writers() {
  [ "$WRITERS_STOPPED" = 1 ] || return 0
  if [ "$KEEP_STOPPED" = 1 ]; then
    echo "LiteLLM и портал оставлены остановленными: базы в неопределённом состоянии. Запуск после ручной проверки:" >&2
    echo "  kubectl -n $NAMESPACE scale deployment/litellm --replicas=1; supervisorctl start portal portal-controllers" >&2
    return 0
  fi
  WRITERS_STOPPED=0
  echo "Запуск LiteLLM и портала"
  kube scale deployment/litellm --replicas=1 || echo "Не удалось запустить LiteLLM: kubectl -n $NAMESPACE scale deployment/litellm --replicas=1" >&2
  # shellcheck disable=SC2086
  $SUPERVISORCTL start portal portal-controllers || echo "Не удалось запустить портал: supervisorctl start portal portal-controllers" >&2
}

SAFETY="$BACKUP_DIR/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
SWAP_ACTIVE=0
drop_scratch() { # временные базы убираются при любом выходе; их содержимое не нужно никому
  local db
  for db in $DATABASES; do
    psql_pg -c "DROP DATABASE IF EXISTS ${db}_restore_new WITH (FORCE)" >/dev/null 2>&1 || true
  done
}
# Вернуть базы в состояние до обмена по фактическому состоянию сервера (а не по записям скрипта: сигнал мог прийти после
# фиксации переименования, но до того, как скрипт это заметил). Есть <база>_replaced: обмен прошёл, вернуть назад. Нет:
# рабочая база на месте, но могла остаться закрытой для подключений, открыть её. Возвращает 1, если что-то не удалось.
rollback_swap() {
  local db rc=0 has
  for db in $DATABASES; do
    if ! has="$(psql_pg -tAq -c "SELECT count(*) FROM pg_database WHERE datname = '${db}_replaced'" 2>/dev/null | tr -d '[:space:]')"; then
      echo "Не удалось проверить состояние базы $db" >&2; rc=1; continue
    fi
    if [ "$has" = 1 ]; then
      swap_back "$db" >/dev/null || { echo "Не удалось вернуть базу $db: прежняя база осталась под именем ${db}_replaced" >&2; rc=1; }
    else
      psql_pg -c "ALTER DATABASE $db ALLOW_CONNECTIONS true" >/dev/null 2>&1 || { echo "Не удалось открыть базу $db для подключений" >&2; rc=1; }
    fi
  done
  return "$rc"
}
on_exit() {
  local rc=$?
  trap - EXIT
  trap '' INT TERM # возврат баз не прерывается повторным сигналом
  if [ "$SWAP_ACTIVE" = 1 ]; then
    SWAP_ACTIVE=0
    echo "Обмен баз прерван, базы возвращаются" >&2
    rollback_swap || { KEEP_STOPPED=1; echo "Возврат баз не удался; возврат вручную из $SAFETY" >&2; }
  fi
  [ "$rc" = 0 ] || drop_scratch
  start_writers
  if [ "$rc" != 0 ] && [ -d "$SAFETY" ]; then
    echo "Восстановление не завершилось. Страховочная копия баз до начала: $SAFETY" >&2
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Проверка Postgres"
pg pg_isready -h 127.0.0.1 -U "$PG_USER" >/dev/null || die "Postgres не отвечает (kubectl -n $NAMESPACE get pods)"

# <база>_replaced остаётся только если прошлый запуск не смог вернуть базы назад: в ней прежние данные, которые больше нигде
# не лежат целиком. Её не стирают молча.
LEFT=""
for db in $DATABASES; do
  [ "$(psql_pg -tAq -c "SELECT count(*) FROM pg_database WHERE datname = '${db}_replaced'" | tr -d '[:space:]')" = 1 ] && LEFT="$LEFT $db"
done
if [ -n "$LEFT" ]; then
  echo "Ошибка: на сервере остались базы прошлого восстановления, которое не смогло вернуть базы назад:" >&2
  for db in $LEFT; do echo "  ${db}_replaced" >&2; done
  echo "В них могут быть данные, которых нет больше нигде; ничего не тронуто. Страховочные копии прошлых запусков: $BACKUP_DIR/pre-restore-*" >&2
  for db in $LEFT; do
    echo "Посмотреть ${db}_replaced:   $KUBECTL -n $NAMESPACE exec -i $PG_POD -- psql -U $PG_USER -d ${db}_replaced -c '\dt'" >&2
    echo "Удалить, когда не нужна:     $KUBECTL -n $NAMESPACE exec -i $PG_POD -- psql -U $PG_USER -d postgres -c 'DROP DATABASE ${db}_replaced WITH (FORCE)'" >&2
  done
  exit 1
fi

echo "Проверка файлов набора $TARGET"
for db in $DATABASES; do
  list_ok "$BACKUP_DIR/$db-$TARGET.dump" || die "файл $BACKUP_DIR/$db-$TARGET.dump повреждён (pg_restore --list не читает его); ничего не тронуто. Возьмите другой набор: --list"
done

# копии загружаются во временные базы: рабочие базы и сервисы не тронуты, пока всё не загружено без ошибок
for db in $DATABASES; do
  echo "База $db: загрузка копии $TARGET во временную базу ${db}_restore_new"
  psql_pg -c "DROP DATABASE IF EXISTS ${db}_restore_new WITH (FORCE)" -c "CREATE DATABASE ${db}_restore_new OWNER $db" >/dev/null
  pg pg_restore -U "$PG_USER" --dbname="${db}_restore_new" --no-owner --role="$db" --exit-on-error <"$BACKUP_DIR/$db-$TARGET.dump"     || die "копия $db не загружается ($BACKUP_DIR/$db-$TARGET.dump): рабочие базы и сервисы не тронуты"
done

echo "Остановка LiteLLM и портала"
WRITERS_STOPPED=1
kube scale deployment/litellm --replicas=0
if [ "${SKIP_WAIT:-}" != 1 ]; then
  kube wait --for=delete pod -l app.kubernetes.io/name=litellm --timeout=120s
fi
# shellcheck disable=SC2086
$SUPERVISORCTL stop portal portal-controllers

# страховочная копия снимается после остановки: в неё попадает всё, что было записано до последней секунды
echo "Страховочная копия текущих баз: $SAFETY"
mkdir -p "$SAFETY"
for db in $DATABASES; do
  pg pg_dump --format=custom -U "$PG_USER" --dbname="$db" >"$SAFETY/$db.dump"
  [ -s "$SAFETY/$db.dump" ] && list_ok "$SAFETY/$db.dump" || die "страховочная копия базы $db не читается; базы не тронуты: $SAFETY/$db.dump"
done
# храним PRE_RESTORE_KEEP последних страховочных копий; имена содержат время, сортировка по имени = по времени
for old in $(ls -1d "$BACKUP_DIR"/pre-restore-* 2>/dev/null | sort -r | tail -n +"$((PRE_KEEP + 1))"); do
  [ "$old" = "$SAFETY" ] || rm -rf "$old"
done

swap_back() { # swap_back БАЗА: вернуть рабочую базу, которая уже заменена
  psql_pg -c "ALTER DATABASE $1 RENAME TO ${1}_restore_new; ALTER DATABASE ${1}_replaced RENAME TO $1; ALTER DATABASE $1 ALLOW_CONNECTIONS true"
}
SWAP_ACTIVE=1 # с этого места любой выход (ошибка, SIGINT, SIGTERM) возвращает базы назад, см. on_exit
for db in $DATABASES; do
  echo "База $db: замена рабочей базы загруженной копией"
  # закрытие базы для новых подключений и сброс старых: отдельными запросами, чтобы они вступили в силу сразу, а не при фиксации обмена
  psql_pg -c "ALTER DATABASE $db ALLOW_CONNECTIONS false" >/dev/null || die "не удалось закрыть базу $db для подключений; базы возвращаются, работают прежние"
  psql_pg -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db' AND pid <> pg_backend_pid()" >/dev/null     || die "не удалось сбросить подключения к базе $db; базы возвращаются, работают прежние"
  # обмен именами одним запросом (одна транзакция): либо обе пары имён изменены, либо ни одна
  psql_pg -c "ALTER DATABASE $db RENAME TO ${db}_replaced; ALTER DATABASE ${db}_restore_new RENAME TO $db" >/dev/null     || die "замена базы $db не удалась; базы возвращаются, работают прежние"
done
SWAP_ACTIVE=0
for db in $DATABASES; do
  psql_pg -c "DROP DATABASE IF EXISTS ${db}_replaced WITH (FORCE)" >/dev/null || echo "Не удалось удалить старую базу ${db}_replaced; можно удалить вручную" >&2
done

start_writers
echo "Готово: базы $DATABASES восстановлены из набора $TARGET. Страховочная копия: $SAFETY"
echo "Проверка: curl -sk https://localhost/readyz (через минуту после запуска)."
