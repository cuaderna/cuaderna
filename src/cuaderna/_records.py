from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    IMPORTED_UNFINISHED = "imported_unfinished"


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
    lease_token: int | None = None
    annotations_json: str | None = None


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: str
    owner: str
    token: int


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    run_id: str
    step_key: str
    name: str
    args_hash: str
    args_json: str
    policy_id: str
    policy_hash: str
    rules: tuple[str, ...]
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalAnswer:
    approved: bool
    by: str
    note: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class Approval:
    request: ApprovalRequest
    answer: ApprovalAnswer | None
