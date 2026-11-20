# Contributing

The project is at an early stage and the design is still moving, so please open an issue before starting on anything larger than a small fix. Issues labelled `good first issue` are small and self-contained; issues labelled `needs design` need an agreed design before any code.

Security problems are not reported in public issues: see [SECURITY.md](SECURITY.md).

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs a supported Python version (3.12 or newer) and the development tools:

```
uv sync
```

[Node.js](https://nodejs.org/) is optional. It is only used to run the tests of the page under `site/`, which are skipped when `node` is not installed.

## Where things are

- `src/cuaderna/`: the library. Its only runtime dependency is pydantic.
- `tests/`: the test suite, one file per behavior.
- `examples/fix_tests/`: a sample agent that uses the library, with its policy, its fault plan and the script that runs the faults.
- `site/`: a static page that shows a recorded run. No build step and no dependencies.
- `docs/`: one page per topic. The README says what the project is for and stays short; detail goes in `docs/`.

## Checks

Run these before pushing or opening a pull request:

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

`uv run ruff format` fixes formatting. Pyright runs in strict mode over `src`, `tests` and `examples`, and the test suite treats warnings as errors.

The `ci` workflow runs the same checks on every pull request and on every push to `main`, on Ubuntu with Python 3.12, 3.13 and 3.14. The first pull request of a new contributor waits for a maintainer to approve the workflow run.

## Pull requests

- One change per pull request, small enough to read in one sitting: aim for under 400 changed lines, not counting recorded files. A refactor and the change it prepares go in separate pull requests.
- Explain why the change is needed, and for a bug fix what the cause was.
- Add tests for behavior that could break without anyone noticing. A bug fix starts with a test that fails without it.
- Commit messages are one short line in the imperative that says what the change does.
- Update the page in `docs/` that describes what you changed.

## Things that are easy to get wrong

- **The journal schema only moves forward.** A change to the tables is a new entry at the end of `_MIGRATIONS` in `src/cuaderna/storage/_migrations.py`. An entry that is already there is never edited, because journals written with it exist.
- **A recorded run keeps its digest.** The hash chain covers what is recorded about a run and its attempts. A change to what is hashed makes every existing run fail `verify`, so new information goes in only when present and leaves older runs as they were.
- **The run file is read by older versions.** A file that an older library would read wrongly needs a new format version, not just a new key.
- **Files under `site/` that end in `.json` are recordings.** They are made by running the sample agent, not by hand. If a change makes `tests/test_sample_run.py` or `tests/test_sample_experiments.py` fail, say so in the pull request instead of editing the recordings.
- **Nothing recorded may hold a secret or a local path.** Step arguments and results are stored as they are. Check any journal, run file or log before attaching it to an issue.

## License

The project is licensed under [Apache-2.0](LICENSE). By opening a pull request you agree that your contribution is licensed under the same terms.
