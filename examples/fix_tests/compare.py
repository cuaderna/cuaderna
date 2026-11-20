import argparse
import asyncio
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from examples.fix_tests import gives_up
from examples.fix_tests.experiments import PLAIN, recorded_error_type
from pydantic import BaseModel, TypeAdapter

import cuaderna
from cuaderna import Engine, Experiment, FaultChange, RunStatus, TrialCounts

COLUMNS = ("fault", "trials", "before", "after", "diverged", "executed", "cost")


class _IndexEntry(BaseModel):
    file: str
    run_id: str
    kind: str
    agent: str = PLAIN


_INDEX = TypeAdapter(list[_IndexEntry])


@dataclass(frozen=True, slots=True)
class _Side:
    experiments: tuple[Experiment, ...]
    labels: Mapping[str, str]


async def outcome_label(engine: Engine, experiment: Experiment) -> str:
    if experiment.status is RunStatus.FAILED:
        return f"failed with {await recorded_error_type(engine, experiment.run_id)}"
    output_json = (await engine.run_record(experiment.run_id)).output_json
    if experiment.status is not RunStatus.COMPLETED or output_json is None:
        return experiment.status.value
    summary = gives_up.Summary.model_validate_json(output_json)
    if summary.gave_up is not None:
        return "gave up"
    return "fixed" if summary.fixed else "not fixed"


def _entries(index: Path, agent_name: str | None) -> list[_IndexEntry]:
    try:
        entries = _INDEX.validate_json(index.read_bytes())
    except (OSError, ValueError) as error:
        raise SystemExit(f"{index}: {error}") from error
    trials = [entry for entry in entries if entry.kind != "source"]
    if agent_name is None:
        agents = sorted({entry.agent for entry in trials})
        if len(agents) > 1:
            raise SystemExit(f"{index} holds runs of several agents {agents}: pick one")
        return trials
    chosen = [entry for entry in trials if entry.agent == agent_name]
    if not chosen:
        raise SystemExit(f"{index} has no runs of the agent {agent_name!r}")
    return chosen


def _run_file(index: Path, entry: _IndexEntry) -> Path:
    folder = index.parent.resolve()
    path = (folder / entry.file).resolve()
    if not path.is_relative_to(folder):
        raise SystemExit(f"{index}: {entry.file} is outside {folder}")
    return path


async def _load(engine: Engine, index: Path, agent_name: str | None, prefix: str) -> _Side:
    experiments: list[Experiment] = []
    labels: dict[str, str] = {}
    for entry in _entries(index, agent_name):
        try:
            run_id = await engine.import_run(
                _run_file(index, entry), as_run_id=f"{prefix}.{entry.run_id}"
            )
            experiment = await engine.experiment(run_id)
        except (cuaderna.RunFileError, cuaderna.RunNotAvailableError) as error:
            raise SystemExit(f"{index}: {entry.file}: {error}") from error
        experiments.append(experiment)
        labels[run_id] = await outcome_label(engine, experiment)
    return _Side(tuple(experiments), labels)


async def compare(
    before: Path, after: Path, *, before_agent: str | None, after_agent: str | None
) -> tuple[FaultChange, ...]:
    with tempfile.TemporaryDirectory(prefix="cuaderna-compare-") as folder:
        engine = Engine(Path(folder) / "journal.db")
        try:
            before_side = await _load(engine, before, before_agent, "before")
            after_side = await _load(engine, after, after_agent, "after")
        finally:
            await engine.close()
    labels = {**before_side.labels, **after_side.labels}
    try:
        return cuaderna.compare_experiments(
            before_side.experiments,
            after_side.experiments,
            outcome=lambda experiment: labels[experiment.run_id],
        )
    except cuaderna.ExperimentMismatchError as error:
        raise SystemExit(str(error)) from error


def _outcomes(counts: TrialCounts) -> str:
    return ", ".join(f"{label} {count}" for label, count in counts.outcomes.items()) or "-"


def _cost(counts: TrialCounts) -> str:
    if counts.unknown_cost_attempts == 0:
        return f"{counts.cost_usd}"
    return f"{counts.cost_usd} (+{counts.unknown_cost_attempts} unpriced)"


def row(change: FaultChange) -> str:
    before = change.before
    after = change.after
    cells = [
        change.fault or "control",
        f"{before.trials} -> {after.trials}",
        _outcomes(before),
        _outcomes(after),
        f"{before.diverged} -> {after.diverged}",
        f"{before.executed} -> {after.executed}",
        f"{_cost(before)} -> {_cost(after)} USD",
    ]
    return " | ".join(cells)


async def _main(arguments: argparse.Namespace) -> None:
    changes = await compare(
        arguments.before,
        arguments.after,
        before_agent=arguments.before_agent,
        after_agent=arguments.after_agent,
    )
    print(" | ".join(COLUMNS))
    for change in changes:
        print(row(change))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compare two runs of the faults of faults.json, fault by fault."
    )
    parser.add_argument("--before", type=Path, required=True, help="the runs.json of the first run")
    parser.add_argument("--after", type=Path, required=True, help="the runs.json of the second run")
    parser.add_argument("--before-agent", help="take only this agent's runs from --before")
    parser.add_argument("--after-agent", help="take only this agent's runs from --after")
    asyncio.run(_main(parser.parse_args()))
