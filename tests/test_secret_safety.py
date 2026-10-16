from __future__ import annotations

import docsync.screening as screening
from docsync.screening import ScreeningState, SecretScanner


def test_malformed_utf8_is_fixed_sanitized_fail_closed_result() -> None:
    result = SecretScanner().screen_bytes(b"token=fixture-decode-secret-201\xff")

    assert result.state is ScreeningState.INCONCLUSIVE
    assert result.excluded
    assert result.sanitized_text is None
    assert result.diagnostic == (
        "Screening could not be completed; the affected content was withheld."
    )
    assert "fixture-decode-secret-201" not in repr(result)
    assert "UnicodeDecodeError" not in repr(result)


def test_scanner_exception_text_is_not_returned(monkeypatch) -> None:
    def scanner_error(_text: str):
        raise RuntimeError("fixture-scanner-exception-secret-202")

    monkeypatch.setattr(screening, "_find_sensitive_spans", scanner_error)
    result = SecretScanner().screen_text("safe source evidence")

    assert result.state is ScreeningState.INCONCLUSIVE
    assert result.excluded
    assert result.sanitized_text is None
    assert result.diagnostic == (
        "Screening could not be completed; the affected content was withheld."
    )
    assert "fixture-scanner-exception-secret-202" not in repr(result)


def test_detected_candidate_and_output_are_blocked_before_sink_use() -> None:
    scanner = SecretScanner()
    candidate = scanner.screen_candidate("Proposal text with api_key=fixture-output-secret-203")
    final_output = scanner.screen_output("Report token: fixture-report-secret-204")

    for result in (candidate, final_output):
        assert result.state is ScreeningState.COMPLETE
        assert result.blocked
        assert result.sanitized_text is None
        assert result.diagnostic == (
            "Detected sensitive content; the affected content was withheld."
        )
    assert "fixture-output-secret-203" not in repr(candidate)
    assert "fixture-report-secret-204" not in repr(final_output)


def test_fake_request_boundary_receives_only_screened_payload() -> None:
    raw_context = "Relevant config: client_secret=fixture-adapter-secret-205"
    screened = SecretScanner().screen_request_context(raw_context)
    adapter_payload = {"context": screened.sanitized_text} if screened.usable else None

    assert adapter_payload is not None
    assert "fixture-adapter-secret-205" not in repr(adapter_payload)
    assert "[REDACTED]" in adapter_payload["context"]


def test_path_secret_is_not_retained_in_safe_provenance() -> None:
    result = SecretScanner().screen_text(
        "The option is configured here.",
        provenance="config/secret=fixture-provenance-secret-206.toml",
    )

    assert result.usable
    assert result.provenance == "config/secret=[REDACTED]"
    assert "fixture-provenance-secret-206" not in repr(result)


def test_scanner_does_not_claim_complete_detection() -> None:
    scanner = SecretScanner()
    fixture = "opaque-secret-not-matching-any-configured-pattern-207"

    result = scanner.screen_text(fixture)

    assert result.usable
    assert result.sanitized_text == fixture
    assert result.categories == ()
    assert "incomplete" in (screening.__doc__ or "").casefold()