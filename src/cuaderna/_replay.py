from dataclasses import dataclass, field

from ._authorization import recorded_failure
from ._errors import ReplayDivergedError
from ._records import StepAttempt
from ._run import StepDefinition, StepKeys
from ._serde import hash_arguments, load_error


@dataclass(slots=True)
class ReplayingContext:
    run_id: str
    recorded: dict[str, StepAttempt]
    step_keys: StepKeys = field(default_factory=StepKeys)
    replayed: set[str] = field(default_factory=set[str])
    first_divergence: ReplayDivergedError | None = None

    async def execute_step(
        self,
        definition: StepDefinition,
        step_key: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        args_json = definition.codec.dump_arguments(args, kwargs)
        recorded = self.recorded.get(step_key)
        if recorded is None:
            raise self._diverged(f"asked for step {step_key!r} with {args_json}, never recorded")
        if recorded.args_hash != hash_arguments(args_json):
            raise self._diverged(
                f"asked for step {step_key!r} with other arguments: "
                f"{recorded.args_json} recorded, {args_json} now"
            )
        self.replayed.add(step_key)
        if recorded.result_json is not None:
            return definition.codec.load_result(recorded.result_json)
        if recorded.error_json is not None:
            details = load_error(recorded.error_json)
            if details["terminal"] or recorded.attempt >= definition.max_attempts:
                raise recorded_failure(step_key, recorded, details)
        raise self._diverged(
            f"asked for step {step_key!r}, which was recorded up to attempt "
            f"{recorded.attempt} and never reached a final outcome"
        )

    def never_replayed(self) -> list[str]:
        return [step_key for step_key in self.recorded if step_key not in self.replayed]

    def _diverged(self, what_happened: str) -> ReplayDivergedError:
        divergence = ReplayDivergedError(f"the replay of run {self.run_id!r} {what_happened}")
        if self.first_divergence is None:
            self.first_divergence = divergence
        return divergence
