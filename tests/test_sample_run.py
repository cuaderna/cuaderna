import re
from pathlib import Path

import pytest
from examples.fix_tests import agent

from cuaderna import Engine

_SAMPLE_RUN = Path(__file__).parent.parent / "site" / "sample-run.json"


async def test_the_published_run_replays_without_executing_a_step(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    summary = await engine.replay(run_id, agent.fix_tests)

    assert summary == agent.Summary(fixed=True, rounds=2)
    assert capsys.readouterr().out == ""


async def test_the_published_run_shows_the_kill_and_names_no_local_path(engine: Engine) -> None:
    run_id = await engine.import_run(_SAMPLE_RUN)

    attempts = await engine.steps(run_id)

    assert [attempt.lease_token for attempt in attempts] == [1, 1, 1, 1, 1, 1, 2, 2, 2]
    assert attempts[6].step_key == "ask_model#2"
    local_path = re.compile(r"[A-Za-z]:\\\\|/home/|/Users/")
    assert local_path.search(_SAMPLE_RUN.read_text(encoding="utf-8")) is None
