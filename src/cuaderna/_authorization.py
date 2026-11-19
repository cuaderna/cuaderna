import json

from pydantic import JsonValue

from ._errors import PermissionDeniedError, StepFailedError
from ._policy import Decision, Policy
from ._records import ApprovalAnswer, StepAttempt
from ._serde import ErrorDetails
from ._step import step_failed

AUTHORIZATION_KEY = "cuaderna.authorization"


def authorization_annotation(
    decision: Decision,
    policy: Policy,
    actor: str | None,
    verdict: str,
    reason: str,
    answer: ApprovalAnswer | None = None,
) -> dict[str, JsonValue]:
    approval: dict[str, JsonValue] | None = None
    if answer is not None:
        approval = {"by": answer.by, "at": answer.at.isoformat(), "note": answer.note}
    return {
        "decision": verdict,
        "effect": decision.effect,
        "reason": reason,
        "policy": policy.id,
        "policy_hash": policy.content_hash,
        "rules": list(decision.rules),
        "actor": actor,
        "approval": approval,
    }


def with_authorization(
    annotations: dict[str, JsonValue], authorization: dict[str, JsonValue] | None
) -> dict[str, JsonValue]:
    kept = {key: value for key, value in annotations.items() if key != AUTHORIZATION_KEY}
    if authorization is None:
        return kept
    return {**kept, AUTHORIZATION_KEY: authorization}


def denial_message(step_key: str, decision: Decision) -> str:
    rules = ", ".join(decision.rules)
    if decision.reason == "no_rule":
        why = "no rule names this step"
    elif decision.reason == "unevaluable":
        why = f"{rules} could not be evaluated against its arguments"
    else:
        why = f"denied by {rules}"
    return f"step {step_key!r} is not allowed: {why}"


def recorded_failure(step_key: str, attempt: StepAttempt, details: ErrorDetails) -> StepFailedError:
    annotations: dict[str, JsonValue] = json.loads(attempt.annotations_json or "{}")
    authorization = annotations.get(AUTHORIZATION_KEY)
    if not isinstance(authorization, dict) or authorization.get("decision") != "deny":
        return step_failed(step_key, details)
    rules = authorization.get("rules")
    named = tuple(str(rule) for rule in rules) if isinstance(rules, list) else ()
    reason = str(authorization.get("reason"))
    return PermissionDeniedError(step_key, details["message"], reason, named)
