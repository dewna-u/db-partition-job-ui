"""Tests for pgAgent job auto-fill helpers and database query safety."""

from __future__ import annotations

import inspect
import json
import os
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
    build_db_config_json,
    calculate_next_run,
    convert_pgagent_schedule_to_cron,
    describe_schedule,
    extract_called_functions,
    extract_db_config_from_function,
    extract_partition_settings,
    extract_single_called_function,
    extract_single_target_table,
    infer_frequency,
    infer_is_create,
    infer_is_create_from_definition,
    infer_partition_operation_from_function,
    validate_six_field_cron,
)
from validators import (
    ValidationError,
    to_interval_string,
    validate_create_drop_unit,
    validate_cron_schedule,
    validate_form_data,
    validate_frequency_unit,
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


# Simplified from the real mubasher_oms.drop_partitions_m77_* function: the only
# destructive statement is dynamic, and everything before it is discovery logic.
REAL_DROP_FUNCTION_BODY = """
CREATE OR REPLACE FUNCTION mubasher_oms.drop_partitions_m77_fi_consolidated_price_data()
 RETURNS void
 LANGUAGE plpgsql
AS $BODY$
DECLARE
    partition_name_arr text[];
BEGIN
    SET datestyle = 'ISO, DMY';

    SELECT array_agg(c.relname)
      INTO partition_name_arr
      FROM pg_inherits i
      JOIN pg_class c ON c.oid = i.inhrelid
     WHERE pg_get_expr(c.relpartbound, c.oid) < cutoff_bound;

    FOR i IN 1..COALESCE(ARRAY_LENGTH(partition_name_arr, 1), 0)
    LOOP
        EXECUTE format(
            'DROP TABLE %I',
            partition_name_arr[i]
        );
    END LOOP;
END;
$BODY$;
"""

EDB_ADD_PARTITION_BODY = """
BEGIN
    EXECUTE format(
        'ALTER TABLE %I.%I ADD PARTITION %I VALUES LESS THAN (%L)',
        v_schema, v_table, v_partition, v_bound
    );
END;
"""

DECLARATIVE_PARTITION_OF_BODY = """
BEGIN
    EXECUTE format(
        'CREATE TABLE %I.%I PARTITION OF %I.%I FOR VALUES FROM (%L) TO (%L)',
        v_schema, v_child, v_schema, v_parent, v_from, v_to
    );
END;
"""


class PartitionOperationInferenceTests(unittest.TestCase):
    """CREATE/DROP must come from executable SQL, never from a name."""

    def test_real_dynamic_drop_table_function(self) -> None:
        evidence = infer_partition_operation_from_function(REAL_DROP_FUNCTION_BODY)
        self.assertEqual(evidence.operation, "DROP")
        self.assertIs(evidence.is_create, False)
        self.assertIn("DROP TABLE", evidence.drop_evidence)
        self.assertIn("DROP TABLE", evidence.reason)

    def test_dynamic_drop_table_format(self) -> None:
        body = "BEGIN EXECUTE format('DROP TABLE %I', partition_name); END;"
        self.assertIs(infer_is_create_from_definition(body), False)

    def test_direct_drop_table(self) -> None:
        self.assertIs(
            infer_is_create_from_definition("BEGIN DROP TABLE old_partition; END;"),
            False,
        )

    def test_edb_add_partition_is_create(self) -> None:
        evidence = infer_partition_operation_from_function(EDB_ADD_PARTITION_BODY)
        self.assertEqual(evidence.operation, "CREATE")
        self.assertIs(evidence.is_create, True)

    def test_declarative_partition_of_is_create(self) -> None:
        evidence = infer_partition_operation_from_function(
            DECLARATIVE_PARTITION_OF_BODY
        )
        self.assertEqual(evidence.operation, "CREATE")
        self.assertIs(evidence.is_create, True)

    def test_attach_partition_is_create(self) -> None:
        body = "BEGIN EXECUTE 'ALTER TABLE p ATTACH PARTITION c FOR VALUES ...'; END;"
        self.assertIs(infer_is_create_from_definition(body), True)

    def test_detach_partition_is_drop(self) -> None:
        body = "BEGIN EXECUTE 'ALTER TABLE p DETACH PARTITION c'; END;"
        self.assertIs(infer_is_create_from_definition(body), False)

    def test_drop_inside_line_comment_is_ignored(self) -> None:
        body = """
        BEGIN
            -- EXECUTE format('DROP TABLE %I', partition_name);
            SELECT 1;
        END;
        """
        evidence = infer_partition_operation_from_function(body)
        self.assertEqual(evidence.operation, "UNKNOWN")
        self.assertIsNone(evidence.is_create)

    def test_create_inside_block_comment_is_ignored(self) -> None:
        body = """
        BEGIN
            /* ALTER TABLE x ADD PARTITION y VALUES LESS THAN ('2026-01-01') */
            SELECT 1;
        END;
        """
        evidence = infer_partition_operation_from_function(body)
        self.assertEqual(evidence.operation, "UNKNOWN")
        self.assertIsNone(evidence.is_create)

    def test_raise_notice_prose_is_not_evidence(self) -> None:
        body = """
        BEGIN
            RAISE NOTICE 'About to drop table % for retention', v_name;
            EXECUTE format('ALTER TABLE %I.%I ADD PARTITION %I', a, b, c);
        END;
        """
        evidence = infer_partition_operation_from_function(body)
        self.assertEqual(evidence.operation, "CREATE")

    def test_both_create_and_drop_is_ambiguous(self) -> None:
        body = """
        BEGIN
            EXECUTE format('ALTER TABLE %I.%I ADD PARTITION %I', a, b, c);
            EXECUTE format('DROP TABLE %I', d);
        END;
        """
        evidence = infer_partition_operation_from_function(body)
        self.assertEqual(evidence.operation, "AMBIGUOUS")
        self.assertIsNone(evidence.is_create)

    def test_no_evidence_is_unknown_not_create(self) -> None:
        body = "BEGIN SELECT count(*) FROM pg_class; END;"
        evidence = infer_partition_operation_from_function(body)
        self.assertEqual(evidence.operation, "UNKNOWN")
        self.assertIsNone(evidence.is_create)

    def test_discovery_selects_do_not_mask_drop(self) -> None:
        # pg_inherits / pg_class / pg_get_expr lookups are not counter-evidence.
        self.assertIs(
            infer_is_create_from_definition(REAL_DROP_FUNCTION_BODY), False
        )


class DbConfigExtractionTests(unittest.TestCase):
    """Database Configuration must mirror the function, never a default."""

    def _config(self, body: str) -> dict:
        config, _warnings = extract_db_config_from_function(body)
        return config

    def test_set_assignment(self) -> None:
        self.assertEqual(
            self._config("BEGIN SET datestyle = 'ISO, DMY'; END;"),
            {"datestyle": "ISO, DMY"},
        )

    def test_set_to_syntax(self) -> None:
        self.assertEqual(
            self._config("BEGIN SET work_mem TO '512MB'; END;"),
            {"work_mem": "512MB"},
        )

    def test_multiple_parameters(self) -> None:
        body = """
        BEGIN
            SET work_mem = '512MB';
            SET maintenance_work_mem = '1GB';
            SET lock_timeout = '5s';
        END;
        """
        self.assertEqual(
            self._config(body),
            {
                "work_mem": "512MB",
                "maintenance_work_mem": "1GB",
                "lock_timeout": "5s",
            },
        )

    def test_set_local_is_recognised(self) -> None:
        self.assertEqual(
            self._config("BEGIN SET LOCAL lock_timeout = '5s'; END;"),
            {"lock_timeout": "5s"},
        )

    def test_set_config_perform(self) -> None:
        self.assertEqual(
            self._config("BEGIN PERFORM set_config('lock_timeout', '5s', true); END;"),
            {"lock_timeout": "5s"},
        )

    def test_set_config_select(self) -> None:
        self.assertEqual(
            self._config("BEGIN SELECT set_config('work_mem', '512MB', true); END;"),
            {"work_mem": "512MB"},
        )

    def test_line_comment_is_not_configuration(self) -> None:
        self.assertEqual(self._config("-- SET work_mem = '512MB';"), {})

    def test_block_comment_is_not_configuration(self) -> None:
        body = """
        /*
        SET maintenance_work_mem = '1GB';
        */
        """
        self.assertEqual(self._config(body), {})

    def test_no_configuration_returns_empty(self) -> None:
        self.assertEqual(self._config("BEGIN SELECT now(); END;"), {})

    def test_dynamic_set_config_value_is_not_invented(self) -> None:
        config, warnings = extract_db_config_from_function(
            "BEGIN PERFORM set_config('work_mem', v_work_mem, true); END;"
        )
        self.assertEqual(config, {})
        self.assertTrue(any("run-time value" in warning for warning in warnings))

    def test_update_set_is_not_configuration(self) -> None:
        body = """
        BEGIN
            UPDATE mubasher_oms.partitioning_job_table
               SET last_run_status = 'SUCCESS'
             WHERE job_id = v_job_id;
        END;
        """
        self.assertEqual(self._config(body), {})

    def test_unrelated_sql_is_not_configuration(self) -> None:
        body = """
        BEGIN
            SELECT now() + INTERVAL '7 days';
            RAISE NOTICE 'partition maintenance';
            EXECUTE format('DROP TABLE %I', v_name);
        END;
        """
        self.assertEqual(self._config(body), {})

    def test_repeated_parameter_uses_last_value_and_warns(self) -> None:
        body = """
        BEGIN
            SET work_mem = '256MB';
            SET work_mem = '512MB';
        END;
        """
        config, warnings = extract_db_config_from_function(body)
        self.assertEqual(config, {"work_mem": "512MB"})
        self.assertTrue(any("more than once" in warning for warning in warnings))

    def test_real_m77_function_configuration(self) -> None:
        config, _warnings = extract_db_config_from_function(REAL_DROP_FUNCTION_BODY)
        self.assertEqual(config, {"datestyle": "ISO, DMY"})

    def test_serialisation_is_valid_json(self) -> None:
        rendered = build_db_config_json({"datestyle": "ISO, DMY"})
        self.assertEqual(json.loads(rendered), {"datestyle": "ISO, DMY"})
        self.assertEqual(json.loads(build_db_config_json({})), {})
        self.assertEqual(json.loads(build_db_config_json(None)), {})

    def test_credential_like_keys_are_dropped(self) -> None:
        config, _warnings = extract_db_config_from_function(
            "BEGIN SET password = 'hunter2'; SET work_mem = '512MB'; END;"
        )
        self.assertEqual(config, {"work_mem": "512MB"})

    def test_no_dummy_defaults_remain_in_autofill_modules(self) -> None:
        # Guards against a demo default creeping back into the production path.
        source_dir = os.path.dirname(os.path.abspath(__file__))
        for module_name in ("job_autofill.py", "database.py", "app.py"):
            path = os.path.join(source_dir, module_name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("512MB", source, module_name)
            self.assertNotIn("maintenance_work_mem", source, module_name)


def _load_details_with_function_body(job_name: str, function_body: str) -> dict:
    """Drive get_pgagent_job_details() against a fake pgAgent job and function."""

    job = {
        "job_id": 7,
        "job_name": job_name,
        "enabled": True,
        "host_agent": None,
        "next_run": datetime(2026, 9, 1, 2, 0, 0),
        "description": None,
    }
    steps = [
        {
            "step_id": 1,
            "step_name": "run",
            "enabled": True,
            "kind": "s",
            "code": "SELECT mubasher_oms.partition_maintenance_m77(current_date);",
            "dbname": "demo_db",
        }
    ]
    function_rows = [
        {"oid": 1, "function_definition": function_body, "argument_count": 1}
    ]

    class Cursor:
        def __init__(self) -> None:
            self.sql = ""

        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, sql, params=None) -> None:
            self.sql = sql

        def fetchone(self):
            if "to_regclass" in self.sql:
                return (True,)
            if "WHERE jobid" in self.sql:
                return dict(job)
            return None

        def fetchall(self):
            if "jstjobid" in self.sql:
                return [dict(row) for row in steps]
            if "jscjobid" in self.sql:
                return []
            if "pg_get_functiondef" in self.sql:
                return [dict(row) for row in function_rows]
            return []

    class Connection:
        def cursor(self, row_factory=None) -> "Cursor":
            return Cursor()

    @contextmanager
    def fake_connection(_kwargs):
        yield Connection()

    with patch.object(database, "_connection", fake_connection), patch.object(
        database, "_pgagent_db_kwargs", return_value={}
    ), patch.object(database, "_main_db_kwargs", return_value={}):
        return get_pgagent_job_details(7)


class OperationOverridesJobNameTests(unittest.TestCase):
    """The executable body outranks the job name and the function name."""

    def test_create_named_job_with_drop_body_infers_drop(self) -> None:
        # The name alone would say CREATE.
        self.assertIs(infer_is_create("JOB_CREATE_SOMETHING"), True)
        details = _load_details_with_function_body(
            "JOB_CREATE_SOMETHING", REAL_DROP_FUNCTION_BODY
        )
        self.assertIs(details["autofill"]["is_create"], False)

    def test_drop_named_job_with_add_partition_body_infers_create(self) -> None:
        self.assertIs(infer_is_create("JOB_DROP_SOMETHING"), False)
        details = _load_details_with_function_body(
            "JOB_DROP_SOMETHING", EDB_ADD_PARTITION_BODY
        )
        self.assertIs(details["autofill"]["is_create"], True)

    def test_ambiguous_body_is_not_rescued_by_job_name(self) -> None:
        body = """
        BEGIN
            EXECUTE format('ALTER TABLE %I.%I ADD PARTITION %I', a, b, c);
            EXECUTE format('DROP TABLE %I', d);
        END;
        """
        details = _load_details_with_function_body("JOB_CREATE_SOMETHING", body)
        self.assertNotIn("is_create", details["autofill"])
        self.assertTrue(
            any(
                "review the operation manually" in warning
                for warning in details["warnings"]
            )
        )

    def test_body_without_evidence_is_not_defaulted_to_create(self) -> None:
        details = _load_details_with_function_body(
            "JOB_CREATE_SOMETHING", "BEGIN SELECT 1; END;"
        )
        self.assertNotIn("is_create", details["autofill"])

    def test_db_config_comes_from_the_loaded_function(self) -> None:
        details = _load_details_with_function_body(
            "JOB_DROP_PARTITIONS_M77", REAL_DROP_FUNCTION_BODY
        )
        autofill = details["autofill"]
        self.assertEqual(
            json.loads(autofill["db_config"]), {"datestyle": "ISO, DMY"}
        )
        # Operation and configuration are independent analyses of the same body.
        self.assertIs(autofill["is_create"], False)

    def test_db_config_is_empty_when_function_sets_nothing(self) -> None:
        details = _load_details_with_function_body(
            "JOB_CREATE_SOMETHING", EDB_ADD_PARTITION_BODY
        )
        self.assertEqual(json.loads(details["autofill"]["db_config"]), {})
        self.assertTrue(
            any(
                "sets no session configuration" in warning
                for warning in details["warnings"]
            )
        )

    def test_existing_create_autofill_still_works(self) -> None:
        details = _load_details_with_function_body(
            "JOB_CREATE_PARTITIONS_R19_CUSTOMER_SUMMARY",
            PartitionSettingsExtractionTests.SAMPLE_CREATE_FN,
        )
        autofill = details["autofill"]
        self.assertIs(autofill["is_create"], True)
        self.assertEqual(autofill["table_schema"], "mubasher_oms")
        self.assertEqual(autofill["table_name"], "r19_customer_summary")
        self.assertEqual(autofill["partition_unit"], "month")
        self.assertEqual(autofill["partition_period"], 1)
        self.assertEqual(autofill["create_drop_amount"], 2)
        self.assertEqual(autofill["create_drop_unit"], "month")


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

    def fetchall(self):
        return self.owner.rows


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
    def __init__(
        self, row=(None,), description=(("result",),), error=None, rows=()
    ) -> None:
        self.row = row
        self.rows = list(rows)
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


def _sent_db_config(db_config) -> dict:
    """Return the db_config value create_partition_job() actually bound."""
    connection = FakeConnection(row=(1,))
    payload = dict(VALID_PAYLOAD)
    payload["db_config"] = db_config

    @contextmanager
    def fake_connection(_kwargs):
        yield connection

    with patch.object(database, "_connection", fake_connection), patch.object(
        database, "_main_db_kwargs", return_value={}
    ):
        create_partition_job(payload)

    _sql, params = connection.executed[0]
    return params["db_config"]


class DbConfigPassThroughTests(unittest.TestCase):
    """
    The application sends db_config_para verbatim.

    Injecting or stripping keys here would hide what is actually stored, so the
    default lock_timeout problem is fixed in the database trigger instead. These
    tests pin the Python side of that contract.
    """

    def test_empty_config_is_sent_as_empty_object(self) -> None:
        sent = _sent_db_config({})
        self.assertIsInstance(sent, Jsonb)
        self.assertEqual(sent.obj, {})

    def test_extracted_settings_are_sent_unchanged(self) -> None:
        sent = _sent_db_config({"datestyle": "ISO, DMY"})
        self.assertEqual(sent.obj, {"datestyle": "ISO, DMY"})

    def test_explicit_lock_timeout_is_preserved(self) -> None:
        sent = _sent_db_config({"lock_timeout": "10s"})
        self.assertEqual(sent.obj, {"lock_timeout": "10s"})

    def test_empty_config_is_not_converted_to_null(self) -> None:
        # Sending NULL to dodge the trigger would lose the distinction between
        # "no configuration" and "not supplied".
        self.assertIsNotNone(_sent_db_config({}))

    def test_application_never_injects_a_default_lock_timeout(self) -> None:
        source_dir = os.path.dirname(os.path.abspath(__file__))
        for module_name in ("job_autofill.py", "database.py", "app.py"):
            path = os.path.join(source_dir, module_name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("30s", source, module_name)

    def test_validator_accepts_an_empty_json_object(self) -> None:
        validated = validate_form_data(
            {
                "job_name": "JOB_X",
                "is_enabled": True,
                "table_schema": "mubasher_oms",
                "table_name": "some_table",
                "db_config": "{}",
                "job_schedule": "0 0 2 26 * *",
                "frequency_amount": 1,
                "frequency_unit": "month",
                "next_run_time": datetime(2026, 8, 26, 2, 0, 0),
                "partition_unit": "month",
                "partition_period": 1,
                "is_create": True,
                "create_drop_amount": 2,
                "create_drop_unit": "month",
            }
        )
        self.assertEqual(validated["db_config"], {})


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


class ManualRunTests(unittest.TestCase):
    def _run(self, connection: FakeConnection, job_id=7):
        @contextmanager
        def fake_connection(_kwargs):
            yield connection

        with patch.object(database, "_connection", fake_connection), patch.object(
            database, "_main_db_kwargs", return_value={}
        ):
            return database.run_partition_job_manual(job_id)

    def test_uses_bound_parameter_and_manual_function(self) -> None:
        conn = FakeConnection(row=("MANUAL_SUCCESS",))
        result = self._run(conn)
        sql, params = conn.executed[0]

        self.assertEqual(result, "MANUAL_SUCCESS")
        self.assertIn("mubasher_oms.run_partition_job_manual", sql)
        self.assertIn("%(job_id)s", sql)
        self.assertEqual(params, {"job_id": 7})
        self.assertNotIn("7", sql)
        self.assertTrue(conn.committed)

    def test_does_not_touch_next_run_time(self) -> None:
        conn = FakeConnection(row=(None,))
        self._run(conn)
        sql, _params = conn.executed[0]
        self.assertNotIn("next_run_time", sql.lower())
        self.assertNotIn("update", sql.lower())

    def test_rejects_non_numeric_job_id(self) -> None:
        with self.assertRaises(DatabaseError):
            database.run_partition_job_manual("not-a-number")

    def test_rolls_back_on_failure(self) -> None:
        conn = FakeConnection(error=FakePgError("boom", sqlstate="42883"))
        with self.assertRaises(DatabaseError):
            self._run(conn)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)


class ReadOnlyQueryTests(unittest.TestCase):
    def _capture(self, loader, *args):
        conn = FakeConnection(row=None, description=None)

        @contextmanager
        def fake_connection(_kwargs):
            yield conn

        with patch.object(database, "_connection", fake_connection), patch.object(
            database, "_main_db_kwargs", return_value={}
        ):
            loader(*args)
        return conn.executed[0]

    def test_configured_jobs_query_is_read_only(self) -> None:
        sql, _params = self._capture(database.get_partition_jobs)
        lowered = sql.lower()
        self.assertIn("select", lowered)
        self.assertIn("order by job_id desc", lowered)
        self.assertIn("frequency", lowered)
        self.assertNotIn("job_frequency", lowered)
        for forbidden in ("insert", "update", "delete", "truncate", "drop "):
            self.assertNotIn(forbidden, lowered)

    def test_history_query_is_read_only_and_bounded(self) -> None:
        sql, params = self._capture(database.get_partition_job_logs, 100)
        lowered = sql.lower()
        self.assertIn("order by job_runtime desc", lowered)
        self.assertIn("%(row_limit)s", sql)
        self.assertEqual(params, {"row_limit": 100})
        for forbidden in ("insert", "update", "delete", "truncate"):
            self.assertNotIn(forbidden, lowered)

    def test_history_limit_is_clamped(self) -> None:
        _sql, params = self._capture(database.get_partition_job_logs, 10_000)
        self.assertEqual(params, {"row_limit": 1000})

    def test_configured_job_by_id_uses_bound_parameter(self) -> None:
        sql, params = self._capture(database.get_partition_job, 5)
        self.assertIn("%(job_id)s", sql)
        self.assertEqual(params, {"job_id": 5})


class ArchitectureTests(unittest.TestCase):
    """The UI must not recreate the old one-pgAgent-job-per-table design."""

    def _sources(self) -> dict[str, str]:
        sources = {}
        for name in ("app.py", "database.py", "validators.py", "job_autofill.py"):
            with open(name, "r", encoding="utf-8") as handle:
                sources[name] = handle.read()
        return sources

    def test_wrong_function_name_is_absent(self) -> None:
        for name, source in self._sources().items():
            self.assertNotIn("insert_into_partition_job_table", source, name)

    def test_job_frequency_column_is_absent(self) -> None:
        for name, source in self._sources().items():
            self.assertNotIn("job_frequency", source, name)

    def test_no_direct_insert_into_configuration_table(self) -> None:
        for name, source in self._sources().items():
            lowered = source.lower()
            self.assertNotIn("insert into mubasher_oms", lowered, name)
            self.assertNotIn("insert into {schema}", lowered, name)
            self.assertNotIn("insert into partitioning_job_table", lowered, name)

    def test_no_writes_to_pgagent_catalog(self) -> None:
        for name, source in self._sources().items():
            lowered = source.lower()
            for forbidden in (
                "insert into pgagent",
                "update pgagent",
                "delete from pgagent",
                "pgagent.pga_jobclass",
            ):
                self.assertNotIn(forbidden, lowered, name)

    def test_both_tabs_share_one_submission_pathway(self) -> None:
        import app

        source = inspect.getsource(app)
        # Exactly one place calls the database create method.
        self.assertEqual(source.count("create_partition_job(validated)"), 1)
        self.assertIn("def submit_partition_configuration", source)
        # Both prefixes flow through the same field renderer and submit button.
        self.assertIn("_render_job_fields(CONVERT_PREFIX)", source)
        self.assertIn("_render_job_fields(NEW_PREFIX)", source)
        self.assertEqual(source.count("_render_submit_button("), 3)

    def test_generic_scanner_names_are_defined(self) -> None:
        self.assertEqual(database.GENERIC_CREATE_SCANNER, "run_partition_create_jobs")
        self.assertEqual(database.GENERIC_DROP_SCANNER, "run_partition_drop_jobs")


class TimeConceptIndependenceTests(unittest.TestCase):
    """Frequency, partition period and create/drop interval must stay separate."""

    def test_three_concepts_map_to_distinct_parameters(self) -> None:
        payload = dict(VALID_PAYLOAD)
        payload["frequency"] = "1 month"
        payload["partition_period"] = 3
        payload["partition_unit"] = "week"
        payload["create_drop_interval"] = "6 months"

        conn = FakeConnection(row=(None,))

        @contextmanager
        def fake_connection(_kwargs):
            yield conn

        with patch.object(database, "_connection", fake_connection), patch.object(
            database, "_main_db_kwargs", return_value={}
        ):
            create_partition_job(payload)

        _sql, params = conn.executed[0]
        self.assertEqual(params["frequency"], "1 month")
        self.assertEqual(params["partition_period"], 3)
        self.assertEqual(params["partition_unit"], "week")
        self.assertEqual(params["create_drop_interval"], "6 months")

    def test_validator_keeps_units_independent(self) -> None:
        validated = validate_form_data(
            {
                "job_name": "JOB_X",
                "is_enabled": True,
                "table_schema": "mubasher_oms",
                "table_name": "some_table",
                "db_config": "{}",
                "job_schedule": "0 0 2 26 * *",
                "frequency_amount": 1,
                "frequency_unit": "month",
                "next_run_time": datetime(2026, 8, 26, 2, 0, 0),
                "partition_unit": "day",
                "partition_period": 7,
                "is_create": False,
                "create_drop_amount": 6,
                "create_drop_unit": "month",
            }
        )
        self.assertEqual(validated["frequency"], "1 month")
        self.assertEqual(validated["partition_unit"], "day")
        self.assertEqual(validated["partition_period"], 7)
        self.assertEqual(validated["create_drop_interval"], "6 months")
        self.assertFalse(validated["is_create"])


class CronValidationTests(unittest.TestCase):
    def test_six_field_schedules_are_accepted(self) -> None:
        for expression in (
            "0 0 2 26 * *",
            "0 */10 * * * *",
            "0 0 2 ? * MON",
            "0 0 2 1-5 * *",
            "0 0 2 * JAN *",
            "*/30 * * * * *",
        ):
            self.assertIsNone(validate_six_field_cron(expression), expression)
            self.assertEqual(validate_cron_schedule(expression), expression)

    def test_invalid_schedules_are_rejected(self) -> None:
        for expression in (
            "0 0 2 26 *",
            "0 0 99 * * *",
            "0 0 2 32 * *",
            "0 0 2 * FOO *",
            "0 0 2 * * 9",
        ):
            self.assertIsNotNone(validate_six_field_cron(expression), expression)
            with self.assertRaises(ValidationError):
                validate_cron_schedule(expression)

    def test_monthly_schedule_is_described_and_monthly(self) -> None:
        self.assertEqual(
            describe_schedule("0 0 2 26 * *"), "02:00 on day 26 of every month"
        )
        self.assertEqual(infer_frequency("0 0 2 26 * *"), ((1, "month"), None))

    def test_year_units_are_supported_end_to_end(self) -> None:
        self.assertEqual(validate_frequency_unit("year"), "year")
        self.assertEqual(validate_create_drop_unit("year"), "year")
        self.assertEqual(to_interval_string(1, "year"), "1 year")
        self.assertEqual(to_interval_string(2, "year"), "2 years")


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
