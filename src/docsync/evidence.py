from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from docsync.config import matches_exclusion
from docsync.git import (
    MAX_EVIDENCE_FILE_BYTES,
    GitDiffResult,
    RawContentSnapshot,
    RawSnapshotCollection,
    SnapshotSideState,
    _collect_raw_file_snapshots,
)
from docsync.models import DiffScope, RunStatus, SkippedInput, StageStatus
from docsync.paths import (
    PathValidationError,
    RepositoryFileChangedError,
    RepositoryFileTooLargeError,
    read_repository_file,
    validate_repository_name,
    validate_repository_path,
)
from docsync.screening import ScreeningResult, SecretScanner

RAW_INPUT_BUDGET_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScreenedSide:
    state: SnapshotSideState
    text: str | None
    object_id: str | None
    mode: str | None


@dataclass(frozen=True, slots=True)
class ScreenedSnapshot:
    path: str
    scope: DiffScope
    old: ScreenedSide
    new: ScreenedSide
    git_binary: bool
    binary: bool
    old_endpoint: str = "unknown"
    new_endpoint: str = "unknown"


@dataclass(frozen=True, slots=True)
class ScreenedSnapshots:
    snapshots: tuple[ScreenedSnapshot, ...]
    status: StageStatus
    complete: bool
    sanitized_detail: str
    raw_bytes: int
    skipped_inputs: tuple[SkippedInput, ...] = ()
    diff_text: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenedDiff:
    text: str | None
    status: StageStatus
    complete: bool
    sanitized_detail: str
    raw_bytes: int
    skipped_inputs: tuple[SkippedInput, ...] = ()


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    path: str
    text: str


@dataclass(frozen=True, slots=True)
class MarkdownInventory:
    documents: tuple[MarkdownDocument, ...]
    status: StageStatus
    complete: bool
    sanitized_detail: str
    raw_bytes: int
    skipped_inputs: tuple[SkippedInput, ...] = ()


class RawInputBudget:
    """Track complete, unique repository evidence payloads without truncation."""

    def __init__(self, initial_bytes: int = 0, maximum_bytes: int = RAW_INPUT_BUDGET_BYTES):
        if initial_bytes < 0 or maximum_bytes < 0:
            raise ValueError("byte budgets must not be negative")
        self.maximum_bytes = maximum_bytes
        self.used_bytes = initial_bytes
        self._seen: set[tuple[str, bytes]] = set()
        self._seeded: set[str] = set()
        self.exceeded = initial_bytes > maximum_bytes

    def admit(self, path: str, content: bytes) -> bool:
        key = (path, hashlib.sha256(content).digest())
        if key in self._seen:
            return not self.exceeded
        if self.exceeded or self.used_bytes + len(content) > self.maximum_bytes:
            self.exceeded = True
            return False
        self._seen.add(key)
        self.used_bytes += len(content)
        return True

    def seed(self, identity: str, size: int) -> bool:
        if size < 0:
            raise ValueError("seed size must not be negative")
        if identity in self._seeded:
            return not self.exceeded
        self._seeded.add(identity)
        if self.exceeded or self.used_bytes + size > self.maximum_bytes:
            self.exceeded = True
            return False
        self.used_bytes += size
        return True


def _screen_side(
    raw_side,
    *,
    path: str,
    scope: DiffScope,
    side: str,
    scanner: SecretScanner,
    budget: RawInputBudget,
) -> tuple[ScreenedSide | None, str]:
    if raw_side.state is not SnapshotSideState.PRESENT:
        return ScreenedSide(raw_side.state, None, raw_side.object_id, raw_side.mode), ""
    content = raw_side.content
    if content is None or len(content) > MAX_EVIDENCE_FILE_BYTES:
        return None, "Snapshot bytes were incomplete or exceeded the file limit."
    provenance = f"{scope.value}:{path}:{side}"
    try:
        result: ScreeningResult = scanner.screen_evidence_item(content, provenance=provenance)
    except Exception:
        return None, "Evidence screening was incomplete; content was withheld."
    if result is None or not result.usable or result.sanitized_text is None:
        return None, "Evidence screening was incomplete; content was withheld."
    if not budget.admit(path, content):
        return None, "Cumulative evidence byte limit was exceeded."
    return (
        ScreenedSide(
            SnapshotSideState.PRESENT,
            result.sanitized_text,
            raw_side.object_id,
            raw_side.mode,
        ),
        "",
    )


