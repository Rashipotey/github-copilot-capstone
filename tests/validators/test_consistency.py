from __future__ import annotations

import difflib
from dataclasses import replace

import pytest

from docsync.evidence import (
    MarkdownDocument,
    MarkdownInventory,
    ScreenedSide,
    ScreenedSnapshot,
    ScreenedSnapshots,
)
from docsync.git import SnapshotSideState
from docsync.models import DiffScope, StageStatus, ValidatorStatus
from docsync.proposals import ProposalKind, ProposalStatus, propose_deterministic
from docsync.screening import ScreeningState, SecretScanner
from docsync.targets import SelectedTarget, TargetSelection
from docsync.validators import consistency
from docsync.validators.consistency import validate_consistency


def context(old: str, new: str, document: str, source_path: str):
    target_path = "docs/reference.md"
    snapshot = ScreenedSnapshot(
        source_path,
        DiffScope.WORKING_TREE,
        ScreenedSide(SnapshotSideState.PRESENT, old, "a" * 40, "100644"),
        ScreenedSide(SnapshotSideState.PRESENT, new, "b" * 40, "100644"),
        False,
        False,
        "index",
        "working-tree",
    )
    diff = f"diff --git a/{source_path} b/{source_path}\n" + "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{source_path}",
            tofile=f"b/{source_path}",
            n=3,
        )
    )
    snapshots = ScreenedSnapshots(
        (snapshot,), StageStatus.COMPLETED, True, "Complete.", len(old) + len(new), diff_text=diff
    )
    inventory = MarkdownInventory(
        (MarkdownDocument(target_path, document),),
        StageStatus.COMPLETED,
        True,
        "Complete.",
        len(document),
    )
    selection = TargetSelection(
        (SelectedTarget(target_path, (source_path,), tuple(range(1, len(document.splitlines()) + 1)), "Unique."),),
        (), (), (), False, StageStatus.COMPLETED, True, "Selected.", (),
        ((source_path, "working-tree", "index", "working-tree"),),
    )
    result = propose_deterministic(snapshots, inventory, selection, scanner=SecretScanner())
    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    return snapshots, inventory, result.proposal


def python_context():
    old = "def fetch(value: int) -> int:\n    return value\n"
    new = "def fetch(value: str) -> int:\n    return value\n"
    document = (
        "API: `src/api.py`\n\n"
        "```python\n"
        "def fetch(value: int) -> int:\n"
        "    pass\n"
        "```\n"
    )
    return context(old, new, document, "src/api.py")


def test_exact_python_signature_and_provenance_pass() -> None:
    snapshots, inventory, candidate = python_context()

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.PASS
    assert result.evidence_refs == ("src/api.py", "docs/reference.md")


@pytest.mark.parametrize(
    ("old_header", "new_header", "changed_marker"),
    [
        (
            "def run(\n    value: str,\n) -> str:",
            "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:",
            "+    enabled: bool = False,",
        ),
        (
            "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:",
            "def run(\n    value: str,\n) -> str:",
            "-    enabled: bool = False,",
        ),
    ],
)
def test_separate_parameter_line_insertion_and_deletion_pass(
    old_header: str, new_header: str, changed_marker: str
) -> None:
    old = old_header + "\n    return value\n"
    new = new_header + "\n    return value\n"
    document = f"API: `src/api.py`\n```python\n{old_header}\n```\n"
    snapshots, inventory, candidate = context(old, new, document, "src/api.py")

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.PASS
    assert changed_marker in (snapshots.diff_text or "")


def test_unrelated_source_hunk_cannot_pass_on_signature_claim_alone() -> None:
    old_header = "def run(\n    value: str,\n) -> str:"
    new_header = "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:"
    base_old = old_header + "\n    return value\n"
    base_new = new_header + "\n    return value\n"
    document = f"API: `src/api.py`\n```python\n{old_header}\n```\n"
    snapshots, inventory, candidate = context(base_old, base_new, document, "src/api.py")
    old = "# old note\n" + base_old
    new = "# revised note\n" + base_new
    snapshot = replace(
        snapshots.snapshots[0],
        old=replace(snapshots.snapshots[0].old, text=old),
        new=replace(snapshots.snapshots[0].new, text=new),
    )
    snapshots = replace(
        snapshots,
        snapshots=(snapshot,),
        diff_text="diff --git a/src/api.py b/src/api.py\n" + "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="a/src/api.py",
                tofile="b/src/api.py",
                n=3,
            )
        ),
    )
    candidate = replace(
        candidate,
        provenance=replace(
            candidate.provenance,
            source_line=2,
            diff_hunk="@@ -1,5 +1,6 @@",
            diff_line=4,
        ),
    )

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.FAIL
    assert "only to the recorded signature header" in result.sanitized_detail


def test_mismatched_one_sided_hunk_fails_consistency_validation() -> None:
    old_header = "def run(\n    value: str,\n) -> str:"
    new_header = "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:"
    document = f"API: `src/api.py`\n```python\n{old_header}\n```\n"
    snapshots, inventory, candidate = context(
        old_header + "\n    return value\n",
        new_header + "\n    return value\n",
        document,
        "src/api.py",
    )
    snapshots = replace(
        snapshots,
        diff_text=(snapshots.diff_text or "").replace(
            "+    enabled: bool = False,", "+    unrelated: bool = False,"
        ),
    )

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.FAIL


def test_exact_config_scalar_and_structured_row_pass() -> None:
    snapshots, inventory, candidate = context(
        '{"service": {"timeout": 10}}\n',
        '{"service": {"timeout": 15}}\n',
        "| Path | Value |\n| --- | --- |\n| /service/timeout | 10 |\n",
        "src/settings.json",
    )

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.PASS
    assert candidate.provenance.old_value == "10"
    assert candidate.provenance.new_value == "15"


