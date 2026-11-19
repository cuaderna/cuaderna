# fix_tests

A small agent that makes the failing tests of a sample repo pass by asking a model for a corrected file. It is written to show three things: a run surviving a kill, a run stopped by its budget and continued with a higher one, and what each model call cost. Every step is recorded in a journal, so a restarted run continues from where it stopped.

## Requirements

- The repo set up with `uv sync`.
- A model, one of:
  - Ollama running on `localhost:11434` with a model pulled. This is the default, with `gpt-oss:20b`; any model that supports structured output works, selected with `--model`.
  - An Anthropic API key in the `ANTHROPIC_API_KEY` environment variable, with `--provider anthropic --model claude-haiku-5-5`. The agent calls the Messages API with `urllib`, so there is no SDK to install.

## Run it

```
uv run python examples/fix_tests/agent.py /tmp/fix-tests --provider anthropic --model claude-haiku-5-5
```

Output:

```
  running the tests
  reading the sources
  asking claude-haiku-5-5
  writing pricing.py
  running the tests
cost: 0.0001764 USD, 0 attempts of unknown cost
fixed: True, rounds: 1
```

The directory given holds a `repo/` copy of `sample/` (the repo being fixed) and `journal.db`. A line is printed only when a step really executes. How many rounds it takes depends on the model: `gpt-oss:20b` fixed one bug per round and needed two.

## What it cost

The agent loads [prices.json](prices.json) and gives it to the engine, so every model call is stored with its cost. The table was written by hand on 2026-11-19 from https://platform.claude.com/docs/en/about-claude/pricing: Claude Haiku 5.5, with its higher prices for prompts over 100,000 tokens as a variant, and Claude Opus 5.5 to compare against. `gpt-oss:20b` on Ollama is there at zero, because a local model has no price per token. Prices change; check them before you trust the numbers.

A model that is not in the table has unknown cost, and the last line says how many attempts that happened to.

## Kill it and run it again

While the process is waiting on the model, stop it with Ctrl+C or `kill -9`. Then run the exact same command again. The second process prints:

```
  asking claude-haiku-5-5
  writing pricing.py
  running the tests
cost: 0.0001764 USD, 0 attempts of unknown cost
fixed: True, rounds: 1
```

The two steps already recorded were not executed again; their results came from the journal. Only the model call that was in flight was repeated. This means one model call may be paid twice, and the first of the two is not in the cost, because an attempt that was executing when the process died leaves no record. The second process also waits up to 5 seconds (the lease length set in the example) for the dead process's lease to expire before it continues.

Running the command once more after it finished returns the stored result without executing anything.

## Give it a budget

`--budget` is a ceiling in USD for the run. With one too small for a single model call:

```
uv run python examples/fix_tests/agent.py /tmp/fix-tests --provider anthropic --model claude-haiku-5-5 --budget 0.00005
```

```
  running the tests
  reading the sources
  asking claude-haiku-5-5
stopped: run 'fix-tests' has spent 0.0001764 USD and its budget is 0.00005 USD
```

The call that crossed the ceiling was made and recorded; the step after it was not. The run is not failed. The same command with `--budget 0.01` continues it and pays for nothing that is already in the journal:

```
  writing pricing.py
  running the tests
cost: 0.0001764 USD, 0 attempts of unknown cost
fixed: True, rounds: 1
```

## Export the run

Add `--export run.json` to write the finished run as one file:

```
uv run python examples/fix_tests/agent.py /tmp/fix-tests --export run.json
```

The directory given on the command line is replaced by `/tmp/fix-tests` everywhere in the file, and the paths of the virtual environment and of the Python installation, which show up in pytest tracebacks, by `<venv>` and `<python>`. Everything else is there in plain text: the sources, the pytest output, what the model answered. The API key is read from the environment inside the step and is never an argument, so it is not in the journal or in the file.

[site/sample-run.json](../../site/sample-run.json) was written this way from one run and three processes: the first, with a budget of 0.00005 USD, was killed 0.4 seconds into its model call; the second, with the same budget, made the call again and was stopped by the budget; the third, with 0.01 USD, finished. `tests/test_sample_run.py` imports it and replays it against this agent, with no model and no repo.

## What to look at in the code

- Reading the sources is a step because it is input that can change between executions, so its result is recorded rather than recomputed.
- `ask_model` has `max_attempts=3`, so a transient failure of the model call is retried before the run fails.
- `ask_model` calls `cuaderna.annotate` with what `cuaderna.usage.from_anthropic` or `from_ollama` makes of the response, right after it arrives and before parsing the answer, so a call that was paid for and then failed to parse still has its cost.
- `write_file` writes the whole file, so repeating it is harmless. It refuses paths outside the editable files with `TerminalError`, so a bad model answer is not retried.
- `fix_tests` is plain Python with a loop; the durability comes from the decorators on the steps, not from a separate workflow language.
