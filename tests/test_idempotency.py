import pytest

import cuaderna
from cuaderna import Engine, InvalidDefinitionError, StepCallError

keys_seen: list[str] = []


@pytest.fixture(autouse=True)
def fresh_keys() -> None:
    keys_seen.clear()


@cuaderna.step
async def charge(amount: int) -> str:
    return cuaderna.idempotency_key()


@cuaderna.step
def charge_in_a_thread(amount: int) -> str:
    return cuaderna.idempotency_key()


@cuaderna.step(max_attempts=3, backoff_seconds=0)
async def charge_on_the_third_try(amount: int) -> str:
    keys_seen.append(cuaderna.idempotency_key())
    if len(keys_seen) < 3:
        raise ConnectionError("try again")
    return keys_seen[-1]


async def test_each_step_call_gets_a_key_made_of_the_run_and_the_step(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> list[str]:
        return [await charge(5), await charge(5), await charge_in_a_thread(5)]

    assert await engine.execute("order-7", flow) == [
        "order-7:charge#1",
        "order-7:charge#2",
        "order-7:charge_in_a_thread#1",
    ]


async def test_the_key_stays_the_same_across_retries(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> str:
        return await charge_on_the_third_try(5)

    await engine.execute("order-7", flow)

    assert keys_seen == ["order-7:charge_on_the_third_try#1"] * 3


async def test_the_key_only_exists_inside_a_step(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> str:
        await charge(5)
        return cuaderna.idempotency_key()

    with pytest.raises(StepCallError):
        cuaderna.idempotency_key()
    with pytest.raises(StepCallError):
        await engine.execute("order-7", flow)


@pytest.mark.parametrize("name", ["pay:now", "pay#1"])
def test_a_step_name_cannot_hold_the_separators_of_the_key(name: str) -> None:
    with pytest.raises(InvalidDefinitionError, match="cannot contain"):

        @cuaderna.step(name=name)
        async def pay() -> None:  # pyright: ignore[reportUnusedFunction]
            return None
