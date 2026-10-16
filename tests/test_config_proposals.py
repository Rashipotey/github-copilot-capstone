from __future__ import annotations

import difflib

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


def context(old: str, new: str, markdown: str, *, path: str):
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
    target_path = "docs/settings.md"
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


def test_json_nested_escaped_pointer_replaces_only_canonical_value_cell() -> None:
    old = '{"service/name": {"timeout~s": 10, "enabled": true}}\n'
    new = '{"service/name": {"timeout~s": 15, "enabled": true}}\n'
    markdown = (
        "Settings for `src/settings.json`.\n\n"
        "| Path | Current | Notes |\n"
        "| --- | ---: | --- |\n"
        r"| /service~1name/timeout~0s | 10 | pipe \| retained |" + "\n"
    )

    result = propose_deterministic(
        *context(old, new, markdown, path="src/settings.json"), scanner=SecretScanner()
    )

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.provenance.old_value == "10"
    assert result.proposal.provenance.new_value == "15"
    assert result.proposal.provenance.source_line == 1
    assert result.proposal.provenance.diff_line is not None
    assert result.proposal.provenance.target_line == 5
    assert result.proposal.replacement == markdown.replace("| 10 |", "| 15 |")
    assert "pipe \\| retained" in result.proposal.replacement


def test_json_string_pipe_is_escaped_without_changing_other_cells() -> None:
    old = '{"mode": "old"}\n'
    new = '{"mode": "new|value"}\n'
    markdown = (
        "| Key | Value | Notes |\n"
        "| --- | --- | --- |\n"
        r'| /mode | "old" | keep \| this pipe |' + "\n"
    )

    result = propose_deterministic(
        *context(old, new, markdown, path="src/settings.json"), scanner=SecretScanner()
    )

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(
        '"old"', r'"new\|value"'
    )


@pytest.mark.parametrize(
    ("old_value", "new_value", "old_cell", "new_cell"),
    [
        ('"old"', '"new"', '"old"', '"new"'),
        ("1.25", "2.5", "1.25", "2.5"),
        ("true", "false", "true", "false"),
        ("null", '"ready"', "null", '"ready"'),
    ],
)
def test_json_supported_scalar_values_use_canonical_json_rendering(
    old_value, new_value, old_cell, new_cell
) -> None:
    old, new = f'{{"mode": {old_value}}}\n', f'{{"mode": {new_value}}}\n'
    markdown = f"src/settings.json\n| Key | Value |\n| --- | --- |\n| /mode | {old_cell} |\n"

    result = propose_deterministic(
        *context(old, new, markdown, path="src/settings.json"), scanner=SecretScanner()
    )

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(f"| {old_cell} |", f"| {new_cell} |")


@pytest.mark.parametrize(
    ("old", "new", "old_cell", "new_cell"),
    [
        ('[service]\nname = "old"\n', '[service]\nname = "new"\n', '"old"', '"new"'),
        ("[service]\nenabled = true\n", "[service]\nenabled = false\n", "true", "false"),
        ("[service]\ncount = 1\n", "[service]\ncount = 2\n", "1", "2"),
        ("[service]\nratio = 1.5\n", "[service]\nratio = 2.5\n", "1.5", "2.5"),
    ],
)
def test_toml_nested_supported_scalars_replace_exact_row(old, new, old_cell, new_cell) -> None:
    markdown = f"Settings: `src/settings.toml`\n| Key | Current |\n| --- | --- |\n| /service/{'name' if 'name' in old else 'enabled' if 'enabled' in old else 'count' if 'count' in old else 'ratio'} | {old_cell} |\n"
    result = propose_deterministic(
        *context(old, new, markdown, path="src/settings.toml"), scanner=SecretScanner()
    )

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(f"| {old_cell} |", f"| {new_cell} |")


@pytest.mark.parametrize(
    ("markdown", "expected_status"),
    [
        ("Settings for src/settings.json.\n", ProposalStatus.NOT_FOUND),
        (
            "| Key | Value |\n| --- | --- |\n| /mode | 1 |\n| /mode | 1 |\n",
            ProposalStatus.UNCERTAIN,
        ),
        (
            "| Key | Value |\n| --- | --- |\n| /mode | 9 |\n",
            ProposalStatus.UNCERTAIN,
        ),
        (
            "| Key | Value |\n| --- | --- |\n| /mode | 1 |\n| /mode | 9 |\n",
            ProposalStatus.UNCERTAIN,
        ),
    ],
)
def test_missing_duplicate_wrong_or_conflicting_rows_emit_no_proposal(markdown, expected_status) -> None:
    result = propose_deterministic(
        *context('{"mode": 1}\n', '{"mode": 2}\n', markdown, path="src/settings.json")
    )

    assert result.status is expected_status
    assert result.proposal is None


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        ('{"mode": 1}\n', '{"mode": 1, "new": 2}\n', "src/settings.json"),
        ('{"mode": 1, "gone": 2}\n', '{"mode": 1}\n', "src/settings.json"),
        ('{"mode": 1, "other": 2}\n', '{"mode": 2, "other": 3}\n', "src/settings.json"),
        ('{"mode": [1]}\n', '{"mode": [2]}\n', "src/settings.json"),
        ('{"mode": NaN}\n', '{"mode": 1}\n', "src/settings.json"),
        (
            "[service]\nvalue = 1979-05-27\n",
            "[service]\nvalue = 1980-05-27\n",
            "src/settings.toml",
        ),
        (
            "[service]\nvalue = [1, 2]\n",
            "[service]\nvalue = [2, 3]\n",
            "src/settings.toml",
        ),
    ],
)
def test_config_add_delete_multi_change_and_unsupported_values_emit_no_proposal(old, new, path) -> None:
    markdown = f"{path}\n| Path | Value |\n| --- | --- |\n| /mode | 1 |\n| /service/value | 1979-05-27 |\n"

    result = propose_deterministic(*context(old, new, markdown, path=path), scanner=SecretScanner())

    assert result.status is ProposalStatus.UNCERTAIN
    assert result.proposal is None