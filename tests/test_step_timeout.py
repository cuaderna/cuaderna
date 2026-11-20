import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import cuaderna
from cuaderna import CostUnknownError, Engine, InvalidDefinitionError, StepFailedError
from tests.test_cost import TABLE

_LIMIT_SECONDS = 0.05

calls: list[int] = []


@pytest.fixture(autouse=True)
def forget_calls() -> None:
    calls.clear()


@pytest.fixture
async def priced_engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, prices=TABLE)
    yield engine
    await engine.close()


@cuaderna.step(max_attempts=2, backoff_seconds=0, timeout_seconds=_LIMIT_SECONDS)
async def hangs_once() -> str:
    calls.append(len(calls) + 1)
    if len(calls) == 1:
        await asyncio.sleep(60)
    return "done"


@cuaderna.step(max_attempts=1, timeout_seconds=_LIMIT_SECONDS)
async def hangs_always() -> str:
    await asyncio.sleep(60)
    return "never"


@cuaderna.step(max_attempts=1, timeout_seconds=_LIMIT_SECONDS)
async def raises_its_own_timeout() -> str:
    raise TimeoutError("the client gave up")


@cuaderna.step(max_attempts=1, timeout_seconds=_LIMIT_SECONDS)
async def annotates_then_hangs() -> str:
    cuaderna.annotate({"colour": "blue"})
    await asyncio.sleep(60)
    return "never"


@cuaderna.step(timeout_seconds=1)
async def finishes_in_time() -> str:
    return "ok"


@cuaderna.step(max_attempts=1, timeout_seconds=_LIMIT_SECONDS)
async def asks_model_then_hangs() -> str:
    cuaderna.annotate({"gen_ai.provider.name": "anthropic", "gen_ai.request.model": "small"})
    await asyncio.sleep(60)
    return "never"


@cuaderna.step
async def follow_up() -> str:
    return "after"


@cuaderna.run
async def retried() -> str:
    return await hangs_once()


@cuaderna.run
async def gives_up() -> str:
    return await hangs_always()


@cuaderna.run
async def own_timeout() -> str:
    return await raises_its_own_timeout()


@cuaderna.run
async def annotated() -> str:
    return await annotates_then_hangs()


@cuaderna.run
async def in_time() -> str:
    return await finishes_in_time()


@cuaderna.run
async def model_then_more() -> str:
    with contextlib.suppress(StepFailedError):
        await asks_model_then_hangs()
    return await follow_up()


async def test_a_timed_out_attempt_is_recorded_and_retried(engine: Engine) -> None:
    assert await engine.execute("run-1", retried) == "done"

    attempts = await engine.steps("run-1")
    assert calls == [1, 2]
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert attempts[0].error_json is not None
    assert json.loads(attempts[0].error_json)["type"] == "StepTimeoutError"
    assert json.loads(attempts[0].error_json)["terminal"] is False
    assert attempts[1].result_json is not None


async def test_the_last_timed_out_attempt_fails_the_run_with_the_library_error(
    engine: Engine,
) -> None:
    with pytest.raises(StepFailedError) as raised:
        await engine.execute("run-1", gives_up)

    assert raised.value.error_type == "StepTimeoutError"
    assert "hangs_always#1" in raised.value.message
    assert str(_LIMIT_SECONDS) in raised.value.message


async def test_a_timeout_the_step_raises_itself_keeps_its_own_type(engine: Engine) -> None:
    with pytest.raises(StepFailedError) as raised:
        await engine.execute("run-1", own_timeout)

    assert raised.value.error_type == "TimeoutError"


async def test_a_step_that_finishes_in_time_is_not_affected(engine: Engine) -> None:
    assert await engine.execute("run-1", in_time) == "ok"


async def test_annotations_set_before_the_timeout_stay_on_the_attempt(engine: Engine) -> None:
    with pytest.raises(StepFailedError):
        await engine.execute("run-1", annotated)

    attempts = await engine.steps("run-1")
    assert json.loads(attempts[0].annotations_json or "{}") == {"colour": "blue"}


async def test_a_timed_out_model_call_has_unknown_cost_and_stops_a_budgeted_run(
    priced_engine: Engine,
) -> None:
    with pytest.raises(CostUnknownError) as raised:
        await priced_engine.execute("run-1", model_then_more.with_budget("1"))

    assert raised.value.step_key == "asks_model_then_hangs#1"


def test_a_sync_step_cannot_have_a_timeout() -> None:
    with pytest.raises(InvalidDefinitionError, match="sync step"):

        @cuaderna.step(timeout_seconds=1)
        def blocking() -> str:
            return "never"


@pytest.mark.parametrize("seconds", [0, -1.5])
def test_a_timeout_has_to_be_greater_than_zero(seconds: float) -> None:
    with pytest.raises(InvalidDefinitionError, match="greater than 0"):

        @cuaderna.step(timeout_seconds=seconds)
        async def never_runs() -> str:
            return "never"
