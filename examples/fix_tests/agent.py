import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel, JsonValue

import cuaderna

_SAMPLE = Path(__file__).parent / "sample"
_MODEL_URL = "http://localhost:11434/api/chat"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_EDIT_TOOL = "write_file"
_PRICES = Path(__file__).parent / "prices.json"
_LEASE_SECONDS = 5.0
_PUBLISHED_WORKDIR = "/tmp/fix-tests"
_INSTRUCTIONS = (
    "You fix failing Python tests. You get the source files and the pytest output. "
    "Answer with the path of one source file and its complete corrected content. "
    "Never change the test files."
)


class TestReport(BaseModel):
    passed: bool
    output: str


class FileEdit(BaseModel):
    path: str
    content: str


class Summary(BaseModel):
    fixed: bool
    rounds: int


@cuaderna.step
def run_tests(repo: str) -> TestReport:
    print("  running the tests")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    return TestReport(passed=completed.returncode == 0, output=completed.stdout[-4000:])


@cuaderna.step
def read_sources(repo: str) -> dict[str, str]:
    print("  reading the sources")
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(_editable_files(Path(repo)))
    }


@cuaderna.step(max_attempts=3)
def ask_model(provider: str, model: str, sources: dict[str, str], test_output: str) -> FileEdit:
    print(f"  asking {model}")
    listing = "\n\n".join(f"# {name}\n{content}" for name, content in sources.items())
    user = f"{listing}\n\n# pytest output\n{test_output}"
    if provider == "ollama":
        return _ask_ollama(model, _INSTRUCTIONS, user)
    if provider == "anthropic":
        return _ask_anthropic(model, _INSTRUCTIONS, user)
    raise cuaderna.TerminalError(f"no such provider: {provider!r}")


def _ask_ollama(model: str, instructions: str, prompt: str) -> FileEdit:
    body: dict[str, JsonValue] = {
        "model": model,
        "stream": False,
        "format": FileEdit.model_json_schema(),
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prompt},
        ],
    }
    reply = _post_json(_MODEL_URL, {}, body)
    cuaderna.annotate(cuaderna.usage.from_ollama(reply, requested_model=model))
    return FileEdit.model_validate_json(reply["message"]["content"])


def _ask_anthropic(model: str, instructions: str, prompt: str) -> FileEdit:
    body: dict[str, JsonValue] = {
        "model": model,
        "max_tokens": 2048,
        "system": instructions,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {
                "name": _EDIT_TOOL,
                "description": "Write the corrected content of one source file.",
                "input_schema": FileEdit.model_json_schema(),
            }
        ],
        "tool_choice": {"type": "tool", "name": _EDIT_TOOL},
    }
    headers = {
        "anthropic-version": "2023-06-01",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
    }
    reply = _post_json(_ANTHROPIC_URL, headers, body)
    cuaderna.annotate(cuaderna.usage.from_anthropic(reply, requested_model=model))
    for block in reply["content"]:
        if block["type"] == "tool_use":
            return FileEdit.model_validate(block["input"])
    raise ValueError("the model answered without the file edit")


def _post_json(url: str, headers: dict[str, str], body: dict[str, JsonValue]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


@cuaderna.step
def write_file(repo: str, edit: FileEdit) -> None:
    print(f"  writing {edit.path}")
    target = Path(repo) / edit.path
    if target not in _editable_files(Path(repo)):
        raise cuaderna.TerminalError(f"the model asked to write {edit.path!r}, which is off limits")
    target.write_text(edit.content, encoding="utf-8")


@cuaderna.run
async def fix_tests(repo: str, provider: str, model: str, max_rounds: int) -> Summary:
    report = await run_tests(repo)
    rounds = 0
    while not report.passed and rounds < max_rounds:
        sources = await read_sources(repo)
        edit = await ask_model(provider, model, sources, report.output)
        await write_file(repo, edit)
        report = await run_tests(repo)
        rounds += 1
    return Summary(fixed=report.passed, rounds=rounds)


def _editable_files(repo: Path) -> list[Path]:
    return [path for path in repo.glob("*.py") if not path.name.startswith("test_")]


@dataclass(frozen=True, slots=True)
class _Options:
    run_id: str
    workdir: Path
    provider: str
    model: str
    max_rounds: int
    budget: str | None
    export: Path | None


async def _main(options: _Options) -> None:
    if options.provider == "anthropic" and "ANTHROPIC_API_KEY" not in os.environ:
        raise SystemExit("set ANTHROPIC_API_KEY to use --provider anthropic")
    repo = options.workdir / "repo"
    if not repo.exists():
        shutil.copytree(_SAMPLE, repo)
    engine = cuaderna.Engine(
        options.workdir / "journal.db",
        lease_seconds=_LEASE_SECONDS,
        prices=cuaderna.PriceTable.load(_PRICES),
    )
    run = fix_tests if options.budget is None else fix_tests.with_budget(options.budget)
    try:
        summary = await engine.execute(
            options.run_id, run, str(repo), options.provider, options.model, options.max_rounds
        )
        if options.export is not None:
            await engine.export_run(
                options.run_id, options.export, redact=partial(_hide_local_paths, options.workdir)
            )
        cost = await engine.cost(options.run_id)
    except (cuaderna.BudgetExceededError, cuaderna.CostUnknownError) as error:
        print(f"stopped: {error}")
        return
    finally:
        await engine.close()
    print(f"cost: {cost.total_usd} USD, {cost.unknown_attempts} attempts of unknown cost")
    print(f"fixed: {summary.fixed}, rounds: {summary.rounds}")


def _hide_local_paths(workdir: Path, _field: str, value: JsonValue) -> JsonValue:
    text = json.dumps(value)
    replacements = (
        (workdir / "repo", f"{_PUBLISHED_WORKDIR}/repo"),
        (workdir, _PUBLISHED_WORKDIR),
        (Path(sys.prefix), "<venv>"),
        (Path(sys.base_prefix), "<python>"),
    )
    for local_path, published_path in replacements:
        text = text.replace(json.dumps(str(local_path))[1:-1], published_path)
    return json.loads(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix the failing tests of a small sample repo.")
    parser.add_argument("workdir", type=Path, help="where the repo copy and the journal live")
    parser.add_argument("--run-id", default="fix-tests")
    parser.add_argument("--provider", choices=["ollama", "anthropic"], default="ollama")
    parser.add_argument("--model", default="gpt-oss:20b")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--budget", help="stop the run when it has spent this many USD")
    parser.add_argument("--export", type=Path, help="write the finished run to this file")
    arguments = parser.parse_args()
    asyncio.run(
        _main(
            _Options(
                arguments.run_id,
                arguments.workdir,
                arguments.provider,
                arguments.model,
                arguments.max_rounds,
                arguments.budget,
                arguments.export,
            )
        )
    )
