"""Unit tests for AWS Secrets Manager / env database credential resolution."""

from __future__ import annotations

import inspect
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import db_credentials
from db_credentials import (
    AUTH_SQLSTATES,
    DatabaseCredentialError,
    credential_provider_status,
    get_database_secret,
    invalidate_database_secret_cache,
    is_authentication_error,
    resolve_database_credentials,
    secret_provider,
)


class _FakeAuthError(Exception):
    """Minimal stand-in with sqlstate for auth detection tests."""

    def __init__(self, sqlstate: str, message: str = "auth failed") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class DbCredentialsEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        db_credentials._reset_for_tests()
        self._env = patch.dict(
            os.environ,
            {
                "PARTITION_DB_SECRET_PROVIDER": "env",
                "DB_USER": "partuser",
                "DB_PASSWORD": "env-secret-password",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        db_credentials._reset_for_tests()

    def test_env_provider_returns_user_and_password(self) -> None:
        creds = resolve_database_credentials()
        self.assertEqual(creds["user"], "partuser")
        self.assertEqual(creds["password"], "env-secret-password")

    def test_env_provider_does_not_initialize_boto3(self) -> None:
        with patch.object(db_credentials, "_secrets_manager_client") as client_fn:
            resolve_database_credentials()
            client_fn.assert_not_called()

    def test_default_provider_is_env(self) -> None:
        with patch.dict(os.environ, {"PARTITION_DB_SECRET_PROVIDER": ""}, clear=False):
            self.assertEqual(secret_provider(), "env")

    def test_env_missing_password_fails(self) -> None:
        with patch.dict(os.environ, {"DB_PASSWORD": ""}, clear=False):
            with self.assertRaises(DatabaseCredentialError):
                resolve_database_credentials()

    def test_env_missing_user_fails(self) -> None:
        with patch.dict(os.environ, {"DB_USER": ""}, clear=False):
            with self.assertRaises(DatabaseCredentialError):
                resolve_database_credentials()


class DbCredentialsAwsTests(unittest.TestCase):
    def setUp(self) -> None:
        db_credentials._reset_for_tests()
        self._env = patch.dict(
            os.environ,
            {
                "PARTITION_DB_SECRET_PROVIDER": "aws",
                "PARTITION_DB_SECRET_ID": "partops/example/db-credentials",
                "AWS_REGION": "eu-west-1",
                "DB_USER": "fallback_user",
                "DB_PASSWORD": "must-never-be-used",
                "PARTITION_DB_SECRET_CACHE_TTL_SECONDS": "300",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        db_credentials._reset_for_tests()

    def _client_returning(self, payload: dict) -> MagicMock:
        client = MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": __import__("json").dumps(payload)
        }
        return client

    def test_aws_calls_secrets_manager(self) -> None:
        client = self._client_returning(
            {"username": "sm_user", "password": "sm_pass"}
        )
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            creds = resolve_database_credentials()
        client.get_secret_value.assert_called_once()
        self.assertEqual(creds["user"], "sm_user")
        self.assertEqual(creds["password"], "sm_pass")

    def test_username_from_secret_supported(self) -> None:
        client = self._client_returning(
            {"username": "from_secret", "password": "p"}
        )
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            creds = resolve_database_credentials()
        self.assertEqual(creds["user"], "from_secret")

    def test_absent_username_falls_back_to_db_user(self) -> None:
        client = self._client_returning({"password": "only_pass"})
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            creds = resolve_database_credentials()
        self.assertEqual(creds["user"], "fallback_user")
        self.assertEqual(creds["password"], "only_pass")

    def test_malformed_json_fails(self) -> None:
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": "{not-json"}
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertIn("not valid JSON", str(ctx.exception))
        self.assertNotIn("must-never-be-used", str(ctx.exception))

    def test_missing_secret_string_fails(self) -> None:
        client = MagicMock()
        client.get_secret_value.return_value = {}
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                get_database_secret()
        self.assertIn("no SecretString", str(ctx.exception))

    def test_missing_password_fails(self) -> None:
        client = self._client_returning({"username": "u"})
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertIn("password", str(ctx.exception).lower())

    def test_access_denied_fails_closed(self) -> None:
        client = MagicMock()
        err = Exception("denied")
        err.response = {"Error": {"Code": "AccessDeniedException"}}  # type: ignore[attr-defined]
        client.get_secret_value.side_effect = err
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertIn("AccessDenied", str(ctx.exception))
        self.assertNotIn("must-never-be-used", str(ctx.exception))

    def test_resource_not_found_fails_closed(self) -> None:
        client = MagicMock()
        err = Exception("missing")
        err.response = {"Error": {"Code": "ResourceNotFoundException"}}  # type: ignore[attr-defined]
        client.get_secret_value.side_effect = err
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertIn("ResourceNotFound", str(ctx.exception))

    def test_generic_aws_failure_fails_closed(self) -> None:
        client = MagicMock()
        client.get_secret_value.side_effect = RuntimeError("network down")
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertIn("Secrets Manager retrieval failed", str(ctx.exception))
        self.assertNotIn("must-never-be-used", str(ctx.exception))

    def test_aws_never_falls_back_to_db_password(self) -> None:
        client = MagicMock()
        client.get_secret_value.side_effect = RuntimeError("boom")
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError):
                resolve_database_credentials()
        # Even after failure, env password must not be returned by a retry path.
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertNotIn("must-never-be-used", str(ctx.exception))

    def test_secret_is_cached_within_ttl(self) -> None:
        client = self._client_returning({"password": "p1", "username": "u1"})
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            first = resolve_database_credentials()
            second = resolve_database_credentials()
        self.assertEqual(first, second)
        self.assertEqual(client.get_secret_value.call_count, 1)

    def test_ttl_expiration_refreshes_secret(self) -> None:
        with patch.dict(
            os.environ, {"PARTITION_DB_SECRET_CACHE_TTL_SECONDS": "10"}, clear=False
        ):
            client = MagicMock()
            client.get_secret_value.side_effect = [
                {"SecretString": '{"password":"p1","username":"u1"}'},
                {"SecretString": '{"password":"p2","username":"u2"}'},
            ]
            mono = {"t": 1000.0}

            def fake_mono() -> float:
                return mono["t"]

            with patch.object(
                db_credentials, "_secrets_manager_client", return_value=client
            ), patch("db_credentials.time.monotonic", side_effect=fake_mono):
                c1 = resolve_database_credentials()
                c2 = resolve_database_credentials()
                mono["t"] = 1020.0  # past TTL
                c3 = resolve_database_credentials()
        self.assertEqual(c1["password"], "p1")
        self.assertEqual(c2["password"], "p1")
        self.assertEqual(c3["password"], "p2")
        self.assertEqual(client.get_secret_value.call_count, 2)

    def test_cache_invalidation_works(self) -> None:
        client = MagicMock()
        client.get_secret_value.side_effect = [
            {"SecretString": '{"password":"a","username":"u"}'},
            {"SecretString": '{"password":"b","username":"u"}'},
        ]
        with patch.object(db_credentials, "_secrets_manager_client", return_value=client):
            first = resolve_database_credentials()
            invalidate_database_secret_cache()
            second = resolve_database_credentials()
        self.assertEqual(first["password"], "a")
        self.assertEqual(second["password"], "b")
        self.assertEqual(client.get_secret_value.call_count, 2)

    def test_cache_is_thread_safe_enough(self) -> None:
        client = self._client_returning({"password": "p", "username": "u"})
        errors = []

        def worker() -> None:
            try:
                with patch.object(
                    db_credentials, "_secrets_manager_client", return_value=client
                ):
                    resolve_database_credentials()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertGreaterEqual(client.get_secret_value.call_count, 1)

    def test_password_absent_from_exception_messages(self) -> None:
        client = MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": '{"password":"super-secret-xyz","username":"u"}'
        }
        # Force a parse path that shouldn't embed password in our errors:
        # missing PARTITION_DB_SECRET_ID
        with patch.dict(os.environ, {"PARTITION_DB_SECRET_ID": ""}, clear=False):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                resolve_database_credentials()
        self.assertNotIn("super-secret-xyz", str(ctx.exception))
        self.assertNotIn("must-never-be-used", str(ctx.exception))

    def test_auth_sqlstates_recognized(self) -> None:
        self.assertTrue(is_authentication_error(_FakeAuthError("28P01")))
        self.assertTrue(is_authentication_error(_FakeAuthError("28000")))
        self.assertFalse(is_authentication_error(_FakeAuthError("08006")))
        self.assertEqual(AUTH_SQLSTATES, frozenset({"28000", "28P01"}))

    def test_credential_provider_status_safe(self) -> None:
        status = credential_provider_status()
        self.assertEqual(status["credential_provider"], "aws")
        blob = str(status)
        self.assertNotIn("must-never-be-used", blob)
        self.assertNotIn("SecretString", blob)
        self.assertTrue(status.get("secret_id_configured"))


class IntegrationPathContractTests(unittest.TestCase):
    def test_api_db_path_uses_resolver(self) -> None:
        import database

        source = inspect.getsource(database._main_db_kwargs)
        self.assertIn("resolve_database_credentials", source)
        self.assertNotIn('_require_env("DB_PASSWORD")', source)

    def test_scheduler_db_path_uses_resolver(self) -> None:
        from scheduler_backend import scheduler_database

        source = inspect.getsource(scheduler_database._db_kwargs)
        self.assertIn("resolve_database_credentials", source)
        self.assertNotIn('_require("DB_PASSWORD")', source)

    def test_resolver_does_not_load_dotenv_files(self) -> None:
        source = inspect.getsource(db_credentials)
        self.assertNotIn("load_dotenv", source)
        self.assertNotIn("from dotenv", source)
        self.assertNotIn("import dotenv", source)

    def test_auth_retry_once_in_api_connection(self) -> None:
        import database

        source = inspect.getsource(database._connect_psycopg_with_auth_retry)
        self.assertIn("invalidate_database_secret_cache", source)
        self.assertIn("is_authentication_error", source)

    def test_auth_retry_once_in_scheduler_connection(self) -> None:
        from scheduler_backend import scheduler_database

        source = inspect.getsource(scheduler_database._connect_with_auth_retry)
        self.assertIn("invalidate_database_secret_cache", source)
        # Only one retry path (two connect attempts max).
        self.assertEqual(source.count("return connect("), 2)

    def test_api_connection_auth_retry_behaviour(self) -> None:
        import database
        from psycopg import Error as PsycopgError

        class AuthErr(PsycopgError):
            sqlstate = "28P01"

        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return {
                "host": "127.0.0.1",
                "port": 5432,
                "dbname": "x",
                "user": "u",
                "password": "p",
            }

        with patch("database.is_authentication_error", return_value=True), patch(
            "database.invalidate_database_secret_cache"
        ) as inv, patch("psycopg.connect") as connect:
            connect.side_effect = [AuthErr("bad"), MagicMock()]
            conn = database._connect_psycopg_with_auth_retry(factory)
        self.assertIsNotNone(conn)
        self.assertEqual(connect.call_count, 2)
        inv.assert_called_once()
        self.assertEqual(calls["n"], 2)

    def test_non_auth_failure_does_not_retry(self) -> None:
        import database
        from psycopg import Error as PsycopgError

        class ConnErr(PsycopgError):
            sqlstate = "08006"

        with patch("database.is_authentication_error", return_value=False), patch(
            "database.invalidate_database_secret_cache"
        ) as inv, patch("psycopg.connect", side_effect=ConnErr("down")):
            with self.assertRaises(ConnErr):
                database._connect_psycopg_with_auth_retry(lambda: {"host": "x"})
        inv.assert_not_called()

    def test_python39_compatible_module_syntax(self) -> None:
        source = open(
            os.path.join(os.path.dirname(__file__), "db_credentials.py"),
            encoding="utf-8",
        ).read()
        # Avoid match/case and walrus-heavy patterns; keep typing Optional-friendly.
        self.assertNotIn("\nmatch ", source)


class LoggingSafetyTests(unittest.TestCase):
    def test_module_logs_do_not_reference_password_fields(self) -> None:
        source = inspect.getsource(db_credentials)
        # Logging calls must not interpolate password/secret string payloads.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("logger."):
                lower = stripped.lower()
                self.assertNotIn("password", lower)
                self.assertNotIn("secretstring", lower)
                self.assertNotIn("secret_binary", lower)


if __name__ == "__main__":
    unittest.main()
