import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    Engine,
    ExperimentMarker,
    Fault,
    FaultPlan,
    FaultPlanError,
    PermissionDeniedError,
    Policy,
    ReplayDivergedError,
    RunMismatchError,
    RunStatus,
    StepAttempt,
    StepFailedError,
)


class Crash(BaseException):
    pass


class World:
    def __init__(self) -> None:
        self.report = "report"
        self.fetches = 0
        self.summaries = 0
        self.published: list[str] = []
        self.crash_on_publish = False
        self.crash_on_fetch = False


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@pytest.fixture
async def engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path)
    yield engine
    await engine.close()


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def fetch() -> str:
    if world.crash_on_fetch:
        raise Crash
    world.fetches += 1
    return world.report


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def summarize(text: str) -> str:
    world.summaries += 1
    return f"summary of {text}"


@cuaderna.step
async def publish(summary: str) -> str:
    if world.crash_on_publish:
        raise Crash
    world.published.append(summary)
    return summary


@cuaderna.run
async def pipeline(swallow: bool) -> str:
    text = await fetch()
    try:
        summary = await summarize(text)
    except StepFailedError:
        if swallow:
            return "gave up"
        raise
    return await publish(summary)


def fault_at(step_key: str, body: dict[str, object]) -> Fault:
    document = {
        "format": "cuaderna.faults/1",
        "id": "plan",
        "faults": [{"id": "f1", "at": step_key, **body}],
    }
    return FaultPlan(document).faults[0]


def timeout(**options: object) -> Fault:
    return fault_at("summarize#1", {"raise": {"type": "Timeout", "message": "too slow", **options}})


def returning(value: object) -> Fault:
    return fault_at("summarize#1", {"return": value})


def appending(pointer: str) -> Fault:
    return fault_at("summarize#1", {"append": {"to": pointer, "text": " (unverified)"}})


async def inject(engine: Engine, fault: Fault, reuse: list[str], swallow: bool = False) -> str:
    await engine.execute("source", pipeline, swallow)
    return await engine.rerun("source", pipeline, as_run_id="experiment", reuse=reuse, fault=fault)


async def attempts_of(engine: Engine, step_key: str) -> list[StepAttempt]:
    return [a for a in await engine.steps("experiment") if a.step_key == step_key]


def annotations(attempt: StepAttempt) -> dict[str, object]:
    return json.loads(attempt.annotations_json or "{}")


def error_of(attempt: StepAttempt) -> dict[str, object]:
    return json.loads(attempt.error_json or "{}")


def fault_annotation(fault: Fault, *, executed: bool) -> dict[str, object]:
    return {
        "plan_id": "plan",
        "plan_hash": fault.plan_hash,
        "fault_id": "f1",
        "kind": fault.kind,
        "executed": executed,
    }


async def test_a_fault_fails_one_attempt_of_a_reused_step_and_the_next_is_served(
    engine: Engine,
) -> None:
    fault = timeout(attempts=1)

    output = await inject(engine, fault, ["summarize"])

    failed, served = await attempts_of(engine, "summarize#1")
    assert (error_of(failed)["type"], error_of(failed)["message"]) == ("Timeout", "too slow")
    assert annotations(failed) == {"cuaderna.fault": fault_annotation(fault, executed=False)}
    assert set(annotations(served)) == {"cuaderna.reuse"}
    assert (world.summaries, output) == (1, "summary of report")


@pytest.mark.parametrize(("terminal", "attempts_made"), [(False, 2), (True, 1)])
async def test_a_fault_without_a_limit_fails_the_step_like_a_real_failure(
    engine: Engine, terminal: bool, attempts_made: int
) -> None:
    fault = timeout(terminal=terminal)

    with pytest.raises(StepFailedError, match="Timeout"):
        await inject(engine, fault, ["summarize"])

    made = await attempts_of(engine, "summarize#1")
    assert len(made) == attempts_made
    assert all("cuaderna.fault" in annotations(attempt) for attempt in made)
    assert error_of(made[-1])["terminal"] is terminal
    assert world.summaries == 1


async def test_a_fault_on_a_step_that_is_not_reused_skips_its_function_on_the_failed_attempt(
    engine: Engine,
) -> None:
    fault = fault_at("fetch#1", {"raise": {"type": "Timeout", "message": "slow", "attempts": 1}})

    await inject(engine, fault, [])

    failed, retried = await attempts_of(engine, "fetch#1")
    assert annotations(failed) == {"cuaderna.fault": fault_annotation(fault, executed=False)}
    assert annotations(retried) == {}
    assert world.fetches == 2


async def test_a_return_fault_records_its_value_without_running_the_step(
    engine: Engine,
) -> None:
    fault = returning("forced")

    await inject(engine, fault, ["summarize"])

    [attempt] = await attempts_of(engine, "summarize#1")
    assert attempt.result_json == '"forced"'
    assert annotations(attempt) == {"cuaderna.fault": fault_annotation(fault, executed=False)}
    assert (world.summaries, world.published[-1]) == (1, "forced")


@pytest.mark.parametrize("swallow", [False, True])
async def test_a_return_fault_that_does_not_fit_the_return_type_fails_the_run(
    engine: Engine, swallow: bool
) -> None:
    with pytest.raises(FaultPlanError, match="f1"):
        await inject(engine, returning(5), ["summarize"], swallow)

    assert len(await attempts_of(engine, "summarize#1")) == 1
    assert (await engine.run_record("experiment")).status is RunStatus.FAILED


