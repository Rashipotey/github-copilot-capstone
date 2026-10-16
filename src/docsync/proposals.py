from __future__ import annotations

import ast
import io
import json
import math
import re
import tokenize
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from markdown_it import MarkdownIt

from docsync.evidence import (
    MarkdownDocument,
    MarkdownInventory,
    ScreenedSnapshot,
    ScreenedSnapshots,
)
from docsync.git import SnapshotSideState
from docsync.models import StageStatus
from docsync.screening import SecretScanner
from docsync.targets import SelectedTarget, TargetSelection

_REDACTED = "[REDACTED]"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TABLE_SEPARATOR = re.compile(r":?-{3,}:?\Z")


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    UNCERTAIN = "uncertain"
    NOT_FOUND = "not_found"


class ProposalKind(StrEnum):
    PYTHON_SIGNATURE = "python_signature"
    CONFIG_SCALAR = "config_scalar"


@dataclass(frozen=True, slots=True)
class ProposalProvenance:
    source_path: str
    source_line: int
    diff_hunk: str | None
    diff_line: int | None
    target_path: str
    target_line: int
    replacement_kind: str
    old_value: str
    new_value: str


@dataclass(frozen=True, slots=True)
class ProposalCandidate:
    kind: ProposalKind
    target_path: str
    replacement: str
    source_paths: tuple[str, ...]
    sanitized_reason: str
    provenance: ProposalProvenance


@dataclass(frozen=True, slots=True)
class ProposalResult:
    status: ProposalStatus
    proposal: ProposalCandidate | None
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class _Callable:
    qualified_name: tuple[str, ...]
    node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class _Header:
    text: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class _DiffLineEvidence:
    removed: frozenset[int]
    added: frozenset[int]
    hunk_headers: tuple[tuple[int, int, int, int, str, int], ...]


@dataclass(frozen=True, slots=True)
class _VerifiedDiffHunk:
    header: str
    diff_line: int
    removed: tuple[int, ...]
    added: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Cell:
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Fence:
    body: str
    source_offsets: tuple[int, ...]
    line_prefixes: tuple[str, ...]
    newline: str


def _result(status: ProposalStatus, detail: str) -> ProposalResult:
    return ProposalResult(status, None, detail)


def _screened_equal(scanner: SecretScanner, value: str, *, provenance: str) -> bool:
    try:
        result = scanner.screen_text(value, provenance=provenance, reject_detected=True)
    except Exception:
        return False
    return bool(
        result is not None
        and result.usable
        and result.sanitized_text == value
        and result.provenance is not None
    )


