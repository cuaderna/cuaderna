import shutil
import subprocess
from pathlib import Path

import pytest

_SITE = Path(__file__).parent.parent / "site"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_labels_say_what_the_file_says() -> None:
    result = subprocess.run(
        ["node", "--test", "labels.test.js"],
        cwd=_SITE,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
