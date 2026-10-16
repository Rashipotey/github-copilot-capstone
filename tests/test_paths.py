import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from docsync.paths import (
    OUTPUT_NAMES,
    PathValidationError,
    RepositoryFileChangedError,
    RepositoryFileTooLargeError,
    _is_reparse_point,
    canonicalize_repository_root,
    validate_output_paths,
    validate_repository_name,
    validate_repository_path,
    read_repository_file,
)


@pytest.mark.parametrize(
    "raw_path",
    [
        "",
        ".",
        "../outside",
        "src/../../outside",
        "/absolute/file",
        r"C:\outside",
        r"src\file.py",
        "bad\x00name",
        "bad\x1bname",
        "bad\nname",
        "bad\tname",
        "bad\x7fname",
        "bad\x85name",
    ],
)
def test_repository_names_reject_unsafe_forms(raw_path: str) -> None:
    with pytest.raises(PathValidationError):
        validate_repository_name(raw_path)


def test_repository_name_normalizes_dot_and_repeated_separator() -> None:
    assert validate_repository_name("./docs//guide.md") == ("docs", "guide.md")


def test_bounded_repository_read_allows_exact_size_and_rejects_oversize(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "exact.md").write_bytes(b"four")
    (repo / "large.md").write_bytes(b"five!")

    assert read_repository_file(repo, "exact.md", maximum_bytes=4) == b"four"
    with pytest.raises(RepositoryFileTooLargeError):
        read_repository_file(repo, "large.md", maximum_bytes=4)


def test_bounded_repository_read_discards_content_when_file_grows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    from types import SimpleNamespace

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "growing.md").write_bytes(b"stable")
    original_fstat = os.fstat
    calls = 0

    def growing_fstat(descriptor: int):
        nonlocal calls
        metadata = original_fstat(descriptor)
        calls += 1
        if calls != 2:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_size=metadata.st_size + 1,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns,
            st_file_attributes=0,
        )

    monkeypatch.setattr("docsync.paths.os.fstat", growing_fstat)

    with pytest.raises(RepositoryFileChangedError):
        read_repository_file(repo, "growing.md", maximum_bytes=32)


def test_reparse_point_attribute_is_rejected() -> None:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    metadata = SimpleNamespace(st_file_attributes=reparse_attribute)

    assert _is_reparse_point(metadata)


def test_repository_path_rejects_simulated_reparse_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    original_lstat = Path.lstat
    junction = repo / "junction"
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def lstat_with_junction(path: Path):
        if path == junction:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=reparse_attribute)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_junction)

    with pytest.raises(PathValidationError):
        validate_repository_path(repo, "junction/evidence.py", must_exist=False)


def test_output_path_rejects_simulated_symlink_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    original_lstat = Path.lstat
    linked_dir = repo / "linked-output"

    def lstat_with_link(path: Path):
        if path == linked_dir:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_link)

    with pytest.raises(PathValidationError):
        validate_output_paths(repo, linked_dir)


def test_output_paths_are_canonical_plans_and_do_not_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "guide.md").write_text("source", encoding="utf-8")

    planned = validate_output_paths(repo, Path("artifacts/run"), ["docs/guide.md"])

    assert planned.directory == (repo / "artifacts" / "run").resolve()
    assert planned.report.name == OUTPUT_NAMES[0]
    assert planned.patch.name == OUTPUT_NAMES[1]
    assert not planned.directory.exists()


def test_output_target_collision_with_repository_input_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(PathValidationError, match="collides"):
        validate_output_paths(repo, repo, [OUTPUT_NAMES[0]])


def test_existing_non_regular_output_target_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / OUTPUT_NAMES[0]).mkdir()

    with pytest.raises(PathValidationError):
        validate_output_paths(repo, repo)