import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    Engine,
    ExperimentMarker,
    InvalidDefinitionError,
    PermissionDeniedError,
    Policy,
    ReplayDivergedError,
    RunFailedError,
    RunMismatchError,
    RunNotAvailableError,
    RunNotFoundError,
    RunStatus,
)
from cuaderna._run import RunDefinition
from tests.test_cost import TABLE, model_call


class Crash(BaseException):
    pass


class World:
    def __init__(self) -> None:
        self.weather = "sunny"
        self.reads = 0
        self.asks = 0
        self.applied: list[str] = []
        self.crash_on_apply = False


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@pytest.fixture
async def engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, prices=TABLE)
    yield engine
    await engine.close()


@pytest.fixture
async def other_engine(tmp_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(tmp_path / "other.db")
    yield engine
    await engine.close()


@cuaderna.step
async def read_world() -> str:
    world.reads += 1
    return world.weather


@cuaderna.step
async def ask(question: str) -> str:
    world.asks += 1
    cuaderna.annotate(model_call("small"))
    return f"answer to {question}"


@cuaderna.step
async def apply(answer: str) -> str:
    if world.crash_on_apply:
        raise Crash
    world.applied.append(answer)
    return answer


@cuaderna.run
async def investigate(swallow: bool) -> str:
    weather = await read_world()
    try:
        answer = await ask(f"what to do when it is {weather}?")
    except ReplayDivergedError:
        if swallow:
            return "gave up"
        raise
    return await apply(answer)


@cuaderna.run
async def stamped() -> str:
    return f"{await cuaderna.now()} {await cuaderna.random()} {await cuaderna.new_id()}"


def only_ask_is_checked(effect: str) -> Policy:
    allow_the_rest: dict[str, object] = {
        "id": "rest",
        "effect": "allow",
        "steps": ["read_world", "apply"],
        "when": {},
    }
    model: dict[str, object] = {"id": "model", "effect": effect, "steps": ["ask"], "when": {}}
    return Policy({"format": "cuaderna.policy/1", "id": "p", "rules": [allow_the_rest, model]})


async def annotations_of(engine: Engine, run_id: str, step_key: str) -> dict[str, object]:
    attempts = await engine.steps(run_id)
    attempt = next(attempt for attempt in attempts if attempt.step_key == step_key)
    return json.loads(attempt.annotations_json or "{}")


async def test_a_control_executes_the_effects_again_and_serves_the_reused_step(
    engine: Engine,
) -> None:
    source_output = await engine.execute("source", investigate, False)
    source_record = await engine.run_record("source")

    output = await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])

    assert output == source_output
    assert (world.reads, world.asks, len(world.applied)) == (2, 1, 2)
    served = {"run_id": "source", "step_key": "ask#1", "attempt": 1}
    assert await annotations_of(engine, "control", "ask#1") == {"cuaderna.reuse": served}
    assert (await engine.run_record("control")).experiment == ExperimentMarker("source", ("ask",))
    assert await engine.run_record("source") == source_record


@pytest.mark.parametrize("swallow", [False, True])
async def test_a_control_stops_when_the_world_changed_even_if_the_run_swallows_it(
    engine: Engine, swallow: bool
) -> None:
    await engine.execute("source", investigate, swallow)
    world.weather = "rainy"

    with pytest.raises(ReplayDivergedError, match="other arguments"):
        await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])

    assert world.asks == 1
    assert (await engine.run_record("control")).status is RunStatus.FAILED
    with pytest.raises(RunFailedError):
        await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])


async def test_execute_refuses_the_id_of_a_rerun(engine: Engine) -> None:
    await engine.execute("source", investigate, False)
    await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])

    with pytest.raises(RunNotAvailableError):
        await engine.execute("control", investigate, False)


