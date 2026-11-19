import asyncio
import json
import threading

import pytest
from pydantic import BaseModel

import cuaderna
from cuaderna import Engine, InvalidDefinitionError, StepCallError, StepFailedError
from cuaderna._records import AttemptStatus
from cuaderna.storage import Storage


class Report(BaseModel):
    failed: int
    names: list[str]


@cuaderna.step
async def double(number: int) -> int:
    return number * 2


@cuaderna.step
def thread_name(label: str) -> str:
    return f"{label}:{threading.current_thread().name}"


@cuaderna.step(name="inspect_repo")
async def build_report(failed: int, *names: str) -> Report:
    return Report(failed=failed, names=list(names))


@cuaderna.step
async def explode(reason: str) -> None:
    raise ValueError(reason)


@cuaderna.step
async def call_a_step_inside(number: int) -> int:
    return await double(number)


async def test_a_finished_step_is_recorded_with_its_arguments_and_result(
    engine: Engine, journal: Storage
) -> None:
    @cuaderna.run
    async def flow(number: int) -> int:
        return await double(number)

    assert await engine.execute("run", flow, 21) == 42

    [attempt] = await journal.load_attempts("run")
    assert (attempt.step_key, attempt.name, attempt.attempt) == ("double#1", "double", 1)
    assert attempt.status is AttemptStatus.OK
    assert json.loads(attempt.args_json) == {"number": 21}
    assert attempt.result_json == "42"


async def test_parallel_calls_get_their_keys_in_call_order(
    engine: Engine, journal: Storage
) -> None:
    @cuaderna.run
    async def flow() -> list[int]:
        return list(await asyncio.gather(double(3), double(2), double(1)))

    assert await engine.execute("run", flow) == [6, 4, 2]

    attempts = await journal.load_attempts("run")
    arguments_by_key = {
        attempt.step_key: json.loads(attempt.args_json)["number"] for attempt in attempts
    }
    assert arguments_by_key == {"double#1": 3, "double#2": 2, "double#3": 1}


async def test_the_same_arguments_hash_the_same_and_different_ones_do_not(
    engine: Engine, journal: Storage
) -> None:
    @cuaderna.run
    async def flow() -> None:
        await double(1)
        await double(number=1)
        await double(2)

    await engine.execute("run", flow)

    first, second, third = await journal.load_attempts("run")
    assert first.args_hash == second.args_hash
    assert first.args_hash != third.args_hash


async def test_a_synchronous_step_runs_in_a_thread(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> str:
        return await thread_name("worker")

    result = await engine.execute("run", flow)

    assert result.startswith("worker:")
    assert result != f"worker:{threading.current_thread().name}"


async def test_models_and_variadic_arguments_are_stored_as_json(
    engine: Engine, journal: Storage
) -> None:
    @cuaderna.run
    async def flow() -> Report:
        return await build_report(2, "test_a", "test_b")

    assert await engine.execute("run", flow) == Report(failed=2, names=["test_a", "test_b"])

    [attempt] = await journal.load_attempts("run")
    assert attempt.step_key == "inspect_repo#1"
    assert json.loads(attempt.args_json) == {"failed": 2, "names": ["test_a", "test_b"]}
    assert attempt.result_json is not None
    assert json.loads(attempt.result_json) == {"failed": 2, "names": ["test_a", "test_b"]}


async def test_a_failing_step_is_recorded_and_raises_step_failed(
    engine: Engine, journal: Storage
) -> None:
    @cuaderna.run
    async def flow() -> None:
        await explode("disk full")

    with pytest.raises(StepFailedError) as raised:
        await engine.execute("run", flow)

    assert (raised.value.step_key, raised.value.error_type) == ("explode#1", "ValueError")
    assert raised.value.message == "disk full"
    assert isinstance(raised.value.__cause__, ValueError)
    [attempt] = await journal.load_attempts("run")
    assert attempt.status is AttemptStatus.ERROR
    assert attempt.error_json is not None
    assert json.loads(attempt.error_json)["type"] == "ValueError"


async def test_a_result_that_does_not_match_the_annotation_fails_the_step(
    engine: Engine, journal: Storage
) -> None:
    @cuaderna.step
    async def lie() -> int:
        return "not a number"  # pyright: ignore[reportReturnType]

    @cuaderna.run
    async def flow() -> int:
        return await lie()

    with pytest.raises(StepFailedError):
        await engine.execute("run", flow)

    [attempt] = await journal.load_attempts("run")
    assert attempt.status is AttemptStatus.ERROR


async def test_a_step_cannot_be_called_outside_a_run() -> None:
    with pytest.raises(StepCallError):
        await double(1)


async def test_a_step_cannot_be_called_inside_another_step(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> int:
        return await call_a_step_inside(1)

    with pytest.raises(StepFailedError) as raised:
        await engine.execute("run", flow)

    assert raised.value.error_type == "StepCallError"


def test_a_step_without_a_return_annotation_is_rejected_when_decorated() -> None:
    with pytest.raises(InvalidDefinitionError):

        @cuaderna.step
        async def unannotated(number: int):  # pyright: ignore[reportUnusedFunction]  # noqa: ANN202
            return number


def test_a_step_with_a_type_that_is_not_json_is_rejected_when_decorated() -> None:
    with pytest.raises(InvalidDefinitionError):

        @cuaderna.step
        async def opaque(lock: threading.Lock) -> None:  # pyright: ignore[reportUnusedFunction]
            del lock
