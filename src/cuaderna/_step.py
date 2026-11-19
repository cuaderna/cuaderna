import asyncio
import functools
import inspect
import json
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, overload

from ._errors import InvalidDefinitionError, StepCallError, StepFailedError, StepMismatchError
from ._records import AttemptStatus, StepAttempt
from ._run import RunContext, current_run
from ._serde import (
    Codec,
    ErrorDetails,
    build_codec,
    describe_error,
    hash_arguments,
    load_error,
    receiver_of,
)

type _AsyncFunction[**P, R] = Callable[P, Coroutine[Any, Any, R]]

_LONGEST_RETRY_DELAY_SECONDS = 60.0

_active_step_key: ContextVar[str | None] = ContextVar("cuaderna_active_step_key", default=None)


@dataclass(frozen=True, slots=True)
class _StepDefinition:
    name: str
    invoke: _AsyncFunction[..., Any]
    codec: Codec[Any]
    max_attempts: int
    backoff_seconds: float


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
    key = _active_step_key.get()
    if key is None:
        raise StepCallError("idempotency_key was called outside a step")
    return key


def _define(
    function: Callable[..., Any], *, name: str | None, max_attempts: int, backoff_seconds: float
) -> _AsyncFunction[..., Any]:
    step_name = name or _default_name(function)
    if max_attempts < 1:
        raise InvalidDefinitionError(f"{step_name}: max_attempts has to be at least 1")
    if inspect.iscoroutinefunction(function):
        invoke: _AsyncFunction[..., Any] = function
    else:
        invoke = functools.partial(asyncio.to_thread, function)
    definition = _StepDefinition(
        step_name, invoke, build_codec(function, step_name), max_attempts, backoff_seconds
    )

    @functools.wraps(function)
    def call(*args: object, **kwargs: object) -> Coroutine[Any, Any, object]:
        context = current_run()
        if context is None:
            raise StepCallError(f"step {step_name!r} was called outside a run")
        if _active_step_key.get() is not None:
            raise StepCallError(f"step {step_name!r} was called inside another step")
        step_key = context.reserve_step_key(step_name)
        return _execute(context, definition, step_key, args, kwargs)

    return call


def _default_name(function: Callable[..., Any]) -> str:
    if receiver_of(inspect.signature(function)) is None:
        return function.__name__
    owner = function.__qualname__.split(".")[-2]
    return f"{owner}.{function.__name__}"


async def _execute(
    context: RunContext,
    definition: _StepDefinition,
    step_key: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> object:
    args_json = definition.codec.dump_arguments(args, kwargs)
    first_attempt = 1
    recorded = context.recorded.get(step_key)
    if recorded is not None:
        if recorded.args_hash != hash_arguments(args_json):
            raise StepMismatchError(
                f"step {step_key!r} was recorded with other arguments: "
                f"{recorded.args_json} then, {args_json} now"
            )
        if recorded.result_json is not None:
            return definition.codec.load_result(recorded.result_json)
        if recorded.error_json is not None:
            details = load_error(recorded.error_json)
            if details["terminal"] or recorded.attempt >= definition.max_attempts:
                raise _step_failed(step_key, details)
        first_attempt = recorded.attempt + 1
    for attempt in range(first_attempt, definition.max_attempts + 1):
        started_at = datetime.now(UTC)
        token = _active_step_key.set(f"{context.lease.run_id}:{step_key}")
        try:
            result_json = definition.codec.dump_result(await definition.invoke(*args, **kwargs))
        except Exception as error:
            details = describe_error(error)
            failed = _attempt(
                step_key,
                definition.name,
                attempt,
                args_json,
                started_at,
                error_json=json.dumps(details),
            )
            await context.storage.record_attempt(context.lease, failed)
            if details["terminal"] or attempt == definition.max_attempts:
                raise _step_failed(step_key, details) from error
            await asyncio.sleep(retry_delay(attempt, definition.backoff_seconds))
        else:
            succeeded = _attempt(
                step_key, definition.name, attempt, args_json, started_at, result_json=result_json
            )
            await context.storage.record_attempt(context.lease, succeeded)
            return definition.codec.load_result(result_json)
        finally:
            _active_step_key.reset(token)
    raise StepMismatchError(f"step {step_key!r} has more recorded attempts than it allows now")


def _step_failed(step_key: str, details: ErrorDetails) -> StepFailedError:
    return StepFailedError(step_key, details["type"], details["message"], details["traceback"])


def _attempt(
    step_key: str,
    name: str,
    attempt: int,
    args_json: str,
    started_at: datetime,
    *,
    result_json: str | None = None,
    error_json: str | None = None,
) -> StepAttempt:
    return StepAttempt(
        step_key=step_key,
        name=name,
        attempt=attempt,
        status=AttemptStatus.OK if error_json is None else AttemptStatus.ERROR,
        args_hash=hash_arguments(args_json),
        args_json=args_json,
        result_json=result_json,
        error_json=error_json,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
