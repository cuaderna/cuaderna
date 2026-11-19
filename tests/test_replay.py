from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

import cuaderna
from cuaderna import (
    Engine,
    ReplayDivergedError,
    RunMismatchError,
    RunNotFoundError,
    RunStatus,
    StepFailedError,
)
from cuaderna._run import RunDefinition
from cuaderna.storage import Storage


class Kill(BaseException):
    pass


@dataclass(frozen=True)
class Ticket:
    holder: str
    valid_until: datetime


class World:
    def __init__(self) -> None:
        self.entered: list[str] = []
        self.route = ["a", "b"]
        self.stamp = "!"
        self.kill_after: int | None = None
        self.broken_bridges = 0
        self.closed = False
        self.received: list[tuple[Ticket, tuple[str, ...], float, dict[str, int]]] = []

    def enter(self, place: str) -> None:
        if len(self.entered) == self.kill_after:
            raise Kill(place)
        self.entered.append(place)


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@cuaderna.step
async def visit(place: str) -> str:
    world.enter(place)
    return place.upper()


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def cross(place: str) -> str:
    world.enter(place)
    if world.broken_bridges > 0:
        world.broken_bridges -= 1
        raise ConnectionError("washed out")
    return place.upper()


@cuaderna.run
async def trip() -> list[str]:
    return [await visit(place) for place in world.route]


@cuaderna.run
async def other_trip() -> list[str]:
    return [await visit(place) for place in world.route]


def _trip_redefined_with_a_parameter() -> RunDefinition[[float], list[str]]:
    @cuaderna.run
    async def trip(pace: float) -> list[str]:
        return [await visit(place) for place in world.route] * int(pace)

    return trip


trip_with_a_pace = _trip_redefined_with_a_parameter()


@cuaderna.run
async def stamped_trip() -> str:
    return await visit("a") + world.stamp


@cuaderna.run
async def crossing() -> str:
    return await cross("bridge")


@cuaderna.run
async def guarded_trip() -> str:
    place = await visit("a")
    if world.closed:
        raise RuntimeError("closed")
    return place


@cuaderna.run
async def careless_trip() -> str:
    try:
        return await visit(world.route[0])
    except Exception:
        return "lost"


@cuaderna.run
async def ticketed_trip(ticket: Ticket, *stops: str, pace: float = 1.0, **notes: int) -> str:
    world.received.append((ticket, stops, pace, notes))
    return await visit(ticket.holder)


async def test_a_finished_run_replays_without_executing_or_writing(engine: Engine) -> None:
    assert await engine.execute("run-1", trip) == ["A", "B"]
    record = await engine.run_record("run-1")
    steps = await engine.steps("run-1")
    world.entered.clear()

    assert await engine.replay("run-1", trip) == ["A", "B"]

    assert world.entered == []
    assert await engine.run_record("run-1") == record
    assert await engine.steps("run-1") == steps


async def test_a_replay_gets_the_recorded_input_in_its_own_types(engine: Engine) -> None:
    ticket = Ticket("ana", datetime(2026, 11, 19, 12, 30, tzinfo=UTC))
    await engine.execute("run-1", ticketed_trip, ticket, "x", "y", pace=2.5, extra=3)

    assert await engine.replay("run-1", ticketed_trip) == "ANA"

    assert world.received == [(ticket, ("x", "y"), 2.5, {"extra": 3})] * 2


async def test_a_step_that_failed_for_good_fails_the_same_way_in_a_replay(engine: Engine) -> None:
    world.broken_bridges = 2
    with pytest.raises(StepFailedError):
        await engine.execute("run-1", crossing)
    world.entered.clear()

    with pytest.raises(StepFailedError, match="washed out") as failure:
        await engine.replay("run-1", crossing)

    assert failure.value.step_key == "cross#1"
    assert world.entered == []
    assert (await engine.run_record("run-1")).status is RunStatus.FAILED


async def test_a_replay_that_returns_where_the_run_failed_has_diverged(engine: Engine) -> None:
    world.closed = True
    with pytest.raises(RuntimeError):
        await engine.execute("run-1", guarded_trip)
    world.closed = False

    with pytest.raises(ReplayDivergedError, match="recorded as failed"):
        await engine.replay("run-1", guarded_trip)


async def test_an_unfinished_run_diverges_at_the_first_step_it_never_recorded(
    engine: Engine, journal: Storage
) -> None:
    world.kill_after = 1
    with pytest.raises(Kill):
        await engine.execute("run-1", trip)
    world.kill_after = None
    world.entered.clear()
    lease = await journal.claim_run("run-1", "another worker", 60)
    assert lease is not None

    with pytest.raises(ReplayDivergedError, match=r"visit#2.*never recorded"):
        await engine.replay("run-1", trip)

    assert world.entered == []
    await journal.renew_lease(lease, 60)
    await journal.release_run(lease)
    assert await engine.execute("run-1", trip) == ["A", "B"]
    assert world.entered == ["b"]


async def test_a_step_killed_between_retries_has_no_outcome_to_replay(engine: Engine) -> None:
    world.broken_bridges = 1
    world.kill_after = 1
    with pytest.raises(Kill):
        await engine.execute("run-1", crossing)

    with pytest.raises(ReplayDivergedError, match=r"cross#1.*attempt 1"):
        await engine.replay("run-1", crossing)


async def test_a_step_asked_with_other_arguments_diverges(engine: Engine) -> None:
    await engine.execute("run-1", trip)
    world.route = ["a", "c"]
    world.entered.clear()

    with pytest.raises(ReplayDivergedError, match=r'visit#2.*"b".*"c"'):
        await engine.replay("run-1", trip)

    assert world.entered == []


async def test_recorded_steps_the_code_no_longer_asks_for_diverge(engine: Engine) -> None:
    await engine.execute("run-1", trip)
    world.route = ["a"]

    with pytest.raises(ReplayDivergedError, match=r"never asked for.*visit#2"):
        await engine.replay("run-1", trip)


async def test_a_different_output_diverges(engine: Engine) -> None:
    await engine.execute("run-1", stamped_trip)
    world.stamp = "?"

    with pytest.raises(ReplayDivergedError, match=r'"A\?".*"A!"'):
        await engine.replay("run-1", stamped_trip)


async def test_a_divergence_the_run_swallows_is_still_raised(engine: Engine) -> None:
    await engine.execute("run-1", careless_trip)
    world.route = ["z"]

    with pytest.raises(ReplayDivergedError, match="visit#1"):
        await engine.replay("run-1", careless_trip)


async def test_a_run_is_only_replayed_by_the_function_that_recorded_it(engine: Engine) -> None:
    await engine.execute("run-1", trip)

    with pytest.raises(RunMismatchError):
        await engine.replay("run-1", other_trip)
    with pytest.raises(RunMismatchError, match="does not fit"):
        await engine.replay("run-1", trip_with_a_pace)
    with pytest.raises(RunNotFoundError):
        await engine.replay("missing", trip)
