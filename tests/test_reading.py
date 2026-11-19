import pytest

import cuaderna
from cuaderna import AttemptStatus, Engine, RunNotFoundError, RunStatus


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def flaky_double(number: int) -> int:
    if not failed_once:
        failed_once.append(True)
        raise ConnectionError("try again")
    return number * 2


failed_once: list[bool] = []


@pytest.fixture(autouse=True)
def fresh_state() -> None:
    failed_once.clear()


@cuaderna.run
async def double_twice(number: int) -> int:
    return await flaky_double(await flaky_double(number))


async def test_a_finished_run_can_be_read_with_every_attempt(engine: Engine) -> None:
    await engine.execute("run-1", double_twice, 3)

    record = await engine.run_record("run-1")
    steps = await engine.steps("run-1")

    assert (record.name, record.status, record.output_json) == (
        "double_twice",
        RunStatus.COMPLETED,
        "12",
    )
    assert [(step.step_key, step.attempt, step.status) for step in steps] == [
        ("flaky_double#1", 1, AttemptStatus.ERROR),
        ("flaky_double#1", 2, AttemptStatus.OK),
        ("flaky_double#2", 1, AttemptStatus.OK),
    ]


async def test_runs_are_listed_newest_first(engine: Engine) -> None:
    for run_id in ("run-1", "run-2", "run-3"):
        await engine.execute(run_id, double_twice, 1)

    assert [record.run_id for record in await engine.runs()] == ["run-3", "run-2", "run-1"]
    assert [record.run_id for record in await engine.runs(limit=1)] == ["run-3"]


async def test_reading_a_run_that_does_not_exist_fails_and_does_not_create_it(
    engine: Engine,
) -> None:
    with pytest.raises(RunNotFoundError):
        await engine.run_record("missing")
    with pytest.raises(RunNotFoundError):
        await engine.steps("missing")

    assert await engine.runs() == []
