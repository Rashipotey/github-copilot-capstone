from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import docsync.artifacts as artifacts
from docsync.artifacts import build_patch, write_artifacts
from docsync.models import ArtifactStatus, RunStatus
from docsync.proposals import ProposalCandidate, ProposalKind, ProposalProvenance
from docsync.reporting import ReportData
from docsync.screening import SecretScanner


ORIGINAL = "# Guide\n\nRun `old()` here.\n"
REPLACEMENT = "# Guide\n\nRun `new()` here.\n"


def _proposal(
    *,
    target: str = "docs/guide.md",
    original: str = ORIGINAL,
    replacement: str = REPLACEMENT,
) -> ProposalCandidate:
    return ProposalCandidate(
        ProposalKind.PYTHON_SIGNATURE,
        target,
        replacement,
        ("src/app.py",),
        "Update the documented signature.",
        ProposalProvenance(
            "src/app.py",
            12,
            "@@ -10,2 +10,2 @@",
            4,
            target,
            3,
            "python_signature",
            "old()",
            "new()",
        ),
    )


def _report(proposals=()) -> ReportData:
    return ReportData(
        execution_status="completed",
        selected_scope="working-tree",
        files_analyzed=1,
        changed_paths=("src/app.py",),
        affected_documents=("docs/guide.md",),
        proposals=tuple(proposals),
    )


def test_patch_builder_emits_standard_unified_patch_without_applying_it() -> None:
    result = build_patch((_proposal(),), {"docs/guide.md": ORIGINAL})

    assert result.content is not None
    patch = result.content.decode("utf-8")
    assert patch.startswith("--- a/docs/guide.md\n+++ b/docs/guide.md\n")
    assert "@@" in patch
    assert "-Run `old()` here." in patch
    assert "+Run `new()` here." in patch
    assert result.proposals == (_proposal(),)


@pytest.mark.parametrize(
    ("original", "replacement", "patch_line"),
    [
        (
            "# API\n\n```python\ndef run(\n    value: str,\n) -> str:\n```\n",
            "# API\n\n```python\ndef run(\n    value: str,\n    enabled: bool = False,\n) -> str:\n```\n",
            "+    enabled: bool = False,",
        ),
        (
            "# API\n\n```python\ndef run(\n    value: str,\n    obsolete: bool = False,\n) -> str:\n```\n",
            "# API\n\n```python\ndef run(\n    value: str,\n) -> str:\n```\n",
            "-    obsolete: bool = False,",
        ),
    ],
)
def test_patch_builder_serializes_single_signature_line_insertions_and_deletions(
    original: str, replacement: str, patch_line: str
) -> None:
    proposal = _proposal(original=original, replacement=replacement)

    result = build_patch((proposal,), {"docs/guide.md": original})

    assert result.content is not None
    patch = result.content.decode("utf-8")
    assert patch.startswith("--- a/docs/guide.md\n+++ b/docs/guide.md\n")
    assert patch_line in patch
    assert result.proposals == (proposal,)


def test_patch_builder_rejects_noop_and_multiple_changed_spans() -> None:
    no_op = _proposal(original=ORIGINAL, replacement=ORIGINAL)
    multiple = _proposal(
        original="# Guide\n\nfirst old(); second old().\n",
        replacement="# Guide\n\nfirst new(); second newer().\n",
    )

    no_op_result = build_patch((no_op,), {"docs/guide.md": ORIGINAL})
    multiple_result = build_patch(
        (multiple,), {"docs/guide.md": "# Guide\n\nfirst old(); second old().\n"}
    )

    assert no_op_result.content is None
    assert multiple_result.content is None


@pytest.mark.parametrize("insertion", ["abXcdef\n", "abcdXef\n"])
def test_insertion_at_replacement_boundary_is_rejected(insertion: str) -> None:
    original = "abcdef\n"
    insert_proposal = _proposal(original=original, replacement=insertion)
    replace_proposal = _proposal(original=original, replacement="abZZef\n")

    result = build_patch(
        (insert_proposal, replace_proposal), {"docs/guide.md": original}
    )

    assert result.content is None
    assert "overlapping" in result.sanitized_detail


