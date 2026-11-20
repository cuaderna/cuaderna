import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

import cuaderna
from cuaderna._serde import canonical_json

LOOK = {"id": "look", "effect": "allow", "steps": ["run_tests", "read_sources"]}
NO_RULE = cuaderna.Decision("deny", "no_rule", ())


def rule(rule_id: str, effect: str, when: dict[str, Any] | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {"id": rule_id, "effect": effect, "steps": ["write_file"]}
    if when is not None:
        document["when"] = when
    return document


def bad_rule(**fields: object) -> dict[str, Any]:
    return {"rules": [{"id": "r", "effect": "allow", "steps": ["a"], **fields}]}


def policy_document(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"format": "cuaderna.policy/1", "id": "fix-tests-1", "rules": list(rules)}


def decide(
    rules: list[dict[str, Any]], arguments: dict[str, Any], step_name: str = "write_file"
) -> cuaderna.Decision:
    return cuaderna.Policy(policy_document(*rules)).decide(step_name, json.dumps(arguments))


def decide_on(when: dict[str, Any], arguments: dict[str, Any]) -> cuaderna.Decision:
    return decide([rule("only", "allow", when)], arguments)


def allows(when: dict[str, Any], arguments: dict[str, Any]) -> bool:
    return decide_on(when, arguments).effect == "allow"


@pytest.mark.parametrize("effect", ["allow", "ask", "deny"])
def test_a_matching_rule_decides_with_its_own_effect(effect: str) -> None:
    decision = decide([rule("only", effect)], {})

    assert decision.effect == effect
    assert (decision.reason, decision.rules) == ("rule", ("only",))


@pytest.mark.parametrize(
    ("effects", "expected"),
    [
        (["allow", "ask", "deny"], "deny"),
        (["deny", "ask", "allow"], "deny"),
        (["allow", "ask"], "ask"),
    ],
)
def test_deny_beats_ask_beats_allow_whatever_the_file_order(
    effects: list[str], expected: str
) -> None:
    rules = [rule(f"rule-{index}", effect) for index, effect in enumerate(effects)]

    decision = decide(rules, {})

    assert decision.effect == expected
    assert decision.rules == tuple(
        f"rule-{index}" for index, effect in enumerate(effects) if effect == expected
    )


@pytest.mark.parametrize("step_name", ["send_email", "Run_Tests"])
def test_a_step_that_no_rule_names_exactly_is_denied(step_name: str) -> None:
    assert decide([LOOK], {}, step_name=step_name) == NO_RULE


@pytest.mark.parametrize(
    ("when", "arguments"),
    [
        ({"edit.path": {"under": "src"}}, {"other": 1}),
        ({"edit.path": {"under": "src"}}, {"edit": "src/a.py"}),
        ({"count": {"glob": "1*"}}, {"count": 12}),
        ({"path": {"under": "src"}}, {"path": None}),
    ],
)
def test_a_condition_that_cannot_be_evaluated_denies(
    when: dict[str, Any], arguments: dict[str, Any]
) -> None:
    assert decide_on(when, arguments) == cuaderna.Decision("deny", "unevaluable", ("only",))


def test_an_unevaluable_rule_wins_over_a_matching_allow_and_is_listed_in_file_order() -> None:
    broken = {"path": {"under": "src"}}
    rules = [rule("fine", "allow"), rule("b", "allow", broken), rule("a", "deny", broken)]

    assert decide(rules, {}) == cuaderna.Decision("deny", "unevaluable", ("b", "a"))


@pytest.mark.parametrize(
    ("expected", "actual", "holds"),
    [
        (True, True, True),
        (True, 1, False),
        (1, True, False),
        (1, 1.0, True),
        (100, 1e2, True),
        (100.0, 100, True),
        (False, 0, False),
        (0, False, False),
        (1, 2, False),
        ("a", "a", True),
        (None, None, True),
    ],
)
def test_equals_compares_json_values_by_type(expected: object, actual: object, holds: bool) -> None:
    assert allows({"value": {"equals": expected}}, {"value": actual}) is holds


@pytest.mark.parametrize(
    ("actual", "holds"),
    [("b", True), (2, True), (2.0, True), (True, False), (1, False), ("c", False)],
)
def test_in_holds_for_any_listed_value(actual: object, holds: bool) -> None:
    assert allows({"value": {"in": ["a", "b", 2]}}, {"value": actual}) is holds


@pytest.mark.parametrize(
    ("value", "holds"), [("test_a.py", True), ("a_test.py", False), ("TEST_a.py", False)]
)
def test_glob_matches_a_string_case_sensitively(value: str, holds: bool) -> None:
    assert allows({"name": {"glob": "test_*.py"}}, {"name": value}) is holds


@pytest.mark.parametrize(
    ("path", "directory", "holds"),
    [
        ("src/a.py", "src", True),
        ("src", "src", True),
        ("src\\a.py", "src", True),
        ("src/../.github/x", "src", False),
        ("/src/a.py", "src", False),
        ("C:\\src\\a.py", "src", False),
        ("srcfoo/a.py", "src", False),
        ("a/b.py", ".", True),
        ("a/../../b.py", ".", False),
        ("..", ".", False),
    ],
)
def test_under_is_decided_on_the_text_of_the_path(path: str, directory: str, holds: bool) -> None:
    assert allows({"path": {"under": directory}}, {"path": path}) is holds


AMBIGUOUS_PATHS = [
    ".GITHUB/wf.yml",
    ".github./wf.yml",
    "GITHUB~1/wf.yml",
    ".github::$INDEX_ALLOCATION/wf.yml",
    "src/.. /secret.txt",
    "src/ok.txt.",
    "src/ok.txt ",
    "src/a.txt:stream",
    "src/CON",
    "src/nul.txt",
    "SRC/a.py",
    "src/\u0065\u0301.py",
    "src/a\x00b",
]


@pytest.mark.parametrize("path", AMBIGUOUS_PATHS)
def test_a_path_the_filesystem_may_read_differently_denies_the_step(path: str) -> None:
    rules = [
        rule("everything", "allow"),
        rule("src", "allow", {"path": {"under": "src"}}),
        rule("workflows", "deny", {"path": {"under": ".github"}}),
    ]

    decision = decide(rules, {"path": path})

    assert decision.effect == "deny"
    assert decision.reason == "unevaluable"


@pytest.mark.parametrize(
    ("path", "effect", "reason"),
    [
        ("src/a.py", "allow", "rule"),
        ("src/Sub/A.py", "allow", "rule"),
        ("docs/a.md", "deny", "no_rule"),
    ],
)
def test_a_plain_path_is_still_decided_on_its_text(path: str, effect: str, reason: str) -> None:
    decision = decide([rule("src", "allow", {"path": {"under": "src"}})], {"path": path})

    assert (decision.effect, decision.reason) == (effect, reason)


def test_a_value_that_matches_a_glob_only_when_case_is_ignored_denies_the_step() -> None:
    rules = [rule("everything", "allow"), rule("tests", "ask", {"name": {"glob": "test_*"}})]

    assert decide(rules, {"name": "TEST_x.py"}).reason == "unevaluable"
    assert decide(rules, {"name": "test_x.py"}).effect == "ask"
    assert decide(rules, {"name": "other.py"}).effect == "allow"


def test_every_path_in_a_when_must_hold() -> None:
    when = {"edit.path": {"under": "src"}, "edit.mode": {"equals": "append"}}

    assert allows(when, {"edit": {"path": "src/a.py", "mode": "append"}})
    assert decide_on(when, {"edit": {"path": "src/a.py", "mode": "replace"}}) == NO_RULE


INVALID_CHANGES: list[dict[str, Any]] = [
    {"format": "cuaderna.policy/2"},
    {"id": ""},
    {"extra": 1},
    bad_rule(id=""),
    bad_rule(steps=[]),
    bad_rule(effect="permit"),
    bad_rule(when={"p": {}}),
    bad_rule(when={"p": {"matches": "x"}}),
    bad_rule(when={"p": {"equals": 1, "glob": "*"}}),
    {"rules": [rule("r", "allow"), rule("r", "deny")]},
]


@pytest.mark.parametrize("change", INVALID_CHANGES)
def test_a_document_this_library_cannot_read_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(cuaderna.PolicyError):
        cuaderna.Policy({**policy_document(), **change})


@pytest.mark.parametrize("directory", ["/src", "C:src", "C:\\src", "src/../x", "..", "a\\..\\b"])
def test_an_under_directory_that_leaves_the_tree_is_rejected(directory: str) -> None:
    with pytest.raises(cuaderna.PolicyError):
        cuaderna.Policy(policy_document(rule("r", "allow", {"p": {"under": directory}})))


def test_a_policy_is_loaded_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_document(LOOK)), encoding="utf-8")

    policy = cuaderna.Policy.load(path)

    assert policy.id == "fix-tests-1"
    assert policy.content_hash == cuaderna.Policy(policy_document(LOOK)).content_hash
    assert policy.decide("run_tests", "{}") == cuaderna.Decision("allow", "rule", ("look",))


