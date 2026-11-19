# cuaderna

A durable, replayable execution engine for AI agents, written in Python.

A *cuaderna* is a rib of a ship's hull: the structure underneath that holds everything else up.

## Status

Early development. The first goal below, resuming after a crash, is implemented on `main`. The package on PyPI is still a placeholder, so for now it has to be installed from this repository.

## What it is

An agent solves a task in many steps: it reads files, calls a model, runs commands, calls the model again. Cuaderna is the layer underneath that code. It is not an agent and it is not a framework for writing agents. You keep writing your agent as plain Python and mark the steps that matter.

Every step is written to a journal with its result as soon as it finishes. Everything else is built on that journal:

- **Resume after a crash.** A run that dies picks up after the last recorded step. A recorded step is never executed again; the step that was in progress when the process died is executed again.
- **Replay.** Any past run can be walked through step by step with the model responses it got at the time, without calling the model.
- **Cost limits.** Each run has a budget, stops when it exceeds it, and reports what every step cost.
- **Permissions and audit.** Tools run with explicit permissions and every call is recorded.
- **Fault injection.** Recorded runs can be used to test how an agent reacts to a tool that fails or a malformed response.

These are goals, listed in the order they will be built. Only the first one is implemented.

## Usage

```python
import asyncio

import cuaderna


@cuaderna.step(max_attempts=3)
async def ask_model(prompt: str) -> str: ...  # call whichever SDK you use


@cuaderna.step
def run_tests(repo: str) -> bool: ...  # a plain function runs in a thread


@cuaderna.run
async def fix_tests(repo: str) -> int:
    rounds = 0
    while not await run_tests(repo):
        await ask_model(f"fix the tests in {repo}")
        rounds += 1
    return rounds


async def main() -> None:
    engine = cuaderna.Engine("cuaderna.db")
    print(await engine.execute("fix-123", fix_tests, "path/to/repo"))
    await engine.close()


asyncio.run(main())
```

Running it again with the same run id resumes the run if it did not finish, and returns the stored result if it did. Arguments and results are stored as JSON, so every parameter and return value of a step needs a type annotation.

The code between steps runs again on every resume and has to decide the same thing each time:

- Read the clock, random numbers and new ids through `await cuaderna.now()`, `await cuaderna.random()` and `await cuaderna.new_id()`, which are recorded like any other step.
- "Exactly once" is not promised. Inside a step, `cuaderna.idempotency_key()` returns a key that stays the same across retries and resumes, to pass to an external system that can discard a repeated request. Model APIs do not document that kind of deduplication, so the one call that was in flight during a crash can be charged twice.
- After a change that makes old runs take a different path, set `@cuaderna.run(code_version="2")`. An unfinished run started by another version is refused instead of replayed.

The journal is a SQLite file. It has to be on a local disk, not on a network drive.

[examples/fix_tests](examples/fix_tests) is a complete agent that can be killed and resumed.

## Install

```
pip install git+https://github.com/cuaderna/cuaderna
```

Requires Python 3.12 or newer.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks to run. Issues and questions are welcome.

## License

Apache-2.0. See [LICENSE](LICENSE).
