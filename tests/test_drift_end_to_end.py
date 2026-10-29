from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from docsync.cli import main
from docsync.config import DRIFT_REPORT_FILENAME


def _git(repo: Path, *args: str) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for repository tests")
    subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "DocSync Tests")
    _git(repo, "config", "user.email", "docsync-tests@example.invalid")


def _write(repo: Path, relative_path: str, content: str) -> Path:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _commit(repo: Path) -> None:
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "baseline")


def _run_drift(repo: Path, output: Path, *options: str) -> int:
    output.mkdir(parents=True, exist_ok=True)
    return main(
        [
            "drift",
            "--repo",
            str(repo),
            "--output-dir",
            str(output),
            *options,
        ]
    )


def test_drift_end_to_end_ac01_to_ac08(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise approved change, matching, outcome, and report-only contracts in isolated repos."""
    repo = tmp_path / "tracked-changes"
    _init_repo(repo)

    _write(repo, "engine.py", "VALUE = 1\n")
    _write(repo, "config-json/options.json", '{"feature_flag": false}\n')
    _write(repo, "config-toml/runtime.toml", 'service_mode = "old"\n')
    _write(
        repo,
        "README.md",
        "References `engine.py`, `config-json/options.json`, and "
        "`config-toml/runtime.toml`.\n"
        "The engine uses `launch_engine`; options use `feature_flag`; "
        "runtime uses `service_mode`.\n",
    )
    _write(repo, "src/orphan/orphan.py", "VALUE = 0\n")
    _write(repo, "removed/old.py", "VALUE = 2\n")
    _write(repo, "unsupported/notes.txt", "baseline\n")
    _write(
        repo,
        "archive/archived.md",
        "An unrelated, potentially stale statement.\n",
    )
    _commit(repo)

    # Staged and unstaged tracked changes are both compared with HEAD.
    _write(
        repo,
        "engine.py",
        "def launch_engine():\n    return 'active'\n",
    )
    _git(repo, "add", "engine.py")

    _write(repo, "config-json/options.json", '{"feature_flag": true}\n')
    _git(repo, "add", "config-json/options.json")
    _write(repo, "config-toml/runtime.toml", 'service_mode = "new"\n')
    _write(repo, "src/orphan/orphan.py", "VALUE = 3\n")
    _write(repo, "unsupported/notes.txt", "changed but unsupported\n")
    (repo / "removed/old.py").unlink()
    _write(repo, "src/untracked.py", "def must_not_be_read():\n    pass\n")

    output = tmp_path / "tracked-output"
    exit_code = _run_drift(repo, output)
    terminal = capsys.readouterr()
    report_path = output / DRIFT_REPORT_FILENAME
    assert report_path.is_file(), terminal.out + terminal.err
    report = report_path.read_text(encoding="utf-8")

    # AC-01/AC-06: eligible tracked staged/unstaged .py/.json/.toml inputs are
    # analyzed against HEAD; untracked, unsupported, deleted, and unrelated old
    # documentation do not become analyzed candidates.
    assert exit_code != 0  # Useful partial report due deleted/unsupported inputs.
    assert "**Analysis status:** Partial" in report
    assert "`engine.py`" in report
    assert "config-json/options.json" in report
    assert "config-toml/runtime.toml" in report
    assert "src/untracked.py" not in report
    assert "archive/archived.md" not in report
    assert "`removed/old.py`" in report and "deleted" in report
    assert "`unsupported/notes.txt`" in report and "unsupported" in report

    # AC-02: each distinct approved signal is retained as an evidence-backed reason.
    for reason in (
        "exact source\\-path reference",
        "source filename\\-stem token",
        "source symbol token",
        "configuration\\-key token",
        "same immediate parent directory",
    ):
        assert reason in report
    assert "launch_engine" in report
    assert "feature_flag" in report
    assert "service_mode" in report

    # AC-04/AC-05: no match is explicit, and every candidate remains potential
    # review material rather than a verified relationship or confirmed defect.
    assert "No relationship established" in report
    assert "orphan/orphan.py" in report
    assert "potential drift candidate" in report
    assert "does not establish a documentation relationship" in report
    assert "prove that documentation is incorrect" in report
    assert "verified" not in report.lower()
    assert "confirmed" not in report.lower()
    assert "PRIVATE_SOURCE" not in report

    # Candidate listing is deterministic and publication is report-only, fixed-name,
    # and confined to the configured output directory.
    candidate_positions = [
        report.index("Changed source `config-json/options.json`"),
        report.index("Changed source `config-toml/runtime.toml`"),
        report.index("Changed source `engine.py`"),
    ]
    assert candidate_positions == sorted(candidate_positions)
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}
    assert not (repo / DRIFT_REPORT_FILENAME).exists()
    assert not (repo / "docsync.patch").exists()
    assert "partial report published" in terminal.err

    # AC-03/AC-07: no-replace collision preserves the already published report.
    original_report = report_path.read_bytes()
    collision_exit = _run_drift(repo, output)
    collision_terminal = capsys.readouterr()
    assert collision_exit != 0
    assert report_path.read_bytes() == original_report
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}
    assert "already exists" in collision_terminal.err

    # AC-05: multiple candidates block publication without --target; valid and
    # invalid explicit targets produce their distinct outcomes.
    ambiguous_repo = tmp_path / "ambiguous"
    _init_repo(ambiguous_repo)
    _write(ambiguous_repo, "src/worker.py", "def run_job():\n    return 1\n")
    _write(ambiguous_repo, "README.md", "The `run_job` entry point.\n")
    _write(ambiguous_repo, "docs/guide.md", "Usage of `run_job`.\n")
    _commit(ambiguous_repo)
    _write(ambiguous_repo, "src/worker.py", "def run_job():\n    return 2\n")

    ambiguous_output = tmp_path / "ambiguous-output"
    ambiguous_exit = _run_drift(ambiguous_repo, ambiguous_output)
    ambiguous_terminal = capsys.readouterr()
    assert ambiguous_exit != 0
    assert "README.md" in ambiguous_terminal.err
    assert "docs/guide.md" in ambiguous_terminal.err
    assert list(ambiguous_output.iterdir()) == []

    explicit_output = tmp_path / "explicit-output"
    explicit_exit = _run_drift(
        ambiguous_repo, explicit_output, "--target", "docs/guide.md"
    )
    capsys.readouterr()
    explicit_report = (explicit_output / DRIFT_REPORT_FILENAME).read_text(
        encoding="utf-8"
    )
    assert explicit_exit == 0
    assert "docs/guide.md" in explicit_report
    assert "potential drift candidate" in explicit_report
    assert "does not establish a documentation relationship" in explicit_report
    assert "verified" not in explicit_report.lower()

    invalid_output = tmp_path / "invalid-target-output"
    invalid_exit = _run_drift(
        ambiguous_repo, invalid_output, "--target", "docs/not-a-candidate.md"
    )
    invalid_terminal = capsys.readouterr()
    assert invalid_exit != 0
    assert "README.md" in invalid_terminal.err
    assert "docs/guide.md" in invalid_terminal.err
    assert list(invalid_output.iterdir()) == []

    # AC-03/AC-08: a complete empty analysis succeeds with the fixed report;
    # a Git acquisition failure is fatal, non-successful, and publishes nothing.
    clean_repo = tmp_path / "clean"
    _init_repo(clean_repo)
    _write(clean_repo, "README.md", "No current source changes.\n")
    _commit(clean_repo)
    clean_output = tmp_path / "clean-output"
    clean_exit = _run_drift(clean_repo, clean_output)
    capsys.readouterr()
    clean_report = (clean_output / DRIFT_REPORT_FILENAME).read_text(
        encoding="utf-8"
    )
    assert clean_exit == 0
    assert "**Analysis status:** Complete" in clean_report
    assert "No candidate findings." in clean_report
    assert {item.name for item in clean_output.iterdir()} == {DRIFT_REPORT_FILENAME}

    fatal_repo = tmp_path / "fatal"
    _init_repo(fatal_repo)  # No HEAD: change-set acquisition cannot complete.
    fatal_output = tmp_path / "fatal-output"
    fatal_exit = _run_drift(fatal_repo, fatal_output)
    fatal_terminal = capsys.readouterr()
    assert fatal_exit != 0
    assert "Git/change-set acquisition failed" in fatal_terminal.err
    assert list(fatal_output.iterdir()) == []


def test_drift_repeated_run_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "deterministic"
    _init_repo(repo)
    _write(repo, "src/api.py", "def serve():\n    return 'old'\n")
    _write(repo, "docs/api.md", "See `src/api.py` and `serve`.\n")
    _commit(repo)
    _write(repo, "src/api.py", "def serve():\n    return 'new'\n")
    output = tmp_path / "deterministic-output"

    first_exit = _run_drift(repo, output)
    capsys.readouterr()
    first_report = (output / DRIFT_REPORT_FILENAME).read_bytes()
    (output / DRIFT_REPORT_FILENAME).unlink()

    second_exit = _run_drift(repo, output)
    capsys.readouterr()
    second_report = (output / DRIFT_REPORT_FILENAME).read_bytes()

    assert first_exit == second_exit == 0
    assert first_report == second_report
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}


def test_drift_preserves_source_and_existing_documentation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "preservation"
    _init_repo(repo)
    source = _write(repo, "src/service.py", "def serve():\n    return 'old'\n")
    documentation = _write(
        repo,
        "docs/service.md",
        "The `serve` operation is described here.\n",
    )
    preexisting_documentation = _write(
        repo, "README.md", "Existing documentation must remain byte-identical.\n"
    )
    _commit(repo)
    source.write_text("def serve():\n    return 'new'\n", encoding="utf-8")
    before = {
        source: source.read_bytes(),
        documentation: documentation.read_bytes(),
        preexisting_documentation: preexisting_documentation.read_bytes(),
    }
    output = tmp_path / "preservation-output"

    exit_code = _run_drift(repo, output)
    capsys.readouterr()

    assert exit_code == 0
    assert {path: path.read_bytes() for path in before} == before
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}
    assert not (repo / "docsync.patch").exists()
    assert not (repo / DRIFT_REPORT_FILENAME).exists()
