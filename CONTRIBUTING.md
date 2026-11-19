# Contributing

The project is at an early stage and the design is still moving, so please open an issue before starting on anything larger than a small fix.

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs the right Python version and the development tools:

```
uv sync
```

## Checks

Run these before pushing or opening a pull request. They are not run automatically.

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

`uv run ruff format` fixes formatting.

## Pull requests

Keep each pull request to one change and explain why it is needed. Add tests for behavior that could break without anyone noticing.
