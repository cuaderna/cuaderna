import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    AttemptStatus,
    Engine,
    PermissionDeniedError,
    Policy,
    RunStatus,
    StepFailedError,
)

KEY = "cuaderna.authorization"
STEP_NAMES = ["write_file", "explode", "forge", "flaky"]

bodies_that_ran: list[str] = []
refusals: list[Exception] = []
Lookalike = type("PermissionDeniedError", (Exception,), {})


def rule(rule_id: str, effect: str, when: dict[str, object] | None = None) -> dict[str, object]:
    return {"id": rule_id, "effect": effect, "steps": STEP_NAMES, "when": when or {}}


def policy_of(*rules: dict[str, object]) -> Policy:
    return Policy({"format": "cuaderna.policy/1", "id": "test-policy", "rules": list(rules)})


def authorization(
    decision: str, reason: str, rules: list[str], policy: Policy
) -> dict[str, object]:
    return {
        "decision": decision,
        "effect": decision,
        "reason": reason,
        "policy": "test-policy",
        "policy_hash": policy.content_hash,
        "rules": rules,
        "actor": "alice",
        "approval": None,
    }


ALLOW_ALL = policy_of(rule("allow-all", "allow"))


@pytest.fixture(autouse=True)
def forget_bodies() -> Iterator[None]:
    bodies_that_ran.clear()
    refusals.clear()
    yield


@pytest.fixture
async def actor_engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, actor="alice")
    yield engine
    await engine.close()


@cuaderna.step(name="write_file")
async def write_file() -> str:
    bodies_that_ran.append("write_file")
    return "written"


@cuaderna.step(name="explode")
async def explode() -> str:
    raise Lookalike("my own refusal")


@cuaderna.step(name="forge")
async def forge() -> str:
    cuaderna.annotate({KEY: "forged"})
    return "forged"


@cuaderna.step(name="flaky", max_attempts=2, backoff_seconds=60)
async def flaky() -> str:
    bodies_that_ran.append("flaky")
    raise ValueError("try again")


STEPS: dict[str, Callable[[], Awaitable[object]]] = {
    "write_file": write_file,
    "explode": explode,
    "forge": forge,
    "flaky": flaky,
    "now": cuaderna.now,
    "random": cuaderna.random,
    "new_id": cuaderna.new_id,
}


@cuaderna.run
async def call(step_names: list[str], catching: bool = False) -> str:
    for step_name in step_names:
        try:
            await STEPS[step_name]()
        except Exception as error:
            if not catching:
                raise
            refusals.append(error)
    return "went on"


async def annotations_of(engine: Engine) -> dict[str, dict[str, object]]:
    [attempt] = await engine.steps("run-1")
    return json.loads(attempt.annotations_json or "{}")


@pytest.mark.parametrize("step_name", ["write_file", "explode"])
async def test_an_allowed_step_records_its_authorization_even_when_it_fails(
    actor_engine: Engine, step_name: str
) -> None:
    policy = policy_of({**rule("edit-source", "allow"), "steps": [step_name]})
    with suppress(StepFailedError):
        await actor_engine.execute("run-1", call.with_policy(policy), [step_name])

    expected = authorization("allow", "rule", ["edit-source"], policy)
    assert (await annotations_of(actor_engine))[KEY] == expected


