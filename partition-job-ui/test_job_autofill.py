"""Tests for pgAgent job auto-fill helpers and database query safety."""

from __future__ import annotations

import inspect
import re
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from psycopg import Error as PsycopgError
from psycopg.types.json import Jsonb

import database
from database import DatabaseError, create_partition_job, get_pgagent_job_details
from job_autofill import (
    calculate_next_run,
    convert_pgagent_schedule_to_cron,
    extract_called_functions,
    extract_partition_settings,
    extract_single_called_function,
    extract_single_target_table,
    infer_frequency,
    infer_is_create,
    infer_is_create_from_definition,
)


class InferIsCreateTests(unittest.TestCase):
    def test_create_token_true(self) -> None:
        self.assertTrue(infer_is_create("JOB_CREATE_PARTITIONS_X"))

    def test_drop_token_false(self) -> None:
        self.assertFalse(infer_is_create("JOB_DROP_PARTITIONS_X"))

    def test_both_create_and_drop_unknown(self) -> None:
        self.assertIsNone(infer_is_create("JOB_CREATE_DROP_PARTITIONS_X"))

    def test_neither_create_nor_drop_unknown(self) -> None:
        self.assertIsNone(infer_is_create("JOB_TEST_PARTITIONING_2"))

    def test_partial_word_does_not_match(self) -> None:
        self.assertIsNone(infer_is_create("JOB_CREATED_PARTITIONS"))
        self.assertIsNone(infer_is_create("JOB_DROPPED_PARTITIONS"))


class FunctionExtractionTests(unittest.TestCase):
    def test_extract_example_select(self) -> None:
        sql = (
            "SELECT mubasher_oms.create_partitions_r19_customer_summary(current_date);"
        )
        self.assertEqual(
            extract_called_functions(sql),
            [("mubasher_oms", "create_partitions_r19_customer_summary")],
        )

    def test_extract_call_and_perform(self) -> None:
        self.assertEqual(
            extract_called_functions("CALL schema_name.function_name(1, 2);"),
            [("schema_name", "function_name")],
        )
        self.assertEqual(
            extract_called_functions("PERFORM schema_name.function_name();"),
            [("schema_name", "function_name")],
        )

    def test_reject_multiple_function_calls(self) -> None:
        sql = """
        SELECT schema_a.func_one(current_date);
        SELECT schema_b.func_two(current_date);
        """
        pair, warning = extract_single_called_function(sql)
        self.assertIsNone(pair)
        self.assertIsNotNone(warning)

    def test_unqualified_call_is_ignored(self) -> None:
        pair, warning = extract_single_called_function(
            "SELECT create_partitions_r19_customer_summary(current_date);"
        )
        self.assertIsNone(pair)
        self.assertIsNotNone(warning)


class ScheduleConversionTests(unittest.TestCase):
    def test_monthly_0200_day_26(self) -> None:
        minutes = [False] * 60
        minutes[0] = True
        hours = [False] * 24
        hours[2] = True
        monthdays = [False] * 32
        monthdays[25] = True  # pgAgent index 25 = calendar day 26
        months = [True] * 12
        weekdays = [False] * 7  # all-false = unrestricted

        cron, warnings = convert_pgagent_schedule_to_cron(
            minutes, hours, monthdays, months, weekdays
        )
        self.assertEqual(cron, "0 0 2 26 * *")
        self.assertEqual(warnings, [])

        frequency, frequency_warning = infer_frequency(cron)
        self.assertEqual(frequency, (1, "month"))
        self.assertIsNone(frequency_warning)

    def test_every_day_infers_daily_frequency(self) -> None:
        minutes = [False] * 60
        minutes[30] = True
        hours = [False] * 24
        hours[2] = True
        monthdays = [True] * 31 + [False]
        months = [True] * 12
        weekdays = [True] * 7
        cron, warnings = convert_pgagent_schedule_to_cron(
            minutes, hours, monthdays, months, weekdays
        )
        self.assertEqual(cron, "0 30 2 * * *")
        self.assertEqual(warnings, [])
        self.assertEqual(infer_frequency(cron), ((1, "day"), None))

    def test_hourly_and_weekly_frequency(self) -> None:
        self.assertEqual(infer_frequency("0 15 * * * *"), ((1, "hour"), None))
        self.assertEqual(infer_frequency("0 0 2 * * 1"), ((1, "week"), None))
        self.assertEqual(infer_frequency("0 * * * * *"), ((1, "minute"), None))