def _validate_inputs(
    snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
    selection: TargetSelection,
    scanner: SecretScanner,
) -> tuple[ScreenedSnapshot, SelectedTarget, MarkdownDocument] | ProposalResult:
    if not snapshots.complete or snapshots.status is not StageStatus.COMPLETED:
        return _result(ProposalStatus.UNCERTAIN, "Screened source snapshots are incomplete.")
    if not inventory.complete or inventory.status is not StageStatus.COMPLETED:
        return _result(ProposalStatus.UNCERTAIN, "Screened Markdown inventory is incomplete.")
    if snapshots.diff_text is None:
        return _result(ProposalStatus.UNCERTAIN, "Screened diff evidence is unavailable.")
    if (
        not selection.complete
        or selection.status is not StageStatus.COMPLETED
        or not selection.proposal_eligible
    ):
        if selection.no_target:
            return _result(ProposalStatus.NOT_FOUND, "No eligible existing documentation target was found.")
        return _result(ProposalStatus.UNCERTAIN, "Target evidence is incomplete, ambiguous, or conflicting.")
    if selection.ambiguities or selection.conflicts:
        return _result(ProposalStatus.UNCERTAIN, "Target evidence is ambiguous or conflicting.")

    sources = [
        snapshot
        for snapshot in snapshots.snapshots
        if not snapshot.binary
        and PurePosixPath(snapshot.path).suffix in {".py", ".json", ".toml"}
    ]
    if len(sources) != 1:
        return _result(ProposalStatus.UNCERTAIN, "Exactly one supported changed source input is required.")
    snapshot = sources[0]
    if (
        snapshot.old.state is not SnapshotSideState.PRESENT
        or snapshot.new.state is not SnapshotSideState.PRESENT
        or snapshot.old.text is None
        or snapshot.new.text is None
    ):
        return _result(ProposalStatus.UNCERTAIN, "Both screened source versions are required.")
    if _REDACTED in snapshot.old.text or _REDACTED in snapshot.new.text:
        return _result(ProposalStatus.UNCERTAIN, "Redacted source evidence is insufficient for a proposal.")
    if not _screened_equal(scanner, snapshot.path, provenance="proposal source path"):
        return _result(ProposalStatus.UNCERTAIN, "Source provenance screening was incomplete.")

    matching_targets = [target for target in selection.targets if snapshot.path in target.source_paths]
    if len(matching_targets) != 1:
        if not matching_targets:
            return _result(ProposalStatus.NOT_FOUND, "No unique target matches the changed source evidence.")
        return _result(ProposalStatus.UNCERTAIN, "More than one target matches the changed source evidence.")
    target = matching_targets[0]
    if not _screened_equal(scanner, target.path, provenance="proposal target path"):
        return _result(ProposalStatus.UNCERTAIN, "Target provenance screening was incomplete.")
    documents = [document for document in inventory.documents if document.path == target.path]
    if len(documents) != 1:
        return _result(ProposalStatus.UNCERTAIN, "The selected target is not a unique existing Markdown document.")
    document = documents[0]
    if _REDACTED in document.text:
        return _result(ProposalStatus.UNCERTAIN, "Redacted target evidence is insufficient for a proposal.")
    if not _screened_equal(scanner, document.path, provenance="proposal target path"):
        return _result(ProposalStatus.UNCERTAIN, "Target provenance screening was incomplete.")
    if not selection.source_provenance or sum(
        provenance[0] == snapshot.path for provenance in selection.source_provenance
    ) != 1:
        return _result(ProposalStatus.UNCERTAIN, "Unique source provenance is unavailable.")
    return snapshot, target, document


def _parse_diff_evidence(diff_text: str, path: str) -> _DiffLineEvidence | None:
    removed: set[int] = set()
    added: set[int] = set()
    hunk_headers: list[tuple[int, int, int, int, str, int]] = []
    in_file = False
    old_line = new_line = 0
    active_hunk = False

    for diff_line, line in enumerate(diff_text.splitlines(), 1):
        if line.startswith("diff --git "):
            in_file = False
            active_hunk = False
            continue
        if line.startswith("+++ "):
            in_file = line == f"+++ b/{path}"
            active_hunk = False
            continue
        if not in_file:
            continue
        match = _HUNK.match(line)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            hunk = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
            hunk_headers.append((old_start, old_count, new_start, new_count, hunk, diff_line))
            old_line, new_line = old_start, new_start
            active_hunk = True
            continue
        if not active_hunk or not line:
            continue
        marker = line[0]
        if marker == " ":
            old_line += 1
            new_line += 1
        elif marker == "-":
            removed.add(old_line)
            old_line += 1
        elif marker == "+":
            added.add(new_line)
            new_line += 1
        elif marker != "\\":
            active_hunk = False

    if not hunk_headers:
        return None
    return _DiffLineEvidence(frozenset(removed), frozenset(added), tuple(hunk_headers))


