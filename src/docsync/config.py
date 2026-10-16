from dataclasses import dataclass
from functools import lru_cache
from fnmatch import fnmatchcase
from pathlib import Path

from docsync.models import DiffScope


DEFAULT_MAX_CHANGED_FILES = 100
DEFAULT_MAX_DIFF_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class RunConfig:
    repo: Path
    scope: DiffScope
    base: str | None
    head: str | None
    excludes: tuple[str, ...]
    output_dir: Path
    max_changed_files: int
    max_diff_bytes: int

    def output_path(self, canonical_repo_root: Path) -> Path:
        """Resolve the configured output directory without validating the path."""
        if self.output_dir.is_absolute():
            return self.output_dir
        return canonical_repo_root / self.output_dir


def normalize_exclude_pattern(pattern: str) -> str:
    if not pattern or "\\" in pattern or pattern.startswith("/"):
        raise ValueError("exclude patterns must be non-empty repo-relative POSIX globs")

    parts = pattern.split("/")
    if ".." in parts:
        raise ValueError("exclude patterns must not escape the repository")

    normalized = "/".join(part for part in parts if part not in ("", "."))
    if not normalized:
        raise ValueError("exclude patterns must identify a repository path")
    return normalized


def matches_exclusion(repo_relative_path: str, patterns: tuple[str, ...]) -> bool:
    """Match normalized repo-relative paths using case-sensitive POSIX globs."""
    path_parts = tuple(repo_relative_path.split("/"))

    for pattern in patterns:
        pattern_parts = tuple(pattern.split("/"))

        @lru_cache(maxsize=None)
        def matches(pattern_index: int, path_index: int, path_end: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == path_end

            if pattern_parts[pattern_index] == "**":
                return matches(pattern_index + 1, path_index, path_end) or (
                    path_index < path_end and matches(pattern_index, path_index + 1, path_end)
                )

            return (
                path_index < path_end
                and fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and matches(pattern_index + 1, path_index + 1, path_end)
            )

        if any(matches(0, 0, end) for end in range(1, len(path_parts) + 1)):
            return True
    return False