class NextRunCalculationTests(unittest.TestCase):
    def test_next_run_before_monthly_day(self) -> None:
        next_run = calculate_next_run(
            "0 0 2 26 * *",
            now=datetime(2026, 8, 6, 10, 0, 0),
        )
        self.assertEqual(next_run, datetime(2026, 8, 26, 2, 0, 0))

    def test_next_run_after_monthly_day(self) -> None:
        next_run = calculate_next_run(
            "0 0 2 26 * *",
            now=datetime(2026, 8, 27, 0, 0, 0),
        )
        self.assertEqual(next_run, datetime(2026, 9, 26, 2, 0, 0))

    def test_next_run_same_day_after_scheduled_time(self) -> None:
        next_run = calculate_next_run(
            "0 0 2 26 * *",
            now=datetime(2026, 8, 26, 3, 0, 0),
        )
        self.assertEqual(next_run, datetime(2026, 9, 26, 2, 0, 0))

    def test_jscstart_is_lower_bound_not_next_run(self) -> None:
        next_run = calculate_next_run(
            "0 0 2 26 * *",
            now=datetime(2026, 8, 6, 10, 0, 0),
            start_time=datetime(2026, 1, 26, 2, 0, 0),
        )
        self.assertEqual(next_run, datetime(2026, 8, 26, 2, 0, 0))


class PartitionSettingsExtractionTests(unittest.TestCase):
    SAMPLE_CREATE_FN = """
    BEGIN
        cur_date = date_trunc('month', start_date + interval '7 days')
            + interval '2 months';
        end_date = date_trunc('month', start_date + interval '7 days')
            + interval '3 months';
        next_date := cur_date + INTERVAL '1 month';
        EXECUTE 'ALTER TABLE mubasher_oms.r19_customer_summary ADD PARTITION '
            || quote_ident(part_name);
    END;
    """

    def test_next_date_interval_sets_partition_period(self) -> None:
        values, _warnings = extract_partition_settings(self.SAMPLE_CREATE_FN)
        self.assertEqual(values.get("partition_unit"), "month")
        self.assertEqual(values.get("partition_period"), 1)

    def test_create_ahead_interval_from_cur_date(self) -> None:
        values, _warnings = extract_partition_settings(self.SAMPLE_CREATE_FN)
        self.assertEqual(values.get("create_drop_amount"), 2)
        self.assertEqual(values.get("create_drop_unit"), "month")

    def test_alter_table_add_partition_target_and_create_flag(self) -> None:
        table, warning = extract_single_target_table(self.SAMPLE_CREATE_FN)
        self.assertEqual(table, ("mubasher_oms", "r19_customer_summary"))
        self.assertIsNone(warning)
        self.assertTrue(infer_is_create_from_definition(self.SAMPLE_CREATE_FN))

    def test_drop_partition_sets_is_create_false(self) -> None:
        definition = """
        EXECUTE 'ALTER TABLE other_schema.other_table DROP PARTITION '
            || quote_ident(part_name);
        """
        self.assertFalse(infer_is_create_from_definition(definition))


class UnknownJobIdTests(unittest.TestCase):
    def test_unknown_job_id_does_not_crash(self) -> None:
        class FakeCursor:
            def __init__(self, row_factory=None) -> None:
                self.sql = ""
                self.params = None

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def execute(self, sql, params=None) -> None:
                self.sql = sql
                self.params = params

            def fetchone(self):
                if "to_regclass" in self.sql:
                    return (True,)
                if "pga_job" in self.sql and "jobid" in self.sql:
                    assert "%(job_id)s" in self.sql
                    assert self.params == {"job_id": 999999}
                    return None
                return None

            def fetchall(self):
                return []

        class FakeConnection:
            def cursor(self, row_factory=None) -> FakeCursor:
                return FakeCursor(row_factory=row_factory)

        @contextmanager
        def fake_connection(_kwargs):
            yield FakeConnection()

        with patch.object(database, "_connection", fake_connection), patch.object(
            database, "_pgagent_db_kwargs", return_value={}
        ):
            with self.assertRaises(DatabaseError) as ctx:
                get_pgagent_job_details(999999)
        self.assertIn("No pgAgent job was found", ctx.exception.message)


class _FakeDiag:
    def __init__(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self, key, value)

    def __getattr__(self, name):  # unspecified diagnostic fields are absent
        return None


class FakePgError(PsycopgError):
    """psycopg error carrying a controllable SQLSTATE and diagnostics."""

    def __init__(self, message: str, sqlstate: str = "", **diag_fields) -> None:
        super().__init__(message)
        self._fake_diag = _FakeDiag(
            sqlstate=sqlstate or None,
            message_primary=message,
            **diag_fields,
        )

    @property
    def diag(self):  # type: ignore[override]
        return self._fake_diag


