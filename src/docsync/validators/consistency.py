from __future__ import annotations

import ast
import json
import math
import re
import tokenize
import tomllib
from dataclasses import dataclass
from io import StringIO
from pathlib import PurePosixPath
from typing import Any

from markdown_it import MarkdownIt

from docsync.evidence import MarkdownInventory, ScreenedSnapshot, ScreenedSnapshots
from docsync.git import SnapshotSideState
from docsync.models import StageStatus, ValidatorResult, ValidatorStatus
from docsync.proposals import ProposalCandidate, ProposalKind, _verified_python_hunks
from docsync.screening import SecretScanner


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SEPARATOR = re.compile(r":?-{3,}:?\Z")


@dataclass(frozen=True, slots=True)
class _Hunk:
    header: str
    diff_line: int
    old_start: int
    new_start: int
    removed: tuple[tuple[int, str], ...]
    added: tuple[tuple[int, str], ...]


def _result(
    status: ValidatorStatus,
    detail: str,
    candidate: ProposalCandidate,
    scanner: SecretScanner,
) -> ValidatorResult:
    refs: list[str] = []
    for path in (candidate.provenance.source_path, candidate.provenance.target_path):
        try:
            screened = scanner.screen_repository_path(path)
        except Exception:
            return ValidatorResult(
                "consistency", ValidatorStatus.INCONCLUSIVE,
                "Consistency provenance paths could not be screened safely.",
            )
        if screened is None or not screened.usable or not screened.sanitized_text:
            return ValidatorResult(
                "consistency", ValidatorStatus.INCONCLUSIVE,
                "Consistency provenance paths could not be screened safely.",
            )
        refs.append(screened.sanitized_text)
    return ValidatorResult("consistency", status, detail, tuple(refs))


def _parse_hunks(diff_text: str, path: str) -> tuple[_Hunk, ...]:
    hunks: list[_Hunk] = []
    active_file = False
    old_line = new_line = 0
    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []
    header = ""
    header_line = 0

    def save() -> None:
        if header:
            hunks.append(_Hunk(header, header_line, old_start, new_start, tuple(removed), tuple(added)))

    old_start = new_start = 0
    for number, line in enumerate(diff_text.splitlines(), 1):
        if line.startswith("diff --git "):
            save()
            header = ""
            active_file = False
        elif line.startswith("+++ "):
            active_file = line == f"+++ b/{path}"
            header = ""
        elif not active_file:
            continue
        else:
            match = _HUNK.match(line)
            if match:
                save()
                old_start = int(match.group(1))
                new_start = int(match.group(3))
                old_count = int(match.group(2) or "1")
                new_count = int(match.group(4) or "1")
                header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
                header_line = number
                old_line, new_line = old_start, new_start
                removed, added = [], []
            elif header and line:
                marker, text = line[0], line[1:]
                if marker == "-":
                    removed.append((old_line, text))
                    old_line += 1
                elif marker == "+":
                    added.append((new_line, text))
                    new_line += 1
                elif marker == " ":
                    old_line += 1
                    new_line += 1
                elif marker != "\\":
                    header = ""
    save()
    return tuple(hunks)


def _line_at(source: str, line: int) -> str | None:
    lines = source.splitlines()
    return lines[line - 1] if 1 <= line <= len(lines) else None


def _diff_is_exact(hunks: tuple[_Hunk, ...], old: str, new: str) -> bool:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if not hunks:
        return False
    for hunk in hunks:
        if any(_line_at(old, number) != text for number, text in hunk.removed):
            return False
        if any(_line_at(new, number) != text for number, text in hunk.added):
            return False
    return bool(old_lines or new_lines)


