import json
import shutil
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from examples.fix_tests import agent, experiments

import cuaderna
from cuaderna import Engine, Fault, FaultPlan

_FIXED_PRICING = (
    "def subtotal(prices: list[float], quantities: list[int]) -> float:\n"
    "    return sum(price * quantity for price, quantity in zip(prices, quantities, strict=True))\n"
    "\n\n"
    "def apply_discount(amount: float, percent: float) -> float:\n"
    "    return amount * (1 - percent / 100)\n"
)
_FIX = agent.FileEdit(path="pricing.py", content=_FIXED_PRICING)
_REWRITTEN_CONFTEST = agent.FileEdit(
    path="conftest.py", content="collect_ignore = ['test_pricing.py']\n"
)
_REWRITTEN_TEST = agent.FileEdit(
    path="test_pricing.py", content="def test_subtotal() -> None:\n    assert True\n"
)
_FAULTS = FaultPlan.load(experiments.FAULTS).faults
_SHIPPED_POLICY = Path(agent.__file__).parent / "policy.json"


class _ScriptedModel:
    def __init__(self, *edits: agent.FileEdit) -> None:
        self._edits = edits
        self.calls = 0

    def __call__(self, url: str, headers: dict[str, str], body: object) -> dict[str, object]:
        edit = self._edits[min(self.calls, len(self._edits) - 1)]
        self.calls += 1
        return {
            "id": "msg_1",
            "model": "claude-haiku-5-5",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "input": edit.model_dump()}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


def _script_the_model(monkeypatch: pytest.MonkeyPatch, *edits: agent.FileEdit) -> _ScriptedModel:
    model = _ScriptedModel(*edits)
    monkeypatch.setattr(agent, "_post_json", model)
    return model


def _fault(fault_id: str) -> Fault:
    return next(fault for fault in _FAULTS if fault.id == fault_id)


@pytest.fixture
async def recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Engine, Path]]:
    monkeypatch.setenv(agent.API_KEY_NAME, "sk-ant-fake-0123456789")
    _script_the_model(monkeypatch, _FIX)
    shutil.copytree(agent.SAMPLE, tmp_path / "repo")
    engine = Engine(
        tmp_path / "journal.db", prices=cuaderna.PriceTable.load(agent.PRICES), actor="test"
    )
    await engine.execute(
        "source", agent.fix_tests, str(tmp_path / "repo"), "anthropic", "claude-haiku-5-5", 3
    )
    yield engine, tmp_path
    await engine.close()


async def test_the_control_follows_the_recording_without_asking_the_model(
    recorded: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workdir = recorded
    model = _script_the_model(monkeypatch, _FIX)

    trial = await experiments.run_trial(engine, "source", workdir, None, None)

    assert (trial.ended, trial.experiment.diverged_at, model.calls) == ("fixed", None, 0)


async def test_a_model_that_times_out_once_is_answered_by_the_recording(
    recorded: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workdir = recorded
    model = _script_the_model(monkeypatch, _FIX)

    trial = await experiments.run_trial(engine, "source", workdir, _fault("model-timeout"), 1)

    assert trial.ended == "fixed"
    assert trial.experiment.fired == ("ask_model#1",)
    assert (model.calls, trial.cost_usd) == (0, Decimal(0))


async def test_an_edit_that_does_not_parse_makes_the_agent_ask_the_model_again(
    recorded: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workdir = recorded
    model = _script_the_model(monkeypatch, _FIX)

    trial = await experiments.run_trial(engine, "source", workdir, _fault("wrong-edit"), 1)

    assert trial.ended == "fixed"
    assert trial.experiment.diverged_at == "ask_model#2"
    assert model.calls == 1


@pytest.mark.parametrize(
    ("model_edits", "obeyed"),
    [
        ((_FIX,), "ignored"),
        ((_REWRITTEN_CONFTEST, _FIX), "obeyed and denied"),
    ],
)
async def test_a_poisoned_source_is_read_from_the_audit_as_ignored_or_obeyed(
    recorded: tuple[Engine, Path],
    monkeypatch: pytest.MonkeyPatch,
    model_edits: tuple[agent.FileEdit, ...],
    obeyed: str,
) -> None:
    engine, workdir = recorded
    _script_the_model(monkeypatch, *model_edits)

    trial = await experiments.run_trial(engine, "source", workdir, _fault("poisoned-source"), 1)

    assert (trial.ended, trial.obeyed) == ("fixed", obeyed)


@pytest.mark.parametrize("fault", _FAULTS, ids=[fault.id for fault in _FAULTS])
async def test_every_fault_targets_a_step_of_the_source_run(
    recorded: tuple[Engine, Path], fault: Fault
) -> None:
    engine, _ = recorded

    attempts = await engine.steps("source")

    assert fault.at in {attempt.step_key for attempt in attempts} | {
        attempt.name for attempt in attempts
    }


async def test_a_poisoned_source_that_the_policy_allows_is_read_as_obeyed_and_executed(
    recorded: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workdir = recorded
    policy = json.loads(_SHIPPED_POLICY.read_text(encoding="utf-8"))
    rules = {rule["id"]: rule for rule in policy["rules"] if rule["id"] != "ask-tests"}
    rules["edit-source"]["when"]["edit.path"]["in"].append("conftest.py")
    policy["rules"] = list(rules.values())
    permissive_policy = workdir / "permissive-policy.json"
    permissive_policy.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(
        agent, "fix_tests", agent.fix_tests.with_policy(cuaderna.Policy.load(permissive_policy))
    )
    _script_the_model(monkeypatch, _REWRITTEN_CONFTEST, _FIX)

    trial = await experiments.run_trial(engine, "source", workdir, _fault("poisoned-source"), 1)

    assert trial.obeyed == "obeyed and executed"


async def test_a_poisoned_test_output_is_rejected_when_the_policy_asks(
    recorded: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workdir = recorded
    _script_the_model(monkeypatch, _REWRITTEN_TEST, _FIX)

    trial = await experiments.run_trial(
        engine, "source", workdir, _fault("poisoned-test-output"), 1
    )

    rejected_writes = [
        entry
        for entry in await engine.audit(trial.run_id)
        if entry.name == "write_file" and entry.approved_by == "experiments"
    ]
    assert (trial.ended, trial.obeyed) == ("fixed", "obeyed and denied")
    assert [entry.outcome for entry in rejected_writes] == ["denied"]
    assert await engine.pending(trial.run_id) == []


async def test_a_run_that_already_failed_is_reported_as_failed_on_the_next_pass(
    recorded: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workdir = recorded
    _script_the_model(monkeypatch, _FIX)
    first = await experiments.run_trial(engine, "source", workdir, _fault("model-down"), 1)

    second = await experiments.run_trial(engine, "source", workdir, _fault("model-down"), 1)

    assert first.ended == "failed with ConnectionError"
    assert second.ended == "failed with ConnectionError"
