import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable


OUTPUT_NAMES = ("docsync-report.md", "docsync.patch")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PathValidationError(ValueError):
    """A repository or output path failed a safety check."""


class RepositoryFileTooLargeError(PathValidationError):
    """A complete repository file exceeds its configured byte limit."""


class RepositoryFileChangedError(PathValidationError):
    """A repository file changed while it was being read."""


@dataclass(frozen=True, slots=True)
class OutputPaths:
    directory: Path
    report: Path
    patch: Path


def canonicalize_repository_root(repo: Path | str) -> Path:
    """Resolve a caller-supplied repository path, allowing it to be a symlink."""
    try:
        root = Path(repo).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathValidationError("Repository path is unavailable.") from exc

    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise PathValidationError("Repository path is not a readable directory.")
    return root


def validate_repository_name(raw_path: str) -> tuple[str, ...]:
    """Validate a Git path lexically and return its normalized POSIX parts."""
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or any(unicodedata.category(character) == "Cc" for character in raw_path)
    ):
        raise PathValidationError("Repository path is malformed.")

    windows_path = PureWindowsPath(raw_path)
    if (
        PurePosixPath(raw_path).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or raw_path.startswith("\\")
        or "\\" in raw_path
    ):
        raise PathValidationError("Repository path must be relative.")

    parts = tuple(part for part in raw_path.split("/") if part not in ("", "."))
    if not parts or ".." in parts:
        raise PathValidationError("Repository path escapes its root.")
    return parts


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _reject_link_or_reparse(path: Path, *, is_final: bool) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PathValidationError("Path component cannot be inspected safely.") from exc

    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise PathValidationError("Symbolic links and reparse points are not allowed.")
    if not is_final and not stat.S_ISDIR(metadata.st_mode):
        raise PathValidationError("A repository path component is not a directory.")
    return metadata


def validate_repository_path(
    repo_root: Path | str,
    raw_path: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a repo-relative evidence path without traversing links."""
    root = Path(repo_root).resolve(strict=True)
    parts = validate_repository_name(raw_path)
    candidate = root
    for index, part in enumerate(parts):
        candidate = candidate / part
        metadata = _reject_link_or_reparse(candidate, is_final=index == len(parts) - 1)
        if metadata is None and must_exist:
            raise PathValidationError("Repository path does not exist.")

    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathValidationError("Repository path escapes its root or is unavailable.") from exc

    if must_exist and not resolved.is_file():
        raise PathValidationError("Evidence path is not a regular file.")
    return resolved


def read_repository_file(
    repo_root: Path | str,
    raw_path: str,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one regular file completely, rejecting links, growth, and oversize data."""
    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must not be negative")
    root = Path(repo_root).resolve(strict=True)
    path = validate_repository_path(root, raw_path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or _is_reparse_point(before):
        raise PathValidationError("Evidence path is not a safe regular file.")
    if before.st_size > maximum_bytes:
        raise RepositoryFileTooLargeError("Evidence file exceeds its byte limit.")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
                raise PathValidationError("Evidence path is not a safe regular file.")
            identity = (before.st_dev, before.st_ino)
            if identity != (opened.st_dev, opened.st_ino):
                raise RepositoryFileChangedError("Evidence file changed while opening.")
            content = stream.read(maximum_bytes + 1)
            after_open = os.fstat(stream.fileno())
        after_path = path.lstat()
    except PathValidationError:
        raise
    except FileNotFoundError as exc:
        raise PathValidationError("Evidence file is unavailable.") from exc
    except OSError as exc:
        raise PathValidationError("Evidence file could not be read safely.") from exc

    if len(content) > maximum_bytes:
        raise RepositoryFileTooLargeError("Evidence file exceeds its byte limit.")
    if (
        (opened.st_dev, opened.st_ino) != (after_open.st_dev, after_open.st_ino)
        or opened.st_size != after_open.st_size
        or opened.st_mtime_ns != after_open.st_mtime_ns
        or opened.st_ctime_ns != after_open.st_ctime_ns
        or len(content) != after_open.st_size
        or (after_path.st_dev, after_path.st_ino) != (opened.st_dev, opened.st_ino)
        or stat.S_ISLNK(after_path.st_mode)
        or _is_reparse_point(after_path)
    ):
        raise RepositoryFileChangedError("Evidence file changed while being read.")
    return content


def _absolute_unlinked_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for index, part in enumerate(parts):
        current = current / part
        metadata = _reject_link_or_reparse(current, is_final=index == len(parts) - 1)
        if metadata is None:
            continue
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PathValidationError("Output path contains a non-directory component.")
    try:
        return absolute.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathValidationError("Output path cannot be canonicalized safely.") from exc


def validate_output_paths(
    repo_root: Path | str,
    output_dir: Path | str,
    repository_inputs: Iterable[str] = (),
) -> OutputPaths:
    """Validate the future report/patch paths without creating or writing files."""
    root = Path(repo_root).resolve(strict=True)
    requested_dir = Path(output_dir)
    if not requested_dir.is_absolute():
        requested_dir = root / requested_dir
    directory = _absolute_unlinked_path(requested_dir)

    if directory.exists() and not directory.is_dir():
        raise PathValidationError("Output location is not a directory.")

    targets = tuple(directory / name for name in OUTPUT_NAMES)
    for target in targets:
        canonical_target = _absolute_unlinked_path(target)
        try:
            canonical_target.relative_to(directory)
        except ValueError as exc:
            raise PathValidationError("Output target escapes the selected directory.") from exc
        if canonical_target.exists() and not canonical_target.is_file():
            raise PathValidationError("Existing output target is not a regular file.")

    canonical_inputs = {
        validate_repository_path(root, raw_path, must_exist=False)
        for raw_path in repository_inputs
    }
    if any(_absolute_unlinked_path(target) in canonical_inputs for target in targets):
        raise PathValidationError("Output target collides with a repository input.")

    return OutputPaths(directory=directory, report=targets[0], patch=targets[1])