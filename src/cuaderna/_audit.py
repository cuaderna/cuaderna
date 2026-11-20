import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from ._authorization import AUTHORIZATION_KEY
from ._library import LIBRARY_STEPS
from ._policy import Decision, Policy
from ._records import AttemptStatus, StepAttempt


@dataclass(frozen=True, slots=True)
class AuditEntry:
    at: datetime
    step_key: str
    name: str
    attempt: int
    args: JsonValue = field(repr=False)
    lease_token: int | None
    outcome: Literal["ok", "error", "denied"]
    decision: Literal["allow", "deny"] | None
    effect: Literal["allow", "ask", "deny"] | None
    reason: str | None
    policy: str | None
    policy_hash: str | None
    rules: tuple[str, ...]
    actor: str | None
    approved_by: str | None
    approved_at: datetime | None
    approval_note: str | None
    imported: bool


@dataclass(frozen=True, slots=True)
class StepDecision:
    step_key: str
    name: str
    args: JsonValue = field(repr=False)
    decision: Decision


def audit_entries(attempts: list[StepAttempt], imported: bool) -> list[AuditEntry]:
    return [_entry(attempt, imported) for attempt in attempts]


def decisions_under(attempts: list[StepAttempt], policy: Policy) -> list[StepDecision]:
    decisions: dict[str, StepDecision] = {}
    for attempt in attempts:
        if attempt.name in LIBRARY_STEPS or attempt.step_key in decisions:
            continue
        decisions[attempt.step_key] = StepDecision(
            step_key=attempt.step_key,
            name=attempt.name,
            args=json.loads(attempt.args_json),
            decision=policy.decide(attempt.name, attempt.args_json),
        )
    return list(decisions.values())


def _entry(attempt: StepAttempt, imported: bool) -> AuditEntry:
    authorization = _authorization(attempt)
    approval = _object(authorization.get("approval"))
    decision = _decision(authorization.get("decision"))
    return AuditEntry(
        at=attempt.started_at,
        step_key=attempt.step_key,
        name=attempt.name,
        attempt=attempt.attempt,
        args=json.loads(attempt.args_json),
        lease_token=attempt.lease_token,
        outcome=_outcome(attempt, decision),
        decision=decision,
        effect=_effect(authorization.get("effect")),
        reason=_text(authorization.get("reason")),
        policy=_text(authorization.get("policy")),
        policy_hash=_text(authorization.get("policy_hash")),
        rules=_rules(authorization.get("rules")),
        actor=_text(authorization.get("actor")),
        approved_by=_text(approval.get("by")),
        approved_at=_moment(approval.get("at")),
        approval_note=_text(approval.get("note")),
        imported=imported,
    )


def _authorization(attempt: StepAttempt) -> dict[str, JsonValue]:
    if attempt.annotations_json is None:
        return {}
    annotations: dict[str, JsonValue] = json.loads(attempt.annotations_json)
    return _object(annotations.get(AUTHORIZATION_KEY))


def _outcome(
    attempt: StepAttempt, decision: Literal["allow", "deny"] | None
) -> Literal["ok", "error", "denied"]:
    if decision == "deny":
        return "denied"
    if attempt.status is AttemptStatus.OK:
        return "ok"
    return "error"


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    return {}


def _text(value: JsonValue | None) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _decision(value: JsonValue | None) -> Literal["allow", "deny"] | None:
    if value == "allow":
        return "allow"
    if value == "deny":
        return "deny"
    return None


def _effect(value: JsonValue | None) -> Literal["allow", "ask", "deny"] | None:
    if value == "ask":
        return "ask"
    return _decision(value)


def _rules(value: JsonValue | None) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(rule for rule in value if isinstance(rule, str))


def _moment(value: JsonValue | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
