import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

import cuaderna
from cuaderna import BudgetExceededError, Engine, InvalidDefinitionError, RunStatus
from cuaderna.storage import Storage
from tests.test_cost import TABLE, model_call

_WORKER_LEASE_SECONDS = 0.5

_WORKER = """
import asyncio
import sys

from tests.test_budget import pay_then_hang
from tests.test_cost import TABLE

from cuaderna import Engine


async def main(journal: str) -> None:
    engine = Engine(journal, lease_seconds=float(sys.argv[2]), prices=TABLE)
    await engine.execute("run", pay_then_hang.with_budget("1"))


asyncio.run(main(sys.argv[1]))
"""

bodies_that_ran: list[str] = []
refusals: list[Exception] = []


@pytest.fixture(autouse=True)
def forget_bodies() -> Iterator[None]:
    bodies_that_ran.clear()
    refusals.clear()
    yield
    bodies_that_ran.clear()
    refusals.clear()


@pytest.fixture
async def priced_engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, prices=TABLE)
    yield engine
    await engine.close()


@cuaderna.step
async def pay(label: str) -> str:
    bodies_that_ran.append(label)
    cuaderna.annotate(model_call("small"))
    return label


@cuaderna.step
async def work_for_free(label: str) -> str:
    bodies_that_ran.append(label)
    return label


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def pay_and_fail() -> str:
    bodies_that_ran.append("pay_and_fail")
    cuaderna.annotate(model_call("small"))
    raise ValueError("not the JSON that was asked for")


@cuaderna.step
async def hang() -> str:
    await asyncio.sleep(60)
    return "never"


@cuaderna.run
async def pay_then_work() -> str:
    await pay("paid")
    return await work_for_free("free")


@cuaderna.run
async def pay_then_fail_and_retry() -> str:
    return await pay_and_fail()


@cuaderna.run
async def pay_then_swallow() -> str:
    await pay("paid")
    for label in ("first free", "second free"):
        try:
            await work_for_free(label)
        except Exception as error:
            refusals.append(error)
    return "gave up quietly"


@cuaderna.run
async def pay_then_swallow_and_raise() -> str:
    await pay("paid")
    try:
        await work_for_free("free")
    except Exception as error:
        raise ValueError("could not go on") from error
    return "kept going"


@cuaderna.run
async def pay_then_hang() -> str:
    await pay("paid")
    print("paid", flush=True)
    return await hang()


@cuaderna.run
async def work_only() -> str:
    return await work_for_free("free")


async def test_a_run_that_reaches_its_budget_stops_before_the_next_step(
    priced_engine: Engine, journal: Storage
) -> None:
    with pytest.raises(BudgetExceededError) as raised:
        await priced_engine.execute("run-1", pay_then_work.with_budget("0.002"))

    attempts = await priced_engine.steps("run-1")
    record = await priced_engine.run_record("run-1")
    assert raised.value.spent_usd == Decimal("0.002")
    assert raised.value.budget_usd == Decimal("0.002")
    assert bodies_that_ran == ["paid"]
    assert [attempt.step_key for attempt in attempts] == ["pay#1"]
    assert record.status is RunStatus.RUNNING
    assert record.output_json is None
    assert await journal.claim_run("run-1", "another worker", 60) is not None


async def test_executing_again_with_the_same_budget_raises_again(priced_engine: Engine) -> None:
    budgeted = pay_then_work.with_budget("0.002")
    with pytest.raises(BudgetExceededError):
        await priced_engine.execute("run-1", budgeted)
    bodies_that_ran.clear()

    with pytest.raises(BudgetExceededError):
        await priced_engine.execute("run-1", budgeted)

    assert bodies_that_ran == []


async def test_executing_with_a_higher_budget_finishes_the_run(priced_engine: Engine) -> None:
    with pytest.raises(BudgetExceededError):
        await priced_engine.execute("run-1", pay_then_work.with_budget("0.002"))

    output = await priced_engine.execute("run-1", pay_then_work.with_budget("1"))

    assert output == "free"
    assert bodies_that_ran == ["paid", "free"]


async def test_failed_attempts_count_towards_the_budget(priced_engine: Engine) -> None:
    with pytest.raises(BudgetExceededError):
        await priced_engine.execute("run-1", pay_then_fail_and_retry.with_budget("0.002"))

    attempts = await priced_engine.steps("run-1")
    assert bodies_that_ran == ["pay_and_fail"]
    assert [attempt.attempt for attempt in attempts] == [1]


async def test_a_run_that_swallows_the_budget_error_still_raises_it(
    priced_engine: Engine,
) -> None:
    with pytest.raises(BudgetExceededError):
        await priced_engine.execute("run-1", pay_then_swallow.with_budget("0.002"))

    record = await priced_engine.run_record("run-1")
    assert bodies_that_ran == ["paid"]
    assert len(refusals) == 2
    assert refusals[0] is refusals[1]
    assert record.status is RunStatus.RUNNING
    assert record.output_json is None


async def test_a_run_that_swallows_the_budget_error_and_fails_is_not_failed(
    priced_engine: Engine,
) -> None:
    with pytest.raises(BudgetExceededError) as raised:
        await priced_engine.execute("run-1", pay_then_swallow_and_raise.with_budget("0.002"))

    record = await priced_engine.run_record("run-1")
    assert isinstance(raised.value.__cause__, ValueError)
    assert record.status is RunStatus.RUNNING


async def test_a_budget_needs_an_engine_with_a_price_table(engine: Engine) -> None:
    with pytest.raises(InvalidDefinitionError):
        await engine.execute("run-1", work_only.with_budget("1"))

    assert await engine.runs() == []


@pytest.mark.parametrize("budget", ["abc", "0", "-1", "NaN", "Infinity", 1.5, 2])
def test_a_budget_has_to_be_a_positive_amount_written_as_a_string_or_a_decimal(
    budget: object,
) -> None:
    async def unlimited() -> None:
        return None

    with pytest.raises(InvalidDefinitionError):
        cuaderna.run(budget_usd=cast("Decimal | str", budget))(unlimited)
    with pytest.raises(InvalidDefinitionError):
        work_only.with_budget(cast("Decimal | str", budget))


async def test_the_spent_sum_survives_a_killed_process(
    journal_path: Path, priced_engine: Engine
) -> None:
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(journal_path), str(_WORKER_LEASE_SECONDS)],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert worker.stdout is not None
    worker.stdout.readline()
    worker.kill()
    worker.wait()
    worker.stdout.close()

    with pytest.raises(BudgetExceededError):
        await priced_engine.execute("run", pay_then_hang.with_budget("0.002"))

    assert bodies_that_ran == []
