# cuaderna

A durable, replayable execution engine for AI agents, written in Python.

A *cuaderna* is a rib of a ship's hull: the structure underneath that holds everything else up.

## What it is for

An agent solves a task in many steps: it reads files, calls a model, runs commands, calls the model again. When the process dies halfway, the work and the money spent on it are gone, and nobody can say afterwards what the agent did or why.

Cuaderna is the layer underneath that code. It is not an agent and it is not a framework for writing agents. You keep writing your agent as plain Python and mark the steps that matter. Every step is written to a journal with its result as soon as it finishes, and everything else is built on that journal:

- **Resume after a crash.** A run that dies picks up after the last recorded step. A recorded step is never executed again.
- **Replay.** Any past run can be walked through step by step with the model responses it got at the time, without calling the model. A recorded run becomes a test.
- **Cost limits.** Every model call is stored with what it cost. A run has a budget and stops when it reaches it, without losing what it did: give it a higher budget and it continues.
- **Permissions and audit.** Tools run with explicit permissions and every call is recorded. *Not built yet.*
- **Fault injection.** Recorded runs are used to test how an agent reacts to a tool that fails or a malformed response. *Not built yet.*

## A first run

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

Kill it while it works and run it again: it continues from the last step it recorded. Run it once more after it finished and it returns the stored result without executing anything.

## See it

[examples/fix_tests](examples/fix_tests/README.md) is a complete agent that fixes the failing tests of a small repo. [The run page](docs/run-page.md) is a static page that shows a recorded run of it: one process killed during its model call, a second one stopped by its budget, and a third one that finished the run, with what each step received, returned and cost.

## Documentation

- [Writing steps and runs](docs/usage.md): steps, retries, resume, replay, exporting a run as a test case, and what the journal holds.
- [Cost and budgets](docs/cost.md): the price table, the cost of each model call, stopping a run at a budget, and what the same run would have cost on another model.
- [The run page](docs/run-page.md): looking at a recorded run in the browser.

## Install

```
pip install git+https://github.com/cuaderna/cuaderna
```

Requires Python 3.12 or newer. The project is in early development and the package on PyPI is still a placeholder, so for now it is installed from this repository.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks to run. Issues and questions are welcome.

## License

Apache-2.0. See [LICENSE](LICENSE).
