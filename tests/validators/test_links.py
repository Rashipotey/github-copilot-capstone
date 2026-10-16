from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

from docsync.evidence import MarkdownDocument, MarkdownInventory
from docsync.models import StageStatus, ValidatorStatus
from docsync.validators.links import validate_links


def inventory(*documents: tuple[str, str]) -> MarkdownInventory:
    return MarkdownInventory(
        tuple(MarkdownDocument(path, text) for path, text in documents),
        StageStatus.COMPLETED,
        True,
        "Complete.",
        0,
    )


def test_relative_file_link_passes(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "assets" / "logo.png").write_bytes(b"image")
    (tmp_path / "docs" / "readme.md").write_text("", encoding="utf-8")

    result = validate_links(
        tmp_path,
        "docs/readme.md",
        "[guide](guide.md) ![logo](../assets/logo.png)",
        inventory(("docs/guide.md", "# Guide\n")),
    )

    assert result.status is ValidatorStatus.PASS


def test_missing_local_path_fails(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("", encoding="utf-8")

    result = validate_links(
        tmp_path, "docs/readme.md", "[missing](missing.md)", inventory()
    )

    assert result.status is ValidatorStatus.FAIL


def test_reference_links_and_images_resolve_locally(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "docs" / "diagram.png").write_bytes(b"image")

    result = validate_links(
        tmp_path,
        "docs/readme.md",
        "[guide][ref] ![diagram][img]\n\n[ref]: guide.md#guide\n[img]: diagram.png",
        inventory(("docs/guide.md", "# Guide\n")),
    )

    assert result.status is ValidatorStatus.PASS


def test_virtual_proposal_satisfies_path_and_fragment_without_disk_file(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("", encoding="utf-8")
    proposed = "[new guide](new.md#first-release)"
    virtual = inventory(("docs/new.md", "# First Release\n"))

    result = validate_links(tmp_path, "docs/readme.md", proposed, virtual)

    assert result.status is ValidatorStatus.PASS


def test_missing_local_fragment_fails(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("", encoding="utf-8")

    result = validate_links(
        tmp_path,
        "docs/readme.md",
        "# Present\n\n[section](#absent)",
        inventory(("docs/readme.md", "# Present\n")),
    )

    assert result.status is ValidatorStatus.FAIL


def test_explicit_commonmark_html_fragment_is_resolved(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("", encoding="utf-8")

    result = validate_links(
        tmp_path,
        "readme.md",
        '<a id="custom-anchor"></a>\n\n[section](#custom-anchor)',
        inventory(),
    )

    assert result.status is ValidatorStatus.PASS


def test_remote_urls_are_not_fetched_or_checked(tmp_path: Path, monkeypatch) -> None:
    import urllib.request

    def no_fetch(*_args, **_kwargs):
        raise AssertionError("remote URL must not be fetched")

    monkeypatch.setattr(urllib.request, "urlopen", no_fetch)
    (tmp_path / "readme.md").write_text("", encoding="utf-8")

    result = validate_links(
        tmp_path, "readme.md", "[remote](https://example.invalid/unchecked)", inventory()
    )

    assert result.status is ValidatorStatus.PASS


def test_malformed_reference_is_inconclusive_and_traversal_fails(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("", encoding="utf-8")

    malformed = validate_links(
        tmp_path, "docs/readme.md", "[bad](file%zz.md)", inventory()
    )
    traversal = validate_links(
        tmp_path, "docs/readme.md", "[escape](../../outside.md)", inventory()
    )
    windows_path = validate_links(
        tmp_path, "docs/readme.md", r"[escape](C:\outside.md)", inventory()
    )

    assert malformed.status is ValidatorStatus.INCONCLUSIVE
    assert traversal.status is ValidatorStatus.FAIL
    assert windows_path.status is ValidatorStatus.FAIL


def test_symlink_target_is_rejected(tmp_path: Path, monkeypatch) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "readme.md").write_text("", encoding="utf-8")
    original_lstat = Path.lstat

    def fake_lstat(path: Path):
        if path == docs / "linked.md":
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    result = validate_links(
        tmp_path, "docs/readme.md", "[linked](linked.md)", inventory()
    )

    assert result.status is ValidatorStatus.FAIL


def test_incomplete_inventory_is_inconclusive(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("", encoding="utf-8")
    incomplete = MarkdownInventory((), StageStatus.INCONCLUSIVE, False, "incomplete", 0)

    result = validate_links(tmp_path, "readme.md", "[a](missing.md)", incomplete)

    assert result.status is ValidatorStatus.INCONCLUSIVE