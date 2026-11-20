import json
from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, Field, JsonValue, model_validator

from ._errors import RunFileError
from ._records import AttemptStatus, RunRecord, RunStatus, StepAttempt
from ._serde import canonical_json, hash_arguments

type Redact = Callable[[str, JsonValue], JsonValue]

_SQLITE_MAX = 2**63 - 1


def _fits_in_utc(moment: datetime) -> datetime:
    try:
        moment.astimezone(UTC)
    except OverflowError as error:
        raise ValueError(f"{moment} is out of range once converted to UTC") from error
    return moment


_Moment = Annotated[datetime, AfterValidator(_fits_in_utc)]


class _RunSection(BaseModel):
    run_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input: JsonValue
    code_version: str | None
    status: RunStatus
    output: JsonValue
    error: JsonValue
    created_at: _Moment
    finished_at: _Moment | None
    seal: str | None = None


class _AttemptSection(BaseModel):
    step_key: str = Field(min_length=1)
    name: str
    attempt: int = Field(ge=1, le=_SQLITE_MAX)
    status: AttemptStatus
    args: JsonValue
    result: JsonValue
    error: JsonValue
    started_at: _Moment
    finished_at: _Moment
    lease_token: int | None = Field(ge=-_SQLITE_MAX - 1, le=_SQLITE_MAX)
    annotations: JsonValue
    chain_hash: str | None = None


class _RunFile(BaseModel):
    format: Literal["cuaderna.run/1"]
    run: _RunSection
    attempts: list[_AttemptSection]

    @model_validator(mode="after")
    def _attempts_are_unique(self) -> Self:
        identities = {(section.step_key, section.attempt) for section in self.attempts}
        if len(identities) != len(self.attempts):
            raise ValueError("two attempts have the same step key and attempt number")
        return self


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
            seal=record.seal if redact is None else None,
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
                chain_hash=attempt.chain_hash,
            )
            for attempt in attempts
        ],
    )
    return json.dumps(document.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def load_run_file(text: str, as_run_id: str | None) -> tuple[RunRecord, list[StepAttempt]]:
    try:
        document = _RunFile.model_validate(
            json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float)
        )
    except (ValueError, RecursionError) as error:
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
        seal=run.seal,
    )
    return record, [_attempt(section) for section in document.attempts]


def _refuse_constant(constant: str) -> float:
    raise ValueError(f"{constant} is not JSON")


def _finite_float(text: str) -> float:
    number = float(text)
    if not isfinite(number):
        raise ValueError(f"{text} is not a finite number")
    return number


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
        chain_hash=section.chain_hash,
    )


def _text(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
