import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

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
async def pay_an_amount_nobody_can_read() -> str:
    cuaderna.annotate({"cuaderna.cost": {"usd": "thirty cents"}})
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


async def test_a_run_adds_up_its_model_calls_and_skips_the_other_steps(
    priced_engine: Engine,
) -> None:
    await priced_engine.execute("run-1", ask, "small")

    cost = await priced_engine.cost("run-1")

    assert (cost.total_usd, cost.unknown_attempts) == (Decimal("0.002"), 0)
    [attempt] = cost.attempts
    assert (attempt.step_key, attempt.attempt, attempt.usd) == ("ask_model#1", 1, Decimal("0.002"))
    assert (attempt.basis, attempt.model, attempt.reason) == ("computed", "small", None)
    assert attempt.parts["output"] == Decimal("0.001")


async def test_a_failed_attempt_and_a_reported_cost_are_part_of_the_total(
    priced_engine: Engine,
) -> None:
    with pytest.raises(StepFailedError):
        await priced_engine.execute("failed", ask_and_fail, "small")
    await priced_engine.execute("reported", ask_a_gateway, "large")

    assert (await priced_engine.cost("failed")).total_usd == Decimal("0.002")
    reported = await priced_engine.cost("reported")
    assert (reported.total_usd, reported.unknown_attempts) == (Decimal("0.0125"), 0)
    assert reported.attempts[0].basis == "reported"


async def test_what_cannot_be_priced_is_counted_as_unknown_and_not_as_free(
    priced_engine: Engine,
) -> None:
    @cuaderna.run
    async def flow() -> list[str]:
        return [
            await ask_model("small"),
            await ask_model("large"),
            await pay_an_amount_nobody_can_read(),
        ]

    await priced_engine.execute("run-1", flow)

    cost = await priced_engine.cost("run-1")
    assert (cost.total_usd, cost.unknown_attempts) == (Decimal("0.002"), 2)
    assert [(attempt.usd, attempt.basis, attempt.reason) for attempt in cost.attempts[1:]] == [
        (None, "unknown", "the price table has no anthropic model named 'large'"),
        (None, "unknown", "the recorded cost cannot be read"),
    ]


async def test_a_model_call_recorded_without_a_table_is_unknown(engine: Engine) -> None:
    await engine.execute("run-1", ask, "small")

    cost = await engine.cost("run-1")

    assert (cost.total_usd, cost.unknown_attempts) == (Decimal(0), 1)
    assert cost.attempts[0].reason == "the model call was recorded without a price table"


async def test_the_cost_of_an_imported_run_is_read_without_a_table(
    priced_engine: Engine, tmp_path: Path
) -> None:
    await priced_engine.execute("run-1", ask, "small")
    await priced_engine.export_run("run-1", tmp_path / "run.json")
    reader = Engine(tmp_path / "reader.db")

    run_id = await reader.import_run(tmp_path / "run.json")
    cost = await reader.cost(run_id)
    await reader.close()

    assert cost == await priced_engine.cost("run-1")


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


ODD_USAGE: dict[str, tuple[Any, str]] = {
    "no output count": ({"input_tokens": 10}, "gen_ai.usage.output_tokens"),
    "huge count": ({"input_tokens": 10**5000, "output_tokens": 5}, "gen_ai.usage.input_tokens"),
    "not an object": ("oops", "gen_ai.usage.input_tokens"),
}
paid_calls: list[str] = []


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def pay_and_annotate_odd_usage(case: str) -> str:
    paid_calls.append(case)
    response = {"model": "small", "usage": ODD_USAGE[case][0]}
    cuaderna.annotate(cuaderna.usage.from_anthropic(response, requested_model="small"))
    return "answer"


@cuaderna.run
async def ask_and_get_odd_usage(case: str) -> str:
    return await pay_and_annotate_odd_usage(case)


@pytest.mark.parametrize("case", ODD_USAGE)
async def test_a_paid_call_with_unreadable_usage_is_recorded_once_as_unknown(
    priced_engine: Engine, case: str
) -> None:
    paid_calls.clear()

    await priced_engine.execute("run-1", ask_and_get_odd_usage, case)

    [recorded] = await recorded_costs(priced_engine, "run-1")
    assert paid_calls == [case]
    assert isinstance(recorded, dict)
    assert recorded["basis"] == "unknown"
    assert ODD_USAGE[case][1] in recorded["reason"]
