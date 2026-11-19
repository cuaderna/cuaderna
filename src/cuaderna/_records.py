from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    name: str
    input_json: str
    code_version: str | None
    status: RunStatus
    output_json: str | None
    error_json: str | None
    created_at: datetime
    finished_at: datetime | None
