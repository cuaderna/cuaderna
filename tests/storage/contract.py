import pytest

from cuaderna._errors import LeaseLostError
from cuaderna._records import Lease, RunStatus
from cuaderna.storage import Storage


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
            await storage.renew_lease(stale, lease_seconds=60)
        with pytest.raises(LeaseLostError):
            await storage.complete_run(stale, '"done"')

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
