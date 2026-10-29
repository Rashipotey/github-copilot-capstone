"""Deterministic candidate matching and target selection for drift analysis."""

from __future__ import annotations

import keyword
from dataclasses import dataclass
from pathlib import PurePosixPath

from docsync.drift import DriftEvidenceCollection, DriftEvidenceItem
from docsync.targets import (
    _JSON_KEY,
    _PYTHON_DECLARATION,
    _TOML_KEY,
    _signal_lines,
)


@dataclass(frozen=True, slots=True)
class DriftMatchReason:
    """One exact matching signal and its Markdown evidence line, if textual."""

    signal: str
    value: str
    evidence_line: int | None


@dataclass(frozen=True, slots=True)
class DriftCandidate:
    """A potential documentation candidate, never a verified relationship."""

    source_path: str
    path: str
    reasons: tuple[DriftMatchReason, ...]
    relationship_status: str = "candidate only"
    verified: bool = False


@dataclass(frozen=True, slots=True)
class DriftSourceMatch:
    """Candidate and selection state for one available changed source."""

    source_path: str
    candidates: tuple[DriftCandidate, ...]
    selected_path: str | None
    ambiguous: bool
    relationship_status: str


@dataclass(frozen=True, slots=True)
class DriftMatchResult:
    """I-04 output contract for finding modeling and report orchestration."""

    source_matches: tuple[DriftSourceMatch, ...]
    candidates: tuple[DriftCandidate, ...]
    candidate_paths: tuple[str, ...]
    selected_path: str | None
    selected_candidates: tuple[DriftCandidate, ...]
    selection_status: str
    detail: str
    report_permitted: bool
    patch_permitted: bool = False
    fatal_evidence: bool = False

    @property
    def candidate_only(self) -> bool:
        return bool(self.candidates) and all(
            candidate.relationship_status == "candidate only"
            and not candidate.verified
            for candidate in self.candidates
        )


def _source_signals(source: DriftEvidenceItem) -> tuple[tuple[str, str], ...]:
    """Extract the approved source-stem, symbol, and key signals."""
    content = source.content or ""
    suffix = PurePosixPath(source.path).suffix
    signals: set[tuple[str, str]] = {
        (PurePosixPath(source.path).stem, "source_stem")
    }

    if suffix == ".py":
        for match in _PYTHON_DECLARATION.finditer(content):
            name = match.group(1)
            if not keyword.iskeyword(name):
                signals.add((name, "symbol"))
    elif suffix == ".json":
        signals.update(
            (match.group(1), "config_key")
            for match in _JSON_KEY.finditer(content)
        )
    elif suffix == ".toml":
        for match in _TOML_KEY.finditer(content):
            key = next(value for value in match.groups() if value is not None)
            signals.add((key, "config_key"))

    return tuple(sorted(signals))


def _available(
    items: tuple[DriftEvidenceItem, ...],
    kind: str,
) -> tuple[DriftEvidenceItem, ...]:
    return tuple(
        item
        for item in items
        if (
            item.kind == kind
            and item.status == "available"
            and item.content is not None
        )
    )


def _candidate_reasons(
    source: DriftEvidenceItem,
    document: DriftEvidenceItem,
) -> tuple[DriftMatchReason, ...]:
    source_path = source.path
    document_path = document.path
    markdown = document.content or ""
    reasons: set[DriftMatchReason] = set()

    for line in _signal_lines(markdown, source_path, "source_path"):
        reasons.add(DriftMatchReason("source_path", source_path, line))

    for value, signal in _source_signals(source):
        helper_signal = "symbol" if signal == "source_stem" else signal
        for line in _signal_lines(markdown, value, helper_signal):
            reasons.add(DriftMatchReason(signal, value, line))

    source_parent = PurePosixPath(source_path).parent.as_posix()
    document_parent = PurePosixPath(document_path).parent.as_posix()
    if source_parent == document_parent:
        reasons.add(DriftMatchReason("same_immediate_parent", source_parent, None))

    return tuple(
        sorted(
            reasons,
            key=lambda reason: (
                reason.signal,
                reason.value,
                reason.evidence_line is None,
                reason.evidence_line or 0,
            ),
        )
    )


def match_drift_candidates(
    evidence: DriftEvidenceCollection,
    *,
    target: str | None = None,
) -> DriftMatchResult:
    """Match only available evidence and apply G-02 candidate selection.

    This function does not render or publish a report. A selection that blocks
    publication is surfaced in ``report_permitted`` for the later pipeline.
    """
    if evidence.fatal:
        return DriftMatchResult(
            (),
            (),
            (),
            None,
            (),
            "fatal_evidence",
            evidence.sanitized_detail,
            False,
            fatal_evidence=True,
        )

    sources = _available(evidence.sources, "source")
    if evidence.markdown_inventory_truncated:
        source_matches = tuple(
            DriftSourceMatch(
                source_path=source.path,
                candidates=(),
                selected_path=None,
                ambiguous=False,
                relationship_status="insufficient evidence",
            )
            for source in sources
        )
        return DriftMatchResult(
            source_matches,
            (),
            (),
            None,
            (),
            "inconclusive",
            "Markdown inventory is incomplete; candidate selection was withheld.",
            True,
        )

    documents = _available(evidence.markdown, "markdown")
    source_candidates: list[tuple[str, tuple[DriftCandidate, ...]]] = []
    all_candidates: list[DriftCandidate] = []

    for source in sources:
        matched: list[DriftCandidate] = []
        for document in documents:
            reasons = _candidate_reasons(source, document)
            if reasons:
                matched.append(DriftCandidate(source.path, document.path, reasons))
        matched.sort(key=lambda candidate: candidate.path)
        source_candidates.append((source.path, tuple(matched)))
        all_candidates.extend(matched)

    candidates = tuple(
        sorted(
            all_candidates,
            key=lambda candidate: (candidate.source_path, candidate.path),
        )
    )
    candidate_paths = tuple(sorted({candidate.path for candidate in candidates}))

    selected_path: str | None = None
    if target is not None:
        if target not in candidate_paths:
            selection_status = "invalid_target"
            detail = (
                "The requested --target is not a candidate. Valid candidates: "
                + (", ".join(candidate_paths) if candidate_paths else "none")
                + "."
            )
            report_permitted = False
        else:
            selected_path = target
            selection_status = "selected"
            detail = "The explicit target is selected as a candidate only."
            report_permitted = True
    elif not candidate_paths:
        selection_status = "no_relationship"
        detail = "No relationship established: no candidate signal matched."
        report_permitted = True
    elif len(candidate_paths) == 1:
        selected_path = candidate_paths[0]
        selection_status = "selected"
        detail = "The sole candidate is selected as a candidate only."
        report_permitted = True
    else:
        selection_status = "ambiguous"
        detail = (
            "Multiple documentation candidates require an explicit --target: "
            + ", ".join(candidate_paths)
            + "."
        )
        report_permitted = False

    selected_candidates = tuple(
        candidate for candidate in candidates if candidate.path == selected_path
    )
    source_matches = tuple(
        DriftSourceMatch(
            source_path=source_path,
            candidates=source_items,
            selected_path=(
                selected_path
                if any(item.path == selected_path for item in source_items)
                else None
            ),
            ambiguous=len({item.path for item in source_items}) > 1,
            relationship_status=(
                "no relationship established"
                if not source_items
                else "candidate only"
            ),
        )
        for source_path, source_items in source_candidates
    )
    return DriftMatchResult(
        source_matches,
        candidates,
        candidate_paths,
        selected_path,
        selected_candidates,
        selection_status,
        detail,
        report_permitted,
    )
