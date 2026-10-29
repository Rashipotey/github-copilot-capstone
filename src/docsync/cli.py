import argparse
import sys
from pathlib import Path
from typing import Sequence

from docsync.config import (
    DEFAULT_MAX_CHANGED_FILES,
    DEFAULT_MAX_DIFF_BYTES,
    DRIFT_REPORT_FILENAME,
    DriftConfig,
    RunConfig,
    normalize_exclude_pattern,
)
from docsync.drift import run_drift
from docsync.models import DiffScope
from docsync.pipeline import run_pipeline


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage()
        self.exit(2, f"{self.prog}: error: invalid command-line arguments; see --help\n")


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def _exclude_pattern(value: str) -> str:
    try:
        return normalize_exclude_pattern(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a repo-relative POSIX glob") from exc


def _required_path(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("expected a non-empty path")
    return Path(value)


def _required_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("expected a non-empty value")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="docsync",
        description="Analyze local repository changes. Use 'docsync drift' for a drift report.",
    )
    parser.add_argument("--repo", type=Path, default=Path("."), metavar="PATH")

    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument("--working-tree", action="store_true")
    scope_group.add_argument("--staged", action="store_true")
    parser.add_argument("--base", metavar="REV")
    parser.add_argument("--head", metavar="REV")

    parser.add_argument("--exclude", action="append", type=_exclude_pattern, default=[], metavar="GLOB")
    parser.add_argument("--output-dir", type=Path, default=Path("docsync-output"), metavar="PATH")
    parser.add_argument(
        "--max-changed-files",
        type=_positive_integer,
        default=DEFAULT_MAX_CHANGED_FILES,
        metavar="N",
    )
    parser.add_argument(
        "--max-diff-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_DIFF_BYTES,
        metavar="N",
    )
    return parser


def build_drift_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="docsync drift",
        description="Analyze potential documentation drift without creating patches.",
    )
    parser.add_argument("--repo", type=Path, default=Path("."), metavar="PATH")
    parser.add_argument(
        "--exclude",
        action="append",
        type=_exclude_pattern,
        default=[],
        metavar="GLOB",
        help="exclude matching repository paths from analysis (repeatable)",
    )
    parser.add_argument(
        "--max-changed-files",
        type=_positive_integer,
        default=DEFAULT_MAX_CHANGED_FILES,
        metavar="N",
        help="maximum changed paths to acquire",
    )
    parser.add_argument(
        "--max-diff-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_DIFF_BYTES,
        metavar="N",
        help="maximum combined Git diff size",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=_required_path,
        metavar="PATH",
        help=f"existing directory for {DRIFT_REPORT_FILENAME}",
    )
    parser.add_argument(
        "--target",
        type=_required_text,
        metavar="DOC",
        help="optional explicit Markdown documentation target",
    )
    return parser


def parse_args(args: Sequence[str] | None = None) -> RunConfig:
    parsed = build_parser().parse_args(args)
    has_base = parsed.base is not None
    has_head = parsed.head is not None

    if has_base != has_head:
        build_parser().error("base and head must be supplied together")
    if has_base and (parsed.working_tree or parsed.staged):
        build_parser().error("revision endpoints cannot be combined with a diff scope flag")

    if has_base:
        scope = DiffScope.BASE_HEAD
    elif parsed.staged:
        scope = DiffScope.STAGED
    else:
        scope = DiffScope.WORKING_TREE

    return RunConfig(
        repo=parsed.repo,
        scope=scope,
        base=parsed.base,
        head=parsed.head,
        excludes=tuple(parsed.exclude),
        output_dir=parsed.output_dir,
        max_changed_files=parsed.max_changed_files,
        max_diff_bytes=parsed.max_diff_bytes,
    )


def parse_drift_args(args: Sequence[str] | None = None) -> DriftConfig:
    parsed = build_drift_parser().parse_args(args)
    return DriftConfig(
        repo=parsed.repo,
        output_dir=parsed.output_dir,
        target=parsed.target,
        excludes=tuple(parsed.exclude),
        max_changed_files=parsed.max_changed_files,
        max_diff_bytes=parsed.max_diff_bytes,
    )


def main(args: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    if argv and argv[0] == "drift":
        config = parse_drift_args(argv[1:])
        return run_drift(config)

    result = run_pipeline(parse_args(argv))
    print(result.summary)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())