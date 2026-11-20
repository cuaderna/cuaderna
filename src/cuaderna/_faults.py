import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from ._errors import FaultPlanError
from ._files import read_text_within
from ._serde import canonical_json

_CLOSED = ConfigDict(extra="forbid", frozen=True)
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


class _Raise(BaseModel):
    model_config = _CLOSED

    error_type: str = Field(min_length=1, alias="type")
    error_message: str = Field(alias="message")
    attempts: int | None = Field(default=None, ge=1, strict=True)
    terminal: bool = Field(default=False, strict=True)


class _Delay(BaseModel):
    model_config = _CLOSED

    seconds: float = Field(gt=0, le=3600, strict=True)
    attempts: int | None = Field(default=None, ge=1, strict=True)


class _Append(BaseModel):
    model_config = _CLOSED

    pointer: str = Field(alias="to", pattern=r"^(/([^~/]|~[01])*)*$")
    text: str = Field(min_length=1)


class _Fault(BaseModel):
    model_config = _CLOSED

    id: str = Field(min_length=1)
    at: str = Field(pattern=r"^[^:#]+(#[1-9][0-9]*)?$")
    raise_: _Raise | None = Field(default=None, alias="raise")
    return_: JsonValue = Field(default=None, alias="return")
    append: _Append | None = None
    delay: _Delay | None = None

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> Self:
        present = [
            name
            for name in ("raise_", "return_", "append", "delay")
            if name in self.model_fields_set
        ]
        if len(present) != 1:
            raise ValueError("a fault has exactly one of raise, return, append and delay")
        if present[0] != "return_" and getattr(self, present[0]) is None:
            raise ValueError("a raise, append or delay fault needs a body")
        return self


class _Document(BaseModel):
    model_config = _CLOSED

    format: Literal["cuaderna.faults/1"]
    id: str = Field(min_length=1)
    faults: list[_Fault]


def _pointer_tokens(pointer: str) -> list[str]:
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


def _append_into(node: JsonValue, tokens: list[str], text: str) -> JsonValue:
    if not tokens:
        if isinstance(node, str):
            return node + text
        raise LookupError
    head, *rest = tokens
    if isinstance(node, dict) and head in node:
        node[head] = _append_into(node[head], rest, text)
        return node
    if isinstance(node, list) and _ARRAY_INDEX.fullmatch(head) and int(head) < len(node):
        node[int(head)] = _append_into(node[int(head)], rest, text)
        return node
    raise LookupError


@dataclass(frozen=True, slots=True)
class Fault:
    id: str
    at: str
    kind: Literal["raise", "return", "append", "delay"]
    plan_id: str
    plan_hash: str
    error_type: str = ""
    error_message: str = ""
    attempts: int | None = None
    terminal: bool = False
    value: JsonValue = None
    pointer: str = ""
    text: str = ""
    seconds: float = 0.0

    def targets(self, step_key: str) -> bool:
        if self.at == step_key:
            return True
        return "#" not in self.at and self.at == step_key.partition("#")[0]

    def fails_attempt(self, attempt: int) -> bool:
        if self.kind != "raise":
            return False
        return self.attempts is None or attempt <= self.attempts

    def delays_attempt(self, attempt: int) -> bool:
        if self.kind != "delay":
            return False
        return self.attempts is None or attempt <= self.attempts

    def appended_to(self, result: JsonValue) -> JsonValue:
        try:
            return _append_into(deepcopy(result), _pointer_tokens(self.pointer), self.text)
        except (LookupError, RecursionError) as error:
            raise FaultPlanError(
                f"fault {self.id!r} found no string at the pointer {self.pointer!r}"
            ) from error


def _build_fault(parsed: _Fault, plan_id: str, plan_hash: str) -> Fault:
    if parsed.raise_ is not None:
        payload = parsed.raise_.model_dump()
        return Fault(parsed.id, parsed.at, "raise", plan_id, plan_hash, **payload)
    if parsed.append is not None:
        payload = parsed.append.model_dump()
        return Fault(parsed.id, parsed.at, "append", plan_id, plan_hash, **payload)
    if parsed.delay is not None:
        payload = parsed.delay.model_dump()
        return Fault(parsed.id, parsed.at, "delay", plan_id, plan_hash, **payload)
    return Fault(parsed.id, parsed.at, "return", plan_id, plan_hash, value=parsed.return_)


class FaultPlan:
    def __init__(self, document: Mapping[str, object]) -> None:
        try:
            parsed = _Document.model_validate(document)
        except ValidationError as error:
            raise FaultPlanError(f"not a fault plan this library can read: {error}") from error
        fault_ids = [fault.id for fault in parsed.faults]
        if len(set(fault_ids)) != len(fault_ids):
            raise FaultPlanError("a fault id is used more than once")
        self.id = parsed.id
        self.content_hash = sha256(
            canonical_json(
                parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
            ).encode()
        ).hexdigest()
        self.faults = tuple(
            _build_fault(fault, self.id, self.content_hash) for fault in parsed.faults
        )

    @classmethod
    def load(cls, path: str | Path, *, max_bytes: int = 1_048_576) -> "FaultPlan":
        try:
            text = read_text_within(path, max_bytes)
        except (OSError, ValueError) as error:
            raise FaultPlanError(f"the fault plan file cannot be read: {error}") from error
        try:
            document = json.loads(text)
        except (ValueError, RecursionError) as error:
            raise FaultPlanError(f"the fault plan is not JSON: {error}") from error
        if not isinstance(document, dict):
            raise FaultPlanError("the fault plan is not a JSON object")
        try:
            return cls(document)  # pyright: ignore[reportUnknownArgumentType]
        except RecursionError as error:
            raise FaultPlanError("the fault plan is nested too deeply") from error
