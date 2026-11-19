import inspect
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ._errors import InvalidDefinitionError
from ._records import Lease
from ._serde import Codec, build_codec
from .storage import Storage


@dataclass(slots=True)
class RunContext:
    storage: Storage
    lease: Lease
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


_active_run: ContextVar[RunContext | None] = ContextVar("cuaderna_active_run", default=None)


def run[**P, R](function: Callable[P, Coroutine[Any, Any, R]]) -> RunDefinition[P, R]:
    name = function.__name__
    if not inspect.iscoroutinefunction(function):
        raise InvalidDefinitionError(f"{name}: a run has to be an async function")
    return RunDefinition(name, function, build_codec(function, name))


def current_run() -> RunContext | None:
    return _active_run.get()


@contextmanager
def active_run(context: RunContext) -> Generator[None]:
    token = _active_run.set(context)
    try:
        yield
    finally:
        _active_run.reset(token)
