"""Disposable PostgreSQL fixture with a restricted evaluation role."""

from __future__ import annotations

import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from skill_evolve.config.models import DatabaseConfig, EvaluatorConfig
from skill_evolve.sql.policy import SqlPolicyError, validate_read_only_sql


class FixtureError(RuntimeError):
    """The isolated PostgreSQL fixture cannot provide valid evaluator evidence."""


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class QueryExecution:
    result: QueryResult | None = None
    error: str | None = None
    unsafe: bool = False
    row_limit_exceeded: bool = False

    @property
    def ok(self) -> bool:
        return self.result is not None and self.error is None


class PostgresFixture:
    """Start a non-production Postgres container and expose a read-only query role."""

    def __init__(
        self,
        database: DatabaseConfig,
        evaluator: EvaluatorConfig,
        schema_file: Path,
        seed_file: Path,
    ) -> None:
        self._database = database
        self._evaluator = evaluator
        self._schema_file = schema_file
        self._seed_file = seed_file
        self._container_name = f"skill-evolve-{uuid.uuid4().hex[:12]}"
        self._owner_password = secrets.token_urlsafe(24)
        self._reader_password = secrets.token_urlsafe(24)
        self._port: int | None = None

    def __enter__(self) -> PostgresFixture:
        self._start_container()
        try:
            self._wait_until_ready()
            self._bootstrap()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        subprocess.run(
            ["docker", "rm", "--force", self._container_name],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )

    def execute_read_only(self, query: str) -> QueryExecution:
        try:
            validate_read_only_sql(query)
        except SqlPolicyError as error:
            return QueryExecution(error=str(error), unsafe=True)
        try:
            with (
                psycopg.connect(**self._reader_connection_kwargs()) as connection,
                connection.transaction(),
            ):
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(f"SET LOCAL statement_timeout = {self._evaluator.timeout_ms}")
                connection.execute(f"SET LOCAL lock_timeout = {self._evaluator.timeout_ms}")
                cursor = connection.execute(query)
                if cursor.description is None:
                    return QueryExecution(error="query did not return a result set", unsafe=True)
                rows = cursor.fetchmany(self._evaluator.max_rows + 1)
                if len(rows) > self._evaluator.max_rows:
                    return QueryExecution(
                        error=f"query returned more than {self._evaluator.max_rows} rows",
                        row_limit_exceeded=True,
                    )
                columns = tuple(column.name for column in cursor.description)
                return QueryExecution(
                    result=QueryResult(columns=columns, rows=tuple(tuple(row) for row in rows))
                )
        except psycopg.Error as error:
            return QueryExecution(error=_database_error_message(error))

    @property
    def container_name(self) -> str:
        """Expose the disposable container identity without exposing any credential."""

        return self._container_name

    def _start_container(self) -> None:
        image = f"{self._database.image}@{self._database.image_digest}"
        command = [
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--detach",
            "--name",
            self._container_name,
            "--env",
            "POSTGRES_USER=skill_evolve_owner",
            "--env",
            f"POSTGRES_PASSWORD={self._owner_password}",
            "--env",
            "POSTGRES_DB=skill_evolve",
            "--publish",
            "127.0.0.1::5432",
            image,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        if completed.returncode != 0:
            message = _redact_secrets(completed.stderr, self._owner_password, self._reader_password)
            raise FixtureError(f"could not start PostgreSQL fixture: {message}")
        port_output = self._docker_output(["docker", "port", self._container_name, "5432/tcp"])
        self._port = _parse_host_port(port_output)

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 20
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(**self._owner_connection_kwargs(), connect_timeout=1):
                    return
            except psycopg.OperationalError as error:
                last_error = _redact_secrets(
                    _database_error_message(error), self._owner_password, self._reader_password
                )
                time.sleep(0.25)
        raise FixtureError(f"PostgreSQL fixture did not become ready: {last_error}")

    def _bootstrap(self) -> None:
        try:
            with psycopg.connect(**self._owner_connection_kwargs(), autocommit=True) as connection:
                connection.execute(self._schema_file.read_text(encoding="utf-8"))
                connection.execute(self._seed_file.read_text(encoding="utf-8"))
                create_reader = sql.SQL("CREATE ROLE eval_reader LOGIN PASSWORD {}").format(
                    sql.Literal(self._reader_password)
                )
                connection.execute(create_reader)
                connection.execute("GRANT CONNECT ON DATABASE skill_evolve TO eval_reader")
                connection.execute("GRANT USAGE ON SCHEMA public TO eval_reader")
                connection.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO eval_reader")
        except (OSError, psycopg.Error) as error:
            message = _redact_secrets(
                _database_error_message(error), self._owner_password, self._reader_password
            )
            raise FixtureError(f"could not bootstrap PostgreSQL fixture: {message}") from error

    def _owner_connection_kwargs(self) -> dict[str, Any]:
        return self._connection_kwargs("skill_evolve_owner", self._owner_password)

    def _reader_connection_kwargs(self) -> dict[str, Any]:
        return self._connection_kwargs("eval_reader", self._reader_password)

    def _connection_kwargs(self, user: str, password: str) -> dict[str, Any]:
        if self._port is None:
            raise FixtureError("PostgreSQL fixture has not been started")
        return {
            "host": "127.0.0.1",
            "port": self._port,
            "dbname": "skill_evolve",
            "user": user,
            "password": password,
        }

    @staticmethod
    def _docker_output(command: list[str]) -> str:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise FixtureError(f"docker command failed: {completed.stderr.strip()}")
        return completed.stdout


def _parse_host_port(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("127.0.0.1:"):
            return int(line.rsplit(":", maxsplit=1)[1])
    raise FixtureError(f"docker did not expose PostgreSQL on localhost: {output.strip()}")


def _database_error_message(error: BaseException) -> str:
    message = str(error).strip().replace("\n", " ")
    return message[:1000] or type(error).__name__


def _redact_secrets(message: str, *secrets: str) -> str:
    redacted = message
    for secret in secrets:
        redacted = redacted.replace(secret, "<redacted>")
    return redacted.replace("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_REDACTED")
