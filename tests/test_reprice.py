from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

import cuaderna
from cuaderna import Engine, PriceTableError, RunNotFoundError, StepFailedError
from tests.test_cost import model_call

TABLE = cuaderna.PriceTable(
    {
        "format": "cuaderna.prices/1",
        "id": "2026-11-21",
        "currency": "USD",
        "models": [
            {"provider": "anthropic", "names": ["small"], "rates": {"input": "1", "output": "5"}},
            {
                "provider": "anthropic",
                "names": ["large"],
                "rates": {"input": "10", "output": "50", "cache_read": "1"},
                "variants": [
                    {
                        "when": {"service_tier": "batch"},
                        "rates": {"input": "5", "output": "25"},
                    }
                ],
            },
        ],
    }
)


@cuaderna.step
async def ask_model(model: str, label: str) -> str:
    cuaderna.annotate(model_call(model))
    return label


@cuaderna.step(max_attempts=1)
async def ask_and_fail(model: str) -> str:
    cuaderna.annotate(model_call(model))
    raise ValueError("not the JSON that was asked for")


@cuaderna.step
async def ask_in_batch(model: str) -> str:
    cuaderna.annotate({**model_call(model), "cuaderna.usage.conditions": {"service_tier": "batch"}})
    return "answer"


@cuaderna.step
async def ask_and_get_no_usage(model: str) -> str:
    cuaderna.annotate({"gen_ai.provider.name": "anthropic", "gen_ai.request.model": model})
    return "answer"


@cuaderna.step
async def ask_and_write_the_cache(model: str) -> str:
    cuaderna.annotate(
        {
            **model_call(model),
            "gen_ai.usage.cache_write.input_tokens": 100,
            "cuaderna.usage.cache_write_by_ttl": {"5m": 100},
        }
    )
    return "answer"


@cuaderna.step
async def pay_by_hand() -> str:
    cuaderna.annotate(cuaderna.usage.reported_cost("0.5", "paid by hand"))
    return "done"


@cuaderna.step
async def work_for_free() -> str:
    return "done"


@cuaderna.run
async def ask_twice(model: str) -> str:
    await ask_model(model, "first")
    return await ask_model(model, "second")


@cuaderna.run
async def ask_and_fail_to_parse(model: str) -> str:
    return await ask_and_fail(model)


@cuaderna.run
async def ask_in_a_batch(model: str) -> str:
    return await ask_in_batch(model)


@cuaderna.run
async def pay_for_a_machine() -> str:
    return await pay_by_hand()


@cuaderna.run
async def ask_without_usage(model: str) -> str:
    return await ask_and_get_no_usage(model)


@cuaderna.run
async def ask_with_a_cache_write(model: str) -> str:
    return await ask_and_write_the_cache(model)


@cuaderna.run
async def do_nothing_paid() -> str:
    return await work_for_free()


@pytest.fixture
async def priced_engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, prices=TABLE)
    yield engine
    await engine.close()


async def test_a_run_is_repriced_on_another_model_and_its_journal_is_untouched(
    priced_engine: Engine,
) -> None:
    await priced_engine.execute("run-1", ask_twice, "small")

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    recorded = await priced_engine.cost("run-1")
    assert repriced.total_usd == Decimal("0.04")
    assert {(cost.basis, cost.model) for cost in repriced.attempts} == {("repriced", "large")}
    assert repriced.attempts[0].parts == {
        "input": Decimal("0.01"),
        "cache_read": Decimal(0),
        "cache_write": Decimal(0),
        "output": Decimal("0.01"),
        "requests": Decimal(0),
    }
    assert recorded.total_usd == Decimal("0.004")
    assert {cost.basis for cost in recorded.attempts} == {"computed"}


async def test_a_failed_attempt_that_annotated_is_repriced(priced_engine: Engine) -> None:
    with pytest.raises(StepFailedError):
        await priced_engine.execute("run-1", ask_and_fail_to_parse, "small")

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    assert repriced.total_usd == Decimal("0.02")
    assert [cost.basis for cost in repriced.attempts] == ["repriced"]


async def test_only_the_base_rates_of_the_model_apply(priced_engine: Engine) -> None:
    await priced_engine.execute("run-1", ask_in_a_batch, "small")

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    assert repriced.total_usd == Decimal("0.02")


async def test_a_reported_cost_on_a_step_that_is_not_a_model_call_is_kept(
    priced_engine: Engine,
) -> None:
    await priced_engine.execute("run-1", pay_for_a_machine)

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    assert repriced.total_usd == Decimal("0.5")
    assert [(cost.usd, cost.basis) for cost in repriced.attempts] == [(Decimal("0.5"), "reported")]


async def test_a_model_call_without_usage_is_unknown_when_repriced(priced_engine: Engine) -> None:
    await priced_engine.execute("run-1", ask_without_usage, "small")

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    assert repriced.unknown_attempts == 1
    assert repriced.attempts[0].usd is None


async def test_a_bucket_the_target_model_has_no_rate_for_is_unknown(
    priced_engine: Engine,
) -> None:
    await priced_engine.execute("run-1", ask_with_a_cache_write, "small")

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    assert repriced.unknown_attempts == 1
    assert repriced.attempts[0].reason


async def test_a_model_the_table_does_not_have_is_refused_even_for_a_run_without_model_calls(
    priced_engine: Engine,
) -> None:
    await priced_engine.execute("run-1", do_nothing_paid)

    with pytest.raises(PriceTableError):
        await priced_engine.reprice("run-1", provider="anthropic", model="huge")


async def test_repricing_needs_an_engine_with_a_price_table(engine: Engine) -> None:
    await engine.execute("run-1", do_nothing_paid)

    with pytest.raises(PriceTableError):
        await engine.reprice("run-1", provider="anthropic", model="large")


async def test_repricing_a_run_that_does_not_exist_is_refused(priced_engine: Engine) -> None:
    with pytest.raises(RunNotFoundError):
        await priced_engine.reprice("missing", provider="anthropic", model="large")


async def test_a_run_recorded_without_a_price_table_can_be_repriced(
    engine: Engine, priced_engine: Engine
) -> None:
    await engine.execute("run-1", ask_twice, "small")

    repriced = await priced_engine.reprice("run-1", provider="anthropic", model="large")

    assert repriced.total_usd == Decimal("0.04")
