from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

import cuaderna
from cuaderna import (
    Engine,
    Experiment,
    Fault,
    FaultPlan,
    PermissionDeniedError,
    Policy,
    RunNotAvailableError,
    RunNotFoundError,
    RunStatus,
)


class World:
    def __init__(self) -> None:
        self.report = "report"


world = World()

PRICES = cuaderna.PriceTable(
    {
        "format": "cuaderna.prices/1",
        "id": "2026-11-19",
        "currency": "USD",
        "models": [
            {"provider": "anthropic", "names": ["small"], "rates": {"input": "1", "output": "5"}}
        ],
    }
)


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@pytest.fixture
async def engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path)
    yield engine
    await engine.close()


@pytest.fixture
async def other_engine(tmp_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(tmp_path / "other.db")
    yield engine
    await engine.close()


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def fetch() -> str:
    return world.report


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def summarize(text: str) -> str:
    return f"summary of {text}"


@cuaderna.step
async def publish(summary: str) -> str:
    return summary


@cuaderna.run
async def pipeline() -> str:
    text = await fetch()
    summary = await summarize(text)
    return await publish(summary)


@cuaderna.step
async def ask() -> str:
    cuaderna.annotate(
        {
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "small",
            "gen_ai.usage.input_tokens": 1_000,
            "gen_ai.usage.output_tokens": 200,
        }
    )
    return "answer"


@cuaderna.run
async def asking() -> str:
    return await ask()


@cuaderna.run
async def twice() -> str:
    first = await summarize("a")
    second = await summarize("b")
    return first + second


def fault_at(step_key: str, body: dict[str, object]) -> Fault:
    document = {
        "format": "cuaderna.faults/1",
        "id": "plan",
        "faults": [{"id": "f1", "at": step_key, **body}],
    }
    return FaultPlan(document).faults[0]


def timeout_once(step_key: str) -> Fault:
    return fault_at(step_key, {"raise": {"type": "Timeout", "message": "slow", "attempts": 1}})


def appending(step_key: str) -> Fault:
    return fault_at(step_key, {"append": {"to": "", "text": "!"}})


def denying_publish() -> Policy:
    rest: dict[str, object] = {
        "id": "rest",
        "effect": "allow",
        "steps": ["fetch", "summarize"],
        "when": {},
    }
    deny: dict[str, object] = {"id": "deny", "effect": "deny", "steps": ["publish"], "when": {}}
    return Policy({"format": "cuaderna.policy/1", "id": "p", "rules": [rest, deny]})


async def experiment_of_rerun(
    engine: Engine, reuse: list[str], fault: Fault | None = None
) -> Experiment:
    await engine.execute("source", pipeline)
    await engine.rerun("source", pipeline, as_run_id="experiment", reuse=reuse, fault=fault)
    return await engine.experiment("experiment")


async def test_a_control_reports_what_it_served_and_what_it_executed(engine: Engine) -> None:
    found = await experiment_of_rerun(engine, ["summarize"])

    assert found == Experiment(
        run_id="experiment",
        source_run_id="source",
        plan=None,
        plan_hash=None,
        fault=None,
        fired=(),
        reused=1,
        executed=2,
        diverged_at=None,
        status=RunStatus.COMPLETED,
        error_type=None,
        cost_usd=Decimal(0),
        unknown_cost_attempts=0,
    )


async def test_a_fault_that_costs_nothing_does_not_diverge(engine: Engine) -> None:
    fault = timeout_once("summarize#1")

    found = await experiment_of_rerun(engine, ["summarize"], fault)

    assert (found.plan, found.plan_hash, found.fault) == ("plan", fault.plan_hash, "f1")
    assert (found.fired, found.reused, found.executed, found.diverged_at) == (
        ("summarize#1",),
        1,
        2,
        None,
    )


async def test_a_reused_step_that_gets_new_arguments_is_where_the_run_diverged(
    engine: Engine,
) -> None:
    await engine.execute("source", pipeline)
    world.report = "other report"
    fault = timeout_once("fetch#1")

    await engine.rerun("source", pipeline, as_run_id="experiment", reuse=["summarize"], fault=fault)

    found = await engine.experiment("experiment")
    assert (found.fired, found.diverged_at) == (("fetch#1",), "summarize#1")
    assert (found.reused, found.executed) == (0, 3)


@pytest.mark.parametrize(("reuse", "reused", "executed"), [(["summarize"], 1, 2), ([], 0, 3)])
async def test_an_append_fault_counts_as_reused_when_served_and_executed_otherwise(
    engine: Engine, reuse: list[str], reused: int, executed: int
) -> None:
    found = await experiment_of_rerun(engine, reuse, appending("summarize#1"))

    assert (found.fired, found.reused, found.executed) == (("summarize#1",), reused, executed)


async def test_a_fault_that_never_fires_leaves_nothing_fired(engine: Engine) -> None:
    found = await experiment_of_rerun(engine, ["summarize"], timeout_once("summarize#9"))

    assert (found.fault, found.fired, found.diverged_at) == ("f1", (), None)


async def test_a_denied_attempt_is_not_counted_as_executed(engine: Engine) -> None:
    await engine.execute("source", pipeline)

    with pytest.raises(PermissionDeniedError):
        await engine.rerun(
            "source",
            pipeline.with_policy(denying_publish()),
            as_run_id="experiment",
            reuse=["summarize"],
        )

    found = await engine.experiment("experiment")
    assert (found.reused, found.executed) == (1, 1)


async def test_a_fault_at_a_step_name_lists_each_call_once_in_order(engine: Engine) -> None:
    await engine.execute("source", twice)
    fault = fault_at("summarize", {"raise": {"type": "Timeout", "message": "slow", "attempts": 1}})

    await engine.rerun("source", twice, as_run_id="experiment", reuse=["summarize"], fault=fault)

    assert (await engine.experiment("experiment")).fired == ("summarize#1", "summarize#2")


async def test_an_experiment_says_what_its_model_calls_cost(journal_path: Path) -> None:
    engine = Engine(journal_path, prices=PRICES)
    try:
        await engine.execute("source", asking)
        await engine.rerun("source", asking, as_run_id="experiment", reuse=[])
        found = await engine.experiment("experiment")
    finally:
        await engine.close()

    assert (found.status, found.error_type) == (RunStatus.COMPLETED, None)
    assert (found.cost_usd, found.unknown_cost_attempts) == (Decimal("0.002"), 0)


async def test_an_experiment_that_failed_says_how(engine: Engine) -> None:
    await engine.execute("source", pipeline)
    fault = fault_at(
        "summarize#1", {"raise": {"type": "Timeout", "message": "slow", "attempts": 2}}
    )

    with pytest.raises(cuaderna.CuadernaError):
        await engine.rerun("source", pipeline, as_run_id="experiment", reuse=[], fault=fault)

    found = await engine.experiment("experiment")
    assert (found.status, found.error_type) == (RunStatus.FAILED, "StepFailedError")
    assert (found.cost_usd, found.unknown_cost_attempts) == (Decimal(0), 0)


async def test_only_an_experiment_can_be_read_as_one(engine: Engine) -> None:
    await engine.execute("source", pipeline)

    with pytest.raises(RunNotAvailableError, match="not an experiment"):
        await engine.experiment("source")
    with pytest.raises(RunNotFoundError):
        await engine.experiment("missing")


async def test_an_imported_experiment_gives_the_same_facts(
    engine: Engine, other_engine: Engine, tmp_path: Path
) -> None:
    found = await experiment_of_rerun(engine, ["summarize"], timeout_once("summarize#1"))
    await engine.export_run("experiment", tmp_path / "run.json")
    await other_engine.import_run(tmp_path / "run.json")

    assert await other_engine.experiment("experiment") == found


async def test_audit_marks_the_injected_and_the_served_attempts(engine: Engine) -> None:
    await experiment_of_rerun(engine, ["summarize"], timeout_once("summarize#1"))

    entries = [(e.step_key, e.injected, e.reused) for e in await engine.audit("experiment")]

    assert entries == [
        ("fetch#1", None, False),
        ("summarize#1", "f1", False),
        ("summarize#1", None, True),
        ("publish#1", None, False),
    ]


async def test_audit_of_an_ordinary_run_has_no_marks(engine: Engine) -> None:
    await engine.execute("source", pipeline)

    assert all((e.injected, e.reused) == (None, False) for e in await engine.audit("source"))
