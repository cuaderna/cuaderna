import hashlib
import inspect
import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from pydantic import TypeAdapter
from pydantic.errors import PydanticUserError

from ._errors import InvalidDefinitionError, TerminalError


class ErrorDetails(TypedDict):
    type: str
    message: str
    traceback: str
    terminal: bool


_ERROR_DETAILS = TypeAdapter(ErrorDetails)


@dataclass(frozen=True, slots=True)
class Codec[R]:
    signature: inspect.Signature
    parameters: dict[str, TypeAdapter[Any]]
    result: TypeAdapter[R]

    def dump_arguments(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        plain = {
            name: self.parameters[name].dump_python(value, mode="json", warnings="error")
            for name, value in bound.arguments.items()
            if name in self.parameters
        }
        return json.dumps(plain, sort_keys=True, separators=(",", ":"))

    def load_arguments(self, arguments_json: str) -> inspect.BoundArguments:
        plain: dict[str, object] = json.loads(arguments_json)
        if plain.keys() != self.parameters.keys():
            raise ValueError(
                f"stored arguments {sorted(plain)} are not the parameters {sorted(self.parameters)}"
            )
        bound = self.signature.bind_partial()
        bound.arguments = {
            name: self.parameters[name].validate_json(json.dumps(plain[name]))
            for name in self.signature.parameters
        }
        return bound

    def dump_result(self, value: R) -> str:
        return self.result.dump_json(value, warnings="error").decode()

    def load_result(self, result_json: str) -> R:
        return self.result.validate_json(result_json)


def build_codec(function: Callable[..., Any], name: str) -> Codec[Any]:
    try:
        signature = inspect.signature(function, eval_str=True)
    except NameError as error:
        raise InvalidDefinitionError(
            f"{name}: an annotation cannot be resolved: {error}"
        ) from error
    receiver = receiver_of(signature)
    parameters = {
        parameter.name: _adapter(name, f"parameter {parameter.name!r}", _annotation_of(parameter))
        for parameter in signature.parameters.values()
        if parameter.name != receiver
    }
    result = _adapter(name, "the return value", signature.return_annotation)
    return Codec(signature, parameters, result)


def receiver_of(signature: inspect.Signature) -> str | None:
    first = next(iter(signature.parameters.values()), None)
    if first is None or first.name not in ("self", "cls"):
        return None
    if first.annotation is not inspect.Parameter.empty:
        return None
    return first.name


def hash_arguments(args_json: str) -> str:
    return hashlib.sha256(args_json.encode()).hexdigest()


def describe_error(error: Exception) -> ErrorDetails:
    return {
        "type": type(error).__qualname__,
        "message": str(error),
        "traceback": "".join(traceback.format_exception(error)),
        "terminal": isinstance(error, TerminalError),
    }


def load_error(error_json: str) -> ErrorDetails:
    return _ERROR_DETAILS.validate_json(error_json)


def _annotation_of(parameter: inspect.Parameter) -> object:
    annotation = parameter.annotation
    if annotation is inspect.Parameter.empty:
        return annotation
    if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
        return tuple[annotation, ...]
    if parameter.kind is inspect.Parameter.VAR_KEYWORD:
        return dict[str, annotation]
    return annotation


def _adapter(name: str, subject: str, annotation: object) -> TypeAdapter[Any]:
    if annotation is inspect.Parameter.empty:
        raise InvalidDefinitionError(f"{name}: {subject} needs a type annotation")
    try:
        return TypeAdapter[Any](cast("type[Any]", annotation))
    except PydanticUserError as error:
        raise InvalidDefinitionError(
            f"{name}: {subject} has a type that cannot be converted to JSON: {annotation!r}"
        ) from error
