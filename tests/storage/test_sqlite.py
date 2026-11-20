import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cuaderna._errors import SchemaVersionError
from cuaderna._records import StepAttempt
from cuaderna.storage import Storage
from cuaderna.storage._sqlite import SqliteStorage

from .contract import StorageContract

_VERSION_1_JOURNAL = Path(__file__).parent / "journal_v1.sql"

_ENDLESS_WRITER = """
import asyncio
import sys
from datetime import UTC, datetime

from cuaderna._records import AttemptStatus, StepAttempt
from cuaderna.storage._sqlite import SqliteStorage


async def main(path: str) -> None:
    storage = SqliteStorage(path)
    await storage.create_run("run", "writer", "{}", None)
    lease = await storage.claim_run("run", "writer", 60)
    index = 0
    while True:
        moment = datetime.now(UTC)
        attempt = StepAttempt(
            f"step#{index}", "step", 1, AttemptStatus.OK, "hash", "[]", "1", None, moment, moment
        )
        await storage.record_attempt(lease, attempt)
        print(index, flush=True)
        index += 1


asyncio.run(main(sys.argv[1]))
"""


class TestSqliteStorage(StorageContract):
    @pytest.fixture
    async def storage(self, tmp_path: Path) -> AsyncIterator[Storage]:
        storage = SqliteStorage(tmp_path / "journal.db")
        yield storage
        await storage.close()


def test_attempts_acknowledged_before_a_kill_survive_it(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    writer = subprocess.Popen(
        [sys.executable, "-c", _ENDLESS_WRITER, str(path)], stdout=subprocess.PIPE, text=True
    )
    assert writer.stdout is not None
    acknowledged = [int(writer.stdout.readline()) for _ in range(20)]
    writer.kill()
    acknowledged += [int(line) for line in writer.stdout.read().split()]
    writer.wait()
    writer.stdout.close()

    survivors = asyncio.run(_load_attempts(path))

    assert [attempt.step_key for attempt in survivors[: len(acknowledged)]] == [
        f"step#{index}" for index in acknowledged
    ]


async def test_a_journal_from_a_newer_library_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    storage = SqliteStorage(path)

    with pytest.raises(SchemaVersionError):
        await storage.create_run("run", "fix_tests", "{}", None)

    await storage.close()


async def test_a_journal_whose_version_was_reset_raises_the_library_error(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    storage = SqliteStorage(path)
    await storage.create_run("run", "fix_tests", "{}", None)
    await storage.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.close()
    storage = SqliteStorage(path)

    with pytest.raises(SchemaVersionError) as raised:
        await storage.create_run("run-2", "fix_tests", "{}", None)

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    await storage.close()


async def test_a_version_1_journal_is_migrated_in_place_and_keeps_its_attempts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.db"
    connection = sqlite3.connect(path)
    connection.executescript(_VERSION_1_JOURNAL.read_text(encoding="utf-8"))
    connection.close()
    storage = SqliteStorage(path)

    old_attempts = await storage.load_attempts("run-1")
    await storage.create_run("run-2", "fix_tests", "{}", None)
    lease = await storage.claim_run("run-2", "worker", 60)
    assert lease is not None
    await storage.record_attempt(lease, old_attempts[0])
    await storage.close()

    assert [
        (attempt.step_key, attempt.attempt, attempt.lease_token, attempt.annotations_json)
        for attempt in old_attempts
    ] == [
        ("flaky_double#1", 1, None, None),
        ("flaky_double#1", 2, None, None),
        ("flaky_double#2", 1, None, None),
    ]
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone() == (5,)
    assert connection.execute(
        "SELECT lease_token FROM step_attempts WHERE run_id = 'run-2'"
    ).fetchall() == [(lease.token,)]
    connection.close()


async def test_several_connections_can_open_a_new_journal_at_once(tmp_path: Path) -> None:
    for round_number in range(10):
        path = tmp_path / f"journal-{round_number}.db"
        storages = [SqliteStorage(path) for _ in range(8)]

        records = await asyncio.gather(
            *(storage.create_run("run", "fix_tests", "{}", None) for storage in storages)
        )

        assert {record.run_id for record in records} == {"run"}
        for storage in storages:
            await storage.close()


async def _load_attempts(path: Path) -> list[StepAttempt]:
    storage = SqliteStorage(path)
    attempts = await storage.load_attempts("run")
    await storage.close()
    return attempts
