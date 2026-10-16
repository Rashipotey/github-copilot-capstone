import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for repository tests")

    subprocess.run(
        [git, "-C", str(tmp_path), "init", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [git, "-C", str(tmp_path), "config", "user.name", "DocSync Tests"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [git, "-C", str(tmp_path), "config", "user.email", "docsync-tests@example.invalid"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tmp_path