def screen_selected_diff(
    diff_result: GitDiffResult,
    *,
    scanner: SecretScanner,
    budget: RawInputBudget,
) -> ScreenedDiff:
    """Screen the complete unified diff once before any downstream use."""
    if diff_result.status is RunStatus.EMPTY_DIFF:
        return ScreenedDiff("", StageStatus.COMPLETED, True, "No changed inputs.", budget.used_bytes)
    if diff_result.status is not RunStatus.COMPLETED or diff_result.diff is None:
        return ScreenedDiff(
            None, StageStatus.INCONCLUSIVE, False,
            "Selected Git diff is incomplete.", budget.used_bytes,
        )
    raw_diff = diff_result.diff
    if not budget.seed("selected-diff", len(raw_diff)):
        return ScreenedDiff(
            None, StageStatus.INCONCLUSIVE, False,
            "Cumulative evidence byte limit was exceeded.", budget.used_bytes,
        )
    try:
        screened = scanner.screen_evidence_item(raw_diff, provenance="selected Git diff")
    except Exception:
        screened = None
    if screened is None or not screened.usable or screened.sanitized_text is None:
        return ScreenedDiff(
            None, StageStatus.INCONCLUSIVE, False,
            "Diff screening was incomplete; all evidence was withheld.", budget.used_bytes,
        )
    return ScreenedDiff(
        screened.sanitized_text,
        StageStatus.COMPLETED,
        True,
        "Complete selected diff screened and admitted.",
        budget.used_bytes,
    )


def _screen_path_safely(scanner: SecretScanner, path: str):
    try:
        return scanner.screen_repository_path(path)
    except Exception:
        return None


def admit_raw_snapshots(
    collection: RawSnapshotCollection,
    *,
    scanner: SecretScanner,
    budget: RawInputBudget,
) -> ScreenedSnapshots:
    """Screen raw snapshot bytes immediately; never return an incomplete evidence set."""
    if not collection.complete or budget.exceeded:
        return ScreenedSnapshots((), StageStatus.INCONCLUSIVE, False, collection.diagnostic, budget.used_bytes)

    admitted: list[ScreenedSnapshot] = []
    skipped_inputs: list[SkippedInput] = []
    for snapshot in collection.snapshots:
        if not snapshot.complete:
            return ScreenedSnapshots(
                (), StageStatus.INCONCLUSIVE, False, snapshot.diagnostic, budget.used_bytes
            )
        for side in (snapshot.old, snapshot.new):
            if side.state is SnapshotSideState.PRESENT and (
                side.content is None or len(side.content) > MAX_EVIDENCE_FILE_BYTES
            ):
                return ScreenedSnapshots(
                    (), StageStatus.INCONCLUSIVE, False,
                    "Snapshot bytes were incomplete or exceeded the file limit.", budget.used_bytes
                )
        binary = snapshot.git_binary or snapshot.binary or any(
            side.state is SnapshotSideState.PRESENT and b"\x00" in side.content
            for side in (snapshot.old, snapshot.new)
        )
        if binary:
            for side in (snapshot.old, snapshot.new):
                if side.state is SnapshotSideState.PRESENT and side.content is not None:
                    if not budget.admit(snapshot.path, side.content):
                        return ScreenedSnapshots(
                            (), StageStatus.INCONCLUSIVE, False,
                            "Cumulative evidence byte limit was exceeded.", budget.used_bytes,
                        )
            screened_path = _screen_path_safely(scanner, snapshot.path)
            if (
                screened_path is None
                or not screened_path.usable
                or screened_path.sanitized_text is None
                or screened_path.sanitized_text != snapshot.path
            ):
                return ScreenedSnapshots(
                    (), StageStatus.INCONCLUSIVE, False,
                    "Repository path screening was incomplete.", budget.used_bytes
                )
            old = ScreenedSide(
                SnapshotSideState.UNAVAILABLE
                if snapshot.old.state is SnapshotSideState.PRESENT
                else snapshot.old.state,
                None,
                snapshot.old.object_id,
                snapshot.old.mode,
            )
            new = ScreenedSide(
                SnapshotSideState.UNAVAILABLE
                if snapshot.new.state is SnapshotSideState.PRESENT
                else snapshot.new.state,
                None,
                snapshot.new.object_id,
                snapshot.new.mode,
            )
            skipped_inputs.append(
                SkippedInput(
                    screened_path.sanitized_text,
                    "binary",
                    "Binary input skipped from content analysis.",
                )
            )
        else:
            old, old_error = _screen_side(
                snapshot.old,
                    path=snapshot.path,
                scope=snapshot.scope,
                side="old",
                scanner=scanner,
                budget=budget,
            )
            if old is None:
                return ScreenedSnapshots((), StageStatus.INCONCLUSIVE, False, old_error, budget.used_bytes)
            new, new_error = _screen_side(
                snapshot.new,
                    path=snapshot.path,
                scope=snapshot.scope,
                side="new",
                scanner=scanner,
                budget=budget,
            )
            if new is None:
                return ScreenedSnapshots((), StageStatus.INCONCLUSIVE, False, new_error, budget.used_bytes)
            screened_path = _screen_path_safely(scanner, snapshot.path)
            if (
                screened_path is None
                or not screened_path.usable
                or screened_path.sanitized_text is None
                or screened_path.sanitized_text != snapshot.path
            ):
                return ScreenedSnapshots(
                    (), StageStatus.INCONCLUSIVE, False,
                    "Repository path screening was incomplete.", budget.used_bytes
                )
        admitted.append(
            ScreenedSnapshot(
                screened_path.sanitized_text,
                snapshot.scope,
                old,
                new,
                snapshot.git_binary,
                binary,
                snapshot.old_endpoint,
                snapshot.new_endpoint,
            )
        )
    return ScreenedSnapshots(
        tuple(admitted), StageStatus.COMPLETED, True, "Snapshots screened and admitted.",
        budget.used_bytes, tuple(skipped_inputs)
    )


