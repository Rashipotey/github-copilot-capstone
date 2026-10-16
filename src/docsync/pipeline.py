from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from pathlib import PurePosixPath

from docsync.artifacts import ArtifactWriteResult, write_artifacts
from docsync.config import RunConfig
from docsync.evidence import (
    MarkdownInventory,
    RawInputBudget,
    ScreenedSnapshots,
    collect_screened_snapshots,
    inventory_current_markdown,
)
from docsync.git import GitError, InvalidRevisionError, collect_diff, inspect_repository
from docsync.models import (
    ArtifactOutcome,
    RunStatus,
    SkippedInput,
    ValidatorResult,
    ValidatorStatus,
)
from docsync.paths import PathValidationError, validate_output_paths
from docsync.proposals import ProposalCandidate, ProposalStatus, propose_deterministic
from docsync.reporting import ReportData, format_terminal_summary
from docsync.screening import SecretScanner
from docsync.targets import TargetSelection, select_targets
from docsync.validators.consistency import validate_consistency
from docsync.validators.links import validate_links
from docsync.validators.markdown import validate_markdown

_INSTRUCTION_LIKE = re.compile(
    r"(?is)\b(?:ignore|disregard|override|forget)\b.{0,100}"
    r"\b(?:all\s+)?(?:previous|prior|above|system|developer|user)?\s*"
    r"(?:instructions?|prompts?|rules?)\b"
)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    report: ReportData
    artifacts: tuple[ArtifactOutcome, ...]
    summary: str
    exit_code: int


def _safe_value(scanner: SecretScanner, value: str | None, fallback: str | None = None) -> str | None:
    if value is None:
        return None
    try:
        screened = scanner.screen_text(value, provenance="pipeline metadata", reject_detected=True)
    except Exception:
        return fallback
    if screened is None or not screened.usable or screened.sanitized_text != value:
        return fallback
    return value


def _safe_path(scanner: SecretScanner, path: str) -> str | None:
    try:
        screened = scanner.screen_repository_path(path)
    except Exception:
        return None
    if screened is None or not screened.usable or screened.sanitized_text != path:
        return None
    return path


def _unique_skips(*groups: tuple[SkippedInput, ...]) -> tuple[SkippedInput, ...]:
    result: list[SkippedInput] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for item in group:
            key = (item.path, item.reason, item.sanitized_detail)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return tuple(result)


def _safe_selection_paths(selection: TargetSelection, scanner: SecretScanner) -> tuple[str, ...]:
    paths = {
        candidate.path
        for candidate in selection.candidates
        if _safe_path(scanner, candidate.path) is not None
    }
    paths.update(
        target.path
        for target in selection.targets
        if _safe_path(scanner, target.path) is not None
    )
    return tuple(sorted(paths))


def _validation_result(name: str, detail: str) -> ValidatorResult:
    return ValidatorResult(name, ValidatorStatus.INCONCLUSIVE, detail)


def _contains_instruction_like_text(
    snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
) -> bool:
    evidence = [snapshots.diff_text or ""]
    for snapshot in snapshots.snapshots:
        evidence.extend((snapshot.old.text or "", snapshot.new.text or ""))
    evidence.extend(document.text for document in inventory.documents)
    return any(_INSTRUCTION_LIKE.search(value) for value in evidence)


def _validate_candidate(
    proposal: ProposalCandidate,
    source_snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
    repo_root: Path,
    scanner: SecretScanner,
) -> tuple[ValidatorResult, ...]:
    results: list[ValidatorResult] = []
    try:
        results.append(validate_markdown(proposal.replacement))
    except Exception:
        results.append(_validation_result("markdown", "Markdown validation could not be completed safely."))
    try:
        results.append(
            validate_links(
                repo_root,
                proposal.target_path,
                proposal.replacement,
                inventory,
                scanner=scanner,
            )
        )
    except Exception:
        results.append(_validation_result("links", "Local reference validation could not be completed safely."))
    try:
        results.append(validate_consistency(proposal, source_snapshots, inventory, scanner=scanner))
    except Exception:
        results.append(
            _validation_result("consistency", "Source consistency validation could not be completed safely.")
        )
    return tuple(results)


