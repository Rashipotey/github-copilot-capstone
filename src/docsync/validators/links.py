from __future__ import annotations

import posixpath
import re
import stat
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from docsync.evidence import MarkdownInventory
from docsync.models import ValidatorResult, ValidatorStatus
from docsync.paths import PathValidationError, canonicalize_repository_root, validate_repository_name
from docsync.screening import SecretScanner


_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _result(status: ValidatorStatus, detail: str, path: str, scanner: SecretScanner) -> ValidatorResult:
    try:
        screened = scanner.screen_repository_path(path)
    except Exception:
        screened = None
    refs = ()
    if screened is not None and screened.usable and screened.sanitized_text:
        refs = (screened.sanitized_text,)
    return ValidatorResult("links", status, detail, refs)


def _local_path(document_path: str, href_path: str) -> str | None:
    if not href_path:
        return document_path
    if "\\" in href_path or "\x00" in href_path or href_path.startswith("/"):
        return None
    decoded = unquote(href_path)
    combined = posixpath.normpath(posixpath.join(posixpath.dirname(document_path), decoded))
    if combined in {"", "."}:
        return document_path
    if combined == ".." or combined.startswith("../") or combined.startswith("/"):
        return None
    return combined


def _inspect_local_path(root: Path, relative: str, virtual_paths: set[str]) -> str:
    """Return present, missing, or unsafe without following a link or reparse point."""
    try:
        parts = validate_repository_name(relative)
    except PathValidationError:
        return "unsafe"
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if relative in virtual_paths and index == len(parts) - 1:
                return "present"
            return "missing"
        except OSError:
            return "unsafe"
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT:
            return "unsafe"
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return "missing"
    try:
        current.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return "unsafe"
    return "present"


def _heading_fragments(markdown: str) -> tuple[set[str], bool]:
    try:
        tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown)
    except Exception:
        return set(), False
    fragments: set[str] = set()
    counts: dict[str, int] = {}
    reliable = True
    for index, token in enumerate(tokens):
        for html_token in (token, *(token.children or ())):
            if html_token.type not in {"html_block", "html_inline"}:
                continue
            for match in re.finditer(
                r"\b(?:id|name)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                html_token.content,
                re.IGNORECASE,
            ):
                fragments.add(next(value for value in match.groups() if value is not None))
        if token.type != "heading_open" or index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        children = inline.children or ()
        supported = {"text", "code_inline", "softbreak", "hardbreak", "link_open", "link_close"}
        if any(child.type not in supported for child in children):
            reliable = False
        text = "".join(child.content for child in children if child.type in {"text", "code_inline"})
        base = _slug(text)
        if not base:
            continue
        count = counts.get(base, 0)
        counts[base] = count + 1
        fragments.add(base if count == 0 else f"{base}-{count}")
    return fragments, reliable


def _slug(text: str) -> str:
    output: list[str] = []
    pending_hyphen = False
    for character in text.casefold():
        category = unicodedata.category(character)
        if character.isspace() or character == "-":
            pending_hyphen = bool(output)
        elif category[0] in {"L", "N"} or character == "_":
            if pending_hyphen and output and output[-1] != "-":
                output.append("-")
            output.append(character)
            pending_hyphen = False
    return "".join(output).strip("-")


def _link_targets(markdown: str):
    parser = MarkdownIt("commonmark")
    tokens = parser.parse(markdown)
    for block in tokens:
        for token in (block, *(block.children or ())):
            if token.type == "link_open":
                href = token.attrGet("href")
                if href is not None:
                    yield href
            elif token.type == "image":
                src = token.attrGet("src")
                if src is not None:
                    yield src


