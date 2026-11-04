#!/bin/sh
# Ежедневная копия баз Postgres главного сервера. Запускается заданием postgres-backup (CronJob, образ postgres:17-alpine)
# и вручную: kubectl -n llm create job --from=cronjob/postgres-backup backup-now.
#
# Для каждой базы из BACKUP_DBS пишет `pg_dump -Fc` в $BACKUP_DIR/<база>-<время UTC>.dump (набор файлов одного запуска
# имеет одно время) и оставляет BACKUP_KEEP последних наборов. Новые файлы появляются под своими именами только после того,
# как записаны и проверены все базы; при ошибке старые копии не удаляются, а недописанные файлы этого запуска убираются.
#
# Окружение: PGHOST, PGUSER, PGPASSWORD (подключение), BACKUP_DIR, BACKUP_KEEP (по умолчанию 14), BACKUP_DBS
# (по умолчанию "portal litellm"), BACKUP_TS (только для тестов: время набора вместо текущего).
set -eu
umask 077

: "${BACKUP_DIR:?BACKUP_DIR не задан}"
KEEP="${BACKUP_KEEP:-14}"
DBS="${BACKUP_DBS:-portal litellm}"
TS="${BACKUP_TS:-$(date -u +%Y%m%dT%H%M%SZ)}"

case "$KEEP" in '' | *[!0-9]* | 0) echo "BACKUP_KEEP: нужно целое число не меньше 1, получено: $KEEP" >&2; exit 2 ;; esac

mkdir -p "$BACKUP_DIR"
# недописанные файлы одного запуска называются <база>-<время>.dump.<pid>.part: параллельный запуск (вручную при работающем
# по расписанию) не трогает чужие. Старше суток .part остался от упавшего запуска и удаляется.
find "$BACKUP_DIR" -maxdepth 1 -name '*.part' -mmin +1440 -exec rm -f {} + 2>/dev/null || true

cleanup() { rm -f "$BACKUP_DIR"/*-"$TS".dump."$$".part; }
trap cleanup EXIT

for db in $DBS; do
  echo "копия базы $db"
  pg_dump --format=custom --dbname="$db" --file="$BACKUP_DIR/$db-$TS.dump.$$.part"
  # архив должен читаться: pg_restore --list разбирает оглавление и падает на повреждённом файле
  pg_restore --list "$BACKUP_DIR/$db-$TS.dump.$$.part" >/dev/null
done
for db in $DBS; do
  mv "$BACKUP_DIR/$db-$TS.dump.$$.part" "$BACKUP_DIR/$db-$TS.dump"
done

# хранение: наборы (одно время) от новых к старым, лишние старые удаляются целиком
old="$(ls -1 "$BACKUP_DIR" | sed -n 's/^[a-z][a-z0-9_]*-\([0-9]\{8\}T[0-9]\{6\}Z\)\.dump$/\1/p' | sort -ru | tail -n +"$((KEEP + 1))")"
for ts in $old; do
  echo "удаляется набор $ts"
  rm -f "$BACKUP_DIR"/*-"$ts".dump
done
echo "готово: набор $TS, хранится не более $KEEP"
