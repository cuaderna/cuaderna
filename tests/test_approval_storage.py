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
    assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    connection.close()