def _verified_python_hunks(
    diff_text: str,
    path: str,
    old_source: str,
    new_source: str,
) -> tuple[_VerifiedDiffHunk, ...] | None:
    old_lines, new_lines = old_source.splitlines(), new_source.splitlines()
    raw_hunks: list[tuple[int, int, int, int, str, int, list[tuple[str, str]]]] = []
    target_sections = 0
    in_target = False
    active: tuple[int, int, int, int, str, int, list[tuple[str, str]]] | None = None
    malformed = False

    def finish_hunk() -> None:
        nonlocal active, malformed
        if active is None:
            return
        old_start, old_count, new_start, new_count, _header, _diff_line, lines = active
        old_consumed = sum(marker != "+" for marker, _ in lines)
        new_consumed = sum(marker != "-" for marker, _ in lines)
        if old_consumed != old_count or new_consumed != new_count:
            malformed = True
        raw_hunks.append(active)
        active = None

    for number, line in enumerate(diff_text.splitlines(), 1):
        if line.startswith("diff --git "):
            finish_hunk()
            in_target = False
            continue
        if line.startswith("@@ ") and in_target:
            finish_hunk()
            match = _HUNK.match(line)
            if match is None:
                malformed = True
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
            active = (old_start, old_count, new_start, new_count, header, number, [])
            continue
        if active is not None and in_target:
            if line.startswith("\\"):
                continue
            if line and line[0] in " +-":
                active[6].append((line[0], line[1:]))
            else:
                malformed = True
            continue
        if line.startswith("+++ "):
            in_target = line == f"+++ b/{path}"
            if in_target:
                target_sections += 1

    finish_hunk()
    if malformed or target_sections != 1 or not raw_hunks:
        return None

    verified: list[_VerifiedDiffHunk] = []
    old_cursor = new_cursor = 0
    for old_start, old_count, new_start, new_count, header, diff_line, lines in raw_hunks:
        old_before = old_start if old_count == 0 else old_start - 1
        new_before = new_start if new_count == 0 else new_start - 1
        if (
            old_before < old_cursor
            or new_before < new_cursor
            or old_lines[old_cursor:old_before] != new_lines[new_cursor:new_before]
        ):
            return None
        old_cursor, new_cursor = old_before, new_before
        removed: list[int] = []
        added: list[int] = []
        for marker, text in lines:
            if marker != "+":
                if old_cursor >= len(old_lines) or old_lines[old_cursor] != text:
                    return None
                if marker == "-":
                    removed.append(old_cursor + 1)
                old_cursor += 1
            if marker != "-":
                if new_cursor >= len(new_lines) or new_lines[new_cursor] != text:
                    return None
                if marker == "+":
                    added.append(new_cursor + 1)
                new_cursor += 1
        if not removed and not added:
            return None
        verified.append(_VerifiedDiffHunk(header, diff_line, tuple(removed), tuple(added)))

    if old_lines[old_cursor:] != new_lines[new_cursor:]:
        return None
    return tuple(verified)


def _hunk_for_lines(
    diff: _DiffLineEvidence,
    old_lines: range,
    new_lines: range,
) -> tuple[str, int] | None:
    old_hits = diff.removed.intersection(old_lines)
    new_hits = diff.added.intersection(new_lines)
    if not old_hits or not new_hits:
        return None
    for old_start, old_count, new_start, new_count, header, diff_line in diff.hunk_headers:
        old_end = old_start + max(old_count - 1, 0)
        new_end = new_start + max(new_count - 1, 0)
        if any(old_start <= line <= old_end for line in old_hits) and any(
            new_start <= line <= new_end for line in new_hits
        ):
            return header, diff_line
    return None


def _walk_statements(statements: list[ast.stmt], scope: tuple[str, ...]):
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _Callable((*scope, statement.name), statement)
            yield from _walk_statements(statement.body, (*scope, statement.name))
        elif isinstance(statement, ast.ClassDef):
            yield from _walk_statements(statement.body, (*scope, statement.name))
        else:
            for _field, value in ast.iter_fields(statement):
                if isinstance(value, list):
                    yield from _walk_statements(
                        [item for item in value if isinstance(item, ast.stmt)], scope
                    )
                elif isinstance(value, ast.stmt):
                    yield from _walk_statements([value], scope)


def _signature_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[Any, ...]:
    return (
        isinstance(node, ast.AsyncFunctionDef),
        ast.dump(node.args, include_attributes=False),
        ast.dump(node.returns, include_attributes=False) if node.returns is not None else None,
        node.type_comment,
        tuple(ast.dump(item, include_attributes=False) for item in getattr(node, "type_params", ())),
    )


