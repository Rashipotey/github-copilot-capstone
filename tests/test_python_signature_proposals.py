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


def context(old: str, new: str, markdown: str, *, path: str = "src/api.py"):
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
    diff = "diff --git a/{0} b/{0}\n".format(path) + "".join(
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
    target_path = "docs/api.md"
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


def test_nested_async_multiline_signature_replaces_only_fenced_header() -> None:
    old = (
        "class Client:\n"
        "    async def fetch(self, item: str, /,\n"
        "                    retries: int = 2) -> str:\n"
        "        return item\n"
    )
    new = (
        "class Client:\n"
        "    async def fetch(self, item: str, /,\n"
        "                    retries: int = 3) -> bytes:\n"
        "        return item\n"
    )
    markdown = (
        "API reference: `src/api.py`.\n\n"
        "```python\n"
        "class Client:\n"
        "    async def fetch(self, item: str, /,\n"
        "                    retries: int = 2) -> str:\n"
        "        pass\n"
        "```\n"
        "Unchanged prose.\n"
    )

    result = propose_deterministic(*context(old, new, markdown), scanner=SecretScanner())

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(
        "retries: int = 2) -> str:", "retries: int = 3) -> bytes:"
    )
    assert result.proposal.provenance.source_path == "src/api.py"
    assert result.proposal.provenance.source_line == 2
    assert result.proposal.provenance.diff_hunk is not None
    assert result.proposal.provenance.diff_line is not None
    assert result.proposal.provenance.target_path == "docs/api.md"
    assert result.proposal.provenance.target_line == 5
    assert result.proposal.provenance.old_value.startswith("async def fetch")


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
def test_separate_parameter_line_insertion_and_deletion_are_supported(
    old_header: str, new_header: str, changed_marker: str
) -> None:
    old = old_header + "\n    return value\n"
    new = new_header + "\n    return value\n"
    markdown = f"src/api.py\n```python\n{old_header}\n```\n"

    result = propose_deterministic(*context(old, new, markdown), scanner=SecretScanner())

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(old_header, new_header)
    diff = context(old, new, markdown)[0].diff_text
    assert diff is not None and changed_marker in diff


def test_unrelated_source_line_change_blocks_signature_proposal() -> None:
    old_header = "def run(\n    value: str,\n) -> str:"
    new_header = "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:"
    old = "# old note\n" + old_header + "\n    return value\n"
    new = "# revised note\n" + new_header + "\n    return value\n"
    markdown = f"src/api.py\n```python\n{old_header}\n```\n"

    result = propose_deterministic(*context(old, new, markdown))

    assert result.status is ProposalStatus.UNCERTAIN
    assert result.proposal is None


def test_mismatched_diff_hunk_line_blocks_signature_proposal() -> None:
    old_header = "def run(\n    value: str,\n) -> str:"
    new_header = "def run(\n    value: str,\n    enabled: bool = False,\n) -> str:"
    markdown = f"src/api.py\n```python\n{old_header}\n```\n"
    snapshots, inventory, selection = context(
        old_header + "\n    return value\n",
        new_header + "\n    return value\n",
        markdown,
    )
    snapshots = replace(
        snapshots,
        diff_text=(snapshots.diff_text or "").replace(
            "+    enabled: bool = False,", "+    unrelated: bool = False,"
        ),
    )

    result = propose_deterministic(snapshots, inventory, selection)

    assert result.status is ProposalStatus.UNCERTAIN
    assert result.proposal is None


def test_fence_line_ending_and_indent_normalization_preserve_document_text() -> None:
    old = "def fetch(value: int = 1) -> int:\r\n    return value\r\n"
    new = "def fetch(value: int = 2) -> int:\r\n    return value\r\n"
    markdown = "src/api.py\r\n\r\n  ```python\r\n  def fetch(value: int = 1) -> int:\r\n      pass\r\n  ```\r\n"

    result = propose_deterministic(*context(old, new, markdown), scanner=SecretScanner())

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(
        "def fetch(value: int = 1) -> int:", "def fetch(value: int = 2) -> int:"
    )
    assert result.proposal.replacement.endswith("  ```\r\n")


def test_blockquoted_fence_preserves_container_prefix_during_multiline_replacement() -> None:
    old = "def fetch(\n    value: int,\n    *, flag: bool = True\n) -> int:\n    return value\n"
    new = "def fetch(\n    value: str,\n    *, flag: bool = False\n) -> bytes:\n    return value\n"
    markdown = (
        "src/api.py\n\n"
        "> ```python\n"
        "> def fetch(\n"
        ">     value: int,\n"
        ">     *, flag: bool = True\n"
        "> ) -> int:\n"
        ">     pass\n"
        "> ```\n"
    )

    result = propose_deterministic(*context(old, new, markdown), scanner=SecretScanner())

    assert result.status is ProposalStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.replacement == markdown.replace(
        "value: int,", "value: str,"
    ).replace("flag: bool = True", "flag: bool = False").replace(") -> int:", ") -> bytes:")
    assert ">     value: str," in result.proposal.replacement


@pytest.mark.parametrize(
    ("old", "new", "anchor", "reason"),
    [
        (
            "def fetch(value: int) -> int:\n    return value\n",
            "def fetch(value: str) -> int:\n    return value\n",
            "```python\ndef other(value: int) -> int:\n    pass\n```\n",
            "missing",
        ),
        (
            "def fetch(value: int) -> int:\n    return value\n",
            "def fetch(value: str) -> int:\n    return value\n",
            "```python\ndef fetch(value: int) -> int:\n    pass\n```\n"
            "```python\ndef fetch(value: int) -> int:\n    pass\n```\n",
            "duplicate",
        ),
    ],
)
def test_missing_or_duplicate_fenced_signature_emits_no_proposal(old, new, anchor, reason) -> None:
    result = propose_deterministic(
        *context(old, new, f"src/api.py\n{anchor}"), scanner=SecretScanner()
    )

    expected = ProposalStatus.NOT_FOUND if reason == "missing" else ProposalStatus.UNCERTAIN
    assert result.status is expected
    assert result.proposal is None


def test_body_changes_and_multiple_changed_callables_emit_no_proposal() -> None:
    old = "def fetch(value: int) -> int:\n    return value\n"
    body_changed = "def fetch(value: str) -> int:\n    return str(value)\n"
    markdown = "src/api.py\n```python\ndef fetch(value: int) -> int:\n    pass\n```\n"
    body_result = propose_deterministic(*context(old, body_changed, markdown))

    multiple_old = "def first(value: int):\n    return value\n\ndef second(flag: bool):\n    return flag\n"
    multiple_new = "def first(value: str):\n    return value\n\ndef second(flag: int):\n    return flag\n"
    multiple_doc = (
        "src/api.py\n```python\ndef first(value: int):\n    pass\n"
        "def second(flag: bool):\n    pass\n```\n"
    )
    multiple_result = propose_deterministic(*context(multiple_old, multiple_new, multiple_doc))

    assert body_result.status is ProposalStatus.UNCERTAIN
    assert body_result.proposal is None
    assert multiple_result.status is ProposalStatus.UNCERTAIN
    assert multiple_result.proposal is None


def test_callable_rename_or_addition_is_not_mistaken_for_same_qualified_callable() -> None:
    old = "class Client:\n    def fetch(self, value: int):\n        return value\n"
    new = "class Client:\n    def retrieve(self, value: str):\n        return value\n"
    markdown = "src/api.py\n```python\n    def fetch(self, value: int):\n        pass\n```\n"

    result = propose_deterministic(*context(old, new, markdown))

    assert result.status is ProposalStatus.UNCERTAIN
    assert result.proposal is None


def test_incomplete_ambiguous_conflicting_and_secret_redacted_evidence_withhold() -> None:
    old = "async def fetch(value: int) -> int:\n    return value\n"
    new = "async def fetch(value: str) -> int:\n    return value\n"
    markdown = "src/api.py\n```python\nasync def fetch(value: int) -> int:\n    pass\n```\n"
    args = context(old, new, markdown)

    incomplete = replace(args[0], complete=False)
    ambiguous = replace(args[2], ambiguities=(object(),))
    conflicting = replace(args[2], conflicts=(object(),))
    redacted_old = replace(
        args[0],
        snapshots=(replace(args[0].snapshots[0], old=replace(args[0].snapshots[0].old, text="[REDACTED]")),),
    )

    results = (
        propose_deterministic(incomplete, args[1], args[2]),
        propose_deterministic(args[0], args[1], ambiguous),
        propose_deterministic(args[0], args[1], conflicting),
        propose_deterministic(redacted_old, args[1], args[2]),
    )

    assert all(result.status is ProposalStatus.UNCERTAIN for result in results)
    assert all(result.proposal is None for result in results)