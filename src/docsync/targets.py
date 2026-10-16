from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from docsync.evidence import MarkdownInventory, ScreenedSnapshot, ScreenedSnapshots
from docsync.models import SkippedInput, StageStatus
from docsync.screening import SecretScanner


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    path: str
    source_paths: tuple[str, ...]
    signal_types: tuple[str, ...]
    anchor_lines: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SelectedTarget:
    path: str
    source_paths: tuple[str, ...]
    anchor_lines: tuple[int, ...]
    sanitized_reason: str


@dataclass(frozen=True, slots=True)
class TargetAmbiguity:
    source_path: str
    candidate_paths: tuple[str, ...]
    sanitized_reason: str


@dataclass(frozen=True, slots=True)
class TargetConflict:
    source_path: str
    candidate_paths: tuple[str, ...]
    sanitized_reason: str


@dataclass(frozen=True, slots=True)
class TargetSelection:
    targets: tuple[SelectedTarget, ...]
    candidates: tuple[TargetCandidate, ...]
    ambiguities: tuple[TargetAmbiguity, ...]
    conflicts: tuple[TargetConflict, ...]
    no_target: bool
    status: StageStatus
    complete: bool
    sanitized_detail: str
    skipped_inputs: tuple[SkippedInput, ...]
    source_provenance: tuple[tuple[str, str, str, str], ...] = ()

    @property
    def proposal_eligible(self) -> bool:
        return bool(self.complete and self.targets and not self.ambiguities and not self.conflicts)


