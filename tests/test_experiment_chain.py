import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

import cuaderna
from cuaderna import Engine, ExperimentMarker, JournalTamperedError, RunFileError
from cuaderna._chain import first_link

PINNED_FIRST_LINK = "79d395a4db03963eee9e058a1f0310217d0bfb4b660dff10110472dae74e046e"
MARKER = ExperimentMarker("source", ("shout",))


@pytest.fixture
async def other_engine(tmp_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(tmp_path / "other.db")
    yield engine
    await engine.close()


@cuaderna.step
async def shout(word: str) -> str:
    return word.upper()


@cuaderna.run
async def echo(word: str) -> str:
    return await shout(word)


async def engine_with_control(engine: Engine) -> Engine:
    await engine.execute("source", echo, "a")
    await engine.rerun("source", echo, as_run_id="control", reuse=[shout])
    return engine


def edit_marker(journal_path: Path, marker_json: str | None) -> None:
    connection = sqlite3.connect(journal_path)
    connection.execute("DROP TRIGGER runs_experiment_is_final")
    connection.execute(
        "UPDATE runs SET experiment_json = ? WHERE run_id = 'control'", (marker_json,)
    )
    connection.commit()
    connection.close()


async def exported_document(engine: Engine, run_id: str, path: Path) -> dict[str, Any]:
    await engine.export_run(run_id, path)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def test_the_first_link_of_an_ordinary_run_is_what_it_always_was() -> None:
    assert first_link("job", "[1]", None) == PINNED_FIRST_LINK


async def test_a_control_verifies_and_its_marker_changes_the_first_link(engine: Engine) -> None:
    await engine_with_control(engine)

    assert await engine.verify("control")
    assert first_link("echo", "{}", MARKER) != first_link("echo", "{}", None)


@pytest.mark.parametrize(
    "marker_json",
    [None, ExperimentMarker("other", ("shout",)).to_json()],
    ids=["removed", "edited"],
)
async def test_a_marker_changed_in_the_journal_is_found(
    engine: Engine, journal_path: Path, marker_json: str | None
) -> None:
    await engine_with_control(engine)
    edit_marker(journal_path, marker_json)

    with pytest.raises(JournalTamperedError):
        await engine.verify("control")


@pytest.mark.parametrize("as_run_id", [None, "copy"])
async def test_an_exported_experiment_is_imported_as_an_experiment(
    engine: Engine, other_engine: Engine, tmp_path: Path, as_run_id: str | None
) -> None:
    await engine_with_control(engine)
    document = await exported_document(engine, "control", tmp_path / "run.json")

    run_id = await other_engine.import_run(tmp_path / "run.json", as_run_id=as_run_id)

    assert document["format"] == "cuaderna.run/2"
    assert (await other_engine.run_record(run_id)).experiment == MARKER
    assert await other_engine.verify(run_id)
    assert await other_engine.replay(run_id, echo) == "A"


async def test_the_file_of_an_ordinary_run_has_no_experiment(
    engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("source", echo, "a")

    document = await exported_document(engine, "source", tmp_path / "run.json")

    assert document["format"] == "cuaderna.run/1"
    assert "experiment" not in document["run"]


async def test_a_redacted_export_keeps_the_marker(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    def hide_everything(path: str, value: JsonValue) -> JsonValue:
        return "***"

    await engine_with_control(engine)
    await engine.export_run("control", tmp_path / "run.json", redact=hide_everything)

    await other_engine.import_run(tmp_path / "run.json")

    assert (await other_engine.run_record("control")).experiment == MARKER


async def test_an_experiment_section_edited_by_hand_is_found_after_the_import(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine_with_control(engine)
    document = await exported_document(engine, "control", tmp_path / "run.json")
    document["run"]["experiment"]["source_run_id"] = "another"
    (tmp_path / "edited.json").write_text(json.dumps(document), encoding="utf-8")

    await other_engine.import_run(tmp_path / "edited.json")

    with pytest.raises(JournalTamperedError):
        await other_engine.verify("control")


def without_key(section: dict[str, Any]) -> None:
    del section["fault_id"]


def with_unknown_key(section: dict[str, Any]) -> None:
    section["owner"] = "ana"


def with_wrong_type(section: dict[str, Any]) -> None:
    section["reuse"] = "shout"


@pytest.mark.parametrize("damage", [without_key, with_unknown_key, with_wrong_type])
async def test_a_malformed_experiment_section_is_refused(
    engine: Engine, other_engine: Engine, tmp_path: Path, damage: Callable[[dict[str, Any]], None]
) -> None:
    await engine_with_control(engine)
    document = await exported_document(engine, "control", tmp_path / "run.json")
    damage(document["run"]["experiment"])
    (tmp_path / "damaged.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RunFileError):
        await other_engine.import_run(tmp_path / "damaged.json")

    assert await other_engine.runs() == []


async def test_a_first_format_file_cannot_hold_an_experiment(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine_with_control(engine)
    document = await exported_document(engine, "control", tmp_path / "run.json")
    document["format"] = "cuaderna.run/1"
    (tmp_path / "old.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RunFileError):
        await other_engine.import_run(tmp_path / "old.json")


async def test_a_second_format_file_without_a_marker_is_an_ordinary_run(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("source", echo, "a")
    document = await exported_document(engine, "source", tmp_path / "run.json")
    document["format"] = "cuaderna.run/2"
    (tmp_path / "second.json").write_text(json.dumps(document), encoding="utf-8")

    await other_engine.import_run(tmp_path / "second.json")

    assert (await other_engine.run_record("source")).experiment is None
