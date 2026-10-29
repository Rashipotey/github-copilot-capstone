from __future__ import annotations

from docsync.drift import DriftEvidenceCollection, DriftEvidenceItem
from docsync.drift_matching import match_drift_candidates


def available(path: str, kind: str, content: str) -> DriftEvidenceItem:
    return DriftEvidenceItem(
        path,
        kind,
        "available",
        None,
        "screened",
        content,
        "modified" if kind == "source" else None,
    )


def collection(
    *sources: DriftEvidenceItem,
    markdown: tuple[DriftEvidenceItem, ...],
    markdown_inventory_truncated: bool = False,
) -> DriftEvidenceCollection:
    return DriftEvidenceCollection(
        tuple(sources),
        markdown,
        False,
        "complete evidence",
        0,
        markdown_inventory_truncated,
    )


def test_drift_path_reference_matches_exact_case_sensitive_posix_text() -> None:
    evidence = collection(
        available("src/Widget.py", "source", "value = 1\n"),
        markdown=(
            available("README.md", "markdown", "`src/Widget.py`\n"),
            available("case.md", "markdown", "`src/widget.py`\n"),
            available("slash.md", "markdown", r"`src\Widget.py`" + "\n"),
        ),
    )

    result = match_drift_candidates(evidence)

    assert result.candidate_paths == ("README.md", "slash.md")
    readme_reasons = {
        (reason.signal, reason.value, reason.evidence_line)
        for reason in result.candidates[0].reasons
    }
    slash_reasons = {
        (reason.signal, reason.value, reason.evidence_line)
        for reason in result.candidates[1].reasons
    }
    assert ("source_path", "src/Widget.py", 1) in readme_reasons
    assert not any(reason[0] == "source_path" for reason in slash_reasons)
    assert not any(
        reason[0] == "source_path"
        for candidate in result.candidates
        if candidate.path == "case.md"
        for reason in candidate.reasons
    )


def test_drift_token_matches_respect_ascii_boundaries_and_case() -> None:
    evidence = collection(
        available("src/widget.py", "source", "def open_item():\n    return 1\n"),
        markdown=(
            available(
                "README.md",
                "markdown",
                "xopen_item\n_open_item\nopen_item9\nOpen_item\nopen_item!\néopen_item\n",
            ),
        ),
    )

    result = match_drift_candidates(evidence)

    symbol_hits = [
        reason.evidence_line
        for reason in result.candidates[0].reasons
        if reason.signal == "symbol"
    ]
    assert symbol_hits == [5, 6]


def test_drift_source_stem_is_a_token_bounded_filename_signal() -> None:
    evidence = collection(
        available("src/account.py", "source", "value = 1\n"),
        markdown=(
            available("README.md", "markdown", "accounts are described here\n"),
            available("guide.md", "markdown", "`account` is covered\n"),
        ),
    )

    result = match_drift_candidates(evidence)

    assert result.candidate_paths == ("guide.md",)
    assert len(result.candidates[0].reasons) == 1
    reason = result.candidates[0].reasons[0]
    assert (reason.signal, reason.value, reason.evidence_line) == (
        "source_stem",
        "account",
        1,
    )


def test_drift_same_directory_uses_exact_immediate_parent_only() -> None:
    evidence = collection(
        available("src/module.py", "source", "value = 1\n"),
        markdown=(
            available("src/guide.md", "markdown", "General guide\n"),
            available("guide.md", "markdown", "General guide\n"),
            available("src/nested/guide.md", "markdown", "General guide\n"),
        ),
    )

    result = match_drift_candidates(evidence)

    assert result.candidate_paths == ("src/guide.md",)
    reason = result.candidates[0].reasons[0]
    assert (reason.signal, reason.value, reason.evidence_line) == (
        "same_immediate_parent",
        "src",
        None,
    )


def test_drift_candidate_union_records_all_reasons_and_evidence_lines() -> None:
    evidence = collection(
        available(
            "src/settings.json",
            "source",
            '{"enabled": true, "feature_flag": false}\n',
        ),
        markdown=(
            available(
                "src/settings.md",
                "markdown",
                "Reference src/settings.json.\n"
                "settings and enabled are documented.\n"
                "feature_flag appears here.\n",
            ),
        ),
    )

    result = match_drift_candidates(evidence)

    reasons = result.candidates[0].reasons
    assert {(reason.signal, reason.value) for reason in reasons} == {
        ("source_path", "src/settings.json"),
        ("source_stem", "settings"),
        ("config_key", "enabled"),
        ("config_key", "feature_flag"),
        ("same_immediate_parent", "src"),
    }
    assert {
        (reason.signal, reason.value, reason.evidence_line)
        for reason in reasons
    } == {
        ("source_path", "src/settings.json", 1),
        ("source_stem", "settings", 1),
        ("source_stem", "settings", 2),
        ("config_key", "enabled", 2),
        ("config_key", "feature_flag", 3),
        ("same_immediate_parent", "src", None),
    }


