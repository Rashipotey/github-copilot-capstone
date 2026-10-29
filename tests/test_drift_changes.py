import subprocess
from pathlib import Path

from docsync.config import DriftConfig
from docsync.drift import acquire_change_set


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


def config_for(repo: Path, **options) -> DriftConfig:
    return DriftConfig(
        repo=repo,
        output_dir=Path("reports"),
        target=None,
        **options,
    )


def test_drift_acquires_staged_and_unstaged_changes_against_head(git_repo: Path) -> None:
    (git_repo / "modified.py").write_text("before\n", encoding="utf-8")
    commit_all(git_repo, "initial")

    (git_repo / "modified.py").write_text("staged\n", encoding="utf-8")
    git(git_repo, "add", "modified.py")
    (git_repo / "modified.py").write_text("unstaged\n", encoding="utf-8")
    (git_repo / "added.json").write_text('{"new": true}\n', encoding="utf-8")
    git(git_repo, "add", "added.json")
    (git_repo / "untracked.toml").write_text("new = true\n", encoding="utf-8")

    changes = acquire_change_set(config_for(git_repo))

    assert [(item.path, item.status) for item in changes.sources] == [
        ("added.json", "added"),
        ("modified.py", "modified"),
    ]
    assert changes.unanalyzed == ()


def test_drift_maps_rename_current_path_to_added_and_old_path_to_deleted(
    git_repo: Path,
) -> None:
    old_path = git_repo / "old.py"
    old_path.write_text("def example():\n    return 1\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    git(git_repo, "mv", "old.py", "new.py")

    changes = acquire_change_set(config_for(git_repo))

    assert [(item.path, item.status) for item in changes.sources] == [("new.py", "added")]
    assert [(item.path, item.reason) for item in changes.unanalyzed] == [
        ("old.py", "deleted")
    ]


def test_drift_excludes_untracked_and_reports_deleted_and_unsupported_paths(
    git_repo: Path,
) -> None:
    (git_repo / "removed.py").write_text("before\n", encoding="utf-8")
    (git_repo / "README.md").write_text("before\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    git(git_repo, "rm", "--quiet", "removed.py")
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    (git_repo / "untracked.py").write_text("not tracked\n", encoding="utf-8")

    changes = acquire_change_set(config_for(git_repo))

    assert changes.sources == ()
    assert [(item.path, item.reason) for item in changes.unanalyzed] == [
        ("README.md", "unsupported"),
        ("removed.py", "deleted"),
    ]


def test_drift_sorts_normalized_paths_deterministically(git_repo: Path) -> None:
    (git_repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    for path in ("z/module.py", "a/module.py", "m/module.json"):
        (git_repo / Path(path).parent).mkdir(parents=True, exist_ok=True)
        (git_repo / path).write_text("changed\n", encoding="utf-8")
        git(git_repo, "add", path)

    changes = acquire_change_set(config_for(git_repo))

    assert [item.path for item in changes.sources] == [
        "a/module.py",
        "m/module.json",
        "z/module.py",
    ]
    assert all("\\" not in item.path for item in changes.sources)


def test_drift_applies_configured_exclusions_and_changed_file_limit(
    git_repo: Path,
) -> None:
    (git_repo / "src").mkdir()
    (git_repo / "src" / "excluded.py").write_text("excluded\n", encoding="utf-8")
    (git_repo / "src" / "included.py").write_text("included\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    (git_repo / "src" / "excluded.py").write_text("changed\n", encoding="utf-8")
    (git_repo / "src" / "included.py").write_text("changed\n", encoding="utf-8")

    changes = acquire_change_set(
        config_for(git_repo, excludes=("src/excluded.py",), max_changed_files=1)
    )

    assert changes.sources == ()
    assert [(item.path, item.reason) for item in changes.unanalyzed] == [
        ("src/excluded.py", "excluded"),
        ("src/included.py", "changed-file-limit"),
    ]


def test_drift_applies_configured_diff_byte_limit(git_repo: Path) -> None:
    (git_repo / "module.py").write_text("before\n", encoding="utf-8")
    commit_all(git_repo, "initial")
    (git_repo / "module.py").write_text("after\n", encoding="utf-8")

    changes = acquire_change_set(config_for(git_repo, max_diff_bytes=1))

    assert changes.sources == ()
    assert [(item.path, item.reason) for item in changes.unanalyzed] == [
        ("module.py", "diff-size-limit")
    ]
