import functools
import inspect
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, overload

from ._errors import InvalidDefinitionError
from ._records import Lease, StepAttempt
from ._serde import Codec, build_codec
from .storage import Storage


@dataclass(slots=True)
class RunContext:
    storage: Storage
    lease: Lease
    recorded: dict[str, StepAttempt]
    occurrences: dict[str, int] = field(default_factory=dict[str, int])

    def reserve_step_key(self, name: str) -> str:
        occurrence = self.occurrences.get(name, 0) + 1
        self.occurrences[name] = occurrence
        return f"{name}#{occurrence}"


@dataclass(frozen=True, slots=True)
class RunDefinition[**P, R]:
    name: str
    function: Callable[P, Coroutine[Any, Any, R]]
    codec: Codec[R]
    code_version: str | None


class _RunDecorator(Protocol):
    def __call__[**P, R](
        self, function: Callable[P, Coroutine[Any, Any, R]], /
    ) -> RunDefinition[P, R]: ...


_active_run: ContextVar[RunContext | None] = ContextVar("cuaderna_active_run", default=None)


@overload
def run[**P, R](function: Callable[P, Coroutine[Any, Any, R]], /) -> RunDefinition[P, R]: ...


@overload
def run(*, code_version: str | None = None) -> _RunDecorator: ...


def run(
    function: Callable[..., Coroutine[Any, Any, Any]] | None = None,
    /,
    *,
    code_version: str | None = None,
) -> Any:
    define = functools.partial(_define, code_version=code_version)
    if function is None:
        return define
    return define(function)


def _define(
    function: Callable[..., Coroutine[Any, Any, Any]], *, code_version: str | None
) -> RunDefinition[..., Any]:
    name = function.__name__
    if not inspect.iscoroutinefunction(function):
        raise InvalidDefinitionError(f"{name}: a run has to be an async function")
    return RunDefinition(name, function, build_codec(function, name), code_version)


def current_run() -> RunContext | None:
    return _active_run.get()


@contextmanager
def active_run(context: RunContext) -> Generator[None]:
    token = _active_run.set(context)
    try:
        yield
    finally:
        _active_run.reset(token)
