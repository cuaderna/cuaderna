import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, JournalTamperedError, RunNotAvailableError
from cuaderna._records import RunStatus

stop_at: list[str] = []
entered: list[str] = []


class Stop(BaseException):
    pass


@cuaderna.step(max_attempts=1)
async def work(value: str) -> str:
    entered.append(value)
    if value in stop_at:
        raise Stop
    return value.upper()


@cuaderna.run
async def job(values: list[str]) -> list[str]:
    return [await work(value) for value in values]


@pytest.fixture(autouse=True)
def clean_world() -> None:
    stop_at.clear()
    entered.clear()


@pytest.fixture
async def other_engine(tmp_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(tmp_path / "other.db")
    yield engine
    await engine.close()


def edit_journal(journal_path: Path, statement: str) -> None:
    connection = sqlite3.connect(journal_path)
    connection.execute("DROP TRIGGER step_attempts_no_update")
    connection.execute("DROP TRIGGER step_attempts_no_delete")
    connection.execute(statement)
    connection.commit()
    connection.close()


async def test_an_imported_run_claiming_completed_is_not_executed(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run-1", job, ["a"])
    await engine.export_run("run-1", tmp_path / "run.json")
    document = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    document["run"]["output"] = ["forged"]
    (tmp_path / "run.json").write_text(json.dumps(document), encoding="utf-8")
    entered.clear()

    await other_engine.import_run(tmp_path / "run.json")

    with pytest.raises(RunNotAvailableError, match="was imported"):
        await other_engine.execute("run-1", job, ["a"])
    assert entered == []


async def test_only_an_imported_run_carries_the_import_moment(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run-1", job, ["a"])
    await engine.export_run("run-1", tmp_path / "run.json")

    await other_engine.import_run(tmp_path / "run.json")

    assert (await engine.run_record("run-1")).imported_at is None
    assert (await other_engine.run_record("run-1")).imported_at is not None


async def test_the_audit_of_an_imported_run_says_it_was_imported(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run-1", job, ["a"])
    await engine.export_run("run-1", tmp_path / "run.json")

    await other_engine.import_run(tmp_path / "run.json")

    assert [entry.imported for entry in await engine.audit("run-1")] == [False]
    assert [entry.imported for entry in await other_engine.audit("run-1")] == [True]


async def test_a_completed_run_with_an_edited_output_is_not_returned(
    engine: Engine, journal_path: Path
) -> None:
    await engine.execute("run-1", job, ["a"])
    connection = sqlite3.connect(journal_path)
    connection.execute("UPDATE runs SET output_json = '[\"forged\"]'")
    connection.commit()
    connection.close()

    with pytest.raises(JournalTamperedError):
        await engine.execute("run-1", job, ["a"])


async def test_an_unfinished_run_with_an_edited_attempt_is_not_resumed(
    engine: Engine, journal_path: Path
) -> None:
    stop_at.append("b")
    with pytest.raises(Stop):
        await engine.execute("run-1", job, ["a", "b"])
    stop_at.clear()
    entered.clear()
    edit_journal(journal_path, "UPDATE step_attempts SET result_json = '\"FORGED\"'")

    with pytest.raises(JournalTamperedError):
        await engine.execute("run-1", job, ["a", "b"])

    assert entered == []
    assert (await engine.run_record("run-1")).status is RunStatus.RUNNING
