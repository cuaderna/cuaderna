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

## What a run cost

cuaderna ships no prices. Providers change them by announcement, and an amount stored in a journal should say whose numbers produced it, so the price table is a file you write and keep next to your code:

```json
{
  "format": "cuaderna.prices/1",
  "id": "2026-11-19",
  "currency": "USD",
  "models": [
    {
      "provider": "anthropic",
      "names": ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
      "rates": {
        "input": "1",
        "output": "5",
        "cache_read": "0.10",
        "cache_write": {"5m": "1.25", "1h": "2"},
        "requests": {"web_search": "10"}
      },
      "variants": [
        {"when": {"service_tier": "batch"}, "rates": {"input": "0.5", "output": "2.5"}}
      ]
    }
  ]
}
```

Token rates are USD per million tokens and request rates USD per thousand requests, written as strings so nothing goes through a float. A model is found by its exact provider and name, with the name in the response tried before the one that was requested. A variant replaces the whole set of rates when everything in its `when` holds: any fact the mapper found in the response (`service_tier`, `speed`, `inference_geo`), or `input_tokens_over` for long-context prices. The first variant that matches wins.

```python
prices = cuaderna.PriceTable.load("prices.json")
engine = cuaderna.Engine("cuaderna.db", prices=prices)

await engine.execute("fix-123", fix_tests, "path/to/repo")

cost = await engine.cost("fix-123")
print(cost.total_usd, cost.unknown_attempts)
for attempt in cost.attempts:
    print(attempt.step_key, attempt.attempt, attempt.usd, attempt.parts)
```

- The cost is computed once, when the attempt is recorded, and stored with it under `cuaderna.cost` together with the `id` of the table and a hash of its content. Editing the table later does not change what past runs cost.
- Failed attempts count. A call that was paid for and then failed to parse is priced as long as `annotate` ran before the exception.
- A model call that cannot be priced has unknown cost, never zero: the table does not have the model, a rate is missing for tokens the call used, or the response carried no usage. `unknown_attempts` counts them, each `AttemptCost` has the `reason`, and `total_usd` adds up only what is known. A model call recorded by an engine with no table is unknown too.
- A cost you already know wins over the table. `cuaderna.annotate(cuaderna.usage.reported_cost("0.0125", "returned by the gateway"))` stores it as reported, which also covers a step that pays for something that is not a model.
- One model call per step. `annotate` replaces keys, so a step that makes two calls records the usage of the last one.
- `engine.cost` reads what the journal holds and needs no table, so it works on an imported run.

### A ceiling for a run

```python
@cuaderna.run(budget_usd="2.00")
async def fix_tests(repo: str) -> Summary: ...


try:
    await engine.execute("fix-123", fix_tests, "path/to/repo")
except cuaderna.BudgetExceededError as stop:
    print(stop.spent_usd, stop.budget_usd)
    await engine.execute("fix-123", fix_tests.with_budget("5.00"), "path/to/repo")
```

- The check is before every step attempt, retries included. Once the run has spent its budget or more, the step call raises `BudgetExceededError` and no attempt is recorded.
- Spent is the sum of the cost of every attempt in the journal, failed ones and those of earlier workers included. It is read from the journal when the run is claimed, so a crash does not reset it.
- The run is not failed. It stays unfinished with its journal, and executing it again with a higher budget continues from where it stopped and pays only for what is missing. With the same budget it raises again before running any step.
- Catching the error inside the run function does not get past it: every later step call raises it again, and `execute` raises it when the function ends.
- An unknown cost stops the run too. A ceiling that skips what it cannot price is worse than none, so with a budget set an attempt of unknown cost makes the next step call raise `CostUnknownError`, which says which attempt it was and why. The run stays unfinished in the same way. When it is executed again, attempts of unknown cost are priced with the table the engine has now, from the usage stored with them, so adding the missing model to the table and executing again is the whole fix. The journal keeps what was recorded: `engine.cost` still shows that attempt as unknown. Without a budget nothing stops.
- Nothing is reserved before a call, because the size of an answer is not known beforehand. A run can go past its ceiling by the cost of the steps that were already in flight when it was crossed: one, or as many as it started together with `asyncio.gather`. A call in flight when the process dies leaves no attempt in the journal, so it was paid and is not in the sum.
- `with_budget` returns the same run with another ceiling, for limits that depend on who asks. A budget needs an engine with a price table; without one `execute` raises `InvalidDefinitionError`.

### What another model would have cost

```python
cost = await engine.cost("fix-123")
on_a_larger_model = await engine.reprice("fix-123", provider="anthropic", model="claude-opus-4-5")
print(cost.total_usd, on_a_larger_model.total_usd)
```

`reprice` returns the same shape as `cost`, with every model call of the run priced at the base rates of another model in the engine's table, from the usage stored with each attempt. The journal is not touched. Each of those attempts has the basis `repriced`, and a model the table does not have raises `PriceTableError`.

It is an estimate, and a rough one:

- The same text is not the same number of tokens on another model. Tokenizers differ between providers and between versions of one provider.
- A cache hit on one model is not a hit on another. Cached tokens are priced at the other model's cache rates as if the cache had behaved the same, and a call that used a kind of token or request the other model has no rate for comes back as unknown.
- No variant is applied: batch, priority and long-context prices of the other model are ignored.
- Another model would have answered something else, and the run would have taken other steps.
- A step that is not a model call keeps the cost it reported.

## Look at a run

[site](site) is a static page that shows a run file: the attempts in the order they were recorded, grouped by the process that wrote them, with the arguments, result, error and annotations of each one. It has no build step and no server side. Serve the folder and open it:

```
python -m http.server --directory site
```

It opens on [site/sample-run.json](site/sample-run.json), a real run of [examples/fix_tests](examples/fix_tests), a complete agent that asks a model to fix failing tests, recorded against `claude-haiku-5-5` with the price table in [examples/fix_tests/prices.json](examples/fix_tests/prices.json). Three processes wrote it:

- The first one had a budget of 0.00005 USD and was killed 0.4 seconds into its model call. It left two attempts. The call that was executing left no record, which is the one call the section on budgets says can be paid and not counted.
- The second one, with the same budget, took the run over 4.7 seconds later, made the model call again and recorded it at 0.0001764 USD. That is over the budget, so its next step call raised `BudgetExceededError` and the run stayed unfinished. The run went 0.0001264 USD past its ceiling, the cost of the one call in flight when it was crossed.
- The third one was given 0.01 USD and finished the run with two more attempts. The model call was not made again.

`engine.reprice` puts the same tokens on `claude-opus-5-5` at 0.007056 USD, 40 times what the run cost. The budgets are not in the file: a budget belongs to the call to `execute`, not to the journal.

Any other run file can be picked or dropped on the page. It is read in the browser and not uploaded.

Two numbers from that run, measured on the machine that recorded it:

- Its five attempts, with source code and pytest output among their arguments and results, make a file of 6,906 bytes, about 1.4 kB per attempt.
- Live, the run took 6.9 seconds from start to finish, 4.7 of them between the last attempt of the killed process and the first of the next one, most of it waiting for the dead process's lease. Replaying it from the journal takes 0.2 milliseconds (median of 200 replays), because no step executes.

## Install

```
pip install git+https://github.com/cuaderna/cuaderna
```

Requires Python 3.12 or newer.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks to run. Issues and questions are welcome.

## License

Apache-2.0. See [LICENSE](LICENSE).
