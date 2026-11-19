import asyncio
import functools
import inspect
import json
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Protocol, overload

from ._errors import StepCallError, StepFailedError
from ._records import AttemptStatus, StepAttempt
from ._run import RunContext, current_run
from ._serde import Codec, build_codec, describe_error, hash_arguments

type _AsyncFunction[**P, R] = Callable[P, Coroutine[Any, Any, R]]

_inside_step: ContextVar[bool] = ContextVar("cuaderna_inside_step", default=False)


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
def step(*, name: str | None = None) -> _StepDecorator: ...


def step(function: Callable[..., Any] | None = None, /, *, name: str | None = None) -> Any:
    if function is None:
        return functools.partial(_define, name=name)
    return _define(function, name=name)


def _define(function: Callable[..., Any], *, name: str | None) -> _AsyncFunction[..., Any]:
    step_name = name or function.__name__
    codec = build_codec(function, step_name)
    if inspect.iscoroutinefunction(function):
        invoke: _AsyncFunction[..., Any] = function
    else:
        invoke = functools.partial(asyncio.to_thread, function)

    @functools.wraps(function)
    def call(*args: object, **kwargs: object) -> Coroutine[Any, Any, object]:
        context = current_run()
        if context is None:
            raise StepCallError(f"step {step_name!r} was called outside a run")
        if _inside_step.get():
            raise StepCallError(f"step {step_name!r} was called inside another step")
        step_key = context.reserve_step_key(step_name)
        return _execute(context, step_name, step_key, invoke, codec, args, kwargs)

    return call


async def _execute(
    context: RunContext,
    name: str,
    step_key: str,
    invoke: _AsyncFunction[..., Any],
    codec: Codec[Any],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> object:
    args_json = codec.dump_arguments(args, kwargs)
    started_at = datetime.now(UTC)
    token = _inside_step.set(True)
    try:
        result_json = codec.dump_result(await invoke(*args, **kwargs))
    except Exception as error:
        details = describe_error(error)
        failed = _attempt(step_key, name, args_json, started_at, error_json=json.dumps(details))
        await context.storage.record_attempt(context.lease, failed)
        raise StepFailedError(
            step_key, details["type"], details["message"], details["traceback"]
        ) from error
    finally:
        _inside_step.reset(token)
    succeeded = _attempt(step_key, name, args_json, started_at, result_json=result_json)
    await context.storage.record_attempt(context.lease, succeeded)
    return codec.load_result(result_json)


def _attempt(
    step_key: str,
    name: str,
    args_json: str,
    started_at: datetime,
    *,
    result_json: str | None = None,
    error_json: str | None = None,
) -> StepAttempt:
    return StepAttempt(
        step_key=step_key,
        name=name,
        attempt=1,
        status=AttemptStatus.OK if error_json is None else AttemptStatus.ERROR,
        args_hash=hash_arguments(args_json),
        args_json=args_json,
        result_json=result_json,
        error_json=error_json,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
