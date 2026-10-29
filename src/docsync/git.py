import os
import re
import shutil
import stat
import subprocess  # nosec B404 # Required local Git CLI; guarded argument-array execution below.
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from docsync.config import DriftConfig, RunConfig
from docsync.models import DiffScope, RunStatus
from docsync.paths import (
    PathValidationError,
    RepositoryFileTooLargeError,
    canonicalize_repository_root,
    read_repository_file,
    validate_repository_name,
    validate_repository_path,
)


GIT_TIMEOUT_SECONDS = 30
STDOUT_LIMIT_BYTES = 2 * 1024 * 1024
STDERR_LIMIT_BYTES = 64 * 1024
_TREE_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n?")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_EVIDENCE_FILE_BYTES = 256 * 1024


class GitError(RuntimeError):
    """A sanitized failure from local Git inspection."""


class GitUnavailableError(GitError):
    pass


class GitTimeoutError(GitError):
    pass


class GitOutputLimitError(GitError):
    pass


class InvalidRevisionError(GitError):
    pass


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    status: str
    is_binary: bool


@dataclass(frozen=True, slots=True)
class GitPathChange:
    """One Git name-status record with its paths in Git-reported order."""

    status: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DriftGitChangeSet:
    """Bounded tracked change metadata for drift analysis."""

    changes: tuple[GitPathChange, ...]
    limit_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GitDiffResult:
    scope: DiffScope
    base: str | None
    head: str | None
    base_tree: str | None
    head_tree: str | None
    changed_files: tuple[ChangedFile, ...]
    diff: bytes | None
    status: RunStatus
    affected_inputs: tuple[str, ...] = ()


class SnapshotSideState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RawSnapshotSide:
    state: SnapshotSideState
    content: bytes | None = None
    object_id: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class RawContentSnapshot:
    path: str
    scope: DiffScope
    old: RawSnapshotSide
    new: RawSnapshotSide
    git_binary: bool
    binary: bool
    complete: bool
    diagnostic: str
    old_endpoint: str = "unresolved"
    new_endpoint: str = "unresolved"


@dataclass(frozen=True, slots=True)
class RawSnapshotCollection:
    snapshots: tuple[RawContentSnapshot, ...]
    complete: bool
    diagnostic: str
    raw_bytes: int


@dataclass(frozen=True, slots=True)
class _ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run_bounded(
    argv: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    stdout_limit: int = STDOUT_LIMIT_BYTES,
    stderr_limit: int = STDERR_LIMIT_BYTES,
) -> _ProcessOutput:
    """Run an argument-list child with bounded concurrent pipe draining."""
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,  # nosec B603 # Resolved Git executable, argument array, bounded pipes and timeout.
        )
    except FileNotFoundError as exc:
        raise GitUnavailableError("Git executable is unavailable.") from exc
    except OSError as exc:
        raise GitError("Git process could not be started.") from exc

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name: str, stream, limit: int) -> None:
        buffer = buffers[name]
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            if len(buffer) + len(chunk) > limit:
                overflow.set()
                try:
                    child.kill()
                except OSError:
                    pass
            elif not overflow.is_set():
                buffer.extend(chunk)

    readers = (
        threading.Thread(target=drain, args=("stdout", child.stdout, stdout_limit), daemon=True),
        threading.Thread(target=drain, args=("stderr", child.stderr, stderr_limit), daemon=True),
    )
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        returncode = child.wait()
    finally:
        for reader in readers:
            reader.join()
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    if timed_out:
        raise GitTimeoutError("Git operation timed out.")
    if overflow.is_set():
        raise GitOutputLimitError("Git output exceeded a safety limit.")
    return _ProcessOutput(returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]))


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise GitUnavailableError("Git executable is unavailable.")
    return executable


def _run_git(repo_root: Path, args: list[str]) -> bytes:
    output = _run_bounded([_git_executable(), "-C", str(repo_root), *args])
    if output.returncode != 0:
        raise GitError("Git operation failed.")
    return output.stdout