def test_insertions_at_same_position_are_rejected() -> None:
    original = "abcdef\n"
    first = _proposal(original=original, replacement="abXcdef\n")
    second = _proposal(original=original, replacement="abYcdef\n")

    result = build_patch((first, second), {"docs/guide.md": original})

    assert result.content is None
    assert "overlapping" in result.sanitized_detail


def test_patch_builder_returns_valid_empty_patch_without_proposals() -> None:
    result = build_patch((), {})

    assert result.content == b""


def test_overlapping_proposals_fail_safely() -> None:
    first = _proposal()
    second = _proposal(replacement="# Guide\n\nRun `other()` here.\n")

    result = build_patch((first, second), {"docs/guide.md": ORIGINAL})

    assert result.content is None
    assert "overlapping" in result.sanitized_detail


def test_non_overlapping_proposals_in_one_document_are_combined() -> None:
    original = "# Guide\n\nFirst old(); second old().\n"
    first_replacement = "# Guide\n\nFirst new(); second old().\n"
    second_replacement = "# Guide\n\nFirst old(); second newer().\n"
    first = _proposal(original=original, replacement=first_replacement)
    second = _proposal(original=original, replacement=second_replacement)
    second = ProposalCandidate(
        second.kind,
        second.target_path,
        second.replacement,
        second.source_paths,
        second.sanitized_reason,
        ProposalProvenance(
            second.provenance.source_path,
            second.provenance.source_line,
            second.provenance.diff_hunk,
            second.provenance.diff_line,
            second.provenance.target_path,
            second.provenance.target_line,
            second.provenance.replacement_kind,
            "old()",
            "newer()",
        ),
    )

    result = build_patch((first, second), {"docs/guide.md": original})

    assert result.content is not None
    patch = result.content.decode("utf-8")
    assert "-First old(); second old()." in patch
    assert "+First new(); second newer()." in patch


def test_default_and_custom_output_locations_create_exactly_two_artifacts(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "docs").mkdir()
    result = write_artifacts(
        git_repo,
        _report((_proposal(),)),
        {"docs/guide.md": ORIGINAL},
        repository_inputs=("docs/guide.md", "src/app.py"),
    )

    default_dir = git_repo / "docsync-output"
    assert result.status is RunStatus.COMPLETED
    assert sorted(path.name for path in default_dir.iterdir()) == ["docsync-report.md", "docsync.patch"]
    assert (default_dir / "docsync.patch").read_bytes() == result.patch_content

    custom = tmp_path / "custom-output"
    custom_result = write_artifacts(
        git_repo,
        _report(),
        {},
        output_dir=custom,
    )
    assert custom_result.status is RunStatus.COMPLETED
    assert sorted(path.name for path in custom.iterdir()) == ["docsync-report.md", "docsync.patch"]
    assert (custom / "docsync.patch").read_bytes() == b""


def test_temporary_artifacts_are_not_staged_in_source_markdown_directory(git_repo: Path) -> None:
    (git_repo / "docs").mkdir()
    result = write_artifacts(
        git_repo,
        _report(),
        {},
        output_dir=git_repo / "docs" / "generated",
        repository_inputs=("docs/guide.md",),
    )

    assert result.status is RunStatus.COMPLETED
    assert sorted(path.name for path in (git_repo / "docs" / "generated").iterdir()) == [
        "docsync-report.md",
        "docsync.patch",
    ]
    assert sorted(path.name for path in (git_repo / "docs").iterdir()) == ["generated"]


@pytest.mark.parametrize("existing_name", ["docsync-report.md", "docsync.patch"])
def test_existing_artifact_blocks_both_writes(git_repo: Path, existing_name: str) -> None:
    existing = git_repo / "docsync-output" / existing_name
    existing.parent.mkdir()
    existing.write_text("keep", encoding="utf-8")

    result = write_artifacts(git_repo, _report(), {}, repository_inputs=())

    assert result.status is RunStatus.FAILED
    assert all(outcome.status is ArtifactStatus.FAILED for outcome in result.outcomes)
    assert existing.read_text(encoding="utf-8") == "keep"
    assert list(existing.parent.iterdir()) == [existing]


