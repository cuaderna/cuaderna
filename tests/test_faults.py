import re
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import JsonValue

import cuaderna

TIMEOUT = {"type": "TimeoutError", "message": "slow"}
RETURN = {"return": 1}
RAISE = {"raise": TIMEOUT}
APPEND = {"append": {"to": "", "text": "x"}}
STEP_KEYS = ["ask_model#1", "ask_model#11", "ask_model#2", "read_sources#1"]


def fault(**fields: object) -> dict[str, object]:
    return {"id": "f", "at": "ask_model", **fields}


def plan_document(*faults: dict[str, object]) -> dict[str, object]:
    return {"format": "cuaderna.faults/1", "id": "p", "faults": list(faults)}


def only_fault(**fields: object) -> cuaderna.Fault:
    return cuaderna.FaultPlan(plan_document(fault(**fields))).faults[0]


def raiser(**fields: object) -> cuaderna.Fault:
    return only_fault(**{"raise": {**TIMEOUT, **fields}})


def appender(pointer: str) -> cuaderna.Fault:
    return only_fault(append={"to": pointer, "text": "T"})


def test_a_raise_fault_loads_with_its_payload() -> None:
    loaded = raiser(attempts=2, terminal=True)

    assert (loaded.id, loaded.at, loaded.kind) == ("f", "ask_model", "raise")
    assert (loaded.error_type, loaded.error_message) == ("TimeoutError", "slow")
    assert (loaded.attempts, loaded.terminal) == (2, True)
    assert (raiser().attempts, raiser().terminal) == (None, False)


@pytest.mark.parametrize("value", [{"path": "a.py"}, None])
def test_a_return_fault_loads_with_its_value_even_when_it_is_null(value: JsonValue) -> None:
    loaded = only_fault(**{"return": value})

    assert (loaded.kind, loaded.value, loaded.fails_attempt(1)) == ("return", value, False)


def test_a_delay_fault_loads_with_its_seconds_and_attempts() -> None:
    loaded = only_fault(delay={"seconds": 0.5, "attempts": 2})

    assert (loaded.kind, loaded.seconds, loaded.attempts) == ("delay", 0.5, 2)
    assert (loaded.delays_attempt(2), loaded.delays_attempt(3)) == (True, False)
    assert only_fault(delay={"seconds": 3600}).delays_attempt(9)


def test_an_append_fault_loads_with_its_pointer_and_text() -> None:
    loaded = appender("/a")

    assert (loaded.kind, loaded.pointer, loaded.text) == ("append", "/a", "T")


def test_a_plan_keeps_file_order_and_stamps_each_fault_with_itself() -> None:
    plan = cuaderna.FaultPlan(plan_document(fault(id="b", **RETURN), fault(id="a", **RETURN)))

    assert [item.id for item in plan.faults] == ["b", "a"]
    assert {(item.plan_id, item.plan_hash) for item in plan.faults} == {("p", plan.content_hash)}
    assert cuaderna.FaultPlan(plan_document()).faults == ()


@pytest.mark.parametrize(
    ("fields", "failing"), [({}, [1, 2, 3]), ({"attempts": 1}, [1]), ({"attempts": 2}, [1, 2])]
)
def test_a_raise_fault_fails_named_attempts(fields: dict[str, object], failing: list[int]) -> None:
    assert [n for n in (1, 2, 3) if raiser(**fields).fails_attempt(n)] == failing


@pytest.mark.parametrize(
    ("at", "targeted"),
    [
        ("ask_model#1", ["ask_model#1"]),
        ("ask_model", ["ask_model#1", "ask_model#11", "ask_model#2"]),
        ("ask", []),
    ],
)
def test_a_fault_targets_a_key_or_a_name(at: str, targeted: list[str]) -> None:
    assert [key for key in STEP_KEYS if only_fault(at=at, **RETURN).targets(key)] == targeted


@pytest.mark.parametrize(
    ("result", "pointer", "expected"),
    [
        ({"a": "x", "b": "y"}, "/a", {"a": "xT", "b": "y"}),
        ({"items": [{"t": "p"}, {"t": "q"}]}, "/items/1/t", {"items": [{"t": "p"}, {"t": "qT"}]}),
        ({"a/b": "x"}, "/a~1b", {"a/b": "xT"}),
        ({"c~d": "x"}, "/c~0d", {"c~d": "xT"}),
        ({"~1": "x"}, "/~01", {"~1": "xT"}),
        ("x", "", "xT"),
    ],
)
def test_appending_adds_text_and_keeps_the_input(
    result: JsonValue, pointer: str, expected: JsonValue
) -> None:
    original = deepcopy(result)

    assert appender(pointer).appended_to(result) == expected
    assert result == original


