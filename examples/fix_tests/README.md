# fix_tests

A small agent that makes the failing tests of a sample repo pass by asking a local model for a corrected file. It is written to show a run surviving a kill: every step is recorded in a journal, so a restarted run continues from where it stopped.

## Requirements

- Ollama running on `localhost:11434` with a model pulled. The default is `gpt-oss:20b`; any model that supports structured output works, selected with `--model`.
- The repo set up with `uv sync`.

## Run it

```
uv run python examples/fix_tests/agent.py /tmp/fix-tests
```

Output:

```
  running the tests
  reading the sources
  asking gpt-oss:20b
  writing pricing.py
  running the tests
  reading the sources
  asking gpt-oss:20b
  writing pricing.py
  running the tests
fixed: True, rounds: 2
```

The directory given holds a `repo/` copy of `sample/` (the repo being fixed) and `journal.db`. A line is printed only when a step really executes.

## Kill it and run it again

While the process is waiting on the model, stop it with Ctrl+C or `kill -9`. Then run the exact same command again. Killed during the second model call, the second process prints:

```
  asking gpt-oss:20b
  writing pricing.py
  running the tests
fixed: True, rounds: 2
```

The six steps already recorded were not executed again; their results came from the journal. Only the model call that was in flight was repeated. This means one model call may be paid twice. The second process also waits up to 5 seconds (the lease length set in the example) for the dead process's lease to expire before it continues.

Running the command once more after it finished returns the stored result without executing anything.

## What to look at in the code

- Reading the sources is a step because it is input that can change between executions, so its result is recorded rather than recomputed.
- `ask_model` has `max_attempts=3`, so a transient failure of the model call is retried before the run fails.
- `write_file` writes the whole file, so repeating it is harmless. It refuses paths outside the editable files with `TerminalError`, so a bad model answer is not retried.
- `fix_tests` is plain Python with a loop; the durability comes from the decorators on the steps, not from a separate workflow language.
