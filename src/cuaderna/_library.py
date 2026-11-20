import random as _random
from datetime import UTC, datetime
from uuid import uuid4

from ._step import library_step

LIBRARY_STEPS = frozenset({"cuaderna.now", "cuaderna.random", "cuaderna.new_id"})


@library_step("cuaderna.now")
async def now() -> datetime:
    return datetime.now(UTC)


@library_step("cuaderna.random")
async def random() -> float:
    return _random.random()


@library_step("cuaderna.new_id")
async def new_id() -> str:
    return uuid4().hex