@pytest.mark.parametrize(
    ("result", "pointer"),
    [
        ({"a": "x"}, "/b"),
        ({"a": ["x"]}, "/a/1"),
        ({"a": ["x"]}, "/a/-"),
        ({"a": ["x", "y"]}, "/a/01"),
        ({"a": 1}, "/a"),
        ({"a": "x"}, "/a/b"),
    ],
)
def test_a_pointer_without_a_string_is_refused(result: JsonValue, pointer: str) -> None:
    with pytest.raises(cuaderna.FaultPlanError, match=rf"'f'.*{re.escape(repr(pointer))}"):
        appender(pointer).appended_to(result)


@pytest.mark.parametrize("at", ["", "a:b", "a#", "#1", "a#0", "a#01", "a#-1", "a#x", "a#1#2"])
def test_a_target_that_is_not_a_step_name_or_key_is_refused(at: str) -> None:
    with pytest.raises(cuaderna.FaultPlanError):
        only_fault(at=at, **RETURN)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**RETURN, "raise": TIMEOUT},
        {"raise": TIMEOUT, **APPEND},
        {"unknown": 1, **RETURN},
        {"id": "", **RETURN},
        {"raise": {**TIMEOUT, "attempts": 0}},
        {"raise": {"type": "", "message": "slow"}},
        {"append": {"to": "a", "text": "x"}},
        {"append": {"to": "/a~2", "text": "x"}},
        {"append": {"to": "/a", "text": ""}},
        {"delay": {"seconds": 0}},
        {"delay": {"seconds": -1}},
        {"delay": {"seconds": 3600.5}},
        {"delay": {"seconds": 1, "attempts": 0}},
        {"delay": {"seconds": 1, "unknown": 1}},
        {"delay": {}},
        {**RETURN, "delay": {"seconds": 1}},
    ],
)
def test_a_malformed_fault_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(cuaderna.FaultPlanError):
        only_fault(**body)


@pytest.mark.parametrize(
    "document",
    [
        plan_document(fault(**RETURN), fault(**RETURN)),
        {**plan_document(), "format": "cuaderna.faults/2"},
        {**plan_document(), "id": ""},
        {**plan_document(), "extra": 1},
    ],
)
def test_a_malformed_plan_is_refused(document: dict[str, object]) -> None:
    with pytest.raises(cuaderna.FaultPlanError):
        cuaderna.FaultPlan(document)


@pytest.mark.parametrize(
    ("content", "max_bytes"),
    [("not json", 100), ("[]", 100), ("[" * 100_000, 1_048_576), ('{"id": "p"}' + " " * 100, 10)],
)
def test_a_file_that_is_not_a_plan_is_refused(tmp_path: Path, content: str, max_bytes: int) -> None:
    path = tmp_path / "faults.json"
    path.write_text(content)

    with pytest.raises(cuaderna.FaultPlanError):
        cuaderna.FaultPlan.load(path, max_bytes=max_bytes)


def test_a_plan_loads_from_a_file_and_a_missing_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "faults.json"
    path.write_text('{"format": "cuaderna.faults/1", "id": "p", "faults": []}')

    assert cuaderna.FaultPlan.load(path).id == "p"
    with pytest.raises(cuaderna.FaultPlanError):
        cuaderna.FaultPlan.load(tmp_path / "missing.json")


def test_the_content_hash_is_a_sha256_that_ignores_key_order() -> None:
    reordered = {"faults": [{"raise": TIMEOUT, "at": "ask_model", "id": "f"}], "id": "p"}
    reordered["format"] = "cuaderna.faults/1"

    content_hash = cuaderna.FaultPlan(plan_document(fault(**RAISE))).content_hash

    assert re.fullmatch(r"[0-9a-f]{64}", content_hash)
    assert cuaderna.FaultPlan(reordered).content_hash == content_hash


@pytest.mark.parametrize(
    "changed",
    [
        {**plan_document(fault(**RAISE)), "id": "other"},
        plan_document(fault(id="g", **RAISE)),
        plan_document(fault(at="ask_model#1", **RAISE)),
        plan_document(fault(**{"raise": {**TIMEOUT, "message": "fast"}})),
        plan_document(fault(**RETURN)),
    ],
)
def test_the_content_hash_changes_with_any_content(changed: dict[str, object]) -> None:
    original = cuaderna.FaultPlan(plan_document(fault(**RAISE)))

    assert cuaderna.FaultPlan(changed).content_hash != original.content_hash
