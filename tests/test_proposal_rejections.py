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
from docsync.models import DiffScope, StageStatus
from docsync.proposals import ProposalStatus, propose_deterministic
from docsync.screening import SecretScanner
from docsync.targets import SelectedTarget, TargetSelection


def case(path: str, old: str, new: str, markdown: str):
    snapshot = ScreenedSnapshot(
        path,
        DiffScope.WORKING_TREE,
        ScreenedSide(SnapshotSideState.PRESENT, old, "a" * 40, "100644"),
        ScreenedSide(SnapshotSideState.PRESENT, new, "b" * 40, "100644"),
        False,
        False,
        "index",
        "working-tree",
    )
    diff = f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    snapshots = ScreenedSnapshots(
        (snapshot,), StageStatus.COMPLETED, True, "Complete.", len(old) + len(new), diff_text=diff
    )
    target_path = "docs/reference.md"
    inventory = MarkdownInventory(
        (MarkdownDocument(target_path, markdown),),
        StageStatus.COMPLETED,
        True,
        "Complete.",
        len(markdown),
    )
    target = SelectedTarget(
        target_path, (path,), tuple(range(1, len(markdown.splitlines()) + 1)), "Unique source evidence."
    )
    selection = TargetSelection(
        (target,), (), (), (), False, StageStatus.COMPLETED, True, "Selected.", (),
        ((path, "working-tree", "index", "working-tree"),),
    )
    return snapshots, inventory, selection


def test_requires_screened_diff_and_matching_unique_target_provenance() -> None:
    old = "def load(value: int):\n    return value\n"
    new = "def load(value: str):\n    return value\n"
    markdown = "src/api.py\n```python\ndef load(value: int):\n    pass\n```\n"
    snapshots, inventory, selection = case("src/api.py", old, new, markdown)

    missing_diff = replace(snapshots, diff_text=None)
    missing_source_provenance = replace(selection, source_provenance=())
    wrong_target = replace(
        selection,
        targets=(SelectedTarget("docs/other.md", ("src/other.py",), (1,), "Mismatch."),),
    )
    duplicate_target = replace(selection, targets=(*selection.targets, selection.targets[0]))

    results = (
        propose_deterministic(missing_diff, inventory, selection),
        propose_deterministic(snapshots, inventory, missing_source_provenance),
        propose_deterministic(snapshots, inventory, wrong_target),
        propose_deterministic(snapshots, inventory, duplicate_target),
    )

    assert all(result.proposal is None for result in results)
    assert results[0].status is ProposalStatus.UNCERTAIN
    assert results[1].status is ProposalStatus.UNCERTAIN
    assert results[2].status is ProposalStatus.NOT_FOUND
    assert results[3].status is ProposalStatus.UNCERTAIN


@pytest.mark.parametrize("field", ["snapshots", "inventory", "selection"])
def test_incomplete_stage_evidence_never_produces_proposal(field: str) -> None:
    old = '{"mode": 1}\n'
    new = '{"mode": 2}\n'
    markdown = "src/settings.json\n| Key | Value |\n| --- | --- |\n| /mode | 1 |\n"
    values = list(case("src/settings.json", old, new, markdown))
    index = {"snapshots": 0, "inventory": 1, "selection": 2}[field]
    values[index] = replace(values[index], complete=False)

    result = propose_deterministic(*values)

    assert result.status is ProposalStatus.UNCERTAIN
    assert result.proposal is None


def test_snapshot_side_missing_or_unsupported_source_has_no_proposal() -> None:
    old = '{"mode": 1}\n'
    new = '{"mode": 2}\n'
    markdown = "src/settings.json\n| Key | Value |\n| --- | --- |\n| /mode | 1 |\n"
    snapshots, inventory, selection = case("src/settings.json", old, new, markdown)
    missing_side = replace(
        snapshots.snapshots[0],
        old=ScreenedSide(SnapshotSideState.MISSING, None, None, None),
    )
    incomplete_sides = replace(snapshots, snapshots=(missing_side,))
    unsupported = case("src/settings.yaml", "mode: 1\n", "mode: 2\n", markdown)

    results = (
        propose_deterministic(incomplete_sides, inventory, selection),
        propose_deterministic(*unsupported),
    )

    assert all(result.status is ProposalStatus.UNCERTAIN for result in results)
    assert all(result.proposal is None for result in results)


def test_proposal_contains_structured_sanitized_provenance_without_scanner_text() -> None:
    old = '{"mode": "old"}\n'
    new = '{"mode": "new"}\n'
    markdown = "src/settings.json\n| Path | Current |\n| --- | --- |\n| /mode | \"old\" |\n"
    result = propose_deterministic(
        *case("src/settings.json", old, new, markdown), scanner=SecretScanner()
    )

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    provenance = result.proposal.provenance
    assert provenance.source_path == "src/settings.json"
    assert provenance.source_line == 1
    assert provenance.diff_hunk.startswith("@@ -")
    assert provenance.target_path == "docs/reference.md"
    assert provenance.target_line == 4
    assert provenance.replacement_kind == "config_scalar"
    assert provenance.old_value == '"old"'
    assert provenance.new_value == '"new"'
    assert "old\"" not in result.sanitized_detail


def test_incomplete_diff_hunk_and_invalid_source_never_produce_candidate() -> None:
    old = "def load(value: int):\n    return value\n"
    new = "def load(value: str):\n    return value\n"
    markdown = "src/api.py\n```python\ndef load(value: int):\n    pass\n```\n"
    snapshots, inventory, selection = case("src/api.py", old, new, markdown)
    no_hunk = replace(snapshots, diff_text="diff --git a/src/api.py b/src/api.py\n")
    invalid_source = replace(
        snapshots,
        snapshots=(
            replace(
                snapshots.snapshots[0],
                old=replace(snapshots.snapshots[0].old, text="def broken(:\n"),
            ),
        ),
    )

    results = (
        propose_deterministic(no_hunk, inventory, selection),
        propose_deterministic(invalid_source, inventory, selection),
    )

    assert all(result.status is ProposalStatus.UNCERTAIN for result in results)
    assert all(result.proposal is None for result in results)