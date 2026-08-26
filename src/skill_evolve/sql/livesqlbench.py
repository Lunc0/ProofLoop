"""Disposable PostgreSQL fixture restored from LiveSQLBench table dumps."""

from __future__ import annotations

import re
import secrets
import subprocess
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from skill_evolve.config.models import EvaluatorConfig
from skill_evolve.sql.policy import SqlPolicyError, validate_read_only_sql
from skill_evolve.sql.postgres import FixtureError, QueryExecution, QueryResult

_DATABASE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_DUMP_ITEM_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


class LiveSQLBenchFixtureError(FixtureError):
    """A table dump fixture cannot provide valid, isolated evaluator evidence."""


@dataclass(frozen=True)
class ScopedReadOnlyExecutor:
    """Read-only executor limited by credentials to one benchmark database."""

    _fixture: LiveSQLBenchFixture
    database_name: str

    def execute_read_only(self, query: str) -> QueryExecution:
        return self._fixture._execute_read_only(self.database_name, query)


class LiveSQLBenchFixture:
    """Restore selected Base-Lite table dumps into isolated databases in one container.

    All dump SQL is executed only by ``psql`` inside a disposable PostgreSQL container.
    Each case receives a distinct reader role with access to exactly one database.
    """

    def __init__(
        self,
        dump_root: Path,
        database_names: Iterable[str],
        evaluator: EvaluatorConfig,
        image: str = "postgres:14.12",
    ) -> None:
        names = tuple(dict.fromkeys(database_names))
        if not names:
            raise LiveSQLBenchFixtureError("at least one LiveSQLBench database is required")
        if any(_DATABASE_NAME_PATTERN.fullmatch(name) is None for name in names):
            raise LiveSQLBenchFixtureError("LiveSQLBench database names must be safe identifiers")
        self._dump_root = dump_root.resolve(strict=True)
        self._database_names = names
        self._evaluator = evaluator
        self._image = image
        self._container_name = f"skill-evolve-livesqlbench-{uuid.uuid4().hex[:12]}"
        self._owner_password = secrets.token_urlsafe(24)
        self._reader_passwords = {name: secrets.token_urlsafe(24) for name in names}
        self._port: int | None = None

    def __enter__(self) -> LiveSQLBenchFixture:
        self._validate_dump_layout()
        self._start_container()
        try:
            self._wait_until_ready()
            self._restore_dumps()
            self._create_reader_roles()
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

    def for_database(self, database_name: str) -> ScopedReadOnlyExecutor:
        if database_name not in self._reader_passwords:
            raise LiveSQLBenchFixtureError(f"database {database_name!r} was not restored")
        return ScopedReadOnlyExecutor(self, database_name)

    def _validate_dump_layout(self) -> None:
        for database_name in self._database_names:
            dump_directory = self._dump_root / f"{database_name}_template"
            order_file = self._dump_root / f"{database_name}_table_orders.txt"
            if not dump_directory.is_dir():
                raise LiveSQLBenchFixtureError(f"missing dump directory: {dump_directory}")
            if not order_file.is_file():
                raise LiveSQLBenchFixtureError(f"missing table order file: {order_file}")
            table_names = _read_table_order(order_file)
            if not table_names:
                raise LiveSQLBenchFixtureError(f"table order file is empty: {order_file}")
            for table_name in table_names:
                _resolve_dump_file(dump_directory, table_name)

    def _start_container(self) -> None:
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
            self._image,
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
            raise LiveSQLBenchFixtureError(
                "could not start LiveSQLBench PostgreSQL fixture: "
                + _redact_secrets(
                    completed.stderr,
                    self._owner_password,
                    *self._reader_passwords.values(),
                )
            )
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
                    _database_error_message(error),
                    self._owner_password,
                    *self._reader_passwords.values(),
                )
                time.sleep(0.25)
        raise LiveSQLBenchFixtureError(
            f"LiveSQLBench PostgreSQL fixture did not become ready: {last_error}"
        )

    def _restore_dumps(self) -> None:
        self._run_docker(["docker", "exec", self._container_name, "mkdir", "--parents", "/dumps"])
        with psycopg.connect(**self._owner_connection_kwargs(), autocommit=True) as connection:
            connection.execute("CREATE ROLE root NOLOGIN")
            connection.execute("REVOKE CONNECT ON DATABASE skill_evolve FROM PUBLIC")
            for database_name in self._database_names:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER skill_evolve_owner").format(
                        sql.Identifier(_fixture_database_name(database_name))
                    )
                )
                connection.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(_fixture_database_name(database_name))
                    )
                )
        for database_name in self._database_names:
            self._copy_dump_directory(database_name)
            with psycopg.connect(
                **self._owner_connection_kwargs(_fixture_database_name(database_name)),
                autocommit=True,
            ) as connection:
                connection.execute("CREATE EXTENSION IF NOT EXISTS hstore")
                connection.execute("CREATE EXTENSION IF NOT EXISTS citext")
            for table_name in _read_table_order(
                self._dump_root / f"{database_name}_table_orders.txt"
            ):
                dump_file = _resolve_dump_file(
                    self._dump_root / f"{database_name}_template", table_name
                )
                self._restore_table_dump(database_name, dump_file.name)

    def _copy_dump_directory(self, database_name: str) -> None:
        source = self._dump_root / f"{database_name}_template"
        self._run_docker(
            [
                "docker",
                "cp",
                str(source),
                f"{self._container_name}:/dumps/{database_name}_template",
            ]
        )

    def _restore_table_dump(self, database_name: str, dump_file_name: str) -> None:
        dump_file = f"/dumps/{database_name}_template/{dump_file_name}"
        completed = subprocess.run(
            [
                "docker",
                "exec",
                "--env",
                f"PGPASSWORD={self._owner_password}",
                self._container_name,
                "psql",
                "--username",
                "skill_evolve_owner",
                "--dbname",
                _fixture_database_name(database_name),
                "--set",
                "ON_ERROR_STOP=1",
                "--file",
                dump_file,
            ],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=60,
        )
        if completed.returncode != 0:
            error = _redact_secrets(
                completed.stderr, self._owner_password, *self._reader_passwords.values()
            )
            raise LiveSQLBenchFixtureError(
                f"could not restore {database_name}.{dump_file_name}: {error.strip()[:1000]}"
            )

    def _create_reader_roles(self) -> None:
        with psycopg.connect(**self._owner_connection_kwargs(), autocommit=True) as connection:
            for database_name, password in self._reader_passwords.items():
                role_name = _reader_role_name(database_name)
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role_name), sql.Literal(password)
                    )
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(_fixture_database_name(database_name)),
                        sql.Identifier(role_name),
                    )
                )
        for database_name in self._database_names:
            role_name = _reader_role_name(database_name)
            with psycopg.connect(
                **self._owner_connection_kwargs(_fixture_database_name(database_name)),
                autocommit=True,
            ) as connection:
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role_name))
                )
                connection.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                        sql.Identifier(role_name)
                    )
                )

    def _execute_read_only(self, database_name: str, query: str) -> QueryExecution:
        try:
            validate_read_only_sql(query)
        except SqlPolicyError as error:
            return QueryExecution(error=str(error), unsafe=True)
        try:
            with (
                psycopg.connect(**self._reader_connection_kwargs(database_name)) as connection,
                connection.transaction(),
            ):
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(f"SET LOCAL statement_timeout = {self._evaluator.timeout_ms}")
                connection.execute(f"SET LOCAL lock_timeout = {self._evaluator.timeout_ms}")
                connection.execute("SET LOCAL search_path TO public")
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

    def _owner_connection_kwargs(self, database_name: str = "skill_evolve") -> dict[str, Any]:
        return self._connection_kwargs("skill_evolve_owner", self._owner_password, database_name)

    def _reader_connection_kwargs(self, database_name: str) -> dict[str, Any]:
        return self._connection_kwargs(
            _reader_role_name(database_name),
            self._reader_passwords[database_name],
            _fixture_database_name(database_name),
        )

    def _connection_kwargs(self, user: str, password: str, database_name: str) -> dict[str, Any]:
        if self._port is None:
            raise LiveSQLBenchFixtureError("fixture has not been started")
        return {
            "host": "127.0.0.1",
            "port": self._port,
            "dbname": database_name,
            "user": user,
            "password": password,
        }

    def _run_docker(self, command: list[str]) -> None:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise LiveSQLBenchFixtureError(
                _redact_secrets(
                    completed.stderr,
                    self._owner_password,
                    *self._reader_passwords.values(),
                )
            )

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
            raise LiveSQLBenchFixtureError(f"docker command failed: {completed.stderr.strip()}")
        return completed.stdout