def _body_shape(node: ast.AST) -> Any:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (
            "callable",
            node.name,
            tuple(_body_shape(item) for item in node.decorator_list),
            tuple(_body_shape(item) for item in node.body),
        )
    if isinstance(node, ast.AST):
        return (
            type(node).__name__,
            tuple((name, _body_shape(value)) for name, value in ast.iter_fields(node)),
        )
    if isinstance(node, list):
        return tuple(_body_shape(item) for item in node)
    return node


def _line_offsets(source: str) -> tuple[list[str], list[int]]:
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    if not lines:
        lines, offsets = [""], [0]
    return lines, offsets


def _extract_header(
    source: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> _Header | None:
    lines, offsets = _line_offsets(source)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    expected = "async" if isinstance(node, ast.AsyncFunctionDef) else "def"
    start_index = next(
        (
            index
            for index, item in enumerate(tokens)
            if item.type == tokenize.NAME
            and item.string == expected
            and item.start[0] == node.lineno
        ),
        None,
    )
    if start_index is None:
        return None
    start = tokens[start_index].start
    depth = 0
    pending_lambda = 0
    colon: tokenize.TokenInfo | None = None
    for item in tokens[start_index + 1 :]:
        if item.type == tokenize.OP:
            if item.string in "([{":
                depth += 1
            elif item.string in ")]}":
                depth -= 1
            elif item.string == ":" and depth == 0:
                if pending_lambda:
                    pending_lambda -= 1
                else:
                    colon = item
                    break
        elif item.type == tokenize.NAME and item.string == "lambda" and depth == 0:
            pending_lambda += 1
    if colon is None:
        return None
    start_offset = offsets[start[0] - 1] + start[1]
    end_offset = offsets[colon.end[0] - 1] + colon.end[1]
    return _Header(source[start_offset:end_offset], start[0], colon.end[0])


def _python_change(snapshot: ScreenedSnapshot):
    old_source, new_source = snapshot.old.text, snapshot.new.text
    try:
        old_tree = ast.parse(old_source, filename=snapshot.path, type_comments=True)
        new_tree = ast.parse(new_source, filename=snapshot.path, type_comments=True)
    except (SyntaxError, ValueError, TypeError):
        return None, "Python source could not be parsed safely."
    old_callables = list(_walk_statements(old_tree.body, ()))
    new_callables = list(_walk_statements(new_tree.body, ()))
    old_by_name: dict[tuple[str, ...], list[_Callable]] = {}
    new_by_name: dict[tuple[str, ...], list[_Callable]] = {}
    for item in old_callables:
        old_by_name.setdefault(item.qualified_name, []).append(item)
    for item in new_callables:
        new_by_name.setdefault(item.qualified_name, []).append(item)
    if set(old_by_name) != set(new_by_name) or any(
        len(items) != 1 for items in (*old_by_name.values(), *new_by_name.values())
    ):
        return None, "Python callable additions, deletions, or duplicate qualifications are unsupported."
    changed = [
        name
        for name in old_by_name
        if _signature_shape(old_by_name[name][0].node)
        != _signature_shape(new_by_name[name][0].node)
    ]
    if len(changed) != 1:
        return None, "Exactly one existing qualified Python callable must change signature."
    if _body_shape(old_tree) != _body_shape(new_tree):
        return None, "Python changes outside the callable signature are unsupported."
    qualified_name = changed[0]
    old_node = old_by_name[qualified_name][0].node
    new_node = new_by_name[qualified_name][0].node
    old_header = _extract_header(old_source, old_node)
    new_header = _extract_header(new_source, new_node)
    if old_header is None or new_header is None or old_header.text == new_header.text:
        return None, "Complete Python signature headers could not be established."
    return (qualified_name, old_node, new_node, old_header, new_header), ""


def _fence_blocks(document: str) -> tuple[_Fence, ...] | None:
    lines, offsets = _line_offsets(document)
    try:
        tokens = MarkdownIt("commonmark").parse(document)
    except Exception:
        return None
    fences: list[_Fence] = []
    for token in tokens:
        if token.type != "fence" or token.map is None:
            continue
        start_line, end_line = token.map
        opening = lines[start_line].rstrip("\r\n")
        if not token.markup or opening.find(token.markup) < 0:
            return None
        marker = token.markup
        closing_line = end_line - 1
        close_text = lines[closing_line].rstrip("\r\n") if closing_line > start_line else ""
        close_match = re.search(r"(?P<marker>`{3,}|~{3,})[ \t]*\Z", close_text)
        has_close = bool(
            close_match
            and close_match.group("marker")[0] == marker[0]
            and len(close_match.group("marker")) >= len(marker)
        )
        body_end = closing_line if has_close else end_line
        body_lines = token.content.splitlines(keepends=True)
        raw_body_lines = lines[start_line + 1 : body_end]
        if len(body_lines) != len(raw_body_lines):
            return None
        source_offsets: list[int] = []
        line_prefixes: list[str] = []
        detected_newline = "\n"
        for relative_index, (raw_line, token_line) in enumerate(zip(raw_body_lines, body_lines)):
            line_index = start_line + 1 + relative_index
            if raw_line.endswith("\r\n"):
                content_end = len(raw_line) - 2
                detected_newline = "\r\n"
            elif raw_line.endswith(("\n", "\r")):
                content_end = len(raw_line) - 1
                detected_newline = raw_line[content_end:]
            else:
                content_end = len(raw_line)
            raw_content = raw_line[:content_end]
            token_content = token_line.rstrip("\r\n")
            if token_content:
                content_start = raw_content.find(token_content)
                if content_start < 0:
                    return None
            else:
                content_start = len(raw_content)
            line_prefixes.append(raw_content[:content_start])
            source_offsets.extend(
                range(
                    offsets[line_index] + content_start,
                    offsets[line_index] + content_start + len(token_content),
                )
            )
            if token_line.endswith(("\n", "\r")):
                source_offsets.append(offsets[line_index] + content_end)
        if len(source_offsets) != len(token.content):
            return None
        fences.append(_Fence(token.content, tuple(source_offsets), tuple(line_prefixes), detected_newline))
    return tuple(fences)


def _find_fenced_signature(document: str, old_header: str):
    fences = _fence_blocks(document)
    if fences is None:
        return None, "Markdown fence mapping could not be established safely."
    needle = old_header.replace("\r\n", "\n").replace("\r", "\n")
    hits: list[tuple[_Fence, int, int]] = []
    for fence in fences:
        start = 0
        while True:
            index = fence.body.find(needle, start)
            if index < 0:
                break
            before = fence.body[index - 1] if index else "\n"
            end = index + len(needle)
            after = fence.body[end] if end < len(fence.body) else "\n"
            line_start = fence.body.rfind("\n", 0, index) + 1
            line_prefix = fence.body[line_start:index]
            if before == "\n" or (line_prefix and not line_prefix.strip()):
                if after in "\n#" or (after == " " and fence.body[end:].lstrip().startswith("#")):
                    hits.append((fence, index, end))
            start = index + 1
    if not hits:
        return None, "The complete old Python signature was not found in a Markdown fence."
    if len(hits) > 1:
        return None, "The complete old Python signature is duplicated in Markdown fences."
    fence, start, end = hits[0]
    if not fence.source_offsets:
        return None, "The Markdown signature anchor could not be mapped to source text."
    raw_start, raw_end = fence.source_offsets[start], fence.source_offsets[end - 1] + 1
    line_number = document.count("\n", 0, raw_start) + 1
    return (fence, raw_start, raw_end, line_number), ""


def _python_proposal(
    snapshot: ScreenedSnapshot,
    target: SelectedTarget,
    document: MarkdownDocument,
    diff_text: str,
) -> tuple[ProposalCandidate | None, str, ProposalStatus]:
    change, detail = _python_change(snapshot)
    if change is None:
        return None, detail, ProposalStatus.UNCERTAIN
    _qualified, _old_node, new_node, old_header, new_header = change
    hunks = _verified_python_hunks(
        diff_text, snapshot.path, snapshot.old.text or "", snapshot.new.text or ""
    )
    if hunks is None:
        return None, "The screened diff does not support the signature replacement.", ProposalStatus.UNCERTAIN
    old_header_lines = range(old_header.start_line, old_header.end_line + 1)
    new_header_lines = range(new_header.start_line, new_header.end_line + 1)
    if any(line not in old_header_lines for hunk in hunks for line in hunk.removed) or any(
        line not in new_header_lines for hunk in hunks for line in hunk.added
    ):
        return None, "The screened diff changes lines outside the Python signature header.", ProposalStatus.UNCERTAIN
    hunk_header, diff_line = hunks[0].header, hunks[0].diff_line
    anchor, detail = _find_fenced_signature(document.text, old_header.text)
    if anchor is None:
        status = ProposalStatus.NOT_FOUND if "not found" in detail else ProposalStatus.UNCERTAIN
        return None, detail, status
    fence, start, end, target_line = anchor
    if target_line not in target.anchor_lines:
        return None, "The fenced signature does not match the selected target anchor.", ProposalStatus.UNCERTAIN
    new_normalized = new_header.text.replace("\r\n", "\n").replace("\r", "\n")
    line_index = fence.body.count("\n", 0, fence.body.find(old_header.text.replace("\r\n", "\n").replace("\r", "\n")))
    prefix = fence.line_prefixes[line_index] if line_index < len(fence.line_prefixes) else ""
    replacement_header = new_normalized.replace("\n", fence.newline + prefix)
    replacement = document.text[:start] + replacement_header + document.text[end:]
    provenance = ProposalProvenance(
        snapshot.path,
        new_node.lineno,
        hunk_header,
        diff_line,
        target.path,
        target_line,
        ProposalKind.PYTHON_SIGNATURE.value,
        old_header.text,
        new_header.text,
    )
    return (
        ProposalCandidate(
            ProposalKind.PYTHON_SIGNATURE,
            target.path,
            replacement,
            (snapshot.path,),
            "Replace one uniquely anchored Python callable signature.",
            provenance,
        ),
        "",
        ProposalStatus.PROPOSED,
    )


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + item.replace("~", "~0").replace("/", "~1") for item in parts)


