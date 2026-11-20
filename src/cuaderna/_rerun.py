import dataclasses
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from ._authorization import REUSE_KEY
from ._errors import ReplayDivergedError
from ._recording import RecordingContext
from ._records import StepAttempt
from ._run import StepDefinition, StepKeys
from ._serde import hash_arguments
from ._step import annotate_as_library


@dataclass(slots=True)
class RerunningContext:
    recording: RecordingContext
    source_run_id: str
    source_attempts: dict[str, StepAttempt]
    reuse: frozenset[str]
    diverged: ReplayDivergedError | None = None

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
        if self.diverged is not None:
            raise self.diverged
        to_execute = self._definition_to_execute(definition, step_key, args, kwargs)
        return await self.recording.execute_step(to_execute, step_key, args, kwargs)

    def _definition_to_execute(
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
        self.diverged = ReplayDivergedError(
            f"the rerun {self.recording.lease.run_id!r} of run {self.source_run_id!r} "
            f"asked for step {step_key!r} {what_happened}"
        )
        return self.diverged
