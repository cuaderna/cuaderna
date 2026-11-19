import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue

import cuaderna
from cuaderna import Engine, JournalTamperedError, RunNotFoundError
from cuaderna._chain import first_link, verified_head
from cuaderna.storage._migrations import pending_statements
from cuaderna.storage._sqlite import SqliteStorage

from .storage.contract import attempt_for

STATEMENTS_UP_TO_VERSION_3 = 12
HEX_64 = re.compile(r"[0-9a-f]{64}")


@cuaderna.step(max_attempts=1)
async def record(value: str) -> str:
    cuaderna.annotate({"note": "ñ", "nested": {"b": 1, "a": [1.5]}})
    return value.upper()


@cuaderna.run
async def chain(values: list[str]) -> list[str]:
    return [await record(value) for value in values]


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


def blank_result(path: str, value: JsonValue) -> JsonValue:
    return "x" if path.endswith(".result") else value


async def test_a_run_verifies_and_returns_a_head(engine: Engine) -> None:
    await engine.execute("run-1", chain, ["a", "b", "c"])

    head = await engine.verify("run-1")

    assert HEX_64.fullmatch(head)
    assert await engine.verify("run-1") == head


async def test_a_run_with_no_attempts_returns_the_first_link(engine: Engine) -> None:
    await engine.execute("empty", chain, [])
    record_of_run = await engine.run_record("empty")

    assert await engine.verify("empty") == first_link(record_of_run.name, record_of_run.input_json)


async def test_runs_with_different_content_have_different_heads(engine: Engine) -> None:
    await engine.execute("one", chain, ["a", "b"])
    await engine.execute("two", chain, ["a", "c"])

    assert await engine.verify("one") != await engine.verify("two")


async def test_an_edited_attempt_is_found(engine: Engine, journal_path: Path) -> None:
    await engine.execute("run-1", chain, ["a", "b", "c"])
    edit_journal(
        journal_path,
        "UPDATE step_attempts SET result_json = '\"X\"' WHERE step_key = 'record#2'",
    )

    with pytest.raises(JournalTamperedError) as raised:
        await engine.verify("run-1")

    assert (raised.value.run_id, raised.value.step_key, raised.value.attempt) == (
        "run-1",
        "record#2",
        1,
    )


async def test_a_removed_middle_attempt_is_found_at_the_next_one(
    engine: Engine, journal_path: Path
) -> None:
    await engine.execute("run-1", chain, ["a", "b", "c"])
    edit_journal(journal_path, "DELETE FROM step_attempts WHERE step_key = 'record#2'")

    with pytest.raises(JournalTamperedError) as raised:
        await engine.verify("run-1")

    assert raised.value.step_key == "record#3"


async def test_a_removed_last_attempt_changes_the_head(engine: Engine, journal_path: Path) -> None:
    await engine.execute("run-1", chain, ["a", "b", "c"])
    head = await engine.verify("run-1")
    edit_journal(journal_path, "DELETE FROM step_attempts WHERE step_key = 'record#3'")

    assert await engine.verify("run-1") != head


@pytest.mark.parametrize("as_run_id", [None, "copy"])
async def test_an_exported_run_verifies_in_another_journal(
    engine: Engine, other_engine: Engine, tmp_path: Path, as_run_id: str | None
) -> None:
    await engine.execute("run-1", chain, ["a", "b"])
    await engine.export_run("run-1", tmp_path / "run.json")

    run_id = await other_engine.import_run(tmp_path / "run.json", as_run_id=as_run_id)

    assert await other_engine.verify(run_id) == await engine.verify("run-1")


async def test_a_redacted_export_does_not_verify(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run-1", chain, ["a", "b"])
    await engine.export_run("run-1", tmp_path / "run.json", redact=blank_result)
    run_id = await other_engine.import_run(tmp_path / "run.json")

    with pytest.raises(JournalTamperedError):
        await other_engine.verify(run_id)


async def test_a_version_3_journal_keeps_recording_without_hashes(journal_path: Path) -> None:
    connection = sqlite3.connect(journal_path)
    for statement in pending_statements(0)[:STATEMENTS_UP_TO_VERSION_3]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 3")
    connection.execute(
        "INSERT INTO runs (run_id, name, input_json, status, created_at, lease_owner, lease_token, "
        "lease_expires_at) VALUES "
        "('run', 'fix_tests', '{}', 'running', '2026-01-01T00:00:00+00:00', 'worker', 1, 0)"
    )
    connection.execute(
        "INSERT INTO step_attempts (run_id, step_key, name, attempt, status, args_hash, "
        "args_json, started_at, finished_at) VALUES "
        "('run', 'step#1', 'step', 1, 'ok', 'h', '[]', "
        "'2026-01-01T00:00:00.000000+00:00', '2026-01-01T00:00:00.000000+00:00')"
    )
    connection.commit()
    connection.close()
    storage = SqliteStorage(journal_path)
    try:
        lease = await storage.claim_run("run", "worker-b", lease_seconds=60)
        assert lease is not None
        await storage.record_attempt(lease, attempt_for("step#2"))
        attempts = await storage.load_attempts("run")
        record_of_run = await storage.get_run("run")
    finally:
        await storage.close()

    assert [attempt.chain_hash for attempt in attempts] == [None, None]
    assert record_of_run is not None
    with pytest.raises(JournalTamperedError, match="no chain hash"):
        verified_head("run", record_of_run, attempts)


async def test_verifying_an_unknown_run_raises_what_cost_raises(engine: Engine) -> None:
    with pytest.raises(RunNotFoundError):
        await engine.cost("nope")
    with pytest.raises(RunNotFoundError):
        await engine.verify("nope")
