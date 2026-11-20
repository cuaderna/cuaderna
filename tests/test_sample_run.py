import re
from decimal import Decimal
from pathlib import Path

import pytest
from examples.fix_tests import agent

from cuaderna import Engine

_SAMPLE_RUN = Path(__file__).parent.parent / "site" / "sample-run.json"
_SAMPLE_SEAL = "5de1a90b8088c3005cf57f454693e906ab437da1079a46853b8d2bee22f0c2bb"


async def test_the_published_run_replays_without_executing_a_step(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    summary = await engine.replay(run_id, agent.fix_tests)

    assert summary == agent.Summary(fixed=True, rounds=1)
    assert capsys.readouterr().out == ""


async def test_the_published_run_shows_its_three_workers_and_names_no_local_path(
    engine: Engine,
) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    attempts = await engine.steps(run_id)

    assert [attempt.lease_token for attempt in attempts] == [1, 1, 2, 3, 3]
    assert [attempt.step_key for attempt in attempts] == [
        "run_tests#1",
        "read_sources#1",
        "ask_model#1",
        "write_file#1",
        "run_tests#2",
    ]
    local_path = re.compile(r"[A-Za-z]:\\\\Users\\\\|/home/|/Users/")
    assert local_path.search(_SAMPLE_RUN.read_text(encoding="utf-8")) is None


async def test_the_published_run_verifies_to_the_seal_it_was_recorded_with(
    engine: Engine,
) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    assert await engine.verify(run_id) == _SAMPLE_SEAL


async def test_the_published_run_carries_the_cost_of_its_model_call(engine: Engine) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    cost = await engine.cost(run_id)

    assert cost.total_usd == Decimal("0.0001758")
    assert cost.unknown_attempts == 0
    assert [(attempt.step_key, attempt.basis) for attempt in cost.attempts] == [
        ("ask_model#1", "computed")
    ]


async def test_the_published_run_audits_with_the_policy_it_was_recorded_under(
    engine: Engine,
) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    entries = await engine.audit(run_id)

    assert {entry.policy for entry in entries} == {"fix-tests-2"}
    assert {len(entry.policy_hash or "") for entry in entries} == {64}
    assert [entry.rules for entry in entries] == [
        ("look",),
        ("look",),
        ("look",),
        ("edit-source",),
        ("look",),
    ]