def _canonical_scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (str, bool, int)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if isinstance(value, float) and math.isfinite(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return None


def _markdown_cell_scalar(value: str) -> str:
    return value.replace("|", r"\|")


def _flatten_config(value: Any):
    if not isinstance(value, dict):
        return None
    scalars: dict[str, tuple[Any, str]] = {}
    nodes: set[str] = {""}

    def visit(item: Any, parts: tuple[str, ...]) -> bool:
        if isinstance(item, dict):
            path = _pointer(parts)
            if parts:
                nodes.add(path)
            for key, child in item.items():
                if not isinstance(key, str):
                    return False
                child_parts = (*parts, key)
                child_path = _pointer(child_parts)
                nodes.add(child_path)
                if isinstance(child, dict):
                    if not visit(child, child_parts):
                        return False
                else:
                    canonical = _canonical_scalar(child)
                    if canonical is None:
                        return False
                    scalars[child_path] = (child, canonical)
            return True
        return False

    if not visit(value, ()):
        return None
    return scalars, nodes


def _config_change(snapshot: ScreenedSnapshot):
    old_text, new_text = snapshot.old.text, snapshot.new.text
    try:
        if PurePosixPath(snapshot.path).suffix == ".json":
            old_value = json.loads(old_text, object_pairs_hook=_json_object, parse_constant=lambda _v: math.nan)
            new_value = json.loads(new_text, object_pairs_hook=_json_object, parse_constant=lambda _v: math.nan)
        else:
            old_value = tomllib.loads(old_text)
            new_value = tomllib.loads(new_text)
    except Exception:
        return None, "Configuration source could not be parsed safely."
    old_flat, new_flat = _flatten_config(old_value), _flatten_config(new_value)
    if old_flat is None or new_flat is None:
        return None, "Only nested tables and supported scalar configuration values are allowed."
    old_scalars, old_nodes = old_flat
    new_scalars, new_nodes = new_flat
    if old_nodes != new_nodes or old_scalars.keys() != new_scalars.keys():
        return None, "Configuration additions and deletions are unsupported."
    changed = [
        path for path in old_scalars
        if old_scalars[path][1] != new_scalars[path][1]
    ]
    if len(changed) != 1:
        return None, "Exactly one existing configuration scalar must change."
    path = changed[0]
    return (path, old_scalars[path][1], new_scalars[path][1]), ""


def _split_pipe_row(line: str) -> tuple[_Cell, ...] | None:
    content = line.rstrip("\r\n")
    separators: list[int] = []
    for index, character in enumerate(content):
        if character != "|":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and content[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            separators.append(index)
    if not separators:
        return None
    boundaries = [-1, *separators, len(content)]
    cells = [
        _Cell(content[start + 1 : end], start + 1, end)
        for start, end in zip(boundaries, boundaries[1:])
    ]
    if content[:1] == "|":
        cells = cells[1:]
    if content[-1:] == "|":
        cells = cells[:-1]
    return tuple(cells)


def _settings_rows(document: str, path: str):
    lines, offsets = _line_offsets(document)
    candidates: list[tuple[int, _Cell]] = []
    index = 0
    while index + 1 < len(lines):
        header = _split_pipe_row(lines[index])
        separator = _split_pipe_row(lines[index + 1])
        if header is None or separator is None or len(header) != len(separator):
            index += 1
            continue
        if not all(_TABLE_SEPARATOR.fullmatch(cell.text.strip()) for cell in separator):
            index += 1
            continue
        header_names = [cell.text.strip().casefold() for cell in header]
        key_columns = [i for i, name in enumerate(header_names) if name in {"key", "path"}]
        value_columns = [i for i, name in enumerate(header_names) if name in {"value", "current"}]
        if len(key_columns) != 1 or len(value_columns) != 1:
            index += 1
            continue
        key_column, value_column = key_columns[0], value_columns[0]
        row = index + 2
        while row < len(lines):
            cells = _split_pipe_row(lines[row])
            if cells is None or len(cells) != len(header):
                break
            key = cells[key_column].text.strip()
            if key == path:
                candidates.append((row, cells[value_column]))
            row += 1
        index = max(row, index + 1)
    return candidates, lines, offsets


def _config_anchor(document: str, path: str, old_value: str):
    rows, lines, offsets = _settings_rows(document, path)
    if not rows:
        return None, "No exact structured settings row was found.", ProposalStatus.NOT_FOUND
    if len(rows) != 1:
        return None, "The settings key has duplicate Markdown rows.", ProposalStatus.UNCERTAIN
    row_index, cell = rows[0]
    leading = len(cell.text) - len(cell.text.lstrip())
    trailing_end = len(cell.text.rstrip())
    value_start, value_end = cell.start + leading, cell.start + trailing_end
    if cell.text[leading:trailing_end] != _markdown_cell_scalar(old_value):
        return None, "The existing settings row does not contain the exact old canonical value.", ProposalStatus.UNCERTAIN
    absolute_start = offsets[row_index] + value_start
    absolute_end = offsets[row_index] + value_end
    return (absolute_start, absolute_end, row_index + 1), "", ProposalStatus.PROPOSED


def _config_proposal(
    snapshot: ScreenedSnapshot,
    target: SelectedTarget,
    document: MarkdownDocument,
    diff_text: str,
) -> tuple[ProposalCandidate | None, str, ProposalStatus]:
    change, detail = _config_change(snapshot)
    if change is None:
        return None, detail, ProposalStatus.UNCERTAIN
    path, old_value, new_value = change
    diff = _parse_diff_evidence(diff_text, snapshot.path)
    old_line = _changed_config_line(snapshot.old.text, path)
    new_line = _changed_config_line(snapshot.new.text, path)
    if diff is None or old_line is None or new_line is None:
        return None, "A complete configuration diff hunk is unavailable.", ProposalStatus.UNCERTAIN
    hunk = _config_diff_hunk(diff, old_line, new_line)
    if hunk is None:
        return None, "The screened diff does not support the changed configuration path.", ProposalStatus.UNCERTAIN
    hunk_header, diff_line = hunk
    anchor, detail, status = _config_anchor(document.text, path, old_value)
    if anchor is None:
        return None, detail, status
    start, end, target_line = anchor
    if target_line not in target.anchor_lines:
        return None, "The settings row does not match the selected target anchor.", ProposalStatus.UNCERTAIN
    replacement = document.text[:start] + _markdown_cell_scalar(new_value) + document.text[end:]
    provenance = ProposalProvenance(
        snapshot.path,
        new_line,
        hunk_header,
        diff_line,
        target.path,
        target_line,
        ProposalKind.CONFIG_SCALAR.value,
        old_value,
        new_value,
    )
    return (
        ProposalCandidate(
            ProposalKind.CONFIG_SCALAR,
            target.path,
            replacement,
            (snapshot.path,),
            "Replace one uniquely anchored configuration scalar value.",
            provenance,
        ),
        "",
        ProposalStatus.PROPOSED,
    )


def _changed_config_line(source: str, pointer: str) -> int | None:
    # A unique leaf-key line is retained as source provenance; ambiguity withholds the proposal.
    parts = _decode_pointer(pointer)
    leaf = parts[-1] if parts else ""
    matches = [
        number
        for number, line in enumerate(source.splitlines(), 1)
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(leaf)}(?![A-Za-z0-9_])", line)
    ]
    return matches[0] if len(matches) == 1 else None


def _config_diff_hunk(
    diff: _DiffLineEvidence, old_line: int, new_line: int
) -> tuple[str, int] | None:
    for old_start, old_count, new_start, new_count, header, diff_line in diff.hunk_headers:
        old_end = old_start + max(old_count - 1, 0)
        new_end = new_start + max(new_count - 1, 0)
        if (
            old_start <= old_line <= old_end
            and new_start <= new_line <= new_end
            and old_line in diff.removed
            and new_line in diff.added
        ):
            return header, diff_line
    return None


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer:
        return ()
    return tuple(
        re.sub(r"~[01]", lambda match: "~" if match.group() == "~0" else "/", part)
        for part in pointer[1:].split("/")
    )


def _proposal_is_safe(proposal: ProposalCandidate, scanner: SecretScanner) -> bool:
    if not _screened_equal(scanner, proposal.replacement, provenance="proposal replacement"):
        return False
    provenance = proposal.provenance
    for value in (
        provenance.source_path,
        provenance.target_path,
        provenance.replacement_kind,
        provenance.old_value,
        provenance.new_value,
        provenance.diff_hunk or "",
    ):
        if value and not _screened_equal(scanner, value, provenance="proposal provenance"):
            return False
    return True


def propose_deterministic(
    snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
    selection: TargetSelection,
    *,
    scanner: SecretScanner | None = None,
) -> ProposalResult:
    """Build one screened direct-replacement candidate from the deterministic allowlist."""
    scanner = scanner or SecretScanner()
    try:
        validated = _validate_inputs(snapshots, inventory, selection, scanner)
        if isinstance(validated, ProposalResult):
            return validated
        snapshot, target, document = validated
        suffix = PurePosixPath(snapshot.path).suffix
        if suffix == ".py":
            proposal, detail, status = _python_proposal(
                snapshot, target, document, snapshots.diff_text or ""
            )
        elif suffix in {".json", ".toml"}:
            proposal, detail, status = _config_proposal(
                snapshot, target, document, snapshots.diff_text or ""
            )
        else:
            return _result(ProposalStatus.UNCERTAIN, "Changed source format is not allowlisted.")
        if proposal is None:
            return _result(status, detail)
        if not _proposal_is_safe(proposal, scanner):
            return _result(ProposalStatus.UNCERTAIN, "Proposal screening was incomplete or detected sensitive content.")
        return ProposalResult(
            ProposalStatus.PROPOSED,
            proposal,
            "One evidence-backed replacement candidate is available for human review.",
        )
    except Exception:
        return _result(ProposalStatus.UNCERTAIN, "Proposal analysis failed safely; no candidate was produced.")