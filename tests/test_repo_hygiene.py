import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SDIST_ALLOW_LIST = {"/src", "/tests", "/README.md", "/LICENSE"}
REQUIRED_IGNORES = {".env", ".env.*", "*.db", "*.db-wal", "*.db-shm", ".playwright-mcp/"}
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#+\s+(.*?)\s*$", re.MULTILINE)


def anchor(heading: str) -> str:
    kept = re.sub(r"[^\w\- ]", "", heading.lower())
    return kept.replace(" ", "-")


def test_every_action_is_pinned_to_a_commit() -> None:
    unpinned: list[str] = []
    for workflow in WORKFLOWS:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" in line and not re.search(r"uses:\s*\S+@[0-9a-f]{40}(\s|$)", line):
                unpinned.append(f"{workflow.name}: {line.strip()}")
    assert WORKFLOWS
    assert unpinned == []


def test_checkout_does_not_persist_credentials() -> None:
    missing: list[str] = []
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        for step in re.split(r"\n\s*- ", text):
            if "actions/checkout@" in step and "persist-credentials: false" not in step:
                missing.append(workflow.name)
    assert missing == []


def test_only_the_publish_job_can_mint_a_token() -> None:
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert publish.count("id-token: write") == 1
    assert publish.index("id-token: write") > publish.index("\n  publish:")


def test_the_publish_job_uploads_with_the_action_that_attests() -> None:
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    publish_job = publish[publish.index("\n  publish:") :]
    assert "uses: pypa/gh-action-pypi-publish@" in publish_job
    assert "uv publish" not in publish_job


def test_sdist_ships_only_the_allow_list() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert set(sdist["include"]) <= SDIST_ALLOW_LIST


def test_gitignore_covers_secrets_and_tool_state() -> None:
    ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert ignored >= REQUIRED_IGNORES


def test_security_pages_exist() -> None:
    assert (ROOT / "SECURITY.md").is_file()
    assert (ROOT / "docs" / "security.md").is_file()


def test_relative_markdown_links_resolve() -> None:
    pages = [ROOT / "README.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").glob("*.md"))]
    broken: list[str] = []
    for page in pages:
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target):
                continue
            path_part, _, fragment = target.partition("#")
            destination = (page.parent / path_part).resolve() if path_part else page
            if not destination.exists():
                broken.append(f"{page.name}: {target}")
            elif fragment and destination.suffix == ".md" and destination.parent.name == "docs":
                headings = HEADING.findall(destination.read_text(encoding="utf-8"))
                if fragment not in {anchor(heading) for heading in headings}:
                    broken.append(f"{page.name}: {target}")
    assert broken == []
