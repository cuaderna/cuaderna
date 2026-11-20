import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ._audit import AuditEntry, StepDecision, audit_entries, decisions_under
from ._chain import verified_digest
from ._cost import RunCost, cost_for_budget, repriced_cost, run_cost
from ._errors import (
    ApprovalNotPendingError,
    ApprovalRequiredError,
    BudgetExceededError,
    CostUnknownError,
    InvalidDefinitionError,
    LeaseLostError,
    PriceTableError,
    ReplayDivergedError,
    RunFailedError,
    RunHeldError,
    RunMismatchError,
    RunNotAvailableError,
    RunNotFoundError,
    StepMismatchError,
)
from ._policy import Policy
from ._prices import PriceTable
from ._recording import Budget, RecordingContext
from ._records import (
    ApprovalAnswer,
    ApprovalRequest,
    Lease,
    RunRecord,
    RunStatus,
    StepAttempt,
)
from ._replay import ReplayingContext
from ._run import RunDefinition, active_run
from ._runfile import Redact, dump_run_file, load_run_file
from ._serde import describe_error
from .storage import Storage
from .storage._sqlite import SqliteStorage

_LONGEST_CLAIM_WAIT_SECONDS = 1.0


class Engine:
    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: float = 60.0,
        claim_timeout_seconds: float | None = None,
        prices: PriceTable | None = None,
        actor: str | None = None,
    ) -> None:
        self._storage: Storage = SqliteStorage(path)
        self._prices = prices
        self._actor = actor
        self._owner = uuid4().hex
        self._lease_seconds = lease_seconds
        self._claim_timeout_seconds = claim_timeout_seconds
        self._giving_back: set[asyncio.Future[None]] = set()

    async def execute[**P, R](
        self, run_id: str, run: RunDefinition[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        if run.budget_usd is not None and self._prices is None:
            raise InvalidDefinitionError(f"{run.name}: a budget needs an engine with a price table")
        input_json = run.codec.dump_arguments(args, kwargs)
        give_up_at = None
        if self._claim_timeout_seconds is not None:
            give_up_at = time.monotonic() + self._claim_timeout_seconds
        lease = None
        while lease is None:
            record = await self._storage.create_run(run_id, run.name, input_json, run.code_version)
            if (record.name, record.input_json) != (run.name, input_json):
                raise RunMismatchError(
                    f"run {run_id!r} was started as {record.name}({record.input_json}), "
                    f"not as {run.name}({input_json})"
                )
            if record.status is RunStatus.FAILED:
                raise RunFailedError(f"run {run_id!r} already failed: {record.error_json}")
            if record.output_json is not None:
                return run.codec.load_result(record.output_json)
            if record.status is RunStatus.IMPORTED_UNFINISHED:
                raise RunNotAvailableError(
                    f"run {run_id!r} was imported unfinished: it can be read and replayed, "
                    "not resumed"
                )
            if record.code_version != run.code_version:
                raise RunMismatchError(
                    f"run {run_id!r} was started with code version {record.code_version!r} "
                    f"and cannot be resumed with {run.code_version!r}"
                )
            lease = await self._claim(run_id)
            if lease is None:
                pause = min(self._lease_seconds / 3, _LONGEST_CLAIM_WAIT_SECONDS)
                if give_up_at is not None:
                    remaining = give_up_at - time.monotonic()
                    if remaining <= 0:
                        raise RunHeldError(
                            f"run {run_id!r} is still held by another worker "
                            f"after {self._claim_timeout_seconds} seconds"
                        )
                    pause = min(pause, remaining)
                await asyncio.sleep(pause)
        heartbeat = asyncio.create_task(self._keep_lease(lease))
        try:
            return await self._drive(run, lease, *args, **kwargs)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run_record(self, run_id: str) -> RunRecord:
        record = await self._storage.get_run(run_id)
        if record is None:
            raise RunNotFoundError(f"run {run_id!r} is not in the journal")
        return record

    async def runs(self, *, limit: int = 100) -> list[RunRecord]:
        return await self._storage.list_runs(limit)

    async def steps(self, run_id: str) -> list[StepAttempt]:
        await self.run_record(run_id)
        return await self._storage.load_attempts(run_id)

    async def cost(self, run_id: str) -> RunCost:
        return run_cost(await self.steps(run_id))

    async def pending(self, run_id: str | None = None) -> list[ApprovalRequest]:
        if run_id is not None:
            await self.run_record(run_id)
        approvals = await self._storage.list_approvals(run_id)
        return [approval.request for approval in approvals if approval.answer is None]

    async def approve(self, request: ApprovalRequest, *, by: str, note: str | None = None) -> None:
        await self._answer(request, True, by, note)

    async def reject(self, request: ApprovalRequest, *, by: str, note: str | None = None) -> None:
        await self._answer(request, False, by, note)

    async def _answer(
        self, request: ApprovalRequest, approved: bool, by: str, note: str | None
    ) -> None:
        if not by:
            raise InvalidDefinitionError("an answer needs the name of who gives it")
        if request not in await self.pending(request.run_id):
            raise ApprovalNotPendingError(
                f"the request for step {request.step_key!r} of run {request.run_id!r} "
                "is no longer pending"
            )
        answer = ApprovalAnswer(approved, by, note, datetime.now(UTC))
        await self._storage.answer_approval(
            request.run_id, request.step_key, request.args_hash, answer
        )

    async def reprice(self, run_id: str, *, provider: str, model: str) -> RunCost:
        if self._prices is None:
            raise PriceTableError("repricing a run needs an engine with a price table")
        return repriced_cost(await self.steps(run_id), self._prices, provider=provider, model=model)

    async def audit(self, run_id: str) -> list[AuditEntry]:
        return audit_entries(await self.steps(run_id))

    async def verify(self, run_id: str) -> str:
        record = await self.run_record(run_id)
        return verified_digest(run_id, record, await self._storage.load_attempts(run_id))

    async def evaluate(self, run_id: str, policy: Policy) -> list[StepDecision]:
        return decisions_under(await self.steps(run_id), policy)

    async def replay[R](self, run_id: str, run: RunDefinition[..., R], /) -> R:
        record = await self.run_record(run_id)
        if record.name != run.name:
            raise RunMismatchError(
                f"run {run_id!r} was recorded as {record.name}, not as {run.name}"
            )
        try:
            arguments = run.codec.load_arguments(record.input_json)
        except ValueError as error:
            raise RunMismatchError(
                f"the recorded input of run {run_id!r} does not fit {run.name} now: {error}"
            ) from error
        attempts = await self._storage.load_attempts(run_id)
        context = ReplayingContext(run_id, {attempt.step_key: attempt for attempt in attempts})
        try:
            with active_run(context):
                output = await run.function(*arguments.args, **arguments.kwargs)
        except Exception as error:
            swallowed = context.first_divergence
            if swallowed is None or swallowed is error:
                raise
            raise swallowed from error
        if context.first_divergence is not None:
            raise context.first_divergence
        never_replayed = context.never_replayed()
        if never_replayed:
            raise ReplayDivergedError(
                f"the replay of run {run_id!r} never asked for the recorded steps "
                f"{', '.join(never_replayed)}"
            )
        output_json = run.codec.dump_result(output)
        if record.status is RunStatus.FAILED:
            raise ReplayDivergedError(
                f"the replay of run {run_id!r} returned {output_json}, "
                f"but the run was recorded as failed: {record.error_json}"
            )
        if record.output_json is not None and json.loads(output_json) != json.loads(
            record.output_json
        ):
            raise ReplayDivergedError(
                f"the replay of run {run_id!r} returned {output_json}, "
                f"not the recorded {record.output_json}"
            )
        return run.codec.load_result(output_json)

    async def export_run(
        self, run_id: str, path: str | Path, *, redact: Redact | None = None
    ) -> None:
        record = await self.run_record(run_id)
        attempts = await self._storage.load_attempts(run_id)
        text = dump_run_file(record, attempts, redact)
        await asyncio.to_thread(Path(path).write_text, text, encoding="utf-8", newline="\n")

    async def import_run(self, path: str | Path, *, as_run_id: str | None = None) -> str:
        text = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        record, attempts = load_run_file(text, as_run_id)
        await self._storage.import_run(record, attempts)
        return record.run_id

    async def _claim(self, run_id: str) -> Lease | None:
        claim = asyncio.ensure_future(
            self._storage.claim_run(run_id, self._owner, self._lease_seconds)
        )
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError:
            giving_back = asyncio.ensure_future(self._give_back(claim))
            self._giving_back.add(giving_back)
            giving_back.add_done_callback(self._giving_back.discard)
            await asyncio.shield(giving_back)
            raise

    async def _give_back(self, claim: asyncio.Future[Lease | None]) -> None:
        [lease] = await asyncio.gather(claim, return_exceptions=True)
        if isinstance(lease, Lease):
            await self._storage.release_run(lease)

    async def _drive[**P, R](
        self, run: RunDefinition[P, R], lease: Lease, /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        try:
            attempts = await self._storage.load_attempts(lease.run_id)
            approvals = await self._storage.list_approvals(lease.run_id)
        except BaseException:
            await self._storage.release_run(lease)
            raise
        recorded = {attempt.step_key: attempt for attempt in attempts}
        answers = {
            (approval.request.step_key, approval.request.args_hash): approval.answer
            for approval in approvals
            if approval.answer is not None
        }
        budget = None
        if run.budget_usd is not None and self._prices is not None:
            claimed = cost_for_budget(attempts, self._prices)
            unknown = next((cost for cost in claimed.attempts if cost.usd is None), None)
            budget = Budget(lease.run_id, run.budget_usd, claimed.total_usd, unknown)
        context = RecordingContext(
            self._storage,
            lease,
            recorded,
            self._prices,
            budget,
            run.policy,
            self._actor,
            answers=answers,
        )
        try:
            with active_run(context):
                output = await run.function(*args, **kwargs)
            output_json = run.codec.dump_result(output)
        except LeaseLostError:
            raise
        except (StepMismatchError, BudgetExceededError, CostUnknownError, ApprovalRequiredError):
            await self._storage.release_run(lease)
            raise
        except Exception as error:
            if context.stopped is not None:
                await self._storage.release_run(lease)
                raise context.stopped from error
            await self._storage.fail_run(lease, json.dumps(describe_error(error)))
            raise
        except BaseException:
            await self._storage.release_run(lease)
            raise
        if context.stopped is not None:
            await self._storage.release_run(lease)
            raise context.stopped
        await self._storage.complete_run(lease, output_json)
        return run.codec.load_result(output_json)

    async def _keep_lease(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(self._lease_seconds / 3)
            await self._storage.renew_lease(lease, self._lease_seconds)

    async def close(self) -> None:
        await asyncio.gather(*self._giving_back, return_exceptions=True)
        await self._storage.close()
