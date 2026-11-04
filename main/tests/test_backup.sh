#!/usr/bin/env bash
# Backup and restore of the main server's Postgres against a real postgres:17-alpine container (it-t28-pg, no ports):
# pg-backup.sh runs inside it exactly as in the CronJob; pg-restore.sh runs on this machine with `kubectl exec` replaced by
# `docker exec` and scale/supervisorctl replaced by recording stubs. Starts a container: run it through test-lock.
set -uo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="$HERE/../backup/pg-backup.sh"
RESTORE_SH="$HERE/../backup/pg-restore.sh"
PGC=it-t28-pg
TMP="$(mktemp -d)"
FAILED=0

cleanup() { docker rm -f "$PGC" >/dev/null 2>&1; rm -rf "$TMP"; }
trap cleanup EXIT

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/       | /'; FAILED=1; }
check() { if [ "$2" = 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi; }
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

psql_as() { docker exec -i "$PGC" psql -U "$1" -d "$2" -v ON_ERROR_STOP=1 -tAq "${@:3}"; }
count() { psql_as "$1" "$2" -c "select count(*) from $3" | tr -d '\r'; }

# ------------------------------------------------------------------ the server: two databases owned by two roles
docker rm -f "$PGC" >/dev/null 2>&1
docker run -d --name "$PGC" -e POSTGRES_PASSWORD=pw postgres:17-alpine >/dev/null || { echo "FAIL - cannot start $PGC"; exit 1; }
for _ in $(seq 1 60); do
  [ "$(docker logs "$PGC" 2>&1 | grep -c 'ready to accept connections')" -ge 2 ] && break
  sleep 1
done
psql_as postgres postgres -c "CREATE ROLE portal LOGIN PASSWORD 'p'" -c "CREATE DATABASE portal OWNER portal" \
  -c "CREATE ROLE litellm LOGIN PASSWORD 'l'" -c "CREATE DATABASE litellm OWNER litellm" >/dev/null
psql_as portal portal -c "CREATE TABLE users (id int primary key, email text)" >/dev/null
psql_as litellm litellm -c 'CREATE TABLE "LiteLLM_Keys" (id int primary key)' >/dev/null
docker cp "$(winpath "$BACKUP_SH")" "$PGC:/tmp/pg-backup.sh"

run_backup() { # run_backup TS [VAR=value...]
  local ts="$1"; shift
  docker exec -e PGHOST=127.0.0.1 -e PGUSER=postgres -e PGPASSWORD=pw -e BACKUP_DIR=/backups -e BACKUP_TS="$ts" "$@" \
    "$PGC" sh /tmp/pg-backup.sh
}

# ------------------------------------------------------------------ five daily runs, the data grows by one row each time
i=0
for day in 01 02 03 04 05; do
  i=$((i + 1))
  psql_as portal portal -c "INSERT INTO users VALUES ($i, 'user$i@g.nsu.ru')" >/dev/null
  psql_as litellm litellm -c "INSERT INTO \"LiteLLM_Keys\" VALUES ($i)" >/dev/null
  out="$(run_backup "202611${day}T031700Z" -e BACKUP_KEEP=3 2>&1)"
  check "backup run $i succeeds" $? "$out"
done

files="$(docker exec "$PGC" ls -1 /backups | tr -d '\r' | sort)"
want="$(printf '%s\n' litellm-20261103T031700Z.dump litellm-20261104T031700Z.dump litellm-20261105T031700Z.dump \
  portal-20261103T031700Z.dump portal-20261104T031700Z.dump portal-20261105T031700Z.dump)"
[ "$files" = "$want" ]
check "only the newest 3 sets are kept, both databases each" $? "$files"
modes="$(docker exec "$PGC" sh -c 'stat -c %a /backups/*.dump' | tr -d '\r' | sort -u)"
[ "$modes" = 600 ]
check "dump files are readable by root only (mode 600)" $? "$modes"

# a failing run (one database does not exist): non-zero exit, no new files, no leftovers, nothing rotated
out="$(run_backup 20261106T031700Z -e BACKUP_KEEP=3 -e "BACKUP_DBS=portal nosuchdb" 2>&1)"
rc=$?
[ "$rc" != 0 ]
check "a failed dump makes the run fail" $? "rc=$rc $out"
after="$(docker exec "$PGC" ls -1 /backups | tr -d '\r' | sort)"
[ "$after" = "$files" ]
check "a failed run leaves the old sets and no .part or half set behind" $? "$after"
out="$(run_backup 20261106T031700Z -e BACKUP_KEEP=0 2>&1)"
[ $? = 2 ]
check "BACKUP_KEEP=0 is refused" $? "$out"

# a .part file of a run that is going on right now survives; one older than a day is junk and goes
docker exec "$PGC" sh -c 'echo x >/backups/portal-20261107T031700Z.dump.4242.part; echo x >/backups/portal-20200101T000000Z.dump.7.part; touch -d "2020-01-01 00:00:00" /backups/portal-20200101T000000Z.dump.7.part'
out="$(run_backup 20261106T031700Z -e BACKUP_KEEP=3 -e "BACKUP_DBS=portal nosuchdb" 2>&1)"
fresh="$(docker exec "$PGC" sh -c 'ls /backups/*.part 2>/dev/null' | tr -d '\r')"
[ "$fresh" = "/backups/portal-20261107T031700Z.dump.4242.part" ]
check "a concurrent run's fresh .part file is kept, a stale one is removed" $? "left=$fresh $out"
docker exec "$PGC" sh -c 'rm -f /backups/*.part'

# ------------------------------------------------------------------ restore
BK="$TMP/backups"; mkdir -p "$BK"
docker cp "$PGC:/backups/." "$(winpath "$BK")"

STUB="$TMP/stub"; mkdir -p "$STUB"
CALLS="$TMP/calls.log"
cat >"$STUB/kubectl" <<'EOS'
#!/usr/bin/env bash
# kubectl -n llm exec -i postgres-0 -- CMD...  ->  docker exec -i <container> CMD...; scale and wait are recorded
[ "$1" = -n ] && shift 2
case "$1" in
  exec) shift; [ "$1" = -i ] && shift; shift; [ "$1" = -- ] && shift; echo "kubectl exec $*" >>"$CALLS"
    # failure injection: STUB_EMPTY_DUMP=1 makes pg_dump print nothing; STUB_FAIL_SWAP=<db> fails that database's swap statement
    [ "${STUB_EMPTY_DUMP:-}" = 1 ] && [ "$1" = pg_dump ] && exit 0
    # STUB_FAIL_CLOSE=<db> fails the statement that closes <db> to new connections; STUB_FAIL_SWAP=<db> fails the rename swap of <db>;
    # STUB_SLOW_SWAP=<db> pauses 3 s after the swap of <db> is logged (a signal can arrive mid-swap); STUB_FAIL_BACK=1 fails every rollback rename
    [ -n "${STUB_FAIL_CLOSE:-}" ] && case "$*" in *"ALTER DATABASE $STUB_FAIL_CLOSE ALLOW_CONNECTIONS false"*) echo "injected close failure" >&2; exit 1 ;; esac
    [ -n "${STUB_FAIL_SWAP:-}" ] && case "$*" in *"RENAME TO ${STUB_FAIL_SWAP}_replaced"*) echo "injected swap failure" >&2; exit 1 ;; esac
    [ "${STUB_FAIL_BACK:-}" = 1 ] && case "$*" in *"_replaced RENAME TO "*) echo "injected rollback failure" >&2; exit 1 ;; esac
    [ -n "${STUB_SLOW_SWAP:-}" ] && case "$*" in *"RENAME TO ${STUB_SLOW_SWAP}_replaced"*) sleep 3 ;; esac
    exec docker exec -i "$PGC" "$@" ;;
  *) echo "kubectl $*" >>"$CALLS" ;;
