import json
import shutil
from pathlib import Path

import pytest
from examples.fix_tests import agent

import cuaderna
from cuaderna import Engine

_KEY = "sk-ant-fake-0123456789"
_FIXED_PRICING = (
    "def subtotal(prices: list[float], quantities: list[int]) -> float:\n"
    "    return sum(price * quantity for price, quantity in zip(prices, quantities, strict=True))\n"
    "\n\n"
    "def apply_discount(amount: float, percent: float) -> float:\n"
    "    return amount * (1 - percent / 100)\n"
)
_PROBE = (
    "import os\n\n\n"
    "def test_the_api_key_is_not_in_the_environment() -> None:\n"
    "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
    "    assert 'SECRET_TOKEN' not in os.environ\n"
)


@cuaderna.run
async def _run_the_tests(repo: str) -> agent.TestReport:
    return await agent.run_tests(repo)


def _write_args(path: str) -> str:
    return json.dumps({"repo": "repo", "edit": {"path": path, "content": "x = 1\n"}})


def _options(workdir: Path) -> agent._Options:  # pyright: ignore[reportPrivateUsage]
    return agent._Options(  # pyright: ignore[reportPrivateUsage]
        "run", workdir, "anthropic", "claude-haiku-5-5", 3, None, None, "actor", "approver"
    )


def _fake_model(sent_keys: list[str]) -> object:
    def reply(url: str, headers: dict[str, str], body: object) -> dict[str, object]:
        sent_keys.append(headers["x-api-key"])
        return {
            "id": "msg_1",
            "model": "claude-haiku-5-5",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "input": {"path": "pricing.py", "content": _FIXED_PRICING}}
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    return reply


async def test_the_tests_run_without_the_api_key_in_their_environment(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    (tmp_path / "test_probe.py").write_text(_PROBE, encoding="utf-8")

    report = await engine.execute("probe", _run_the_tests, str(tmp_path))

    assert report.passed, report.output


async def test_the_tests_report_the_same_output_for_the_same_state(
    engine: Engine, tmp_path: Path
) -> None:
    reports: list[agent.TestReport] = []
    for name in ("first", "second"):
        repo = tmp_path / name / "repo"
        shutil.copytree(agent.SAMPLE, repo)
        reports.append(await engine.execute(name, _run_the_tests, str(repo)))

    assert reports[0] == reports[1]
    assert "passed in" not in reports[0].output


def test_the_test_environment_keeps_only_the_listed_names() -> None:
    environment = agent._test_environment(  # pyright: ignore[reportPrivateUsage]
        {
            "PATH": "/bin",
            "SystemRoot": "C:\\Windows",
            "HOME": "/home/me",
            "ANTHROPIC_API_KEY": _KEY,
            "AWS_SECRET_ACCESS_KEY": "x",
            "PYTHONPATH": "/elsewhere",
        }
    )

    assert environment == {"PATH": "/bin", "SystemRoot": "C:\\Windows", "HOME": "/home/me"}


@pytest.mark.parametrize(
    "path",
    [
        "test_pricing.py",
        "./test_pricing.py",
        "x/../test_pricing.py",
        "TEST_pricing.py",
        "TEST_P~1.PY",
        "test_pricing.py.",
        "test_pricing.py ",
        "tests/test_pricing.py",
        "pricing_test.py",
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "evil.pth",
        "sitecustomize.py",
        "usercustomize.py",
    ],
)
def test_an_edit_that_can_change_what_pytest_runs_is_never_allowed(path: str) -> None:
    policy = cuaderna.Policy.load(agent._POLICY)  # pyright: ignore[reportPrivateUsage]

    decision = policy.decide("write_file", _write_args(path))

    assert decision.effect == "ask"


def test_an_edit_of_the_source_file_of_the_sample_is_allowed() -> None:
    policy = cuaderna.Policy.load(agent._POLICY)  # pyright: ignore[reportPrivateUsage]

    assert policy.decide("write_file", _write_args("pricing.py")).effect == "allow"


def test_a_new_file_is_not_allowed() -> None:
    policy = cuaderna.Policy.load(agent._POLICY)  # pyright: ignore[reportPrivateUsage]

    assert policy.decide("write_file", _write_args("helper.py")).effect != "allow"


@pytest.mark.parametrize(
    "value", ["", "  \n", "sk-ant\nfake", "sk ant", "sk-ant\x00", "sk-ant-\u00e9"]
)
def test_a_key_that_is_not_a_header_value_stops_before_any_run(value: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        agent.read_api_key({"ANTHROPIC_API_KEY": value})

    assert value.strip() == "" or value.strip() not in str(stopped.value)


def test_a_trailing_newline_is_stripped_from_the_key() -> None:
    key = agent.read_api_key({"ANTHROPIC_API_KEY": _KEY + "\n"})

    assert key == _KEY


async def test_a_key_with_an_inner_newline_starts_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake\nrest")
    monkeypatch.setattr(agent, "_post_json", _fake_model([]))

    with pytest.raises(SystemExit):
        await agent._main(_options(tmp_path))  # pyright: ignore[reportPrivateUsage]

    assert not (tmp_path / "journal.db").exists()


async def test_the_key_is_in_no_recorded_attempt_of_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_keys: list[str] = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY + "\n")
    monkeypatch.setattr(agent, "_post_json", _fake_model(sent_keys))

    await agent._main(_options(tmp_path))  # pyright: ignore[reportPrivateUsage]

    assert sent_keys == [_KEY]
    reopened = Engine(tmp_path / "journal.db")
    try:
        attempts = await reopened.steps("run")
    finally:
        await reopened.close()
    assert [attempt.step_key for attempt in attempts][-1] == "run_tests#2"
    for attempt in attempts:
        assert _KEY not in repr(attempt)
