from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path


class DiffScope(StrEnum):
    WORKING_TREE = "working-tree"
    STAGED = "staged"
    BASE_HEAD = "base-head"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    EMPTY_DIFF = "empty_diff"
    INVALID_INPUT = "invalid_input"
    LIMIT_EXCEEDED = "limit_exceeded"
    SCREENING_FAILED = "screening_failed"
    VALIDATION_FAILED = "validation_failed"
    PARTIAL_ARTIFACTS = "partial_artifacts"
    FAILED = "failed"


class StageStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    WARNING = "warning"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


class ValidatorStatus(StrEnum):
    PASS = auto()
    FAIL = auto()
    WARNING = auto()
    INCONCLUSIVE = auto()


class ArtifactStatus(StrEnum):
    WRITTEN = "written"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: str
    status: StageStatus
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class SkippedInput:
    path: str
    reason: str
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class Proposal:
    target_path: str
    replacement: str
    source_paths: tuple[str, ...]
    sanitized_reason: str


@dataclass(frozen=True, slots=True)
class ArtifactOutcome:
    name: str
    path: Path
    status: ArtifactStatus
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class ValidatorResult:
    validator: str
    status: ValidatorStatus
    sanitized_detail: str
    evidence_refs: tuple[str, ...] = ()