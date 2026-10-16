from pathlib import Path

import pytest

from docsync.models import ArtifactOutcome, ArtifactStatus, ValidatorResult, ValidatorStatus
from docsync.proposals import ProposalCandidate, ProposalKind, ProposalProvenance
from docsync.reporting import ReportData, ReportSafetyError, format_terminal_summary, render_report


def _proposal() -> ProposalCandidate:
    return ProposalCandidate(
        ProposalKind.CONFIG_SCALAR,
        "docs/guide.md",
        "# Guide\n\nRun `new()` here.\n",
        ("src/app.py",),
        "Update the documented callable.",
        ProposalProvenance(
            "src/app.py",
            12,
            "@@ -10,2 +10,2 @@",
            4,
            "docs/guide.md",
            3,
            "python_signature",
            "old()",
            "new()",
        ),
    )


def test_report_includes_required_run_proposal_validation_and_artifact_fields() -> None:
    report = render_report(
        ReportData(
            execution_status="completed",
            selected_scope="base-head",
            base_endpoint="base-ref",
            head_endpoint="head-ref",
            files_analyzed=2,
            changed_paths=("src/app.py",),
            affected_documents=("docs/guide.md",),
            proposals=(_proposal(),),
            validators=(ValidatorResult("markdown", ValidatorStatus.PASS, "Syntax is valid."),),
            warnings=("Pattern-based secret detection is incomplete.",),
            uncertainties=("One target remains ambiguous.",),
            not_found=("No supported change was found for settings.",),
            resource_limits=("Diff limit: 1048576 bytes.",),
            unresolved_items=("Manual review is required.",),
            artifact_paths=("C:/repo/docsync-output/docsync-report.md", "C:/repo/docsync-output/docsync.patch"),
        )
    )

    for expected in (
        "Selected scope: base-head",
        "Base endpoint:",
        "Head endpoint:",
        "Files analyzed: 2",
        "Changed paths: 1",
        "Affected Documentation",
        "Proposals",
        "src/app.py",
        "@@ -10,2 +10,2 @@",
        "markdown: pass",
        "Warnings",
        "Uncertainties",
        "Not Found",
        "Resource Limits",
        "Unresolved Items",
        "docsync-report.md",
    ):
        assert expected in report


def test_report_rejects_secret_in_path_or_proposal_provenance() -> None:
    report = ReportData(
        execution_status="completed",
        selected_scope="working-tree",
        changed_paths=("src/app.py",),
        affected_documents=("docs/api_key=known-secret.md",),
    )

    with pytest.raises(ReportSafetyError):
        render_report(report)


def test_terminal_summary_uses_actual_partial_artifact_outcomes(tmp_path: Path) -> None:
    report = ReportData(
        execution_status="completed",
        selected_scope="working-tree",
        files_analyzed=2,
        changed_paths=("src/app.py",),
    )
    summary = format_terminal_summary(
        report,
        (
            ArtifactOutcome("docsync-report.md", tmp_path / "report.md", ArtifactStatus.WRITTEN, "Written."),
            ArtifactOutcome("docsync.patch", tmp_path / "docsync.patch", ArtifactStatus.FAILED, "Not written."),
        ),
    )

    assert "DocSync: partial_artifacts" in summary
    assert "docsync.patch: failed" in summary


def test_terminal_summary_withholds_secret_like_fields(tmp_path: Path) -> None:
    report = ReportData(
        execution_status="completed",
        selected_scope="token=secret-value",
        validators=(ValidatorResult("password=hidden-value", ValidatorStatus.FAIL, "Failed safely."),),
    )
    summary = format_terminal_summary(
        report,
        (ArtifactOutcome("docsync-report.md", tmp_path / "report.md", ArtifactStatus.WRITTEN, "Written."),),
    )

    assert "secret-value" not in summary
    assert "hidden-value" not in summary
    assert "[withheld]" in summary
