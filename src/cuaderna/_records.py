from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AttemptStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


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


@dataclass(frozen=True, slots=True)
class StepAttempt:
    step_key: str
    name: str
    attempt: int
    status: AttemptStatus
    args_hash: str
    args_json: str
    result_json: str | None
    error_json: str | None
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: str
    owner: str
    token: int
