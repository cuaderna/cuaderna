from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from ._errors import ExperimentMismatchError
from ._experiment import Experiment
from ._records import RunStatus


@dataclass(frozen=True, slots=True)
class TrialCounts:
    trials: int
    outcomes: Mapping[str, int]
    diverged: int
    executed: int
    reused: int
    cost_usd: Decimal
    unknown_cost_attempts: int


@dataclass(frozen=True, slots=True)
class FaultChange:
    fault: str | None
    before: TrialCounts
    after: TrialCounts


def compare_experiments(
    before: Iterable[Experiment],
    after: Iterable[Experiment],
    *,
    outcome: Callable[[Experiment], str] | None = None,
) -> tuple[FaultChange, ...]:
    label = outcome if outcome is not None else _default_outcome
    before_trials = list(before)
    after_trials = list(after)
    _require_distinct_runs(before_trials, "before")
    _require_distinct_runs(after_trials, "after")
    _require_one_plan(before_trials + after_trials)
    faults: list[str | None] = []
    for trial in before_trials + after_trials:
        if trial.fault not in faults:
            faults.append(trial.fault)
    return tuple(
        FaultChange(
            fault=fault,
            before=_counts([t for t in before_trials if t.fault == fault], label),
            after=_counts([t for t in after_trials if t.fault == fault], label),
        )
        for fault in faults
    )


def _default_outcome(experiment: Experiment) -> str:
    if experiment.status is RunStatus.COMPLETED:
        return "completed"
    if experiment.status is RunStatus.FAILED:
        if experiment.error_type is None:
            return "failed"
        return f"failed with {experiment.error_type}"
    return experiment.status.value


def _require_distinct_runs(trials: list[Experiment], side: str) -> None:
    seen: set[str] = set()
    for trial in trials:
        if trial.run_id in seen:
            raise ExperimentMismatchError(
                f"run {trial.run_id!r} appears more than once in the {side} experiments"
            )
        seen.add(trial.run_id)


def _require_one_plan(trials: list[Experiment]) -> None:
    plans: list[tuple[str | None, str | None]] = []
    for trial in trials:
        if trial.fault is None:
            continue
        found = (trial.plan, trial.plan_hash)
        if found not in plans:
            plans.append(found)
    if len(plans) > 1:
        listed = ", ".join(f"{plan!r} with hash {plan_hash!r}" for plan, plan_hash in plans)
        raise ExperimentMismatchError(f"the experiments come from different plans: {listed}")


def _counts(trials: list[Experiment], label: Callable[[Experiment], str]) -> TrialCounts:
    outcomes: dict[str, int] = {}
    for trial in trials:
        name = label(trial)
        outcomes[name] = outcomes.get(name, 0) + 1
    return TrialCounts(
        trials=len(trials),
        outcomes=outcomes,
        diverged=sum(1 for t in trials if t.diverged_at is not None),
        executed=sum(t.executed for t in trials),
        reused=sum(t.reused for t in trials),
        cost_usd=sum((t.cost_usd for t in trials), Decimal(0)),
        unknown_cost_attempts=sum(t.unknown_cost_attempts for t in trials),
    )
