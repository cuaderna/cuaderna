import json
import re
from pathlib import Path

import pytest
from examples.fix_tests import agent
from pydantic import JsonValue

from cuaderna import Engine, StepFailedError

_SITE = Path(__file__).parent.parent / "site"
_INDEX: list[dict[str, JsonValue]] = json.loads((_SITE / "runs.json").read_text(encoding="utf-8"))
_LOCAL_PATH = re.compile(r"[A-Za-z]:\\\\Users\\\\|/home/|/Users/")
_FAILED_WITH = "failed with "

_FACTS_BY_FAULT = {
    "model-timeout": (("ask_model#1",), None, 1),
    "model-down": (("ask_model#1",), None, 0),
    "tests-unavailable": (("run_tests#2",), None, 1),
    "wrong-edit": (("ask_model#1",), "ask_model#2", 0),
    "poisoned-source": (("read_sources#1",), "ask_model#1", 0),
    "poisoned-test-output": (("run_tests#1",), "ask_model#1", 0),
}


def _entry_id(entry: dict[str, JsonValue]) -> str:
    return str(entry["run_id"])


def _first_trial_of_each_fault() -> list[dict[str, JsonValue]]:
    return [entry for entry in _INDEX if entry["trial"] == 1]


def _published_files() -> list[Path]:
    return [_SITE / "sample-run.json", *sorted((_SITE / "experiments").glob("*.json"))]


def test_the_index_lists_exactly_the_published_runs() -> None:
    listed = sorted(str(entry["file"]) for entry in _INDEX)

    assert listed == sorted(path.relative_to(_SITE).as_posix() for path in _published_files())


@pytest.mark.parametrize("entry", _INDEX, ids=_entry_id)
async def test_each_entry_of_the_index_says_what_its_file_holds(
    engine: Engine, entry: dict[str, JsonValue]
) -> None:
    run_id = await engine.import_run(_SITE / str(entry["file"]))

    record = await engine.run_record(run_id)

    assert run_id == entry["run_id"]
    if entry["kind"] == "source":
        assert record.experiment is None
        return
    assert record.experiment is not None
    assert record.experiment.source_run_id == "fix-tests"
    assert record.experiment.fault_id == entry["fault"]
    assert (entry["kind"] == "control") == (entry["fault"] is None)


@pytest.mark.parametrize("entry", _INDEX, ids=_entry_id)
async def test_each_published_run_replays_to_the_end_it_recorded_without_executing_a_step(
    engine: Engine, capsys: pytest.CaptureFixture[str], entry: dict[str, JsonValue]
) -> None:
    run_id = await engine.import_run(_SITE / str(entry["file"]))
    ended = str(entry["ended"])

    if ended.startswith(_FAILED_WITH):
        with pytest.raises(StepFailedError) as raised:
            await engine.replay(run_id, agent.fix_tests)
        assert raised.value.error_type == ended.removeprefix(_FAILED_WITH)
    else:
        summary = await engine.replay(run_id, agent.fix_tests)
        assert ended == "fixed"
        assert summary.fixed is True
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("entry", _first_trial_of_each_fault(), ids=_entry_id)
async def test_the_first_trial_of_each_fault_has_the_facts_that_were_recorded(
    engine: Engine, entry: dict[str, JsonValue]
) -> None:
    run_id = await engine.import_run(_SITE / str(entry["file"]))

    experiment = await engine.experiment(run_id)

    fired, diverged_at, reused = _FACTS_BY_FAULT[str(entry["fault"])]
    assert experiment.fired == fired
    assert experiment.diverged_at == diverged_at
    assert experiment.reused == reused


@pytest.mark.parametrize("entry", _INDEX, ids=_entry_id)
async def test_each_published_run_verifies_after_import(
    engine: Engine, entry: dict[str, JsonValue]
) -> None:
    run_id = await engine.import_run(_SITE / str(entry["file"]))

    await engine.verify(run_id)


@pytest.mark.parametrize("trial", [1, 2, 3])
async def test_the_model_timeout_trials_cost_nothing_and_the_control_injects_nothing(
    engine: Engine, trial: int
) -> None:
    timeout_id = await engine.import_run(
        _SITE / "experiments" / f"fix-tests.model-timeout.{trial}.json"
    )
    control_id = await engine.import_run(_SITE / "experiments" / "fix-tests.control.json")

    cost = await engine.cost(timeout_id)
    control = await engine.experiment(control_id)
    control_audit = await engine.audit(control_id)

    assert cost.total_usd == 0
    assert control.fired == ()
    assert control.fault is None
    assert [entry.injected for entry in control_audit] == [None] * len(control_audit)


@pytest.mark.parametrize("path", _published_files(), ids=lambda path: path.name)
def test_no_published_file_holds_a_key_or_a_local_path(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert "sk-ant" not in text
    assert _LOCAL_PATH.search(text) is None