_PYTHON_DECLARATION = re.compile(r"(?m)^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_JSON_KEY = re.compile(r'"([^"\r\n]+)"\s*:', re.MULTILINE)
_TOML_KEY = re.compile(r"(?m)^\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9_.-]+))\s*=")


def _source_signals(snapshot: ScreenedSnapshot) -> tuple[tuple[str, str], ...]:
    signals: set[tuple[str, str]] = {(snapshot.path, "source_path")}
    suffix = PurePosixPath(snapshot.path).suffix
    texts = (snapshot.old.text, snapshot.new.text)
    for text in texts:
        if text is None:
            continue
        if suffix == ".py":
            for match in _PYTHON_DECLARATION.finditer(text):
                name = match.group(1)
                if not keyword.iskeyword(name):
                    signals.add((name, "symbol"))
        elif suffix == ".json":
            signals.update((match.group(1), "config_key") for match in _JSON_KEY.finditer(text))
        elif suffix == ".toml":
            for match in _TOML_KEY.finditer(text):
                key = next(value for value in match.groups() if value is not None)
                signals.add((key, "config_key"))
    return tuple(sorted(signals))


def _signal_lines(text: str, value: str, signal_type: str) -> tuple[int, ...]:
    if signal_type == "source_path":
        return tuple(index for index, line in enumerate(text.splitlines(), 1) if value in line)
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])")
    return tuple(
        index
        for index, line in enumerate(text.splitlines(), 1)
        if pattern.search(line)
    )


def _safe_source_snapshots(snapshots: ScreenedSnapshots) -> tuple[ScreenedSnapshot, ...]:
    return tuple(
        snapshot
        for snapshot in snapshots.snapshots
        if not snapshot.binary and PurePosixPath(snapshot.path).suffix in {".py", ".json", ".toml"}
    )


def _screen_text_safely(scanner: SecretScanner, value: str, *, provenance: str):
    try:
        return scanner.screen_text(value, provenance=provenance)
    except Exception:
        return None


def select_targets(
    snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
    *,
    scanner: SecretScanner,
) -> TargetSelection:
    """Select only existing Markdown files with unambiguous screened evidence anchors."""
    skipped = (*snapshots.skipped_inputs, *inventory.skipped_inputs)
    if not snapshots.complete or not inventory.complete:
        return TargetSelection(
            (), (), (), (), False, StageStatus.INCONCLUSIVE, False,
            "Target selection withheld because required evidence is incomplete.", skipped,
        )

    source_provenance: list[tuple[str, str, str, str]] = []
    for snapshot in snapshots.snapshots:
        provenance_values = (
            snapshot.path,
            snapshot.scope.value,
            snapshot.old_endpoint,
            snapshot.new_endpoint,
        )
        screened_values = tuple(
            _screen_text_safely(scanner, value, provenance="snapshot provenance")
            for value in provenance_values
        )
        if any(
            result is None or not result.usable or result.sanitized_text != value
            for result, value in zip(screened_values, provenance_values)
        ):
            return TargetSelection(
                (), (), (), (), False, StageStatus.INCONCLUSIVE, False,
                "Source provenance screening was incomplete; target selection was withheld.", skipped,
            )
        source_provenance.append(provenance_values)

    candidate_sources: dict[str, set[str]] = {}
    candidate_types: dict[str, set[str]] = {}
    candidate_lines: dict[str, set[int]] = {}
    selected_sources: dict[str, set[str]] = {}
    selected_lines: dict[str, set[int]] = {}
    ambiguities: list[TargetAmbiguity] = []
    conflicts: list[TargetConflict] = []
    for snapshot in _safe_source_snapshots(snapshots):
        source_path = snapshot.path
        screened_source = _screen_text_safely(
            scanner, source_path, provenance="repository path"
        )
        if (
            not screened_source.usable
            or screened_source.sanitized_text is None
            or screened_source.sanitized_text != source_path
        ):
            return TargetSelection(
                (), (), (), (), False, StageStatus.INCONCLUSIVE, False,
                "Source provenance screening was incomplete; target selection was withheld.", skipped,
            )

        signal_hits: dict[tuple[str, str], dict[str, tuple[int, ...]]] = {}
        for value, signal_type in _source_signals(snapshot):
            screened_signal = _screen_text_safely(scanner, value, provenance=source_path)
            if (
                screened_signal is None
                or not screened_signal.usable
                or screened_signal.sanitized_text != value
            ):
                continue
            for document in inventory.documents:
                lines = _signal_lines(document.text, value, signal_type)
                if lines:
                    signal_hits.setdefault((value, signal_type), {})[document.path] = lines

        # A stem-only mention is a candidate, never a positive selection signal.
        if not any(signal_hits.values()):
            stem = PurePosixPath(source_path).stem
            if len(stem) > 1:
                for document in inventory.documents:
                    lines = _signal_lines(document.text, stem, "symbol")
                    if lines:
                        candidate_sources.setdefault(document.path, set()).add(source_path)
                        candidate_types.setdefault(document.path, set()).add("filename_stem")
                        candidate_lines.setdefault(document.path, set()).update(lines)
                        ambiguities.append(
                            TargetAmbiguity(
                                source_path,
                                (document.path,),
                                "Only a filename-stem mention was found; relevance is ambiguous.",
                            )
                        )
            continue

        source_ambiguous = False
        source_conflict = False
        for (value, signal_type), hits in signal_hits.items():
            for path, lines in hits.items():
                candidate_sources.setdefault(path, set()).add(source_path)
                candidate_types.setdefault(path, set()).add(signal_type)
                candidate_lines.setdefault(path, set()).update(lines)
            if len(hits) > 1:
                paths = tuple(sorted(hits))
                if signal_type == "source_path":
                    contexts = {
                        path: tuple(
                            inventory_doc.text.splitlines()[line - 1].strip()
                            for line in hits[path]
                        )
                        for path in paths
                        for inventory_doc in inventory.documents
                        if inventory_doc.path == path
                    }
                    if len({context for context in contexts.values()}) > 1:
                        conflicts.append(
                            TargetConflict(
                                source_path,
                                paths,
                                "The same changed path has conflicting existing Markdown anchors.",
                            )
                        )
                        source_conflict = True
                    else:
                        ambiguities.append(
                            TargetAmbiguity(
                                source_path,
                                paths,
                                "The same changed-path anchor occurs in multiple documents.",
                            )
                        )
                        source_ambiguous = True
                else:
                    ambiguities.append(
                        TargetAmbiguity(
                            source_path,
                            paths,
                            "The same source symbol or key occurs in multiple documents.",
                        )
                    )
                    source_ambiguous = True
            elif len(next(iter(hits.values()))) != 1:
                ambiguities.append(
                    TargetAmbiguity(
                        source_path,
                        tuple(hits),
                        "A relevant evidence anchor occurs more than once in one document.",
                    )
                )
                source_ambiguous = True

        if not source_ambiguous and not source_conflict:
            for path, lines in (
                (path, line_set)
                for (value, signal_type), hits in signal_hits.items()
                for path, hit_lines in hits.items()
                for line_set in (hit_lines,)
            ):
                selected_sources.setdefault(path, set()).add(source_path)
                selected_lines.setdefault(path, set()).update(lines)

    candidates = tuple(
        TargetCandidate(
            path,
            tuple(sorted(candidate_sources[path])),
            tuple(sorted(candidate_types[path])),
            tuple(sorted(candidate_lines[path])),
        )
        for path in sorted(candidate_sources)
    )
    targets = tuple(
        SelectedTarget(
            path,
            tuple(sorted(source_paths)),
            tuple(sorted(selected_lines.get(path, ()))),
            "Existing Markdown contains unique changed-source evidence.",
        )
        for path, source_paths in sorted(selected_sources.items())
    )
    conflicts_tuple = tuple(conflicts)
    ambiguities_tuple = tuple(ambiguities)
    if conflicts_tuple:
        status = StageStatus.WARNING
        detail = "Conflicting documentation anchors require human resolution."
    elif ambiguities_tuple:
        status = StageStatus.WARNING
        detail = "Ambiguous documentation candidates require human resolution."
    else:
        status = StageStatus.COMPLETED
        detail = "Documentation target selection completed."
    no_target = not targets and not candidates
    if no_target:
        detail = "No suitable existing documentation target was found."
    return TargetSelection(
        targets,
        candidates,
        ambiguities_tuple,
        conflicts_tuple,
        no_target,
        status,
        True,
        detail,
        skipped,
        tuple(source_provenance),
    )