def _validate_links(
    repo_root: Path | str,
    document_path: str,
    proposed_markdown: str,
    inventory: MarkdownInventory,
    *,
    scanner: SecretScanner | None = None,
) -> ValidatorResult:
    """Check CommonMark links locally against disk and the proposed virtual state."""
    scanner = scanner or SecretScanner()
    if not inventory.complete:
        return _result(
            ValidatorStatus.INCONCLUSIVE,
            "The Markdown inventory is incomplete; local references could not be checked.",
            document_path,
            scanner,
        )
    try:
        root = canonicalize_repository_root(repo_root)
        validate_repository_name(document_path)
        if not isinstance(proposed_markdown, str):
            raise ValueError
        links = tuple(_link_targets(proposed_markdown))
    except Exception:
        return _result(
            ValidatorStatus.INCONCLUSIVE,
            "Markdown references could not be parsed or repository paths could not be checked.",
            document_path,
            scanner,
        )

    documents = {document.path: document.text for document in inventory.documents}
    virtual_paths = set(documents)
    virtual_paths.add(document_path)
    documents[document_path] = proposed_markdown

    for href in links:
        if re.match(r"^[A-Za-z]:(?:/|\\\\|%5[cC])", href):
            return _result(
                ValidatorStatus.FAIL,
                "A local reference uses an absolute or unsafe path.",
                document_path,
                scanner,
            )
        try:
            parsed = urlsplit(href)
        except ValueError:
            return _result(
                ValidatorStatus.INCONCLUSIVE,
                "A malformed local reference could not be resolved.", document_path, scanner,
            )
        if parsed.scheme or parsed.netloc:
            continue
        if "\\" in href or re.match(r"^[A-Za-z]:[\\/]", href):
            return _result(
                ValidatorStatus.FAIL,
                "A local reference uses an absolute or unsafe path.",
                document_path,
                scanner,
            )
        if _BAD_ESCAPE.search(href) or _BAD_ESCAPE.search(unquote(href)):
            return _result(
                ValidatorStatus.INCONCLUSIVE,
                "A malformed local reference could not be resolved.", document_path, scanner,
            )
        relative = _local_path(document_path, parsed.path)
        if relative is None:
            return _result(
                ValidatorStatus.FAIL,
                "A local reference escapes the repository or uses an unsafe path.",
                document_path,
                scanner,
            )
        state = _inspect_local_path(root, relative, virtual_paths)
        if state == "missing":
            return _result(
                ValidatorStatus.FAIL,
                "A local reference points to a missing repository path.", document_path, scanner,
            )
        if state == "unsafe":
            return _result(
                ValidatorStatus.FAIL,
                "A local reference traverses a link, reparse point, or unsafe path.",
                document_path,
                scanner,
            )
        if parsed.fragment:
            target_markdown = documents.get(relative)
            if target_markdown is None:
                return _result(
                    ValidatorStatus.INCONCLUSIVE,
                    "A fragment target is not Markdown and could not be checked locally.",
                    document_path,
                    scanner,
                )
            fragments, checkable = _heading_fragments(target_markdown)
            fragment = unquote(parsed.fragment)
            if fragment not in fragments:
                if not checkable:
                    return _result(
                        ValidatorStatus.INCONCLUSIVE,
                        "A local fragment could not be checked because its heading syntax is unsupported.",
                        document_path,
                        scanner,
                    )
                return _result(
                    ValidatorStatus.FAIL,
                    "A local Markdown fragment does not match a heading in the virtual document.",
                    document_path,
                    scanner,
                )

    return _result(
        ValidatorStatus.PASS,
        "CommonMark local links and Markdown fragments resolve within the repository; remote URLs were not fetched.",
        document_path,
        scanner,
    )


def validate_links(
    repo_root: Path | str,
    document_path: str,
    proposed_markdown: str,
    inventory: MarkdownInventory,
    *,
    scanner: SecretScanner | None = None,
) -> ValidatorResult:
    scanner = scanner or SecretScanner()
    try:
        return _validate_links(
            repo_root,
            document_path,
            proposed_markdown,
            inventory,
            scanner=scanner,
        )
    except Exception:
        return _result(
            ValidatorStatus.INCONCLUSIVE,
            "Local references could not be checked safely.",
            document_path if isinstance(document_path, str) else "[path withheld]",
            scanner,
        )