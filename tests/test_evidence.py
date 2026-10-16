import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from docsync import evidence as evidence_module
from docsync.cli import parse_args
from docsync.evidence import (
    RAW_INPUT_BUDGET_BYTES,
    RawInputBudget,
    admit_raw_snapshots,
    collect_screened_snapshots,
    inventory_current_markdown,
)
from docsync.git import (
    ChangedFile,
    GitDiffResult,
    RawContentSnapshot,
    RawSnapshotCollection,
    RawSnapshotSide,
    SnapshotSideState,
    collect_diff,
)
from docsync.models import DiffScope, RunStatus, StageStatus
from docsync.paths import RepositoryFileChangedError
from docsync.screening import ScreeningResult, ScreeningState, SecretScanner


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    return result.stdout


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", message)


def config_for(repo: Path, *args: str):
    return parse_args(["--repo", str(repo), *args])


def diff_for(path: str) -> GitDiffResult:
    return GitDiffResult(
        DiffScope.WORKING_TREE,
        None,
        None,
        None,
        None,
        (ChangedFile(path, "M", False),),
        b"selected diff",
        RunStatus.COMPLETED,
    )


class RecordingScanner(SecretScanner):
    def __init__(self):
        self.received: list[bytes] = []

    def screen_evidence_item(self, content, *, provenance=None):
        if isinstance(content, bytes):
            self.received.append(content)
        return super().screen_evidence_item(content, provenance=provenance)


class FailingScanner:
    def screen_evidence_item(self, content, *, provenance=None):
        return ScreeningResult(
            ScreeningState.INCONCLUSIVE,
            None,
            (),
            (),
            None,
            "Screening could not be completed; the affected content was withheld.",
            excluded=True,
        )

    def screen_repository_path(self, path):
        raise AssertionError("path screening must not follow failed content screening")


def raw_one(path: str, old: bytes, new: bytes) -> RawSnapshotCollection:
    snapshot = RawContentSnapshot(
        path,
        DiffScope.WORKING_TREE,
        RawSnapshotSide(SnapshotSideState.PRESENT, old, "a" * 40, "100644"),
        RawSnapshotSide(SnapshotSideState.PRESENT, new, None, "100644"),
        False,
        False,
        True,
        "Snapshot captured.",
    )
    return RawSnapshotCollection((snapshot,), True, "Snapshots captured.", len(old) + len(new))


def test_scanner_receives_snapshot_bytes_before_only_sanitized_evidence_is_returned(
    git_repo: Path,
    monkeypatch,
    caplog,
) -> None:
    secret = b"source token=SNAPSHOT_SECRET_3187"
    raw = raw_one("src/module.py", b"old source", secret)
    monkeypatch.setattr(evidence_module, "_collect_raw_file_snapshots", lambda *_args, **_kwargs: raw)
    scanner = RecordingScanner()

    result = collect_screened_snapshots(
        config_for(git_repo),
        diff_for("src/module.py"),
        scanner=scanner,
        budget=RawInputBudget(),
    )

    assert result.complete
    assert secret in scanner.received
    assert "SNAPSHOT_SECRET_3187" not in repr(result)
    assert "SNAPSHOT_SECRET_3187" not in caplog.text
    assert "[REDACTED]" in result.snapshots[0].new.text


def test_scanner_failure_withholds_all_snapshot_bytes(git_repo: Path, monkeypatch) -> None:
    secret = b"source token=SCANNER_FAILURE_SECRET_904"
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: raw_one("src/module.py", b"old", secret),
    )

    result = collect_screened_snapshots(
        config_for(git_repo),
        diff_for("src/module.py"),
        scanner=FailingScanner(),
    )

    assert not result.complete
    assert result.snapshots == ()
    assert "SCANNER_FAILURE_SECRET_904" not in repr(result)


def test_selected_diff_and_snapshot_share_the_cumulative_budget(
    git_repo: Path,
    monkeypatch,
) -> None:
    raw = raw_one("src/module.py", b"o", b"n")
    monkeypatch.setattr(evidence_module, "_collect_raw_file_snapshots", lambda *_args, **_kwargs: raw)
    scanner = RecordingScanner()

    result = collect_screened_snapshots(
        config_for(git_repo),
        diff_for("src/module.py"),
        scanner=scanner,
        budget=RawInputBudget(maximum_bytes=14),
    )

    assert not result.complete
    assert result.snapshots == ()
    assert result.raw_bytes == 14


