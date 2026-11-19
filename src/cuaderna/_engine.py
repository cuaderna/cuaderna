import json
from pathlib import Path
from uuid import uuid4

from ._errors import LeaseLostError, RunNotAvailableError
from ._run import RunContext, RunDefinition, active_run
from ._serde import describe_error
from .storage import Storage
from .storage._sqlite import SqliteStorage


class Engine:
    def __init__(self, path: str | Path, *, lease_seconds: float = 60.0) -> None:
        self._storage: Storage = SqliteStorage(path)
        self._owner = uuid4().hex
        self._lease_seconds = lease_seconds

    async def execute[**P, R](
        self, run_id: str, run: RunDefinition[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        input_json = run.codec.dump_arguments(args, kwargs)
        await self._storage.create_run(run_id, run.name, input_json, None)
        lease = await self._storage.claim_run(run_id, self._owner, self._lease_seconds)
        if lease is None:
            raise RunNotAvailableError(
                f"run {run_id!r} has already finished or is held by another worker"
            )
        try:
            with active_run(RunContext(self._storage, lease)):
                output = await run.function(*args, **kwargs)
            output_json = run.codec.dump_result(output)
        except LeaseLostError:
            raise
        except Exception as error:
            await self._storage.fail_run(lease, json.dumps(describe_error(error)))
            raise
        except BaseException:
            await self._storage.release_run(lease)
            raise
        await self._storage.complete_run(lease, output_json)
        return run.codec.load_result(output_json)

    async def close(self) -> None:
        await self._storage.close()