class FakeCursor:
    def __init__(self, owner: "FakeConnection") -> None:
        self.owner = owner
        self.description = owner.description

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self.owner.executed.append((sql, params))
        if self.owner.error is not None:
            raise self.owner.error

    def fetchone(self):
        return self.owner.row


class FakeTransaction:
    def __init__(self, owner: "FakeConnection") -> None:
        self.owner = owner

    def __enter__(self) -> "FakeTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.owner.committed = True
        else:
            self.owner.rolled_back = True
        return False


class FakeConnection:
    def __init__(self, row=(None,), description=(("result",),), error=None) -> None:
        self.row = row
        self.description = description
        self.error = error
        self.executed: list = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, row_factory=None) -> FakeCursor:
        return FakeCursor(self)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


VALID_PAYLOAD = {
    "job_name": "JOB_CREATE_PARTITIONS_R19_CUSTOMER_SUMMARY",
    "is_enabled": True,
    "table_schema": "mubasher_oms",
    "table_name": "r19_customer_summary",
    "db_config": {"work_mem": "512MB", "dbname": "example_db"},
    "job_schedule": "0 0 2 26 * *",
    "frequency": "1 month",
    "next_run_time": datetime(2026, 8, 26, 2, 0, 0),
    "partition_unit": "month",
    "partition_period": 1,
    "is_create": True,
    "create_drop_interval": "2 months",
}


def _run_create(connection: FakeConnection):
    @contextmanager
    def fake_connection(_kwargs):
        yield connection

    with patch.object(database, "_connection", fake_connection), patch.object(
        database, "_main_db_kwargs", return_value={}
    ):
        return create_partition_job(dict(VALID_PAYLOAD))


class CreatePartitionJobCallTests(unittest.TestCase):
    def test_calls_function_with_parameterized_named_arguments(self) -> None:
        conn = FakeConnection(row=(42,))
        result = _run_create(conn)

        self.assertEqual(result, 42)
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]

        self.assertIn("mubasher_oms.insert_data_to_partition_job_table", sql)
        # The function is the only write path: no direct INSERT workaround.
        self.assertNotIn("insert into", sql.lower())

        placeholders = re.findall(r"%\(([a-z_]+)\)s", sql)
        self.assertEqual(len(placeholders), 12)
        self.assertEqual(sorted(placeholders), sorted(params.keys()))

        self.assertEqual(
            placeholders,
            [
                "job_name",
                "is_enabled",
                "table_schema",
                "table_name",
                "db_config",
                "job_schedule",
                "frequency",
                "next_run_time",
                "partition_unit",
                "partition_period",
                "is_create",
                "create_drop_interval",
            ],
        )

        self.assertEqual(params["frequency"], "1 month")
        self.assertEqual(params["create_drop_interval"], "2 months")
        self.assertEqual(params["partition_period"], 1)
        self.assertEqual(params["next_run_time"], datetime(2026, 8, 26, 2, 0, 0))
        self.assertIsInstance(params["db_config"], Jsonb)
        # Values are bound, never interpolated into the statement text.
        self.assertNotIn(VALID_PAYLOAD["job_name"], sql)

    def test_commits_once_on_success(self) -> None:
        conn = FakeConnection(row=(1,))
        _run_create(conn)
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)

    def test_void_returning_function_succeeds(self) -> None:
        conn = FakeConnection(row=None, description=None)
        self.assertIsNone(_run_create(conn))
        self.assertTrue(conn.committed)

    def test_void_returning_function_with_null_row_succeeds(self) -> None:
        conn = FakeConnection(row=(None,))
        self.assertIsNone(_run_create(conn))
        self.assertTrue(conn.committed)

    def test_rolls_back_on_failure(self) -> None:
        conn = FakeConnection(error=FakePgError("boom", sqlstate="42501"))
        with self.assertRaises(DatabaseError):
            _run_create(conn)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)


