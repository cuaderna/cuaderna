import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainValidator,
    ValidationError,
    field_validator,
)

from ._errors import PriceTableError
from ._serde import canonical_json

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
