import hashlib
import json
from datetime import UTC, datetime

from pydantic import JsonValue

from ._errors import JournalTamperedError
from ._records import RunRecord, StepAttempt
from ._serde import canonical_json


def first_link(name: str, input_json: str) -> str:
    return _digest(canonical_json({"name": name, "input": json.loads(input_json)}))


def next_link(previous: str, attempt: StepAttempt) -> str:
    recorded = {
        "step_key": attempt.step_key,
        "name": attempt.name,
        "attempt": attempt.attempt,
        "status": attempt.status.value,
        "args": json.loads(attempt.args_json),
        "result": _value(attempt.result_json),
        "error": _value(attempt.error_json),
        "started_at": _moment_text(attempt.started_at),
        "finished_at": _moment_text(attempt.finished_at),
        "lease_token": attempt.lease_token,
        "annotations": _value(attempt.annotations_json),
    }
    return _digest(previous + canonical_json(recorded))


def verified_head(run_id: str, record: RunRecord, attempts: list[StepAttempt]) -> str:
    head = first_link(record.name, record.input_json)
    for attempt in attempts:
        if attempt.chain_hash is None:
            raise _tampered(
                run_id,
                attempt,
                "has no chain hash: it was recorded before chains existed or the hash was removed",
            )
        head = next_link(head, attempt)
        if attempt.chain_hash != head:
            raise _tampered(run_id, attempt, "does not match its chain hash")
    return head


def _tampered(run_id: str, attempt: StepAttempt, problem: str) -> JournalTamperedError:
    message = f"attempt {attempt.attempt} of step {attempt.step_key!r} in run {run_id!r} {problem}"
    return JournalTamperedError(message, run_id, attempt.step_key, attempt.attempt)


def _value(stored_json: str | None) -> JsonValue:
    if stored_json is None:
        return None
    return json.loads(stored_json)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _moment_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