def collect_screened_snapshots(
    config,
    diff_result: GitDiffResult,
    *,
    scanner: SecretScanner,
    budget: RawInputBudget | None = None,
) -> ScreenedSnapshots:
    """Screen the diff first, then admit only complete selected-scope snapshots."""
    if budget is None:
        budget = RawInputBudget()
    screened_diff = screen_selected_diff(diff_result, scanner=scanner, budget=budget)
    if not screened_diff.complete:
        return ScreenedSnapshots(
            (), screened_diff.status, False, screened_diff.sanitized_detail,
            budget.used_bytes, screened_diff.skipped_inputs,
        )
    if diff_result.status is RunStatus.EMPTY_DIFF:
        return ScreenedSnapshots(
            (), StageStatus.COMPLETED, True, "No changed source snapshots.",
            budget.used_bytes, diff_text=screened_diff.text,
        )

    admitted: list[ScreenedSnapshot] = []
    skipped_inputs: list[SkippedInput] = []
    for changed in diff_result.changed_files:
        path_result = _screen_path_safely(scanner, changed.path)
        if path_result is None or not path_result.usable or path_result.sanitized_text is None:
            return ScreenedSnapshots(
                (), StageStatus.INCONCLUSIVE, False,
                "Changed path screening was incomplete; all evidence was withheld.",
                budget.used_bytes, tuple(skipped_inputs),
            )
        safe_path = path_result.sanitized_text
        if safe_path != changed.path:
            skipped_inputs.append(
                SkippedInput(safe_path, "unsafe", "Changed path could not be used safely.")
            )
            return ScreenedSnapshots(
                (), StageStatus.INCONCLUSIVE, False,
                "Changed path screening was incomplete; all evidence was withheld.",
                budget.used_bytes, tuple(skipped_inputs),
            )
        if matches_exclusion(safe_path, config.excludes):
            skipped_inputs.append(
                SkippedInput(safe_path, "excluded", "Matched a configured path exclusion.")
            )
            continue
        if changed.is_binary:
            skipped_inputs.append(
                SkippedInput(safe_path, "binary", "Binary input skipped from content analysis.")
            )
            continue
        if Path(safe_path).suffix not in {".py", ".json", ".toml", ".md"}:
            skipped_inputs.append(
                SkippedInput(safe_path, "unsupported", "Changed file format is unsupported.")
            )
            continue
        raw = _collect_raw_file_snapshots(config, diff_result, paths=(changed.path,))
        if not raw.complete:
            diagnostic = " ".join(item.diagnostic for item in raw.snapshots).casefold()
            reason = "over_size" if "exceeds its byte limit" in diagnostic else "unreadable_or_unsafe"
            skipped_inputs.append(
                SkippedInput(
                    safe_path,
                    reason,
                    "Changed evidence was unavailable; the complete evidence set was withheld.",
                )
            )
            return ScreenedSnapshots(
                (), StageStatus.INCONCLUSIVE, False,
                "One or more changed evidence inputs were unavailable; all evidence was withheld.",
                budget.used_bytes, tuple(skipped_inputs),
            )
        result = admit_raw_snapshots(raw, scanner=scanner, budget=budget)
        if not result.complete:
            return ScreenedSnapshots(
                (), result.status, False, result.sanitized_detail,
                budget.used_bytes, (*skipped_inputs, *result.skipped_inputs),
            )
        admitted.extend(result.snapshots)
        skipped_inputs.extend(result.skipped_inputs)
    return ScreenedSnapshots(
        tuple(admitted), StageStatus.COMPLETED, True,
        "Snapshots screened and admitted.", budget.used_bytes,
        tuple(skipped_inputs), screened_diff.text,
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_skip(
    scanner: SecretScanner,
    path: str,
    reason: str,
    detail: str,
) -> SkippedInput:
    screened = _screen_path_safely(scanner, path)
    safe_path = (
        screened.sanitized_text
        if screened is not None and screened.usable and screened.sanitized_text is not None
        else "[path withheld]"
    )
    return SkippedInput(safe_path, reason, detail)


def _markdown_paths(
    repo_root: Path,
    excludes: tuple[str, ...],
    scanner: SecretScanner,
) -> tuple[tuple[str, ...], bool, tuple[SkippedInput, ...]]:
    paths: list[str] = []
    skipped: list[SkippedInput] = []
    complete = True
    pending = [(repo_root, "")]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            complete = False
            skipped.append(
                SkippedInput(
                    relative_directory or "[repository root]",
                    "unreadable_or_unsafe",
                    "Directory contents could not be inspected safely.",
                )
            )
            continue
        for entry in entries:
            relative = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
            if relative == ".git" or relative.startswith(".git/"):
                continue
            path_result = _screen_path_safely(scanner, relative)
            if path_result is None or not path_result.usable or path_result.sanitized_text is None:
                complete = False
                skipped.append(
                    SkippedInput("[path withheld]", "unsafe", "Markdown inventory contains an unsafe path.")
                )
                continue
            safe_path = path_result.sanitized_text
            try:
                parts = validate_repository_name(relative)
                if "/".join(parts) != relative:
                    complete = False
                    skipped.append(
                        SkippedInput(safe_path, "unsafe", "Repository path is not normalized.")
                    )
                    continue
                metadata = entry.stat(follow_symlinks=False)
            except (OSError, PathValidationError):
                if relative.endswith(".md"):
                    complete = False
                skipped.append(
                    SkippedInput(safe_path, "unreadable_or_unsafe", "Path metadata could not be read safely.")
                )
                continue
            if matches_exclusion(relative, excludes):
                skipped.append(
                    SkippedInput(safe_path, "excluded", "Matched a configured path exclusion.")
                )
                continue
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                try:
                    points_to_directory = entry.is_dir(follow_symlinks=True)
                except OSError:
                    points_to_directory = True
                if points_to_directory or relative.endswith(".md"):
                    complete = False
                skipped.append(
                    SkippedInput(safe_path, "unreadable_or_unsafe", "Link or reparse-point input was skipped.")
                )
                continue
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((Path(entry.path), relative))
            elif relative.endswith(".md"):
                if not stat.S_ISREG(metadata.st_mode):
                    complete = False
                    skipped.append(
                        SkippedInput(safe_path, "unreadable_or_unsafe", "Markdown candidate is not a regular file.")
                    )
                else:
                    paths.append(relative)
    return tuple(sorted(paths)), complete, tuple(skipped)


def inventory_current_markdown(
    repo_root: Path | str,
    *,
    excludes: tuple[str, ...] = (),
    scanner: SecretScanner,
    budget: RawInputBudget | None = None,
) -> MarkdownInventory:
    """Screen every current-worktree Markdown candidate or return an empty incomplete inventory."""
    root = Path(repo_root).resolve(strict=True)
    if budget is None:
        budget = RawInputBudget()
    if budget.exceeded:
        return MarkdownInventory((), StageStatus.INCONCLUSIVE, False, "Cumulative evidence byte limit was exceeded.", budget.used_bytes)

    paths, complete, skipped_inputs = _markdown_paths(root, excludes, scanner)
    documents: list[MarkdownDocument] = []
    if not complete:
        return MarkdownInventory(
            (), StageStatus.INCONCLUSIVE, False,
            "Markdown inventory is incomplete or unsafe.", budget.used_bytes,
            skipped_inputs,
        )

    for relative_path in paths:
        safe_path = _safe_skip(
            scanner, relative_path, "unreadable_or_unsafe", "Markdown candidate was withheld."
        ).path
        if safe_path != relative_path:
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "unsafe", "Markdown candidate path could not be used safely."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Markdown inventory contains an unsafe path.", budget.used_bytes,
                skipped_inputs,
            )
        try:
            validate_repository_path(root, relative_path)
            content = read_repository_file(
                root,
                relative_path,
                maximum_bytes=MAX_EVIDENCE_FILE_BYTES,
            )
        except RepositoryFileTooLargeError:
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "over_size", "Markdown candidate exceeds the per-file limit."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Markdown candidate exceeds the file limit.", budget.used_bytes,
                skipped_inputs,
            )
        except RepositoryFileChangedError:
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "unreadable_or_unsafe", "Markdown candidate changed while reading."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Markdown candidate changed while reading.", budget.used_bytes,
                skipped_inputs,
            )
        except (PathValidationError, OSError):
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "unreadable_or_unsafe", "Markdown candidate could not be read safely."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Markdown inventory is incomplete or unsafe.", budget.used_bytes,
                skipped_inputs,
            )
        if b"\x00" in content:
            if not budget.admit(relative_path, content):
                skipped_inputs = (*skipped_inputs, SkippedInput(
                    safe_path, "resource_limit", "Cumulative evidence byte limit was exceeded."
                ))
                return MarkdownInventory(
                    (), StageStatus.INCONCLUSIVE, False,
                    "Cumulative evidence byte limit was exceeded.", budget.used_bytes,
                    skipped_inputs,
                )
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "binary", "Binary input skipped from documentation target analysis."
            ))
            continue
        try:
            screened = scanner.screen_evidence_item(content, provenance=relative_path)
        except Exception:
            screened = None
        if screened is None or not screened.usable or screened.sanitized_text is None:
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "unreadable_or_unsafe", "Markdown screening was incomplete; inventory withheld."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Markdown screening was incomplete; inventory withheld.", budget.used_bytes,
                skipped_inputs,
            )
        if not budget.admit(relative_path, content):
            skipped_inputs = (*skipped_inputs, SkippedInput(
                safe_path, "resource_limit", "Cumulative evidence byte limit was exceeded."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Cumulative evidence byte limit was exceeded.", budget.used_bytes,
                skipped_inputs,
            )
        screened_path = _screen_path_safely(scanner, relative_path)
        if (
            screened_path is None
            or not screened_path.usable
            or screened_path.sanitized_text is None
            or screened_path.sanitized_text != relative_path
        ):
            skipped_inputs = (*skipped_inputs, SkippedInput(
                "[path withheld]", "unsafe", "Repository path screening was incomplete."
            ))
            return MarkdownInventory(
                (), StageStatus.INCONCLUSIVE, False,
                "Repository path screening was incomplete.", budget.used_bytes,
                skipped_inputs,
            )
        documents.append(MarkdownDocument(screened_path.sanitized_text, screened.sanitized_text))

    return MarkdownInventory(
        tuple(documents), StageStatus.COMPLETED, True,
        "Markdown inventory screened and complete.", budget.used_bytes,
        skipped_inputs,
    )
