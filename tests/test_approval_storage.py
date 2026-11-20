import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cuaderna._errors import ApprovalNotPendingError, LeaseLostError
from cuaderna._records import ApprovalAnswer, ApprovalRequest, Lease
from cuaderna.storage import Storage
from cuaderna.storage._migrations import pending_statements
from cuaderna.storage._sqlite import SqliteStorage

from .storage.contract import attempt_for, claimed_run

MOMENT = datetime(2026, 1, 1, tzinfo=UTC)
STATEMENTS_UP_TO_VERSION_2 = 4
STATEMENTS_UP_TO_VERSION_4 = 13
ANSWER_YES = ApprovalAnswer(approved=True, by="ana", note="looks fine", at=MOMENT)


def request_for(run_id: str = "run", step_key: str = "deploy#1") -> ApprovalRequest:
    return ApprovalRequest(
        run_id=run_id,
        step_key=step_key,
        name="deploy",
        args_hash="hash",
        args_json="[]",
        policy_id="policy",
        policy_hash="policy-hash",
        rules=("deploy needs a human",),
        requested_at=MOMENT,
    )


@pytest.fixture
async def storage(journal_path: Path) -> AsyncIterator[Storage]:
    storage = SqliteStorage(journal_path)
    yield storage
    await storage.close()


async def test_a_request_is_listed_as_pending(storage: Storage) -> None:
    lease = await claimed_run(storage)

    await storage.request_approval(lease, request_for())

    approvals = await storage.list_approvals("run")
    assert [(a.request, a.answer) for a in approvals] == [(request_for(), None)]


async def test_asking_twice_keeps_one_row(storage: Storage) -> None:
    lease = await claimed_run(storage)

    await storage.request_approval(lease, request_for())
    await storage.request_approval(lease, request_for())

    assert len(await storage.list_approvals("run")) == 1


async def test_a_request_under_a_lost_lease_is_refused(storage: Storage) -> None:
    lease = await claimed_run(storage)
    stale = Lease(run_id=lease.run_id, owner=lease.owner, token=lease.token + 1)

    with pytest.raises(LeaseLostError):
        await storage.request_approval(stale, request_for())


async def test_an_answer_is_listed_with_its_request(storage: Storage) -> None:
    lease = await claimed_run(storage)
    await storage.request_approval(lease, request_for())

    await storage.answer_approval("run", "deploy#1", "hash", ANSWER_YES)

    approvals = await storage.list_approvals(None)
    assert [a.answer for a in approvals] == [ANSWER_YES]


async def test_a_second_answer_is_refused(storage: Storage) -> None:
    lease = await claimed_run(storage)
    await storage.request_approval(lease, request_for())
    await storage.answer_approval("run", "deploy#1", "hash", ANSWER_YES)

    with pytest.raises(ApprovalNotPendingError):
        await storage.answer_approval("run", "deploy#1", "hash", ANSWER_YES)


async def test_an_answer_for_an_unknown_request_is_refused(storage: Storage) -> None:
    with pytest.raises(ApprovalNotPendingError):
        await storage.answer_approval("run", "deploy#1", "hash", ANSWER_YES)


async def test_listing_one_run_excludes_another(storage: Storage) -> None:
    lease = await claimed_run(storage)
    await storage.request_approval(lease, request_for())
    await storage.create_run("other", "fix_tests", "{}", None)
    other_lease = await storage.claim_run("other", "worker-b", lease_seconds=60)
    assert other_lease is not None
    await storage.request_approval(other_lease, request_for(run_id="other"))

    approvals = await storage.list_approvals("run")

    assert [a.request.run_id for a in approvals] == ["run"]
    assert len(await storage.list_approvals(None)) == 2


@pytest.mark.parametrize(
    ("table", "column"),
    [("step_attempts", "name"), ("approval_requests", "name"), ("approval_answers", "answered_by")],
)
@pytest.mark.parametrize("statement", ["UPDATE {table} SET {column} = 'x'", "DELETE FROM {table}"])
async def test_journal_tables_are_append_only(
    storage: Storage, journal_path: Path, table: str, column: str, statement: str
) -> None:
    lease = await claimed_run(storage)
    await storage.record_attempt(lease, attempt_for("deploy#1"))
    await storage.request_approval(lease, request_for())
    await storage.answer_approval("run", "deploy#1", "hash", ANSWER_YES)
    connection = sqlite3.connect(journal_path)
    before = connection.execute(f"SELECT * FROM {table}").fetchall()

    with pytest.raises(sqlite3.Error, match="append-only"):
        connection.execute(statement.format(table=table, column=column))

    assert connection.execute(f"SELECT * FROM {table}").fetchall() == before
    connection.close()


