from __future__ import annotations

import ast
import html
import json
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from docsync.artifacts import _DirectoryAnchor, _link_anchored, _screen_output
from docsync.config import DRIFT_REPORT_FILENAME, DriftConfig, matches_exclusion
from docsync.evidence import RAW_INPUT_BUDGET_BYTES, RawInputBudget
from docsync.git import GitError, MAX_EVIDENCE_FILE_BYTES, collect_drift_change_set
from docsync.paths import (
    PathValidationError,
    RepositoryFileTooLargeError,
    canonicalize_repository_root,
    read_repository_file,
    validate_repository_name,
)
from docsync.screening import SecretScanner

if TYPE_CHECKING:
    from docsync.drift_matching import DriftMatchReason, DriftMatchResult


@dataclass(frozen=True, slots=True)
class DriftSourceChange:
    path: str
    status: str


@dataclass(frozen=True, slots=True)
class DriftUnanalyzedChange:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class DriftChangeSet:
    sources: tuple[DriftSourceChange, ...]
    unanalyzed: tuple[DriftUnanalyzedChange, ...]


@dataclass(frozen=True, slots=True)
class DriftEvidenceItem:
    """One bounded evidence item; unanalyzed items never carry content."""

    path: str
    kind: str
    status: str
    reason: str | None
    sanitized_detail: str
    content: str | None = None
    change_status: str | None = None


@dataclass(frozen=True, slots=True)
class DriftEvidenceCollection:
    sources: tuple[DriftEvidenceItem, ...]
    markdown: tuple[DriftEvidenceItem, ...]
    fatal: bool
    sanitized_detail: str
    raw_bytes: int
    markdown_inventory_truncated: bool = False


