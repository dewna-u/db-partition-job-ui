#!/usr/bin/env bash
# =============================================================================
# PartOps disposable-database framework test (LOCAL, THROWAWAY ONLY)
# =============================================================================
# Creates a brand-new PostgreSQL cluster with `initdb` in a temporary
# directory (localhost only, trust auth, NOT a service), installs the PartOps
# framework from this repository, runs the tests, and DESTROYS the cluster.
# It never connects to any other server and needs no production data.
#
# Requirements: existing PostgreSQL (or EDB) server binaries — it does not
# install anything. Python with this repo's requirements for the preflight.
#
# Usage:
#   PG_BIN="/c/Program Files/PostgreSQL/16/bin" \
#     scripts/disposable_db/run_disposable_db_test.sh --i-understand-this-is-disposable
#   Optional: PARTOPS_DISPOSABLE_PORT (default 55439), PARTOPS_DISPOSABLE_KEEP=1
#             (keep the data dir for debugging; the server is still stopped).
#
# Stages:
#   1 roles → 2 bootstrap → 3 captured legacy functions (if ALL present)
#   → 4 realtime_scheduler_v1.sql (needs 3) → 5 migrations (5A applied twice:
#   idempotency; pre-5C jobs inserted, then Phase 5C 20261117_01/_02 applied
#   twice) → 6 Phase 5A smoke + target-lock tests → 6c Phase 5C registry,
#   backfill and remote-target guards + routing/health per target (second
#   disposable DB partops_target2) → 7 framework
#   preflight (expects FAIL while captures are missing) → 8 read-only capture
#   script → 9 failed-jobs gauge → 10 full integration (only when 3 complete)
#   → 11 Phase 5D remote execution on control + target DBs with TEST-ONLY
#   stub workers (never production code; validates PartOps orchestration only).
# Exit: 0 = everything that could run passed; 1 = a test failed.
# =============================================================================
set -u

if [ "${1:-}" != "--i-understand-this-is-disposable" ]; then
  echo "Refusing to run without --i-understand-this-is-disposable"; exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PG_BIN="${PG_BIN:-}"
PORT="${PARTOPS_DISPOSABLE_PORT:-55439}"
if [ -z "$PG_BIN" ] || [ ! -x "$PG_BIN/initdb" ] && [ ! -x "$PG_BIN/initdb.exe" ]; then
  echo "Set PG_BIN to an existing PostgreSQL/EDB bin directory (no software is installed)."; exit 2
fi
WORK="$(mktemp -d "${TMPDIR:-/tmp}/partops-disposable.XXXXXX")"
DATA="$WORK/data"
PSQL=("$PG_BIN/psql" -h 127.0.0.1 -p "$PORT" -U postgres -X -q -v ON_ERROR_STOP=1)
DB=(-d partops_disposable)
T="$REPO/sql/tests/disposable"
status=0

cleanup() {
  "$PG_BIN/pg_ctl" -D "$DATA" -m fast -w stop >/dev/null 2>&1
  if [ "${PARTOPS_DISPOSABLE_KEEP:-0}" != "1" ]; then rm -rf "$WORK"; else echo "kept $WORK"; fi
}
trap cleanup EXIT

"$PG_BIN/initdb" -D "$DATA" -U postgres -A trust -E UTF8 --no-locale >"$WORK/initdb.log" 2>&1 \
  || { echo "initdb failed"; cat "$WORK/initdb.log"; exit 1; }
"$PG_BIN/pg_ctl" -D "$DATA" -o "-p $PORT -c listen_addresses=127.0.0.1" -l "$WORK/server.log" -w start >/dev/null \
  || { echo "server start failed"; cat "$WORK/server.log"; exit 1; }
"$PG_BIN/postgres" --version
"${PSQL[@]}" -d postgres -c "CREATE DATABASE partops_disposable" || exit 1

step() { local name="$1"; shift; echo "== $name"; "$@" || { echo "!! FAILED: $name"; status=1; exit 1; }; }
APPROVE="CREATE TEMP TABLE _partition_realtime_migration_approved AS SELECT now() AS approved_at, current_database() AS approved_database;"

step "1 roles/schema"   "${PSQL[@]}" "${DB[@]}" -f "$T/00_roles.sql"
step "2 bootstrap"      "${PSQL[@]}" "${DB[@]}" -c "$APPROVE" -f "$REPO/sql/bootstrap_partition_job_framework.sql"
step "2b grants"        "${PSQL[@]}" "${DB[@]}" -f "$T/10_post_bootstrap_grants.sql"

