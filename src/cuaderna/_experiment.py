import json
from dataclasses import dataclass

from pydantic import JsonValue

from ._audit import audit_entries
from ._authorization import FAULT_KEY
from ._errors import RunNotAvailableError
from ._records import RunRecord, StepAttempt


@dataclass(frozen=True, slots=True)
class Experiment:
    run_id: str
    source_run_id: str
    plan: str | None
    plan_hash: str | None
    fault: str | None
    fired: tuple[str, ...]
    reused: int
    executed: int
    diverged_at: str | None


def experiment_of(record: RunRecord, attempts: list[StepAttempt]) -> Experiment:
    marker = record.experiment
    if marker is None:
        raise RunNotAvailableError(f"run {record.run_id!r} is not an experiment")
    fired: list[str] = []
    reused = 0
    executed = 0
    diverged_at: str | None = None
    for attempt, entry in zip(attempts, audit_entries(attempts, False), strict=True):
        if entry.injected is not None and attempt.step_key not in fired:
            fired.append(attempt.step_key)
        if entry.reused:
            reused += 1
        elif _ran_its_function(attempt, entry.outcome, entry.injected is not None):
            executed += 1
            if diverged_at is None and attempt.name in marker.reuse:
                diverged_at = attempt.step_key
    return Experiment(
        run_id=record.run_id,
        source_run_id=marker.source_run_id,
        plan=marker.plan_id,
        plan_hash=marker.plan_hash,
        fault=marker.fault_id,
        fired=tuple(fired),
        reused=reused,
        executed=executed,
        diverged_at=diverged_at,
    )


def _ran_its_function(attempt: StepAttempt, outcome: str, injected: bool) -> bool:
    if outcome == "denied":
        return False
    if not injected:
        return True
    return _fault_executed(attempt)


def _fault_executed(attempt: StepAttempt) -> bool:
    annotations: dict[str, JsonValue] = json.loads(attempt.annotations_json or "{}")
    fault = annotations.get(FAULT_KEY)
    return isinstance(fault, dict) and fault.get("executed") is True
