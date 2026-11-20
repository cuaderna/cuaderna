import sqlite3
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, ExperimentMarker, RunNotAvailableError
from cuaderna.storage import Storage
from cuaderna.storage._migrations import pending_statements

CONTROL = ExperimentMarker(source_run_id="source", reuse=("ask_model", "plan"))
FAULTED = ExperimentMarker(
    source_run_id="source",
    reuse=("ask_model",),
    plan_id="plan",
    plan_hash="plan-hash",
    fault_id="fault",
)
STATEMENTS_UP_TO_VERSION_7 = len(pending_statements(0)) - 2

entered: list[str] = []


@cuaderna.step(max_attempts=1)
async def work(value: str) -> str:
    entered.append(value)
    return value.upper()


@cuaderna.run
async def job(value: str) -> str:
    return await work(value)


@pytest.fixture(autouse=True)
def clean_world() -> None:
    entered.clear()


@pytest.mark.parametrize("marker", [CONTROL, FAULTED])
async def test_a_marker_round_trips_through_the_journal(
    journal: Storage, marker: ExperimentMarker
) -> None:
    created = await journal.create_run("run", "job", '{"value":"a"}', None, marker)

    assert created.experiment == marker
    assert (await journal.get_run("run")) == created
    assert (await journal.list_runs(10)) == [created]


@pytest.mark.parametrize("other", [None, FAULTED])
async def test_creating_an_existing_run_again_returns_the_stored_marker(
    journal: Storage, other: ExperimentMarker | None
) -> None:
    await journal.create_run("run", "job", '{"value":"a"}', None, CONTROL)

    again = await journal.create_run("run", "job", '{"value":"a"}', None, other)

    assert again.experiment == CONTROL


async def test_the_marker_cannot_be_updated_in_place(journal: Storage, journal_path: Path) -> None:
    await journal.create_run("run", "job", '{"value":"a"}', None, CONTROL)
    connection = sqlite3.connect(journal_path)

    with pytest.raises(sqlite3.IntegrityError, match="experiment_json is final"):
        connection.execute("UPDATE runs SET experiment_json = NULL")
    connection.close()


async def test_a_version_7_journal_migrates_with_its_runs_unmarked(
    journal: Storage, journal_path: Path
) -> None:
    connection = sqlite3.connect(journal_path)
    for statement in pending_statements(0)[:STATEMENTS_UP_TO_VERSION_7]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 7")
    connection.execute(
        "INSERT INTO runs (run_id, name, input_json, status, created_at) "
        "VALUES ('old', 'job', '[\"a\"]', 'running', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    stored = await journal.get_run("old")

    assert stored is not None
    assert stored.experiment is None


async def test_execute_refuses_a_marked_run_and_runs_no_step(
    engine: Engine, journal: Storage
) -> None:
    await journal.create_run("run-1", "job", '{"value":"a"}', None, CONTROL)

    with pytest.raises(RunNotAvailableError, match="experiment on run 'source'"):
        await engine.execute("run-1", job, "a")

    assert entered == []


async def test_a_run_created_by_execute_has_no_marker(engine: Engine) -> None:
    await engine.execute("run-1", job, "a")

    assert (await engine.run_record("run-1")).experiment is None