def _header_at(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, int, int] | None:
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    if not lines:
        return None
    try:
        tokens = list(tokenize.generate_tokens(StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    keyword = "async" if isinstance(node, ast.AsyncFunctionDef) else "def"
    start = next((
        index for index, token in enumerate(tokens)
        if token.type == tokenize.NAME and token.string == keyword and token.start[0] == node.lineno
    ), None)
    if start is None:
        return None
    depth = 0
    lambda_colons = 0
    colon = None
    for token in tokens[start + 1:]:
        if token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                if lambda_colons:
                    lambda_colons -= 1
                else:
                    colon = token
                    break
        elif token.type == tokenize.NAME and token.string == "lambda" and depth == 0:
            lambda_colons += 1
    if colon is None:
        return None
    start_pos = tokens[start].start
    start_offset = offsets[start_pos[0] - 1] + start_pos[1]
    end_offset = offsets[colon.end[0] - 1] + colon.end[1]
    return source[start_offset:end_offset], node.lineno, colon.end[0]


def _callables(statements: list[ast.stmt], scope: tuple[str, ...] = ()):
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield (*scope, statement.name), statement
            yield from _callables(statement.body, (*scope, statement.name))
        elif isinstance(statement, ast.ClassDef):
            yield from _callables(statement.body, (*scope, statement.name))
        else:
            for _, value in ast.iter_fields(statement):
                if isinstance(value, list):
                    yield from _callables([item for item in value if isinstance(item, ast.stmt)], scope)
                elif isinstance(value, ast.stmt):
                    yield from _callables([value], scope)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[Any, ...]:
    return (
        isinstance(node, ast.AsyncFunctionDef),
        ast.dump(node.args, include_attributes=False),
        ast.dump(node.returns, include_attributes=False) if node.returns is not None else None,
        node.type_comment,
        tuple(ast.dump(item, include_attributes=False) for item in getattr(node, "type_params", ())),
    )


def _structure(node: ast.AST):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (
            "callable", node.name,
            tuple(_structure(item) for item in node.decorator_list),
            tuple(_structure(item) for item in node.body),
        )
    if isinstance(node, ast.AST):
        return (
            type(node).__name__,
            tuple((name, _structure(value)) for name, value in ast.iter_fields(node)),
        )
    if isinstance(node, (list, tuple)):
        return tuple(_structure(item) for item in node)
    return node


def _python_supported(snapshot: ScreenedSnapshot, candidate: ProposalCandidate) -> bool:
    try:
        old_tree = ast.parse(snapshot.old.text or "", type_comments=True)
        new_tree = ast.parse(snapshot.new.text or "", type_comments=True)
    except (SyntaxError, ValueError, TypeError):
        return False
    old_items, new_items = dict(_callables(old_tree.body)), dict(_callables(new_tree.body))
    if len(old_items) != len(set(name for name, _ in _callables(old_tree.body))):
        return False
    if old_items.keys() != new_items.keys() or _structure(old_tree) != _structure(new_tree):
        return False
    changed = [name for name in old_items if _signature(old_items[name]) != _signature(new_items[name])]
    if len(changed) != 1:
        return False
    old_header = _header_at(snapshot.old.text or "", old_items[changed[0]])
    new_header = _header_at(snapshot.new.text or "", new_items[changed[0]])
    provenance = candidate.provenance
    return bool(
        old_header is not None
        and new_header is not None
        and old_header[0] == provenance.old_value
        and new_header[0] == provenance.new_value
        and new_header[1] == provenance.source_line
    )


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in parts)


def _flatten(value: Any, parts: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                return None
            nested = _flatten(child, (*parts, key))
            if nested is None:
                return None
            result.update(nested)
        return result
    if isinstance(value, list):
        result = {}
        for index, child in enumerate(value):
            nested = _flatten(child, (*parts, str(index)))
            if nested is None:
                return None
            result.update(nested)
        return result
    if value is None or isinstance(value, (str, bool, int)):
        return {_pointer(parts): value}
    if isinstance(value, float) and math.isfinite(value):
        return {_pointer(parts): value}
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _parse_config(source: str, suffix: str) -> dict[str, Any] | None:
    try:
        if suffix == ".json":
            value = json.loads(source, object_pairs_hook=_unique_object)
        else:
            value = tomllib.loads(source)
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return None
    return _flatten(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _config_supported(snapshot: ScreenedSnapshot, candidate: ProposalCandidate) -> bool:
    suffix = PurePosixPath(snapshot.path).suffix
    old = _parse_config(snapshot.old.text or "", suffix)
    new = _parse_config(snapshot.new.text or "", suffix)
    if old is None or new is None or old.keys() != new.keys():
        return False
    changed = [path for path in old if old[path] != new[path]]
    provenance = candidate.provenance
    if len(changed) != 1:
        return False
    changed_path = changed[0]
    if (
        _canonical(old[changed_path]) != provenance.old_value
        or _canonical(new[changed_path]) != provenance.new_value
    ):
        return False
    leaf = changed_path.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    matching_lines = [
        index for index, line in enumerate((snapshot.new.text or "").splitlines(), 1)
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(leaf)}(?![A-Za-z0-9_])", line)
    ]
    return len(matching_lines) == 1 and matching_lines[0] == provenance.source_line


def _mapped_fences(document: str):
    lines = document.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    for token in MarkdownIt("commonmark").parse(document):
        if token.type != "fence" or token.map is None:
            continue
        start_line, end_line = token.map
        token_lines = token.content.splitlines(keepends=True)
        raw_lines = lines[start_line + 1:end_line - 1]
        if len(token_lines) != len(raw_lines):
            continue
        mapping: list[int] = []
        prefixes: list[str] = []
        newline = "\n"
        valid = True
        for index, (raw, content) in enumerate(zip(raw_lines, token_lines)):
            raw_body = raw.rstrip("\r\n")
            content_body = content.rstrip("\r\n")
            position = raw_body.find(content_body) if content_body else len(raw_body)
            if position < 0:
                valid = False
                break
            prefixes.append(raw_body[:position])
            mapping.extend(range(offsets[start_line + 1 + index] + position,
                                 offsets[start_line + 1 + index] + position + len(content_body)))
            if content.endswith(("\n", "\r")):
                newline = "\r\n" if raw.endswith("\r\n") else raw[-1:]
                mapping.append(offsets[start_line + 1 + index] + len(raw_body))
        if valid and len(mapping) == len(token.content):
            yield token, tuple(mapping), tuple(prefixes), newline


def _python_replacement(document: str, candidate: ProposalCandidate) -> tuple[str | None, int]:
    old = candidate.provenance.old_value.replace("\r\n", "\n").replace("\r", "\n")
    new = candidate.provenance.new_value.replace("\r\n", "\n").replace("\r", "\n")
    hits = []
    try:
        fences = tuple(_mapped_fences(document))
    except Exception:
        return None, 0
    for token, mapping, prefixes, newline in fences:
        position = 0
        while (found := token.content.find(old, position)) >= 0:
            line_index = token.content.count("\n", 0, found)
            line_start = token.content.rfind("\n", 0, found) + 1
            before = token.content[line_start:found]
            after_index = found + len(old)
            after = token.content[after_index:after_index + 1]
            if (not before.strip()) and (not after or after == "\n" or after.isspace()):
                if line_index < len(prefixes) and found + len(old) <= len(mapping):
                    start, end = mapping[found], mapping[found + len(old) - 1] + 1
                    formatted = new.replace("\n", newline + prefixes[line_index])
                    hits.append((start, end, formatted, token.map[0] + 2 + line_index))
            position = found + 1
    if len(hits) != 1:
        return None, len(hits)
    start, end, replacement, line = hits[0]
    if line != candidate.provenance.target_line:
        return None, 0
    return document[:start] + replacement + document[end:], 1


def _split_pipe_row(line: str):
    line = line.rstrip("\r\n")
    cells = []
    start = 1 if line.startswith("|") else 0
    index = start
    escaped = False
    while index < len(line):
        character = line[index]
        if character == "|" and not escaped:
            cells.append((start, index, line[start:index]))
            start = index + 1
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
        index += 1
    if start < len(line) or not line.endswith("|"):
        end = len(line) - 1 if line.endswith("|") else len(line)
        cells.append((start, end, line[start:end]))
    return cells


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|")


def _config_replacement(
    document: str,
    candidate: ProposalCandidate,
    changed_path: str,
) -> tuple[str | None, int]:
    lines = document.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    hits = []
    for index in range(len(lines) - 1):
        header, separator = _split_pipe_row(lines[index]), _split_pipe_row(lines[index + 1])
        if not header or len(header) != len(separator):
            continue
        if not all(_SEPARATOR.fullmatch(cell[2].strip()) for cell in separator):
            continue
        names = [cell[2].strip().casefold() for cell in header]
        keys = [i for i, name in enumerate(names) if name in {"key", "path"}]
        values = [i for i, name in enumerate(names) if name in {"value", "current"}]
        if len(keys) != 1 or len(values) != 1:
            continue
        row = index + 2
        while row < len(lines):
            cells = _split_pipe_row(lines[row])
            if len(cells) != len(header):
                break
            key_cell = cells[keys[0]][2].strip()
            value_start, value_end, value_text = cells[values[0]]
            value = value_text.strip()
            if key_cell == changed_path:
                if value != _escape_cell(candidate.provenance.old_value):
                    hits.append((None, None, None, -1))
                else:
                    leading = len(value_text) - len(value_text.lstrip())
                    trailing = len(value_text.rstrip())
                    hits.append((
                        offsets[row] + value_start + leading,
                        offsets[row] + value_start + trailing,
                        _escape_cell(candidate.provenance.new_value),
                        row + 1,
                    ))
            row += 1
    if len(hits) != 1:
        return None, len(hits)
    start, end, replacement, line = hits[0]
    if line == -1:
        return None, -1
    if start is None or line != candidate.provenance.target_line:
        return None, 0
    return document[:start] + replacement + document[end:], 1


def _hunk_has_line_change(
    hunks: tuple[_Hunk, ...],
    hunk: _Hunk,
    old_line: int,
    new_line: int,
) -> bool:
    return any(line == old_line for line, _ in hunk.removed) and any(
        line == new_line for line, _ in hunk.added
    )


def _competing_python_anchor(inventory: MarkdownInventory, candidate: ProposalCandidate) -> bool:
    for document in inventory.documents:
        if document.path == candidate.target_path:
            continue
        _, count = _python_replacement(document.text, candidate)
        if count:
            return True
    return False


def _validate_consistency(
    candidate: ProposalCandidate,
    snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
    *,
    scanner: SecretScanner | None = None,
) -> ValidatorResult:
    """Verify a candidate is an exact, allowlisted replacement supported by complete evidence."""
    scanner = scanner or SecretScanner()
    if (
        not snapshots.complete or snapshots.status is not StageStatus.COMPLETED
        or snapshots.diff_text is None or not inventory.complete
        or inventory.status is not StageStatus.COMPLETED
    ):
        return _result(ValidatorStatus.INCONCLUSIVE, "Complete screened source and Markdown evidence are required.", candidate, scanner)
    provenance = candidate.provenance
    if candidate.target_path != provenance.target_path or (
        len(snapshots.snapshots) == 1
        and candidate.source_paths == (snapshots.snapshots[0].path,)
        and provenance.source_path != snapshots.snapshots[0].path
    ):
        return _result(ValidatorStatus.FAIL, "Candidate provenance contradicts the screened evidence mapping.", candidate, scanner)
    sources = [snapshot for snapshot in snapshots.snapshots if snapshot.path == provenance.source_path]
    documents = [document for document in inventory.documents if document.path == provenance.target_path]
    if len(sources) != 1 or len(documents) != 1:
        return _result(ValidatorStatus.INCONCLUSIVE, "Source snapshot or unique target document is unavailable.", candidate, scanner)
    snapshot, document = sources[0], documents[0]
    if (
        snapshot.binary or snapshot.old.state is not SnapshotSideState.PRESENT
        or snapshot.new.state is not SnapshotSideState.PRESENT
        or snapshot.old.text is None or snapshot.new.text is None
    ):
        return _result(ValidatorStatus.INCONCLUSIVE, "Both complete screened source versions are required.", candidate, scanner)
    if (
        candidate.target_path != provenance.target_path
        or candidate.source_paths != (provenance.source_path,)
        or provenance.replacement_kind != candidate.kind.value
    ):
        return _result(ValidatorStatus.FAIL, "Candidate provenance contradicts its target or source mapping.", candidate, scanner)

    hunks = _parse_hunks(snapshots.diff_text, snapshot.path)
    matching_hunks = [hunk for hunk in hunks if hunk.header == provenance.diff_hunk and hunk.diff_line == provenance.diff_line]
    if not matching_hunks or not _diff_is_exact(hunks, snapshot.old.text, snapshot.new.text):
        return _result(ValidatorStatus.FAIL, "The screened diff does not exactly support the recorded source change.", candidate, scanner)

    if candidate.kind is ProposalKind.PYTHON_SIGNATURE and PurePosixPath(snapshot.path).suffix == ".py":
        supported = _python_supported(snapshot, candidate)
        if supported:
            old_ast = ast.parse(snapshot.old.text or "", type_comments=True)
            new_ast = ast.parse(snapshot.new.text or "", type_comments=True)
            old_nodes = dict(_callables(old_ast.body))
            new_nodes = dict(_callables(new_ast.body))
            changed_name = next(
                name for name in old_nodes if _signature(old_nodes[name]) != _signature(new_nodes[name])
            )
            old_header = _header_at(snapshot.old.text or "", old_nodes[changed_name])
            new_header = _header_at(snapshot.new.text or "", new_nodes[changed_name])
            verified_hunks = _verified_python_hunks(
                snapshots.diff_text,
                snapshot.path,
                snapshot.old.text or "",
                snapshot.new.text or "",
            )
            relevant = bool(old_header and new_header and verified_hunks)
            if relevant:
                old_header_lines = range(old_header[1], old_header[2] + 1)
                new_header_lines = range(new_header[1], new_header[2] + 1)
                relevant = all(
                    line in old_header_lines
                    for hunk in verified_hunks or ()
                    for line in hunk.removed
                ) and all(
                    line in new_header_lines
                    for hunk in verified_hunks or ()
                    for line in hunk.added
                ) and any(
                    hunk.header == provenance.diff_hunk
                    and hunk.diff_line == provenance.diff_line
                    for hunk in verified_hunks or ()
                )
            expected, anchor_count = _python_replacement(document.text, candidate)
            if not relevant:
                return _result(ValidatorStatus.FAIL, "The exact screened diff does not map only to the recorded signature header.", candidate, scanner)
            if anchor_count != 1:
                return _result(ValidatorStatus.INCONCLUSIVE, "The exact fenced target anchor is missing or not unique.", candidate, scanner)
            if _competing_python_anchor(inventory, candidate):
                return _result(ValidatorStatus.WARNING, "A competing Markdown signature anchor exists in another document.", candidate, scanner)
            if expected != candidate.replacement:
                return _result(ValidatorStatus.FAIL, "The proposed document changes more than the exact signature anchor.", candidate, scanner)
        else:
            return _result(ValidatorStatus.FAIL, "The Python source change is unsupported or contradicts the candidate.", candidate, scanner)
    elif candidate.kind is ProposalKind.CONFIG_SCALAR and PurePosixPath(snapshot.path).suffix in {".json", ".toml"}:
        supported = _config_supported(snapshot, candidate)
        if not supported:
            return _result(ValidatorStatus.FAIL, "The configuration source change is unsupported or contradicts the candidate.", candidate, scanner)
        suffix = PurePosixPath(snapshot.path).suffix
        changed = _parse_config(snapshot.old.text or "", suffix)
        revised = _parse_config(snapshot.new.text or "", suffix)
        if changed is None or revised is None:
            return _result(ValidatorStatus.INCONCLUSIVE, "Configuration evidence could not be reparsed safely.", candidate, scanner)
        changed_path = next(path for path in changed if changed[path] != revised[path])
        leaf = changed_path.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        old_source_line = next(
            index for index, line in enumerate((snapshot.old.text or "").splitlines(), 1)
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(leaf)}(?![A-Za-z0-9_])", line)
        )
        new_source_line = provenance.source_line
        if not any(_hunk_has_line_change(hunks, hunk, old_source_line, new_source_line) for hunk in matching_hunks):
            return _result(ValidatorStatus.FAIL, "The recorded configuration value is absent from its diff hunk.", candidate, scanner)
        expected, anchor_count = _config_replacement(document.text, candidate, changed_path)
        if anchor_count < 0:
            return _result(ValidatorStatus.FAIL, "The settings anchor contradicts the recorded old scalar value.", candidate, scanner)
        if anchor_count != 1:
            return _result(ValidatorStatus.INCONCLUSIVE, "The exact structured settings-row anchor is missing or not unique.", candidate, scanner)
        for other in inventory.documents:
            if other.path == document.path:
                continue
            _, other_count = _config_replacement(other.text, candidate, changed_path)
            if other_count:
                return _result(ValidatorStatus.WARNING, "A competing settings-row anchor exists in another document.", candidate, scanner)
        if expected != candidate.replacement:
            return _result(ValidatorStatus.FAIL, "The proposed document changes more than the exact scalar value.", candidate, scanner)
    else:
        return _result(ValidatorStatus.WARNING, "The candidate change type is outside the deterministic allowlist.", candidate, scanner)

    return _result(ValidatorStatus.PASS, "The exact allowlisted replacement matches complete screened source, diff, and target evidence; semantic completeness is not assessed.", candidate, scanner)


def validate_consistency(
    candidate: ProposalCandidate,
    snapshots: ScreenedSnapshots,
    inventory: MarkdownInventory,
    *,
    scanner: SecretScanner | None = None,
) -> ValidatorResult:
    scanner = scanner or SecretScanner()
    try:
        return _validate_consistency(candidate, snapshots, inventory, scanner=scanner)
    except Exception:
        return ValidatorResult(
            "consistency",
            ValidatorStatus.INCONCLUSIVE,
            "Source-to-documentation evidence could not be checked safely.",
        )