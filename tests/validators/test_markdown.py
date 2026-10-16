from __future__ import annotations

from markdown_it import MarkdownIt

from docsync.models import ValidatorStatus
from docsync.validators import markdown


def test_valid_commonmark_passes_without_semantic_claim() -> None:
    result = markdown.validate_markdown("# API\n\n```python\ndef run(): ...\n```\n")

    assert result.status is ValidatorStatus.PASS
    assert "semantic" in result.sanitized_detail


def test_empty_markdown_fails() -> None:
    result = markdown.validate_markdown(" \n\t")

    assert result.status is ValidatorStatus.FAIL


def test_unclosed_fence_fails_when_parser_emits_fence_token() -> None:
    result = markdown.validate_markdown("```python\ndef run():\n")

    assert result.status is ValidatorStatus.FAIL
    assert result.evidence_refs


def test_parser_exception_is_inconclusive(monkeypatch) -> None:
    def fail():
        raise RuntimeError("untrusted exception contents")

    monkeypatch.setattr(markdown, "_parser", fail)
    result = markdown.validate_markdown("# Valid CommonMark")

    assert result.status is ValidatorStatus.INCONCLUSIVE
    assert "untrusted exception" not in result.sanitized_detail


def test_parser_uses_commonmark_without_optional_extensions() -> None:
    parser = markdown._parser()

    assert isinstance(parser, MarkdownIt)
    assert "table" not in parser.block.ruler.get_active_rules()
    assert "strikethrough" not in parser.inline.ruler.get_active_rules()