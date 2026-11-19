import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request
from functools import partial
from pathlib import Path

from pydantic import BaseModel, JsonValue

import cuaderna

_SAMPLE = Path(__file__).parent / "sample"
_MODEL_URL = "http://localhost:11434/api/chat"
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
def ask_model(model: str, sources: dict[str, str], test_output: str) -> FileEdit:
    print(f"  asking {model}")
    listing = "\n\n".join(f"# {name}\n{content}" for name, content in sources.items())
    body = {
        "model": model,
        "stream": False,
        "format": FileEdit.model_json_schema(),
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": f"{listing}\n\n# pytest output\n{test_output}"},
        ],
    }
    request = urllib.request.Request(
        _MODEL_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        reply = json.load(response)
    cuaderna.annotate(cuaderna.usage.from_ollama(reply, requested_model=model))
    return FileEdit.model_validate_json(reply["message"]["content"])


@cuaderna.step
def write_file(repo: str, edit: FileEdit) -> None:
    print(f"  writing {edit.path}")
    target = Path(repo) / edit.path
    if target not in _editable_files(Path(repo)):
        raise cuaderna.TerminalError(f"the model asked to write {edit.path!r}, which is off limits")
    target.write_text(edit.content, encoding="utf-8")


@cuaderna.run
async def fix_tests(repo: str, model: str, max_rounds: int) -> Summary:
    report = await run_tests(repo)
    rounds = 0
    while not report.passed and rounds < max_rounds:
        sources = await read_sources(repo)
        edit = await ask_model(model, sources, report.output)
        await write_file(repo, edit)
        report = await run_tests(repo)
        rounds += 1
    return Summary(fixed=report.passed, rounds=rounds)


def _editable_files(repo: Path) -> list[Path]:
    return [path for path in repo.glob("*.py") if not path.name.startswith("test_")]


async def _main(
    run_id: str, workdir: Path, model: str, max_rounds: int, export: Path | None
) -> None:
    repo = workdir / "repo"
    if not repo.exists():
        shutil.copytree(_SAMPLE, repo)
    engine = cuaderna.Engine(workdir / "journal.db", lease_seconds=_LEASE_SECONDS)
    try:
        summary = await engine.execute(run_id, fix_tests, str(repo), model, max_rounds)
        if export is not None:
            await engine.export_run(run_id, export, redact=partial(_hide_local_paths, workdir))
    finally:
        await engine.close()
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
    parser.add_argument("--model", default="gpt-oss:20b")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--export", type=Path, help="write the finished run to this file")
    arguments = parser.parse_args()
    asyncio.run(
        _main(
            arguments.run_id,
            arguments.workdir,
            arguments.model,
            arguments.max_rounds,
            arguments.export,
        )
    )