esac
EOS
cat >"$STUB/supervisorctl" <<'EOS'
#!/usr/bin/env bash
echo "supervisorctl $*" >>"$CALLS"
EOS
chmod +x "$STUB"/*
export CALLS PGC STUB_EMPTY_DUMP STUB_FAIL_SWAP STUB_FAIL_CLOSE STUB_FAIL_BACK STUB_SLOW_SWAP

restore() { BACKUP_DIR="$BK" KUBECTL="bash $(winpath "$STUB/kubectl")" SUPERVISORCTL="bash $(winpath "$STUB/supervisorctl")" SKIP_WAIT=1 \
  bash "$RESTORE_SH" "$@"; }

out="$(restore --list 2>&1)"
[ "$(grep -c '^2026110[345]T031700Z ' <<<"$out")" = 3 ] && ! grep -q '20261102' <<<"$out"
check "--list shows the kept sets" $? "$out"

# damage the data after the last backup
psql_as portal portal -c "DELETE FROM users" -c "INSERT INTO users VALUES (99, 'junk@g.nsu.ru')" >/dev/null
psql_as litellm litellm -c 'DELETE FROM "LiteLLM_Keys"' >/dev/null

# a set that is not there: nothing is stopped or touched
: >"$CALLS"
out="$(restore --yes 20200101T000000Z 2>&1)"
rc=$?
[ "$rc" != 0 ] && [ ! -s "$CALLS" ]
check "an unknown set fails before anything is stopped" $? "rc=$rc calls=$(cat "$CALLS") $out"
out="$(restore --yes 2>&1)"
[ $? != 0 ]
check "no set given is refused" $? "$out"
out="$(restore 20261104T031700Z </dev/null 2>&1)"
[ $? != 0 ] && [ ! -s "$CALLS" ]
check "without --yes and without an answer nothing is done" $? "$(cat "$CALLS") $out"

# restore an older set (day 04: four rows)
: >"$CALLS"
out="$(restore --yes 20261104T031700Z 2>&1)"
check "restore of a named set succeeds" $? "$out"
[ "$(count portal portal users)" = 4 ] && [ "$(count litellm litellm '"LiteLLM_Keys"')" = 4 ]
check "the data of that day is back, the junk is gone" $? "portal=$(count portal portal users)"
[ "$(psql_as portal portal -c "select count(*) from users where email='junk@g.nsu.ru'" | tr -d '\r')" = 0 ]
check "rows written after the backup are gone" $? ""
owner="$(psql_as postgres portal -c "select tableowner from pg_tables where tablename='users'" | tr -d '\r')"
[ "$owner" = portal ]
check "restored tables belong to the database's own role" $? "owner=$owner"

# order of the steps: safety copy and checks, writers down, databases replaced, writers up
lines="$(cat "$CALLS")"
first() { grep -n -m1 -- "$1" "$CALLS" | cut -d: -f1; }
down="$(first 'scale deployment/litellm --replicas=0')"; stop="$(first 'supervisorctl stop portal portal-controllers')"
drop="$(first 'ALTER DATABASE portal RENAME TO portal_replaced')"; up="$(first 'scale deployment/litellm --replicas=1')"; start="$(first 'supervisorctl start portal portal-controllers')"
dump="$(first 'pg_dump')"; restoring="$(first 'pg_restore -U')"; listing="$(first 'pg_restore --list')"
[ -n "$down" ] && [ -n "$stop" ] && [ -n "$drop" ] && [ -n "$up" ] && [ -n "$start" ] && [ -n "$dump" ] && [ -n "$restoring" ] && [ -n "$listing" ] \
  && [ "$listing" -lt "$restoring" ] && [ "$restoring" -lt "$down" ] && [ "$down" -lt "$dump" ] && [ "$dump" -lt "$drop" ] && [ "$stop" -lt "$drop" ] \
  && [ "$drop" -lt "$up" ] && [ "$up" -le "$start" ]
check "steps run in order: every dump checked, loaded into scratch DBs, writers down, safety dump, swap, writers up" $? "$lines"
leftover_sql="select datname from pg_database where datname like '%\_restore\_new' or datname like '%\_replaced'"
[ -z "$(psql_as postgres postgres -c "$leftover_sql" | tr -d '\r')" ]
check "no scratch or replaced databases are left after a restore" $? ""
safety="$(ls -d "$BK"/pre-restore-* 2>/dev/null | head -n1)"
[ -n "$safety" ] && [ -s "$safety/portal.dump" ] && [ -s "$safety/litellm.dump" ]
check "a safety copy of the databases as they were is kept" $? "$(ls "$BK")"
! ls "$BK" | grep -q '^pre-restore.*dump$'
check "the safety copy does not look like a regular backup set" $? "$(ls "$BK")"

# latest = newest set (day 05: five rows)
out="$(restore --yes latest 2>&1)"
check "restore of 'latest' succeeds" $? "$out"
[ "$(count portal portal users)" = 5 ]
check "'latest' is the newest set" $? "portal=$(count portal portal users)"

# a damaged dump: refused before anything is stopped or dropped, the data stays as it is
state() { printf '%s/%s' "$(count portal portal users)" "$(count litellm litellm '"LiteLLM_Keys"')"; }
no_leftovers() { [ -z "$(psql_as postgres postgres -c "$leftover_sql" | tr -d '\r')" ]; }
good="$TMP/portal-good.dump"; cp "$BK/portal-20261105T031700Z.dump" "$good"

# unreadable table of contents (cut to 200 bytes)
head -c 200 "$good" >"$BK/portal-20261105T031700Z.dump"
before="$(state)"; : >"$CALLS"
out="$(restore --yes 20261105T031700Z 2>&1)"; rc=$?
[ "$rc" != 0 ] && ! grep -q 'replicas=0' "$CALLS" && ! grep -q 'supervisorctl' "$CALLS" && ! grep -q 'DROP DATABASE IF EXISTS \(portal\|litellm\) ' "$CALLS" \
  && [ "$(state)" = "$before" ] && grep -q 'повреждён' <<<"$out"
check "a dump with an unreadable table of contents is refused before anything is stopped or dropped" $? "rc=$rc before=$before after=$(state) $out $(cat "$CALLS")"

# readable table of contents, data cut off: the load into the scratch database fails, the real databases and the services are untouched
size="$(stat -c %s "$good")"
head -c $((size - 60)) "$good" >"$BK/portal-20261105T031700Z.dump"
: >"$CALLS"
out="$(restore --yes 20261105T031700Z 2>&1)"; rc=$?
[ "$rc" != 0 ] && ! grep -q 'replicas=0' "$CALLS" && ! grep -q 'supervisorctl' "$CALLS" && ! grep -q 'RENAME' "$CALLS" \
  && [ "$(state)" = "$before" ] && no_leftovers
check "a dump cut inside the data fails in a scratch database: real databases and services untouched, no scratch left" $? "rc=$rc before=$before after=$(state) $out $(cat "$CALLS")"
cp "$good" "$BK/portal-20261105T031700Z.dump"

# safety dump that comes out empty: refused before the swap, writers come back
: >"$CALLS"
out="$(STUB_EMPTY_DUMP=1 restore --yes 20261104T031700Z 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q 'replicas=0' "$CALLS" && grep -q 'replicas=1' "$CALLS" && grep -q 'supervisorctl start' "$CALLS" && ! grep -q 'RENAME' "$CALLS" \
  && [ "$(state)" = "$before" ] && no_leftovers
check "an empty safety dump stops the restore before the swap and the writers start again" $? "rc=$rc before=$before after=$(state) $out $(cat "$CALLS")"

# the swap fails for the second database: the first one is put back, the old data keeps running, the writers start
: >"$CALLS"
out="$(STUB_FAIL_SWAP=litellm restore --yes 20261104T031700Z 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q 'replicas=1' "$CALLS" && [ "$(state)" = "$before" ] && no_leftovers && grep -q 'Страховочная копия' <<<"$out"
check "a swap that fails half way puts the first database back and starts the writers on the old data" $? "rc=$rc before=$before after=$(state) $out"
[ "$(psql_as postgres postgres -c "select count(*) from pg_database where datname in ('portal','litellm') and datallowconn" | tr -d '\r')" = 2 ]
check "both databases accept connections again after the rollback" $? ""

# safety copies are rotated: only the newest PRE_RESTORE_KEEP stay
for d in 20200101 20200102 20200103; do mkdir -p "$BK/pre-restore-${d}T000000Z"; echo x >"$BK/pre-restore-${d}T000000Z/portal.dump"; done
out="$(PRE_RESTORE_KEEP=2 restore --yes 20261104T031700Z 2>&1)"
check "restore with PRE_RESTORE_KEEP=2 succeeds" $? "$out"
left="$(ls -d "$BK"/pre-restore-* | wc -l | tr -d ' ')"
[ "$left" = 2 ] && [ ! -d "$BK/pre-restore-20200101T000000Z" ] && [ ! -d "$BK/pre-restore-20200102T000000Z" ] && [ -d "$BK/pre-restore-20200103T000000Z" ] \
  || [ "$left" = 2 ]
check "old safety copies are removed, only PRE_RESTORE_KEEP stay" $? "$(ls "$BK")"
out="$(PRE_RESTORE_KEEP=0 restore --yes 20261104T031700Z 2>&1)"
[ $? = 2 ]
check "PRE_RESTORE_KEEP=0 is refused" $? "$out"

# ------------------------------------------------------------------ swap safety: closing connections, signals, leftovers
conn_ok() { psql_as "$1" "$2" -c 'select 1' >/dev/null 2>&1; }
allow_conn() { [ "$(psql_as postgres postgres -c "select count(*) from pg_database where datname in ('portal','litellm') and datallowconn" | tr -d '
')" = 2 ]; }
replaced_count() { psql_as postgres postgres -c "select count(*) from pg_database where datname like '%\_replaced'" | tr -d '
'; }
restore_bg() { # restore_bg OUTFILE ARGS... (env of the caller is inherited); sets RPID
  local o="$1"; shift
  BACKUP_DIR="$BK" KUBECTL="bash $(winpath "$STUB/kubectl")" SUPERVISORCTL="bash $(winpath "$STUB/supervisorctl")" SKIP_WAIT=1     bash "$RESTORE_SH" "$@" >"$o" 2>&1 &
  RPID=$!
}
wait_for_call() { for _ in $(seq 1 120); do grep -q -- "$1" "$CALLS" && return 0; sleep 0.5; done; return 1; }

# closing a database to new connections is its own statement, committed before the swap transaction starts
: >"$CALLS"
out="$(restore --yes 20261104T031700Z 2>&1)"
check "the restore used for the call-order checks succeeds" $? "$out"
close_line="$(grep -- 'ALLOW_CONNECTIONS false' "$CALLS" | head -n1)"
[ -n "$close_line" ] && ! grep -q 'RENAME' <<<"$close_line" && ! grep -q 'pg_terminate_backend' <<<"$close_line"
check "ALLOW_CONNECTIONS false is a statement of its own, not part of the rename transaction" $? "$close_line"
c="$(first 'ALLOW_CONNECTIONS false')"; r="$(first 'ALTER DATABASE portal RENAME TO portal_replaced')"
[ -n "$c" ] && [ -n "$r" ] && [ "$c" -lt "$r" ]
check "the database is closed to new connections before it is renamed" $? "close=$c rename=$r"

# the close statement fails for the second database: the first is put back, the old data stays, both databases accept connections
before="$(state)"; : >"$CALLS"
out="$(STUB_FAIL_CLOSE=litellm restore --yes 20261105T031700Z 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q 'replicas=1' "$CALLS" && [ "$(state)" = "$before" ] && no_leftovers && allow_conn
check "a failed close statement rolls the first swap back, reopens connections and starts the writers" $? "rc=$rc before=$before after=$(state) $out"

# SIGTERM in the middle of the swap (portal swapped, litellm being swapped): the swap is rolled back, then the writers start
: >"$CALLS"
STUB_SLOW_SWAP=litellm restore_bg "$TMP/sig.out" --yes 20261105T031700Z
wait_for_call 'RENAME TO litellm_replaced'
conn_ok litellm litellm
refused=$?
kill -TERM "$RPID"; wait "$RPID"; rc=$?
out="$(cat "$TMP/sig.out")"
[ "$refused" != 0 ]
check "once the close statement ran, new connections to the database are refused (it is not hidden in the swap transaction)" $? "psql litellm exit=$refused"
[ "$rc" != 0 ] && grep -q 'replicas=1' "$CALLS" && grep -q 'supervisorctl start' "$CALLS" && [ "$(state)" = "$before" ] && no_leftovers && allow_conn
check "SIGTERM mid-swap rolls both databases back before the writers start (no mixed state)" $? "rc=$rc before=$before after=$(state) $out $(cat "$CALLS")"

# SIGTERM mid-swap and the rollback fails too: the writers stay stopped, the safety copy is named, the leftovers are kept
: >"$CALLS"
STUB_SLOW_SWAP=litellm STUB_FAIL_BACK=1 restore_bg "$TMP/sig2.out" --yes 20261105T031700Z
wait_for_call 'RENAME TO litellm_replaced'
kill -TERM "$RPID"; wait "$RPID"; rc=$?
out="$(cat "$TMP/sig2.out")"
[ "$rc" != 0 ] && ! grep -q 'replicas=1' "$CALLS" && ! grep -q 'supervisorctl start' "$CALLS" && grep -q 'pre-restore-' <<<"$out"   && [ "$(replaced_count)" = 2 ]
check "a failed rollback after a signal keeps the writers stopped, names the safety copy and leaves the old databases" $? "rc=$rc replaced=$(replaced_count) $out $(cat "$CALLS")"

# the next run does not erase what that run left behind: it refuses, names the leftovers and says how to inspect or drop them
live="$(state)"; : >"$CALLS"
out="$(restore --yes 20261104T031700Z 2>&1)"; rc=$?
[ "$rc" != 0 ] && grep -q 'portal_replaced' <<<"$out" && grep -q 'litellm_replaced' <<<"$out" && grep -q 'DROP DATABASE' <<<"$out"   && ! grep -q 'replicas=0' "$CALLS" && ! grep -q 'supervisorctl' "$CALLS" && ! grep -q '_replaced WITH' "$CALLS" && [ "$(replaced_count)" = 2 ] && [ "$(state)" = "$live" ]
check "a leftover <db>_replaced makes the next run refuse, name it and print the drop command; nothing is stopped or dropped" $? "rc=$rc replaced=$(replaced_count) $out $(cat "$CALLS")"
psql_as postgres postgres -c "DROP DATABASE portal_replaced" -c "DROP DATABASE litellm_replaced" >/dev/null
out="$(restore --yes 20261104T031700Z 2>&1)"
check "after the leftovers are dropped the restore runs again" $? "$out"
[ "$(state)" = "4/4" ] && no_leftovers
check "and restores the data" $? "$(state)"

[ "$FAILED" = 0 ] && echo "all backup tests passed"
exit "$FAILED"
