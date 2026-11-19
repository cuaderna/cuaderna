import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn

from pydantic import JsonValue

from ._cost import AttemptCost, run_cost, with_cost
from ._errors import BudgetExceededError, CostUnknownError, StepMismatchError
from ._prices import PriceTable
from ._records import AttemptStatus, Lease, StepAttempt
from ._run import StepDefinition, StepKeys
from ._serde import describe_error, hash_arguments, load_error
from ._step import ActiveStep, active_step, retry_delay, step_failed
from .storage import Storage

StopError = BudgetExceededError | CostUnknownError


@dataclass(slots=True)
class Budget:
    run_id: str
    limit_usd: Decimal
    spent_usd: Decimal
    unknown: AttemptCost | None = None

    def stop(self) -> BudgetExceededError | CostUnknownError | None:
        if self.unknown is not None:
            return CostUnknownError(
                self.run_id, self.unknown.step_key, self.unknown.attempt, self.unknown.reason
            )
        if self.spent_usd >= self.limit_usd:
            return BudgetExceededError(self.run_id, self.limit_usd, self.spent_usd)
        return None

    def add(self, attempt: StepAttempt) -> None:
        cost = run_cost([attempt])
        self.spent_usd += cost.total_usd
        if cost.unknown_attempts and self.unknown is None:
            self.unknown = cost.attempts[0]


@dataclass(slots=True)
class RecordingContext:
    storage: Storage
    lease: Lease
    recorded: dict[str, StepAttempt]
    prices: PriceTable | None = None
    budget: Budget | None = None
    step_keys: StepKeys = field(default_factory=StepKeys)
    stopped: StopError | None = None

    async def execute_step(
        self,
        definition: StepDefinition,
        step_key: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        if self.stopped is not None:
            raise self.stopped
        args_json = definition.codec.dump_arguments(args, kwargs)
        first_attempt = 1
        recorded = self.recorded.get(step_key)
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
                    raise step_failed(step_key, details)
            first_attempt = recorded.attempt + 1
        for attempt in range(first_attempt, definition.max_attempts + 1):
            if self.budget is not None:
                stop = self.budget.stop()
                if stop is not None:
                    self._stop(stop)
            started_at = datetime.now(UTC)
            running = ActiveStep(f"{self.lease.run_id}:{step_key}")
            token = active_step.set(running)
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
                    with_cost(running.annotations, self.prices),
                    error_json=json.dumps(details),
                )
                await self._record(failed)
                if details["terminal"] or attempt == definition.max_attempts:
                    raise step_failed(step_key, details) from error
                await asyncio.sleep(retry_delay(attempt, definition.backoff_seconds))
            else:
                succeeded = _attempt(
                    step_key,
                    definition.name,
                    attempt,
                    args_json,
                    started_at,
                    with_cost(running.annotations, self.prices),
                    result_json=result_json,
                )
                await self._record(succeeded)
                return definition.codec.load_result(result_json)
            finally:
                active_step.reset(token)
        raise StepMismatchError(f"step {step_key!r} has more recorded attempts than it allows now")

    def _stop(self, error: StopError) -> NoReturn:
        self.stopped = error
        raise error

    async def _record(self, attempt: StepAttempt) -> None:
        await self.storage.record_attempt(self.lease, attempt)
        if self.budget is not None:
            self.budget.add(attempt)


def _attempt(
    step_key: str,
    name: str,
    attempt: int,
    args_json: str,
    started_at: datetime,
    annotations: dict[str, JsonValue],
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
        annotations_json=json.dumps(annotations) if annotations else None,
    )
