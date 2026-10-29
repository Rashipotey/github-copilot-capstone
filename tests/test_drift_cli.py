from pathlib import Path

import pytest

import docsync.cli as cli
from docsync.config import (
    DEFAULT_MAX_CHANGED_FILES,
    DEFAULT_MAX_DIFF_BYTES,
    DRIFT_REPORT_FILENAME,
    DriftConfig,
)


def test_drift_cli_help_and_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["drift", "--help"])

    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "docsync drift" in help_text
    assert "--output-dir PATH" in help_text
    assert "--target DOC" in help_text
    assert "--repo PATH" in help_text
    assert "--exclude GLOB" in help_text
    assert "--max-changed-files N" in help_text
    assert "--max-diff-bytes N" in help_text
    assert DRIFT_REPORT_FILENAME in help_text

    config = cli.parse_drift_args(
        ["--repo", "checkout", "--output-dir", "reports", "--target", "docs/guide.md"]
    )
    assert config == DriftConfig(
        repo=Path("checkout"),
        output_dir=Path("reports"),
        target="docs/guide.md",
        excludes=(),
        max_changed_files=DEFAULT_MAX_CHANGED_FILES,
        max_diff_bytes=DEFAULT_MAX_DIFF_BYTES,
    )
    assert DRIFT_REPORT_FILENAME == "docsync-drift-report.md"


def test_drift_cli_requires_configured_output_directory() -> None:
    with pytest.raises(SystemExit) as error:
        cli.parse_drift_args([])
    assert error.value.code == 2

    with pytest.raises(SystemExit) as error:
        cli.parse_drift_args(["--output-dir", ""])
    assert error.value.code == 2

    with pytest.raises(SystemExit) as error:
        cli.parse_drift_args(["--output-dir", "reports", "--target", ""])
    assert error.value.code == 2


def test_drift_cli_dispatches_report_only_without_patch_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    observed: list[DriftConfig] = []

    def recording_handler(config: DriftConfig) -> int:
        observed.append(config)
        return 7

    def forbidden_pipeline(*args, **kwargs):
        raise AssertionError("drift must not enter the combined report-and-patch pipeline")

    monkeypatch.setattr(cli, "run_drift", recording_handler)
    monkeypatch.setattr(cli, "run_pipeline", forbidden_pipeline)

    exit_code = cli.main(
        [
            "drift",
            "--repo",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--target",
            "README.md",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 7
    assert observed == [
        DriftConfig(
            repo=tmp_path,
            output_dir=output_dir,
            target="README.md",
            excludes=(),
            max_changed_files=DEFAULT_MAX_CHANGED_FILES,
            max_diff_bytes=DEFAULT_MAX_DIFF_BYTES,
        )
    ]
    assert captured.err == ""
    assert list(output_dir.iterdir()) == []


def test_drift_reuses_existing_exclusion_and_limit_configuration() -> None:
    config = cli.parse_drift_args(
        [
            "--output-dir",
            "reports",
            "--exclude",
            "generated/**",
            "--exclude",
            "./vendor//**",
            "--max-changed-files",
            "12",
            "--max-diff-bytes",
            "4096",
        ]
    )

    assert config.excludes == ("generated/**", "vendor/**")
    assert config.max_changed_files == 12
    assert config.max_diff_bytes == 4096
