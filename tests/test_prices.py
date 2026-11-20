import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import cuaderna
from cuaderna._prices import CallCost, UnknownCost

BATCH_RATES = {"input": "0.5", "output": "2.5"}
LONG_RATES = {"input": "2", "output": "7.5"}


def table_document(**model: object) -> dict[str, Any]:
    haiku = {
        "provider": "anthropic",
        "names": ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
        "rates": {
            "input": "1",
            "output": "5",
            "cache_read": "0.10",
            "cache_write": {"5m": "1.25", "1h": "2"},
            "requests": {"web_search": "10"},
        },
        **model,
    }
    return {"format": "cuaderna.prices/1", "id": "2026-11-19", "currency": "USD", "models": [haiku]}


def model_call(**facts: object) -> dict[str, Any]:
    return {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-haiku-4-5",
        "gen_ai.response.model": "claude-haiku-4-5-20251001",
        "gen_ai.usage.input_tokens": 1_000,
        "gen_ai.usage.output_tokens": 200,
        **facts,
    }


def priced(document: dict[str, Any], call: dict[str, Any]) -> CallCost:
    cost = cuaderna.PriceTable(document).price(call)
    assert isinstance(cost, CallCost)
    return cost


def unknown(document: dict[str, Any], call: dict[str, Any]) -> str:
    cost = cuaderna.PriceTable(document).price(call)
    assert isinstance(cost, UnknownCost)
    return cost.reason


def test_every_kind_of_token_is_charged_at_its_own_rate() -> None:
    call = model_call(
        **{
            "gen_ai.usage.input_tokens": 12_550,
            "gen_ai.usage.output_tokens": 300,
            "gen_ai.usage.cache_read.input_tokens": 10_000,
            "gen_ai.usage.cache_write.input_tokens": 2_500,
            "cuaderna.usage.cache_write_by_ttl": {"5m": 500, "1h": 2_000},
            "cuaderna.usage.requests": {"web_search": 3},
        }
    )

    cost = priced(table_document(), call)

    assert cost.parts == {
        "input": Decimal("0.00005"),
        "cache_read": Decimal("0.001"),
        "cache_write": Decimal("0.004625"),
        "output": Decimal("0.0015"),
        "requests": Decimal("0.03"),
    }
    assert cost.usd == Decimal("0.037175")
    assert cost.model == "claude-haiku-4-5-20251001"
    assert cost.variant is None


def test_the_requested_model_is_used_when_the_response_model_has_no_prices() -> None:
    call = model_call(
        **{"gen_ai.response.model": "claude-haiku-4-5-latest", "gen_ai.provider.name": "Anthropic"}
    )

    cost = priced(table_document(), call)

    assert cost.model == "claude-haiku-4-5"
    assert cost.usd == Decimal("0.002")


def test_a_model_is_not_matched_by_the_start_of_its_name() -> None:
    call = model_call(
        **{"gen_ai.request.model": "claude-haiku", "gen_ai.response.model": "claude-haiku-4"}
    )

    assert "claude-haiku-4" in unknown(table_document(), call)


def test_a_variant_replaces_every_rate_when_its_condition_holds() -> None:
    document = table_document(variants=[{"when": {"service_tier": "batch"}, "rates": BATCH_RATES}])
    batch = model_call(**{"cuaderna.usage.conditions": {"service_tier": "batch"}})

    assert priced(document, batch).usd == Decimal("0.001")
    assert priced(document, batch).variant == {"service_tier": "batch"}
    assert priced(document, model_call()).usd == Decimal("0.002")


def test_long_context_rates_apply_to_the_whole_request() -> None:
    document = table_document(
        variants=[{"when": {"input_tokens_over": 1_000}, "rates": LONG_RATES}]
    )
    at_the_threshold = model_call()
    over_it = model_call(**{"gen_ai.usage.input_tokens": 1_001})

    assert priced(document, at_the_threshold).parts["input"] == Decimal("0.001")
    assert priced(document, over_it).parts["input"] == Decimal("0.002002")
    assert priced(document, over_it).parts["output"] == Decimal("0.0015")


def test_the_first_variant_that_matches_wins() -> None:
    both = {"when": {"service_tier": "batch", "input_tokens_over": 500}, "rates": {"input": "9"}}
    document = table_document(
        variants=[both, {"when": {"service_tier": "batch"}, "rates": BATCH_RATES}]
    )
    call = model_call(
        **{"gen_ai.usage.output_tokens": 0, "cuaderna.usage.conditions": {"service_tier": "batch"}}
    )

    assert priced(document, call).usd == Decimal("0.009")