def _ambiguous_evidence() -> DriftEvidenceCollection:
    return collection(
        available("src/worker.py", "source", "def process_job():\n    pass\n"),
        markdown=(
            available("README.md", "markdown", "`process_job` is used.\n"),
            available("docs/worker.md", "markdown", "`process_job` is used.\n"),
        ),
    )


def test_drift_multiple_candidates_without_target_are_ambiguous_and_block_report() -> None:
    result = match_drift_candidates(_ambiguous_evidence())

    assert result.selection_status == "ambiguous"
    assert result.candidate_paths == ("README.md", "docs/worker.md")
    assert result.selected_path is None
    assert result.selected_candidates == ()
    assert not result.report_permitted
    assert not result.patch_permitted
    assert result.source_matches[0].ambiguous
    assert "README.md" in result.detail
    assert "docs/worker.md" in result.detail


def test_drift_invalid_target_lists_valid_candidates() -> None:
    result = match_drift_candidates(_ambiguous_evidence(), target="docs/unknown.md")

    assert result.selection_status == "invalid_target"
    assert result.selected_path is None
    assert result.selected_candidates == ()
    assert not result.report_permitted
    assert "README.md" in result.detail
    assert "docs/worker.md" in result.detail


def test_drift_explicit_target_selects_one_candidate_only() -> None:
    result = match_drift_candidates(_ambiguous_evidence(), target="docs/worker.md")

    assert result.selection_status == "selected"
    assert result.selected_path == "docs/worker.md"
    assert len(result.selected_candidates) == 1
    assert result.selected_candidates[0].path == "docs/worker.md"
    assert result.selected_candidates[0].relationship_status == "candidate only"
    assert not result.selected_candidates[0].verified
    assert result.candidate_paths == ("README.md", "docs/worker.md")
    assert result.source_matches[0].ambiguous
    assert result.source_matches[0].selected_path == "docs/worker.md"
    assert result.report_permitted
    assert "candidate only" in result.detail
    assert result.candidate_only


def test_drift_sole_candidate_is_selected_as_candidate_not_verified() -> None:
    result = match_drift_candidates(
        collection(
            available("src/worker.py", "source", "def process_job():\n    pass\n"),
            markdown=(
                available("README.md", "markdown", "`process_job` is used.\n"),
            ),
        )
    )

    assert result.selection_status == "selected"
    assert result.selected_path == "README.md"
    assert len(result.selected_candidates) == 1
    assert result.selected_candidates[0].path == "README.md"
    assert result.selected_candidates[0].relationship_status == "candidate only"
    assert not result.selected_candidates[0].verified
    assert result.report_permitted


def test_drift_no_candidate_means_no_relationship_established() -> None:
    result = match_drift_candidates(
        collection(
            available("src/worker.py", "source", "value = 1\n"),
            markdown=(
                available("README.md", "markdown", "General documentation.\n"),
            ),
        )
    )

    assert result.selection_status == "no_relationship"
    assert result.candidate_paths == ()
    assert result.selected_path is None
    assert result.selected_candidates == ()
    assert result.source_matches[0].relationship_status == "no relationship established"
    assert result.detail.lower().startswith("no relationship established")
    assert result.report_permitted
    assert not result.candidate_only


def test_drift_incomplete_inventory_withholds_candidate_and_relationship_selection() -> None:
    evidence = collection(
        available("module.py", "source", "value = 1\n"),
        markdown=(available("module.md", "markdown", "Module overview.\n"),),
        markdown_inventory_truncated=True,
    )

    for target in (None, "module.md"):
        result = match_drift_candidates(evidence, target=target)

        assert result.selection_status == "inconclusive"
        assert result.report_permitted
        assert result.candidate_paths == ()
        assert result.selected_path is None
        assert result.source_matches[0].relationship_status == "insufficient evidence"


def test_drift_ignores_unavailable_source_and_markdown_content() -> None:
    evidence = DriftEvidenceCollection(
        (
            DriftEvidenceItem(
                "src/private.py",
                "source",
                "unanalyzed",
                "unreadable",
                "unavailable",
                None,
            ),
            available("src/worker.py", "source", "def process_job():\n    pass\n"),
        ),
        (
            DriftEvidenceItem(
                "README.md",
                "markdown",
                "unanalyzed",
                "over_size",
                "unavailable",
                None,
            ),
        ),
        False,
        "partial evidence",
        0,
    )

    result = match_drift_candidates(evidence)

    assert len(result.source_matches) == 1
    assert result.source_matches[0].source_path == "src/worker.py"
    assert result.candidate_paths == ()


def test_drift_fatal_evidence_is_not_matched() -> None:
    result = match_drift_candidates(
        DriftEvidenceCollection((), (), True, "Safe fatal detail.", 0),
        target="README.md",
    )

    assert result.selection_status == "fatal_evidence"
    assert result.fatal_evidence
    assert not result.report_permitted
    assert result.source_matches == ()
