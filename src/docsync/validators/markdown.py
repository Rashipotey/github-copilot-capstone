from __future__ import annotations

import re

from markdown_it import MarkdownIt

from docsync.models import ValidatorResult, ValidatorStatus


def _parser() -> MarkdownIt:
    return MarkdownIt("commonmark")


def validate_markdown(proposed_markdown: str) -> ValidatorResult:
    """Check basic CommonMark parseability without style or semantic claims."""
    if not isinstance(proposed_markdown, str):
        return ValidatorResult(
            "markdown", ValidatorStatus.INCONCLUSIVE,
            "Proposed Markdown could not be checked.",
        )
    if not proposed_markdown.strip():
        return ValidatorResult(
            "markdown", ValidatorStatus.FAIL,
            "Proposed Markdown is empty.",
        )

    try:
        parser = _parser()
        tokens = parser.parse(proposed_markdown)
    except Exception:
        return ValidatorResult(
            "markdown", ValidatorStatus.INCONCLUSIVE,
            "CommonMark parsing could not be completed.",
        )

    lines = proposed_markdown.splitlines()
    for token in tokens:
        if token.type != "fence" or token.map is None:
            continue
        start, end = token.map
        opening = lines[start] if start < len(lines) else ""
        marker = token.markup
        closing = lines[end - 1] if 0 < end <= len(lines) else ""
        close = re.search(r"(`{3,}|~{3,})[ \t]*\Z", closing)
        if (
            not marker
            or close is None
            or close.group(1)[0] != marker[0]
            or len(close.group(1)) < len(marker)
            or marker not in opening
        ):
            return ValidatorResult(
                "markdown", ValidatorStatus.FAIL,
                "A fenced code block is unclosed or structurally invalid.",
                ("proposed Markdown fence",),
            )

    return ValidatorResult(
        "markdown", ValidatorStatus.PASS,
        "Proposed Markdown parsed as CommonMark; semantic and style correctness are not assessed.",
    )