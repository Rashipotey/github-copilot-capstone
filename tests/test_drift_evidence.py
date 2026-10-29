from pathlib import Path

import pytest

from docsync import drift as drift_module
from docsync.config import DriftConfig
from docsync.drift import (
    DriftChangeSet,
    DriftSourceChange,
    DriftUnanalyzedChange,
    collect_drift_evidence,
)
from docsync.evidence import RawInputBudget
from docsync.git import MAX_EVIDENCE_FILE_BYTES
from docsync.screening import SecretScanner


def config_for(repo: Path, *, excludes: tuple[str, ...] = ()) -> DriftConfig:
    return DriftConfig(repo=repo, output_dir=Path("reports"), target=None, excludes=excludes)


def changes_for(*paths: str) -> DriftChangeSet:
    return DriftChangeSet(
        tuple(DriftSourceChange(path, "modified") for path in paths),
        (),
    )


def records_by_path(records):
    return {item.path: item for item in records}


def test_drift_collects_only_bounded_screened_syntactically_valid_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text('{"enabled": true}\n', encoding="utf-8")
    (tmp_path / "settings.toml").write_text("enabled = true\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Safe documentation\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Nested guide\n", encoding="utf-8")

    result = collect_drift_evidence(
        config_for(tmp_path),
        changes_for("module.py", "settings.json", "settings.toml"),
    )

    assert not result.fatal
    sources = records_by_path(result.sources)
    assert set(sources) == {"module.py", "settings.json", "settings.toml"}
    assert all(item.status == "available" and item.content for item in sources.values())
    assert all(item.change_status == "modified" for item in sources.values())
    assert sources["module.py"].content.replace("\r\n", "\n") == "def run():\n    return 1\n"
    assert sources["settings.json"].content.replace("\r\n", "\n") == '{"enabled": true}\n'
    assert [item.path for item in result.markdown if item.status == "available"] == [
        "README.md",
        "docs/guide.md",
    ]
    assert all(
        item.content is None or "[REDACTED]" not in item.content
        for item in (*result.sources, *result.markdown)
    )


def test_drift_exclusions_apply_to_changed_sources_and_markdown_inventory(
    tmp_path: Path,
) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "hidden.py").write_text("valid = True\n", encoding="utf-8")
    (tmp_path / "private" / "guide.md").write_text("# Private\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Public\n", encoding="utf-8")

    result = collect_drift_evidence(
        config_for(tmp_path, excludes=("private/**",)),
        changes_for("private/hidden.py"),
    )

    assert not result.fatal
    assert [(item.path, item.reason, item.content) for item in result.sources] == [
        ("private/hidden.py", "excluded", None)
    ]
    assert [item.path for item in result.markdown if item.status == "available"] == [
        "README.md"
    ]
    assert any(
        item.path == "private" and item.reason == "excluded"
        for item in result.markdown
    )


