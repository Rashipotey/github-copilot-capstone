from pathlib import Path

import pytest

from docsync.config import DriftConfig
from docsync.git import GitError
from docsync.drift import (
    DriftAnalysisStatus,
    DriftChangeSet,
    DriftSourceChange,
    DriftUnanalyzedChange,
    analyze_drift,
)
from docsync.screening import SecretScanner


def config_for(
    repo: Path,
    *,
    output_dir: Path | None = None,
    target: str | None = None,
) -> DriftConfig:
    output = output_dir or repo / "output"
    output.mkdir(parents=True, exist_ok=True)
    return DriftConfig(repo=repo, output_dir=output, target=target)


def use_changes(monkeypatch: pytest.MonkeyPatch, changes: DriftChangeSet) -> None:
    monkeypatch.setattr("docsync.drift.acquire_change_set", lambda config: changes)


def test_drift_findings_use_candidate_only_language_and_do_not_emit_source_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text(
        "def process_job():\n    return 'PRIVATE_SOURCE_BODY_42'\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "`process_job` is described here.\n", encoding="utf-8"
    )
    use_changes(
        monkeypatch,
        DriftChangeSet((DriftSourceChange("src/worker.py", "modified"),), ()),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.COMPLETE
    assert result.exit_code == 0
    assert result.report_markdown is not None
    assert "potential drift candidate" in result.report_markdown
    assert (
        "candidate only" in result.report_markdown.lower()
        or "potential" in result.report_markdown.lower()
    )
    assert "verified" not in result.report_markdown.lower()
    assert "PRIVATE_SOURCE_BODY_42" not in result.report_markdown
    assert "process_job" in result.report_markdown
    assert list((tmp_path / "output").iterdir()) == []


def test_drift_no_relationship_and_ambiguity_have_distinct_rendered_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("General documentation.\n", encoding="utf-8")
    changes = DriftChangeSet((DriftSourceChange("src/worker.py", "modified"),), ())
    use_changes(monkeypatch, changes)

    no_match = analyze_drift(config_for(tmp_path))

    assert no_match.status is DriftAnalysisStatus.COMPLETE
    assert no_match.report_markdown is not None
    assert "No relationship established" in no_match.report_markdown

    (tmp_path / "README.md").write_text("`process_job` overview.\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "`process_job` usage.\n", encoding="utf-8"
    )
    (tmp_path / "src" / "worker.py").write_text(
        "def process_job():\n    pass\n", encoding="utf-8"
    )
    ambiguous = analyze_drift(config_for(tmp_path))

    assert ambiguous.status is DriftAnalysisStatus.AMBIGUOUS
    assert ambiguous.exit_code != 0
    assert ambiguous.report_markdown is None
    assert "README.md" in ambiguous.message
    assert "docs/guide.md" in ambiguous.message

    invalid_target = analyze_drift(
        config_for(
            tmp_path,
            output_dir=tmp_path / "output",
            target="docs/not-a-candidate.md",
        )
    )
    assert invalid_target.status is DriftAnalysisStatus.INVALID_TARGET
    assert invalid_target.exit_code != 0
    assert invalid_target.report_markdown is None
    assert "README.md" in invalid_target.message
    assert "docs/guide.md" in invalid_target.message
    assert list((tmp_path / "output").iterdir()) == []


def test_drift_complete_report_includes_empty_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_changes(monkeypatch, DriftChangeSet((), ()))

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.COMPLETE
    assert result.exit_code == 0
    assert result.report_markdown is not None
    assert "## Findings" in result.report_markdown
    assert "No candidate findings." in result.report_markdown
    assert result.analyzed_source_count == 0


def test_drift_partial_includes_successful_no_relationship_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("General documentation.\n", encoding="utf-8")
    use_changes(
        monkeypatch,
        DriftChangeSet(
            (DriftSourceChange("src/worker.py", "modified"),),
            (DriftUnanalyzedChange("gone.py", "deleted"),),
        ),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.PARTIAL
    assert result.exit_code != 0
    assert result.report_markdown is not None
    assert "**Analysis status:** Partial" in result.report_markdown
    assert "No relationship established" in result.report_markdown
    assert "gone.py" in result.report_markdown
    assert result.analyzed_source_count == 1


def test_drift_inventory_limit_returns_partial_report_with_incomplete_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    for index in range(500):
        (tmp_path / f"entry-{index:03}.txt").write_text("", encoding="utf-8")
    use_changes(
        monkeypatch,
        DriftChangeSet((DriftSourceChange("module.py", "modified"),), ()),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.PARTIAL
    assert result.exit_code != 0
    assert result.report_markdown is not None
    assert "**Analysis status:** Partial" in result.report_markdown
    assert any(
        item.path == "[repository inventory]"
        and item.reason == "resource_limit"
        and item.sanitized_detail
        == (
            "Markdown inventory entry limit (500) was exceeded; "
            "remaining entries were not inspected."
        )
        for item in result.unanalyzed_inputs
    )
    assert (
        "Markdown inventory entry limit \\(500\\) was exceeded"
        in result.report_markdown
    )
    assert "[repository inventory]" in result.report_markdown
    assert "Insufficient evidence" in result.report_markdown
    assert "No relationship established" not in result.report_markdown
    assert result.match_result is not None
    assert result.match_result.selection_status == "inconclusive"
    assert result.match_result.candidate_paths == ()
    assert result.analyzed_source_count == 1


def test_drift_partial_without_successfully_analyzed_source_writes_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_changes(
        monkeypatch,
        DriftChangeSet(
            (),
            (DriftUnanalyzedChange("gone.py", "deleted"),),
        ),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.PARTIAL
    assert result.exit_code != 0
    assert result.report_markdown is None
    assert result.analyzed_source_count == 0


def test_drift_partial_report_is_labeled_and_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "src" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    use_changes(
        monkeypatch,
        DriftChangeSet(
            (
                DriftSourceChange("src/ok.py", "modified"),
                DriftSourceChange("src/bad.py", "modified"),
            ),
            (),
        ),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.PARTIAL
    assert result.exit_code != 0
    assert result.report_markdown is not None
    assert "**Analysis status:** Partial" in result.report_markdown
    assert "src/bad.py" in result.report_markdown
    assert result.analyzed_source_count == 1


def test_drift_fatal_acquisition_or_unsafe_output_path_has_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "PRIVATE_GIT_DIAGNOSTIC_918"

    def failed_acquisition(config):
        raise GitError(secret)

    monkeypatch.setattr("docsync.drift.acquire_change_set", failed_acquisition)
    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.FATAL
    assert result.exit_code != 0
    assert result.report_markdown is None
    assert secret not in result.message

    missing_output = tmp_path / "missing-output"
    missing_config = DriftConfig(repo=tmp_path, output_dir=missing_output, target=None)
    result = analyze_drift(missing_config)
    assert result.status is DriftAnalysisStatus.FATAL
    assert result.report_markdown is None
    assert "unsafe or unavailable" in result.message


def test_drift_required_decode_or_screen_failure_is_fatal_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    secret = "DECODE_SECRET_FOR_REPORT_880"
    (tmp_path / "src" / "bad.py").write_bytes(
        f"# token={secret}\n".encode("utf-8") + b"\xff"
    )
    use_changes(
        monkeypatch,
        DriftChangeSet((DriftSourceChange("src/bad.py", "modified"),), ()),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.FATAL
    assert result.report_markdown is None
    assert secret not in result.message
    assert "token=" not in result.message


def test_drift_never_counts_skipped_items_as_analyzed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    use_changes(
        monkeypatch,
        DriftChangeSet((DriftSourceChange("src/bad.py", "modified"),), ()),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.status is DriftAnalysisStatus.PARTIAL
    assert result.analyzed_source_count == 0
    assert result.report_markdown is None
    assert result.unanalyzed_inputs[0].reason == "parse_failed"


def test_drift_renderer_emits_valid_markdown_with_escaped_untrusted_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "src" / "[guide](x).md").write_text(
        "Unrelated content.\n", encoding="utf-8"
    )
    use_changes(
        monkeypatch,
        DriftChangeSet((DriftSourceChange("src/module.py", "modified"),), ()),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.report_markdown is not None
    assert result.report_markdown.startswith("# Documentation Drift Analysis\n")
    assert "`src/[guide](x).md`" in result.report_markdown
    assert "## Findings" in result.report_markdown
    assert not result.report_markdown.endswith("\n\n")


def test_drift_reason_label_does_not_trigger_credential_screening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "The worker module is described here.\n", encoding="utf-8"
    )
    use_changes(
        monkeypatch,
        DriftChangeSet((DriftSourceChange("src/worker.py", "modified"),), ()),
    )

    result = analyze_drift(config_for(tmp_path))

    assert result.report_markdown is not None
    assert "token (`worker`)" in result.report_markdown
    screened = SecretScanner().screen_output(result.report_markdown.encode("utf-8"))
    assert screened.usable
    assert screened.sanitized_text == result.report_markdown
