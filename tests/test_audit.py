import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

import cuaderna
from cuaderna import (
    AttemptStatus,
    Engine,
    PermissionDeniedError,
    Policy,
    RunNotFoundError,
    StepAttempt,
    StepFailedError,
)
from cuaderna._audit import audit_entries

KEY = "cuaderna.authorization"
MOMENT = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def policy_of(*rules: dict[str, object]) -> Policy:
    return Policy({"format": "cuaderna.policy/1", "id": "audit-policy", "rules": list(rules)})


def rule(rule_id: str, effect: str, steps: list[str]) -> dict[str, object]:
    return {"id": rule_id, "effect": effect, "steps": steps}


@cuaderna.step(name="read_file", max_attempts=1)
async def read_file(path: str) -> str:
    return path


@cuaderna.step(name="write_file", max_attempts=1)
async def write_file(path: str) -> str:
    raise ValueError("disk full")


@cuaderna.run
async def work() -> list[str]:
    stamp = await cuaderna.new_id()
    return [stamp, await read_file("a.txt")]


@cuaderna.run
async def broken() -> str:
    return await write_file("b.txt")


@cuaderna.run
async def refused() -> str:
    try:
        return await write_file("c.txt")
    except PermissionDeniedError:
        return "went on"


def attempt_with(annotations: dict[str, JsonValue] | None) -> StepAttempt:
    return StepAttempt(
        step_key="read_file#1",
        name="read_file",
        attempt=1,
        status=AttemptStatus.OK,
        args_hash="hash",
        args_json='{"path": "a.txt"}',
        result_json='"a.txt"',
        error_json=None,
        started_at=MOMENT,
        finished_at=MOMENT,
        lease_token=3,
        annotations_json=None if annotations is None else json.dumps(annotations),
    )


async def test_allowed_denied_and_failed_attempts_read_as_entries(journal_path: Path) -> None:
    engine = Engine(journal_path, actor="ci")
    try:
        allow_read = policy_of(rule("look", "allow", ["read_file", "write_file"]))
        await engine.execute("allowed", work.with_policy(allow_read))
        with pytest.raises(StepFailedError):
            await engine.execute("failed", broken.with_policy(allow_read))
        await engine.execute("denied", refused.with_policy(policy_of()))
        [_, read_entry] = await engine.audit("allowed")
        [failed_entry] = await engine.audit("failed")
        [denied_entry] = await engine.audit("denied")
    finally:
        await engine.close()

    assert (read_entry.outcome, read_entry.decision, read_entry.effect) == ("ok", "allow", "allow")
    assert (read_entry.policy, read_entry.policy_hash) == ("audit-policy", allow_read.content_hash)
    assert (read_entry.rules, read_entry.actor, read_entry.args) == (
        ("look",),
        "ci",
        {"path": "a.txt"},
    )
    assert (failed_entry.outcome, failed_entry.decision) == ("error", "allow")
    assert (denied_entry.outcome, denied_entry.reason) == ("denied", "no_rule")


def test_an_attempt_without_a_policy_has_no_decision() -> None:
    [entry] = audit_entries([attempt_with({"other": 1})])

    assert (entry.decision, entry.policy, entry.rules, entry.actor) == (None, None, (), None)


def test_an_approval_is_read_with_who_when_and_why() -> None:
    approval: dict[str, JsonValue] = {"by": "maxi", "at": MOMENT.isoformat(), "note": "diff ok"}
    recorded: dict[str, JsonValue] = {"decision": "allow", "effect": "ask", "approval": approval}
    [entry] = audit_entries([attempt_with({KEY: recorded})])

    assert (entry.effect, entry.approved_by, entry.approved_at) == ("ask", "maxi", MOMENT)
    assert entry.approval_note == "diff ok"


@pytest.mark.parametrize("unreadable", ["forged", 7, ["allow"], None])
def test_an_authorization_that_is_not_an_object_is_ignored(unreadable: JsonValue) -> None:
    [entry] = audit_entries([attempt_with({KEY: unreadable})])

    assert (entry.decision, entry.rules, entry.outcome) == (None, (), "ok")


def test_a_field_of_the_wrong_type_is_treated_as_absent() -> None:
    recorded: dict[str, JsonValue] = {
        "decision": "maybe",
        "effect": 4,
        "rules": ["a", 2],
        "actor": ["x"],
        "approval": {"by": 1, "at": "yesterday", "note": None},
    }
    [entry] = audit_entries([attempt_with({KEY: recorded})])

    assert (entry.decision, entry.effect, entry.actor, entry.rules) == (None, None, None, ("a",))
    assert (entry.approved_by, entry.approved_at, entry.approval_note) == (None, None, None)


async def test_an_imported_run_is_audited_without_a_policy(
    journal_path: Path, tmp_path: Path
) -> None:
    source = Engine(journal_path, actor="ci")
    other = Engine(tmp_path / "other.db")
    try:
        policy = policy_of(rule("look", "allow", ["read_file"]))
        await source.execute("run-1", work.with_policy(policy))
        await source.export_run("run-1", tmp_path / "run.json")
        run_id = await other.import_run(tmp_path / "run.json")
        entries = await other.audit(run_id)
    finally:
        await source.close()
        await other.close()

    assert [(entry.name, entry.decision, entry.actor) for entry in entries] == [
        ("cuaderna.new_id", None, None),
        ("read_file", "allow", "ci"),
    ]


async def test_evaluate_tries_a_policy_on_a_recorded_run(engine: Engine) -> None:
    await engine.execute("run-1", work)
    stricter = policy_of(rule("no-reads", "deny", ["read_file"]))

    [decision] = await engine.evaluate("run-1", stricter)

    assert (decision.step_key, decision.name, decision.args) == (
        "read_file#1",
        "read_file",
        {"path": "a.txt"},
    )
    assert (decision.decision.effect, decision.decision.rules) == ("deny", ("no-reads",))


async def test_an_unknown_run_raises_what_cost_raises(engine: Engine) -> None:
    for call in (engine.cost("nope"), engine.audit("nope"), engine.evaluate("nope", policy_of())):
        with pytest.raises(RunNotFoundError):
            await call
