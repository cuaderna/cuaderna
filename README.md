# cuaderna

A durable, replayable execution engine for AI agents, written in Python.

A *cuaderna* is a rib of a ship's hull: the structure underneath that holds everything else up.

## Status

Early development. The first two goals below, resuming after a crash and replaying a recorded run, are implemented on `main`. The package on PyPI is still a placeholder, so for now it has to be installed from this repository.

## What it is

An agent solves a task in many steps: it reads files, calls a model, runs commands, calls the model again. Cuaderna is the layer underneath that code. It is not an agent and it is not a framework for writing agents. You keep writing your agent as plain Python and mark the steps that matter.

Every step is written to a journal with its result as soon as it finishes. Everything else is built on that journal:

- **Resume after a crash.** A run that dies picks up after the last recorded step. A recorded step is never executed again; the step that was in progress when the process died is executed again.
- **Replay.** Any past run can be walked through step by step with the model responses it got at the time, without calling the model.
- **Cost limits.** Each run has a budget, stops when it exceeds it, and reports what every step cost.
- **Permissions and audit.** Tools run with explicit permissions and every call is recorded.
- **Fault injection.** Recorded runs can be used to test how an agent reacts to a tool that fails or a malformed response.

These are goals, listed in the order they will be built. The first two are implemented: a run resumes after a crash, replays from code, exports as a file, and a static page shows that file.

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
- Facts about a step that are not its result, like which model answered and how many tokens it used, go in `cuaderna.annotate({...})` from inside the step. They are stored with the attempt, also when it fails, and take no part in resume or replay. The keys are free; for model calls use the [OpenTelemetry GenAI names](https://github.com/open-telemetry/semantic-conventions-genai), where `gen_ai.usage.input_tokens` counts every input token, cached or not. Providers do not count alike (Anthropic reports cached tokens apart from `input_tokens`), so `cuaderna.usage` builds the annotations from a response: `from_anthropic`, `from_openai_responses`, `from_openai_chat` and `from_ollama` each take the response as a plain mapping, as in `cuaderna.usage.from_anthropic(response.model_dump(mode="json"), requested_model=...)`, and keep the provider's own usage object under `provider.usage`. `from_openai_chat` also reads what other services return in that format; pass `provider="openrouter"` or whichever it is, so the call is not recorded as OpenAI's. Annotate right after the response arrives, so a step that fails later still records what the call used.
- After a change that makes old runs take a different path, set `@cuaderna.run(code_version="2")`. An unfinished run started by another version is refused instead of replayed.

A method can be a step. Its `self` is not stored, so whatever the step reads from the object has to be the same on every execution, like the rest of the code between steps. A run has to be a plain function.

A recorded run can be replayed. The run function executes again with the input it had, and every step returns what the journal has for it:

```python
rounds = await engine.replay("fix-123", fix_tests)
```

No step executes, nothing is written and the run is not claimed, so it works on a finished, failed or unfinished run, also while another process is executing it. A replay is strict: it raises `ReplayDivergedError` if the code asks for a step the journal does not have or has with other arguments, if it leaves recorded steps unused, or if it returns something else than the run did. That makes a recorded run a test that fails when the agent takes another path. Only steps are replayed: the code between them runs for real, so it must not have effects of its own. The code version is not checked, because replaying old runs against new code is the point.

A run can be taken out of the journal as one JSON file and loaded into another journal, which turns a real run into a test case:

```python
await engine.export_run("fix-123", "tests/cases/fix-123.json")

run_id = await other_engine.import_run("tests/cases/fix-123.json")
assert await other_engine.replay(run_id, fix_tests) == 2
```

`import_run` refuses a run id the journal already has unless `as_run_id=` gives it another one. A run imported unfinished can be read and replayed, never resumed.

The journal and the run file hold everything in plain text: prompts, tool output, error messages, whatever a step received or returned, including a secret if one was passed as an argument. Read secrets from the environment inside the step instead of passing them around. Before publishing a run file, pass `redact=` to `export_run`, a function `(path, value) -> value` that is called with `"input"`, `"output"`, `"error"` and `"<step key>.args"`, `.result`, `.error` and `.annotations`, and read the result yourself.

The journal is a SQLite file. It has to be on a local disk, not on a network drive.

## Look at a run

[site](site) is a static page that shows a run file: the attempts in the order they were recorded, grouped by the process that wrote them, with the arguments, result, error and annotations of each one. It has no build step and no server side. Serve the folder and open it:

```
python -m http.server --directory site
```

It opens on [site/sample-run.json](site/sample-run.json), a real run of [examples/fix_tests](examples/fix_tests), a complete agent that asks a local model to fix a failing test. The process was killed two seconds into its second model call, and a second process took the run over and finished it. The page shows the six attempts of the first process, the 6.3 seconds until the second one started, and its three attempts. The model call that was executing when the first process died left no record and was made again, which is the one call the section above says can be charged twice.

Any other run file can be picked or dropped on the page. It is read in the browser and not uploaded.

Two numbers from that run, measured on the machine that recorded it:

- Its nine attempts, with source code and pytest output among their arguments and results, make a file of 21,301 bytes, about 2.4 kB per attempt.
- Live, the run took 23.9 seconds from start to finish, 6.3 of them waiting for the dead process's lease. Replaying it from the journal takes 0.5 milliseconds (median of 200 replays), because no step executes.

## Install

```
pip install git+https://github.com/cuaderna/cuaderna
```

Requires Python 3.12 or newer.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks to run. Issues and questions are welcome.

## License

Apache-2.0. See [LICENSE](LICENSE).