def test_rejects_non_regular_existing_target(git_repo: Path) -> None:
    output = git_repo / "docsync-output"
    output.mkdir()
    (output / "docsync.patch").mkdir()

    result = write_artifacts(git_repo, _report(), {})

    assert result.status is RunStatus.FAILED
    assert (output / "docsync.patch").is_dir()


def test_source_collision_is_rejected(git_repo: Path) -> None:
    source = git_repo / "docsync-report.md"
    source.write_text("original", encoding="utf-8")

    result = write_artifacts(
        git_repo,
        _report(),
        {},
        output_dir=git_repo,
        repository_inputs=("docsync-report.md",),
    )

    assert result.status is RunStatus.FAILED
    assert source.read_text(encoding="utf-8") == "original"


def test_known_secret_in_proposal_is_withheld_from_report_and_patch(git_repo: Path) -> None:
    secret = "api_key=known-secret-123"
    proposal = _proposal(replacement=f"# Guide\n\n{secret}\n")

    result = write_artifacts(
        git_repo,
        _report((proposal,)),
        {"docs/guide.md": ORIGINAL},
        repository_inputs=("docs/guide.md",),
    )

    output = git_repo / "docsync-output"
    assert result.status is RunStatus.COMPLETED
    assert secret.encode() not in (output / "docsync-report.md").read_bytes()
    assert secret.encode() not in (output / "docsync.patch").read_bytes()
    assert result.proposals == ()
    assert "unsafe proposals were withheld" in (output / "docsync-report.md").read_text(encoding="utf-8")


class FailingOutputScanner(SecretScanner):
    def screen_output(self, output):
        raise RuntimeError("raw exception must not escape")


def test_scanner_failure_fails_closed_without_creating_artifacts(git_repo: Path) -> None:
    result = write_artifacts(git_repo, _report(), {}, scanner=FailingOutputScanner())

    assert result.status is RunStatus.FAILED
    assert not (git_repo / "docsync-output").exists()
    assert all("raw exception" not in outcome.sanitized_detail for outcome in result.outcomes)


class LeakingPatchScanner(SecretScanner):
    def screen_output(self, output):
        if isinstance(output, bytes) and output.startswith(b"--- a/"):
            return super().screen_output(b"password=known-output-leak")
        return super().screen_output(output)


def test_final_patch_output_leak_withholds_patch_and_reports_it(git_repo: Path) -> None:
    result = write_artifacts(
        git_repo,
        _report((_proposal(),)),
        {"docs/guide.md": ORIGINAL},
        scanner=LeakingPatchScanner(),
    )

    output = git_repo / "docsync-output"
    assert result.status is RunStatus.PARTIAL_ARTIFACTS
    assert (output / "docsync-report.md").exists()
    assert not (output / "docsync.patch").exists()
    report_text = (output / "docsync-report.md").read_text(encoding="utf-8")
    assert "Patch artifact could not be staged safely" in report_text
    assert "Status: partial\\_artifacts" in report_text
    assert "known-output-leak" not in report_text


@pytest.mark.parametrize("failed_target", ["docsync.patch", "docsync-report.md"])
def test_temporary_write_failure_has_truthful_partial_outcomes(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_target: str,
) -> None:
    original_stage = artifacts._stage_bytes
    calls = 0

    def fail_selected_stage(data, directory, scanner, proposals):
        nonlocal calls
        calls += 1
        if (failed_target == "docsync.patch" and calls == 1) or (
            failed_target == "docsync-report.md" and calls == 2
        ):
            raise OSError("disk details must not escape")
        return original_stage(data, directory, scanner, proposals)

    monkeypatch.setattr(artifacts, "_stage_bytes", fail_selected_stage)
    result = write_artifacts(git_repo, _report(), {})

    statuses = {item.name: item.status for item in result.outcomes}
    assert result.status is RunStatus.PARTIAL_ARTIFACTS
    assert statuses[failed_target] is ArtifactStatus.FAILED
    assert statuses["docsync.patch" if failed_target == "docsync-report.md" else "docsync-report.md"] is ArtifactStatus.WRITTEN
    assert "disk details" not in result.sanitized_detail
    assert not list(git_repo.glob(".docsync-*.tmp"))


