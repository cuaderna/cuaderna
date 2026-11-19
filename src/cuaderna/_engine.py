import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from ._errors import (
    LeaseLostError,
    RunFailedError,
    RunHeldError,
    RunMismatchError,
    RunNotFoundError,
    StepMismatchError,
)
from ._recording import RecordingContext
from ._records import Lease, RunRecord, RunStatus, StepAttempt
from ._run import RunDefinition, active_run
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
    ) -> None:
        self._storage: Storage = SqliteStorage(path)
        self._owner = uuid4().hex
        self._lease_seconds = lease_seconds
        self._claim_timeout_seconds = claim_timeout_seconds
        self._giving_back: set[asyncio.Future[None]] = set()

    async def execute[**P, R](
        self, run_id: str, run: RunDefinition[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
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
        except BaseException:
            await self._storage.release_run(lease)
            raise
        recorded = {attempt.step_key: attempt for attempt in attempts}
        try:
            with active_run(RecordingContext(self._storage, lease, recorded)):
                output = await run.function(*args, **kwargs)
            output_json = run.codec.dump_result(output)
        except LeaseLostError:
            raise
        except StepMismatchError:
            await self._storage.release_run(lease)
            raise
        except Exception as error:
            await self._storage.fail_run(lease, json.dumps(describe_error(error)))
            raise
        except BaseException:
            await self._storage.release_run(lease)
            raise
        await self._storage.complete_run(lease, output_json)
        return run.codec.load_result(output_json)

    async def _keep_lease(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(self._lease_seconds / 3)
            await self._storage.renew_lease(lease, self._lease_seconds)

    async def close(self) -> None:
        await asyncio.gather(*self._giving_back, return_exceptions=True)
        await self._storage.close()