def test_incomplete_raw_snapshot_collection_cannot_admit_partial_records() -> None:
    raw = raw_one("src/one.py", b"old", b"new")
    incomplete = replace(raw, complete=False, diagnostic="Snapshot read failed or was unsafe.")

    result = admit_raw_snapshots(incomplete, scanner=SecretScanner(), budget=RawInputBudget())

    assert result.status is StageStatus.INCONCLUSIVE
    assert not result.complete
    assert result.snapshots == ()


@pytest.mark.parametrize(
    ("metadata_binary", "content"),
    [(False, b"binary\x00\xffpayload"), (True, b"\xff\xfe binary payload")],
)
def test_binary_snapshot_is_skipped_before_utf8_screening(
    git_repo: Path,
    monkeypatch,
    metadata_binary: bool,
    content: bytes,
) -> None:
    raw = raw_one("src/module.py", b"old source", content)
    if metadata_binary:
        raw = replace(
            raw,
            snapshots=(replace(raw.snapshots[0], git_binary=True, binary=True),),
        )
    monkeypatch.setattr(evidence_module, "_collect_raw_file_snapshots", lambda *_args, **_kwargs: raw)
    scanner = RecordingScanner()

    result = collect_screened_snapshots(
        config_for(git_repo),
        diff_for("src/module.py"),
        scanner=scanner,
    )

    assert result.complete
    assert result.status is StageStatus.COMPLETED
    assert scanner.received == [b"selected diff"]
    assert result.snapshots[0].binary
    assert result.snapshots[0].path == "src/module.py"
    assert result.snapshots[0].old.state is SnapshotSideState.UNAVAILABLE
    assert result.snapshots[0].old.text is None
    assert result.snapshots[0].new.state is SnapshotSideState.UNAVAILABLE
    assert result.snapshots[0].new.text is None
    assert len(result.skipped_inputs) == 1
    assert result.skipped_inputs[0].path == "src/module.py"
    assert result.skipped_inputs[0].reason == "binary"
    assert "skipped" in result.skipped_inputs[0].sanitized_detail


