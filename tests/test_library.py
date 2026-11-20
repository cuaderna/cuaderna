from datetime import UTC, datetime
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, InvalidDefinitionError, StepCallError


class Crash(BaseException):
    pass


seen: list[tuple[datetime, float, str]] = []
crash_after_reading = False


@pytest.fixture(autouse=True)
def fresh_state() -> None:
    global crash_after_reading
    seen.clear()
    crash_after_reading = False


@cuaderna.step
async def checkpoint() -> None:
    if crash_after_reading:
        raise Crash


@cuaderna.run
async def read_the_world() -> tuple[datetime, float, str]:
    values = (await cuaderna.now(), await cuaderna.random(), await cuaderna.new_id())
    seen.append(values)
    await checkpoint()
    return values


@cuaderna.run
async def two_ids() -> list[str]:
    return [await cuaderna.new_id(), await cuaderna.new_id()]


async def test_library_steps_return_time_randomness_and_ids(engine: Engine) -> None:
    before = datetime.now(UTC)

    moment, number, identifier = await engine.execute("run-1", read_the_world)

    assert before <= moment <= datetime.now(UTC)
    assert 0 <= number < 1
    assert len(identifier) == 32
    assert int(identifier, 16) >= 0


async def test_each_call_is_its_own_recorded_value(engine: Engine) -> None:
    first, second = await engine.execute("run-1", two_ids)

    assert first != second
    assert await engine.execute("run-1", two_ids) == [first, second]


async def test_a_resumed_run_sees_the_same_values(journal_path: Path) -> None:
    global crash_after_reading
    crash_after_reading = True
    first = Engine(journal_path)
    with pytest.raises(Crash):
        await first.execute("run-1", read_the_world)
    await first.close()

    crash_after_reading = False
    second = Engine(journal_path)
    resumed = await second.execute("run-1", read_the_world)
    await second.close()

    assert seen == [resumed, resumed]


async def test_a_user_step_cannot_take_a_library_name() -> None:
    async def impostor() -> str:
        return "written"

    with pytest.raises(InvalidDefinitionError):
        cuaderna.step(name="cuaderna.now")(impostor)
    with pytest.raises(InvalidDefinitionError):
        cuaderna.step(name="cuaderna.anything")(impostor)


async def test_library_steps_need_a_run() -> None:
    with pytest.raises(StepCallError):
        await cuaderna.now()
