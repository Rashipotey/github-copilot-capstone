from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from docsync.models import (
    ArtifactOutcome,
    ArtifactStatus,
    RunStatus,
    SkippedInput,
    ValidatorResult,
)
from docsync.paths import PathValidationError, validate_repository_name
from docsync.proposals import ProposalCandidate
from docsync.screening import SecretScanner


class ReportSafetyError(ValueError):
    """A report field could not be proven safe for output."""


@dataclass(frozen=True, slots=True)
class ReportData:
    execution_status: str
    selected_scope: str
    base_endpoint: str | None = None
    head_endpoint: str | None = None
    files_analyzed: int = 0
    changed_paths: tuple[str, ...] = ()
    skipped_inputs: tuple[SkippedInput, ...] = ()
    affected_documents: tuple[str, ...] = ()
    proposals: tuple[ProposalCandidate, ...] = ()
    validators: tuple[ValidatorResult, ...] = ()
    warnings: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()
    resource_limits: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()


def _screened(value: str, *, label: str, scanner: SecretScanner) -> str:
    try:
        result = scanner.screen_text(value, provenance=f"report {label}", reject_detected=True)
    except Exception as exc:
        raise ReportSafetyError("Report content screening failed; output was withheld.") from exc
    if (
        result is None
        or not result.usable
        or result.sanitized_text != value
        or result.provenance is None
    ):
        raise ReportSafetyError("Report content was unsafe or could not be screened; output was withheld.")
    return value


def _screened_repo_path(value: str, *, label: str, scanner: SecretScanner) -> str:
    value = _screened(value, label=label, scanner=scanner)
    try:
        validate_repository_name(value)
    except (PathValidationError, TypeError) as exc:
        raise ReportSafetyError("Report contains an unsafe repository path; output was withheld.") from exc
    return value


def _inline(value: str) -> str:
    flattened = value.replace("\r", " ").replace("\n", " ")
    escaped = html.escape(flattened, quote=False)
    for marker in ("\\", "`", "*", "_", "[", "]", "|", "#"):
        escaped = escaped.replace(marker, "\\" + marker)
    return escaped


def _code(value: str) -> str:
    flattened = value.replace("\r", " ").replace("\n", " ")
    marker = "`" * (max((len(run) for run in flattened.split(" ")), default=0) + 1)
    if marker in flattened:
        marker += "`"
    return f"{marker}{flattened}{marker}"


def _proposal_fields(proposal: ProposalCandidate, scanner: SecretScanner) -> tuple[str, ...]:
    provenance = proposal.provenance
    values = (
        proposal.target_path,
        proposal.sanitized_reason,
        provenance.source_path,
        provenance.target_path,
        provenance.replacement_kind,
        provenance.old_value,
        provenance.new_value,
        provenance.diff_hunk or "",
    )
    screened = tuple(_screened(value, label="proposal provenance", scanner=scanner) for value in values)
    _screened_repo_path(proposal.target_path, label="proposal target path", scanner=scanner)
    _screened_repo_path(provenance.source_path, label="proposal source path", scanner=scanner)
    _screened_repo_path(provenance.target_path, label="proposal provenance path", scanner=scanner)
    for path in proposal.source_paths:
        _screened_repo_path(path, label="proposal source path", scanner=scanner)
    provenance_lines = (
        provenance.source_line,
        provenance.target_line,
        provenance.diff_line,
    )
    if any(value is not None and (type(value) is not int or value < 1) for value in provenance_lines):
        raise ReportSafetyError("Report contains invalid proposal provenance; output was withheld.")
    return screened


