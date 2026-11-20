import asyncio
import json
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    Engine,
    InvalidDefinitionError,
    RunFailedError,
    RunHeldError,
    RunNotAvailableError,
)
from cuaderna._records import Lease, RunStatus
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


async def test_executing_a_completed_run_again_returns_its_stored_result(engine: Engine) -> None:
    executions: list[int] = []

    @cuaderna.run
    async def counted() -> int:
        executions.append(1)
        return len(executions)

    assert await engine.execute("run", counted) == 1
    assert await engine.execute("run", counted) == 1
    assert executions == [1]


async def test_a_failed_run_cannot_be_executed_again(engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        await engine.execute("run", broken, "out of budget")

    with pytest.raises(RunFailedError, match="RuntimeError"):
        await engine.execute("run", broken, "out of budget")


async def test_a_second_worker_waits_for_the_first_and_gets_the_same_result(
    engine: Engine, journal_path: Path
) -> None:
    executions: list[int] = []

    @cuaderna.run
    async def slow() -> int:
        executions.append(1)
        await asyncio.sleep(0.3)
        return len(executions)

    other_engine = Engine(journal_path, lease_seconds=0.3)
    try:
        results = await asyncio.gather(
            engine.execute("run", slow), other_engine.execute("run", slow)
        )
    finally:
        await other_engine.close()

    assert results == [1, 1]
    assert executions == [1]


async def test_a_worker_with_a_claim_timeout_gives_up_on_a_held_run(
    engine: Engine, journal_path: Path
) -> None:
    started = asyncio.Event()

    @cuaderna.run
    async def hold() -> None:
        started.set()
        await asyncio.Event().wait()

    holder = asyncio.create_task(engine.execute("run", hold))
    await started.wait()
    impatient = Engine(journal_path, claim_timeout_seconds=0.2)
    hurried = Engine(journal_path, claim_timeout_seconds=0)
    try:
        with pytest.raises(RunHeldError, match="still held"):
            await impatient.execute("run", hold)
        with pytest.raises(RunHeldError, match="still held"):
            await hurried.execute("run", hold)
    finally:
        await impatient.close()
        await hurried.close()
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)


async def test_a_claim_timeout_does_not_stop_a_run_that_is_free(journal_path: Path) -> None:
    @cuaderna.run
    async def answer() -> int:
        return 42

    hurried = Engine(journal_path, claim_timeout_seconds=0)
    try:
        assert await hurried.execute("run", answer) == 42
    finally:
        await hurried.close()


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


@pytest.mark.parametrize("slow_call", ["claim_run", "load_attempts"])
async def test_a_run_cancelled_while_it_is_being_claimed_gives_up_its_lease(
    engine: Engine, journal: Storage, monkeypatch: pytest.MonkeyPatch, slow_call: str
) -> None:
    storage = engine._storage  # pyright: ignore[reportPrivateUsage]
    answer_now = getattr(storage, slow_call)

    async def answer_late(*args: object) -> object:
        answer = await answer_now(*args)
        await asyncio.sleep(0.3)
        return answer

    monkeypatch.setattr(storage, slow_call, answer_late)
    task = asyncio.create_task(engine.execute("run", wait_forever))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await journal.claim_run("run", "another worker", 60) is not None


async def test_a_run_cancelled_twice_while_it_is_being_claimed_gives_up_its_lease(
    engine: Engine, journal: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = engine._storage  # pyright: ignore[reportPrivateUsage]
    claim_now = storage.claim_run
    claimed = asyncio.Event()
    may_answer = asyncio.Event()

    async def claim_and_answer_late(run_id: str, owner: str, lease_seconds: float) -> Lease | None:
        lease = await claim_now(run_id, owner, lease_seconds)
        claimed.set()
        await may_answer.wait()
        return lease

    monkeypatch.setattr(storage, "claim_run", claim_and_answer_late)
    task = asyncio.create_task(engine.execute("run", wait_forever))
    await claimed.wait()
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    may_answer.set()

    async with asyncio.timeout(5):
        while await journal.claim_run("run", "another worker", 60) is None:
            await asyncio.sleep(0.01)


def test_held_and_failed_runs_are_both_not_available() -> None:
    assert issubclass(RunHeldError, RunNotAvailableError)
    assert issubclass(RunFailedError, RunNotAvailableError)


def test_a_run_has_to_be_an_async_function() -> None:
    with pytest.raises(InvalidDefinitionError):

        @cuaderna.run  # pyright: ignore[reportArgumentType]
        def synchronous() -> None:  # pyright: ignore[reportUnusedFunction]
            return None


def test_a_run_cannot_be_a_method() -> None:
    with pytest.raises(InvalidDefinitionError, match="cannot be a method"):

        class Shop:  # pyright: ignore[reportUnusedClass]
            @cuaderna.run
            async def checkout(self) -> None:
                return None
