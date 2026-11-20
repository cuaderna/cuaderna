import dataclasses
import json
from collections.abc import Callable, Coroutine
from dataclasses import InitVar, dataclass, field
from typing import Any, cast

from pydantic import JsonValue

from ._authorization import FAULT_KEY, REUSE_KEY
from ._errors import FaultPlanError, ReplayDivergedError, TerminalError
from ._faults import Fault
from ._recording import RecordingContext
from ._records import StepAttempt
from ._run import StepDefinition, StepKeys
from ._serde import hash_arguments
from ._step import annotate_as_library


@dataclass(slots=True)
class RerunningContext:
    recording: RecordingContext
    recorded_attempts: InitVar[list[StepAttempt]]
    source_run_id: str
    source_attempts: dict[str, StepAttempt]
    reuse: frozenset[str]
    fault: Fault | None = None
    fired: bool = field(init=False)
    failure: ReplayDivergedError | FaultPlanError | None = None

    def __post_init__(self, recorded_attempts: list[StepAttempt]) -> None:
        self.fired = any(
            FAULT_KEY in json.loads(attempt.annotations_json or "{}")
            for attempt in recorded_attempts
        )

    @property
    def step_keys(self) -> StepKeys:
        return self.recording.step_keys

    async def execute_step(
        self,
        definition: StepDefinition,
        step_key: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        if self.failure is not None:
            raise self.failure
        to_execute = self._definition_to_execute(definition, step_key, args, kwargs)
        return await self.recording.execute_step(to_execute, step_key, args, kwargs)

    def _definition_to_execute(
        self,
        definition: StepDefinition,
        step_key: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> StepDefinition:
        replaying = self._replaying_definition(definition, step_key, args, kwargs)
        if self.fault is None or not self.fault.targets(step_key):
            return replaying
        served = replaying.invoke is not definition.invoke
        faulting = self._faulting(self.fault, replaying, step_key, served)
        return dataclasses.replace(replaying, invoke=faulting)

    def _faulting(
        self, fault: Fault, underlying: StepDefinition, step_key: str, served: bool
    ) -> Callable[..., Coroutine[Any, Any, object]]:
        own = self.recording.recorded.get(step_key)
        attempt = 0 if own is None else own.attempt

        async def faulted(*args: object, **kwargs: object) -> object:
            nonlocal attempt
            attempt += 1
            try:
                return await self._apply(fault, underlying, attempt, served, args, kwargs)
            except FaultPlanError as error:
                self.failure = error
                raise

        return faulted

    async def _apply(
        self,
        fault: Fault,
        underlying: StepDefinition,
        attempt: int,
        served: bool,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        if fault.kind == "raise":
            if not fault.fails_attempt(attempt):
                return await underlying.invoke(*args, **kwargs)
            self._mark_fired(fault, executed=False)
            base = TerminalError if fault.terminal else Exception
            error_class = cast(type[Exception], type(fault.error_type, (base,), {}))
            raise error_class(fault.error_message)
        if fault.kind == "return":
            self._mark_fired(fault, executed=False)
            return self._loaded(fault, underlying, fault.value)
        result = await underlying.invoke(*args, **kwargs)
        appended = fault.appended_to(json.loads(underlying.codec.dump_result(result)))
        self._mark_fired(fault, executed=not served)
        return self._loaded(fault, underlying, appended)

    def _mark_fired(self, fault: Fault, *, executed: bool) -> None:
        self.fired = True
        annotate_as_library(
            {
                FAULT_KEY: {
                    "plan_id": fault.plan_id,
                    "plan_hash": fault.plan_hash,
                    "fault_id": fault.id,
                    "kind": fault.kind,
                    "executed": executed,
                }
            }
        )

    def _loaded(self, fault: Fault, definition: StepDefinition, value: JsonValue) -> object:
        try:
            return definition.codec.load_result(json.dumps(value))
        except ValueError as error:
            raise FaultPlanError(
                f"fault {fault.id!r} does not give a value the step returns: {error}"
            ) from error

    def _replaying_definition(
        self,
        definition: StepDefinition,
        step_key: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> StepDefinition:
        if definition.name not in self.reuse and not definition.is_library:
            return definition
        own = self.recording.recorded.get(step_key)
        if own is not None and own.result_json is not None:
            return definition
        args_json = definition.codec.dump_arguments(args, kwargs)
        source = self.source_attempts.get(step_key)
        if (
            source is not None
            and source.args_hash == hash_arguments(args_json)
            and source.result_json is not None
        ):
            return dataclasses.replace(
                definition, invoke=self._serving(definition, source, source.result_json)
            )
        if definition.is_library:
            return definition
        if self.fired:
            return definition
        raise self._diverge(step_key, source, hash_arguments(args_json))

    def _serving(
        self, definition: StepDefinition, source: StepAttempt, result_json: str
    ) -> Callable[..., Coroutine[Any, Any, object]]:
        async def serve(*_args: object, **_kwargs: object) -> object:
            annotate_as_library(
                {
                    REUSE_KEY: {
                        "run_id": self.source_run_id,
                        "step_key": source.step_key,
                        "attempt": source.attempt,
                    }
                }
            )
            return definition.codec.load_result(result_json)

        return serve

    def _diverge(
        self, step_key: str, source: StepAttempt | None, args_hash: str
    ) -> ReplayDivergedError:
        if source is None:
            what_happened = "that the source run never recorded"
        elif source.args_hash != args_hash:
            what_happened = f"with other arguments: {source.args_hash} recorded, {args_hash} now"
        else:
            what_happened = "that the source run recorded as failed"
        self.failure = ReplayDivergedError(
            f"the rerun {self.recording.lease.run_id!r} of run {self.source_run_id!r} "
            f"asked for step {step_key!r} {what_happened}"
        )
        return self.failure
