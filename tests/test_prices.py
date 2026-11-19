import json
from pathlib import Path
from typing import Any

import pytest

import cuaderna


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
