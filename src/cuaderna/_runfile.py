import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, JsonValue, ValidationError

from ._errors import RunFileError
from ._records import AttemptStatus, RunRecord, RunStatus, StepAttempt
from ._serde import canonical_json, hash_arguments

type Redact = Callable[[str, JsonValue], JsonValue]


class _RunSection(BaseModel):
    run_id: str
    name: str
    input: JsonValue
    code_version: str | None
    status: RunStatus
    output: JsonValue
    error: JsonValue
    created_at: datetime
    finished_at: datetime | None


class _AttemptSection(BaseModel):
    step_key: str
    name: str
    attempt: int
    status: AttemptStatus
    args: JsonValue
    result: JsonValue
    error: JsonValue
    started_at: datetime
    finished_at: datetime
    lease_token: int | None
    annotations: JsonValue


class _RunFile(BaseModel):
    format: Literal["cuaderna.run/1"]
    run: _RunSection
    attempts: list[_AttemptSection]


def dump_run_file(record: RunRecord, attempts: list[StepAttempt], redact: Redact | None) -> str:
    def stored(path: str, text: str | None) -> JsonValue:
        if text is None:
            return None
        value: JsonValue = json.loads(text)
        if redact is None:
            return value
        return redact(path, value)

    document = _RunFile(
        format="cuaderna.run/1",
        run=_RunSection(
            run_id=record.run_id,
            name=record.name,
            input=stored("input", record.input_json),
            code_version=record.code_version,
            status=record.status,
            output=stored("output", record.output_json),
            error=stored("error", record.error_json),
            created_at=record.created_at,
            finished_at=record.finished_at,
        ),
        attempts=[
            _AttemptSection(
                step_key=attempt.step_key,
                name=attempt.name,
                attempt=attempt.attempt,
                status=attempt.status,
                args=stored(f"{attempt.step_key}.args", attempt.args_json),
                result=stored(f"{attempt.step_key}.result", attempt.result_json),
                error=stored(f"{attempt.step_key}.error", attempt.error_json),
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                lease_token=attempt.lease_token,
                annotations=stored(f"{attempt.step_key}.annotations", attempt.annotations_json),
            )
            for attempt in attempts
        ],
    )
    return json.dumps(document.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def load_run_file(text: str, as_run_id: str | None) -> tuple[RunRecord, list[StepAttempt]]:
    try:
        document = _RunFile.model_validate_json(text)
    except ValidationError as error:
        raise RunFileError(f"not a run file this library can read: {error}") from error
    run = document.run
    status = run.status
    if status is RunStatus.RUNNING:
        status = RunStatus.IMPORTED_UNFINISHED
    record = RunRecord(
        run_id=as_run_id or run.run_id,
        name=run.name,
        input_json=canonical_json(run.input),
        code_version=run.code_version,
        status=status,
        output_json=_text(run.output) if status is RunStatus.COMPLETED else None,
        error_json=_text(run.error) if status is RunStatus.FAILED else None,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )
    return record, [_attempt(section) for section in document.attempts]


def _attempt(section: _AttemptSection) -> StepAttempt:
    args_json = canonical_json(section.args)
    succeeded = section.status is AttemptStatus.OK
    return StepAttempt(
        step_key=section.step_key,
        name=section.name,
        attempt=section.attempt,
        status=section.status,
        args_hash=hash_arguments(args_json),
        args_json=args_json,
        result_json=_text(section.result) if succeeded else None,
        error_json=None if succeeded else _text(section.error),
        started_at=section.started_at,
        finished_at=section.finished_at,
        lease_token=section.lease_token,
        annotations_json=None if section.annotations is None else _text(section.annotations),
    )


def _text(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