@pytest.mark.parametrize(
    ("rules", "reason", "named", "why"),
    [
        ([rule("never-ci", "deny")], "rule", ["never-ci"], "denied by never-ci"),
        ([rule("a", "deny"), rule("b", "deny")], "rule", ["a", "b"], "denied by a, b"),
        ([], "no_rule", [], "no rule names this step"),
        (
            [rule("edit-source", "allow", {"missing": {"equals": 1}})],
            "unevaluable",
            ["edit-source"],
            "edit-source could not be evaluated against its arguments",
        ),
    ],
)
async def test_a_denied_step_never_executes_and_is_recorded(
    actor_engine: Engine, rules: list[dict[str, object]], reason: str, named: list[str], why: str
) -> None:
    policy = policy_of(*rules)
    with pytest.raises(PermissionDeniedError) as raised:
        await actor_engine.execute("run-1", call.with_policy(policy), ["write_file"])

    [attempt] = await actor_engine.steps("run-1")
    message = f"step 'write_file#1' is not allowed: {why}"
    assert str(raised.value) == message
    assert (raised.value.reason, raised.value.rules) == (reason, tuple(named))
    assert bodies_that_ran == []
    assert attempt.status is AttemptStatus.ERROR
    assert json.loads(attempt.error_json or "") == {
        "type": "PermissionDeniedError",
        "message": message,
        "traceback": "",
        "terminal": True,
    }
    assert await annotations_of(actor_engine) == {KEY: authorization("deny", reason, named, policy)}
    assert (await actor_engine.run_record("run-1")).status is RunStatus.FAILED


async def test_a_caught_denial_completes_the_run_and_replays_as_the_same_error(
    engine: Engine,
) -> None:
    denying = policy_of(rule("never-ci", "deny"))

    output = await engine.execute("run-1", call.with_policy(denying), ["write_file"], True)
    await engine.replay("run-1", call)

    assert output == "went on"
    assert (await engine.run_record("run-1")).status is RunStatus.COMPLETED
    recorded, replayed = refusals
    assert isinstance(replayed, PermissionDeniedError)
    assert (str(replayed), replayed.rules) == (str(recorded), ("never-ci",))


async def test_resuming_after_a_denial_raises_it_from_the_journal(engine: Engine) -> None:
    denying = policy_of({**rule("never-ci", "deny"), "steps": ["write_file"]}, rule("ok", "allow"))
    steps = ["write_file", "flaky"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            engine.execute("run-1", call.with_policy(denying), steps, True), timeout=1
        )
    first = refusals.pop()

    await engine.execute("run-1", call.with_policy(ALLOW_ALL), steps, True)

    assert bodies_that_ran == ["flaky", "flaky"]
    assert isinstance(refusals[0], PermissionDeniedError)
    assert str(refusals[0]) == str(first)
    assert refusals[0].rules == ("never-ci",)


async def test_a_user_error_named_like_a_denial_stays_a_plain_step_failure(
    engine: Engine,
) -> None:
    with pytest.raises(StepFailedError) as raised:
        await engine.execute("run-1", call.with_policy(ALLOW_ALL), ["explode"])
    with pytest.raises(StepFailedError) as replayed:
        await engine.replay("run-1", call)

    assert type(raised.value) is type(replayed.value) is StepFailedError
    assert raised.value.error_type == "PermissionDeniedError"


@pytest.mark.parametrize(("policy", "recorded"), [(ALLOW_ALL, "allow"), (None, None)])
async def test_a_forged_authorization_is_replaced_under_a_policy_and_removed_without(
    engine: Engine, policy: Policy | None, recorded: str | None
) -> None:
    await engine.execute("run-1", call if policy is None else call.with_policy(policy), ["forge"])

    assert (await annotations_of(engine)).get(KEY, {}).get("decision") == recorded


async def test_library_steps_run_under_a_policy_that_names_nothing(engine: Engine) -> None:
    await engine.execute("run-1", call.with_policy(policy_of()), ["now", "random", "new_id"])

    attempts = await engine.steps("run-1")
    assert [attempt.status for attempt in attempts] == [AttemptStatus.OK] * 3
    assert all(attempt.annotations_json is None for attempt in attempts)


async def test_a_retry_after_a_resume_is_checked_on_its_own(engine: Engine) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            engine.execute("run-1", call.with_policy(ALLOW_ALL), ["flaky"]), timeout=1
        )

    with pytest.raises(PermissionDeniedError):
        await engine.execute("run-1", call.with_policy(policy_of()), ["flaky"])

    attempts = await engine.steps("run-1")
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert json.loads(attempts[1].error_json or "")["type"] == "PermissionDeniedError"