def test_current_markdown_inventory_includes_root_docs_and_untracked_files(
    git_repo: Path,
) -> None:
    (git_repo / "README.md").write_text("# Root\n", encoding="utf-8")
    (git_repo / "docs" / "nested").mkdir(parents=True)
    (git_repo / "docs" / "nested" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (git_repo / "untracked.md").write_text("# Untracked target\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("not Markdown", encoding="utf-8")

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert inventory.complete
    assert [document.path for document in inventory.documents] == [
        "README.md",
        "docs/nested/guide.md",
        "untracked.md",
    ]


def test_current_markdown_inventory_applies_case_sensitive_glob_exclusions(
    git_repo: Path,
) -> None:
    (git_repo / "README.md").write_text("# Included\n", encoding="utf-8")
    (git_repo / "docs" / "nested").mkdir(parents=True)
    (git_repo / "docs" / "nested" / "guide.md").write_text("# Included\n", encoding="utf-8")
    (git_repo / "private").mkdir()
    (git_repo / "private" / "secret.md").write_text("# Excluded\n", encoding="utf-8")

    inventory = inventory_current_markdown(
        git_repo,
        excludes=("private/**", "docs/nested/guide.md"),
        scanner=SecretScanner(),
    )

    assert inventory.complete
    assert [document.path for document in inventory.documents] == ["README.md"]
    assert any(item.path == "private" and item.reason == "excluded" for item in inventory.skipped_inputs)


def test_markdown_inventory_exact_file_limit_is_allowed(git_repo: Path) -> None:
    (git_repo / "README.md").write_bytes(b"x" * (256 * 1024))

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert inventory.complete
    assert len(inventory.documents[0].text) == 256 * 1024


def test_oversized_markdown_makes_inventory_incomplete_and_empty(git_repo: Path) -> None:
    (git_repo / "README.md").write_bytes(b"x" * (256 * 1024 + 1))

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert not inventory.complete
    assert inventory.status is StageStatus.INCONCLUSIVE
    assert inventory.documents == ()
    assert "file limit" in inventory.sanitized_detail
    assert inventory.skipped_inputs[0].reason == "over_size"


def test_redacted_markdown_path_makes_inventory_incomplete(git_repo: Path) -> None:
    (git_repo / "token=PATH_SECRET_502.md").write_text("# Title\n", encoding="utf-8")

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert not inventory.complete
    assert inventory.documents == ()
    assert "PATH_SECRET_502" not in repr(inventory)


def test_binary_markdown_is_reported_without_invalidating_inventory(git_repo: Path) -> None:
    (git_repo / "README.md").write_bytes(b"# text\x00binary")

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert inventory.complete
    assert inventory.documents == ()
    assert inventory.skipped_inputs[0].reason == "binary"


def test_inventory_cumulative_budget_exact_and_over_limit(git_repo: Path) -> None:
    (git_repo / "README.md").write_bytes(b"# ok\n")
    exact = inventory_current_markdown(
        git_repo,
        scanner=SecretScanner(),
        budget=RawInputBudget(maximum_bytes=5),
    )
    over = inventory_current_markdown(
        git_repo,
        scanner=SecretScanner(),
        budget=RawInputBudget(initial_bytes=1, maximum_bytes=5),
    )

    assert exact.complete
    assert exact.raw_bytes == 5
    assert not over.complete
    assert over.documents == ()
    assert "byte limit" in over.sanitized_detail
    assert over.skipped_inputs[0].reason == "resource_limit"


def test_duplicate_evidence_bytes_are_charged_once() -> None:
    budget = RawInputBudget(maximum_bytes=4)

    assert budget.admit("same.md", b"four")
    assert budget.admit("same.md", b"four")
    assert budget.used_bytes == 4
    assert not budget.admit("other.md", b"x")


def test_default_raw_input_budget_is_32_mib_and_exact_limit_is_allowed() -> None:
    budget = RawInputBudget()

    assert RAW_INPUT_BUDGET_BYTES == 32 * 1024 * 1024
    assert budget.seed("selected-diff", RAW_INPUT_BUDGET_BYTES)
    assert budget.used_bytes == RAW_INPUT_BUDGET_BYTES
    assert not budget.admit("extra.md", b"x")


def test_bad_markdown_scan_blocks_entire_inventory(git_repo: Path) -> None:
    (git_repo / "README.md").write_bytes(b"token=INVENTORY_SCAN_SECRET_44\xff")
    (git_repo / "docs.md").write_text("# Also present", encoding="utf-8")

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert not inventory.complete
    assert inventory.documents == ()
    assert "INVENTORY_SCAN_SECRET_44" not in repr(inventory)


def test_inventory_is_explicitly_incomplete_for_unsafe_candidate(
    git_repo: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(evidence_module, "_markdown_paths", lambda *_: (("README.md",), False, ()))
    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert not inventory.complete
    assert inventory.documents == ()


def test_complete_unified_diff_screens_removed_and_added_secrets_before_return(
    git_repo: Path,
    monkeypatch,
    caplog,
) -> None:
    raw_diff = (
        b"diff --git a/src/module.py b/src/module.py\n"
        b"--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n"
        b"-password=OLD_DIFF_SECRET_771\n"
        b"+token=NEW_DIFF_SECRET_882\n"
        b" safe context\n"
    )
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: raw_one("src/module.py", b"old source", b"new source"),
    )
    scanner = RecordingScanner()
    result = collect_screened_snapshots(
        config_for(git_repo),
        replace(diff_for("src/module.py"), diff=raw_diff),
        scanner=scanner,
    )

    assert result.complete
    assert result.diff_text is not None
    assert "OLD_DIFF_SECRET_771" not in result.diff_text
    assert "NEW_DIFF_SECRET_882" not in result.diff_text
    assert "OLD_DIFF_SECRET_771" not in repr(result)
    assert "NEW_DIFF_SECRET_882" not in repr(result)
    assert "OLD_DIFF_SECRET_771" not in caplog.text
    assert "NEW_DIFF_SECRET_882" not in caplog.text
    assert scanner.received[0] == raw_diff


def test_diff_scanner_failure_withholds_every_snapshot(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: pytest.fail("snapshots must not be read after diff scan failure"),
    )

    result = collect_screened_snapshots(
        config_for(git_repo),
        diff_for("src/module.py"),
        scanner=FailingScanner(),
    )

    assert not result.complete
    assert result.snapshots == ()
    assert result.diff_text is None
    assert "SCANNER_FAILURE_SECRET" not in repr(result)


def test_raising_diff_scanner_fails_closed(git_repo: Path, monkeypatch) -> None:
    class RaisingScanner(SecretScanner):
        def screen_evidence_item(self, _content, *, provenance=None):
            raise RuntimeError("DIFF_SCANNER_EXCEPTION_SECRET_990")

    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: pytest.fail("scanner exception must stop snapshot reads"),
    )
    result = collect_screened_snapshots(
        config_for(git_repo),
        diff_for("src/module.py"),
        scanner=RaisingScanner(),
    )

    assert not result.complete
    assert result.snapshots == ()
    assert result.diff_text is None
    assert "DIFF_SCANNER_EXCEPTION_SECRET_990" not in repr(result)


def test_excluded_unified_diff_content_withholds_every_snapshot(git_repo: Path, monkeypatch) -> None:
    raw_diff = (
        b"diff --git a/src/module.py b/src/module.py\n"
        b"-----BEGIN PRIVATE KEY-----\n"
        b"DIFF_PRIVATE_KEY_MATERIAL_113\n"
        b"-----END PRIVATE KEY-----\n"
    )
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: pytest.fail("excluded diff content must stop evidence reads"),
    )

    result = collect_screened_snapshots(
        config_for(git_repo),
        replace(diff_for("src/module.py"), diff=raw_diff),
        scanner=SecretScanner(),
    )

    assert not result.complete
    assert result.snapshots == ()
    assert result.diff_text is None
    assert "DIFF_PRIVATE_KEY_MATERIAL_113" not in repr(result)


