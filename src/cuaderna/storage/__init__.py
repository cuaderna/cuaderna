from typing import Protocol

from cuaderna._records import RunRecord


class Storage(Protocol):
    async def create_run(
        self, run_id: str, name: str, input_json: str, code_version: str | None
    ) -> RunRecord: ...

    async def close(self) -> None: ...
