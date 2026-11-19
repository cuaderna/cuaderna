import asyncio
import sqlite3
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cuaderna._errors import JournalConflictError, LeaseLostError
from cuaderna._records import AttemptStatus, Lease, RunRecord, RunStatus, StepAttempt
from cuaderna.storage._migrations import LATEST_VERSION, pending_statements

_RUN_COLUMNS = (
    "run_id, name, input_json, code_version, status, output_json, error_json, "
    "created_at, finished_at"
)
_ATTEMPT_COLUMNS = (
    "step_key, name, attempt, status, args_hash, args_json, result_json, error_json, "
    "started_at, finished_at"
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

    async def claim_run(self, run_id: str, owner: str, lease_seconds: float) -> Lease | None:
        return await self._call(
            lambda connection: _claim_run(connection, run_id, owner, lease_seconds)
        )

    async def renew_lease(self, lease: Lease, lease_seconds: float) -> None:
        await self._call(lambda connection: _renew_lease(connection, lease, lease_seconds))

    async def record_attempt(self, lease: Lease, attempt: StepAttempt) -> None:
        await self._call(lambda connection: _record_attempt(connection, lease, attempt))

    async def load_attempts(self, run_id: str) -> list[StepAttempt]:
        return await self._call(lambda connection: _load_attempts(connection, run_id))

    async def complete_run(self, lease: Lease, output_json: str) -> None:
        await self._call(
            lambda connection: _finish_run(
                connection, lease, RunStatus.COMPLETED, output_json=output_json, error_json=None
            )
        )

    async def fail_run(self, lease: Lease, error_json: str) -> None:
        await self._call(
            lambda connection: _finish_run(
                connection, lease, RunStatus.FAILED, output_json=None, error_json=error_json
            )
        )

    async def release_run(self, lease: Lease) -> None:
        await self._call(lambda connection: _release_run(connection, lease))

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


def _claim_run(
    connection: sqlite3.Connection, run_id: str, owner: str, lease_seconds: float
) -> Lease | None:
    now = time.time()
    with _write_transaction(connection):
        row = connection.execute(
            "UPDATE runs SET lease_owner = ?, lease_token = lease_token + 1, lease_expires_at = ? "
            "WHERE run_id = ? AND status = ? "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
            "RETURNING lease_token",
            (owner, now + lease_seconds, run_id, RunStatus.RUNNING.value, now),
        ).fetchone()
    if row is None:
        return None
    return Lease(run_id=run_id, owner=owner, token=row[0])


def _renew_lease(connection: sqlite3.Connection, lease: Lease, lease_seconds: float) -> None:
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE runs SET lease_expires_at = ? "
            "WHERE run_id = ? AND lease_token = ? AND status = ?",
            (time.time() + lease_seconds, lease.run_id, lease.token, RunStatus.RUNNING.value),
        )
        _require_lease_held(cursor, lease)


def _record_attempt(connection: sqlite3.Connection, lease: Lease, attempt: StepAttempt) -> None:
    with _write_transaction(connection):
        cursor = connection.execute(
            "SELECT 1 FROM runs WHERE run_id = ? AND lease_token = ? AND status = ?",
            (lease.run_id, lease.token, RunStatus.RUNNING.value),
        )
        if cursor.fetchone() is None:
            raise _lease_lost(lease)
        try:
            connection.execute(
                f"INSERT INTO step_attempts (run_id, {_ATTEMPT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lease.run_id,
                    attempt.step_key,
                    attempt.name,
                    attempt.attempt,
                    attempt.status.value,
                    attempt.args_hash,
                    attempt.args_json,
                    attempt.result_json,
                    attempt.error_json,
                    _to_text(attempt.started_at),
                    _to_text(attempt.finished_at),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise JournalConflictError(
                f"attempt {attempt.attempt} of step {attempt.step_key!r} is already recorded "
                f"for run {lease.run_id!r}"
            ) from error


def _load_attempts(connection: sqlite3.Connection, run_id: str) -> list[StepAttempt]:
    rows = connection.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM step_attempts WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [_attempt_from_row(row) for row in rows]


def _finish_run(
    connection: sqlite3.Connection,
    lease: Lease,
    status: RunStatus,
    *,
    output_json: str | None,
    error_json: str | None,
) -> None:
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE runs SET status = ?, output_json = ?, error_json = ?, finished_at = ?, "
            "lease_owner = NULL, lease_expires_at = NULL "
            "WHERE run_id = ? AND lease_token = ? AND status = ?",
            (
                status.value,
                output_json,
                error_json,
                _now_text(),
                lease.run_id,
                lease.token,
                RunStatus.RUNNING.value,
            ),
        )
        _require_lease_held(cursor, lease)


def _release_run(connection: sqlite3.Connection, lease: Lease) -> None:
    with _write_transaction(connection):
        connection.execute(
            "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL "
            "WHERE run_id = ? AND lease_token = ?",
            (lease.run_id, lease.token),
        )


def _require_lease_held(cursor: sqlite3.Cursor, lease: Lease) -> None:
    if cursor.rowcount == 0:
        raise _lease_lost(lease)


def _lease_lost(lease: Lease) -> LeaseLostError:
    return LeaseLostError(
        f"{lease.owner!r} no longer holds run {lease.run_id!r}: "
        "it was claimed by another worker or has finished"
    )


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


def _attempt_from_row(row: tuple[Any, ...]) -> StepAttempt:
    return StepAttempt(
        step_key=row[0],
        name=row[1],
        attempt=row[2],
        status=AttemptStatus(row[3]),
        args_hash=row[4],
        args_json=row[5],
        result_json=row[6],
        error_json=row[7],
        started_at=datetime.fromisoformat(row[8]),
        finished_at=datetime.fromisoformat(row[9]),
    )


def _now_text() -> str:
    return _to_text(datetime.now(UTC))


def _to_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
