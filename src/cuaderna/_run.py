import dataclasses
import functools
import inspect
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, overload

from ._errors import InvalidDefinitionError
from ._policy import Policy
from ._serde import Codec, build_codec, receiver_of


@dataclass(frozen=True, slots=True)
class StepDefinition:
    name: str
    invoke: Callable[..., Coroutine[Any, Any, Any]]
    codec: Codec[Any]
    max_attempts: int
    backoff_seconds: float
    timeout_seconds: float | None = None
    is_library: bool = False


@dataclass(slots=True)
class StepKeys:
    occurrences: dict[str, int] = field(default_factory=dict[str, int])

    def reserve(self, name: str) -> str:
        occurrence = self.occurrences.get(name, 0) + 1
        self.occurrences[name] = occurrence
        return f"{name}#{occurrence}"


class RunContext(Protocol):
    @property
    def step_keys(self) -> StepKeys: ...

    async def execute_step(
        self,
        definition: StepDefinition,
        step_key: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RunDefinition[**P, R]:
    name: str
    function: Callable[P, Coroutine[Any, Any, R]]
    codec: Codec[R]
    code_version: str | None
    budget_usd: Decimal | None = None
    policy: Policy | None = None

    def with_budget(self, budget_usd: Decimal | str) -> "RunDefinition[P, R]":
        return dataclasses.replace(self, budget_usd=_as_budget(self.name, budget_usd))

    def with_policy(self, policy: Policy) -> "RunDefinition[P, R]":
        return dataclasses.replace(self, policy=policy)


class _RunDecorator(Protocol):
    def __call__[**P, R](
        self, function: Callable[P, Coroutine[Any, Any, R]], /
    ) -> RunDefinition[P, R]: ...


_active_run: ContextVar[RunContext | None] = ContextVar("cuaderna_active_run", default=None)


@overload
def run[**P, R](function: Callable[P, Coroutine[Any, Any, R]], /) -> RunDefinition[P, R]: ...


@overload
def run(
    *,
    code_version: str | None = None,
    budget_usd: Decimal | str | None = None,
    policy: Policy | None = None,
) -> _RunDecorator: ...


def run(
    function: Callable[..., Coroutine[Any, Any, Any]] | None = None,
    /,
    *,
    code_version: str | None = None,
    budget_usd: Decimal | str | None = None,
    policy: Policy | None = None,
) -> Any:
    define = functools.partial(
        _define, code_version=code_version, budget_usd=budget_usd, policy=policy
    )
    if function is None:
        return define
    return define(function)


def _define(
    function: Callable[..., Coroutine[Any, Any, Any]],
    *,
    code_version: str | None,
    budget_usd: Decimal | str | None,
    policy: Policy | None,
) -> RunDefinition[..., Any]:
    name = function.__name__
    if not inspect.iscoroutinefunction(function):
        raise InvalidDefinitionError(f"{name}: a run has to be an async function")
    if receiver_of(inspect.signature(function)) is not None:
        raise InvalidDefinitionError(f"{name}: a run cannot be a method")
    budget = None if budget_usd is None else _as_budget(name, budget_usd)
    return RunDefinition(name, function, build_codec(function, name), code_version, budget, policy)


def _as_budget(name: str, budget_usd: object) -> Decimal:
    if isinstance(budget_usd, (Decimal, str)):
        try:
            amount = Decimal(budget_usd)
        except InvalidOperation:
            amount = None
        if amount is not None and amount.is_finite() and amount > 0:
            return amount
    raise InvalidDefinitionError(
        f"{name}: a budget has to be an amount in USD greater than zero, "
        f"written as a string or a Decimal, not {budget_usd!r}"
    )


def current_run() -> RunContext | None:
    return _active_run.get()


@contextmanager
def active_run(context: RunContext) -> Generator[None]:
    token = _active_run.set(context)
    try:
        yield
    finally:
        _active_run.reset(token)
