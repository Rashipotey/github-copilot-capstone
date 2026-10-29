from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import docsync.cli as cli
import docsync.drift as drift
from docsync.config import DRIFT_REPORT_FILENAME, DriftConfig
from docsync.drift import (
    DriftAnalysisResult,
    DriftAnalysisStatus,
    DriftChangeSet,
    DriftSourceChange,
    DriftUnanalyzedChange,
    run_drift,
)
from docsync.paths import PathValidationError


def _config(repo: Path, output: Path) -> DriftConfig:
    return DriftConfig(repo=repo, output_dir=output, target=None)


def _allow_empty_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        drift,
        "acquire_change_set",
        lambda _config: DriftChangeSet((), ()),
    )


def test_drift_requires_existing_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "not-created"

    exit_code = run_drift(_config(tmp_path, output))

    assert exit_code != 0
    assert not output.exists()
    assert "unsafe or unavailable" in capsys.readouterr().err


def test_drift_rejects_reparse_or_link_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    original_lstat = Path.lstat
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def report_link(path: Path):
        if path == output:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_file_attributes=reparse,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", report_link)

    exit_code = run_drift(_config(tmp_path, output))

    assert exit_code != 0
    assert list(output.iterdir()) == []
    assert "unsafe or unavailable" in capsys.readouterr().err


def test_drift_rejects_report_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    report = output / DRIFT_REPORT_FILENAME
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"preserve outside target")
    original_lstat = Path.lstat

    def report_is_symlink(path: Path):
        if path == report:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", report_is_symlink)

    exit_code = run_drift(_config(tmp_path, output))

    assert exit_code != 0
    assert outside.read_bytes() == b"preserve outside target"
    assert list(output.iterdir()) == []
    assert "unsafe or unavailable" in capsys.readouterr().err


def test_drift_rechecks_containment_immediately_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    original_validate = drift._validate_drift_output_directory
    validation_calls = 0

    def change_to_link_on_publication(config: DriftConfig, repo_root: Path) -> Path:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 3:
            raise PathValidationError("simulated reparse traversal")
        return original_validate(config, repo_root)

    monkeypatch.setattr(
        drift, "_validate_drift_output_directory", change_to_link_on_publication
    )

    exit_code = run_drift(_config(tmp_path, output))

    assert validation_calls == 3
    assert exit_code != 0
    assert list(output.iterdir()) == []
    assert "publication failed safely" in capsys.readouterr().err


def test_drift_refuses_existing_report_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    report = output / DRIFT_REPORT_FILENAME
    report.write_bytes(b"preserve existing report")

    exit_code = run_drift(_config(tmp_path, output))

    assert exit_code != 0
    assert report.read_bytes() == b"preserve existing report"
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}
    assert "already exists" in capsys.readouterr().err


def test_drift_exclusive_publication_preserves_racing_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    report = output / DRIFT_REPORT_FILENAME
    original_link = drift._link_anchored

    def create_collision_then_link(source, target, source_anchor, target_anchor):
        target.write_bytes(b"created concurrently")
        original_link(source, target, source_anchor, target_anchor)

    monkeypatch.setattr(drift, "_link_anchored", create_collision_then_link)

    exit_code = run_drift(_config(tmp_path, output))

    assert exit_code != 0
    assert report.read_bytes() == b"created concurrently"
    assert list(output.iterdir()) == [report]
    assert "already exists" in capsys.readouterr().err


def test_drift_cleans_temporary_file_after_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _allow_empty_analysis(monkeypatch)
    output = tmp_path / "output"
    output.mkdir()

    def fail_link(*_args, **_kwargs):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(drift, "_link_anchored", fail_link)

    exit_code = run_drift(_config(tmp_path, output))

    assert exit_code != 0
    assert list(output.iterdir()) == []
    error = capsys.readouterr().err
    assert "publication failed safely" in error
    assert "private filesystem detail" not in error


def test_drift_cli_publishes_only_report_for_complete_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    _allow_empty_analysis(monkeypatch)

    exit_code = cli.main(
        ["drift", "--repo", str(tmp_path), "--output-dir", str(output)]
    )

    report = output / DRIFT_REPORT_FILENAME
    assert exit_code == 0
    assert report.is_file()
    assert report.read_text(encoding="utf-8").startswith(
        "# Documentation Drift Analysis\n"
    )
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}
    assert "complete report published" in capsys.readouterr().out


def test_drift_cli_publishes_useful_partial_report_and_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        drift,
        "acquire_change_set",
        lambda _config: DriftChangeSet(
            (DriftSourceChange("src/worker.py", "modified"),),
            (DriftUnanalyzedChange("gone.py", "deleted"),),
        ),
    )

    exit_code = cli.main(
        ["drift", "--repo", str(tmp_path), "--output-dir", str(output)]
    )

    report = output / DRIFT_REPORT_FILENAME
    assert exit_code != 0
    assert report.is_file()
    assert "**Analysis status:** Partial" in report.read_text(encoding="utf-8")
    assert {item.name for item in output.iterdir()} == {DRIFT_REPORT_FILENAME}
    assert "partial report published" in capsys.readouterr().err


@pytest.mark.parametrize(
    "status",
    [
        DriftAnalysisStatus.AMBIGUOUS,
        DriftAnalysisStatus.INVALID_TARGET,
        DriftAnalysisStatus.FATAL,
        DriftAnalysisStatus.PARTIAL,
    ],
)
def test_drift_cli_does_not_publish_disallowed_analysis_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: DriftAnalysisStatus,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    result = DriftAnalysisResult(
        status=status,
        exit_code=1,
        report_markdown=None,
        message="sanitized analysis outcome",
    )
    monkeypatch.setattr(drift, "analyze_drift", lambda _config: result)

    exit_code = cli.main(
        ["drift", "--repo", str(tmp_path), "--output-dir", str(output)]
    )

    assert exit_code != 0
    assert list(output.iterdir()) == []
    assert "sanitized analysis outcome" in capsys.readouterr().err