class ErrorClassificationTests(unittest.TestCase):
    def test_insufficient_privilege_maps_to_permission_error(self) -> None:
        exc = FakePgError(
            "permission denied for table partitioning_job_table", sqlstate="42501"
        )
        mapped = database._map_psycopg_error(exc)
        self.assertIn("does not have the required permission", mapped.message)
        self.assertIn("partitioning_job_table", mapped.message)

    def test_undefined_function_is_not_a_permission_error(self) -> None:
        exc = FakePgError(
            "function mubasher_oms.insert_data_to_partition_job_table(...) does not exist",
            sqlstate="42883",
        )
        mapped = database._map_psycopg_error(exc)
        self.assertNotIn("permission", mapped.message.lower())
        self.assertIn("parameter names and types", mapped.message)

    def test_type_mismatch_is_not_a_permission_error(self) -> None:
        exc = FakePgError("invalid input syntax for type interval", sqlstate="22P02")
        mapped = database._map_psycopg_error(exc)
        self.assertNotIn("permission", mapped.message.lower())
        self.assertIn("invalid type or format", mapped.message)

    def test_unique_violation_is_not_a_permission_error(self) -> None:
        exc = FakePgError("duplicate key value", sqlstate="23505")
        mapped = database._map_psycopg_error(exc)
        self.assertNotIn("permission", mapped.message.lower())
        self.assertIn("already exist", mapped.message)

    def test_permission_text_without_42501_is_not_misclassified(self) -> None:
        # A non-permission SQLSTATE must never be reported as a permission error
        # merely because the message text mentions permission denied.
        exc = FakePgError(
            "check constraint failed: permission denied for legacy row",
            sqlstate="23514",
        )
        mapped = database._map_psycopg_error(exc)
        self.assertNotIn("required permission", mapped.message)

    def test_connection_failure_without_sqlstate(self) -> None:
        exc = FakePgError("could not connect to server", sqlstate="")
        mapped = database._map_psycopg_error(exc)
        self.assertIn("Unable to connect", mapped.message)

    def test_unknown_sqlstate_reports_code_without_secrets(self) -> None:
        exc = FakePgError("something odd", sqlstate="XX000")
        mapped = database._map_psycopg_error(exc)
        self.assertIn("XX000", mapped.message)
        self.assertNotIn("password", mapped.message.lower())


class ConfiguredObjectNameTests(unittest.TestCase):
    def test_default_function_identity(self) -> None:
        self.assertEqual(
            database.partition_job_function_identity(),
            ("mubasher_oms", "insert_data_to_partition_job_table"),
        )

    def test_environment_override_is_validated(self) -> None:
        with patch.dict(
            "os.environ", {"PARTITION_JOB_FUNCTION": "bad name; DROP TABLE x"}
        ):
            with self.assertRaises(DatabaseError):
                database.build_create_partition_job_sql()

    def test_environment_override_applies(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PARTITION_JOB_SCHEMA": "other_schema",
                "PARTITION_JOB_FUNCTION": "other_function",
            },
        ):
            sql = database.build_create_partition_job_sql()
        self.assertIn("other_schema.other_function", sql)


class BoundParameterTests(unittest.TestCase):
    def test_job_id_sql_uses_bound_parameters(self) -> None:
        self.assertIn("%(job_id)s", database._PGAGENT_JOB_BY_ID_SQL)
        self.assertIn("%(job_id)s", database._PGAGENT_STEPS_SQL)
        self.assertIn("%(job_id)s", database._PGAGENT_SCHEDULES_SQL)
        self.assertNotIn("%s" + "{job_id}", database._PGAGENT_JOB_BY_ID_SQL)
        for sql in (
            database._PGAGENT_JOB_BY_ID_SQL,
            database._PGAGENT_STEPS_SQL,
            database._PGAGENT_SCHEDULES_SQL,
        ):
            self.assertNotIn("+", sql.replace("||", ""))
            self.assertNotIn(".format(", sql)
            self.assertNotIn("f\"", sql)
            self.assertNotIn("f'", sql)

        source = inspect.getsource(get_pgagent_job_details)
        self.assertIn('params = {"job_id": job_id_int}', source)
        self.assertIn("cur.execute(_PGAGENT_JOB_BY_ID_SQL, params)", source)
        self.assertIn("cur.execute(_PGAGENT_STEPS_SQL, params)", source)
        self.assertIn("cur.execute(_PGAGENT_SCHEDULES_SQL, params)", source)

    def test_function_lookup_uses_bound_parameters(self) -> None:
        self.assertIn("%(function_schema)s", database._FUNCTION_DEF_SQL)
        self.assertIn("%(function_name)s", database._FUNCTION_DEF_SQL)

    def test_connstr_not_selected(self) -> None:
        self.assertNotIn("jstconnstr", database._PGAGENT_STEPS_SQL.lower())

    def test_database_imports_calculate_next_run(self) -> None:
        self.assertTrue(callable(database.calculate_next_run))


if __name__ == "__main__":
    unittest.main()
