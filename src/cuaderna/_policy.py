import json
import posixpath
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from ._errors import PolicyError
from ._files import read_text_within
from ._serde import canonical_json

Effect = Literal["allow", "ask", "deny"]
Reason = Literal["rule", "no_rule", "unevaluable"]

_CLOSED = ConfigDict(extra="forbid", frozen=True)
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_SHORT_NAME_MARKER = re.compile(r"~[0-9]")
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class _Unevaluable(Exception):
    pass


def _with_forward_slashes(path: str) -> str:
    return path.replace("\\", "/")


class _Condition(BaseModel):
    model_config = _CLOSED

    equals: JsonValue = None
    member_of: list[JsonValue] = Field(default=[], alias="in")
    glob: str = ""
    under: str = ""

    @field_validator("under")
    @classmethod
    def _under_stays_inside_the_tree(cls, directory: str) -> str:
        slashed = _with_forward_slashes(directory)
        if slashed.startswith("/") or _DRIVE_LETTER.match(slashed):
            raise ValueError(f"{directory!r} is not a relative directory")
        if ".." in slashed.split("/"):
            raise ValueError(f"{directory!r} climbs out of the tree")
        return directory

    @model_validator(mode="after")
    def _exactly_one_key(self) -> Self:
        if len(self.model_fields_set) != 1:
            raise ValueError("a condition has exactly one of equals, in, glob and under")
        return self


class _Rule(BaseModel):
    model_config = _CLOSED

    id: str = Field(min_length=1)
    effect: Effect
    steps: list[str] = Field(min_length=1)
    when: dict[str, _Condition] = {}


class _Document(BaseModel):
    model_config = _CLOSED

    format: Literal["cuaderna.policy/1"]
    id: str = Field(min_length=1)
    rules: list[_Rule]


@dataclass(frozen=True, slots=True)
class Decision:
    effect: Effect
    reason: Reason
    rules: tuple[str, ...]


def _lookup(arguments: JsonValue, path: str) -> JsonValue:
    current = arguments
    for name in path.split("."):
        if not isinstance(current, dict) or name not in current:
            raise _Unevaluable
        current = current[name]
    return current


def _is_ambiguous_component(component: str) -> bool:
    if any(ord(character) < 32 for character in component):
        return True
    if component not in (".", "..") and component.endswith((".", " ")):
        return True
    if ":" in component or _SHORT_NAME_MARKER.search(component):
        return True
    return component.split(".")[0].strip().upper() in _DEVICE_NAMES


def _has_ambiguous_spelling(slashed: str) -> bool:
    if unicodedata.normalize("NFC", slashed) != slashed:
        return True
    without_drive = _DRIVE_LETTER.sub("", slashed)
    return any(_is_ambiguous_component(part) for part in without_drive.split("/"))


def _is_inside(normalized: str, base: str) -> bool:
    if base == ".":
        return normalized != ".." and not normalized.startswith("../")
    return normalized == base or normalized.startswith(base + "/")


def _is_under(value: str, directory: str) -> bool:
    slashed = _with_forward_slashes(value)
    if _has_ambiguous_spelling(slashed):
        raise _Unevaluable
    if slashed.startswith("/") or _DRIVE_LETTER.match(slashed):
        return False
    normalized = posixpath.normpath(slashed)
    base = posixpath.normpath(_with_forward_slashes(directory))
    if _is_inside(normalized, base):
        return True
    if _is_inside(normalized.casefold(), base.casefold()):
        raise _Unevaluable
    return False


def _is_number(value: JsonValue) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _same_json_value(left: JsonValue, right: JsonValue) -> bool:
    if _is_number(left) and _is_number(right):
        return left == right
    return canonical_json(left) == canonical_json(right)


def _holds(condition: _Condition, value: JsonValue) -> bool:
    if "equals" in condition.model_fields_set:
        return _same_json_value(value, condition.equals)
    if "member_of" in condition.model_fields_set:
        return any(_same_json_value(value, option) for option in condition.member_of)
    if not isinstance(value, str):
        raise _Unevaluable
    if "glob" in condition.model_fields_set:
        if fnmatchcase(value, condition.glob):
            return True
        if fnmatchcase(value.casefold(), condition.glob.casefold()):
            raise _Unevaluable
        return False
    return _is_under(value, condition.under)


def _applies(rule: _Rule, arguments: JsonValue) -> bool:
    results = [_holds(condition, _lookup(arguments, path)) for path, condition in rule.when.items()]
    return all(results)


class Policy:
    def __init__(self, document: Mapping[str, object]) -> None:
        try:
            parsed = _Document.model_validate(document)
        except ValidationError as error:
            raise PolicyError(f"not a policy this library can read: {error}") from error
        rule_ids = [rule.id for rule in parsed.rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise PolicyError("a rule id is used more than once")
        self.id = parsed.id
        self.content_hash = sha256(
            canonical_json(
                parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
            ).encode()
        ).hexdigest()
        self._rules = parsed.rules

    @classmethod
    def load(cls, path: str | Path, *, max_bytes: int = 1_048_576) -> "Policy":
        try:
            text = read_text_within(path, max_bytes)
        except (OSError, ValueError) as error:
            raise PolicyError(f"the policy file cannot be read: {error}") from error
        try:
            document = json.loads(text)
        except (ValueError, RecursionError) as error:
            raise PolicyError(f"the policy is not JSON: {error}") from error
        if not isinstance(document, dict):
            raise PolicyError("the policy is not a JSON object")
        try:
            return cls(document)  # pyright: ignore[reportUnknownArgumentType]
        except RecursionError as error:
            raise PolicyError("the policy is nested too deeply") from error

    def decide(self, step_name: str, args_json: str) -> Decision:
        arguments: JsonValue = json.loads(args_json)
        unevaluable: list[str] = []
        matching: list[_Rule] = []
        for rule in self._rules:
            if step_name not in rule.steps:
                continue
            try:
                if _applies(rule, arguments):
                    matching.append(rule)
            except _Unevaluable:
                unevaluable.append(rule.id)
        if unevaluable:
            return Decision("deny", "unevaluable", tuple(unevaluable))
        for effect in ("deny", "ask", "allow"):
            ids = tuple(rule.id for rule in matching if rule.effect == effect)
            if ids:
                return Decision(effect, "rule", ids)
        return Decision("deny", "no_rule", ())
