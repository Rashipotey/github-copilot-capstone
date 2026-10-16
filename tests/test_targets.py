from __future__ import annotations

from docsync.evidence import (
    MarkdownDocument,
    MarkdownInventory,
    ScreenedSide,
    ScreenedSnapshot,
    ScreenedSnapshots,
)
from docsync.models import DiffScope, SkippedInput, StageStatus
from docsync.git import SnapshotSideState
from docsync.screening import SecretScanner
from docsync.targets import select_targets


def snapshots_for(path: str = "src/api.py", old: str = "def fetch_item():\n", new: str = "def fetch_item():\n") -> ScreenedSnapshots:
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
    return ScreenedSnapshots(
        (snapshot,), StageStatus.COMPLETED, True, "Snapshots screened.", 32,
        diff_text="selected diff",
    )


def inventory_for(*documents: tuple[str, str]) -> MarkdownInventory:
    return MarkdownInventory(
        tuple(MarkdownDocument(path, text) for path, text in documents),
        StageStatus.COMPLETED,
        True,
        "Markdown inventory screened.",
        64,
    )


def test_selects_multiple_clear_targets_with_distinct_unique_anchors() -> None:
    snapshots = snapshots_for(
        old="def fetch_item():\n",
        new="def fetch_item():\n",
    )
    inventory = inventory_for(
        ("README.md", "API implementation: src/api.py\n"),
        ("docs/usage.md", "Call `fetch_item` to retrieve an item.\n"),
    )

    result = select_targets(snapshots, inventory, scanner=SecretScanner())

    assert result.complete
    assert [target.path for target in result.targets] == ["README.md", "docs/usage.md"]
    assert result.ambiguities == ()
    assert result.conflicts == ()
    assert result.proposal_eligible
    assert result.source_provenance == (
        ("src/api.py", "working-tree", "index", "working-tree"),
    )


def test_duplicate_symbol_across_docs_is_ambiguous_not_arbitrarily_selected() -> None:
    result = select_targets(
        snapshots_for(),
        inventory_for(
            ("README.md", "Use `fetch_item` for retrieval.\n"),
            ("docs/api.md", "The `fetch_item` callable retrieves items.\n"),
        ),
        scanner=SecretScanner(),
    )

    assert result.targets == ()
    assert len(result.ambiguities) == 1
    assert result.ambiguities[0].candidate_paths == ("README.md", "docs/api.md")
    assert not result.proposal_eligible


def test_conflicting_existing_path_anchors_are_reported() -> None:
    result = select_targets(
        snapshots_for(),
        inventory_for(
            ("README.md", "src/api.py supports legacy retrieval.\n"),
            ("docs/api.md", "src/api.py no longer supports legacy retrieval.\n"),
        ),
        scanner=SecretScanner(),
    )

    assert result.targets == ()
    assert result.ambiguities == ()
    assert len(result.conflicts) == 1
    assert result.conflicts[0].candidate_paths == ("README.md", "docs/api.md")
    assert not result.proposal_eligible


def test_filename_stem_only_is_an_ambiguous_candidate() -> None:
    result = select_targets(
        snapshots_for(),
        inventory_for(("README.md", "The api component is described below.\n")),
        scanner=SecretScanner(),
    )

    assert result.targets == ()
    assert result.candidates[0].signal_types == ("filename_stem",)
    assert result.ambiguities
    assert not result.no_target


def test_no_target_is_explicit_and_does_not_create_documents() -> None:
    result = select_targets(
        snapshots_for(),
        inventory_for(("README.md", "General project overview.\n")),
        scanner=SecretScanner(),
    )

    assert result.targets == ()
    assert result.candidates == ()
    assert result.no_target
    assert result.sanitized_detail == "No suitable existing documentation target was found."


def test_incomplete_source_or_markdown_evidence_withholds_all_targets() -> None:
    incomplete = MarkdownInventory(
        (), StageStatus.INCONCLUSIVE, False, "Inventory incomplete.", 0,
        (SkippedInput("README.md", "over_size", "Candidate exceeds limit."),),
    )

    result = select_targets(snapshots_for(), incomplete, scanner=SecretScanner())

    assert not result.complete
    assert result.targets == ()
    assert result.candidates == ()
    assert result.skipped_inputs[0].reason == "over_size"
    assert not result.proposal_eligible


def test_duplicate_anchor_inside_one_markdown_file_is_ambiguous() -> None:
    result = select_targets(
        snapshots_for(),
        inventory_for(("README.md", "Use `fetch_item` here.\nUse `fetch_item` again.\n")),
        scanner=SecretScanner(),
    )

    assert result.targets == ()
    assert result.ambiguities
    assert result.ambiguities[0].candidate_paths == ("README.md",)
