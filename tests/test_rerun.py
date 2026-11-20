import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    Engine,
    ExperimentMarker,
    ReplayDivergedError,
    RunFailedError,
    RunMismatchError,
    RunNotAvailableError,
    RunStatus,
)
from tests.test_cost import TABLE, model_call


class World:
    def __init__(self) -> None:
        self.weather = "sunny"
        self.reads = 0
        self.asks = 0
        self.applied: list[str] = []


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@pytest.fixture
async def engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, prices=TABLE)
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