async def test_insert_or_replace_cannot_rewrite_an_attempt(
    storage: Storage, journal_path: Path
) -> None:
    lease = await claimed_run(storage)
    await storage.record_attempt(lease, attempt_for("deploy#1"))
    connection = sqlite3.connect(journal_path)
    before = connection.execute("SELECT * FROM step_attempts").fetchall()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO step_attempts (run_id, step_key, name, attempt, status, "
            "args_hash, args_json, started_at, finished_at) "
            "VALUES ('run', 'deploy#1', 'deploy', 1, 'error', 'other', '[]', 'x', 'x')"
        )

    assert connection.execute("SELECT * FROM step_attempts").fetchall() == before
    connection.close()


async def test_insert_or_replace_cannot_turn_a_rejection_into_an_approval(
    storage: Storage, journal_path: Path
) -> None:
    lease = await claimed_run(storage)
    await storage.request_approval(lease, request_for())
    await storage.answer_approval(
        "run", "deploy#1", "hash", ApprovalAnswer(approved=False, by="ana", note=None, at=MOMENT)
    )
    connection = sqlite3.connect(journal_path)
    requests_before = connection.execute("SELECT * FROM approval_requests").fetchall()
    answers_before = connection.execute("SELECT * FROM approval_answers").fetchall()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO approval_answers "
            "(request_id, approved, answered_by, note, answered_at) VALUES (1, 1, 'eve', NULL, 'x')"
        )
    connection.execute(
        "INSERT OR REPLACE INTO approval_requests (id, run_id, step_key, name, args_hash, "
        "args_json, policy_id, policy_hash, rules_json, requested_at) "
        "VALUES (1, 'run', 'deploy#1', 'other', 'hash', '[]', 'p', 'h', '[]', 'x')"
    )

    assert connection.execute("SELECT * FROM approval_requests").fetchall() == requests_before
    assert connection.execute("SELECT * FROM approval_answers").fetchall() == answers_before
    connection.close()


async def test_asking_twice_for_the_same_step_still_keeps_one_request(
    storage: Storage,
) -> None:
    lease = await claimed_run(storage)

    await storage.request_approval(lease, request_for())
    await storage.request_approval(lease, request_for())

    assert len(await storage.list_approvals("run")) == 1


async def test_a_version_4_journal_migrates_to_5_and_gets_the_replace_triggers(
    storage: Storage, journal_path: Path
) -> None:
    connection = sqlite3.connect(journal_path)
    for statement in pending_statements(0)[:STATEMENTS_UP_TO_VERSION_4]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 4")
    connection.close()

    lease = await claimed_run(storage)
    await storage.record_attempt(lease, attempt_for("deploy#1"))

    connection = sqlite3.connect(journal_path)
    assert connection.execute("PRAGMA user_version").fetchone() == (6,)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO step_attempts (run_id, step_key, name, attempt, status, "
            "args_hash, args_json, started_at, finished_at) "
            "VALUES ('run', 'deploy#1', 'deploy', 1, 'error', 'other', '[]', 'x', 'x')"
        )
    connection.close()


async def test_a_version_2_journal_migrates_to_3_with_its_attempts(
    storage: Storage, journal_path: Path
) -> None:
    moment = MOMENT.isoformat()
    connection = sqlite3.connect(journal_path)
    for statement in pending_statements(0)[:STATEMENTS_UP_TO_VERSION_2]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 2")
    connection.execute(
        "INSERT INTO runs (run_id, name, input_json, status, created_at) "
        f"VALUES ('old', 'fix_tests', '{{}}', 'running', '{moment}')"
    )
    connection.execute(
        "INSERT INTO step_attempts (run_id, step_key, name, attempt, status, args_hash, "
        "args_json, started_at, finished_at) VALUES "
        f"('old', 'step#1', 'step', 1, 'ok', 'h', '[]', '{moment}', '{moment}')"
    )
    connection.commit()
    connection.close()

    attempts = await storage.load_attempts("old")

    assert [attempt.step_key for attempt in attempts] == ["step#1"]
    connection = sqlite3.connect(journal_path)
    assert connection.execute("PRAGMA user_version").fetchone() == (6,)
    connection.close()
