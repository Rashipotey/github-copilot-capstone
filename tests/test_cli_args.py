from pathlib import Path

import pytest

from docsync.cli import build_parser, parse_args
from docsync.config import DEFAULT_MAX_CHANGED_FILES, DEFAULT_MAX_DIFF_BYTES
from docsync.models import DiffScope


def test_defaults_select_working_tree_and_approved_limits() -> None:
    config = parse_args([])

    assert config.repo == Path(".")
    assert config.scope is DiffScope.WORKING_TREE
    assert config.base is None
    assert config.head is None
    assert config.excludes == ()
    assert config.output_dir == Path("docsync-output")
    assert config.max_changed_files == DEFAULT_MAX_CHANGED_FILES == 100
    assert config.max_diff_bytes == DEFAULT_MAX_DIFF_BYTES == 1_048_576


def test_explicit_working_tree_scope() -> None:
    assert parse_args(["--working-tree"]).scope is DiffScope.WORKING_TREE


def test_staged_scope() -> None:
    assert parse_args(["--staged"]).scope is DiffScope.STAGED


def test_base_and_head_select_endpoint_scope() -> None:
    config = parse_args(["--base", "release", "--head", "feature"])

    assert config.scope is DiffScope.BASE_HEAD
    assert config.base == "release"
    assert config.head == "feature"


@pytest.mark.parametrize(
    "args",
    [
        ["--base", "release"],
        ["--head", "feature"],
        ["--working-tree", "--staged"],
        ["--working-tree", "--base", "release", "--head", "feature"],
        ["--staged", "--base", "release", "--head", "feature"],
    ],
)
def test_invalid_scope_combinations_fail(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(args)

    assert error.value.code == 2


@pytest.mark.parametrize("option", ["--max-changed-files", "--max-diff-bytes"])
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_numeric_limits_must_be_positive_integers(option: str, value: str) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args([option, value])

    assert error.value.code == 2


def test_explicit_options_are_preserved() -> None:
    config = parse_args(
        [
            "--repo",
            "worktree",
            "--output-dir",
            "build/results",
            "--max-changed-files",
            "25",
            "--max-diff-bytes",
            "2048",
        ]
    )

    assert config.repo == Path("worktree")
    assert config.output_dir == Path("build/results")
    assert config.max_changed_files == 25
    assert config.max_diff_bytes == 2048


def test_parse_errors_do_not_echo_untrusted_values(capsys: pytest.CaptureFixture[str]) -> None:
    hostile_value = "DO_NOT_ECHO_SECRET_8271"

    with pytest.raises(SystemExit):
        parse_args(["--max-diff-bytes", hostile_value])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert hostile_value not in output
    assert "invalid command-line arguments" in output


def test_overwrite_flag_is_rejected_with_sanitized_argparse_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(["--overwrite"])

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "invalid command-line arguments" in captured.err
    assert "--overwrite" not in build_parser().format_help()


def test_parser_has_no_provider_or_network_options() -> None:
    help_text = build_parser().format_help()

    assert "--provider" not in help_text
    assert "--api" not in help_text