# 3. Captured authoritative definitions (never invented).
REQUIRED_CAPTURES="cron_to_interval_or_next_run create_any_table_partition drop_any_table_partition insert_data_to_partition_job_table"
MISSING=""
for n in $REQUIRED_CAPTURES; do [ -f "$REPO/sql/framework/captured/$n.sql" ] || MISSING="$MISSING $n"; done
FULL=0
if [ -z "$MISSING" ]; then
  FULL=1
  [ -f "$REPO/sql/framework/captured/cron_field_to_int_array.sql" ] && \
    step "3 captured cron_field_to_int_array" "${PSQL[@]}" "${DB[@]}" -f "$REPO/sql/framework/captured/cron_field_to_int_array.sql"
  for n in $REQUIRED_CAPTURES; do
    step "3 captured $n" "${PSQL[@]}" "${DB[@]}" -f "$REPO/sql/framework/captured/$n.sql"
  done
  step "4 realtime_scheduler_v1" "${PSQL[@]}" "${DB[@]}" -c "$APPROVE" -f "$REPO/sql/realtime_scheduler_v1.sql"
else
  echo "== 3/4 SKIPPED — FRAMEWORK DEFINITION NOT AVAILABLE:$MISSING"
  echo "        (realtime_scheduler_v1.sql not applied: its dependency gate requires them)"
fi

for m in 20260929_01_add_execution_duration_ms.sql 20260929_02_update_data_to_partition_job_table.sql \
         20260929_03_run_partition_job_manual_duration.sql 20261013_02_secure_update_partition_job.sql \
         20261110_01_partition_job_attention.sql 20261110_02_missed_run_reconcile_retry_and_manual_lock.sql \
         20261110_03_partops_framework_version.sql \
         20261110_01_partition_job_attention.sql 20261110_02_missed_run_reconcile_retry_and_manual_lock.sql \
         20261110_03_partops_framework_version.sql; do
  step "5 migration $m" "${PSQL[@]}" "${DB[@]}" -f "$REPO/sql/migrations/$m"
done
echo "   (Phase 5A/5B migrations applied twice: idempotent)"

# Phase 5C: jobs that predate the target registry, then the 5C migrations (twice).
step "5c pre-5C fixture jobs" "${PSQL[@]}" "${DB[@]}" -f "$T/15_pre5c_jobs.sql"
for m in 20261117_01_partops_database_target.sql 20261117_02_target_aware_executors.sql          20261117_03_remote_execution_ledger.sql 20261117_04_remote_execution_functions.sql          20261117_01_partops_database_target.sql 20261117_02_target_aware_executors.sql          20261117_03_remote_execution_ledger.sql 20261117_04_remote_execution_functions.sql; do
  step "5c migration $m" "${PSQL[@]}" "${DB[@]}" -f "$REPO/sql/migrations/$m"
done
echo "   (Phase 5C/5D migrations applied twice: idempotent)"

step "6 Phase 5A smoke" "${PSQL[@]}" "${DB[@]}" -f "$T/20_phase5a_smoke.sql"
"${PSQL[@]}" "${DB[@]}" -c "SELECT pg_advisory_lock(87421032, (abs(hashtext('partition_job_target:smoke.fills')::bigint) % 2147483647)::integer); SELECT pg_sleep(8);" >/dev/null &
HOLDER=$!
step "6 target locks" "${PSQL[@]}" "${DB[@]}" -c "SELECT pg_sleep(2);" -f "$T/30_target_lock.sql"
wait "$HOLDER"

# 6c. Phase 5C target registry + routing. A SECOND disposable database plays a
# managed target (schema only — no worker functions, nothing is invented).
"${PSQL[@]}" -d postgres -c "CREATE DATABASE partops_target2" || exit 1
"${PSQL[@]}" -d partops_target2 -c "CREATE SCHEMA mubasher_oms; GRANT USAGE ON SCHEMA mubasher_oms TO partition_job_scheduler, partition_job_ui;" || exit 1
step "6c Phase 5C targets" "${PSQL[@]}" "${DB[@]}" -v port="$PORT" -f "$T/25_phase5c_targets.sql"
echo "== 6c routing/health per target (control + managed, read-only)"
DB_HOST=127.0.0.1 DB_PORT="$PORT" DB_NAME=partops_disposable DB_USER=partition_job_scheduler   DB_PASSWORD=disposable-trust-auth PARTITION_DB_SECRET_PROVIDER=env   PARTOPS_TARGET_DB02_DB_USER=partition_job_scheduler PARTOPS_TARGET_DB02_DB_PASSWORD=disposable-trust-auth   PARTOPS_TARGET_DB04_DB_USER=partition_job_scheduler PARTOPS_TARGET_DB04_DB_PASSWORD=disposable-trust-auth   python "$REPO/scripts/disposable_db/target_routing_check.py" || { echo "!! routing check failed"; status=1; }

# 7. Framework preflight (read-only) as the scheduler role.
export DB_HOST=127.0.0.1 DB_PORT="$PORT" DB_NAME=partops_disposable DB_USER=partition_job_scheduler \
       DB_PASSWORD=disposable-trust-auth PARTITION_DB_SECRET_PROVIDER=env
