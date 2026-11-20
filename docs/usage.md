# Writing steps and runs

How to mark steps, what a resume and a replay do, and what the journal holds.

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
- "Exactly once" is not promised. Inside a step, `cuaderna.idempotency_key()` returns a key that stays the same across retries and resumes, to pass to an external system that can discard a repeated request. Model APIs do not document that kind of deduplication, so the one call that was in flight during a crash can be charged twice. A lease can also be lost under a live step, so the key matters for effects outside the journal even without a crash: a step must not block the event loop for longer than `lease_seconds`, because the heartbeat that renews the lease runs on that loop. When the renewal finds the lease taken, `execute` cancels the run and raises `LeaseLostError`, but a step that does not give control back cannot be cancelled and may run again in another process.
- A journal that another process keeps write-locked makes a write wait, then raises `JournalBusyError`. The run is not marked failed: it stays unfinished and the next `execute` resumes it. On POSIX the journal and the files from `export_run` are created readable by their owner only (mode 0600), because they hold prompts and tool output.
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

`import_run` refuses a run id the journal already has unless `as_run_id=` gives it another one. An imported run is marked with `imported_at` and can be read and replayed, never executed, whether the file said it finished or not. A file larger than `max_bytes` (64 MiB by default) or not valid for this library is refused with `RunFileError`, and a refused import leaves nothing in the journal.

The journal file and its `-wal` file hold everything in plain text: prompts, tool output, error messages, whatever a step received or returned, including a secret if one was passed as an argument. Read secrets from the environment inside the step instead of passing them around. `pending()` and `audit()` return arguments because a reviewer needs them. The repr of a record and the messages of the run and step mismatch errors and of `RunFailedError` carry no recorded values; read them through `engine.run_record` and `engine.steps`. A `StepFailedError` still carries the text of the exception the step raised, so keep secrets out of exception messages.

Before publishing a run file, pass `redact=` to `export_run`, a function `(path, value) -> value` that is called with `"input"`, `"output"`, `"error"` and `"<step key>.args"`, `.result`, `.error` and `.annotations`, and read the result yourself. A redact that covers only `.args` and `.result` leaves `.error` and `.annotations`. A redacted file carries no hashes, because the hash of a short value can be guessed offline, so it cannot be verified.

The journal is a SQLite file. It has to be on a local disk, not on a network drive.