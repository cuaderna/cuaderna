from collections.abc import Mapping
from decimal import Decimal

from pydantic import JsonValue

from ._prices import PriceTable, UnknownCost

_COST_KEY = "cuaderna.cost"
_MODEL_CALL_PREFIX = "gen_ai."


def with_cost(annotations: dict[str, JsonValue], prices: PriceTable | None) -> dict[str, JsonValue]:
    reported = annotations.get(_COST_KEY)
    if isinstance(reported, dict) and reported.get("usd") is not None:
        return {**annotations, _COST_KEY: {**reported, "basis": "reported"}}
    if prices is None or not _is_model_call(annotations):
        return annotations
    return {**annotations, _COST_KEY: _computed(prices, annotations)}


def _computed(prices: PriceTable, annotations: dict[str, JsonValue]) -> dict[str, JsonValue]:
    cost = prices.price(annotations)
    if isinstance(cost, UnknownCost):
        return {"usd": None, "basis": "unknown", "reason": cost.reason}
    return {
        "usd": _as_text(cost.usd),
        "basis": "computed",
        "price_table": prices.id,
        "price_table_hash": prices.content_hash,
        "model": cost.model,
        "variant": None if cost.variant is None else dict(cost.variant),
        "parts": {part: _as_text(amount) for part, amount in cost.parts.items()},
    }


def _is_model_call(annotations: Mapping[str, JsonValue]) -> bool:
    return any(key.startswith(_MODEL_CALL_PREFIX) for key in annotations)


def _as_text(amount: Decimal) -> str:
    return format(amount, "f")