class DriftAnalysisStatus(StrEnum):
    """User-visible outcome of drift analysis, distinct from publication."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FATAL = "fatal"
    AMBIGUOUS = "ambiguous"
    INVALID_TARGET = "invalid_target"


@dataclass(frozen=True, slots=True)
class DriftUnanalyzedInput:
    """Safe, content-free description of an input excluded from analysis."""

    path: str
    kind: str
    reason: str
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class DriftAnalysisResult:
    """Analysis output for the later report publisher; contains no file writes."""

    status: DriftAnalysisStatus
    exit_code: int
    report_markdown: str | None
    message: str
    match_result: DriftMatchResult | None = None
    unanalyzed_inputs: tuple[DriftUnanalyzedInput, ...] = ()
    analyzed_source_count: int = 0

    @property
    def report_permitted(self) -> bool:
        return self.report_markdown is not None


class DriftReportSafetyError(ValueError):
    """A report field failed screening or could not be rendered safely."""


_ELIGIBLE_SOURCE_SUFFIXES = {".py", ".json", ".toml"}
_MAX_MARKDOWN_INVENTORY_ENTRIES = 500
_UNANALYZED_DETAILS = {
    "deleted": "Deleted input is not available for content analysis.",
    "unsupported": "Input format is unsupported.",
    "excluded": "Matched a configured path exclusion.",
    "changed-file-limit": "Changed-file limit was exceeded.",
    "diff-size-limit": "Git diff-size limit was exceeded.",
}


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _status_item(
    path: str,
    kind: str,
    reason: str,
    detail: str,
) -> DriftEvidenceItem:
    return DriftEvidenceItem(path, kind, "unanalyzed", reason, detail)


def _screen_required_path(scanner: SecretScanner, path: str) -> bool:
    """Accept only an unchanged, non-redacted screened path."""
    try:
        result = scanner.screen_repository_path(path)
    except Exception:
        return False
    return bool(
        result is not None
        and result.usable
        and result.sanitized_text == path
        and not result.categories
        and not result.detection_counts
    )


def _parse_source(path: str, text: str) -> bool:
    """Validate supported source syntax without executing repository code."""
    try:
        suffix = Path(path).suffix
        if suffix == ".py":
            ast.parse(text, filename=path, mode="exec")
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml":
            tomllib.loads(text)
        else:
            return False
    except (SyntaxError, ValueError, RecursionError):
        return False
    return True


def _screen_content(
    scanner: SecretScanner,
    content: bytes,
    *,
    provenance: str,
) -> str | None:
    """Return only unchanged UTF-8 evidence with no detected/redacted content."""
    try:
        decoded = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    try:
        result = scanner.screen_evidence_item(content, provenance=provenance)
    except Exception:
        return None
    if (
        result is None
        or not result.usable
        or result.sanitized_text != decoded
        or result.categories
        or result.detection_counts
    ):
        return None
    return result.sanitized_text


def _safe_inventory_path(
    scanner: SecretScanner,
    path: str,
) -> str:
    """Sanitize a diagnostic path, withholding it if screening changes it."""
    return path if _screen_required_path(scanner, path) else "[path withheld]"


def _report_field(value: str, *, label: str, scanner: SecretScanner) -> str:
    """Require report text to survive screening unchanged before Markdown escaping."""
    try:
        screened = scanner.screen_text(
            value,
            provenance=f"drift report {label}",
            reject_detected=True,
        )
    except Exception as exc:
        raise DriftReportSafetyError(
            "Report content could not be screened safely."
        ) from exc
    if (
        screened is None
        or not screened.usable
        or screened.sanitized_text != value
        or screened.categories
        or screened.detection_counts
    ):
        raise DriftReportSafetyError("Report content failed safety screening.")
    return value


def _markdown_text(value: str) -> str:
    """Escape untrusted inline text so it cannot change Markdown structure."""
    flattened = value.replace("\r", " ").replace("\n", " ")
    escaped = html.escape(flattened, quote=False)
    markdown_markers = (
        "\\",
        "`",
        "*",
        "_",
        "{",
        "}",
        "[",
        "]",
        "(",
        ")",
        "#",
        "+",
        "-",
        ".",
        "!",
        "|",
        "~",
    )
    for marker in markdown_markers:
        escaped = escaped.replace(marker, "\\" + marker)
    return escaped


def _markdown_code(value: str) -> str:
    """Render a path/token as a code span with a delimiter absent from its text."""
    flattened = value.replace("\r", " ").replace("\n", " ")
    longest_run = max(
        (len(run) for run in re.findall(r"`+", flattened)),
        default=0,
    )
    delimiter = "`" * (longest_run + 1)
    return f"{delimiter}{flattened}{delimiter}"


def _reason_description(reason: DriftMatchReason) -> str:
    descriptions = {
        "source_path": "exact source-path reference",
        "source_stem": "source filename-stem token",
        "symbol": "source symbol token",
        "config_key": "configuration-key token",
        "same_immediate_parent": "same immediate parent directory",
    }
    description = descriptions.get(reason.signal)
    if description is None:
        raise DriftReportSafetyError("A candidate reason was not recognized.")
    return description


def render_drift_report(
    status: DriftAnalysisStatus,
    matches: DriftMatchResult,
    unanalyzed_inputs: tuple[DriftUnanalyzedInput, ...] = (),
    *,
    scanner: SecretScanner | None = None,
) -> str:
    """Render candidate-only Markdown from screened paths/reasons, never file content."""
    if status not in {DriftAnalysisStatus.COMPLETE, DriftAnalysisStatus.PARTIAL}:
        raise ValueError("Only complete or partial analyses can have a report.")
    if not matches.report_permitted or matches.fatal_evidence:
        raise ValueError("The match result does not permit a report.")

    scanner = scanner or SecretScanner()

    def text(value: str, label: str) -> str:
        return _markdown_text(_report_field(value, label=label, scanner=scanner))

    lines = [
        "# Documentation Drift Analysis",
        "",
        f"**Analysis status:** {status.value.title()}",
        "",
        "Findings are potential review candidates only. A candidate does not establish "
        "a documentation relationship or prove that documentation is incorrect.",
        "",
        "## Findings",
        "",
    ]

    selected = matches.selected_candidates
    if selected:
        for candidate in selected:
            path = _report_field(candidate.path, label="candidate path", scanner=scanner)
            source = _report_field(candidate.source_path, label="source path", scanner=scanner)
            lines.append(
                f"- Changed source {_markdown_code(source)} has potential drift "
                f"candidate {_markdown_code(path)}."
            )
            for reason in candidate.reasons:
                description = _report_field(
                    _reason_description(reason),
                    label="candidate reason description",
                    scanner=scanner,
                )
                value = _report_field(reason.value, label="candidate reason", scanner=scanner)
                explanation = (
                    f"{_markdown_text(description)} ({_markdown_code(value)})"
                )
                if reason.evidence_line is not None:
                    if type(reason.evidence_line) is not int or reason.evidence_line < 1:
                        raise DriftReportSafetyError("Candidate evidence line is invalid.")
                    explanation += f" (Markdown line {reason.evidence_line})"
                lines.append(f"  - {explanation}")
        lines.append("")

    no_relationship = tuple(
        item
        for item in matches.source_matches
        if item.relationship_status == "no relationship established"
    )
    insufficient_evidence = tuple(
        item
        for item in matches.source_matches
        if item.relationship_status == "insufficient evidence"
    )
    if no_relationship:
        for source_match in no_relationship:
            source = _report_field(source_match.source_path, label="source path", scanner=scanner)
            lines.append(
                f"- Changed source {_markdown_code(source)}: **No relationship established** "
                "because no candidate signal matched."
            )
        lines.append("")
    if insufficient_evidence:
        for source_match in insufficient_evidence:
            source = _report_field(source_match.source_path, label="source path", scanner=scanner)
            lines.append(
                f"- Changed source {_markdown_code(source)}: **Insufficient evidence**; "
                "Markdown inventory is incomplete, so candidate selection and a "
                "no-relationship conclusion were withheld."
            )
        lines.append("")
    if not selected and not no_relationship and not insufficient_evidence:
        lines.append("- No candidate findings.")
        lines.append("")

    if unanalyzed_inputs:
        lines.extend(("## Unanalyzed Inputs", ""))
        for item in unanalyzed_inputs:
            path = _report_field(item.path, label="unanalyzed path", scanner=scanner)
            kind = _report_field(item.kind, label="unanalyzed input kind", scanner=scanner)
            reason = _report_field(item.reason, label="unanalyzed reason", scanner=scanner)
            detail = _report_field(
                item.sanitized_detail, label="unanalyzed detail", scanner=scanner
            )
            lines.append(
                f"- {_markdown_code(path)} ({text(kind, 'unanalyzed input kind')}): "
                f"{text(reason, 'unanalyzed reason')} — {text(detail, 'unanalyzed detail')}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _unanalyzed_inputs(
    evidence: DriftEvidenceCollection,
) -> tuple[DriftUnanalyzedInput, ...]:
    records = [
        DriftUnanalyzedInput(
            item.path,
            item.kind,
            item.reason or item.status,
            item.sanitized_detail,
        )
        for item in (*evidence.sources, *evidence.markdown)
        if item.status != "available"
    ]
    return tuple(
        sorted(records, key=lambda item: (item.kind, item.path, item.reason))
    )


def _validate_drift_output_directory(
    config: DriftConfig,
    repo_root: Path,
) -> Path:
    """Preflight the configured report path without creating or replacing anything."""
    requested = config.output_path(repo_root)
    absolute = Path(os.path.abspath(requested))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts

    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PathValidationError("Configured output path is unavailable.") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise PathValidationError("Configured output path contains a link.")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PathValidationError("Configured output path is not a directory.")
    if not absolute.is_dir():
        raise PathValidationError("Configured output location is not a directory.")

    resolved_directory = absolute.resolve(strict=True)
    report_path = resolved_directory / DRIFT_REPORT_FILENAME
    try:
        report_metadata = report_path.lstat()
    except FileNotFoundError:
        report_metadata = None
    except OSError as exc:
        raise PathValidationError("Configured report path cannot be inspected.") from exc
    if report_metadata is not None and (
        stat.S_ISLNK(report_metadata.st_mode)
        or _is_reparse_point(report_metadata)
        or not stat.S_ISREG(report_metadata.st_mode)
    ):
        raise PathValidationError("Configured report path is unsafe.")
    try:
        report_path.resolve(strict=False).relative_to(resolved_directory)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathValidationError("Configured report path escapes its output directory.") from exc
    return resolved_directory


def _fatal_result(message: str) -> DriftAnalysisResult:
    return DriftAnalysisResult(
        DriftAnalysisStatus.FATAL,
        2,
        None,
        message,
    )


def _enumerate_markdown(
    repo_root: Path,
    *,
    excludes: tuple[str, ...],
    scanner: SecretScanner,
) -> tuple[tuple[str, ...], tuple[DriftEvidenceItem, ...], bool, bool]:
    """Walk at most 500 repository entries while building the Markdown inventory."""
    paths: list[str] = []
    skipped: list[DriftEvidenceItem] = []
    fatal = False
    entries_seen = 0
    pending = [(repo_root, "")]

    while pending:
        directory, relative_directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if entries_seen == _MAX_MARKDOWN_INVENTORY_ENTRIES:
                        return (
                            (),
                            (
                                _status_item(
                                    "[repository inventory]",
                                    "markdown",
                                    "resource_limit",
                                    (
                                        "Markdown inventory entry limit "
                                        f"({_MAX_MARKDOWN_INVENTORY_ENTRIES}) was exceeded; "
                                        "remaining entries were not inspected."
                                    ),
                                ),
                            ),
                            False,
                            True,
                        )
                    entries_seen += 1

                    relative = (
                        f"{relative_directory}/{entry.name}"
                        if relative_directory
                        else entry.name
                    )
                    if relative == ".git" or relative.startswith(".git/"):
                        continue
                    if matches_exclusion(relative, excludes):
                        skipped.append(
                            _status_item(
                                _safe_inventory_path(scanner, relative),
                                "markdown",
                                "excluded",
                                "Matched a configured path exclusion.",
                            )
                        )
                        continue

                    try:
                        parts = validate_repository_name(relative)
                        if "/".join(parts) != relative:
                            raise PathValidationError(
                                "Repository path is not normalized."
                            )
                        metadata = entry.stat(follow_symlinks=False)
                    except (OSError, PathValidationError):
                        if relative.endswith(".md"):
                            skipped.append(
                                _status_item(
                                    _safe_inventory_path(scanner, relative),
                                    "markdown",
                                    "unreadable",
                                    "Path metadata could not be read safely.",
                                )
                            )
                        continue

                    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                        try:
                            points_to_directory = entry.is_dir(follow_symlinks=True)
                        except OSError:
                            points_to_directory = True
                        if relative.endswith(".md") or points_to_directory:
                            skipped.append(
                                _status_item(
                                    _safe_inventory_path(scanner, relative),
                                    "markdown",
                                    "unreadable",
                                    "Link or reparse-point input was not followed.",
                                )
                            )
                        continue

                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append((Path(entry.path), relative))
                    elif relative.endswith(".md"):
                        if not stat.S_ISREG(metadata.st_mode):
                            skipped.append(
                                _status_item(
                                    _safe_inventory_path(scanner, relative),
                                    "markdown",
                                    "unreadable",
                                    "Markdown candidate is not a regular file.",
                                )
                            )
                        elif not _screen_required_path(scanner, relative):
                            fatal = True
                            break
                        else:
                            paths.append(relative)
        except OSError:
            diagnostic_path = relative_directory or "[repository inventory]"
            skipped.append(
                _status_item(
                    _safe_inventory_path(scanner, diagnostic_path),
                    "markdown",
                    "unreadable",
                    "Directory contents could not be inspected safely.",
                )
            )
            continue

        if fatal:
            break

    return tuple(sorted(paths)), tuple(skipped), fatal, False


def collect_drift_evidence(
    config: DriftConfig,
    changes: DriftChangeSet,
    *,
    repo_root: Path | str | None = None,
    scanner: SecretScanner | None = None,
    budget: RawInputBudget | None = None,
) -> DriftEvidenceCollection:
    """Collect bounded source and Markdown evidence with G-05 fail-closed rules.

    File-local read, size, and syntax failures are represented as unanalyzed
    records. Any required UTF-8 decoding, path-screening, or content-screening
    safety failure withholds the entire evidence collection and is fatal.
    """
    try:
        root = canonicalize_repository_root(
            repo_root if repo_root is not None else config.repo
        )
    except PathValidationError:
        return DriftEvidenceCollection(
            (), (), True, "Repository evidence root is unavailable or unsafe.", 0
        )

    if scanner is None:
        scanner = SecretScanner()
    if budget is None:
        budget = RawInputBudget(maximum_bytes=RAW_INPUT_BUDGET_BYTES)

    source_items: list[DriftEvidenceItem] = []
    markdown_items: list[DriftEvidenceItem] = []

    for changed in changes.unanalyzed:
        reason = changed.reason if changed.reason in _UNANALYZED_DETAILS else "unsupported"
        detail = _UNANALYZED_DETAILS[reason]
        try:
            parts = validate_repository_name(changed.path)
            valid_path = "/".join(parts) == changed.path
        except PathValidationError:
            valid_path = False
        if not valid_path or not _screen_required_path(scanner, changed.path):
            return DriftEvidenceCollection(
                (), (), True, "Required repository-path screening failed.", budget.used_bytes
            )
        source_items.append(
            _status_item(changed.path, "source", reason, detail)
        )

    for changed in changes.sources:
        path = changed.path
        try:
            parts = validate_repository_name(path)
            if "/".join(parts) != path or Path(path).suffix not in _ELIGIBLE_SOURCE_SUFFIXES:
                raise PathValidationError("Source path is invalid.")
        except PathValidationError:
            return DriftEvidenceCollection(
                (), (), True, "Required source path validation failed.", budget.used_bytes
            )
        if changed.status not in {"added", "modified"}:
            return DriftEvidenceCollection(
                (), (), True, "Changed-source status is invalid.", budget.used_bytes
            )
        if not _screen_required_path(scanner, path):
            return DriftEvidenceCollection(
                (), (), True, "Required repository-path screening failed.", budget.used_bytes
            )
        if matches_exclusion(path, config.excludes):
            source_items.append(
                _status_item(
                    path, "source", "excluded", _UNANALYZED_DETAILS["excluded"]
                )
            )
            continue

        if budget.exceeded:
            source_items.append(
                _status_item(
                    path,
                    "source",
                    "resource_limit",
                    "Cumulative evidence byte limit was exceeded.",
                )
            )
            continue
        try:
            content = read_repository_file(
                root, path, maximum_bytes=MAX_EVIDENCE_FILE_BYTES
            )
        except RepositoryFileTooLargeError:
            source_items.append(
                _status_item(
                    path, "source", "over_size", "Source exceeds the per-file limit."
                )
            )
            continue
        except (PathValidationError, OSError):
            source_items.append(
                _status_item(
                    path,
                    "source",
                    "unreadable",
                    "Source could not be read safely.",
                )
            )
            continue

        if not budget.admit(path, content):
            source_items.append(
                _status_item(
                    path,
                    "source",
                    "resource_limit",
                    "Cumulative evidence byte limit was exceeded.",
                )
            )
            continue
        if b"\x00" in content:
            source_items.append(
                _status_item(
                    path, "source", "unsupported", "Binary source is not analyzed."
                )
            )
            continue
        screened_text = _screen_content(scanner, content, provenance=path)
        if screened_text is None:
            return DriftEvidenceCollection(
                (), (), True,
                "Required source decoding or screening safety check failed.",
                budget.used_bytes,
            )
        if not _parse_source(path, screened_text):
            source_items.append(
                _status_item(
                    path, "source", "parse_failed", "Source syntax could not be validated."
                )
            )
            continue
        source_items.append(
            DriftEvidenceItem(
                path, "source", "available", None,
                "Source was bounded, decoded, screened, and syntax-validated.",
                screened_text, changed.status,
            )
        )

    (
        markdown_paths,
        inventory_skips,
        inventory_fatal,
        markdown_inventory_truncated,
    ) = _enumerate_markdown(root, excludes=config.excludes, scanner=scanner)
    markdown_items.extend(inventory_skips)
    if inventory_fatal:
        return DriftEvidenceCollection(
            (), (), True, "Required Markdown path screening failed.", budget.used_bytes
        )

    for path in markdown_paths:
        if budget.exceeded:
            markdown_items.append(
                _status_item(
                    path,
                    "markdown",
                    "resource_limit",
                    "Cumulative evidence byte limit was exceeded.",
                )
            )
            continue
        try:
            content = read_repository_file(
                root, path, maximum_bytes=MAX_EVIDENCE_FILE_BYTES
            )
        except RepositoryFileTooLargeError:
            markdown_items.append(
                _status_item(
                    path, "markdown", "over_size",
                    "Markdown candidate exceeds the per-file limit.",
                )
            )
            continue
        except (PathValidationError, OSError):
            markdown_items.append(
                _status_item(
                    path, "markdown", "unreadable",
                    "Markdown candidate could not be read safely.",
                )
            )
            continue

        if not budget.admit(path, content):
            markdown_items.append(
                _status_item(
                    path, "markdown", "resource_limit",
                    "Cumulative evidence byte limit was exceeded.",
                )
            )
            continue
        if b"\x00" in content:
            markdown_items.append(
                _status_item(
                    path, "markdown", "unsupported",
                    "Binary Markdown candidate is not analyzed.",
                )
            )
            continue
        screened_text = _screen_content(scanner, content, provenance=path)
        if screened_text is None:
            return DriftEvidenceCollection(
                (), (), True,
                "Required Markdown decoding or screening safety check failed.",
                budget.used_bytes,
            )
        markdown_items.append(
            DriftEvidenceItem(
                path, "markdown", "available", None,
                "Markdown was bounded, decoded, and screened.",
                screened_text,
            )
        )

    source_items.sort(key=lambda item: (item.path, item.reason or ""))
    markdown_items.sort(key=lambda item: (item.path, item.reason or ""))
    return DriftEvidenceCollection(
        tuple(source_items),
        tuple(markdown_items),
        False,
        "Drift evidence collection completed with per-file statuses.",
        budget.used_bytes,
        markdown_inventory_truncated,
    )


def acquire_change_set(config: DriftConfig) -> DriftChangeSet:
    """Acquire and normalize the approved tracked change set without reading files."""
    git_changes = collect_drift_change_set(config)
    sources: list[DriftSourceChange] = []
    unanalyzed: list[DriftUnanalyzedChange] = []

    def deleted(path: str) -> None:
        unanalyzed.append(DriftUnanalyzedChange(path, "deleted"))

    def added_or_modified(path: str, status: str) -> None:
        if Path(path).suffix not in _ELIGIBLE_SOURCE_SUFFIXES:
            unanalyzed.append(DriftUnanalyzedChange(path, "unsupported"))
        elif matches_exclusion(path, config.excludes):
            unanalyzed.append(DriftUnanalyzedChange(path, "excluded"))
        elif git_changes.limit_reason is not None:
            unanalyzed.append(DriftUnanalyzedChange(path, git_changes.limit_reason))
        else:
            sources.append(DriftSourceChange(path, status))

    for change in git_changes.changes:
        if change.status == "R":
            old_path, new_path = change.paths
            deleted(old_path)
            added_or_modified(new_path, "added")
        elif change.status == "A":
            added_or_modified(change.paths[0], "added")
        elif change.status == "M":
            added_or_modified(change.paths[0], "modified")
        elif change.status == "D":
            deleted(change.paths[0])
        else:
            unanalyzed.extend(
                DriftUnanalyzedChange(path, "unsupported")
                for path in change.paths
            )

    sources.sort(key=lambda item: item.path)
    unanalyzed.sort(key=lambda item: (item.path, item.reason))
    return DriftChangeSet(tuple(sources), tuple(unanalyzed))


def analyze_drift(config: DriftConfig) -> DriftAnalysisResult:
    """Analyze the configured changes and return content for the later publisher.

    This function deliberately does not create, replace, or otherwise write files.
    """
    from docsync.drift_matching import match_drift_candidates

    scanner = SecretScanner()
    try:
        repo_root = canonicalize_repository_root(config.repo)
    except PathValidationError:
        return _fatal_result(
            "Repository change-set acquisition failed; no report was produced."
        )

    try:
        _validate_drift_output_directory(config, repo_root)
    except PathValidationError:
        return _fatal_result(
            "Configured output path is unsafe or unavailable; no report was produced."
        )

    try:
        changes = acquire_change_set(config)
    except (GitError, PathValidationError):
        return _fatal_result("Git/change-set acquisition failed; no report was produced.")

    try:
        evidence = collect_drift_evidence(
            config,
            changes,
            repo_root=repo_root,
            scanner=scanner,
        )
    except (PathValidationError, OSError, UnicodeError):
        return _fatal_result(
            "Required evidence could not be collected safely; no report was produced."
        )
    if evidence.fatal:
        return _fatal_result(
            "Required decoding or screening safety check failed; no report was produced."
        )

    matches = match_drift_candidates(evidence, target=config.target)
    if matches.fatal_evidence:
        return _fatal_result(
            "Required evidence safety check failed; no report was produced."
        )

    unanalyzed = _unanalyzed_inputs(evidence)
    analyzed_count = len(matches.source_matches)

    if matches.selection_status in {"ambiguous", "invalid_target"}:
        try:
            message = _report_field(
                matches.detail, label="selection diagnostic", scanner=scanner
            )
        except DriftReportSafetyError:
            return _fatal_result(
                "Selection diagnostic failed safety screening; no report was produced."
            )
        status = (
            DriftAnalysisStatus.AMBIGUOUS
            if matches.selection_status == "ambiguous"
            else DriftAnalysisStatus.INVALID_TARGET
        )
        return DriftAnalysisResult(
            status,
            2,
            None,
            message,
            matches,
            unanalyzed,
            analyzed_count,
        )

    if not matches.report_permitted:
        return _fatal_result("Analysis did not permit a report; no report was produced.")

    if unanalyzed:
        status = DriftAnalysisStatus.PARTIAL
        if analyzed_count == 0:
            return DriftAnalysisResult(
                status,
                1,
                None,
                (
                    "Analysis was partial, but no eligible source was safely analyzed; "
                    "no report was produced."
                ),
                matches,
                unanalyzed,
                0,
            )
    else:
        status = DriftAnalysisStatus.COMPLETE

    try:
        report = render_drift_report(
            status,
            matches,
            unanalyzed,
            scanner=scanner,
        )
    except DriftReportSafetyError:
        return _fatal_result("Report content failed safety validation; no report was produced.")

    return DriftAnalysisResult(
        status,
        1 if status is DriftAnalysisStatus.PARTIAL else 0,
        report,
        (
            "Partial analysis; report content is available for publication."
            if status is DriftAnalysisStatus.PARTIAL
            else "Complete analysis; report content is available for publication."
        ),
        matches,
        unanalyzed,
        analyzed_count,
    )


def _publish_drift_report(
    config: DriftConfig,
    repo_root: Path,
    report_markdown: str,
) -> None:
    """Atomically publish only the fixed report beneath its existing output directory."""
    data = report_markdown.encode("utf-8")
    scanner = SecretScanner()
    if not _screen_output(data, scanner):
        raise DriftReportSafetyError("Report content failed safety validation.")

    output_directory = _validate_drift_output_directory(config, repo_root)
    target = output_directory / DRIFT_REPORT_FILENAME
    temporary: Path | None = None

    # Keep one verified directory anchor for both exclusive staging and final publication.
    # The final hard-link operation is atomic and refuses an existing name on POSIX and
    # Windows; no report bytes are exposed until the complete staged file is linked.
    with _DirectoryAnchor(output_directory) as anchor:
        def reject_existing_target() -> None:
            try:
                if os.name == "nt":
                    metadata = target.lstat()
                else:
                    metadata = os.stat(
                        target.name,
                        dir_fd=anchor.fd,
                        follow_symlinks=False,
                    )
            except FileNotFoundError:
                return
            if (
                stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise PathValidationError("Configured report path is unsafe.")
            raise FileExistsError("Configured report already exists.")

        try:
            if _validate_drift_output_directory(config, repo_root) != output_directory:
                raise PathValidationError("Configured output directory changed.")
            anchor.verify()
            reject_existing_target()
            temporary = anchor.create_temporary(data)

            # Recheck path components immediately before publication, not just during
            # analysis or staging. The anchor additionally detects directory swaps.
            if _validate_drift_output_directory(config, repo_root) != output_directory:
                raise PathValidationError("Configured output directory changed.")
            anchor.verify()
            reject_existing_target()
            try:
                _link_anchored(temporary, target, anchor, anchor)
            except OSError:
                # Windows reports failed NT hard-link operations as a generic OSError;
                # inspect the anchored destination to preserve actionable collision
                # reporting while retaining the atomic no-replace operation.
                reject_existing_target()
                raise
        finally:
            if temporary is not None:
                try:
                    anchor.unlink(temporary.name)
                except FileNotFoundError:
                    pass


def run_drift(config: DriftConfig) -> int:
    """Analyze first, then publish only an allowed complete or useful partial report."""
    try:
        result = analyze_drift(config)
    except Exception:
        print(
            "DocSync drift: analysis failed safely; no report was produced. "
            "Check the repository, configured scope, and output path.",
            file=sys.stderr,
        )
        return 2

    allowed_statuses = {
        DriftAnalysisStatus.COMPLETE,
        DriftAnalysisStatus.PARTIAL,
    }
    if result.status not in allowed_statuses or result.report_markdown is None:
        print(f"DocSync drift: {result.message}", file=sys.stderr)
        return result.exit_code if result.exit_code != 0 else 1

    try:
        repo_root = canonicalize_repository_root(config.repo)
        _publish_drift_report(config, repo_root, result.report_markdown)
    except FileExistsError:
        print(
            f"DocSync drift: {DRIFT_REPORT_FILENAME} already exists; choose an empty "
            "output directory. The existing file was not changed.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            "DocSync drift: report publication failed safely. Verify that the existing "
            "output directory is writable and has no symlink or junction traversal; "
            "no report was published.",
            file=sys.stderr,
        )
        return 2

    if result.status is DriftAnalysisStatus.PARTIAL:
        print(
            f"DocSync drift: partial report published as {DRIFT_REPORT_FILENAME}; "
            "review the unanalyzed inputs.",
            file=sys.stderr,
        )
        return result.exit_code if result.exit_code != 0 else 1

    print(
        f"DocSync drift: complete report published as {DRIFT_REPORT_FILENAME}."
    )
    return 0