def _base_report(
    config: RunConfig,
    scanner: SecretScanner,
    *,
    status: RunStatus,
    changed_paths: tuple[str, ...] = (),
    skipped_inputs: tuple[SkippedInput, ...] = (),
    files_analyzed: int = 0,
    warnings: tuple[str, ...] = (),
    uncertainties: tuple[str, ...] = (),
    not_found: tuple[str, ...] = (),
    resource_limits: tuple[str, ...] = (),
    validators: tuple[ValidatorResult, ...] = (),
    affected_documents: tuple[str, ...] = (),
    proposals: tuple[ProposalCandidate, ...] = (),
) -> ReportData:
    return ReportData(
        execution_status=status.value,
        selected_scope=config.scope.value,
        base_endpoint=_safe_value(scanner, config.base),
        head_endpoint=_safe_value(scanner, config.head),
        files_analyzed=files_analyzed,
        changed_paths=changed_paths,
        skipped_inputs=skipped_inputs,
        affected_documents=affected_documents,
        proposals=proposals,
        validators=validators,
        warnings=warnings,
        uncertainties=uncertainties,
        not_found=not_found,
        resource_limits=resource_limits,
    )


def _finish(
    config: RunConfig,
    report: ReportData,
    scanner: SecretScanner,
    *,
    repo_root: Path | None,
    repository_inputs: tuple[str, ...] = (),
    original_documents: dict[str, str] | None = None,
) -> PipelineResult:
    if repo_root is None:
        validations = ", ".join(
            f"{item.validator}={item.status.value}" for item in report.validators
        ) or "none"
        summary = "\n".join(
            (
                f"DocSync: {report.execution_status}",
                f"Scope: {report.selected_scope}",
                f"Files analyzed: {report.files_analyzed}; changed: {len(report.changed_paths)}; "
                f"skipped: {len(report.skipped_inputs)}; proposals: {len(report.proposals)}",
                f"Validations: {validations}",
                f"Warnings: {len(report.warnings)}; uncertainties: {len(report.uncertainties)}; "
                f"Not Found: {len(report.not_found)}",
                "Artifacts: not written (repository or output preflight failed)",
            )
        )
        return PipelineResult(report, (), summary, 1)

    artifacts: ArtifactWriteResult = write_artifacts(
        repo_root,
        report,
        original_documents or {},
        proposals=report.proposals,
        output_dir=config.output_path(repo_root),
        repository_inputs=repository_inputs,
        scanner=scanner,
    )
    final_report = replace(report, proposals=artifacts.proposals)
    summary = format_terminal_summary(final_report, artifacts.outcomes, scanner=scanner)
    successful = {RunStatus.COMPLETED.value, RunStatus.EMPTY_DIFF.value}
    exit_code = int(report.execution_status not in successful or artifacts.status is not RunStatus.COMPLETED)
    return PipelineResult(final_report, artifacts.outcomes, summary, exit_code)