def inspect_repository(repo: Path | str) -> Path:
    """Return the canonical worktree root for the supplied repository path."""
    supplied_root = canonicalize_repository_root(repo)
    try:
        output = _run_git(supplied_root, ["rev-parse", "--show-toplevel"])
    except (GitUnavailableError, GitTimeoutError, GitOutputLimitError):
        raise
    except GitError as exc:
        raise GitError("Path is not an accessible Git worktree.") from exc
    try:
        git_root_text = output.decode("utf-8", errors="strict").rstrip("\r\n")
        git_root = Path(git_root_text).resolve(strict=True)
        if not git_root.is_dir():
            raise ValueError
    except (UnicodeDecodeError, OSError, RuntimeError, ValueError) as exc:
        raise GitError("Git worktree root is invalid.") from exc
    return git_root


def _resolve_tree(repo_root: Path, revision: str) -> str:
    if not revision or "\x00" in revision:
        raise InvalidRevisionError("Git revision is invalid.")
    try:
        output = _run_bounded(
            [
                _git_executable(),
                "-C",
                str(repo_root),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{tree}}",
            ]
        )
    except GitError:
        raise
    if output.returncode != 0 or not _TREE_ID.fullmatch(output.stdout):
        raise InvalidRevisionError("Git revision is invalid.")
    return output.stdout.strip().decode("ascii")


def _scope_tree_ids(config: RunConfig, repo_root: Path) -> tuple[str | None, str | None]:
    if config.scope is not DiffScope.BASE_HEAD:
        return None, None
    if config.base is None or config.head is None:
        raise InvalidRevisionError("Both Git revisions are required.")
    return _resolve_tree(repo_root, config.base), _resolve_tree(repo_root, config.head)


def _scope_args(
    config: RunConfig,
    base_tree: str | None,
    head_tree: str | None,
) -> list[str]:
    if config.scope is DiffScope.WORKING_TREE:
        return []
    if config.scope is DiffScope.STAGED:
        return ["--cached"]
    if config.scope is DiffScope.BASE_HEAD and base_tree and head_tree:
        return [base_tree, head_tree]
    raise GitError("Git diff scope is invalid.")


def _decode_path(raw_path: bytes, repo_root: Path) -> str:
    try:
        path = raw_path.decode("utf-8", errors="strict")
        parts = validate_repository_name(path)
        if "/".join(parts) != path:
            raise PathValidationError("Repository path is not normalized.")
    except (UnicodeDecodeError, PathValidationError) as exc:
        raise GitError("Git returned an unsafe repository path.") from exc
    return path


def _parse_name_status_entries(data: bytes, repo_root: Path) -> tuple[GitPathChange, ...]:
    if not data:
        return ()
    if not data.endswith(b"\x00"):
        raise GitError("Git returned malformed path metadata.")
    fields = data[:-1].split(b"\x00")
    records: list[GitPathChange] = []
    index = 0
    while index < len(fields):
        status_field = fields[index]
        index += 1
        try:
            status_text = status_field.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitError("Git returned malformed path metadata.") from exc
        status = status_text[0] if status_text else ""
        if status not in {"A", "C", "D", "M", "R", "T", "U", "X", "B"}:
            raise GitError("Git returned malformed path metadata.")
        if status_text != status and (
            status not in {"C", "R"}
            or not status_text[1:].isdigit()
            or int(status_text[1:]) > 100
        ):
            raise GitError("Git returned malformed path metadata.")
        path_count = 2 if status[0] in "RC" else 1
        if index + path_count > len(fields):
            raise GitError("Git returned malformed path metadata.")
        raw_paths = fields[index : index + path_count]
        index += path_count
        paths = tuple(_decode_path(raw_path, repo_root) for raw_path in raw_paths)
        records.append(GitPathChange(status, paths))
    return tuple(records)


def _parse_name_status(data: bytes, repo_root: Path) -> tuple[tuple[str, str], ...]:
    """Parse name-status metadata into the legacy flattened path/status view."""
    return tuple(
        (path, entry.status)
        for entry in _parse_name_status_entries(data, repo_root)
        for path in entry.paths
    )