def test_tokens_without_a_rate_have_no_known_cost() -> None:
    document = table_document(rates={"input": "1", "output": "5", "cache_write": {"5m": "1.25"}})
    written_for_an_hour = model_call(
        **{
            "gen_ai.usage.cache_write.input_tokens": 400,
            "cuaderna.usage.cache_write_by_ttl": {"1h": 400},
        }
    )
    read_from_cache = model_call(**{"gen_ai.usage.cache_read.input_tokens": 400})

    assert "cache_write" in unknown(document, written_for_an_hour)
    assert "cache_read" in unknown(document, read_from_cache)
    assert priced(document, model_call()).usd == Decimal("0.002")


def test_a_call_without_usage_has_no_known_cost() -> None:
    cut_short: dict[str, Any] = {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-haiku-4-5",
    }
    not_counts = model_call(**{"gen_ai.usage.input_tokens": "many"})
    more_cached_than_input = model_call(**{"gen_ai.usage.cache_read.input_tokens": 5_000})
    writes_without_lifetime = model_call(**{"gen_ai.usage.cache_write.input_tokens": 400})

    assert "no usage" in unknown(table_document(), cut_short)
    assert "cannot be read" in unknown(table_document(), not_counts)
    assert "more than the input" in unknown(table_document(), more_cached_than_input)
    assert "lifetime" in unknown(table_document(), writes_without_lifetime)


@pytest.mark.parametrize(
    "rates",
    [{"input": 1.5}, {"input": 1}, {"input": "-1"}, {"input": "NaN"}, {"input": "cheap"}],
)
def test_a_rate_that_is_not_a_decimal_string_is_rejected(rates: dict[str, Any]) -> None:
    with pytest.raises(cuaderna.PriceTableError):
        cuaderna.PriceTable(table_document(rates=rates))


def test_a_name_listed_twice_for_a_provider_is_rejected() -> None:
    document = table_document(names=["claude-haiku-4-5", "Claude-Haiku-4-5"])

    with pytest.raises(cuaderna.PriceTableError, match="more than once"):
        cuaderna.PriceTable(document)


@pytest.mark.parametrize(
    "change",
    [
        {"format": "cuaderna.prices/2"},
        {"currency": "EUR"},
        {"valid_from": "2026-11-19"},
        {"models": [{"provider": "anthropic", "rates": {"input": "1"}}]},
    ],
)
def test_a_document_in_another_shape_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(cuaderna.PriceTableError):
        cuaderna.PriceTable({**table_document(), **change})


def test_a_threshold_that_is_not_a_count_is_rejected() -> None:
    document = table_document(variants=[{"when": {"input_tokens_over": "200k"}, "rates": {}}])

    with pytest.raises(cuaderna.PriceTableError):
        cuaderna.PriceTable(document)


def test_the_hash_follows_the_content_and_not_the_id() -> None:
    original = cuaderna.PriceTable(table_document())
    same_content = cuaderna.PriceTable(json.loads(json.dumps(table_document())))
    edited = cuaderna.PriceTable(table_document(rates={"input": "1", "output": "6"}))

    assert original.id == edited.id == "2026-11-19"
    assert len(original.content_hash) == 12
    assert original.content_hash == same_content.content_hash
    assert original.content_hash != edited.content_hash


def test_a_table_is_loaded_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(table_document()), encoding="utf-8")

    table = cuaderna.PriceTable.load(path)

    assert table.content_hash == cuaderna.PriceTable(table_document()).content_hash


@pytest.mark.parametrize("text", ["{not json", "[]"])
def test_a_file_that_is_not_a_json_object_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "prices.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(cuaderna.PriceTableError):
        cuaderna.PriceTable.load(path)


DEEP = b'{"a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
HUGE_INT = b'{"a": ' + b"9" * 5_000 + b"}"


@pytest.mark.parametrize("content", [DEEP, HUGE_INT, b"\xff", b""])
def test_a_hostile_file_is_rejected_as_a_price_table_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "prices.json"
    path.write_bytes(content)

    with pytest.raises(cuaderna.PriceTableError):
        cuaderna.PriceTable.load(path)


def test_a_directory_is_rejected_as_a_price_table_error(tmp_path: Path) -> None:
    with pytest.raises(cuaderna.PriceTableError):
        cuaderna.PriceTable.load(tmp_path)


def test_a_file_of_exactly_max_bytes_is_read(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    content = json.dumps(table_document()).encode()
    path.write_bytes(content)

    cuaderna.PriceTable.load(path, max_bytes=len(content))

    with pytest.raises(cuaderna.PriceTableError, match="limit"):
        cuaderna.PriceTable.load(path, max_bytes=len(content) - 1)
