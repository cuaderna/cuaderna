from cuaderna._records import RunStatus
from cuaderna.storage import Storage


class StorageContract:
    async def test_creating_an_existing_run_keeps_the_original(self, storage: Storage) -> None:
        await storage.create_run("run", "fix_tests", '{"repo": "a"}', "v1")

        existing = await storage.create_run("run", "other_name", '{"repo": "b"}', "v2")

        assert existing.name == "fix_tests"
        assert existing.input_json == '{"repo": "a"}'
        assert existing.code_version == "v1"
        assert existing.status is RunStatus.RUNNING
