import asyncio
import json

import pytest

import cuaderna
from cuaderna import Engine, InvalidDefinitionError, RunNotAvailableError
from cuaderna._records import RunStatus
from cuaderna.storage import Storage


@cuaderna.run
async def total(numbers: list[int]) -> int:
    return sum(numbers)


@cuaderna.run
async def broken(reason: str) -> None:
    raise RuntimeError(reason)


@cuaderna.run
async def wait_forever() -> None:
    await asyncio.Event().wait()


async def test_a_completed_run_stores_its_input_and_output(
    engine: Engine, journal: Storage
) -> None:
    assert await engine.execute("run", total, [1, 2, 3]) == 6

    record = await journal.create_run("run", "total", "{}", None)
    assert record.status is RunStatus.COMPLETED
    assert json.loads(record.input_json) == {"numbers": [1, 2, 3]}
    assert record.output_json == "6"


async def test_a_run_that_raises_is_marked_failed_and_the_error_reaches_the_caller(
    engine: Engine, journal: Storage
) -> None:
    with pytest.raises(RuntimeError, match="out of budget"):
        await engine.execute("run", broken, "out of budget")

    record = await journal.create_run("run", "broken", "{}", None)
    assert record.status is RunStatus.FAILED
    assert record.error_json is not None
    assert json.loads(record.error_json)["message"] == "out of budget"


async def test_a_finished_run_cannot_be_executed_again(engine: Engine) -> None:
    await engine.execute("run", total, [1])

    with pytest.raises(RunNotAvailableError):
        await engine.execute("run", total, [1])


async def test_a_cancelled_run_stays_unfinished_and_gives_up_its_lease(
    engine: Engine, journal: Storage
) -> None:
    task = asyncio.create_task(engine.execute("run", wait_forever))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = await journal.create_run("run", "wait_forever", "{}", None)
    assert record.status is RunStatus.RUNNING
    assert await journal.claim_run("run", "another worker", 60) is not None


def test_a_run_has_to_be_an_async_function() -> None:
    with pytest.raises(InvalidDefinitionError):

        @cuaderna.run  # pyright: ignore[reportArgumentType]
        def synchronous() -> None:  # pyright: ignore[reportUnusedFunction]
            return None
