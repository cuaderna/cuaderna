import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

import cuaderna
from cuaderna import Engine, StepFailedError

TABLE = cuaderna.PriceTable(
    {
        "format": "cuaderna.prices/1",
        "id": "2026-11-19",
        "currency": "USD",
        "models": [
            {"provider": "anthropic", "names": ["small"], "rates": {"input": "1", "output": "5"}}
        ],
    }
)
SMALL_CALL_COST = {
    "usd": "0.002",
    "basis": "computed",
    "price_table": "2026-11-19",
    "price_table_hash": TABLE.content_hash,
    "model": "small",
    "variant": None,
    "parts": {
        "input": "0.001",
        "cache_read": "0",
        "cache_write": "0",
        "output": "0.001",
        "requests": "0",
    },
}


def model_call(model: str) -> dict[str, JsonValue]:
    return {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": 1_000,
        "gen_ai.usage.output_tokens": 200,
    }


@cuaderna.step
async def ask_model(model: str) -> str:
    cuaderna.annotate(model_call(model))
    return "answer"


@cuaderna.step(max_attempts=1)
async def ask_and_fail_to_parse(model: str) -> str:
    cuaderna.annotate(model_call(model))
    raise ValueError("not the JSON that was asked for")


@cuaderna.step
async def ask_through_a_gateway(model: str) -> str:
    cuaderna.annotate(model_call(model))
    cuaderna.annotate(cuaderna.usage.reported_cost("0.0125", "the gateway returned it"))
    return "answer"


@cuaderna.step
async def rent_a_machine() -> str:
    cuaderna.annotate({"cuaderna.cost": {"usd": "0.30"}})
    return "done"


@cuaderna.step
def count_words(text: str) -> int:
    cuaderna.annotate({"thread": True})
    return len(text.split())


@cuaderna.run
async def ask(model: str) -> int:
    return await count_words(await ask_model(model))


@cuaderna.run
async def ask_and_fail(model: str) -> str:
    return await ask_and_fail_to_parse(model)


@cuaderna.run
async def ask_a_gateway(model: str) -> str:
    return await ask_through_a_gateway(model)


@cuaderna.run
async def rent() -> str:
    return await rent_a_machine()


@pytest.fixture
async def priced_engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, prices=TABLE)
    yield engine
    await engine.close()


async def recorded_costs(engine: Engine, run_id: str) -> list[object]:
    return [
        attempt.annotations_json and json.loads(attempt.annotations_json).get("cuaderna.cost")
        for attempt in await engine.steps(run_id)
    ]


async def test_a_model_call_is_priced_when_its_attempt_is_recorded(priced_engine: Engine) -> None:
    await priced_engine.execute("run-1", ask, "small")

    assert await recorded_costs(priced_engine, "run-1") == [SMALL_CALL_COST, None]


async def test_a_call_that_was_paid_for_and_then_failed_is_priced_too(
    priced_engine: Engine,
) -> None:
    with pytest.raises(StepFailedError):
        await priced_engine.execute("run-1", ask_and_fail, "small")

    assert await recorded_costs(priced_engine, "run-1") == [SMALL_CALL_COST]


async def test_a_model_the_table_does_not_have_is_recorded_as_unknown(
    priced_engine: Engine,
) -> None:
    await priced_engine.execute("run-1", ask, "large")

    assert await recorded_costs(priced_engine, "run-1") == [
        {
            "usd": None,
            "basis": "unknown",
            "reason": "the price table has no anthropic model named 'large'",
        },
        None,
    ]


async def test_a_reported_cost_is_kept_and_the_table_is_not_asked(priced_engine: Engine) -> None:
    await priced_engine.execute("run-1", ask_a_gateway, "large")

    assert await recorded_costs(priced_engine, "run-1") == [
        {"usd": "0.0125", "basis": "reported", "reason": "the gateway returned it"}
    ]


async def test_a_cost_written_by_hand_is_marked_as_reported(engine: Engine) -> None:
    await engine.execute("run-1", rent)

    assert await recorded_costs(engine, "run-1") == [{"usd": "0.30", "basis": "reported"}]


async def test_an_engine_with_no_table_records_no_cost(engine: Engine) -> None:
    await engine.execute("run-1", ask, "small")

    assert await recorded_costs(engine, "run-1") == [None, None]


def test_a_reported_cost_has_to_be_an_amount() -> None:
    assert cuaderna.usage.reported_cost(Decimal("1E-7"), "tiny")["cuaderna.cost"] == {
        "usd": "0.0000001",
        "basis": "reported",
        "reason": "tiny",
    }
    with pytest.raises(ValidationError):
        cuaderna.usage.reported_cost("-1", "a refund")
    with pytest.raises(ValidationError):
        cuaderna.usage.reported_cost("a lot", "no number")
