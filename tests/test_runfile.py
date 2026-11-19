import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue

import cuaderna
from cuaderna import (
    Engine,
    JournalConflictError,
    ReplayDivergedError,
    RunFileError,
    RunNotAvailableError,
    RunStatus,
)


class Kill(BaseException):
    pass


class World:
    def __init__(self) -> None:
        self.entered: list[str] = []
        self.failures_left = 0
        self.kill_at: str | None = None


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@pytest.fixture
async def other_engine(tmp_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(tmp_path / "other.db")
    yield engine
    await engine.close()


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def visit(place: str, password: str = "") -> str:
    if world.kill_at == place:
        raise Kill(place)
    world.entered.append(place)
    cuaderna.annotate({"guide": "ana"})
    if world.failures_left > 0:
        world.failures_left -= 1
        raise ConnectionError("try again")
    return place.upper()


@cuaderna.run
async def trip(places: list[str], password: str = "") -> list[str]:
    return [await visit(place, password) for place in places]


async def test_an_exported_run_replays_in_another_journal(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    world.failures_left = 1
    await engine.execute("run-1", trip, ["a", "ñ"])
    await engine.export_run("run-1", tmp_path / "run.json")
    world.entered.clear()

    run_id = await other_engine.import_run(tmp_path / "run.json")

    assert run_id == "run-1"
    assert await other_engine.replay(run_id, trip) == ["A", "Ñ"]
    assert await other_engine.execute(run_id, trip, ["a", "ñ"]) == ["A", "Ñ"]
    assert world.entered == []
    assert await other_engine.run_record(run_id) == await engine.run_record("run-1")
    await other_engine.export_run(run_id, tmp_path / "again.json")
    assert (tmp_path / "again.json").read_bytes() == (tmp_path / "run.json").read_bytes()


async def test_a_run_file_holds_json_values_and_not_text(engine: Engine, tmp_path: Path) -> None:
    world.failures_left = 1
    await engine.execute("run-1", trip, ["a"])

    await engine.export_run("run-1", tmp_path / "run.json")

    document = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert document["format"] == "cuaderna.run/1"
    assert document["run"]["input"] == {"places": ["a"], "password": ""}
    assert (document["run"]["status"], document["run"]["output"]) == ("completed", ["A"])
    failed, succeeded = document["attempts"]
    assert (failed["status"], failed["result"], failed["error"]["type"]) == (
        "error",
        None,
        "ConnectionError",
    )
    assert (succeeded["step_key"], succeeded["attempt"], succeeded["result"]) == ("visit#1", 2, "A")
    assert succeeded["args"] == {"place": "a", "password": ""}
    assert succeeded["annotations"] == {"guide": "ana"}
    assert succeeded["lease_token"] == 1


async def test_a_run_id_already_in_the_journal_needs_a_new_one(
    engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run-1", trip, ["a"])
    await engine.export_run("run-1", tmp_path / "run.json")

    with pytest.raises(JournalConflictError):
        await engine.import_run(tmp_path / "run.json")
    copy_id = await engine.import_run(tmp_path / "run.json", as_run_id="run-1-copy")

    assert copy_id == "run-1-copy"
    assert await engine.replay("run-1-copy", trip) == ["A"]
    assert len(await engine.steps("run-1")) == 1


async def test_an_unfinished_run_is_imported_for_reading_and_never_resumed(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    world.kill_at = "b"
    with pytest.raises(Kill):
        await engine.execute("run-1", trip, ["a", "b"])
    world.kill_at = None
    await engine.export_run("run-1", tmp_path / "run.json")
    world.entered.clear()

    await other_engine.import_run(tmp_path / "run.json")

    assert (await other_engine.run_record("run-1")).status is RunStatus.IMPORTED_UNFINISHED
    assert [step.step_key for step in await other_engine.steps("run-1")] == ["visit#1"]
    with pytest.raises(RunNotAvailableError, match="imported unfinished"):
        await other_engine.execute("run-1", trip, ["a", "b"])
    with pytest.raises(ReplayDivergedError, match="visit#2"):
        await other_engine.replay("run-1", trip)
    assert world.entered == []


async def test_redaction_sees_every_stored_value_and_its_result_is_what_gets_written(
    engine: Engine, tmp_path: Path
) -> None:
    world.failures_left = 1
    await engine.execute("run-1", trip, ["a"], "hunter2")
    seen: list[str] = []

    def hide_password(path: str, value: JsonValue) -> JsonValue:
        seen.append(path)
        if isinstance(value, dict) and "password" in value:
            return {**value, "password": "***"}
        return value

    await engine.export_run("run-1", tmp_path / "run.json", redact=hide_password)

    text = (tmp_path / "run.json").read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert json.loads(text)["attempts"][1]["args"] == {"place": "a", "password": "***"}
    assert seen == [
        "input",
        "output",
        "visit#1.args",
        "visit#1.error",
        "visit#1.annotations",
        "visit#1.args",
        "visit#1.result",
        "visit#1.annotations",
    ]


async def test_an_edited_run_file_is_imported_as_edited(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run-1", trip, ["a"])
    await engine.export_run("run-1", tmp_path / "run.json")
    document = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    document["run"]["input"]["places"] = ["z"]
    document["run"]["output"] = ["Z"]
    document["attempts"][0]["args"]["place"] = "z"
    document["attempts"][0]["result"] = "Z"
    (tmp_path / "edited.json").write_text(json.dumps(document), encoding="utf-8")

    await other_engine.import_run(tmp_path / "edited.json")

    assert await other_engine.replay("run-1", trip) == ["Z"]


@pytest.mark.parametrize(
    "text",
    ["not json", "{}", '{"format": "cuaderna.run/2", "run": {}, "attempts": []}'],
)
async def test_a_file_that_is_not_a_known_run_file_is_refused(
    engine: Engine, tmp_path: Path, text: str
) -> None:
    (tmp_path / "run.json").write_text(text, encoding="utf-8")

    with pytest.raises(RunFileError):
        await engine.import_run(tmp_path / "run.json")

    assert await engine.runs() == []
