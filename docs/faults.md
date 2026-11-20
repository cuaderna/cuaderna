# Fault injection

Run a recorded run again, change one thing in what a step gives back, and read what the agent did about it.

A rerun executes the run function again from the start, as a new run with the input of a recorded one. Steps you list in `reuse` get the answer that was recorded instead of executing, so the model is not called again. A fault changes one call of one step. The rerun is a new run in the same journal, and the source is not touched.

## A fault plan

```json
{
  "format": "cuaderna.faults/1",
  "id": "flaky-model",
  "faults": [
    {
      "id": "model-times-out",
      "at": "ask_model",
      "raise": {"type": "TimeoutError", "message": "model timed out", "attempts": 1}
    }
  ]
}
```

`FaultPlan.load("faults.json")` reads at most `max_bytes` (1 MiB by default). `FaultPlan(document)` takes the same thing as a dict. Both raise `FaultPlanError` for a file that is larger, unreadable or not JSON, and for a document that is not a plan: another `format`, a key this page does not list, an `id` that is empty or used twice, or a fault that is not exactly one of the three kinds. `plan.faults` is the tuple of faults in file order, and `plan.content_hash` is the SHA-256 of the plan, which does not depend on the order of keys.

`at` is a step name, such as `ask_model`, which targets every call of that step, or a step key, such as `ask_model#2`, which targets one call. It cannot be empty or contain `:`.

| Kind | What it does | Fields |
| --- | --- | --- |
| `raise` | The attempt fails with an exception and the step does not run. | `type` and `message`, `attempts` (fail the first N attempts of each targeted call; without it every attempt fails), `terminal` (true makes it a `TerminalError`, so the step is not retried) |
| `return` | The attempt returns the value you give, and the step does not run. | The value, any JSON, also `null`. It has to fit the return type of the step. |
| `append` | The step runs, or is served from the recording, and text is added to a string in its result. | `to`, a JSON Pointer ([RFC 6901](https://www.rfc-editor.org/rfc/rfc6901)) into the result as JSON, where `""` is the whole result, and `text` |

A `raise` fault reaches the run function the way a real failure does: after the retries the step allows, as a `StepFailedError`. The attempt is recorded with the `type` and `message` you gave. An `append` whose pointer finds no string, or a `return` that does not fit the step, fails the run with `FaultPlanError`.

## Running one

```python
import asyncio

import cuaderna
from cuaderna import FaultPlan


@cuaderna.step
async def read_repo(path: str) -> str:
    return f"files of {path}"


@cuaderna.step(max_attempts=2, backoff_seconds=0)
async def ask_model(prompt: str) -> str:
    return f"patch for {prompt}"  # a real agent calls a model here


@cuaderna.run
async def fix(path: str) -> str:
    text = await read_repo(path)
    return await ask_model(text)


async def main() -> None:
    engine = cuaderna.Engine("cuaderna.db")
    await engine.execute("source", fix, "repo")

    await engine.rerun("source", fix, as_run_id="control", reuse=[ask_model])

    fault = FaultPlan.load("faults.json").faults[0]
    await engine.rerun("source", fix, as_run_id="trial-1", reuse=[ask_model], fault=fault)
    await engine.close()


asyncio.run(main())
```

`rerun(source_run_id, run, as_run_id=..., reuse=[...], fault=None)` takes one `Fault`, not a plan. `reuse` holds step functions or step names.

- The control, a rerun without a fault, is strict to the end. A reused step gets the recorded answer while its key and arguments match the source. If the code asks for a reused step with other arguments, one the source never recorded, or one the source recorded as failed, the rerun raises `ReplayDivergedError` and the run is recorded as failed.
- With a fault, the rerun is strict until the fault fires and live after it. From then on a reused step whose arguments changed executes for real, because the agent was expected to take another path. A fault whose target is never called leaves the rerun strict.
- Steps that are not in `reuse` always execute. `cuaderna.now`, `random` and `new_id` return what the source recorded.
- The policy of the run still decides every call, a reused one too, and a denied call is not faulted. A reused attempt costs nothing, so it adds nothing to a budget.
- In the example above, `ask_model#1` has two attempts in `trial-1`. The first failed with the injected timeout and carries the annotation `cuaderna.fault` with the plan, its hash, the fault id, the kind and `executed`. The second was served from the recording and carries `cuaderna.reuse` with the source run, step key and attempt. A step cannot set either annotation: if it tries, they are dropped.
- A rerun that was stopped resumes by calling `rerun` again with the same arguments. It does not inject twice and does not repeat the attempts it recorded.
- The run is marked as an experiment: `(await engine.run_record("trial-1")).experiment` holds the source run, the `reuse` list and the plan id, plan hash and fault id. `execute` refuses an experiment with `RunNotAvailableError`. `rerun` with the same run id but another source, `reuse` list or fault raises `RunMismatchError`.

## What the library does not do for you

- Only a step whose result depends on its arguments alone may be listed in `reuse`. `run_tests(repo)` may not: it returns what the files in `repo` say now, and serving the old answer hides that the code changed.
- You restore the environment. The library snapshots nothing. The steps that execute have to return the same thing in the same state, up to the fault, and a timing in the output of a tool is enough to break that. The control is how you find out: if it diverges, the experiment would not have told you anything.
- One fault per experiment. A trial is a separate run with its own id, so ten trials are ten calls to `rerun`.
- Whether the agent recovered is your assertion. The library gives facts and no verdict.
- Whether injected text worked is read from the audit log: the agent ignored it, obeyed it and was denied, or obeyed it and the step executed.
- Not done: forking a run from a step, injecting into a first execution, probabilities, delays, hangs, several faults at once, faults inside a step, rewriting arguments, detecting or filtering injected text, and snapshots of the environment.

## Reading an experiment

```python
experiment = await engine.experiment("trial-1")
experiment.fault, experiment.fired, experiment.reused, experiment.executed, experiment.diverged_at
```

`engine.experiment(run_id)` returns an `Experiment`, a frozen dataclass with facts only:

- `run_id`, `source_run_id`, `plan` and `plan_hash` (the plan id and its hash), and `fault` (the fault id, `None` for a control).
- `fired`, the step keys where the fault was applied, each once, in journal order.
- `reused`, the number of attempts served from the recording.
- `executed`, the number of attempts on which the function of the step ran. A served attempt, an injected attempt that did not run the step and a denied attempt are not counted.
- `diverged_at`, the step key of the first attempt of a reused step whose function had to run, or `None` when the run followed the source to the end.

An unknown run raises `RunNotFoundError`, and a run that is not an experiment raises `RunNotAvailableError`. Each entry of `engine.audit(run_id)` has `injected`, the fault id or `None`, and `reused`, a boolean.

## Keeping an experiment

An experiment is exported with `engine.export_run` like any run. The marker is part of the hash chain, so `verify` finds a marker that was changed, and it is part of the file. A run file with an experiment has the format `cuaderna.run/2`, which version 0.1.0 refuses. Ordinary runs are still `cuaderna.run/1`. An imported experiment is still an experiment.

An exported experiment is also a regression test. After `import_run`, `engine.replay` executes the run function with the recorded steps, and raises `ReplayDivergedError` when the code no longer reacts to that failure the way it did. As with any replay, that tells you the path changed, not that it got worse: read the difference and decide.
