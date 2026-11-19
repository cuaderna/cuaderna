import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cuaderna._errors import SchemaVersionError
from cuaderna.storage import Storage
from cuaderna.storage._sqlite import SqliteStorage

from .contract import StorageContract


class TestSqliteStorage(StorageContract):
    @pytest.fixture
    async def storage(self, tmp_path: Path) -> AsyncIterator[Storage]:
        storage = SqliteStorage(tmp_path / "journal.db")
        yield storage
        await storage.close()


async def test_a_journal_from_a_newer_library_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    storage = SqliteStorage(path)

    with pytest.raises(SchemaVersionError):
        await storage.create_run("run", "fix_tests", "{}", None)

    await storage.close()