def _parse_numstat(data: bytes, repo_root: Path) -> dict[str, bool]:
    if not data:
        return {}
    if not data.endswith(b"\x00"):
        raise GitError("Git returned malformed binary metadata.")
    fields = data[:-1].split(b"\x00")
    binary_paths: dict[str, bool] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        columns = record.split(b"\t", 2)
        if len(columns) != 3:
            raise GitError("Git returned malformed binary metadata.")
        added, deleted, path_field = columns
        if not path_field:
            if index >= len(fields):
                raise GitError("Git returned malformed binary metadata.")
            path_field = fields[index]
            index += 1
            if index >= len(fields):
                raise GitError("Git returned malformed binary metadata.")
            index += 1
        elif not ((added == b"-" and deleted == b"-") or (added.isdigit() and deleted.isdigit())):
            raise GitError("Git returned malformed binary metadata.")
        path = _decode_path(path_field, repo_root)
        binary_paths[path] = added == b"-" and deleted == b"-"
    return binary_paths


def collect_diff(config: RunConfig) -> GitDiffResult:
    """Capture one approved diff scope, returning no partial analysis on limits."""
    if config.max_changed_files <= 0 or config.max_diff_bytes <= 0:
        raise GitError("Configured Git limits must be positive.")

    repo_root = inspect_repository(config.repo)
    base_tree, head_tree = _scope_tree_ids(config, repo_root)
    scope_args = _scope_args(config, base_tree, head_tree)
    common = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
    metadata_args = [*common, *scope_args, "--name-status", "-z", "--"]
    numstat_args = [*common, *scope_args, "--numstat", "-z", "--"]

    name_records = _parse_name_status(_run_git(repo_root, metadata_args), repo_root)
    numstat = _parse_numstat(_run_git(repo_root, numstat_args), repo_root)
    if {path for path, _ in name_records} != set(numstat):
        raise GitError("Git returned inconsistent change metadata.")

    changed_files = tuple(
        ChangedFile(path=path, status=status, is_binary=numstat[path])
        for path, status in name_records
    )
    affected_inputs = tuple(item.path for item in changed_files)
    if not changed_files:
        return GitDiffResult(
            config.scope,
            config.base,
            config.head,
            base_tree,
            head_tree,
            (),
            b"",
            RunStatus.EMPTY_DIFF,
        )

    if len(changed_files) > config.max_changed_files:
        return GitDiffResult(
            config.scope,
            config.base,
            config.head,
            base_tree,
            head_tree,
            changed_files,
            None,
            RunStatus.LIMIT_EXCEEDED,
            affected_inputs,
        )

    diff_args = [*common, *scope_args, "--",]
    diff = _run_git(repo_root, diff_args)
    if len(diff) > config.max_diff_bytes:
        return GitDiffResult(
            config.scope,
            config.base,
            config.head,
            base_tree,
            head_tree,
            changed_files,
            None,
            RunStatus.LIMIT_EXCEEDED,
            affected_inputs,
        )

    return GitDiffResult(
        config.scope,
        config.base,
        config.head,
        base_tree,
        head_tree,
        changed_files,
        diff,
        RunStatus.COMPLETED,
    )


def collect_drift_change_set(config: DriftConfig) -> DriftGitChangeSet:
    """Acquire tracked staged and unstaged path changes against HEAD, boundedly."""
    if config.max_changed_files <= 0 or config.max_diff_bytes <= 0:
        raise GitError("Configured Git limits must be positive.")

    repo_root = inspect_repository(config.repo)
    common = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames"]
    metadata = _run_git(
        repo_root,
        [*common, "--name-status", "-z", "HEAD", "--"],
    )
    changes = _parse_name_status_entries(metadata, repo_root)
    changed_path_count = sum(len(change.paths) for change in changes)
    if changed_path_count > config.max_changed_files:
        return DriftGitChangeSet(changes, "changed-file-limit")
    if not changes:
        return DriftGitChangeSet(())

    diff = _run_git(repo_root, [*common, "HEAD", "--"])
    if len(diff) > config.max_diff_bytes:
        return DriftGitChangeSet(changes, "diff-size-limit")
    return DriftGitChangeSet(changes)