def test_binary_snapshot_bytes_still_count_toward_raw_budget() -> None:
    binary = raw_one("src/blob.py", b"old", b"b\x00x")
    result = admit_raw_snapshots(
        binary,
        scanner=SecretScanner(),
        budget=RawInputBudget(maximum_bytes=4),
    )

    assert not result.complete
    assert result.snapshots == ()
    assert result.raw_bytes == 3


@pytest.mark.parametrize(
    ("path", "excludes", "reason"),
    [
        ("src/module.js", (), "unsupported"),
        ("src/module.py", ("src/**",), "excluded"),
    ],
)
def test_unsupported_and_excluded_changes_are_reported_without_snapshot_reads(
    git_repo: Path,
    monkeypatch,
    path: str,
    excludes: tuple[str, ...],
    reason: str,
) -> None:
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: pytest.fail("skipped paths must not be read"),
    )
    result = collect_screened_snapshots(
        config_for(git_repo, *[item for pattern in excludes for item in ("--exclude", pattern)]),
        replace(diff_for(path), changed_files=(ChangedFile(path, "M", False),)),
        scanner=SecretScanner(),
    )

    assert result.complete
    assert result.snapshots == ()
    assert result.skipped_inputs[0].path == path
    assert result.skipped_inputs[0].reason == reason


def test_oversized_changed_snapshot_is_skipped_and_withheld(git_repo: Path, monkeypatch) -> None:
    raw = raw_one("src/module.py", b"old", b"new")
    unavailable = replace(
        raw.snapshots[0],
        complete=False,
        diagnostic="Evidence file exceeds its byte limit.",
    )
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: replace(
            raw, snapshots=(unavailable,), complete=False,
            diagnostic="One or more snapshots unavailable.",
        ),
    )

    result = collect_screened_snapshots(
        config_for(git_repo), diff_for("src/module.py"), scanner=SecretScanner()
    )

    assert not result.complete
    assert result.snapshots == ()
    assert result.skipped_inputs[0].reason == "over_size"


def test_selected_endpoint_snapshot_is_not_replaced_by_current_worktree_source(
    git_repo: Path,
    monkeypatch,
) -> None:
    current = git_repo / "src" / "module.py"
    current.parent.mkdir()
    current.write_text("current worktree source", encoding="utf-8")
    endpoint_snapshot = raw_one(
        "src/module.py", b"selected old endpoint", b"selected new endpoint"
    )
    monkeypatch.setattr(
        evidence_module,
        "_collect_raw_file_snapshots",
        lambda *_args, **_kwargs: endpoint_snapshot,
    )

    result = collect_screened_snapshots(
        config_for(git_repo), diff_for("src/module.py"), scanner=SecretScanner()
    )

    assert result.complete
    assert result.snapshots[0].new.text == "selected new endpoint"
    assert "current worktree source" not in repr(result)


def test_markdown_read_growth_is_reported_and_withholds_inventory(
    git_repo: Path,
    monkeypatch,
) -> None:
    (git_repo / "README.md").write_text("# stable\n", encoding="utf-8")

    def grow_during_read(*_args, **_kwargs):
        raise RepositoryFileChangedError("unsafe fixture detail")

    monkeypatch.setattr(evidence_module, "read_repository_file", grow_during_read)
    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert not inventory.complete
    assert inventory.documents == ()
    assert inventory.skipped_inputs[0].reason == "unreadable_or_unsafe"
    assert "unsafe fixture detail" not in repr(inventory)


def test_generated_named_markdown_is_not_implicitly_excluded(git_repo: Path) -> None:
    (git_repo / "generated-output.md").write_text("# Existing doc\n", encoding="utf-8")

    inventory = inventory_current_markdown(git_repo, scanner=SecretScanner())

    assert inventory.complete
    assert [document.path for document in inventory.documents] == ["generated-output.md"]