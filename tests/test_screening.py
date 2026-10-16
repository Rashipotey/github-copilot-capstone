from __future__ import annotations

import pytest

from docsync.screening import (
    DetectionCategory,
    ScreeningState,
    SecretScanner,
)


def test_detection_category_serialized_labels_are_stable() -> None:
    assert {category.name: category.value for category in DetectionCategory} == {
        "PEM_PRIVATE_KEY": "pem_private_key",
        "PASSWORD_ASSIGNMENT": "password_assignment",
        "PASSWD_ASSIGNMENT": "passwd_assignment",
        "SECRET_ASSIGNMENT": "secret_assignment",
        "TOKEN_ASSIGNMENT": "token_assignment",
        "API_KEY_ASSIGNMENT": "api_key_assignment",
        "ACCESS_KEY_ASSIGNMENT": "access_key_assignment",
        "CLIENT_SECRET_ASSIGNMENT": "client_secret_assignment",
        "PRIVATE_KEY_ASSIGNMENT": "private_key_assignment",
        "HTTP_BEARER": "http_bearer",
        "HTTP_BASIC": "http_basic",
        "JWT_SHAPED": "jwt_shaped",
    }


@pytest.mark.parametrize(
    ("text", "secret", "category"),
    [
        (
            'password = "fixture-password-91"',
            "fixture-password-91",
            DetectionCategory.PASSWORD_ASSIGNMENT,
        ),
        (
            "passwd: fixture-passwd-92",
            "fixture-passwd-92",
            DetectionCategory.PASSWD_ASSIGNMENT,
        ),
        (
            'secret="fixture-secret-93"',
            "fixture-secret-93",
            DetectionCategory.SECRET_ASSIGNMENT,
        ),
        (
            "refresh_token: fixture-token-94",
            "fixture-token-94",
            DetectionCategory.TOKEN_ASSIGNMENT,
        ),
        (
            "api_key = fixture-api-key-95",
            "fixture-api-key-95",
            DetectionCategory.API_KEY_ASSIGNMENT,
        ),
        (
            'access-key: "fixture-access-key-96"',
            "fixture-access-key-96",
            DetectionCategory.ACCESS_KEY_ASSIGNMENT,
        ),
        (
            "client_secret=fixture-client-secret-97",
            "fixture-client-secret-97",
            DetectionCategory.CLIENT_SECRET_ASSIGNMENT,
        ),
        (
            "private key: fixture-private-key-98",
            "fixture-private-key-98",
            DetectionCategory.PRIVATE_KEY_ASSIGNMENT,
        ),
        (
            "Authorization: Bearer fixture-bearer-value-99",
            "fixture-bearer-value-99",
            DetectionCategory.HTTP_BEARER,
        ),
        (
            "Authorization: Basic Zml4dHVyZTpwYXNzd29yZA==",
            "Zml4dHVyZTpwYXNzd29yZA==",
            DetectionCategory.HTTP_BASIC,
        ),
        (
            "token: aaa.bbb.ccc",
            "aaa.bbb.ccc",
            DetectionCategory.TOKEN_ASSIGNMENT,
        ),
        (
            "value=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturepart123",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturepart123",
            DetectionCategory.JWT_SHAPED,
        ),
    ],
)
def test_detects_and_redacts_each_inline_pattern(
    text: str,
    secret: str,
    category: DetectionCategory,
) -> None:
    result = SecretScanner().screen_text(
        f"Example configuration: {text}",
        provenance="src/config.py",
    )

    assert result.state is ScreeningState.COMPLETE
    assert result.usable
    assert result.sanitized_text is not None
    assert secret not in result.sanitized_text
    assert category in result.categories
    assert dict(result.detection_counts)[category] == 1
    assert secret not in repr(result)


def test_private_key_block_excludes_entire_evidence_item() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "fixture-private-key-material-101\n"
        "-----END PRIVATE KEY-----"
    )

    result = SecretScanner().screen_evidence_item(
        f"Use this key for signing.\n{private_key}\nSigning algorithm: EdDSA",
        provenance="src/keys.py",
    )

    assert result.complete
    assert result.excluded
    assert not result.usable
    assert result.sanitized_text is None
    assert DetectionCategory.PEM_PRIVATE_KEY in result.categories
    assert "fixture-private-key-material-101" not in repr(result)