def run_pipeline(config: RunConfig, *, scanner: SecretScanner | None = None) -> PipelineResult:
    """Run the offline, proposal-only documentation analysis pipeline."""
    scanner = scanner or SecretScanner()
    repo_root: Path | None = None
    repository_inputs: tuple[str, ...] = ()
    try:
        repo_root = inspect_repository(config.repo)
        output_paths = validate_output_paths(repo_root, config.output_path(repo_root))
    except (GitError, PathValidationError, OSError):
        report = _base_report(
            config,
            scanner,
            status=RunStatus.INVALID_INPUT,
            uncertainties=("Repository or output path preflight failed safely.",),
        )
        return _finish(config, report, scanner, repo_root=None)

    if output_paths.report.exists() or output_paths.patch.exists():
        report = _base_report(
            config,
            scanner,
            status=RunStatus.FAILED,
            uncertainties=("An output artifact already exists; repository analysis was not started.",),
        )
        return _finish(config, report, scanner, repo_root=repo_root)

    try:
        diff = collect_diff(config)
    except InvalidRevisionError:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.INVALID_INPUT,
            uncertainties=("A selected Git revision is invalid; repository evidence was not read.",),
        )
        return _finish(config, report, scanner, repo_root=repo_root)
    except GitError:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.FAILED,
            uncertainties=("The selected local Git diff could not be collected safely.",),
        )
        return _finish(config, report, scanner, repo_root=repo_root)
    except Exception:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.FAILED,
            uncertainties=("The selected local Git diff could not be collected safely.",),
        )
        return _finish(config, report, scanner, repo_root=repo_root)

    safe_changed = tuple(
        path
        for item in diff.changed_files
        if (path := _safe_path(scanner, item.path)) is not None
    )
    repository_inputs = safe_changed
    if diff.status is RunStatus.EMPTY_DIFF:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.EMPTY_DIFF,
            not_found=("No changed files were found in the selected diff scope.",),
        )
        return _finish(config, report, scanner, repo_root=repo_root)
    if diff.status is RunStatus.LIMIT_EXCEEDED:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.LIMIT_EXCEEDED,
            changed_paths=safe_changed,
            resource_limits=("The selected diff exceeded a configured analysis limit; proposals were withheld.",),
            not_found=("No proposals were generated from incomplete evidence.",),
        )
        return _finish(
            config, report, scanner, repo_root=repo_root, repository_inputs=repository_inputs
        )

    budget = RawInputBudget()
    try:
        snapshots = collect_screened_snapshots(config, diff, scanner=scanner, budget=budget)
    except Exception:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.FAILED,
            changed_paths=safe_changed,
            uncertainties=("Source evidence collection failed safely; proposals were withheld.",),
            not_found=("No proposals were generated from incomplete evidence.",),
        )
        return _finish(
            config, report, scanner, repo_root=repo_root, repository_inputs=repository_inputs
        )
    if not snapshots.complete:
        screening_failure = "screening" in snapshots.sanitized_detail.casefold()
        limit_failure = "limit" in snapshots.sanitized_detail.casefold()
        report = _base_report(
            config,
            scanner,
            status=(
                RunStatus.LIMIT_EXCEEDED
                if limit_failure
                else RunStatus.SCREENING_FAILED
                if screening_failure
                else RunStatus.FAILED
            ),
            changed_paths=safe_changed,
            skipped_inputs=snapshots.skipped_inputs,
            resource_limits=(snapshots.sanitized_detail,)
            if "limit" in snapshots.sanitized_detail.casefold()
            else (),
            uncertainties=("Required diff or source evidence was incomplete; proposals were withheld.",),
            not_found=("No proposals were generated from incomplete evidence.",),
        )
        return _finish(
            config, report, scanner, repo_root=repo_root, repository_inputs=repository_inputs
        )

    try:
        inventory = inventory_current_markdown(
            repo_root,
            excludes=config.excludes,
            scanner=scanner,
            budget=budget,
        )
    except Exception:
        report = _base_report(
            config,
            scanner,
            status=RunStatus.FAILED,
            changed_paths=safe_changed,
            skipped_inputs=snapshots.skipped_inputs,
            files_analyzed=len(snapshots.snapshots),
            uncertainties=("Markdown inventory failed safely; proposals were withheld.",),
            not_found=("No proposals were generated from incomplete evidence.",),
        )
        return _finish(
            config,
            report,
            scanner,
            repo_root=repo_root,
            repository_inputs=repository_inputs,
        )
    repository_inputs = tuple(
        sorted(set((*repository_inputs, *(document.path for document in inventory.documents))))
    )
    skipped = _unique_skips(snapshots.skipped_inputs, inventory.skipped_inputs)
    if not inventory.complete:
        limit_failure = "limit" in inventory.sanitized_detail.casefold()
        report = _base_report(
            config,
            scanner,
            status=(
                RunStatus.LIMIT_EXCEEDED
                if limit_failure
                else RunStatus.SCREENING_FAILED
                if "screening" in inventory.sanitized_detail.casefold()
                else RunStatus.FAILED
            ),
            changed_paths=safe_changed,
            skipped_inputs=skipped,
            files_analyzed=len(snapshots.snapshots),
            uncertainties=("Markdown inventory was incomplete; proposals were withheld.",),
            not_found=("No proposals were generated from incomplete evidence.",),
        )
        return _finish(
            config,
            report,
            scanner,
            repo_root=repo_root,
            repository_inputs=repository_inputs,
        )

    if _contains_instruction_like_text(snapshots, inventory):
        report = _base_report(
            config,
            scanner,
            status=RunStatus.COMPLETED,
            changed_paths=safe_changed,
            skipped_inputs=skipped,
            files_analyzed=len(snapshots.snapshots) + len(inventory.documents),
            uncertainties=(
                "Instruction-like repository text was withheld; proposals were blocked.",
            ),
        )
        return _finish(
            config,
            report,
            scanner,
            repo_root=repo_root,
            repository_inputs=repository_inputs,
            original_documents={document.path: document.text for document in inventory.documents},
        )

    proposals: list[ProposalCandidate] = []
    validator_results: list[ValidatorResult] = []
    uncertainties: list[str] = []
    not_found: list[str] = []
    warnings: list[str] = []
    affected: set[str] = set()
    supported_snapshots = tuple(
        snapshot
        for snapshot in snapshots.snapshots
        if PurePosixPath(snapshot.path).suffix in {".py", ".json", ".toml"}
    )
    for snapshot in supported_snapshots:
        single_source = replace(snapshots, snapshots=(snapshot,))
        try:
            selection = select_targets(single_source, inventory, scanner=scanner)
        except Exception:
            uncertainties.append("Documentation target selection failed safely; the source was withheld.")
            continue
        affected.update(_safe_selection_paths(selection, scanner))
        if selection.ambiguities:
            for ambiguity in selection.ambiguities:
                candidates = tuple(
                    path
                    for path in ambiguity.candidate_paths
                    if _safe_path(scanner, path) is not None
                )
                suffix = f" Candidates: {', '.join(candidates)}." if candidates else ""
                uncertainties.append("Documentation target selection is ambiguous." + suffix)
        if selection.conflicts:
            for conflict in selection.conflicts:
                candidates = tuple(
                    path
                    for path in conflict.candidate_paths
                    if _safe_path(scanner, path) is not None
                )
                suffix = f" Candidates: {', '.join(candidates)}." if candidates else ""
                uncertainties.append("Existing documentation anchors conflict." + suffix)
        if selection.no_target:
            not_found.append(f"No suitable documentation target was found for {snapshot.path}.")
            continue
        if selection.ambiguities or selection.conflicts or not selection.proposal_eligible:
            continue

        result = propose_deterministic(single_source, inventory, selection, scanner=scanner)
        if result.status is ProposalStatus.PROPOSED and result.proposal is not None:
            candidate_results = _validate_candidate(
                result.proposal, single_source, inventory, repo_root, scanner
            )
            if any(
                validation.validator == "consistency" and validation.status is ValidatorStatus.PASS
                for validation in candidate_results
            ):
                proposals.append(result.proposal)
            else:
                uncertainties.append(
                    f"{snapshot.path}: proposal withheld because source consistency was not verified."
                )
            validator_results.extend(candidate_results)
            for validation in candidate_results:
                if validation.status is not ValidatorStatus.PASS:
                    warnings.append(
                        f"{validation.validator} validation reported {validation.status.value}."
                    )
        elif result.status is ProposalStatus.NOT_FOUND:
            not_found.append(f"{snapshot.path}: no supported documentation anchor was found.")
        else:
            uncertainties.append(f"{snapshot.path}: deterministic proposal support was insufficient.")

    if not supported_snapshots:
        not_found.append("No supported changed source input was available for deterministic proposals.")
    if not proposals and not not_found and not uncertainties:
        uncertainties.append("No proposal could be established from the available evidence.")
    status = (
        RunStatus.VALIDATION_FAILED
        if any(item.status is ValidatorStatus.FAIL for item in validator_results)
        else RunStatus.COMPLETED
    )
    if any(item.reason == "excluded" for item in skipped):
        warnings.append("Configured exclusions omitted one or more inputs from analysis.")

    report = _base_report(
        config,
        scanner,
        status=status,
        changed_paths=safe_changed,
        skipped_inputs=skipped,
        files_analyzed=len(snapshots.snapshots) + len(inventory.documents),
        warnings=tuple(dict.fromkeys(warnings)),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
        not_found=tuple(dict.fromkeys(not_found)),
        validators=tuple(validator_results),
        affected_documents=tuple(sorted(affected | {item.target_path for item in proposals})),
        proposals=tuple(proposals),
    )
    return _finish(
        config,
        report,
        scanner,
        repo_root=repo_root,
        repository_inputs=repository_inputs,
        original_documents={document.path: document.text for document in inventory.documents},
    )