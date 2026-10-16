import argparse
from pathlib import Path
from typing import Sequence

from docsync.config import (
    DEFAULT_MAX_CHANGED_FILES,
    DEFAULT_MAX_DIFF_BYTES,
    RunConfig,
    normalize_exclude_pattern,
)
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


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="docsync", description="Analyze local repository changes.")
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


def main(args: Sequence[str] | None = None) -> int:
    result = run_pipeline(parse_args(args))
    print(result.summary)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())