import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

import cuaderna
from cuaderna import (
    ApprovalRequiredError,
    Engine,
    Policy,
    RunFailedError,
    RunMismatchError,
    StepMismatchError,
)
from cuaderna._serde import canonical_json, hash_arguments

SECRET = "sk-FAKE-SECRET-0123456789"
OTHER_SECRET = "sk-OTHER-FAKE-9876543210"

calls: list[int] = []
keys_in_use = [SECRET]


class Stop(BaseException):
    pass


@pytest.fixture(autouse=True)
def fresh_calls() -> Iterator[None]:
    calls.clear()
    keys_in_use[:] = [SECRET]
    yield


@pytest.fixture
async def actor_engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path, actor="alice")
    yield engine
    await engine.close()


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def use_key(key: str) -> str:
    calls.append(1)
    cuaderna.annotate({"seen": key})
    if len(calls) == 1:
        raise ConnectionError(f"rejected {key}")
    return f"token for {key}"


@cuaderna.run
async def deploy(key: str) -> str:
    return await use_key(key)


@cuaderna.run
async def deploy_then_stop() -> str:
    await use_key(keys_in_use[0])
    raise Stop


@cuaderna.run
async def fail_with(key: str) -> None:
    raise RuntimeError(key)


def allow_or_ask(effect: str) -> Policy:
    rule: dict[str, object] = {"id": "rule", "effect": effect, "steps": ["use_key"], "when": {}}
    return Policy({"format": "cuaderna.policy/1", "id": "p", "rules": [rule]})


def scrub(path: str, value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return "[REDACTED]" if SECRET in value else value
    if isinstance(value, list):
        return [scrub(path, item) for item in value]
    if isinstance(value, dict):
        return {name: scrub(path, item) for name, item in value.items()}
    return value


async def test_the_repr_of_a_record_does_not_show_what_was_recorded(
    actor_engine: Engine,
) -> None:
    await actor_engine.execute("done", deploy, SECRET)
    with pytest.raises(RuntimeError):
        await actor_engine.execute("failed", fail_with, SECRET)
    with pytest.raises(ApprovalRequiredError):
        await actor_engine.execute("asked", deploy.with_policy(allow_or_ask("ask")), SECRET)

    records = [
        await actor_engine.run_record("done"),
        await actor_engine.run_record("failed"),
        *await actor_engine.steps("done"),
        *await actor_engine.audit("done"),
        *await actor_engine.evaluate("done", allow_or_ask("allow")),
        *await actor_engine.pending(),
    ]
    shown = [repr(record) for record in records]

    assert len(shown) == 8
    assert [text for text in shown if SECRET in text] == []
    assert "done" in shown[0]


async def test_a_run_that_started_with_another_input_is_named_by_hashes(engine: Engine) -> None:
    await engine.execute("run", deploy, SECRET)

    with pytest.raises(RunMismatchError) as raised:
        await engine.execute("run", deploy, OTHER_SECRET)

    message = str(raised.value)
    assert SECRET not in message
    assert OTHER_SECRET not in message
    assert hash_arguments(canonical_json({"key": SECRET})) in message
    assert hash_arguments(canonical_json({"key": OTHER_SECRET})) in message


async def test_a_step_with_other_arguments_is_named_by_hashes(engine: Engine) -> None:
    with pytest.raises(Stop):
        await engine.execute("run", deploy_then_stop)
    keys_in_use[0] = OTHER_SECRET

    with pytest.raises(StepMismatchError) as raised:
        await engine.execute("run", deploy_then_stop)

    message = str(raised.value)
    [attempt, *_] = await engine.steps("run")
    assert SECRET not in message
    assert OTHER_SECRET not in message
    assert "use_key#1" in message
    assert attempt.args_hash in message
    assert hash_arguments(canonical_json({"key": OTHER_SECRET})) in message


async def test_a_failed_run_names_the_error_type_and_not_the_recorded_error(
    engine: Engine,
) -> None:
    with pytest.raises(RuntimeError):
        await engine.execute("run", fail_with, SECRET)

    with pytest.raises(RunFailedError) as raised:
        await engine.execute("run", fail_with, SECRET)

    assert SECRET not in str(raised.value)
    assert "RuntimeError" in str(raised.value)
    assert SECRET in str((await engine.run_record("run")).error_json)


async def test_a_redacted_export_has_no_secret_and_no_hash_to_guess_it_from(
    engine: Engine, tmp_path: Path
) -> None:
    await engine.execute("run", deploy, SECRET)
    await engine.export_run("run", tmp_path / "plain.json")
    await engine.export_run("run", tmp_path / "redacted.json", redact=scrub)

    plain = json.loads((tmp_path / "plain.json").read_text(encoding="utf-8"))
    text = (tmp_path / "redacted.json").read_text(encoding="utf-8")
    redacted = json.loads(text)

    assert SECRET in json.dumps(plain)
    assert all(attempt["chain_hash"] is not None for attempt in plain["attempts"])
    assert SECRET not in text
    assert len(redacted["attempts"]) == 2
    assert all(attempt["chain_hash"] is None for attempt in redacted["attempts"])
