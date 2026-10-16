from __future__ import annotations

import socket
import subprocess
import tomllib
import urllib.request
from pathlib import Path

import pytest

from docsync.cli import main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _prepare_repo(repo: Path) -> None:
    source = repo / "src" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text('def run(value: str = "old") -> str:\n    return value\n', encoding="utf-8")
    (repo / "README.md").write_text(
        'Source: `src/api.py`.\n\n````python\ndef run(value: str = "old") -> str:\n````\n',
        encoding="utf-8",
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    source.write_text(
        'def run(value: str = "new") -> str:\n    return value\n',
        encoding="utf-8",
    )

def _prepare_signature_addition_repo(repo: Path) -> None:
    source = repo / "src" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run(value: str) -> str:\n    return value\n", encoding="utf-8")
    (repo / "README.md").write_text(
        "Source: `src/api.py`.\n\n"
        "```python\ndef run(value: str) -> str:\n```\n",
        encoding="utf-8",
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    source.write_text(
        "def run(value: str, enabled: bool = False) -> str:\n    return value\n",
        encoding="utf-8",
    )


def _prepare_signature_line_change_repo(repo: Path, *, deletion: bool) -> None:
    without_parameter = "def run(\n    value: str,\n) -> str:\n"
    with_parameter = "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:\n"
    baseline = with_parameter if deletion else without_parameter
    changed = without_parameter if deletion else with_parameter
    source = repo / "src" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text(baseline + "    return value\n", encoding="utf-8")
    (repo / "README.md").write_text(
        "Source: `src/api.py`.\n\n```python\n" + baseline + "```\n",
        encoding="utf-8",
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    source.write_text(changed + "    return value\n", encoding="utf-8")


def test_help_and_console_script_are_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--repo" in help_text
    assert "--staged" in help_text
    assert "--output-dir" in help_text
    assert "--overwrite" not in help_text
    assert "--provider" not in help_text

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["docsync"] == "docsync.cli:main"


def test_removed_overwrite_option_preserves_existing_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "existing-output"
    output.mkdir()
    report = output / "docsync-report.md"
    patch = output / "docsync.patch"
    report.write_bytes(b"user report")
    patch.write_bytes(b"user patch")

    with pytest.raises(SystemExit) as error:
        main(["--output-dir", str(output), "--overwrite"])

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "invalid command-line arguments" in captured.err
    assert report.read_bytes() == b"user report"
    assert patch.read_bytes() == b"user patch"

def test_cli_writes_signature_parameter_insertion_and_preserves_source_docs(
    git_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_signature_addition_repo(git_repo)
    readme = git_repo / "README.md"
    readme_before = readme.read_bytes()
    output = tmp_path.parent / f"{tmp_path.name}-signature-output"

    exit_code = main(["--repo", str(git_repo), "--output-dir", str(output)])
    terminal = capsys.readouterr().out
    report = (output / "docsync-report.md").read_text(encoding="utf-8")
    patch = (output / "docsync.patch").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "proposals: 1" in terminal
    assert "Proposed changes: 1" in report
    assert "README.md" in report
    assert "src/api.py" in report
    assert "enabled: bool = False" in report
    assert patch.startswith("--- a/README.md\n+++ b/README.md\n")
    assert "+def run(value: str, enabled: bool = False) -> str:" in patch
    assert "-def run(value: str) -> str:" in patch
    assert readme.read_bytes() == readme_before


@pytest.mark.parametrize(
    ("deletion", "patch_line"),
    [
        (False, "+    enabled: bool = False,"),
        (True, "-    enabled: bool = False,"),
    ],
)
def test_cli_writes_separate_signature_parameter_line_change_without_editing_readme(
    git_repo: Path,
    tmp_path: Path,
    deletion: bool,
    patch_line: str,
) -> None:
    _prepare_signature_line_change_repo(git_repo, deletion=deletion)
    readme = git_repo / "README.md"
    readme_before = readme.read_bytes()
    output = tmp_path.parent / f"{tmp_path.name}-signature-line-output"

    exit_code = main(["--repo", str(git_repo), "--output-dir", str(output)])
    report = (output / "docsync-report.md").read_text(encoding="utf-8")
    patch = (output / "docsync.patch").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "Proposed changes: 1" in report
    assert "consistency: pass" in report
    assert patch.startswith("--- a/README.md\n+++ b/README.md\n")
    assert patch_line in patch
    assert readme.read_bytes() == readme_before


def test_cli_runs_offline_and_emits_only_sanitized_summary(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = git_repo
    _prepare_repo(repo)
    attempted: list[str] = []

    def forbidden_network(*args, **kwargs):
        attempted.append("network")
        raise AssertionError("network access is forbidden in the local pipeline")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    output = tmp_path.parent / f"{tmp_path.name}-cli-output"
    exit_code = main(["--repo", str(repo), "--output-dir", str(output)])
    terminal = capsys.readouterr().out

    assert exit_code == 0
    assert attempted == []
    assert "DocSync: completed" in terminal
    assert "Files analyzed:" in terminal
    assert "skipped:" in terminal
    assert "proposals: 1" in terminal
    assert "Validations: markdown=pass, links=pass, consistency=pass" in terminal
    assert "Warnings:" in terminal
    assert "uncertainties:" in terminal
    assert "docsync-report.md" in terminal
    assert "docsync.patch" in terminal
    assert {path.name for path in output.iterdir()} == {"docsync-report.md", "docsync.patch"}