def _read_table_order(path: Path) -> tuple[str, ...]:
    try:
        names = tuple(path.read_text(encoding="utf-8").split())
    except OSError as error:
        raise LiveSQLBenchFixtureError(
            f"could not read table order file {path}: {error}"
        ) from error
    if any(_DUMP_ITEM_PATTERN.fullmatch(name) is None for name in names):
        raise LiveSQLBenchFixtureError(f"table order contains an unsafe table name: {path}")
    if len(names) != len(set(names)):
        raise LiveSQLBenchFixtureError(f"table order contains duplicates: {path}")
    return names


def _resolve_dump_file(dump_directory: Path, table_name: str) -> Path:
    """Resolve an official table-order identifier to one safe, local ``.sql`` file.

    Polar uses PascalCase identifiers in its table-order file but lowercase dump
    filenames.  Matching is therefore case-insensitive, while the directory
    glob and identifier checks still forbid arbitrary paths.
    """

    matches = tuple(
        path
        for path in dump_directory.glob("*.sql")
        if path.stem.casefold() == table_name.casefold() and path.is_file()
    )
    if len(matches) != 1:
        raise LiveSQLBenchFixtureError(
            f"missing or ambiguous ordered table dump for {table_name!r} in {dump_directory}"
        )
    return matches[0]


def _fixture_database_name(database_name: str) -> str:
    return f"livesqlbench_{database_name}"


def _reader_role_name(database_name: str) -> str:
    return f"eval_reader_{database_name}"


def _parse_host_port(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("127.0.0.1:"):
            return int(line.rsplit(":", maxsplit=1)[1])
    raise LiveSQLBenchFixtureError(
        f"docker did not expose PostgreSQL on localhost: {output.strip()}"
    )


def _database_error_message(error: BaseException) -> str:
    message = str(error).strip().replace("\n", " ")
    return message[:1000] or type(error).__name__


def _redact_secrets(message: str, *secrets_to_redact: str) -> str:
    redacted = message
    for secret in secrets_to_redact:
        redacted = redacted.replace(secret, "<redacted>")
    return redacted.replace("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_REDACTED")
