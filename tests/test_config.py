from pathlib import Path

import pytest

from docsync.cli import parse_args
from docsync.config import matches_exclusion, normalize_exclude_pattern
from docsync.models import (
    ArtifactOutcome,
    ArtifactStatus,
    Proposal,
    RunStatus,
    SkippedInput,
    StageResult,
    StageStatus,
    ValidatorResult,
    ValidatorStatus,
)


def test_default_output_resolves_from_canonical_repository_root() -> None:
    config = parse_args([])
    repo_root = Path("/canonical/project")

    assert config.output_path(repo_root) == repo_root / "docsync-output"


def test_relative_output_resolves_from_repository_root() -> None:
    config = parse_args(["--output-dir", "artifacts/run-1"])
    repo_root = Path("/canonical/project")

    assert config.output_path(repo_root) == repo_root / "artifacts/run-1"


def test_absolute_output_remains_absolute() -> None:
    output_dir = Path("/tmp/docsync-output")
    config = parse_args(["--output-dir", str(output_dir)])

    assert config.output_path(Path("/canonical/project")) == output_dir


def test_exclusions_are_repeatable_and_normalized() -> None:
    config = parse_args(["--exclude", "docs/**/*.md", "--exclude", "./build//**"])

    assert config.excludes == ("docs/**/*.md", "build/**")


@pytest.mark.parametrize(
    ("repo_path", "patterns", "expected"),
    [
        ("docs/guide.md", ("docs/*.md",), True),
        ("docs/nested/guide.md", ("docs/*.md",), False),
        ("docs/nested/guide.md", ("docs/**/*.md",), True),
        ("build/deep/output.py", ("build",), True),
        ("Docs/guide.md", ("docs/*.md",), False),
        ("docs/guide.MD", ("docs/*.md",), False),
    ],
)
def test_exclusion_matching_is_case_sensitive_posix_glob(
    repo_path: str,
    patterns: tuple[str, ...],
    expected: bool,
) -> None:
    assert matches_exclusion(repo_path, patterns) is expected


@pytest.mark.parametrize("pattern", ["", "/absolute/*.md", "../outside", "docs/../secrets", r"docs\\*.md", "."])
def test_exclusions_must_be_repo_relative_posix_globs(pattern: str) -> None:
    with pytest.raises(ValueError):
        normalize_exclude_pattern(pattern)


def test_result_contracts_are_typed_and_carry_sanitized_details() -> None:
    stage = StageResult("git_diff", StageStatus.COMPLETED, "Diff collected")
    skipped = SkippedInput("generated/output.py", "excluded", "Matched configured exclusion")
    proposal = Proposal("README.md", "new text", ("src/module.py",), "Signature changed")
    artifact = ArtifactOutcome("report", Path("docsync-report.md"), ArtifactStatus.WRITTEN, "Written")
    validator = ValidatorResult("links", ValidatorStatus.INCONCLUSIVE, "Target could not be resolved")

    assert stage.status.value == "completed"
    assert skipped.path == "generated/output.py"
    assert proposal.source_paths == ("src/module.py",)
    assert artifact.status.value == "written"
    assert validator.status.value == "inconclusive"
    assert {status.value for status in ValidatorStatus} == {
        "pass",
        "fail",
        "warning",
        "inconclusive",
    }
    assert RunStatus.EMPTY_DIFF.value == "empty_diff"
    assert RunStatus.INVALID_INPUT.value == "invalid_input"
    assert RunStatus.LIMIT_EXCEEDED.value == "limit_exceeded"
    assert RunStatus.SCREENING_FAILED.value == "screening_failed"
    assert RunStatus.PARTIAL_ARTIFACTS.value == "partial_artifacts"