@pytest.mark.parametrize(
    ("made_from", "made_reusing"), [("other", ["ask"]), ("source", []), (None, [])]
)
async def test_rerun_refuses_an_id_made_as_another_experiment(
    engine: Engine, made_from: str | None, made_reusing: list[str]
) -> None:
    await engine.execute("source", investigate, False)
    await engine.execute("other", investigate, False)
    if made_from is None:
        await engine.execute("control", investigate, False)
    else:
        await engine.rerun(made_from, investigate, as_run_id="control", reuse=made_reusing)

    with pytest.raises(RunMismatchError, match="not an experiment on run 'source'"):
        await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])


async def test_a_step_function_and_its_name_make_the_same_experiment(engine: Engine) -> None:
    await engine.execute("source", investigate, False)
    await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])

    await engine.rerun("source", investigate, as_run_id="control", reuse=["ask", ask])

    assert world.reads == 2


async def test_a_killed_control_resumes_without_repeating_what_it_recorded(
    engine: Engine,
) -> None:
    await engine.execute("source", investigate, False)
    world.crash_on_apply = True
    with pytest.raises(Crash):
        await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])
    world.crash_on_apply = False

    await engine.rerun("source", investigate, as_run_id="control", reuse=[ask])

    steps = await engine.steps("control")
    assert [attempt.step_key for attempt in steps] == ["read_world#1", "ask#1", "apply#1"]
    assert (world.reads, world.asks) == (2, 1)
    assert (await engine.run_record("control")).status is RunStatus.COMPLETED


async def test_the_source_can_be_an_imported_run(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("source", investigate, False)
    await engine.export_run("source", tmp_path / "run.json")
    await other_engine.import_run(tmp_path / "run.json")

    await other_engine.rerun("source", investigate, as_run_id="control", reuse=[ask])

    assert world.asks == 1
    assert (await other_engine.run_record("control")).status is RunStatus.COMPLETED


async def test_a_reused_call_adds_nothing_to_a_budget_smaller_than_the_source_cost(
    engine: Engine,
) -> None:
    await engine.execute("source", investigate, False)
    assert (await engine.cost("source")).total_usd > Decimal("0.001")

    await engine.rerun("source", investigate.with_budget("0.001"), as_run_id="control", reuse=[ask])

    assert world.asks == 1


async def test_an_allowed_reused_call_records_its_authorization_next_to_the_reuse(
    engine: Engine,
) -> None:
    await engine.execute("source", investigate, False)

    await engine.rerun(
        "source",
        investigate.with_policy(only_ask_is_checked("allow")),
        as_run_id="control",
        reuse=[ask],
    )

    assert set(await annotations_of(engine, "control", "ask#1")) == {
        "cuaderna.reuse",
        "cuaderna.authorization",
    }


async def test_a_denied_reused_call_is_not_served(engine: Engine) -> None:
    await engine.execute("source", investigate, False)

    with pytest.raises(PermissionDeniedError):
        await engine.rerun(
            "source",
            investigate.with_policy(only_ask_is_checked("deny")),
            as_run_id="control",
            reuse=[ask],
        )

    [denied] = [a for a in await engine.steps("control") if a.step_key == "ask#1"]
    assert denied.result_json is None


async def test_library_steps_return_the_values_of_the_source(engine: Engine) -> None:
    source_output = await engine.execute("source", stamped)

    output = await engine.rerun("source", stamped, as_run_id="control", reuse=[])

    assert output == source_output


@pytest.mark.parametrize(
    ("source_id", "run", "reuse", "error"),
    [
        ("missing", investigate, [ask], RunNotFoundError),
        ("source", stamped, [], RunMismatchError),
        ("source", investigate, [len], InvalidDefinitionError),
    ],
)
async def test_rerun_refuses_what_it_cannot_start_from(
    engine: Engine,
    source_id: str,
    run: RunDefinition[..., str],
    reuse: list[Callable[..., object] | str],
    error: type[Exception],
) -> None:
    await engine.execute("source", investigate, False)

    with pytest.raises(error):
        await engine.rerun(source_id, run, as_run_id="control", reuse=reuse)
