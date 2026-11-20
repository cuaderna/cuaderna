import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn

from pydantic import JsonValue

from ._authorization import (
    authorization_annotation,
    denial_message,
    recorded_failure,
    with_authorization,
)
from ._cost import AttemptCost, run_cost, with_cost
from ._errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    CostUnknownError,
    PermissionDeniedError,
    StepLimitExceededError,
    StepMismatchError,
)
from ._policy import Decision, Policy
from ._prices import PriceTable
from ._records import ApprovalAnswer, ApprovalRequest, AttemptStatus, Lease, StepAttempt
from ._run import StepDefinition, StepKeys
from ._serde import describe_error, hash_arguments, load_error
from ._step import ActiveStep, active_step, retry_delay, step_failed
from .storage import Storage

StopError = BudgetExceededError | CostUnknownError | ApprovalRequiredError | StepLimitExceededError


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
    max_steps: int | None = None
    attempts_recorded: int = 0
    policy: Policy | None = None
    actor: str | None = None
    step_keys: StepKeys = field(default_factory=StepKeys)
    stopped: StopError | None = None
    answers: dict[tuple[str, str], ApprovalAnswer] = field(
        default_factory=dict[tuple[str, str], ApprovalAnswer]
    )

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
        as_recorded = definition.codec.copy_arguments(args, kwargs)
        recorded = self.recorded.get(step_key)
        if recorded is not None:
            if recorded.args_hash != hash_arguments(args_json):
                raise StepMismatchError(
                    f"step {step_key!r} was recorded with arguments {recorded.args_hash}, "
                    f"now called with {hash_arguments(args_json)}"
                )
            if recorded.result_json is not None:
                return definition.codec.load_result(recorded.result_json)
            if recorded.error_json is not None:
                details = load_error(recorded.error_json)
                if details["terminal"] or recorded.attempt >= definition.max_attempts:
                    raise recorded_failure(step_key, recorded, details)
            first_attempt = recorded.attempt + 1
        for attempt in range(first_attempt, definition.max_attempts + 1):
            if self.max_steps is not None and self.attempts_recorded >= self.max_steps:
                self._stop(
                    StepLimitExceededError(
                        self.lease.run_id, self.max_steps, self.attempts_recorded
                    )
                )
            authorization = await self._authorize(definition, step_key, attempt, args_json)
            if self.budget is not None:
                stop = self.budget.stop()
                if stop is not None:
                    self._stop(stop)
            started_at = datetime.now(UTC)
            running = ActiveStep(f"{self.lease.run_id}:{step_key}")
            token = active_step.set(running)
            try:
                fresh = definition.codec.copy_arguments(as_recorded.args, as_recorded.kwargs)
                result_json = definition.codec.dump_result(
                    await definition.invoke(*fresh.args, **fresh.kwargs)
                )
            except Exception as error:
                details = describe_error(error)
                failed = _attempt(
                    step_key,
                    definition.name,
                    attempt,
                    args_json,
                    started_at,
                    with_authorization(with_cost(running.annotations, self.prices), authorization),
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
                    with_authorization(with_cost(running.annotations, self.prices), authorization),
                    result_json=result_json,
                )
                await self._record(succeeded)
                return definition.codec.load_result(result_json)
            finally:
                active_step.reset(token)
        raise StepMismatchError(f"step {step_key!r} has more recorded attempts than it allows now")

    async def _authorize(
        self, definition: StepDefinition, step_key: str, attempt: int, args_json: str
    ) -> dict[str, JsonValue] | None:
        if self.policy is None or definition.is_library:
            return None
        decision = self.policy.decide(definition.name, args_json)
        if decision.effect == "ask":
            return await self._ask(self.policy, definition, step_key, attempt, args_json, decision)
        if decision.effect == "allow":
            return authorization_annotation(
                decision, self.policy, self.actor, "allow", decision.reason
            )
        authorization = authorization_annotation(
            decision, self.policy, self.actor, "deny", decision.reason
        )
        denied = PermissionDeniedError(
            step_key, denial_message(step_key, decision), decision.reason, decision.rules
        )
        return await self._deny(definition, step_key, attempt, args_json, denied, authorization)

    async def _ask(
        self,
        policy: Policy,
        definition: StepDefinition,
        step_key: str,
        attempt: int,
        args_json: str,
        decision: Decision,
    ) -> dict[str, JsonValue]:
        args_hash = hash_arguments(args_json)
        answer = self.answers.get((step_key, args_hash))
        if answer is None:
            request = ApprovalRequest(
                self.lease.run_id,
                step_key,
                definition.name,
                args_hash,
                args_json,
                policy.id,
                policy.content_hash,
                decision.rules,
                datetime.now(UTC),
            )
            await self.storage.request_approval(self.lease, request)
            self._stop(ApprovalRequiredError(self.lease.run_id, step_key, definition.name))
        if answer.approved:
            return authorization_annotation(decision, policy, self.actor, "allow", "rule", answer)
        authorization = authorization_annotation(
            decision, policy, self.actor, "deny", "rejected", answer
        )
        denied = PermissionDeniedError(
            step_key,
            f"step {step_key!r} is not allowed: rejected by {answer.by}",
            "rejected",
            decision.rules,
        )
        return await self._deny(definition, step_key, attempt, args_json, denied, authorization)

    async def _deny(
        self,
        definition: StepDefinition,
        step_key: str,
        attempt: int,
        args_json: str,
        denied: PermissionDeniedError,
        authorization: dict[str, JsonValue],
    ) -> NoReturn:
        started_at = datetime.now(UTC)
        details = {
            "type": denied.error_type,
            "message": denied.message,
            "traceback": "",
            "terminal": True,
        }
        annotations = with_authorization({}, authorization)
        error_json = json.dumps(details)
        await self._record(
            _attempt(
                step_key,
                definition.name,
                attempt,
                args_json,
                started_at,
                annotations,
                error_json=error_json,
            )
        )
        raise denied

    def _stop(self, error: StopError) -> NoReturn:
        self.stopped = error
        raise error

    async def _record(self, attempt: StepAttempt) -> None:
        await self.storage.record_attempt(self.lease, attempt)
        self.attempts_recorded += 1
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
