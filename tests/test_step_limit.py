from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, InvalidDefinitionError, RunStatus

bodies_that_ran: list[int] = []


@pytest.fixture(autouse=True)
def forget_bodies() -> None:
    bodies_that_ran.clear()


@asynccontextmanager
async def engine_capped_at(journal_path: Path, max_steps: int | None) -> AsyncGenerator[Engine]:
    engine = Engine(journal_path, max_steps=max_steps)
    try:
        yield engine
    finally:
        await engine.close()


@cuaderna.step
async def work(number: int) -> int:
    bodies_that_ran.append(number)
    return number


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def always_fails() -> int:
    bodies_that_ran.append(-1)
    raise ValueError("no")


@cuaderna.run
async def five_steps() -> int:
    total = 0
    for number in range(5):
        total += await work(number)
    return total


@cuaderna.run
async def retrying() -> int:
    return await always_fails()


async def test_the_cap_stops_the_run_before_the_step_past_it_runs(journal_path: Path) -> None:
    async with engine_capped_at(journal_path, 3) as engine:
        with pytest.raises(cuaderna.StepLimitExceededError) as raised:
            await engine.execute("run-1", five_steps)

        record = await engine.run_record("run-1")

    assert bodies_that_ran == [0, 1, 2]
    assert (raised.value.run_id, raised.value.limit, raised.value.recorded) == ("run-1", 3, 3)
    assert record.status is RunStatus.RUNNING
    assert record.output_json is None


async def test_retries_count_against_the_cap(journal_path: Path) -> None:
    async with engine_capped_at(journal_path, 2) as engine:
        with pytest.raises(cuaderna.StepLimitExceededError) as raised:
            await engine.execute("run-1", retrying)

        attempts = await engine.steps("run-1")

    assert bodies_that_ran == [-1, -1]
    assert len(attempts) == 2
    assert raised.value.recorded == 2


async def test_a_resumed_run_counts_what_the_journal_already_holds(journal_path: Path) -> None:
    async with engine_capped_at(journal_path, 2) as first:
        with pytest.raises(cuaderna.StepLimitExceededError):
            await first.execute("run-1", five_steps)

    async with engine_capped_at(journal_path, 3) as second:
        with pytest.raises(cuaderna.StepLimitExceededError) as raised:
            await second.execute("run-1", five_steps)

    assert bodies_that_ran == [0, 1, 2]
    assert raised.value.recorded == 3


async def test_a_later_execute_with_a_higher_cap_finishes_the_run(journal_path: Path) -> None:
    async with engine_capped_at(journal_path, 2) as capped:
        with pytest.raises(cuaderna.StepLimitExceededError):
            await capped.execute("run-1", five_steps)

    async with engine_capped_at(journal_path, 5) as roomy:
        total = await roomy.execute("run-1", five_steps)
        record = await roomy.run_record("run-1")

    assert total == 10
    assert bodies_that_ran == [0, 1, 2, 3, 4]
    assert record.status is RunStatus.COMPLETED


async def test_a_run_with_no_cap_is_not_limited(journal_path: Path) -> None:
    async with engine_capped_at(journal_path, None) as engine:
        assert await engine.execute("run-1", five_steps) == 10


@pytest.mark.parametrize("cap", [0, -1, True])
def test_an_invalid_cap_is_refused(journal_path: Path, cap: int) -> None:
    with pytest.raises(InvalidDefinitionError):
        Engine(journal_path, max_steps=cap)
