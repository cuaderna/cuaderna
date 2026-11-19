import asyncio
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    Engine,
    RunMismatchError,
    StepFailedError,
    StepMismatchError,
    TerminalError,
)
from cuaderna._records import AttemptStatus
from cuaderna.storage import Storage


class Crash(BaseException):
    pass


class World:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.start = "a"
        self.crash_at: str | None = None
        self.failures_left = 0

    def reach(self, place: str) -> None:
        if self.crash_at == place:
            raise Crash(place)
        self.calls.append(place)


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@cuaderna.step
async def visit(place: str) -> str:
    world.reach(place)
    return place.upper()


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def flaky(place: str) -> str:
    world.reach(place)
    if world.failures_left > 0:
        world.failures_left -= 1
        raise ConnectionError("try again")
    return place.upper()


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def rejected(place: str) -> str:
    world.reach(place)
    raise TerminalError("bad request")


@cuaderna.run
async def trip(places: list[str]) -> list[str]:
    return [await visit(place) for place in places]


@cuaderna.run
async def trip_from_memory() -> list[str]:
    return [await visit(world.start), await visit("end")]


@cuaderna.run
async def flaky_trip() -> str:
    return await flaky("bridge")


@cuaderna.run
async def trip_that_survives_a_failure() -> str:
    try:
        await flaky("bridge")
    except StepFailedError:
        return await visit("ferry")
    return "bridge"


@cuaderna.run
async def rejected_trip() -> str:
    return await rejected("border")


async def test_a_resumed_run_does_not_execute_recorded_steps_again(engine: Engine) -> None:
    world.crash_at = "c"
    with pytest.raises(Crash):
        await engine.execute("run", trip, ["a", "b", "c"])
    world.crash_at = None

    assert await engine.execute("run", trip, ["a", "b", "c"]) == ["A", "B", "C"]
    assert world.calls == ["a", "b", "c"]


async def test_a_step_that_arrives_with_other_arguments_stops_the_resume(engine: Engine) -> None:
    world.crash_at = "end"
    with pytest.raises(Crash):
        await engine.execute("run", trip_from_memory)
    world.crash_at = None
    world.start = "b"

    with pytest.raises(StepMismatchError):
        await engine.execute("run", trip_from_memory)

    world.start = "a"
    assert await engine.execute("run", trip_from_memory) == ["A", "END"]


async def test_a_run_id_cannot_be_reused_with_another_input(engine: Engine) -> None:
    world.crash_at = "a"
    with pytest.raises(Crash):
        await engine.execute("run", trip, ["a"])

    with pytest.raises(RunMismatchError):
        await engine.execute("run", trip, ["b"])


async def test_a_step_is_retried_until_it_succeeds(engine: Engine, journal: Storage) -> None:
    world.failures_left = 2

    assert await engine.execute("run", flaky_trip) == "BRIDGE"

    attempts = await journal.load_attempts("run")
    assert [(attempt.attempt, attempt.status) for attempt in attempts] == [
        (1, AttemptStatus.ERROR),
        (2, AttemptStatus.ERROR),
        (3, AttemptStatus.OK),
    ]


async def test_a_resume_continues_the_retries_where_they_stopped(
    engine: Engine, journal: Storage
) -> None:
    world.failures_left = 1
    with pytest.raises(Crash):
        task = asyncio.ensure_future(engine.execute("run", flaky_trip))
        while world.calls != ["bridge"]:
            await asyncio.sleep(0)
        world.crash_at = "bridge"
        await task
    world.crash_at = None

    assert await engine.execute("run", flaky_trip) == "BRIDGE"

    attempts = await journal.load_attempts("run")
    assert [attempt.attempt for attempt in attempts] == [1, 2]


async def test_a_step_out_of_attempts_fails_again_on_resume_without_executing(
    engine: Engine,
) -> None:
    world.failures_left = 3
    world.crash_at = "ferry"
    with pytest.raises(Crash):
        await engine.execute("run", trip_that_survives_a_failure)
    world.crash_at = None

    assert await engine.execute("run", trip_that_survives_a_failure) == "FERRY"
    assert world.calls == ["bridge", "bridge", "bridge", "ferry"]


async def test_a_terminal_error_is_not_retried(engine: Engine, journal: Storage) -> None:
    with pytest.raises(StepFailedError) as raised:
        await engine.execute("run", rejected_trip)

    assert raised.value.error_type == "TerminalError"
    assert len(await journal.load_attempts("run")) == 1


async def test_a_long_run_keeps_its_lease(journal_path: Path, journal: Storage) -> None:
    engine = Engine(journal_path, lease_seconds=0.15)

    @cuaderna.run
    async def slow() -> None:
        await asyncio.sleep(0.5)
        assert await journal.claim_run("run", "another worker", 60) is None

    await engine.execute("run", slow)
    await engine.close()
