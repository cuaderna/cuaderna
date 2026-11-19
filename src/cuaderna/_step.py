import asyncio
import functools
import inspect
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any, Protocol, overload

from ._errors import InvalidDefinitionError, StepCallError, StepFailedError
from ._run import StepDefinition, current_run
from ._serde import ErrorDetails, build_codec, receiver_of

type _AsyncFunction[**P, R] = Callable[P, Coroutine[Any, Any, R]]

_LONGEST_RETRY_DELAY_SECONDS = 60.0

active_step_key: ContextVar[str | None] = ContextVar("cuaderna_active_step_key", default=None)


class _StepDecorator(Protocol):
    @overload
    def __call__[**P, R](self, function: _AsyncFunction[P, R], /) -> _AsyncFunction[P, R]: ...

    @overload
    def __call__[**P, R](self, function: Callable[P, R], /) -> _AsyncFunction[P, R]: ...


@overload
def step[**P, R](function: _AsyncFunction[P, R], /) -> _AsyncFunction[P, R]: ...


@overload
def step[**P, R](function: Callable[P, R], /) -> _AsyncFunction[P, R]: ...


@overload
def step(
    *, name: str | None = None, max_attempts: int = 1, backoff_seconds: float = 1.0
) -> _StepDecorator: ...


def step(
    function: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    max_attempts: int = 1,
    backoff_seconds: float = 1.0,
) -> Any:
    define = functools.partial(
        _define, name=name, max_attempts=max_attempts, backoff_seconds=backoff_seconds
    )
    if function is None:
        return define
    return define(function)


def retry_delay(failed_attempt: int, backoff_seconds: float) -> float:
    return min(backoff_seconds * 2 ** (failed_attempt - 1), _LONGEST_RETRY_DELAY_SECONDS)


def idempotency_key() -> str:
    key = active_step_key.get()
    if key is None:
        raise StepCallError("idempotency_key was called outside a step")
    return key


def _define(
    function: Callable[..., Any], *, name: str | None, max_attempts: int, backoff_seconds: float
) -> _AsyncFunction[..., Any]:
    step_name = name or _default_name(function)
    if ":" in step_name or "#" in step_name:
        raise InvalidDefinitionError(f"{step_name}: a step name cannot contain ':' or '#'")
    if max_attempts < 1:
        raise InvalidDefinitionError(f"{step_name}: max_attempts has to be at least 1")
    if inspect.iscoroutinefunction(function):
        invoke: _AsyncFunction[..., Any] = function
    else:
        invoke = functools.partial(asyncio.to_thread, function)
    definition = StepDefinition(
        step_name, invoke, build_codec(function, step_name), max_attempts, backoff_seconds
    )

    @functools.wraps(function)
    def call(*args: object, **kwargs: object) -> Coroutine[Any, Any, object]:
        context = current_run()
        if context is None:
            raise StepCallError(f"step {step_name!r} was called outside a run")
        if active_step_key.get() is not None:
            raise StepCallError(f"step {step_name!r} was called inside another step")
        step_key = context.step_keys.reserve(step_name)
        return context.execute_step(definition, step_key, args, kwargs)

    return call


def _default_name(function: Callable[..., Any]) -> str:
    if receiver_of(inspect.signature(function)) is None:
        return function.__name__
    owner = function.__qualname__.split(".")[-2]
    return f"{owner}.{function.__name__}"


def step_failed(step_key: str, details: ErrorDetails) -> StepFailedError:
    return StepFailedError(step_key, details["type"], details["message"], details["traceback"])