def test_drift_retains_per_file_unreadable_oversized_and_parse_failed_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "unreadable.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "oversized.py").write_bytes(b"x" * (MAX_EVIDENCE_FILE_BYTES + 1))
    (tmp_path / "invalid.json").write_text('{"missing": }', encoding="utf-8")
    (tmp_path / "invalid.toml").write_text("key = [", encoding="utf-8")
    (tmp_path / "invalid.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (tmp_path / "valid.py").write_text("value = 2\n", encoding="utf-8")
    (tmp_path / "ok.md").write_text("# Still available\n", encoding="utf-8")

    real_read = drift_module.read_repository_file

    def selectively_unreadable(root, path, *, maximum_bytes):
        if path == "unreadable.py":
            raise PermissionError("private diagnostic must not escape")
        return real_read(root, path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(drift_module, "read_repository_file", selectively_unreadable)
    changes = changes_for(
        "unreadable.py",
        "oversized.py",
        "invalid.json",
        "invalid.toml",
        "invalid.py",
        "valid.py",
    )

    result = collect_drift_evidence(config_for(tmp_path), changes)

    assert not result.fatal
    sources = records_by_path(result.sources)
    assert sources["unreadable.py"].reason == "unreadable"
    assert sources["oversized.py"].reason == "over_size"
    assert all(
        sources[path].reason == "parse_failed"
        and sources[path].content is None
        for path in ("invalid.json", "invalid.toml", "invalid.py")
    )
    assert sources["valid.py"].status == "available"
    assert any(item.path == "ok.md" and item.status == "available" for item in result.markdown)
    assert "private diagnostic" not in repr(result)


def test_drift_markdown_inventory_retains_file_local_read_and_size_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "unreadable.md").write_text("# Hidden\n", encoding="utf-8")
    (tmp_path / "oversized.md").write_bytes(b"x" * (MAX_EVIDENCE_FILE_BYTES + 1))
    (tmp_path / "available.md").write_text("# Available\n", encoding="utf-8")

    real_read = drift_module.read_repository_file

    def selectively_unreadable(root, path, *, maximum_bytes):
        if path == "unreadable.md":
            raise PermissionError("markdown read error")
        return real_read(root, path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(drift_module, "read_repository_file", selectively_unreadable)
    result = collect_drift_evidence(config_for(tmp_path), DriftChangeSet((), ()))

    assert not result.fatal
    markdown = records_by_path(result.markdown)
    assert markdown["unreadable.md"].reason == "unreadable"
    assert markdown["unreadable.md"].content is None
    assert markdown["oversized.md"].reason == "over_size"
    assert markdown["oversized.md"].content is None
    assert markdown["available.md"].status == "available"


def test_drift_cumulative_limit_marks_items_unanalyzed_without_content(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text("value = 123\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Documentation\n", encoding="utf-8")

    result = collect_drift_evidence(
        config_for(tmp_path),
        changes_for("module.py"),
        budget=RawInputBudget(maximum_bytes=4),
    )

    assert not result.fatal
    assert result.sources[0].reason == "resource_limit"
    assert result.sources[0].content is None
    assert any(
        item.path == "README.md"
        and item.reason == "resource_limit"
        and item.content is None
        for item in result.markdown
    )


def test_drift_invalid_utf8_is_fatal_and_withholds_all_evidence(
    tmp_path: Path,
) -> None:
    secret = "DECODE_SECRET_9341"
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_bytes(
        f"# token={secret}\n".encode("utf-8") + b"\xff"
    )

    result = collect_drift_evidence(
        config_for(tmp_path),
        changes_for("module.py"),
    )

    assert result.fatal
    assert result.sources == ()
    assert result.markdown == ()
    assert secret not in repr(result)
    assert "token=" not in repr(result)


def test_drift_invalid_utf8_source_is_fatal_and_withheld(tmp_path: Path) -> None:
    secret = "SOURCE_DECODE_SECRET_331"
    (tmp_path / "module.py").write_bytes(
        f"# token={secret}\n".encode("utf-8") + b"\xff"
    )

    result = collect_drift_evidence(config_for(tmp_path), changes_for("module.py"))

    assert result.fatal
    assert result.sources == ()
    assert secret not in repr(result)


def test_drift_detected_secret_redaction_is_not_accepted_as_success(
    tmp_path: Path,
) -> None:
    secret = "SCREEN_SECRET_5703"
    (tmp_path / "module.py").write_text(
        f"# safe context\nTOKEN={secret}\n", encoding="utf-8"
    )

    result = collect_drift_evidence(config_for(tmp_path), changes_for("module.py"))

    assert result.fatal
    assert result.sources == ()
    assert secret not in repr(result)
    assert "[REDACTED]" not in repr(result)


def test_drift_required_screening_failure_is_fatal_and_sanitized(
    tmp_path: Path,
) -> None:
    secret = "SCANNER_FAILURE_SECRET_339"
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")

    class RaisingScanner(SecretScanner):
        def screen_evidence_item(self, content, *, provenance=None):
            raise RuntimeError(secret)

    result = collect_drift_evidence(
        config_for(tmp_path),
        changes_for("module.py"),
        scanner=RaisingScanner(),
    )

    assert result.fatal
    assert result.sources == ()
    assert secret not in repr(result)


def test_drift_preserves_existing_change_set_unanalyzed_statuses(
    tmp_path: Path,
) -> None:
    changes = DriftChangeSet(
        (),
        (
            DriftUnanalyzedChange("gone.py", "deleted"),
            DriftUnanalyzedChange("README.rst", "unsupported"),
            DriftUnanalyzedChange("private.py", "excluded"),
        ),
    )

    result = collect_drift_evidence(config_for(tmp_path), changes)

    assert not result.fatal
    assert [(item.path, item.reason, item.content) for item in result.sources] == [
        ("README.rst", "unsupported", None),
        ("gone.py", "deleted", None),
        ("private.py", "excluded", None),
    ]
