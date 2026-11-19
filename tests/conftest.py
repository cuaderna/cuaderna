from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cuaderna import Engine
from cuaderna.storage import Storage
from cuaderna.storage._sqlite import SqliteStorage


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "journal.db"


@pytest.fixture
async def engine(journal_path: Path) -> AsyncIterator[Engine]:
    engine = Engine(journal_path)
    yield engine
    await engine.close()


@pytest.fixture
async def journal(journal_path: Path) -> AsyncIterator[Storage]:
    storage = SqliteStorage(journal_path)
    yield storage
    await storage.close()