def test_unrelated_proposed_document_change_fails() -> None:
    snapshots, inventory, candidate = python_context()
    candidate = replace(candidate, replacement=candidate.replacement + "Unrelated change.\n")

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.FAIL


def test_candidate_provenance_must_match_source_and_target() -> None:
    snapshots, inventory, candidate = python_context()
    candidate = replace(
        candidate,
        provenance=replace(candidate.provenance, source_path="src/other.py"),
    )

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.FAIL


def test_source_contradicting_diff_fails() -> None:
    snapshots, inventory, candidate = python_context()
    changed_diff = snapshots.diff_text.replace(
        "+def fetch(value: str) -> int:", "+def fetch(value: bytes) -> int:"
    )
    snapshots = replace(snapshots, diff_text=changed_diff)

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.FAIL


def test_unsupported_change_warns_without_claiming_pass() -> None:
    snapshots, inventory, candidate = python_context()
    candidate = replace(
        candidate,
        kind=ProposalKind.CONFIG_SCALAR,
        provenance=replace(candidate.provenance, replacement_kind=ProposalKind.CONFIG_SCALAR.value),
    )

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.WARNING


def test_competing_target_anchor_warns() -> None:
    snapshots, inventory, candidate = python_context()
    duplicate = MarkdownDocument("docs/other.md", inventory.documents[0].text)
    inventory = replace(inventory, documents=(*inventory.documents, duplicate))

    result = validate_consistency(candidate, snapshots, inventory)

    assert result.status is ValidatorStatus.WARNING


def test_missing_or_non_unique_anchor_is_inconclusive() -> None:
    snapshots, inventory, candidate = python_context()
    missing = replace(candidate, provenance=replace(candidate.provenance, target_line=99))
    duplicate_doc = inventory.documents[0].text + "\n```python\ndef fetch(value: int) -> int:\n    pass\n```\n"
    duplicate_inventory = replace(
        inventory,
        documents=(replace(inventory.documents[0], text=duplicate_doc),),
    )

    assert validate_consistency(missing, snapshots, inventory).status is ValidatorStatus.INCONCLUSIVE
    assert validate_consistency(candidate, snapshots, duplicate_inventory).status is ValidatorStatus.INCONCLUSIVE


def test_missing_or_incomplete_evidence_is_never_pass() -> None:
    snapshots, inventory, candidate = python_context()
    incomplete_snapshots = replace(snapshots, complete=False)
    missing_target = replace(inventory, documents=())

    assert validate_consistency(candidate, incomplete_snapshots, inventory).status is ValidatorStatus.INCONCLUSIVE
    assert validate_consistency(candidate, snapshots, missing_target).status is ValidatorStatus.INCONCLUSIVE


@pytest.mark.parametrize("failed_path", ["src/api.py", "docs/reference.md"])
@pytest.mark.parametrize("failure", ["raises", "incomplete", "excluded", "missing"])
def test_provenance_screening_failure_is_sanitized_and_never_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture, failed_path: str, failure: str,
) -> None:
    snapshots, inventory, candidate = python_context()
    scanner = SecretScanner()
    screen_path = scanner.screen_repository_path
    raw_error = "password=fixture-provenance-error-123"

    def screen_with_failure(path: str):
        if path != failed_path:
            return screen_path(path)
        if failure == "raises":
            raise RuntimeError(raw_error)
        if failure == "missing":
            return None
        return replace(
            screen_path(path),
            state=ScreeningState.INCONCLUSIVE if failure == "incomplete" else ScreeningState.COMPLETE,
            excluded=failure == "excluded",
            diagnostic=raw_error,
        )

    monkeypatch.setattr(scanner, "screen_repository_path", screen_with_failure)

    result = validate_consistency(candidate, snapshots, inventory, scanner=scanner)

    assert result.status is ValidatorStatus.INCONCLUSIVE
    assert result.evidence_refs == ()
    assert result.sanitized_detail == "Consistency provenance paths could not be screened safely."
    captured = capsys.readouterr()
    assert raw_error not in repr(result) + captured.out + captured.err + caplog.text


@pytest.mark.parametrize("suffix", ["json", "toml"])
@pytest.mark.parametrize("failed_call", [3, 4])
@pytest.mark.parametrize("failure", ["missing", "raises"])
def test_config_reparse_failure_is_sanitized_and_never_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture, suffix: str, failed_call: int, failure: str,
) -> None:
    old = '{"timeout": 10}\n' if suffix == "json" else "timeout = 10\n"
    new = '{"timeout": 15}\n' if suffix == "json" else "timeout = 15\n"
    snapshots, inventory, candidate = context(
        old, new, "| Path | Value |\n| --- | --- |\n| /timeout | 10 |\n",
        f"src/settings.{suffix}",
    )
    parse_config = consistency._parse_config
    calls = 0
    raw_error = "password=fixture-reparse-error-456"

    def parse_with_failure(source: str, source_suffix: str):
        nonlocal calls
        calls += 1
        if calls == failed_call:
            if failure == "raises":
                raise ValueError(raw_error)
            return None
        return parse_config(source, source_suffix)

    monkeypatch.setattr(consistency, "_parse_config", parse_with_failure)

    result = validate_consistency(candidate, snapshots, inventory)

    assert calls >= failed_call
    assert result.status is ValidatorStatus.INCONCLUSIVE
    assert result.sanitized_detail == (
        "Configuration evidence could not be reparsed safely." if failure == "missing"
        else "Source-to-documentation evidence could not be checked safely."
    )
    captured = capsys.readouterr()
    assert raw_error not in repr(result) + captured.out + captured.err + caplog.text
