import json

import cuaderna
from cuaderna import Engine
from cuaderna.storage import Storage


class Shop:
    def __init__(self, currency: str) -> None:
        self.currency = currency
        self.charges = 0

    @cuaderna.step
    async def charge(self, amount: int) -> str:
        self.charges += 1
        return f"{amount} {self.currency}"

    @cuaderna.step
    def refund(self, amount: int) -> str:
        return f"-{amount} {self.currency}"

    @classmethod
    @cuaderna.step
    async def describe(cls, label: str) -> str:
        return f"{cls.__name__}:{label}"


shop = Shop("EUR")


@cuaderna.run
async def checkout() -> list[str]:
    return [await shop.charge(5), await shop.refund(2), await Shop.describe("open")]


async def test_a_method_can_be_a_step_and_its_instance_is_not_recorded(
    engine: Engine, journal: Storage
) -> None:
    assert await engine.execute("run", checkout) == ["5 EUR", "-2 EUR", "Shop:open"]

    attempts = await journal.load_attempts("run")
    assert [attempt.step_key for attempt in attempts] == [
        "Shop.charge#1",
        "Shop.refund#1",
        "Shop.describe#1",
    ]
    assert [json.loads(attempt.args_json) for attempt in attempts] == [
        {"amount": 5},
        {"amount": 2},
        {"label": "open"},
    ]


async def test_a_method_step_runs_on_the_instance_it_was_called_on(engine: Engine) -> None:
    charges_before = shop.charges

    await engine.execute("run", checkout)

    assert shop.charges == charges_before + 1


async def test_a_recorded_method_step_is_not_executed_again(engine: Engine) -> None:
    await engine.execute("run", checkout)
    charges_after_the_first_execution = shop.charges

    await engine.execute("run", checkout)

    assert shop.charges == charges_after_the_first_execution
