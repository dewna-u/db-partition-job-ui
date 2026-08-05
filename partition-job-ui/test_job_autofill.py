"""Tests for pgAgent job auto-fill helpers and database query safety."""

from __future__ import annotations

import inspect
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import database
from database import DatabaseError, get_pgagent_job_details
from job_autofill import (
    convert_pgagent_schedule_to_cron,
    extract_called_functions,
    extract_single_called_function,
    infer_frequency,
    infer_is_create,
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
        weekdays = [True] * 7

        cron, warnings = convert_pgagent_schedule_to_cron(
            minutes, hours, monthdays, months, weekdays
        )
        self.assertEqual(cron, "0 0 2 26 * *")
        self.assertEqual(warnings, [])

        frequency, frequency_warning = infer_frequency(cron)
        self.assertIsNone(frequency)
        self.assertIsNotNone(frequency_warning)

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


if __name__ == "__main__":
    unittest.main()
