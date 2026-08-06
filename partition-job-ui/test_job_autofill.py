"""Tests for pgAgent job auto-fill helpers and database query safety."""

from __future__ import annotations

import inspect
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import database
from database import DatabaseError, get_pgagent_job_details
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
