import asyncio
import json
from pathlib import Path
from uuid import uuid4

from ._errors import (
    LeaseLostError,
    RunMismatchError,
    RunNotAvailableError,
    StepMismatchError,
)
from ._records import Lease, RunStatus
from ._run import RunContext, RunDefinition, active_run
from ._serde import describe_error
from .storage import Storage
from .storage._sqlite import SqliteStorage

_LONGEST_CLAIM_WAIT_SECONDS = 1.0


class Engine:
    def __init__(self, path: str | Path, *, lease_seconds: float = 60.0) -> None:
        self._storage: Storage = SqliteStorage(path)
        self._owner = uuid4().hex
        self._lease_seconds = lease_seconds

    async def execute[**P, R](
        self, run_id: str, run: RunDefinition[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        input_json = run.codec.dump_arguments(args, kwargs)
        lease = None
        while lease is None:
            record = await self._storage.create_run(run_id, run.name, input_json, None)
            if (record.name, record.input_json) != (run.name, input_json):
                raise RunMismatchError(
                    f"run {run_id!r} was started as {record.name}({record.input_json}), "
                    f"not as {run.name}({input_json})"
                )
            if record.status is RunStatus.FAILED:
                raise RunNotAvailableError(f"run {run_id!r} already failed: {record.error_json}")
            if record.output_json is not None:
                return run.codec.load_result(record.output_json)
            lease = await self._storage.claim_run(run_id, self._owner, self._lease_seconds)
            if lease is None:
                await asyncio.sleep(min(self._lease_seconds / 3, _LONGEST_CLAIM_WAIT_SECONDS))
        heartbeat = asyncio.create_task(self._keep_lease(lease))
        try:
            return await self._drive(run, lease, *args, **kwargs)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _drive[**P, R](
        self, run: RunDefinition[P, R], lease: Lease, /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        attempts = await self._storage.load_attempts(lease.run_id)
        recorded = {attempt.step_key: attempt for attempt in attempts}
        try:
            with active_run(RunContext(self._storage, lease, recorded)):
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
        await self._storage.close()
