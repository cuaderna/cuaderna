import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field, JsonValue, ValidationError

from ._prices import PriceTable, UnknownCost
from ._records import StepAttempt

_COST_KEY = "cuaderna.cost"
_MODEL_CALL_PREFIX = "gen_ai."


@dataclass(frozen=True, slots=True)
class AttemptCost:
    step_key: str
    attempt: int
    usd: Decimal | None
    basis: str
    model: str | None
    parts: Mapping[str, Decimal]
    reason: str | None


@dataclass(frozen=True, slots=True)
class RunCost:
    total_usd: Decimal
    unknown_attempts: int
    attempts: list[AttemptCost]


class _RecordedCost(BaseModel):
    usd: Decimal | None = Field(ge=0)
    basis: str
    model: str | None = None
    parts: dict[str, Decimal] = {}
    reason: str | None = None


def with_cost(annotations: dict[str, JsonValue], prices: PriceTable | None) -> dict[str, JsonValue]:
    reported = annotations.get(_COST_KEY)
    if isinstance(reported, dict) and reported.get("usd") is not None:
        return {**annotations, _COST_KEY: {**reported, "basis": "reported"}}
    if prices is None or not _is_model_call(annotations):
        return annotations
    return {**annotations, _COST_KEY: _computed(prices, annotations)}


def run_cost(attempts: list[StepAttempt]) -> RunCost:
    costs: list[AttemptCost] = []
    total = Decimal(0)
    unknown = 0
    for attempt in attempts:
        cost = _attempt_cost(attempt)
        if cost is None:
            continue
        costs.append(cost)
        if cost.usd is None:
            unknown += 1
        else:
            total += cost.usd
    return RunCost(total_usd=total, unknown_attempts=unknown, attempts=costs)


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


def _attempt_cost(attempt: StepAttempt) -> AttemptCost | None:
    if attempt.annotations_json is None:
        return None
    annotations: dict[str, JsonValue] = json.loads(attempt.annotations_json)
    recorded = annotations.get(_COST_KEY)
    if recorded is None:
        if _is_model_call(annotations):
            return _unknown(attempt, "the model call was recorded without a price table")
        return None
    try:
        cost = _RecordedCost.model_validate(recorded)
    except ValidationError:
        return _unknown(attempt, "the recorded cost cannot be read")
    if cost.usd is None:
        return _unknown(attempt, cost.reason)
    return AttemptCost(
        step_key=attempt.step_key,
        attempt=attempt.attempt,
        usd=cost.usd,
        basis=cost.basis,
        model=cost.model,
        parts=cost.parts,
        reason=cost.reason,
    )


def _unknown(attempt: StepAttempt, reason: str | None) -> AttemptCost:
    return AttemptCost(
        step_key=attempt.step_key,
        attempt=attempt.attempt,
        usd=None,
        basis="unknown",
        model=None,
        parts={},
        reason=reason,
    )


def _is_model_call(annotations: Mapping[str, JsonValue]) -> bool:
    return any(key.startswith(_MODEL_CALL_PREFIX) for key in annotations)


def _as_text(amount: Decimal) -> str:
    return format(amount, "f")