@pytest.mark.parametrize("failed_target", ["docsync.patch", "docsync-report.md"])
def test_atomic_replace_failure_has_truthful_partial_outcomes(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_target: str,
) -> None:
    original_publish = artifacts._publish_staged_file

    def fail_selected_publish(temporary, target, data, scanner, proposals):
        if target.name == failed_target:
            raise OSError("publish details must not escape")
        return original_publish(temporary, target, data, scanner, proposals)

    monkeypatch.setattr(artifacts, "_publish_staged_file", fail_selected_publish)
    result = write_artifacts(git_repo, _report(), {})

    statuses = {item.name: item.status for item in result.outcomes}
    assert result.status is RunStatus.PARTIAL_ARTIFACTS
    assert statuses[failed_target] is ArtifactStatus.FAILED
    other = "docsync.patch" if failed_target == "docsync-report.md" else "docsync-report.md"
    assert statuses[other] is ArtifactStatus.WRITTEN
    assert "publish details" not in result.sanitized_detail
    if failed_target == "docsync.patch":
        report_text = (git_repo / "docsync-output" / "docsync-report.md").read_text(encoding="utf-8")
        assert "Patch artifact creation failed" in report_text
        assert "Status: partial\\_artifacts" in report_text
    assert not list(git_repo.glob(".docsync-*.tmp"))


