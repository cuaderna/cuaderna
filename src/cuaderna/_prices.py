import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from ._errors import PriceTableError
from ._files import read_text_within
from ._serde import canonical_json

MAX_TOKEN_COUNT = 10**12
_MAX_RATE = Decimal(10**9)
_MAX_RATE_DECIMALS = 18
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


_Rate = Annotated[Decimal, BeforeValidator(_rate_from_string)]
TokenCount = Annotated[int, Field(ge=0, le=MAX_TOKEN_COUNT, strict=True)]


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
    input_tokens: TokenCount | None = Field(default=None, alias="gen_ai.usage.input_tokens")
    output_tokens: TokenCount | None = Field(default=None, alias="gen_ai.usage.output_tokens")
    cache_read_tokens: TokenCount = Field(default=0, alias="gen_ai.usage.cache_read.input_tokens")
    cache_write_tokens: TokenCount = Field(default=0, alias="gen_ai.usage.cache_write.input_tokens")
    cache_writes_by_lifetime: dict[str, TokenCount] = Field(
        default={}, alias="cuaderna.usage.cache_write_by_ttl"
    )
    requests: dict[str, TokenCount] = Field(default={}, alias="cuaderna.usage.requests")
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
            _require_rates_within_bounds(prices)
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
    def load(cls, path: str | Path, *, max_bytes: int = 1_048_576) -> "PriceTable":
        try:
            text = read_text_within(path, max_bytes)
        except (OSError, ValueError) as error:
            raise PriceTableError(f"the price table file cannot be read: {error}") from error
        try:
            document = json.loads(text)
        except (ValueError, RecursionError) as error:
            raise PriceTableError(f"the price table is not JSON: {error}") from error
        if not isinstance(document, dict):
            raise PriceTableError("the price table is not a JSON object")
        try:
            return cls(document)  # pyright: ignore[reportUnknownArgumentType]
        except RecursionError as error:
            raise PriceTableError("the price table is nested too deeply") from error

    def price(self, annotations: Mapping[str, JsonValue]) -> CallCost | UnknownCost:
        call = _read_call(annotations)
        if isinstance(call, UnknownCost):
            return call
        if call.provider is None:
            return UnknownCost("the annotations name no provider")
        usage = _read_usage(call)
        if isinstance(usage, UnknownCost):
            return usage
        found = self._find(call.provider, call.response_model, call.requested_model)
        if found is None:
            asked_for = call.response_model or call.requested_model
            return UnknownCost(f"the price table has no {call.provider} model named {asked_for!r}")
        name, prices = found
        variant = _first_matching(prices.variants, call.conditions, usage[0])
        rates = prices.rates if variant is None else variant.rates
        return _price_call(call, usage, name, rates, None if variant is None else variant.when)

    def price_as(
        self, annotations: Mapping[str, JsonValue], *, provider: str, model: str
    ) -> CallCost | UnknownCost:
        call = _read_call(annotations)
        if isinstance(call, UnknownCost):
            return call
        usage = _read_usage(call)
        if isinstance(usage, UnknownCost):
            return usage
        return _price_call(call, usage, model, self._base_prices(provider, model).rates, None)

    def require_model(self, provider: str, model: str) -> None:
        self._base_prices(provider, model)

    def _base_prices(self, provider: str, model: str) -> _ModelPrices:
        found = self._find(provider, model)
        if found is None:
            raise PriceTableError(f"the price table has no {provider} model named {model!r}")
        return found[1]

    def _find(self, provider: str, *names: str | None) -> tuple[str, _ModelPrices] | None:
        for name in names:
            if name is None:
                continue
            prices = self._prices_by_name.get((provider.lower(), name.lower()))
            if prices is not None:
                return name, prices
        return None


def _require_rates_within_bounds(prices: _ModelPrices) -> None:
    for rates in [prices.rates, *(variant.rates for variant in prices.variants)]:
        named = {"input": rates.input, "output": rates.output, "cache_read": rates.cache_read}
        named.update({f"cache_write.{kind}": rate for kind, rate in rates.cache_write.items()})
        named.update({f"requests.{kind}": rate for kind, rate in rates.requests.items()})
        for field, rate in named.items():
            if rate is not None and not _is_within_bounds(rate):
                raise PriceTableError(
                    f"the {prices.provider} model {prices.names[0]!r} has a {field} rate of "
                    f"{rate}, which is above {_MAX_RATE} or has more than "
                    f"{_MAX_RATE_DECIMALS} decimal places"
                )


def _is_within_bounds(rate: Decimal) -> bool:
    exponent = rate.as_tuple().exponent
    return rate <= _MAX_RATE and isinstance(exponent, int) and exponent >= -_MAX_RATE_DECIMALS


def _read_call(annotations: Mapping[str, JsonValue]) -> _ModelCall | UnknownCost:
    try:
        return _ModelCall.model_validate(annotations)
    except ValidationError:
        return UnknownCost("the usage in the annotations cannot be read")


def _read_usage(call: _ModelCall) -> tuple[int, int] | UnknownCost:
    if call.input_tokens is None or call.output_tokens is None:
        missing = [
            field
            for field, count in (
                ("gen_ai.usage.input_tokens", call.input_tokens),
                ("gen_ai.usage.output_tokens", call.output_tokens),
            )
            if count is None
        ]
        return UnknownCost(f"the response reported no usage in {' or '.join(missing)}")
    return call.input_tokens, call.output_tokens


def _price_call(
    call: _ModelCall,
    usage: tuple[int, int],
    name: str,
    rates: _Rates,
    variant: Mapping[str, JsonValue] | None,
) -> CallCost | UnknownCost:
    try:
        return _compute_call(call, usage, name, rates, variant)
    except ArithmeticError:
        return UnknownCost("the arithmetic of the cost overflowed")


def _compute_call(
    call: _ModelCall,
    usage: tuple[int, int],
    name: str,
    rates: _Rates,
    variant: Mapping[str, JsonValue] | None,
) -> CallCost | UnknownCost:
    input_tokens, output_tokens = usage
    uncached_tokens = input_tokens - call.cache_read_tokens - call.cache_write_tokens
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
        "output": _amount(output_tokens, rates.output, _TOKENS_PER_RATE),
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
        variant=variant,
        parts=parts,
    )


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