def test_encrypted_pem_private_key_block_is_detected() -> None:
    result = SecretScanner().screen_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        "fixture-encrypted-key-material-111\n"
        "-----END ENCRYPTED PRIVATE KEY-----"
    )

    assert result.excluded
    assert DetectionCategory.PEM_PRIVATE_KEY in result.categories
    assert "fixture-encrypted-key-material-111" not in repr(result)


def test_unterminated_private_key_fails_inconclusively_and_excludes_item() -> None:
    result = SecretScanner().screen_text(
        "-----BEGIN RSA PRIVATE KEY-----\nfixture-key-material-102"
    )

    assert result.state is ScreeningState.INCONCLUSIVE
    assert result.excluded
    assert result.sanitized_text is None
    assert "fixture-key-material-102" not in repr(result)
    assert "fixture-key-material-102" not in result.diagnostic


def test_redaction_without_remaining_evidence_excludes_item() -> None:
    result = SecretScanner().screen_text("token=fixture-token-only-103")

    assert result.complete
    assert result.excluded
    assert result.sanitized_text is None
    assert "fixture-token-only-103" not in repr(result)


def test_uncertain_assignment_boundary_excludes_item() -> None:
    result = SecretScanner().screen_text("password =\nmore config follows")

    assert result.state is ScreeningState.INCONCLUSIVE
    assert result.excluded
    assert result.sanitized_text is None


def test_unterminated_quoted_assignment_excludes_item() -> None:
    result = SecretScanner().screen_text('Relevant setting: password="fixture-open-quote-112')

    assert result.state is ScreeningState.INCONCLUSIVE
    assert result.excluded
    assert result.sanitized_text is None
    assert "fixture-open-quote-112" not in repr(result)


def test_safe_context_and_nearby_non_secret_prose_are_preserved() -> None:
    source = (
        "The password field is required. Set it in the interactive prompt. "
        "Basic authentication is available for compatible clients."
    )

    result = SecretScanner().screen_text(source)

    assert result.usable
    assert result.sanitized_text == source
    assert result.categories == ()


def test_both_diff_sides_and_request_context_are_screened() -> None:
    scanner = SecretScanner()
    old_result = scanner.screen_diff_side(
        "Changed option: token=fixture-old-token-104",
        side="old",
        provenance="src/settings.py",
    )
    new_result = scanner.screen_diff_side(
        "Changed option: token=fixture-new-token-105",
        side="new",
        provenance="src/settings.py",
    )
    request_result = scanner.screen_request_context(
        "Context: client_secret=fixture-request-secret-106"
    )

    assert old_result.usable and new_result.usable and request_result.usable
    assert "fixture-old-token-104" not in repr(old_result)
    assert "fixture-new-token-105" not in repr(new_result)
    assert "fixture-request-secret-106" not in repr(request_result)


def test_path_and_diagnostic_values_are_sanitized() -> None:
    scanner = SecretScanner()
    path_result = scanner.screen_repository_path("config/secret=fixture-path-secret-107.toml")
    diagnostic_result = scanner.screen_diagnostic(
        "Failed reading token=fixture-diagnostic-secret-108"
    )

    assert path_result.sanitized_text is not None
    assert "fixture-path-secret-107" not in repr(path_result)
    assert diagnostic_result.sanitized_text is not None
    assert "fixture-diagnostic-secret-108" not in repr(diagnostic_result)


def test_known_categories_are_limited_and_counts_do_not_expose_values() -> None:
    result = SecretScanner().screen_text(
        "password=fixture-count-secret-109; token=fixture-count-token-110"
    )

    assert result.categories == (
        DetectionCategory.PASSWORD_ASSIGNMENT,
        DetectionCategory.TOKEN_ASSIGNMENT,
    )
    assert dict(result.detection_counts) == {
        DetectionCategory.PASSWORD_ASSIGNMENT: 1,
        DetectionCategory.TOKEN_ASSIGNMENT: 1,
    }
    assert "fixture-count-secret-109" not in repr(result)
    assert "fixture-count-token-110" not in repr(result)