import subprocess
import sys
from pathlib import Path

import cuaderna
from cuaderna import Engine

_STEPS = 300
_WORKER_LEASE_SECONDS = 0.5

_WORKER = """
import asyncio
import sys

from tests.test_crash import count_to

from cuaderna import Engine


async def main(journal: str, effects: str) -> None:
    engine = Engine(journal, lease_seconds=float(sys.argv[4]))
    await engine.execute("run", count_to, effects, int(sys.argv[3]))


asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


@cuaderna.step
def leave_a_mark(effects: str, number: int) -> int:
    with Path(effects).open("a") as file:
        file.write(f"{number}\n")
    print(number, flush=True)
    return number


@cuaderna.run
async def count_to(effects: str, limit: int) -> int:
    total = 0
    for number in range(limit):
        total += await leave_a_mark(effects, number)
    return total


async def test_a_killed_run_resumes_and_repeats_at_most_the_step_in_flight(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.db"
    effects = tmp_path / "effects.txt"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WORKER,
            str(journal),
            str(effects),
            str(_STEPS),
            str(_WORKER_LEASE_SECONDS),
        ],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert worker.stdout is not None
    for _ in range(10):
        worker.stdout.readline()
    worker.kill()
    worker.wait()
    worker.stdout.close()
    marks_before_resume = effects.read_text().split()

    engine = Engine(journal)
    try:
        total = await engine.execute("run", count_to, str(effects), _STEPS)
    finally:
        await engine.close()

    marks = [int(mark) for mark in effects.read_text().split()]
    assert total == sum(range(_STEPS))
    assert 10 <= len(marks_before_resume) < _STEPS
    assert sorted(set(marks)) == list(range(_STEPS))
    assert len(marks) - _STEPS <= 1
