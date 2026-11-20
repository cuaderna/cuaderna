import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ._serde import canonical_json


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    IMPORTED_UNFINISHED = "imported_unfinished"


class AttemptStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ExperimentMarker:
    source_run_id: str
    reuse: tuple[str, ...]
    plan_id: str | None = None
    plan_hash: str | None = None
    fault_id: str | None = None

    def to_json(self) -> str:
        return canonical_json(
            {
                "source_run_id": self.source_run_id,
                "plan_id": self.plan_id,
                "plan_hash": self.plan_hash,
                "fault_id": self.fault_id,
                "reuse": list(self.reuse),
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "ExperimentMarker":
        document = json.loads(text)
        return cls(
            source_run_id=document["source_run_id"],
            reuse=tuple(document["reuse"]),
            plan_id=document["plan_id"],
            plan_hash=document["plan_hash"],
            fault_id=document["fault_id"],
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    name: str
    input_json: str = field(repr=False)
    code_version: str | None
    status: RunStatus
    output_json: str | None = field(repr=False)
    error_json: str | None = field(repr=False)
    created_at: datetime
    finished_at: datetime | None
    seal: str | None = None
    imported_at: datetime | None = None
    experiment: ExperimentMarker | None = None


@dataclass(frozen=True, slots=True)
class StepAttempt:
    step_key: str
    name: str
    attempt: int
    status: AttemptStatus
    args_hash: str
    args_json: str = field(repr=False)
    result_json: str | None = field(repr=False)
    error_json: str | None = field(repr=False)
    started_at: datetime
    finished_at: datetime
    lease_token: int | None = None
    annotations_json: str | None = field(default=None, repr=False)
    chain_hash: str | None = None


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
    args_json: str = field(repr=False)
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
