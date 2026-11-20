import asyncio
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, JournalBusyError, LeaseLostError
from cuaderna.storage import Storage, _sqlite


@pytest.fixture(autouse=True)
def short_busy_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sqlite, "_BUSY_TIMEOUT_MS", 50)
    monkeypatch.setattr(_sqlite, "_BUSY_ATTEMPTS", 5)
    monkeypatch.setattr(_sqlite, "_BUSY_PAUSE_SECONDS", 0.1)


class ForeignWriter:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, autocommit=True)
        self.held_before = False

    def hold_the_write_lock(self) -> None:
        self.held_before = True
        self.connection.execute("BEGIN IMMEDIATE")

    def let_go(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")


@pytest.fixture
def foreign_writer(journal_path: Path, journal: Storage) -> Iterator[ForeignWriter]:
    writer = ForeignWriter(journal_path)
    yield writer
    writer.let_go()
    writer.connection.close()


def lock_once_then_release(writer: ForeignWriter, seconds: float | None) -> None:
    if writer.held_before:
        return
    writer.hold_the_write_lock()
    if seconds is not None:
        asyncio.get_running_loop().call_later(seconds, writer.let_go)


async def test_a_short_lock_delays_the_record_without_losing_it(
    journal_path: Path, journal: Storage, foreign_writer: ForeignWriter
) -> None:
    @cuaderna.step
    async def paid_call() -> int:
        lock_once_then_release(foreign_writer, 0.2)
        return 7

    @cuaderna.run
    async def flow() -> int:
        return await paid_call()

    engine = Engine(journal_path)
    assert await engine.execute("run", flow) == 7
    await engine.close()
    assert len(await journal.load_attempts("run")) == 1


async def test_a_long_lock_leaves_the_run_running_for_a_later_execute(
    journal_path: Path, journal: Storage, foreign_writer: ForeignWriter
) -> None:
    runs_of_the_step = 0

    @cuaderna.step
    async def paid_call() -> int:
        nonlocal runs_of_the_step
        runs_of_the_step += 1
        lock_once_then_release(foreign_writer, None)
        return 7

    @cuaderna.run
    async def flow() -> int:
        return await paid_call()

    engine = Engine(journal_path, lease_seconds=1.0)
    with pytest.raises(JournalBusyError):
        await engine.execute("run", flow)
    foreign_writer.let_go()
    record = await journal.get_run("run")
    assert record is not None
    assert record.status.value == "running"
    assert await engine.execute("run", flow) == 7
    await engine.close()
    assert runs_of_the_step == 2
    assert len(await journal.load_attempts("run")) == 1


async def test_a_run_that_loses_its_lease_is_cancelled(journal_path: Path) -> None:
    saw_cancellation = False

    @cuaderna.step
    async def long_step() -> None:
        nonlocal saw_cancellation
        with closing(sqlite3.connect(journal_path, autocommit=True)) as other:
            other.execute("UPDATE runs SET lease_token = lease_token + 1")
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            saw_cancellation = True
            raise

    @cuaderna.run
    async def flow() -> None:
        await long_step()

    engine = Engine(journal_path, lease_seconds=0.15)
    with pytest.raises(LeaseLostError):
        await engine.execute("run", flow)
    await engine.close()
    assert saw_cancellation


@pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX feature")
async def test_the_journal_and_an_exported_run_are_private(
    journal_path: Path, tmp_path: Path
) -> None:
    @cuaderna.run
    async def flow() -> int:
        return 1

    engine = Engine(journal_path)
    await engine.execute("run", flow)
    exported = tmp_path / "run.json"
    await engine.export_run("run", exported)
    modes = {
        path.name: path.stat().st_mode & 0o777
        for path in (journal_path, journal_path.with_name("journal.db-wal"), exported)
    }
    await engine.close()
    assert modes == {"journal.db": 0o600, "journal.db-wal": 0o600, "run.json": 0o600}
