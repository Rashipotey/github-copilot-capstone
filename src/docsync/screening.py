"""Pattern-based secret screening; detection is intentionally incomplete."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Iterable


class ScreeningState(StrEnum):
    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"


class DetectionCategory(StrEnum):
    PEM_PRIVATE_KEY = auto()
    PASSWORD_ASSIGNMENT = auto()
    PASSWD_ASSIGNMENT = auto()
    SECRET_ASSIGNMENT = auto()
    TOKEN_ASSIGNMENT = auto()
    API_KEY_ASSIGNMENT = auto()
    ACCESS_KEY_ASSIGNMENT = auto()
    CLIENT_SECRET_ASSIGNMENT = auto()
    PRIVATE_KEY_ASSIGNMENT = auto()
    HTTP_BEARER = auto()
    HTTP_BASIC = auto()
    JWT_SHAPED = auto()


_SCREENING_FAILURE = "Screening could not be completed; the affected content was withheld."
_DETECTED_CONTENT = "Detected sensitive content; the affected content was withheld."
_SCREENING_COMPLETE = "Screening completed."
_REDACTED = "[REDACTED]"

_PEM_BEGIN = re.compile(
    r"-----BEGIN (?P<label>(?:(?:RSA|EC|OPENSSH|DSA|ENCRYPTED) )?PRIVATE KEY)-----"
)
_PEM_END = re.compile(
    r"-----END (?P<label>(?:(?:RSA|EC|OPENSSH|DSA|ENCRYPTED) )?PRIVATE KEY)-----"
)
_CREDENTIAL_PREFIX = re.compile(
    r"(?P<label>(?<![A-Za-z0-9])(?:password|passwd|secret|token|"
    r"api[ _-]*key|access[ _-]*key|client[ _-]*secret|private[ _-]*key)"
    r"(?![A-Za-z0-9]))(?P<key_quote>[\"']?)[ \t]*(?:[:=])[ \t]*",
    re.IGNORECASE,
)
_AUTH_VALUE = re.compile(
    r"\b(?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"(?P<scheme>Bearer|Basic)[ \t]+(?P<value>[A-Za-z0-9._~+/-]+=*)",
    re.IGNORECASE,
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_ONLY_CREDENTIAL_LABELS = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[ _-]*key|access[ _-]*key|"
    r"client[ _-]*secret|private[ _-]*key|authorization|bearer|basic)"
)


@dataclass(frozen=True, slots=True)
class _SensitiveSpan:
    start: int
    end: int
    category: DetectionCategory


@dataclass(frozen=True, slots=True)
class _Scan:
    spans: tuple[_SensitiveSpan, ...]
    uncertain_boundary: bool = False
    exclude_item: bool = False


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    state: ScreeningState
    sanitized_text: str | None
    categories: tuple[DetectionCategory, ...]
    detection_counts: tuple[tuple[DetectionCategory, int], ...]
    provenance: str | None
    diagnostic: str
    excluded: bool = False
    blocked: bool = False

    @property
    def complete(self) -> bool:
        return self.state is ScreeningState.COMPLETE

    @property
    def usable(self) -> bool:
        return (
            self.complete
            and self.sanitized_text is not None
            and not self.excluded
            and not self.blocked
        )


def _category_for_label(label: str) -> DetectionCategory:
    normalized = re.sub(r"[ _-]", "", label.casefold())
    return {
        "password": DetectionCategory.PASSWORD_ASSIGNMENT,
        "passwd": DetectionCategory.PASSWD_ASSIGNMENT,
        "secret": DetectionCategory.SECRET_ASSIGNMENT,
        "token": DetectionCategory.TOKEN_ASSIGNMENT,
        "apikey": DetectionCategory.API_KEY_ASSIGNMENT,
        "accesskey": DetectionCategory.ACCESS_KEY_ASSIGNMENT,
        "clientsecret": DetectionCategory.CLIENT_SECRET_ASSIGNMENT,
        "privatekey": DetectionCategory.PRIVATE_KEY_ASSIGNMENT,
    }[normalized]


def _quoted_value_end(text: str, start: int, quote: str) -> int | None:
    for index in range(start + 1, len(text)):
        if text[index] in "\r\n":
            return None
        if text[index] == quote:
            backslashes = 0
            cursor = index - 1
            while cursor > start and text[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                return index
    return None


def _find_sensitive_spans(text: str) -> _Scan:
    spans: list[_SensitiveSpan] = []

    for begin in _PEM_BEGIN.finditer(text):
        ending = _PEM_END.search(text, begin.end())
        if ending is None or ending.group("label") != begin.group("label"):
            return _Scan(tuple(spans), uncertain_boundary=True, exclude_item=True)
        spans.append(
            _SensitiveSpan(begin.start(), ending.end(), DetectionCategory.PEM_PRIVATE_KEY)
        )

    for prefix in _CREDENTIAL_PREFIX.finditer(text):
        value_start = prefix.end()
        if value_start >= len(text) or text[value_start] in "\r\n,;#":
            return _Scan(tuple(spans), uncertain_boundary=True, exclude_item=True)

        if text[value_start] in "\"'":
            value_end = _quoted_value_end(text, value_start, text[value_start])
            if value_end is None:
                return _Scan(tuple(spans), uncertain_boundary=True, exclude_item=True)
            if value_end == value_start + 1:
                continue
        else:
            value_end = value_start
            while value_end < len(text) and text[value_end] not in " \t\r\n,;#`\"'":
                value_end += 1
            if value_end == value_start:
                return _Scan(tuple(spans), uncertain_boundary=True, exclude_item=True)

        spans.append(
            _SensitiveSpan(
                value_start,
                value_end,
                _category_for_label(prefix.group("label")),
            )
        )

    for match in _AUTH_VALUE.finditer(text):
        category = (
            DetectionCategory.HTTP_BEARER
            if match.group("scheme").casefold() == "bearer"
            else DetectionCategory.HTTP_BASIC
        )
        spans.append(_SensitiveSpan(match.start("value"), match.end("value"), category))

    spans.extend(
        _SensitiveSpan(match.start(), match.end(), DetectionCategory.JWT_SHAPED)
        for match in _JWT.finditer(text)
    )
    return _Scan(tuple(spans))


def _redact(text: str, spans: Iterable[_SensitiveSpan]) -> str:
    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    merged: list[tuple[int, int]] = []
    for span in ordered:
        if merged and span.start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span.end))
        else:
            merged.append((span.start, span.end))

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.extend((text[cursor:start], _REDACTED))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _has_safe_context(text: str) -> bool:
    without_marker = text.replace(_REDACTED, " ")
    without_labels = _ONLY_CREDENTIAL_LABELS.sub(" ", without_marker)
    return any(character.isalnum() for character in without_labels)


class SecretScanner:
    """Screen untrusted text/bytes and return only sanitized evidence."""

    def screen_bytes(
        self,
        content: bytes,
        *,
        provenance: str | None = None,
    ) -> ScreeningResult:
        try:
            decoded = content.decode("utf-8", errors="strict")
        except Exception:
            return self._failure()
        return self.screen_text(decoded, provenance=provenance)

    def screen_text(
        self,
        content: str,
        *,
        provenance: str | None = None,
        reject_detected: bool = False,
    ) -> ScreeningResult:
        try:
            if not isinstance(content, str):
                return self._failure()

            scan = _find_sensitive_spans(content)
            metadata_result = self._screen_provenance(provenance)
            if metadata_result[0] is ScreeningState.INCONCLUSIVE:
                return self._failure()

            provenance_text, metadata_spans = metadata_result[1], metadata_result[2]
            all_spans = (*scan.spans, *metadata_spans)
            counts: dict[DetectionCategory, int] = {}
            for span in all_spans:
                counts[span.category] = counts.get(span.category, 0) + 1
            categories = tuple(sorted(counts, key=lambda category: category.value))
            detection_counts = tuple((category, counts[category]) for category in categories)

            if scan.uncertain_boundary:
                return ScreeningResult(
                    ScreeningState.INCONCLUSIVE,
                    None,
                    categories,
                    detection_counts,
                    provenance_text,
                    _SCREENING_FAILURE,
                    excluded=True,
                )

            if scan.exclude_item or any(
                span.category is DetectionCategory.PEM_PRIVATE_KEY for span in scan.spans
            ):
                return ScreeningResult(
                    ScreeningState.COMPLETE,
                    None,
                    categories,
                    detection_counts,
                    provenance_text,
                    _DETECTED_CONTENT,
                    excluded=True,
                )

            sanitized = _redact(content, scan.spans)
            if metadata_spans:
                sanitized_provenance = _redact(provenance or "", metadata_spans)
                if not _has_safe_context(sanitized_provenance):
                    return ScreeningResult(
                        ScreeningState.COMPLETE,
                        None,
                        categories,
                        detection_counts,
                        None,
                        _DETECTED_CONTENT,
                        excluded=True,
                    )
                provenance_text = sanitized_provenance

            detected = bool(all_spans)
            if reject_detected and detected:
                return ScreeningResult(
                    ScreeningState.COMPLETE,
                    None,
                    categories,
                    detection_counts,
                    provenance_text,
                    _DETECTED_CONTENT,
                    blocked=True,
                )

            if scan.spans and not _has_safe_context(sanitized):
                return ScreeningResult(
                    ScreeningState.COMPLETE,
                    None,
                    categories,
                    detection_counts,
                    provenance_text,
                    _DETECTED_CONTENT,
                    excluded=True,
                )

            return ScreeningResult(
                ScreeningState.COMPLETE,
                sanitized,
                categories,
                detection_counts,
                provenance_text,
                _DETECTED_CONTENT if detected else _SCREENING_COMPLETE,
            )
        except Exception:
            return self._failure()

    def screen_diff_side(
        self,
        content: str | bytes,
        *,
        side: str,
        provenance: str | None = None,
    ) -> ScreeningResult:
        if side not in {"old", "new"}:
            return self._failure()
        return self._screen(content, provenance=provenance)

    def screen_evidence_item(
        self,
        content: str | bytes,
        *,
        provenance: str | None = None,
    ) -> ScreeningResult:
        return self._screen(content, provenance=provenance)

    def screen_repository_path(self, path: str) -> ScreeningResult:
        return self.screen_text(path, provenance="repository path")

    def screen_diagnostic(self, diagnostic: str) -> ScreeningResult:
        return self.screen_text(diagnostic, provenance="diagnostic")

    def screen_request_context(
        self,
        context: str | bytes,
        *,
        provenance: str | None = None,
    ) -> ScreeningResult:
        return self._screen(context, provenance=provenance)

    def screen_candidate(self, candidate: str | bytes) -> ScreeningResult:
        return self._screen(candidate, reject_detected=True)

    def screen_output(self, output: str | bytes) -> ScreeningResult:
        return self._screen(output, reject_detected=True)

    def _screen(
        self,
        content: str | bytes,
        *,
        provenance: str | None = None,
        reject_detected: bool = False,
    ) -> ScreeningResult:
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8", errors="strict")
            except Exception:
                return self._failure()
        return self.screen_text(
            content,
            provenance=provenance,
            reject_detected=reject_detected,
        )

    def _screen_provenance(
        self,
        provenance: str | None,
    ) -> tuple[ScreeningState, str | None, tuple[_SensitiveSpan, ...]]:
        if provenance is None:
            return ScreeningState.COMPLETE, None, ()
        if not isinstance(provenance, str):
            return ScreeningState.INCONCLUSIVE, None, ()
        scan = _find_sensitive_spans(provenance)
        if scan.uncertain_boundary or scan.exclude_item:
            return ScreeningState.INCONCLUSIVE, None, ()
        if any(span.category is DetectionCategory.PEM_PRIVATE_KEY for span in scan.spans):
            return ScreeningState.INCONCLUSIVE, None, ()
        sanitized = _redact(provenance, scan.spans)
        return ScreeningState.COMPLETE, sanitized, scan.spans

    @staticmethod
    def _failure() -> ScreeningResult:
        return ScreeningResult(
            ScreeningState.INCONCLUSIVE,
            None,
            (),
            (),
            None,
            _SCREENING_FAILURE,
            excluded=True,
        )