def test_target_appearing_after_preflight_is_never_replaced(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_link = artifacts._link_anchored
    raced_target = git_repo / "docsync-output" / "docsync-report.md"

    def create_target_then_link(source, target, source_anchor, target_anchor):
        if Path(target) == raced_target and not raced_target.exists():
            raced_target.write_bytes(b"created by another process")
        return original_link(source, target, source_anchor, target_anchor)

    monkeypatch.setattr(artifacts, "_link_anchored", create_target_then_link)
    result = write_artifacts(git_repo, _report(), {})

    statuses = {item.name: item.status for item in result.outcomes}
    assert result.status is RunStatus.PARTIAL_ARTIFACTS
    assert statuses["docsync-report.md"] is ArtifactStatus.FAILED
    assert statuses["docsync.patch"] is ArtifactStatus.WRITTEN
    assert raced_target.read_bytes() == b"created by another process"
    assert not list(git_repo.glob(".docsync-*.tmp"))


def test_publication_succeeds_after_post_link_identity_verification(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()
    output.mkdir()
    scanner = SecretScanner()
    temporary = artifacts._stage_bytes(b"artifact", staging, scanner, ())
    with artifacts._DirectoryAnchor(staging) as source_anchor:
        staged_identity = source_anchor.file_identity(temporary.name)

    artifacts._publish_staged_file(
        temporary,
        output / "docsync.patch",
        b"artifact",
        scanner,
        (),
    )

    with artifacts._DirectoryAnchor(output) as target_anchor:
        assert target_anchor.file_identity("docsync.patch") == staged_identity
    assert (output / "docsync.patch").read_bytes() == b"artifact"
    assert not temporary.exists()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows handle-based identity cleanup")
def test_publication_rolls_back_only_its_target_after_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    moved_output = tmp_path / "moved-output"
    staging.mkdir()
    output.mkdir()
    (output / "keep.txt").write_bytes(b"existing anchored file")
    temporary = artifacts._stage_bytes(b"artifact", staging, SecretScanner(), ())
    original_link = artifacts._link_anchored

    def link_then_swap(source, target, source_anchor, target_anchor):
        original_link(source, target, source_anchor, target_anchor)
        output.rename(moved_output)
        output.mkdir()
        (output / target.name).write_bytes(b"preexisting replacement target")
        (output / "keep.txt").write_bytes(b"replacement directory file")

    monkeypatch.setattr(artifacts, "_link_anchored", link_then_swap)
    with pytest.raises(OSError, match="artifact was rolled back"):
        artifacts._publish_staged_file(
            temporary,
            output / "docsync.patch",
            b"artifact",
            SecretScanner(),
            (),
        )

    assert not (moved_output / "docsync.patch").exists()
    assert (moved_output / "keep.txt").read_bytes() == b"existing anchored file"
    assert (output / "docsync.patch").read_bytes() == b"preexisting replacement target"
    assert (output / "keep.txt").read_bytes() == b"replacement directory file"
    artifacts._cleanup_temporary(temporary)
    assert not temporary.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-verified rollback")
def test_publication_preserves_target_when_rollback_identity_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    moved_output = tmp_path / "moved-output"
    staging.mkdir()
    output.mkdir()
    temporary = artifacts._stage_bytes(b"artifact", staging, SecretScanner(), ())
    original_link = artifacts._link_anchored

    def replace_after_link(source, target, source_anchor, target_anchor):
        original_link(source, target, source_anchor, target_anchor)
        output.rename(moved_output)
        (moved_output / target.name).unlink()
        (moved_output / target.name).write_bytes(b"created by another process")
        output.mkdir()
        (output / target.name).write_bytes(b"preexisting replacement target")

    monkeypatch.setattr(artifacts, "_link_anchored", replace_after_link)
    with pytest.raises(OSError, match="rollback was not safe"):
        artifacts._publish_staged_file(
            temporary,
            output / "docsync.patch",
            b"artifact",
            SecretScanner(),
            (),
        )

    assert (moved_output / "docsync.patch").read_bytes() == b"created by another process"
    assert (output / "docsync.patch").read_bytes() == b"preexisting replacement target"
    artifacts._cleanup_temporary(temporary)
    assert not temporary.exists()


def test_publication_fails_closed_when_directory_anchor_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()
    output.mkdir()
    temporary = staging / ".docsync-owned.tmp"
    temporary.write_bytes(b"artifact")

    class MissingAnchor:
        def __init__(self, _directory):
            raise OSError("anchor unavailable")

    monkeypatch.setattr(artifacts, "_DirectoryAnchor", MissingAnchor)
    with pytest.raises(OSError):
        artifacts._publish_staged_file(
            temporary,
            output / "docsync.patch",
            b"artifact",
            SecretScanner(),
            (),
        )

    assert not (output / "docsync.patch").exists()
    assert temporary.read_bytes() == b"artifact"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows directory-handle semantics")
def test_windows_directory_anchor_rejects_output_directory_rename(tmp_path: Path) -> None:
    output = tmp_path / "output"
    moved_output = tmp_path / "moved-output"
    output.mkdir()

    with artifacts._DirectoryAnchor(output) as anchor:
        output.rename(moved_output)
        try:
            with pytest.raises(OSError):
                anchor.verify()
        finally:
            moved_output.rename(output)

    assert output.is_dir()
    assert not moved_output.exists()


def test_writer_does_not_change_source_documents_or_git_index(git_repo: Path) -> None:
    source = git_repo / "docs" / "guide.md"
    source.parent.mkdir()
    source.write_text(ORIGINAL, encoding="utf-8")
    source_before = source.read_bytes()
    index_before = subprocess.run(
        ["git", "-C", str(git_repo), "diff", "--cached", "--binary"],
        check=True,
        capture_output=True,
    ).stdout

    result = write_artifacts(
        git_repo,
        _report((_proposal(),)),
        {"docs/guide.md": ORIGINAL},
        repository_inputs=("docs/guide.md",),
    )

    index_after = subprocess.run(
        ["git", "-C", str(git_repo), "diff", "--cached", "--binary"],
        check=True,
        capture_output=True,
    ).stdout
    assert result.status is RunStatus.COMPLETED
    assert source.read_bytes() == source_before
    assert index_after == index_before


def test_renderer_rejects_traversal_path() -> None:
    from docsync.reporting import ReportSafetyError, render_report

    with pytest.raises(ReportSafetyError):
        render_report(_report().__class__(
            execution_status="completed",
            selected_scope="working-tree",
            changed_paths=("../outside.py",),
        ))