def render_report(data: ReportData, *, scanner: SecretScanner | None = None) -> str:
    """Render the detailed review report; repository-derived fields must already be safe."""
    scanner = scanner or SecretScanner()
    if type(data.files_analyzed) is not int or data.files_analyzed < 0:
        raise ValueError("files_analyzed must not be negative")

    status = _screened(data.execution_status, label="status", scanner=scanner)
    scope = _screened(data.selected_scope, label="scope", scanner=scanner)
    base = _screened(data.base_endpoint or "Not applicable", label="base endpoint", scanner=scanner)
    head = _screened(data.head_endpoint or "Not applicable", label="head endpoint", scanner=scanner)
    changed_paths = tuple(
        _screened_repo_path(path, label="changed path", scanner=scanner) for path in data.changed_paths
    )
    affected = tuple(
        _screened_repo_path(path, label="affected documentation path", scanner=scanner)
        for path in data.affected_documents
    )
    skipped = tuple(
        (
            _screened_repo_path(item.path, label="skipped path", scanner=scanner),
            _screened(item.reason, label="skip reason", scanner=scanner),
            _screened(item.sanitized_detail, label="skip detail", scanner=scanner),
        )
        for item in data.skipped_inputs
    )
    validator_rows = tuple(
        (
            _screened(item.validator, label="validator name", scanner=scanner),
            _screened(item.status.value, label="validator status", scanner=scanner),
            _screened(item.sanitized_detail, label="validator detail", scanner=scanner),
            tuple(_screened(ref, label="validator evidence reference", scanner=scanner) for ref in item.evidence_refs),
        )
        for item in data.validators
    )
    warnings = tuple(_screened(value, label="warning", scanner=scanner) for value in data.warnings)
    uncertainties = tuple(
        _screened(value, label="uncertainty", scanner=scanner) for value in data.uncertainties
    )
    not_found = tuple(_screened(value, label="not found item", scanner=scanner) for value in data.not_found)
    limits = tuple(_screened(value, label="resource limit", scanner=scanner) for value in data.resource_limits)
    unresolved = tuple(
        _screened(value, label="unresolved item", scanner=scanner) for value in data.unresolved_items
    )
    paths_list = []
    for value in data.artifact_paths:
        screened_path = _screened(value, label="artifact path", scanner=scanner)
        if (
            not Path(screened_path).is_absolute()
            or "\x00" in screened_path
            or "\n" in screened_path
            or "\r" in screened_path
            or ".." in Path(screened_path).parts
        ):
            raise ReportSafetyError("Report contains an unsafe artifact path; output was withheld.")
        paths_list.append(screened_path)
    paths = tuple(paths_list)
    if paths and (
        len(paths) != 2
        or tuple(Path(value).name for value in paths)
        != ("docsync-report.md", "docsync.patch")
    ):
        raise ReportSafetyError("Report must identify exactly the two DocSync output paths.")

    proposal_rows = []
    for proposal in data.proposals:
        _proposal_fields(proposal, scanner)
        replacement_check = scanner.screen_candidate(proposal.replacement)
        if (
            replacement_check is None
            or not replacement_check.usable
            or replacement_check.sanitized_text != proposal.replacement
        ):
            raise ReportSafetyError("Proposal content was unsafe or could not be screened; output was withheld.")
        provenance = proposal.provenance
        proposal_rows.append(
            "\n".join(
                (
                    f"- Target: {_code(proposal.target_path)}",
                    f"  - Change: {_inline(provenance.replacement_kind)}; "
                    f"{_code(provenance.old_value)} -> {_code(provenance.new_value)}",
                    f"  - Reason: {_inline(proposal.sanitized_reason)}",
                    f"  - Source: {_code(provenance.source_path)}:{provenance.source_line}; "
                    f"diff { _code(provenance.diff_hunk or 'unavailable') } "
                    f"(line {provenance.diff_line if provenance.diff_line is not None else 'unavailable'})",
                    f"  - Documentation anchor: {_code(provenance.target_path)}:{provenance.target_line}",
                )
            )
        )

    def bullets(values: Iterable[str]) -> list[str]:
        return [f"- {_inline(value)}" for value in values] or ["- None"]

    lines = [
        "# DocSync Report",
        "",
        "## Execution",
        f"- Status: {_inline(status)}",
        f"- Selected scope: {_inline(scope)}",
        f"- Base endpoint: {_code(base)}",
        f"- Head endpoint: {_code(head)}",
        f"- Files analyzed: {data.files_analyzed}",
        f"- Changed paths: {len(changed_paths)}",
        f"- Skipped inputs: {len(skipped)}",
        f"- Proposed changes: {len(proposal_rows)}",
        "",
        "## Changed Inputs",
        *([f"- {_code(path)}" for path in changed_paths] or ["- None"]),
        "",
        "## Skipped Inputs",
        *(
            [f"- {_code(path)} ({_inline(reason)}): {_inline(detail)}" for path, reason, detail in skipped]
            or ["- None"]
        ),
        "",
        "## Affected Documentation",
        *([f"- {_code(path)}" for path in affected] or ["- None"]),
        "",
        "## Proposals",
        *(proposal_rows or ["- None"]),
        "",
        "## Validation",
    ]
    if validator_rows:
        for name, result, detail, references in validator_rows:
            lines.append(f"- **{_inline(name)}: {_inline(result)}**: {_inline(detail)}")
            if references:
                lines.extend(f"  - Evidence: {_code(reference)}" for reference in references)
    else:
        lines.append("- None")

    for title, values in (
        ("Warnings", warnings),
        ("Uncertainties", uncertainties),
        ("Not Found", not_found),
        ("Resource Limits", limits),
        ("Unresolved Items", unresolved),
        ("Artifacts", paths),
    ):
        lines.extend(("", f"## {title}", *bullets(values)))

    return "\n".join(lines) + "\n"


def format_terminal_summary(
    data: ReportData,
    outcomes: Iterable[ArtifactOutcome],
    *,
    scanner: SecretScanner | None = None,
) -> str:
    """Format a concise summary from actual per-artifact outcomes."""
    scanner = scanner or SecretScanner()
    actual = tuple(outcomes)
    written = sum(item.status is ArtifactStatus.WRITTEN for item in actual)
    def terminal_safe(value: str) -> str:
        try:
            result = scanner.screen_text(value, provenance="terminal summary", reject_detected=True)
        except Exception:
            return "[withheld]"
        if result is None or not result.usable or result.sanitized_text != value:
            return "[withheld]"
        return value.replace("\r", " ").replace("\n", " ")

    if len(actual) == 2 and written == 2:
        status = terminal_safe(data.execution_status)
    elif written:
        status = RunStatus.PARTIAL_ARTIFACTS.value
    else:
        status = RunStatus.FAILED.value
    changed_count = len(data.changed_paths)
    validator_summary = ", ".join(
        f"{terminal_safe(item.validator)}={item.status.value}" for item in data.validators
    ) or "none"
    lines = [
        f"DocSync: {status}",
        f"Scope: {terminal_safe(data.selected_scope)}",
        f"Files analyzed: {data.files_analyzed}; changed: {changed_count}; "
        f"skipped: {len(data.skipped_inputs)}; proposals: {len(data.proposals)}",
        f"Validations: {validator_summary}",
        f"Warnings: {len(data.warnings)}; uncertainties: {len(data.uncertainties)}; "
        f"Not Found: {len(data.not_found)}",
        "Artifacts:",
    ]
    for item in actual:
        path = terminal_safe(str(item.path))
        lines.append(
            f"- {terminal_safe(item.name)}: {item.status.value} ({path}) - "
            f"{terminal_safe(item.sanitized_detail)}"
        )
    return "\n".join(lines)