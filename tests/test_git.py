import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from docsync import git as git_module
from docsync.cli import parse_args
from docsync.config import DEFAULT_MAX_DIFF_BYTES
from docsync.git import (
    GitError,
    GitOutputLimitError,
    GitTimeoutError,
    GitUnavailableError,
    InvalidRevisionError,
    _ProcessOutput,
    _parse_name_status,
    _parse_index_entries,
    _parse_tree_entries,
    _collect_raw_file_snapshots,
    _run_bounded,
    _run_git,
    collect_diff,
    inspect_repository,
    SnapshotSideState,
)
from docsync.models import DiffScope, RunStatus
from docsync.paths import PathValidationError


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


def config_for(repo: Path, *scope_args: str, **limits: int):
    return parse_args(["--repo", str(repo), *scope_args]) if not limits else replace(
        parse_args(["--repo", str(repo), *scope_args]), **limits
    )


def test_inspect_repository_returns_worktree_root(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "nested").mkdir()

    assert inspect_repository(git_repo / "nested") == git_repo.resolve()


def test_worktree_and_staged_scopes_exclude_each_other_and_untracked(git_repo: Path) -> None:
    for name in ("working.txt", "staged.txt"):
        (git_repo / name).write_text("initial\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    (git_repo / "working.txt").write_text("working change\n", encoding="utf-8")
    (git_repo / "staged.txt").write_text("staged change\n", encoding="utf-8")
    git(git_repo, "add", "staged.txt")
    (git_repo / "untracked.txt").write_text("not in a diff\n", encoding="utf-8")
    index_before = (git_repo / ".git" / "index").read_bytes()
    status_before = git(git_repo, "status", "--porcelain", "-z")

    working = collect_diff(config_for(git_repo))
    staged = collect_diff(config_for(git_repo, "--staged"))

    assert {item.path for item in working.changed_files} == {"working.txt"}
    assert {item.path for item in staged.changed_files} == {"staged.txt"}
    assert "untracked.txt" not in working.diff.decode("utf-8")
    assert "untracked.txt" not in staged.diff.decode("utf-8")
    assert (git_repo / ".git" / "index").read_bytes() == index_before
    assert git(git_repo, "status", "--porcelain", "-z") == status_before


def test_explicit_revisions_compare_endpoint_trees_not_merge_base(git_repo: Path) -> None:
    (git_repo / "base.txt").write_text("base\n", encoding="utf-8")
    commit_all(git_repo, "base")
    git(git_repo, "branch", "endpoint-base")
    (git_repo / "right-only.txt").write_text("right\n", encoding="utf-8")
    commit_all(git_repo, "right endpoint")
    git(git_repo, "branch", "endpoint-right")
    git(git_repo, "checkout", "--quiet", "endpoint-base")
    (git_repo / "left-only.txt").write_text("left\n", encoding="utf-8")
    commit_all(git_repo, "left endpoint")
    git(git_repo, "branch", "endpoint-left")

    result = collect_diff(
        config_for(git_repo, "--base", "endpoint-right", "--head", "endpoint-left")
    )

    assert result.scope is DiffScope.BASE_HEAD
    assert result.base_tree and result.head_tree
    assert {item.path for item in result.changed_files} == {"left-only.txt", "right-only.txt"}


def test_working_tree_snapshot_is_exact_index_to_worktree_bytes(git_repo: Path) -> None:
    path = git_repo / "module.py"
    path.write_bytes(b"index version\n")
    commit_all(git_repo, "initial")
    path.write_bytes(b"index snapshot\n")
    git(git_repo, "add", "module.py")
    path.write_bytes(b"worktree snapshot\n")
    config = config_for(git_repo)
    diff = collect_diff(config)

    result = _collect_raw_file_snapshots(config, diff)

    assert result.complete
    snapshot = result.snapshots[0]
    assert snapshot.scope is DiffScope.WORKING_TREE
    assert snapshot.old_endpoint == "index"
    assert snapshot.new_endpoint == "working-tree"
    assert snapshot.old.content == b"index snapshot\n"
    assert snapshot.new.content == b"worktree snapshot\n"


def test_staged_snapshot_is_exact_head_to_index_bytes(git_repo: Path) -> None:
    path = git_repo / "module.py"
    path.write_bytes(b"HEAD version\n")
    commit_all(git_repo, "initial")
    path.write_bytes(b"index version\n")
    git(git_repo, "add", "module.py")
    path.write_bytes(b"later worktree version\n")
    config = config_for(git_repo, "--staged")
    diff = collect_diff(config)

    result = _collect_raw_file_snapshots(config, diff)

    assert result.complete
    snapshot = result.snapshots[0]
    assert snapshot.scope is DiffScope.STAGED
    assert snapshot.old_endpoint.startswith("tree:")
    assert snapshot.new_endpoint == "index"
    assert snapshot.old.content == b"HEAD version\n"
    assert snapshot.new.content == b"index version\n"


def test_base_head_snapshot_uses_named_endpoint_tree_blobs(git_repo: Path) -> None:
    path = git_repo / "module.py"
    path.write_bytes(b"base endpoint\n")
    commit_all(git_repo, "base")
    base = git(git_repo, "rev-parse", "HEAD").decode().strip()
    path.write_bytes(b"head endpoint\n")
    commit_all(git_repo, "head")
    head = git(git_repo, "rev-parse", "HEAD").decode().strip()
    config = config_for(git_repo, "--base", base, "--head", head)
    diff = collect_diff(config)

    result = _collect_raw_file_snapshots(config, diff)

    assert result.complete
    snapshot = result.snapshots[0]
    assert snapshot.scope is DiffScope.BASE_HEAD
    assert snapshot.old_endpoint == f"tree:{diff.base_tree}"
    assert snapshot.new_endpoint == f"tree:{diff.head_tree}"
    assert snapshot.old.content == b"base endpoint\n"
    assert snapshot.new.content == b"head endpoint\n"
    assert snapshot.old.object_id and snapshot.new.object_id


def test_staged_addition_and_deletion_have_explicit_missing_sides(git_repo: Path) -> None:
    removed = git_repo / "removed.py"
    removed.write_bytes(b"remove me\n")
    commit_all(git_repo, "initial")
    git(git_repo, "rm", "--quiet", "removed.py")
    added = git_repo / "added.py"
    added.write_bytes(b"add me\n")
    git(git_repo, "add", "added.py")
    config = config_for(git_repo, "--staged")

    result = _collect_raw_file_snapshots(config, collect_diff(config))
    snapshots = {item.path: item for item in result.snapshots}

    assert result.complete
    assert snapshots["added.py"].old.state is SnapshotSideState.MISSING
    assert snapshots["added.py"].new.content == b"add me\n"
    assert snapshots["removed.py"].old.content == b"remove me\n"
    assert snapshots["removed.py"].new.state is SnapshotSideState.MISSING


def test_invalid_snapshot_revisions_fail_before_blob_reads(git_repo: Path, monkeypatch) -> None:
    config = config_for(git_repo, "--base", "missing-invalid-revision", "--head", "HEAD")
    changed = git_module.ChangedFile("file.py", "M", False)
    diff = git_module.GitDiffResult(
        DiffScope.BASE_HEAD,
        config.base,
        config.head,
        None,
        None,
        (changed,),
        b"diff",
        RunStatus.COMPLETED,
    )
    content_reads: list[list[str]] = []

    def record_content_read(_root: Path, args: list[str], **_kwargs) -> bytes:
        content_reads.append(args)
        raise AssertionError("content plumbing ran before revision validation")

    monkeypatch.setattr(git_module, "_run_git_bytes", record_content_read)

    result = _collect_raw_file_snapshots(config, diff)

    assert not result.complete
    assert content_reads == []
    assert result.snapshots[0].old.content is None


def test_snapshot_exact_file_size_allowed_and_oversize_has_no_bytes(git_repo: Path) -> None:
    exact = git_repo / "exact.py"
    exact.write_bytes(b"x" * (256 * 1024))
    commit_all(git_repo, "exact")
    exact.write_bytes(b"z" * (256 * 1024))
    config = config_for(git_repo)
    exact_result = _collect_raw_file_snapshots(config, collect_diff(config))
    assert exact_result.complete
    exact_snapshot = exact_result.snapshots[0]
    assert len(exact_snapshot.old.content or b"") == 256 * 1024
    assert len(exact_snapshot.new.content or b"") == 256 * 1024

    oversized = git_repo / "oversized.py"
    oversized.write_bytes(b"y" * (256 * 1024 + 1))
    git(git_repo, "add", "oversized.py")
    staged = config_for(git_repo, "--staged")
    over_result = _collect_raw_file_snapshots(staged, collect_diff(staged))

    assert not over_result.complete
    oversized_snapshot = next(item for item in over_result.snapshots if item.path == "oversized.py")
    assert oversized_snapshot.old.content is None
    assert oversized_snapshot.new.content is None


def test_snapshot_blob_output_overflow_returns_no_partial_bytes(git_repo: Path, monkeypatch) -> None:
    path = git_repo / "module.py"
    path.write_bytes(b"before\n")
    commit_all(git_repo, "initial")
    path.write_bytes(b"after\n")
    config = config_for(git_repo)
    diff = collect_diff(config)
    original = git_module._run_bounded

    def overflow_blob(argv: list[str], **kwargs):
        if argv[-2:-1] == ["blob"]:
            raise GitOutputLimitError("Git output exceeded a safety limit.")
        return original(argv, **kwargs)

    monkeypatch.setattr(git_module, "_run_bounded", overflow_blob)

    result = _collect_raw_file_snapshots(config, diff)

    assert not result.complete
    assert result.snapshots[0].old.content is None
    assert result.snapshots[0].new.content is None


def test_git_symlink_blob_is_rejected(git_repo: Path) -> None:
    path = git_repo / "link.txt"
    path.write_bytes(b"regular file\n")
    commit_all(git_repo, "initial")
    link_oid = subprocess.run(
        ["git", "-C", str(git_repo), "hash-object", "-w", "--stdin"],
        input=b"target.txt",
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    ).stdout.decode().strip()
    git(git_repo, "update-index", "--add", "--cacheinfo", "120000", link_oid, "link.txt")
    config = config_for(git_repo, "--staged")

    result = _collect_raw_file_snapshots(config, collect_diff(config))

    assert not result.complete
    snapshot = result.snapshots[0]
    assert snapshot.old.content is None
    assert snapshot.new.content is None


@pytest.mark.parametrize(
    "metadata",
    [
        b"100644 " + b"a" * 40 + b" 0\t../escape\x00",
        b"100644 " + b"a" * 40 + b" 0\tbad\xff\x00",
        b"100644 " + b"a" * 40 + b" 0\t./not-normalized\x00",
    ],
)
def test_index_blob_metadata_rejects_unsafe_paths(metadata: bytes) -> None:
    with pytest.raises(GitError):
        _parse_index_entries(metadata, {"wanted"})


def test_tree_blob_metadata_rejects_malformed_and_unsafe_paths() -> None:
    oid = b"a" * 40
    with pytest.raises(GitError):
        _parse_tree_entries(b"100644 blob " + oid + b"\t../escape\x00", {"../escape"})
    with pytest.raises(GitError):
        _parse_tree_entries(b"100644 blob " + oid + b"\twanted", {"wanted"})


def test_invalid_revision_is_rejected_before_diff_metadata(git_repo: Path, monkeypatch) -> None:
    original = git_module._run_git
    commands: list[list[str]] = []

    def recording_run(root: Path, args: list[str]) -> bytes:
        commands.append(args)
        return original(root, args)

    monkeypatch.setattr(git_module, "_run_git", recording_run)
    hostile_ref = "--DO_NOT_ECHO_4471"

    with pytest.raises(InvalidRevisionError) as error:
        collect_diff(config_for(git_repo, f"--base={hostile_ref}", "--head=HEAD"))

    assert "diff" not in [args[0] for args in commands]
    assert hostile_ref not in str(error.value)


def test_option_like_revision_is_not_interpreted_as_an_option(git_repo: Path) -> None:
    with pytest.raises(InvalidRevisionError):
        collect_diff(config_for(git_repo, "--base=--help", "--head=HEAD"))


def test_nul_metadata_preserves_spaced_path(git_repo: Path) -> None:
    odd_name = "space dir/option-like -name.txt"
    path = git_repo / odd_name
    path.parent.mkdir()
    path.write_text("before\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    path.write_text("after\n", encoding="utf-8")

    result = collect_diff(config_for(git_repo))

    assert [item.path for item in result.changed_files] == [odd_name]


def test_nul_metadata_parser_preserves_spaces_and_unicode(git_repo: Path) -> None:
    path = "spaced dir/café.txt"
    parsed = _parse_name_status(f"M\x00{path}\x00".encode("utf-8"), git_repo)

    assert parsed == ((path, "M"),)


@pytest.mark.parametrize("path", ["bad\x1bname", "bad\nname", "bad\tname", "bad\x7fname", "bad\x85name"])
def test_nul_metadata_rejects_control_paths_without_echoing_them(
    git_repo: Path,
    path: str,
) -> None:
    with pytest.raises(GitError) as error:
        _parse_name_status(f"M\x00{path}\x00".encode("utf-8"), git_repo)

    assert str(error.value) == "Git returned an unsafe repository path."
    assert path not in str(error.value)


@pytest.mark.parametrize("metadata", [b"M\x00../escape\x00", b"M\x00/absolute\x00", b"M\x00bad\xff\x00"])
def test_malformed_or_unsafe_git_path_metadata_is_rejected(git_repo: Path, metadata: bytes) -> None:
    with pytest.raises(GitError):
        _parse_name_status(metadata, git_repo)


def test_changed_file_count_exact_maximum_is_allowed_and_overage_has_no_diff(git_repo: Path) -> None:
    for name in ("one.txt", "two.txt"):
        (git_repo / name).write_text("before\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    for name in ("one.txt", "two.txt"):
        (git_repo / name).write_text("after\n", encoding="utf-8")

    exact = collect_diff(config_for(git_repo, **{"max_changed_files": 2}))
    over = collect_diff(config_for(git_repo, **{"max_changed_files": 1}))

    assert exact.status is RunStatus.COMPLETED
    assert len(exact.changed_files) == 2
    assert over.status is RunStatus.LIMIT_EXCEEDED
    assert over.diff is None
    assert set(over.affected_inputs) == {"one.txt", "two.txt"}


def test_default_100_file_limit_is_exact(git_repo: Path, monkeypatch) -> None:
    diff_calls = 0
    changed_count = 100

    def fake_git(_root: Path, args: list[str]) -> bytes:
        nonlocal diff_calls
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return f"{git_repo}\n".encode()
        paths = [f"file-{index:03}.txt" for index in range(changed_count)]
        if "--name-status" in args:
            return b"".join(b"M\x00" + path.encode() + b"\x00" for path in paths)
        if "--numstat" in args:
            return b"".join(b"1\t1\t" + path.encode() + b"\x00" for path in paths)
        diff_calls += 1
        return b"diff"

    monkeypatch.setattr(git_module, "_run_git", fake_git)
    config = parse_args(["--repo", str(git_repo)])
    exact = collect_diff(config)
    changed_count = 101
    over = collect_diff(config)

    assert exact.status is RunStatus.COMPLETED
    assert len(exact.changed_files) == 100
    assert over.status is RunStatus.LIMIT_EXCEEDED
    assert over.diff is None
    assert len(over.affected_inputs) == 101
    assert diff_calls == 1


def test_text_diff_exact_maximum_is_allowed_and_overage_discards_diff(git_repo: Path) -> None:
    (git_repo / "content.txt").write_text("before\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    (git_repo / "content.txt").write_text("after\n", encoding="utf-8")
    unrestricted = collect_diff(config_for(git_repo))
    assert unrestricted.diff is not None

    exact = collect_diff(config_for(git_repo, **{"max_diff_bytes": len(unrestricted.diff)}))
    over = collect_diff(config_for(git_repo, **{"max_diff_bytes": len(unrestricted.diff) - 1}))

    assert exact.status is RunStatus.COMPLETED
    assert len(exact.diff or b"") == len(unrestricted.diff)
    assert over.status is RunStatus.LIMIT_EXCEEDED
    assert over.diff is None
    assert over.affected_inputs == ("content.txt",)


@pytest.mark.parametrize(
    ("diff_size", "expected_status"),
    [
        (DEFAULT_MAX_DIFF_BYTES, RunStatus.COMPLETED),
        (DEFAULT_MAX_DIFF_BYTES + 1, RunStatus.LIMIT_EXCEEDED),
    ],
)
def test_default_one_mib_diff_limit_is_exact(
    git_repo: Path,
    monkeypatch,
    diff_size: int,
    expected_status: RunStatus,
) -> None:
    def fake_git(_root: Path, args: list[str]) -> bytes:
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return f"{git_repo}\n".encode()
        if "--name-status" in args:
            return b"M\x00large.txt\x00"
        if "--numstat" in args:
            return b"1\t1\tlarge.txt\x00"
        return b"x" * diff_size

    monkeypatch.setattr(git_module, "_run_git", fake_git)

    result = collect_diff(parse_args(["--repo", str(git_repo)]))

    assert result.status is expected_status
    assert (result.diff is not None) is (diff_size == DEFAULT_MAX_DIFF_BYTES)
    if expected_status is RunStatus.LIMIT_EXCEEDED:
        assert result.affected_inputs == ("large.txt",)


def test_binary_metadata_does_not_depend_on_filename(git_repo: Path) -> None:
    (git_repo / "binary-disguised-as-text.txt").write_bytes(b"first\x00payload")
    commit_all(git_repo, "initial")
    (git_repo / "binary-disguised-as-text.txt").write_bytes(b"second\x00payload")

    result = collect_diff(config_for(git_repo))

    assert len(result.changed_files) == 1
    assert result.changed_files[0].is_binary is True
    assert result.diff is not None
    assert b"payload" not in result.diff
    snapshots = _collect_raw_file_snapshots(config_for(git_repo), result)
    assert snapshots.complete
    assert snapshots.snapshots[0].git_binary
    assert snapshots.snapshots[0].binary
    assert snapshots.snapshots[0].old.content is None
    assert snapshots.snapshots[0].new.content is None


def test_empty_diff_has_explicit_status(git_repo: Path) -> None:
    (git_repo / "tracked.txt").write_text("unchanged\n", encoding="utf-8")
    commit_all(git_repo, "initial")

    result = collect_diff(config_for(git_repo))

    assert result.status is RunStatus.EMPTY_DIFF
    assert result.diff == b""
    assert result.changed_files == ()


def test_git_unavailable_is_sanitized(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(git_module.shutil, "which", lambda _: None)

    with pytest.raises(GitUnavailableError) as error:
        inspect_repository(git_repo)

    assert str(error.value) == "Git executable is unavailable."


def test_git_command_failure_does_not_expose_stderr(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        git_module,
        "_run_bounded",
        lambda *args, **kwargs: _ProcessOutput(1, b"", b"SECRET_GIT_STDERR_892"),
    )

    with pytest.raises(GitError) as error:
        _run_git(git_repo, ["status"])

    assert str(error.value) == "Git operation failed."
    assert "SECRET_GIT_STDERR_892" not in str(error.value)


@pytest.mark.parametrize(
    ("stream", "limit", "payload_size", "fails"),
    [
        ("stdout", 128, 128, False),
        ("stdout", 128, 129, True),
        ("stderr", 128, 128, False),
        ("stderr", 128, 129, True),
    ],
)
def test_bounded_child_output_exact_and_over_ceilings(
    stream: str,
    limit: int,
    payload_size: int,
    fails: bool,
) -> None:
    code = (
        "import sys;"
        f"sys.{stream}.buffer.write(b'x' * {payload_size});"
        f"sys.{stream}.buffer.flush()"
    )

    if fails:
        with pytest.raises(GitOutputLimitError):
            _run_bounded([sys.executable, "-c", code], stdout_limit=limit, stderr_limit=limit)
    else:
        output = _run_bounded([sys.executable, "-c", code], stdout_limit=limit, stderr_limit=limit)
        captured = output.stdout if stream == "stdout" else output.stderr
        assert len(captured) == limit


def test_bounded_child_timeout_discards_partial_output() -> None:
    code = "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(5)"

    with pytest.raises(GitTimeoutError) as error:
        _run_bounded([sys.executable, "-c", code], timeout=0.1)

    assert "partial" not in str(error.value)


def test_non_repository_is_rejected_without_command_text(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repository"
    non_repo.mkdir()

    with pytest.raises(GitError) as error:
        inspect_repository(non_repo)

    assert "fatal:" not in str(error.value)