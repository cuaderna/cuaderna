from datetime import UTC, datetime

import pytest

from cuaderna._errors import JournalConflictError, LeaseLostError
from cuaderna._records import AttemptStatus, Lease, RunStatus, StepAttempt
from cuaderna.storage import Storage


def attempt_for(step_key: str, attempt: int = 1) -> StepAttempt:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    return StepAttempt(
        step_key=step_key,
        name=step_key.split("#")[0],
        attempt=attempt,
        status=AttemptStatus.OK,
        args_hash="hash",
        args_json="[]",
        result_json='"done"',
        error_json=None,
        started_at=moment,
        finished_at=moment,
    )


async def claimed_run(storage: Storage, owner: str = "worker-a") -> Lease:
    await storage.create_run("run", "fix_tests", '{"repo": "a"}', None)
    lease = await storage.claim_run("run", owner, lease_seconds=60)
    assert lease is not None
    return lease


class StorageContract:
    async def test_creating_an_existing_run_keeps_the_original(self, storage: Storage) -> None:
        await storage.create_run("run", "fix_tests", '{"repo": "a"}', "v1")

        existing = await storage.create_run("run", "other_name", '{"repo": "b"}', "v2")

        assert existing.name == "fix_tests"
        assert existing.input_json == '{"repo": "a"}'
        assert existing.code_version == "v1"
        assert existing.status is RunStatus.RUNNING

    async def test_attempts_load_in_the_order_they_were_recorded(self, storage: Storage) -> None:
        lease = await claimed_run(storage)
        recorded = [attempt_for("zeta#1"), attempt_for("alpha#1"), attempt_for("alpha#1", 2)]

        for attempt in recorded:
            await storage.record_attempt(lease, attempt)

        assert await storage.load_attempts("run") == recorded

    async def test_a_live_lease_blocks_other_workers(self, storage: Storage) -> None:
        await claimed_run(storage, "worker-a")

        assert await storage.claim_run("run", "worker-b", lease_seconds=60) is None

    async def test_an_expired_lease_can_be_taken_over(self, storage: Storage) -> None:
        await storage.create_run("run", "fix_tests", "{}", None)
        await storage.claim_run("run", "worker-a", lease_seconds=0)

        takeover = await storage.claim_run("run", "worker-b", lease_seconds=60)

        assert takeover is not None
        assert takeover.owner == "worker-b"

    async def test_a_worker_that_lost_its_lease_cannot_write(self, storage: Storage) -> None:
        await storage.create_run("run", "fix_tests", "{}", None)
        stale = await storage.claim_run("run", "worker-a", lease_seconds=0)
        assert stale is not None
        await storage.claim_run("run", "worker-b", lease_seconds=60)

        with pytest.raises(LeaseLostError):
            await storage.record_attempt(stale, attempt_for("step#1"))
        with pytest.raises(LeaseLostError):
            await storage.renew_lease(stale, lease_seconds=60)
        with pytest.raises(LeaseLostError):
            await storage.complete_run(stale, '"done"')

        assert await storage.load_attempts("run") == []

    async def test_renewing_keeps_other_workers_out(self, storage: Storage) -> None:
        await storage.create_run("run", "fix_tests", "{}", None)
        lease = await storage.claim_run("run", "worker-a", lease_seconds=0)
        assert lease is not None

        await storage.renew_lease(lease, lease_seconds=60)

        assert await storage.claim_run("run", "worker-b", lease_seconds=60) is None

    async def test_a_released_run_can_be_claimed_again(self, storage: Storage) -> None:
        lease = await claimed_run(storage, "worker-a")

        await storage.release_run(lease)

        assert await storage.claim_run("run", "worker-b", lease_seconds=60) is not None

    async def test_the_same_attempt_cannot_be_recorded_twice(self, storage: Storage) -> None:
        lease = await claimed_run(storage)
        await storage.record_attempt(lease, attempt_for("step#1"))

        with pytest.raises(JournalConflictError):
            await storage.record_attempt(lease, attempt_for("step#1"))

    async def test_a_completed_run_keeps_its_output_and_cannot_be_claimed(
        self, storage: Storage
    ) -> None:
        lease = await claimed_run(storage)

        await storage.complete_run(lease, '{"fixed": 3}')

        finished = await storage.create_run("run", "fix_tests", "{}", None)
        assert finished.status is RunStatus.COMPLETED
        assert finished.output_json == '{"fixed": 3}'
        assert finished.finished_at is not None
        assert await storage.claim_run("run", "worker-b", lease_seconds=60) is None

    async def test_a_failed_run_keeps_its_error(self, storage: Storage) -> None:
        lease = await claimed_run(storage)

        await storage.fail_run(lease, '{"type": "ValueError"}')

        finished = await storage.create_run("run", "fix_tests", "{}", None)
        assert finished.status is RunStatus.FAILED
        assert finished.error_json == '{"type": "ValueError"}'
