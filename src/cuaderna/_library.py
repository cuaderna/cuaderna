import random as _random
from datetime import UTC, datetime
from uuid import uuid4

from ._step import step


@step(name="cuaderna.now")
async def now() -> datetime:
    return datetime.now(UTC)


@step(name="cuaderna.random")
async def random() -> float:
    return _random.random()


@step(name="cuaderna.new_id")
async def new_id() -> str:
    return uuid4().hex
