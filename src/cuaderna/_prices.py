import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PlainValidator,
    ValidationError,
    field_validator,
)

from ._errors import PriceTableError
from ._serde import canonical_json

_TOKENS_PER_RATE = Decimal(1_000_000)
_REQUESTS_PER_RATE = Decimal(1_000)
_INPUT_TOKENS_OVER = "input_tokens_over"
_CLOSED = ConfigDict(extra="forbid", frozen=True)


def _rate_from_string(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("a rate is a decimal number written as a string")
    try:
        rate = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{value!r} is not a decimal number") from error
    if not rate.is_finite() or rate < 0:
        raise ValueError(f"{value!r} is not a rate")
    return rate


_Rate = Annotated[Decimal, PlainValidator(_rate_from_string)]


class _Rates(BaseModel):
    model_config = _CLOSED

    input: _Rate | None = None
    output: _Rate | None = None
    cache_read: _Rate | None = None
    cache_write: dict[str, _Rate] = {}
    requests: dict[str, _Rate] = {}


class _Variant(BaseModel):
    model_config = _CLOSED

    when: dict[str, JsonValue] = Field(min_length=1)
    rates: _Rates

    @field_validator("when")
    @classmethod
    def _threshold_is_a_count(cls, when: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if _INPUT_TOKENS_OVER not in when:
            return when
        threshold = when[_INPUT_TOKENS_OVER]
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise ValueError(f"{_INPUT_TOKENS_OVER} is a whole number of tokens")
        return when


class _ModelPrices(BaseModel):
    model_config = _CLOSED

    provider: str
    names: list[str] = Field(min_length=1)
    rates: _Rates
    variants: list[_Variant] = []


class _Document(BaseModel):
    model_config = _CLOSED

    format: Literal["cuaderna.prices/1"]
    id: str
    currency: Literal["USD"]
    models: list[_ModelPrices]


class _ModelCall(BaseModel):
    provider: str | None = Field(default=None, alias="gen_ai.provider.name")
    requested_model: str | None = Field(default=None, alias="gen_ai.request.model")
    response_model: str | None = Field(default=None, alias="gen_ai.response.model")
    input_tokens: NonNegativeInt | None = Field(default=None, alias="gen_ai.usage.input_tokens")
    output_tokens: NonNegativeInt | None = Field(default=None, alias="gen_ai.usage.output_tokens")
    cache_read_tokens: NonNegativeInt = Field(
        default=0, alias="gen_ai.usage.cache_read.input_tokens"
    )
    cache_write_tokens: NonNegativeInt = Field(
        default=0, alias="gen_ai.usage.cache_write.input_tokens"
    )
    cache_writes_by_lifetime: dict[str, NonNegativeInt] = Field(
        default={}, alias="cuaderna.usage.cache_write_by_ttl"
    )
    requests: dict[str, NonNegativeInt] = Field(default={}, alias="cuaderna.usage.requests")
    conditions: dict[str, JsonValue] = Field(default={}, alias="cuaderna.usage.conditions")


@dataclass(frozen=True, slots=True)
class CallCost:
    usd: Decimal
    model: str
    variant: Mapping[str, JsonValue] | None
    parts: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class UnknownCost:
    reason: str


class PriceTable:
    def __init__(self, document: Mapping[str, object]) -> None:
        try:
            parsed = _Document.model_validate(document)
        except ValidationError as error:
            raise PriceTableError(f"not a price table this library can read: {error}") from error
        prices_by_name: dict[tuple[str, str], _ModelPrices] = {}
        for prices in parsed.models:
            for name in prices.names:
                key = (prices.provider.lower(), name.lower())
                if key in prices_by_name:
                    raise PriceTableError(
                        f"the {prices.provider} model {name!r} is listed more than once"
                    )
                prices_by_name[key] = prices
        self.id = parsed.id
        self.content_hash = sha256(
            canonical_json(parsed.model_dump(mode="json")).encode()
        ).hexdigest()[:12]
        self._prices_by_name = prices_by_name

    @classmethod
    def load(cls, path: str | Path) -> "PriceTable":
        text = Path(path).read_text(encoding="utf-8")
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise PriceTableError(f"the price table is not JSON: {error}") from error
        if not isinstance(document, dict):
            raise PriceTableError("the price table is not a JSON object")
        return cls(document)  # pyright: ignore[reportUnknownArgumentType]

    def price(self, annotations: Mapping[str, JsonValue]) -> CallCost | UnknownCost:
        try:
            call = _ModelCall.model_validate(annotations)
        except ValidationError:
            return UnknownCost("the usage in the annotations cannot be read")
        if call.provider is None:
            return UnknownCost("the annotations name no provider")
        if call.input_tokens is None or call.output_tokens is None:
            return UnknownCost("the response reported no usage")
        found = self._find(call.provider, call.response_model, call.requested_model)
        if found is None:
            asked_for = call.response_model or call.requested_model
            return UnknownCost(f"the price table has no {call.provider} model named {asked_for!r}")
        name, prices = found
        variant = _first_matching(prices.variants, call.conditions, call.input_tokens)
        rates = prices.rates if variant is None else variant.rates
        uncached_tokens = call.input_tokens - call.cache_read_tokens - call.cache_write_tokens
        if uncached_tokens < 0:
            return UnknownCost("the cached tokens add up to more than the input")
        if sum(call.cache_writes_by_lifetime.values()) != call.cache_write_tokens:
            return UnknownCost("the cache writes are not broken down by lifetime")
        amounts = {
            "input": _amount(uncached_tokens, rates.input, _TOKENS_PER_RATE),
            "cache_read": _amount(call.cache_read_tokens, rates.cache_read, _TOKENS_PER_RATE),
            "cache_write": _amount_by_kind(
                call.cache_writes_by_lifetime, rates.cache_write, _TOKENS_PER_RATE
            ),
            "output": _amount(call.output_tokens, rates.output, _TOKENS_PER_RATE),
            "requests": _amount_by_kind(call.requests, rates.requests, _REQUESTS_PER_RATE),
        }
        parts: dict[str, Decimal] = {}
        for part, amount in amounts.items():
            if amount is None:
                return UnknownCost(f"the price table has no {part} rate for this call")
            parts[part] = amount
        return CallCost(
            usd=sum(parts.values(), Decimal(0)),
            model=name,
            variant=None if variant is None else variant.when,
            parts=parts,
        )

    def _find(self, provider: str, *names: str | None) -> tuple[str, _ModelPrices] | None:
        for name in names:
            if name is None:
                continue
            prices = self._prices_by_name.get((provider.lower(), name.lower()))
            if prices is not None:
                return name, prices
        return None


def _first_matching(
    variants: list[_Variant], conditions: Mapping[str, JsonValue], input_tokens: int
) -> _Variant | None:
    for variant in variants:
        if _matches(variant.when, conditions, input_tokens):
            return variant
    return None


def _matches(
    when: Mapping[str, JsonValue], conditions: Mapping[str, JsonValue], input_tokens: int
) -> bool:
    for key, expected in when.items():
        if key == _INPUT_TOKENS_OVER:
            if not isinstance(expected, int) or input_tokens <= expected:
                return False
        elif key not in conditions or conditions[key] != expected:
            return False
    return True


def _amount(count: int, rate: Decimal | None, counts_per_rate: Decimal) -> Decimal | None:
    if count == 0:
        return Decimal(0)
    if rate is None:
        return None
    return count * rate / counts_per_rate


def _amount_by_kind(
    counts: Mapping[str, int], rates: Mapping[str, Decimal], counts_per_rate: Decimal
) -> Decimal | None:
    total = Decimal(0)
    for kind, count in counts.items():
        amount = _amount(count, rates.get(kind), counts_per_rate)
        if amount is None:
            return None
        total += amount
    return total
