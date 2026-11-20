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
cost: 0.0001758 USD, 0 attempts of unknown cost
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
cost: 0.0001758 USD, 0 attempts of unknown cost
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
stopped: run 'fix-tests' has spent 0.0001758 USD and its budget is 0.00005 USD
```

The call that crossed the ceiling was made and recorded; the step after it was not. The run is not failed. The same command with `--budget 0.01` continues it and pays for nothing that is already in the journal:

```
  writing pricing.py
  running the tests
cost: 0.0001758 USD, 0 attempts of unknown cost
fixed: True, rounds: 1
```

## Let a person approve an edit

The run carries the policy in [policy.json](policy.json). The look-only steps are allowed and `write_file` is allowed only for the source file of the sample, `pricing.py`, by its exact name. A name with `test` in it in any spelling, and any `.ini`, `.cfg`, `.toml` or `.pth` file or `sitecustomize.py`, needs a person, because pytest would run or read it. Nothing else is named, so any other file, a new one included, is denied. Because a deny wins over an ask and an ask over an allow, a name that matches an ask rule is never allowed.

When the model proposes an edit the policy denies, the run does not stop: the reason is appended to the pytest output the next `ask_model` call receives, so the model can try another file. That text is an argument of the step, so it is recorded and the run stays the same on resume.

When the model proposes an edit to a test file, the run stops with `ApprovalRequiredError`. The command line prints the step and its arguments and asks:

```
approval needed for write_file#1 with {"edit":{"content":"...","path":"test_pricing.py"},"repo":"/tmp/fix-tests/repo"}
  approve? [y/N]
```

`y` approves it; anything else rejects it, and a rejection reaches the run as a denial that the model is told about. The answer is recorded under the name given with `--approver`, which is your OS user name by default, and the run continues in the same process. `--actor` (default `fix-tests-example`) is the name the engine records for every step the run executes. Both are recorded claims, not verified identities.

## What running the tests means

Running the tests runs code the model wrote: pytest imports the file it just wrote. It runs as your user, in a child process whose environment holds only a short list of names (`PATH`, the home and temp directories and what Python needs to start), so `ANTHROPIC_API_KEY` is not in it. There is no other isolation: the code can read your files and use the network. For a repo you do not trust, use a container or a throwaway machine.

## Export the run

Add `--export run.json` to write the finished run as one file:

```
uv run python examples/fix_tests/agent.py /tmp/fix-tests --export run.json
```

The directory given on the command line is replaced by `/tmp/fix-tests` everywhere in the file, and the paths of the virtual environment and of the Python installation, which show up in pytest tracebacks, by `<venv>` and `<python>`. Everything else is there in plain text: the sources, the pytest output, what the model answered. The API key is read once at the start, stripped of surrounding whitespace and refused if it holds anything but printable characters, so a malformed key stops the command before a run starts. It is never an argument of a step, so it is not in the journal or in the file.

The chain hashes of a run cover the values as recorded, so a file with hidden paths cannot be checked with `verify`. `--keep-paths` exports the values as recorded and the file verifies; use it only where the paths say nothing you want to keep private, such as a work directory and a Python installation under neutral paths.

[site/sample-run.json](../../site/sample-run.json) was written with `--keep-paths` from one run and three processes: the first, with a budget of 0.00005 USD, was killed during its model call; the second, with the same budget, made the call again and was stopped by the budget; the third, with 0.01 USD, finished. `tests/test_sample_run.py` imports it and replays it against this agent, with no model and no repo.

## What to look at in the code

- Reading the sources is a step because it is input that can change between executions, so its result is recorded rather than recomputed.
- `ask_model` has `max_attempts=3`, so a transient failure of the model call is retried before the run fails.
- `ask_model` calls `cuaderna.annotate` with what `cuaderna.usage.from_anthropic` or `from_ollama` makes of the response, right after it arrives and before parsing the answer, so a call that was paid for and then failed to parse still has its cost.
- `write_file` writes the whole file, so repeating it is harmless. Which paths it may write is the policy's call, not the step's.
- `fix_tests` is plain Python with a loop; the durability comes from the decorators on the steps, not from a separate workflow language.

## Try faults on a recorded run

[faults.json](faults.json) holds six faults for this agent: a model call that times out, a model that is down, a test run that cannot start, a wrong edit from the model, and two texts added to what the model reads. [experiments.py](experiments.py) runs them against a finished run of the agent, with the model call served from the recording and three trials of each fault:

```
uv run python -m examples.fix_tests.experiments --journal /tmp/fix-tests/journal.db --source fix-tests --workdir /tmp/fix-tests --out /tmp/fix-tests-experiments
```

`--workdir` is the directory the source run was made in, because its repo is where the steps that execute run again. `--trials` sets how many trials each fault gets. It runs a control first and stops if the control does not follow the recording. It prints one line per run with the fault, the trial, the step where it fired, where the run first had to execute something again, how many attempts came from the recording and how many executed, how the run ended, what it cost and what the injected text did. It then writes each run as a file in `--out`, with local paths hidden unless you pass `--keep-paths` (which keeps the values as recorded, so `verify` can check the files), and a `runs.json` index that [the run page](../../docs/run-page.md) reads.

Faults that make the model run again need the same model access the source run had, for example `ANTHROPIC_API_KEY` for a run recorded with `--provider anthropic`. What the published recordings show is in [the run page](../../docs/run-page.md#the-experiments).

[gives_up.py](gives_up.py) is the same agent with one change: when a step fails for good, it ends the run with a summary that says which step failed and how, instead of raising. `--agent gives-up` reruns the faults with it, and `--fault` picks which faults run, so the two that end the first agent's run can be tried without any model access:

```
uv run python -m examples.fix_tests.experiments --journal /tmp/fix-tests/journal.db --source fix-tests --workdir /tmp/fix-tests --out /tmp/fix-tests-gives-up --agent gives-up --fault model-down --fault tests-unavailable
```

Its runs are named `fix-tests.gives-up.control` and `fix-tests.gives-up.<fault>.<trial>`. A recording of the first agent failing on `model-down`, replayed against `gives_up.fix_tests`, raises `ReplayDivergedError`: the run that failed now returns. That is the regression signal [docs/faults.md](../../docs/faults.md#keeping-an-experiment) describes, and it costs nothing to run.

[compare.py](compare.py) puts two runs of the plan side by side, fault by fault: it imports the files a `runs.json` lists into a temporary journal and prints, for each fault, how many trials ended fixed, not fixed, gave up or failed, how many diverged, how many attempts executed and what they cost, before and after. When an index holds runs of both agents, `--before-agent` and `--after-agent` pick which. The published runs compare the first agent with the one that gives up:

```
uv run python -m examples.fix_tests.compare --before site/runs.json --before-agent agent --after site/runs.json --after-agent gives-up
```

On `model-down` the first agent failed with `ConnectionError` in 3 of 3 trials and the second gave up in 3 of 3. Faults that only one side ran show `-` on the other. It refuses two sets that come from different versions of the plan, and it gives counts, not a verdict: what a difference in three trials can mean is in [docs/faults.md](../../docs/faults.md#comparing-two-runs-of-a-plan).
