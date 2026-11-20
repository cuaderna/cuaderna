from collections.abc import Sequence
from decimal import Decimal

import pytest

from cuaderna import (
    CuadernaError,
    Experiment,
    ExperimentMismatchError,
    FaultChange,
    RunStatus,
    compare_experiments,
)


def trial(
    run_id: str,
    fault: str | None = "f1",
    *,
    status: RunStatus = RunStatus.COMPLETED,
    error_type: str | None = None,
    plan: str | None = "plan",
    plan_hash: str | None = "hash-1",
    diverged_at: str | None = None,
    reused: int = 1,
    executed: int = 2,
    cost_usd: str = "0",
    unknown_cost_attempts: int = 0,
) -> Experiment:
    return Experiment(
        run_id=run_id,
        source_run_id="source",
        plan=None if fault is None else plan,
        plan_hash=None if fault is None else plan_hash,
        fault=fault,
        fired=(),
        reused=reused,
        executed=executed,
        diverged_at=diverged_at,
        status=status,
        error_type=error_type,
        cost_usd=Decimal(cost_usd),
        unknown_cost_attempts=unknown_cost_attempts,
    )


def failed(run_id: str, error_type: str | None, fault: str | None = "f1") -> Experiment:
    return trial(run_id, fault, status=RunStatus.FAILED, error_type=error_type)


def faults_of(changes: Sequence[FaultChange]) -> list[str | None]:
    return [change.fault for change in changes]


def test_faults_are_grouped_in_first_seen_order_over_before_then_after() -> None:
    before = [trial("b1", "f2"), trial("b2", None), trial("b3", "f2")]
    after = [trial("a1", "f1"), trial("a2", "f2")]

    changes = compare_experiments(before, after)

    assert faults_of(changes) == ["f2", None, "f1"]
    assert (changes[0].before.trials, changes[0].after.trials) == (2, 1)
    assert (changes[1].before.trials, changes[1].after.trials) == (1, 0)


def test_a_fault_only_in_after_has_empty_counts_before() -> None:
    changes = compare_experiments([], [trial("a1", cost_usd="0.5", unknown_cost_attempts=1)])

    only = changes[0].before
    assert (only.trials, dict(only.outcomes), only.diverged, only.executed, only.reused) == (
        0,
        {},
        0,
        0,
        0,
    )
    assert (only.cost_usd, only.unknown_cost_attempts) == (Decimal(0), 0)
    assert changes[0].after.trials == 1


def test_default_labels_name_the_error_of_a_failed_run() -> None:
    after = [
        trial("a1"),
        failed("a2", "StepFailedError"),
        failed("a3", None),
        failed("a4", "StepFailedError"),
        trial("a5", status=RunStatus.RUNNING),
    ]

    outcomes = compare_experiments([], after)[0].after.outcomes

    assert dict(outcomes) == {
        "completed": 1,
        "failed with StepFailedError": 2,
        "failed": 1,
        "running": 1,
    }
    assert list(outcomes) == ["completed", "failed with StepFailedError", "failed", "running"]


def test_a_custom_outcome_replaces_the_default_labels() -> None:
    before = [trial("b1", reused=1), trial("b2", reused=0), trial("b3", reused=0)]

    changes = compare_experiments(
        before, [], outcome=lambda e: "served" if e.reused else "executed"
    )

    assert dict(changes[0].before.outcomes) == {"served": 1, "executed": 2}


def test_counts_sum_what_the_experiments_report() -> None:
    before = [
        trial("b1", executed=2, reused=1, cost_usd="0.002", unknown_cost_attempts=1),
        trial("b2", executed=3, reused=0, cost_usd="0.003", unknown_cost_attempts=2),
    ]

    counts = compare_experiments(before, [])[0].before

    assert (counts.trials, counts.executed, counts.reused) == (2, 5, 1)
    assert (counts.cost_usd, counts.unknown_cost_attempts) == (Decimal("0.005"), 3)


def test_diverged_counts_the_experiments_that_left_the_source() -> None:
    before = [trial("b1", diverged_at="summarize#1"), trial("b2"), trial("b3", diverged_at="x#1")]

    assert compare_experiments(before, [])[0].before.diverged == 2


def test_a_different_plan_hash_is_refused() -> None:
    before = [trial("b1", plan_hash="hash-1")]
    after = [trial("a1", plan_hash="hash-2")]

    with pytest.raises(ExperimentMismatchError, match=r"hash-1.*hash-2"):
        compare_experiments(before, after)


def test_a_different_plan_id_is_refused_and_names_both() -> None:
    with pytest.raises(ExperimentMismatchError, match=r"'plan'.*'other'"):
        compare_experiments([trial("b1")], [trial("a1", plan="other")])


def test_the_mismatch_is_both_a_cuaderna_error_and_a_value_error() -> None:
    with pytest.raises(CuadernaError):
        compare_experiments([trial("b1", plan_hash="x")], [trial("a1", plan_hash="y")])
    with pytest.raises(ValueError):
        compare_experiments([trial("b1", plan_hash="x")], [trial("a1", plan_hash="y")])


def test_the_same_run_twice_in_one_side_is_refused() -> None:
    with pytest.raises(ExperimentMismatchError, match=r"'b1'.*before"):
        compare_experiments([trial("b1"), trial("b1")], [])
    with pytest.raises(ExperimentMismatchError, match=r"'a1'.*after"):
        compare_experiments([], [trial("a1"), trial("a1")])


def test_the_same_run_on_both_sides_is_not_a_repeat() -> None:
    changes = compare_experiments([trial("t1")], [trial("t1")])

    assert (changes[0].before.trials, changes[0].after.trials) == (1, 1)


def test_controls_have_no_plan_and_mix_with_a_plan() -> None:
    before = [trial("b1", None), trial("b2", "f1")]
    after = [trial("a1", None), trial("a2", "f1")]

    changes = compare_experiments(before, after)

    assert faults_of(changes) == [None, "f1"]
    assert all(change.before.trials == change.after.trials == 1 for change in changes)