def _run_git_bytes(
    repo_root: Path,
    args: list[str],
    *,
    stdout_limit: int = STDOUT_LIMIT_BYTES,
) -> bytes:
    output = _run_bounded(
        [_git_executable(), "-C", str(repo_root), *args],
        stdout_limit=stdout_limit,
        stderr_limit=STDERR_LIMIT_BYTES,
    )
    if output.returncode != 0:
        raise GitError("Git operation failed.")
    return output.stdout


def _verify_tree(repo_root: Path, revision: str) -> str:
    tree_id = _resolve_tree(repo_root, revision)
    if not _OBJECT_ID.fullmatch(tree_id):
        raise InvalidRevisionError("Git tree identifier is invalid.")
    return tree_id


def _parse_index_entries(data: bytes, requested: set[str]) -> dict[str, tuple[str, str]]:
    if data and not data.endswith(b"\x00"):
        raise GitError("Git returned malformed index metadata.")
    entries: dict[str, tuple[str, str]] = {}
    for record in data[:-1].split(b"\x00") if data else ():
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise GitError("Git returned malformed index metadata.")
        mode_raw, oid_raw, stage_raw = fields
        path = _decode_path(raw_path, Path("."))
        if path not in requested:
            continue
        try:
            mode = mode_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict")
            stage = int(stage_raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitError("Git returned malformed index metadata.") from exc
        if (
            mode not in {"100644", "100755", "120000", "160000"}
            or not _OBJECT_ID.fullmatch(oid)
            or stage != 0
            or path in entries
        ):
            raise GitError("Git returned ambiguous index metadata.")
        entries[path] = (mode, oid)
    return entries


def _parse_tree_entries(data: bytes, requested: set[str]) -> dict[str, tuple[str, str]]:
    if data and not data.endswith(b"\x00"):
        raise GitError("Git returned malformed tree metadata.")
    entries: dict[str, tuple[str, str]] = {}
    for record in data[:-1].split(b"\x00") if data else ():
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise GitError("Git returned malformed tree metadata.")
        mode_raw, object_type, oid_raw = fields
        path = _decode_path(raw_path, Path("."))
        if path not in requested:
            continue
        try:
            mode = mode_raw.decode("ascii", errors="strict")
            kind = object_type.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitError("Git returned malformed tree metadata.") from exc
        if (
            mode not in {"100644", "100755", "120000", "160000"}
            or kind not in {"blob", "commit"}
            or not _OBJECT_ID.fullmatch(oid)
            or path in entries
        ):
            raise GitError("Git returned ambiguous tree metadata.")
        entries[path] = (mode, oid)
    return entries


def _tree_entries(repo_root: Path, tree_id: str, paths: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    if not _OBJECT_ID.fullmatch(tree_id):
        raise InvalidRevisionError("Git tree identifier is invalid.")
    pathspecs = [f":(literal){path}" for path in paths]
    if not pathspecs:
        return {}
    output = _run_git_bytes(
        repo_root,
        ["ls-tree", "-r", "-z", "--full-tree", tree_id, "--", *pathspecs],
    )
    return _parse_tree_entries(output, set(paths))


def _index_entries(repo_root: Path, paths: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    output = _run_git_bytes(repo_root, ["ls-files", "--stage", "-z"])
    return _parse_index_entries(output, set(paths))


def _read_git_blob(repo_root: Path, mode: str, object_id: str) -> RawSnapshotSide:
    if mode in {"120000", "160000"}:
        raise GitError("Git symlink or submodule content is not admissible.")
    if mode not in {"100644", "100755"} or not _OBJECT_ID.fullmatch(object_id):
        raise GitError("Git returned an unsafe blob reference.")
    size_output = _run_git_bytes(repo_root, ["cat-file", "-s", object_id], stdout_limit=128)
    if not size_output.endswith(b"\n") or not size_output[:-1].isdigit():
        raise GitError("Git returned malformed object size metadata.")
    size = int(size_output[:-1])
    if size > MAX_EVIDENCE_FILE_BYTES:
        raise GitOutputLimitError("Evidence file exceeds its byte limit.")
    content = _run_git_bytes(
        repo_root,
        ["cat-file", "blob", object_id],
        stdout_limit=MAX_EVIDENCE_FILE_BYTES,
    )
    if len(content) != size:
        raise GitError("Git object content did not match its verified size.")
    return RawSnapshotSide(SnapshotSideState.PRESENT, content, object_id, mode)


def _missing_side() -> RawSnapshotSide:
    return RawSnapshotSide(SnapshotSideState.MISSING)


def _side_from_entry(
    repo_root: Path,
    entry: tuple[str, str] | None,
) -> RawSnapshotSide:
    return _missing_side() if entry is None else _read_git_blob(repo_root, *entry)


def _read_worktree_side(repo_root: Path, path: str) -> RawSnapshotSide:
    try:
        parts = validate_repository_name(path)
        candidate = repo_root
        for index, part in enumerate(parts):
            candidate = candidate / part
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                return _missing_side()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise PathValidationError("Repository path contains a link or reparse point.")
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise PathValidationError("Repository path component is not a directory.")
        resolved = validate_repository_path(repo_root, path)
        content = read_repository_file(
            repo_root,
            path,
            maximum_bytes=MAX_EVIDENCE_FILE_BYTES,
        )
    except PathValidationError as exc:
        candidate = repo_root.joinpath(*validate_repository_name(path))
        try:
            candidate.lstat()
        except FileNotFoundError:
            return _missing_side()
        if isinstance(exc, RepositoryFileTooLargeError):
            raise GitOutputLimitError("Evidence file exceeds its byte limit.") from exc
        raise GitError("Working-tree evidence path is unsafe or unavailable.") from exc
    if not resolved.is_relative_to(repo_root):
        raise GitError("Working-tree evidence path escapes the repository.")
    return RawSnapshotSide(SnapshotSideState.PRESENT, content, None, "100644")


def _snapshot_failure(path: str, scope: DiffScope, git_binary: bool, diagnostic: str) -> RawContentSnapshot:
    unavailable = RawSnapshotSide(SnapshotSideState.UNAVAILABLE)
    return RawContentSnapshot(
        path,
        scope,
        unavailable,
        unavailable,
        git_binary,
        git_binary,
        False,
        diagnostic,
    )


def _collect_raw_file_snapshots(
    config: RunConfig,
    diff_result: GitDiffResult,
    *,
    paths: Iterable[str] | None = None,
) -> RawSnapshotCollection:
    """Internal byte-level snapshot primitive; callers must immediately screen results."""
    try:
        repo_root = inspect_repository(config.repo)
        requested_paths = tuple(paths) if paths is not None else tuple(
            item.path for item in diff_result.changed_files
        )
        if len(set(requested_paths)) != len(requested_paths):
            raise GitError("Snapshot paths are duplicated.")
        for path in requested_paths:
            parts = validate_repository_name(path)
            if "/".join(parts) != path:
                raise GitError("Snapshot path is not normalized.")

        old_tree: str | None = None
        new_tree: str | None = None
        old_endpoint = "index"
        new_endpoint = "working-tree"
        if config.scope is DiffScope.STAGED:
            old_tree = _verify_tree(repo_root, "HEAD")
            old_endpoint = f"tree:{old_tree}"
            new_endpoint = "index"
        elif config.scope is DiffScope.BASE_HEAD:
            if config.base is None or config.head is None:
                raise InvalidRevisionError("Both Git revisions are required.")
            old_tree = _verify_tree(repo_root, config.base)
            new_tree = _verify_tree(repo_root, config.head)
            if diff_result.base_tree != old_tree or diff_result.head_tree != new_tree:
                raise InvalidRevisionError("Git endpoint trees do not match the selected scope.")
            old_endpoint = f"tree:{old_tree}"
            new_endpoint = f"tree:{new_tree}"

        old_entries: dict[str, tuple[str, str]] = {}
        new_entries: dict[str, tuple[str, str]] = {}
        if config.scope is DiffScope.WORKING_TREE:
            old_entries = _index_entries(repo_root, requested_paths)
        elif config.scope is DiffScope.STAGED:
            old_entries = _tree_entries(repo_root, old_tree or "", requested_paths)
            new_entries = _index_entries(repo_root, requested_paths)
        elif config.scope is DiffScope.BASE_HEAD:
            old_entries = _tree_entries(repo_root, old_tree or "", requested_paths)
            new_entries = _tree_entries(repo_root, new_tree or "", requested_paths)
        else:
            raise GitError("Git diff scope is invalid.")
    except GitError as exc:
        unavailable = tuple(
            _snapshot_failure(path, config.scope, False, str(exc))
            for path in (paths or (item.path for item in diff_result.changed_files))
        )
        return RawSnapshotCollection(unavailable, False, str(exc), 0)
    except (PathValidationError, OSError, ValueError):
        unavailable = tuple(
            _snapshot_failure(path, config.scope, False, "Snapshot input is unsafe or unavailable.")
            for path in (paths or (item.path for item in diff_result.changed_files))
        )
        return RawSnapshotCollection(unavailable, False, "Snapshot input is unsafe or unavailable.", 0)

    changed_by_path = {item.path: item for item in diff_result.changed_files}
    snapshots: list[RawContentSnapshot] = []
    total_bytes = 0
    complete = True
    for path in requested_paths:
        changed = changed_by_path.get(path)
        git_binary = bool(changed and changed.is_binary)
        if git_binary:
            snapshots.append(
                RawContentSnapshot(
                    path,
                    config.scope,
                    RawSnapshotSide(SnapshotSideState.UNAVAILABLE),
                    RawSnapshotSide(SnapshotSideState.UNAVAILABLE),
                    True,
                    True,
                    True,
                    "Binary Git input was excluded from content snapshots.",
                    old_endpoint,
                    new_endpoint,
                )
            )
            continue
        try:
            if config.scope is DiffScope.WORKING_TREE:
                old = _side_from_entry(repo_root, old_entries.get(path))
                new = _read_worktree_side(repo_root, path)
            elif config.scope is DiffScope.STAGED:
                old = _side_from_entry(repo_root, old_entries.get(path))
                new = _side_from_entry(repo_root, new_entries.get(path))
            else:
                old = _side_from_entry(repo_root, old_entries.get(path))
                new = _side_from_entry(repo_root, new_entries.get(path))
            old_bytes = old.content or b""
            new_bytes = new.content or b""
            is_binary = False
            if changed is not None:
                if (
                    (changed.status == "A" and old.state is not SnapshotSideState.MISSING)
                    or (changed.status == "D" and new.state is not SnapshotSideState.MISSING)
                    or (
                        changed.status not in {"A", "D"}
                        and (old.state is SnapshotSideState.MISSING or new.state is SnapshotSideState.MISSING)
                    )
                ):
                    raise GitError("Snapshot sides do not match the selected change metadata.")
            total_bytes += len(old_bytes) + len(new_bytes)
            snapshots.append(
                RawContentSnapshot(
                    path,
                    config.scope,
                    old,
                    new,
                    False,
                    is_binary,
                    True,
                    "Binary content was excluded from evidence." if is_binary else "Snapshot captured.",
                    old_endpoint,
                    new_endpoint,
                )
            )
        except (GitError, PathValidationError, OSError) as exc:
            complete = False
            diagnostic = (
                "Evidence file exceeds its byte limit."
                if isinstance(exc, GitOutputLimitError)
                else "Snapshot read failed or was unsafe."
            )
            snapshots.append(_snapshot_failure(path, config.scope, git_binary, diagnostic))

    return RawSnapshotCollection(
        tuple(snapshots),
        complete,
        "Snapshots captured." if complete else "One or more snapshots were unavailable.",
        total_bytes,
    )