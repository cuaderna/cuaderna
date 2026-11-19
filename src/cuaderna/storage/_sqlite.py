import asyncio
import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cuaderna._records import RunRecord, RunStatus
from cuaderna.storage._migrations import LATEST_VERSION, pending_statements

_RUN_COLUMNS = (
    "run_id, name, input_json, code_version, status, output_json, error_json, "
    "created_at, finished_at"
)


class SqliteStorage:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cuaderna-sqlite")
        self._connection: sqlite3.Connection | None = None

    async def create_run(
        self, run_id: str, name: str, input_json: str, code_version: str | None
    ) -> RunRecord:
        return await self._call(
            lambda connection: _create_run(connection, run_id, name, input_json, code_version)
        )

    async def close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._executor, self._close_connection)
        self._executor.shutdown()

    async def _call[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, lambda: operation(self._open_connection())
        )

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = _connect(self._path)
        return self._connection

    def _close_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, autocommit=True)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        _migrate(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    with _write_transaction(connection):
        (current_version,) = connection.execute("PRAGMA user_version").fetchone()
        for statement in pending_statements(current_version):
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {LATEST_VERSION}")


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _create_run(
    connection: sqlite3.Connection,
    run_id: str,
    name: str,
    input_json: str,
    code_version: str | None,
) -> RunRecord:
    with _write_transaction(connection):
        connection.execute(
            "INSERT INTO runs (run_id, name, input_json, code_version, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (run_id) DO NOTHING",
            (run_id, name, input_json, code_version, RunStatus.RUNNING.value, _now_text()),
        )
        row = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return _run_from_row(row)


def _run_from_row(row: tuple[Any, ...]) -> RunRecord:
    finished_at = row[8]
    return RunRecord(
        run_id=row[0],
        name=row[1],
        input_json=row[2],
        code_version=row[3],
        status=RunStatus(row[4]),
        output_json=row[5],
        error_json=row[6],
        created_at=datetime.fromisoformat(row[7]),
        finished_at=None if finished_at is None else datetime.fromisoformat(finished_at),
    )


def _now_text() -> str:
    return _to_text(datetime.now(UTC))


def _to_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