@pytest.mark.parametrize(
    ("reuse", "executed", "summaries"), [(["summarize"], False, 1), ([], True, 2)]
)
async def test_an_append_fault_adds_text_to_the_result_of_the_call(
    engine: Engine, reuse: list[str], executed: bool, summaries: int
) -> None:
    fault = appending("")

    await inject(engine, fault, reuse)

    [attempt] = await attempts_of(engine, "summarize#1")
    assert attempt.result_json == '"summary of report (unverified)"'
    assert annotations(attempt)["cuaderna.fault"] == fault_annotation(fault, executed=executed)
    assert ("cuaderna.reuse" in annotations(attempt)) is not executed
    assert world.summaries == summaries


async def test_an_append_fault_with_no_string_at_its_pointer_fails_the_run(
    engine: Engine,
) -> None:
    with pytest.raises(FaultPlanError, match="f1"):
        await inject(engine, appending("/missing"), ["summarize"])

    assert (await engine.run_record("experiment")).status is RunStatus.FAILED


async def test_a_reused_step_with_new_arguments_runs_for_real_only_after_the_fault_fired(
    engine: Engine,
) -> None:
    await engine.execute("source", pipeline, False)
    fires_first = fault_at("fetch#1", {"return": "other report"})

    await engine.rerun(
        "source", pipeline, as_run_id="experiment", reuse=["summarize"], fault=fires_first
    )

    assert world.summaries == 2
    world.report = "new report"
    fires_later = fault_at("publish#1", {"return": "unused"})
    with pytest.raises(ReplayDivergedError):
        await engine.rerun(
            "source", pipeline, as_run_id="late", reuse=["summarize"], fault=fires_later
        )


async def test_a_fault_whose_target_is_never_called_leaves_a_strict_rerun(
    engine: Engine,
) -> None:
    fault = fault_at("publish#2", {"return": "unused"})

    output = await inject(engine, fault, ["summarize"])

    assert output == "summary of report"
    assert all("cuaderna.fault" not in annotations(a) for a in await engine.steps("experiment"))
    assert (await engine.run_record("experiment")).status is RunStatus.COMPLETED


def policy_for_summaries(effect: str) -> Policy:
    allow: dict[str, object] = {
        "id": "rest",
        "effect": "allow",
        "steps": ["fetch", "publish"],
        "when": {},
    }
    summaries: dict[str, object] = {
        "id": "summaries",
        "effect": effect,
        "steps": ["summarize"],
        "when": {},
    }
    return Policy({"format": "cuaderna.policy/1", "id": "p", "rules": [allow, summaries]})


async def test_a_denied_call_is_not_faulted(engine: Engine) -> None:
    await engine.execute("source", pipeline.with_policy(policy_for_summaries("allow")), False)

    with pytest.raises(PermissionDeniedError):
        await engine.rerun(
            "source",
            pipeline.with_policy(policy_for_summaries("deny")),
            as_run_id="experiment",
            reuse=["summarize"],
            fault=timeout(attempts=1),
        )

    [denied] = await attempts_of(engine, "summarize#1")
    assert "cuaderna.fault" not in annotations(denied)


@pytest.mark.parametrize(
    ("fault", "summarize_attempts"), [(timeout(attempts=1), 2), (returning("forced"), 1)]
)
async def test_a_killed_experiment_resumes_without_injecting_twice(
    engine: Engine, fault: Fault, summarize_attempts: int
) -> None:
    await engine.execute("source", pipeline, False)
    world.crash_on_publish = True
    with pytest.raises(Crash):
        await engine.rerun(
            "source", pipeline, as_run_id="experiment", reuse=["summarize"], fault=fault
        )
    world.crash_on_publish = False

    await engine.rerun("source", pipeline, as_run_id="experiment", reuse=["summarize"], fault=fault)

    made = await attempts_of(engine, "summarize#1")
    assert len(made) == summarize_attempts
    assert sum("cuaderna.fault" in annotations(attempt) for attempt in made) == 1
    assert (await engine.run_record("experiment")).status is RunStatus.COMPLETED


async def test_the_marker_names_the_plan_and_another_fault_is_another_experiment(
    engine: Engine,
) -> None:
    fault = timeout(attempts=1)
    await inject(engine, fault, ["summarize"])

    marker = (await engine.run_record("experiment")).experiment
    assert marker == ExperimentMarker(
        "source",
        ("summarize",),
        plan_id="plan",
        plan_hash=fault.plan_hash,
        fault_id="f1",
    )
    with pytest.raises(RunMismatchError):
        await engine.rerun(
            "source",
            pipeline,
            as_run_id="experiment",
            reuse=["summarize"],
            fault=returning("forced"),
        )


async def test_an_experiment_killed_before_the_retry_of_an_injected_attempt_resumes_at_the_retry(
    engine: Engine,
) -> None:
    fault = fault_at("fetch#1", {"raise": {"type": "Timeout", "message": "slow", "attempts": 1}})
    await engine.execute("source", pipeline, False)
    world.crash_on_fetch = True
    with pytest.raises(Crash):
        await engine.rerun("source", pipeline, as_run_id="experiment", reuse=[], fault=fault)
    world.crash_on_fetch = False

    await engine.rerun("source", pipeline, as_run_id="experiment", reuse=[], fault=fault)

    failed, retried = await attempts_of(engine, "fetch#1")
    assert "cuaderna.fault" in annotations(failed)
    assert (retried.attempt, annotations(retried)) == (2, {})
