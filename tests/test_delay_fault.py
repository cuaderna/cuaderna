import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, Fault, FaultPlan, StepAttempt, StepFailedError
from cuaderna._run import RunDefinition

DELAY_SECONDS = 0.2
TIMEOUT_SECONDS = 0.05


@cuaderna.step(max_attempts=1)
async def unbounded() -> str:
    return "done"


@cuaderna.step(max_attempts=1, timeout_seconds=TIMEOUT_SECONDS)
async def bounded_once() -> str:
    return "done"


@cuaderna.step(max_attempts=2, backoff_seconds=0, timeout_seconds=TIMEOUT_SECONDS)
async def bounded_twice() -> str:
    return "done"


@cuaderna.run
async def run_unbounded() -> str:
    return await unbounded()


@cuaderna.run
async def run_bounded_once() -> str:
    return await bounded_once()


@cuaderna.run
async def run_bounded_twice() -> str:
    return await bounded_twice()


@pytest.fixture
async def engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path)
    yield engine
    await engine.close()


def delay_at(step_key: str, **options: object) -> Fault:
    document = {
        "format": "cuaderna.faults/1",
        "id": "plan",
        "faults": [{"id": "f1", "at": step_key, "delay": {"seconds": DELAY_SECONDS, **options}}],
    }
    return FaultPlan(document).faults[0]


async def attempts_of(engine: Engine, step_key: str) -> list[StepAttempt]:
    return [a for a in await engine.steps("experiment") if a.step_key == step_key]


async def rerun(engine: Engine, run_function: RunDefinition[[], str], fault: Fault) -> str:
    await engine.execute("source", run_function)
    return await engine.rerun("source", run_function, as_run_id="experiment", reuse=[], fault=fault)


def has_fault(attempt: StepAttempt) -> bool:
    return "cuaderna.fault" in json.loads(attempt.annotations_json or "{}")


async def test_a_delay_without_a_timeout_only_slows_the_step(engine: Engine) -> None:
    result = await rerun(engine, run_unbounded, delay_at("unbounded#1"))

    [attempt] = await attempts_of(engine, "unbounded#1")
    assert (result, attempt.result_json) == ("done", '"done"')
    assert json.loads(attempt.annotations_json or "{}")["cuaderna.fault"]["kind"] == "delay"


async def test_a_delay_longer_than_the_timeout_fails_the_attempt(engine: Engine) -> None:
    with pytest.raises(StepFailedError):
        await rerun(engine, run_bounded_once, delay_at("bounded_once#1"))

    [attempt] = await attempts_of(engine, "bounded_once#1")
    assert json.loads(attempt.error_json or "{}")["type"] == "StepTimeoutError"
    assert has_fault(attempt)


async def test_a_delay_on_the_first_attempt_only_lets_the_retry_succeed(engine: Engine) -> None:
    result = await rerun(engine, run_bounded_twice, delay_at("bounded_twice#1", attempts=1))

    delayed, retried = await attempts_of(engine, "bounded_twice#1")
    assert result == "done"
    assert json.loads(delayed.error_json or "{}")["type"] == "StepTimeoutError"
    assert has_fault(delayed)
    assert (retried.result_json, has_fault(retried)) == ('"done"', False)
