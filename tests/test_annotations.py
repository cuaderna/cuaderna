import json

import pytest

import cuaderna
from cuaderna import Engine, StepCallError, StepFailedError


class Kill(BaseException):
    pass


class World:
    def __init__(self) -> None:
        self.failures_left = 0
        self.kill_at: str | None = None


world = World()


@pytest.fixture(autouse=True)
def fresh_world() -> None:
    world.__init__()


@cuaderna.step
async def ask_model(prompt: str) -> str:
    cuaderna.annotate({"gen_ai.request.model": "small", "gen_ai.usage.input_tokens": 12})
    cuaderna.annotate({"gen_ai.usage.input_tokens": 15, "provider.usage": {"cached": [1, 2]}})
    return prompt.upper()


@cuaderna.step
def count_words(text: str) -> int:
    cuaderna.annotate({"thread": True})
    return len(text.split())


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def ask_flaky_model(prompt: str) -> str:
    if world.failures_left > 0:
        world.failures_left -= 1
        cuaderna.annotate({"request_id": "first"})
        raise ConnectionError("try again")
    return prompt.upper()


@cuaderna.step
async def annotate_with_an_object(prompt: str) -> str:
    cuaderna.annotate({"client": object()})  # type: ignore[dict-item]
    return prompt


@cuaderna.step
async def visit(place: str) -> str:
    if world.kill_at == place:
        raise Kill(place)
    return place.upper()


async def annotations_by_attempt(engine: Engine, run_id: str) -> list[object]:
    return [
        attempt.annotations_json and json.loads(attempt.annotations_json)
        for attempt in await engine.steps(run_id)
    ]


async def test_what_a_step_annotates_is_stored_with_its_attempt(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> int:
        return await count_words(await ask_model("two words"))

    await engine.execute("run-1", flow)

    assert await annotations_by_attempt(engine, "run-1") == [
        {
            "gen_ai.request.model": "small",
            "gen_ai.usage.input_tokens": 15,
            "provider.usage": {"cached": [1, 2]},
        },
        {"thread": True},
    ]


async def test_a_failed_attempt_keeps_its_annotations_and_the_retry_starts_clean(
    engine: Engine,
) -> None:
    @cuaderna.run
    async def flow() -> str:
        return await ask_flaky_model("hello")

    world.failures_left = 1
    await engine.execute("run-1", flow)

    assert await annotations_by_attempt(engine, "run-1") == [{"request_id": "first"}, None]


async def test_annotating_only_works_inside_a_step_and_with_json_values(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> str:
        return await annotate_with_an_object("hello")

    with pytest.raises(StepCallError):
        cuaderna.annotate({"model": "small"})
    with pytest.raises(StepFailedError, match="client"):
        await engine.execute("run-1", flow)


async def test_attempts_show_which_claim_of_the_run_wrote_them(engine: Engine) -> None:
    @cuaderna.run
    async def flow() -> list[str]:
        return [await visit("a"), await visit("b")]

    world.kill_at = "b"
    with pytest.raises(Kill):
        await engine.execute("run-1", flow)
    world.kill_at = None
    await engine.execute("run-1", flow)

    first, second = [attempt.lease_token for attempt in await engine.steps("run-1")]
    assert first is not None
    assert second is not None
    assert first < second
