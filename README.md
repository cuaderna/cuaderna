# cuaderna

A durable, replayable execution engine for AI agents, written in Python.

A *cuaderna* is a rib of a ship's hull: the structure underneath that holds everything else up.

## Status

Early development. The package on PyPI is a placeholder and there is nothing to use yet.

## What it is

An agent solves a task in many steps: it reads files, calls a model, runs commands, calls the model again. Cuaderna is the layer underneath that code. It is not an agent and it is not a framework for writing agents. You keep writing your agent as plain Python and mark the steps that matter.

Every step is written to a journal with its result as soon as it finishes. Everything else is built on that journal:

- **Resume after a crash.** A run that dies picks up after the last recorded step, so finished steps are not executed or paid for again.
- **Replay.** Any past run can be walked through step by step with the model responses it got at the time, without calling the model.
- **Cost limits.** Each run has a budget, stops when it exceeds it, and reports what every step cost.
- **Permissions and audit.** Tools run with explicit permissions and every call is recorded.
- **Fault injection.** Recorded runs can be used to test how an agent reacts to a tool that fails or a malformed response.

These are goals, listed in the order they will be built. None of them is implemented yet.

## Install

```
pip install cuaderna
```

Requires Python 3.12 or newer.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks to run. Issues and questions are welcome.

## License

Apache-2.0. See [LICENSE](LICENSE).