@pytest.mark.parametrize("content", [b"{", b"[]", b"3", b"\xff"])
def test_a_file_that_is_not_a_json_object_is_rejected(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "policy.json"
    path.write_bytes(content)

    with pytest.raises(cuaderna.PolicyError):
        cuaderna.Policy.load(path)


@pytest.mark.parametrize("name", ["missing.json", ""])
def test_a_file_that_cannot_be_read_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(cuaderna.PolicyError):
        cuaderna.Policy.load(tmp_path / name)


def test_the_hash_follows_the_content_and_not_the_layout() -> None:
    original = cuaderna.Policy(policy_document(LOOK))
    reordered = cuaderna.Policy(json.loads(json.dumps(policy_document(LOOK), sort_keys=True)))
    edited = cuaderna.Policy(policy_document({**LOOK, "effect": "deny"}))

    assert original.content_hash == reordered.content_hash
    assert original.content_hash != edited.content_hash


def test_the_hash_is_the_full_sha256_of_the_canonical_policy() -> None:
    document = policy_document(LOOK)
    other = policy_document({**LOOK, "effect": "deny"})

    first = cuaderna.Policy(document).content_hash
    second = cuaderna.Policy(other).content_hash

    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert re.fullmatch(r"[0-9a-f]{64}", second)
    assert first != second
    assert first == hashlib.sha256(canonical_json(document).encode()).hexdigest()


DEEP = b'{"a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
HUGE_INT = b'{"a": ' + b"9" * 5_000 + b"}"


@pytest.mark.parametrize("content", [DEEP, HUGE_INT, b"\xff"])
def test_a_hostile_file_is_rejected_as_a_policy_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "policy.json"
    path.write_bytes(content)

    with pytest.raises(cuaderna.PolicyError):
        cuaderna.Policy.load(path)


def test_a_file_of_exactly_max_bytes_is_read(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    content = json.dumps(policy_document(LOOK)).encode()
    path.write_bytes(content)

    assert cuaderna.Policy.load(path, max_bytes=len(content)).id == "fix-tests-1"

    with pytest.raises(cuaderna.PolicyError, match="limit"):
        cuaderna.Policy.load(path, max_bytes=len(content) - 1)
