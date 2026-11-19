import pytest

import cuaderna
from cuaderna import Engine, RunMismatchError
from cuaderna._run import RunDefinition
from cuaderna.storage import Storage


class Crash(BaseException):
    pass


crash_in_the_step = False


@pytest.fixture(autouse=True)
def fresh_state() -> None:
    global crash_in_the_step
    crash_in_the_step = False


@cuaderna.step
async def checkpoint() -> None:
    if crash_in_the_step:
        raise Crash


def greet_at(code_version: str | None) -> RunDefinition[[str], str]:
    @cuaderna.run(code_version=code_version)
    async def greet(name: str) -> str:
        await checkpoint()
        return f"hello {name}"

    return greet


async def start_and_crash(engine: Engine, code_version: str | None) -> None:
    global crash_in_the_step
    crash_in_the_step = True
    with pytest.raises(Crash):
        await engine.execute("run-1", greet_at(code_version), "ana")
    crash_in_the_step = False


async def test_the_code_version_is_stored_with_the_run(engine: Engine, journal: Storage) -> None:
    await engine.execute("run-1", greet_at("2"), "ana")

    stored = await journal.create_run("run-1", "greet", "{}", None)
    assert stored.code_version == "2"


async def test_an_unfinished_run_is_not_resumed_by_another_code_version(engine: Engine) -> None:
    await start_and_crash(engine, "1")

    with pytest.raises(RunMismatchError):
        await engine.execute("run-1", greet_at("2"), "ana")
    with pytest.raises(RunMismatchError):
        await engine.execute("run-1", greet_at(None), "ana")
    assert await engine.execute("run-1", greet_at("1"), "ana") == "hello ana"


async def test_a_run_started_without_a_version_is_not_resumed_by_a_versioned_one(
    engine: Engine,
) -> None:
    await start_and_crash(engine, None)

    with pytest.raises(RunMismatchError):
        await engine.execute("run-1", greet_at("1"), "ana")


async def test_a_finished_run_returns_its_result_to_any_code_version(engine: Engine) -> None:
    await engine.execute("run-1", greet_at("1"), "ana")

    assert await engine.execute("run-1", greet_at("2"), "ana") == "hello ana"