echo "== 7 framework preflight (scope=scheduler, as partition_job_scheduler)"
python "$REPO/scripts/preflight_framework.py" --expect-database partops_disposable --scope scheduler --only-problems
rc=$?
if [ "$FULL" = "1" ]; then
  [ "$rc" = "0" ] || { echo "!! preflight should PASS with complete framework (rc=$rc)"; status=1; }
else
  [ "$rc" = "2" ] && echo "   preflight FAIL as expected (framework incomplete) — scheduler gate would block" \
                  || { echo "!! preflight should FAIL while definitions are missing (rc=$rc)"; status=1; }
fi

# 8. The read-only capture script must run cleanly (and roll back).
step "8 capture script (read-only)" "${PSQL[@]}" "${DB[@]}" -P pager=off -f "$REPO/sql/capture_reference_framework.sql" -o "$WORK/capture.txt"
grep -q "Capture complete" "$WORK/capture.txt" && echo "   capture output: $(wc -l < "$WORK/capture.txt") lines"

# 9. Failed-jobs gauge: exact SQL from scheduler_database.count_failed_jobs().
python - "$REPO" > "$WORK/gauge.sql" <<'PY'
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock
sys.path.insert(0, sys.argv[1])
from scheduler_backend import scheduler_database
from scheduler_backend.models import SchedulerConfig
cap = []
@contextmanager
def fake(purpose):
    conn = MagicMock(); cur = MagicMock()
    cur.execute.side_effect = lambda sql, params=None: cap.append(sql)
    cur.fetchone.return_value = {"failed_count": 0}
    conn.cursor.return_value.__enter__.return_value = cur
    yield conn
scheduler_database.open_connection = fake
scheduler_database.count_failed_jobs(SchedulerConfig())
print(cap[0] + ";")
PY
gauge() { "${PSQL[@]}" "${DB[@]}" -tA -c "SET ROLE partition_job_scheduler;" -f "$WORK/gauge.sql" | tail -1; }
c1=$(gauge)
"${PSQL[@]}" "${DB[@]}" -c "INSERT INTO mubasher_oms.partitioning_job_table_log (job_id, job_name, last_run_status, job_runtime) VALUES (4, 'SMOKE', 'MANUAL_SUCCESS', timestamp '2026-11-01 05:00');"
c2=$(gauge)
echo "== 9 failed_count before/after manual success: $c1 / $c2"
[ "$c1" = "2" ] && [ "$c2" = "1" ] || { echo "!! gauge semantics wrong"; status=1; }

# 10. Full integration (only with every captured definition present).
if [ "$FULL" = "1" ]; then
  step "10 full integration" "${PSQL[@]}" "${DB[@]}" -f "$T/40_integration_full.sql"
else
  echo "== 10 SKIPPED — FRAMEWORK DEFINITION NOT AVAILABLE:$MISSING"
fi

# 11. Phase 5D remote execution on real databases. TEST-ONLY STUB workers /
# cron helper (sql/tests/disposable/stubs — disposable only, never production)
# are installed AFTER the stages above so those keep testing the real gap.
S="$T/stubs"
step "11 TEST-ONLY stub cron helper (control)" "${PSQL[@]}" "${DB[@]}" -f "$S/test_only_stub_cron_helper.sql"
step "11 TEST-ONLY stub workers (control db01)" "${PSQL[@]}" "${DB[@]}" -f "$S/test_only_stub_workers.sql"
step "11 TEST-ONLY stub workers (target db02)" "${PSQL[@]}" -d partops_target2 -f "$S/test_only_stub_workers.sql"
echo "== 11 Phase 5D remote execution + scheduler concurrency (multi-database)"
DB_HOST=127.0.0.1 DB_PORT="$PORT" DB_NAME=partops_disposable DB_USER=partition_job_scheduler   DB_PASSWORD=disposable-trust-auth PARTITION_DB_SECRET_PROVIDER=env REALTIME_DB_EXPECTED_NAME=partops_disposable   PARTOPS_TARGET_DB02_DB_USER=partition_job_scheduler PARTOPS_TARGET_DB02_DB_PASSWORD=disposable-trust-auth   PARTOPS_TARGET_DB04_DB_USER=partition_job_scheduler PARTOPS_TARGET_DB04_DB_PASSWORD=disposable-trust-auth   python "$REPO/scripts/disposable_db/remote_execution_check.py" || { echo "!! remote execution check failed"; status=1; }

echo "== missing authoritative objects in this disposable DB:"
"${PSQL[@]}" "${DB[@]}" -tA -c "SELECT n || ' -> ' || COALESCE(to_regproc('mubasher_oms.' || n)::text, 'NOT PRESENT') FROM unnest(ARRAY['cron_to_interval_or_next_run','create_any_table_partition','drop_any_table_partition','insert_data_to_partition_job_table','get_upcoming_partition_jobs']) AS n"
[ "$status" = "0" ] && echo "RESULT: all runnable stages passed (full integration: $([ "$FULL" = 1 ] && echo RUN || echo SKIPPED))"
exit $status
