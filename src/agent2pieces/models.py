"""Typed domain records stored by the Agent2Pieces ledger."""

from __future__ import annotations

import re
import uuid
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMPORT_ID_PATTERN = re.compile(r"^[a-z2-7]{26}$")


class SourceAgent(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    HERMES = "hermes"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXCLUDED = "excluded"
    SUPERSEDED = "superseded"
    IMPORTED = "imported"


class GroupStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    EXCLUDED = "excluded"
    IMPORTED = "imported"


class ScanState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportItemState(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    REMOTE_DUPLICATE = "remote_duplicate"
    IMPORTED = "imported"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    SKIPPED = "skipped"


class AttemptKind(StrEnum):
    MARKER_PREFLIGHT = "marker_preflight"
    WRITE = "write"
    MARKER_RECHECK = "marker_recheck"


class AttemptState(StrEnum):
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class SourceDisposition(StrEnum):
    EXCLUDED = "excluded"
    QUARANTINED = "quarantined"


class FindingSeverity(StrEnum):
    BLOCK = "block"
    WARN = "warn"


class FindingState(StrEnum):
    OPEN = "open"
    CLEARED = "cleared"
    OVERRIDDEN = "overridden"


class ResumeAction(StrEnum):
    RECHECK = "recheck"
    RETRY = "retry"
    SKIP = "skip"


class LedgerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidatePayload(LedgerModel):
    title: Annotated[str, Field(min_length=1, max_length=240)]
    markdown_body: str
    external_links: list[str] = Field(default_factory=list)
    project_scope: Annotated[str, Field(max_length=512)] = ""

    @field_validator("title", "markdown_body", "project_scope")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL characters are not permitted")
        return value

    @field_validator("markdown_body")
    @classmethod
    def validate_body_bytes(cls, value: str) -> str:
        if not value:
            raise ValueError("markdown body must not be empty")
        if len(value.encode("utf-8")) > 65_536:
            raise ValueError("markdown body exceeds 65536 UTF-8 bytes")
        return value

    @field_validator("external_links")
    @classmethod
    def validate_links(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("external links must be absolute credential-free HTTP(S) URLs")
        return values


class SourceRevisionIdentity(LedgerModel):
    agent: SourceAgent
    root_id: str
    source_key: Annotated[str, Field(min_length=1)]
    source_hash: str
    candidate_input_hash: str

    @field_validator("root_id")
    @classmethod
    def validate_uuid4(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except ValueError as error:
            raise ValueError("root_id must be a canonical UUID4") from error
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("root_id must be a canonical UUID4")
        return value

    @field_validator("source_hash", "candidate_input_hash")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("hash must be lowercase SHA-256 hex")
        return value


class SettingsRecord(LedgerModel):
    version: int
    ledger_instance_id: str
    mcp_base_url: str
    codex_enabled: bool
    claude_enabled: bool
    hermes_enabled: bool
    created_at: str
    updated_at: str


class SourceRevisionRecord(LedgerModel):
    revision_id: str
    predecessor_revision_id: str | None
    agent: SourceAgent
    root_id: str
    source_key: str
    source_path: str
    source_hash: str
    candidate_input_hash: str
    first_observed_at: str


class SourceObservationRecord(LedgerModel):
    observation_id: str
    revision_id: str
    scan_id: str
    source_updated_at: str
    file_mtime_ns: int
    raw_byte_count: int
    observed_at: str


class SourceDispositionRecord(LedgerModel):
    disposition_id: str
    scan_id: str
    agent: SourceAgent
    root_id: str
    source_path: str
    source_key: str | None
    disposition: SourceDisposition
    byte_count: int
    reason: str
    detail: str | None
    created_at: str


class SafetyFindingRecord(LedgerModel):
    finding_id: str
    candidate_id: str
    candidate_version: int
    reason_code: str
    severity: FindingSeverity
    line_number: int | None
    state: FindingState
    override_reason: str | None
    override_at: str | None
    created_at: str


class HostPathMappingRecord(LedgerModel):
    mapping_id: str
    local_root: str
    host_root: str
    project: str
    created_at: str


class CandidateRecord(LedgerModel):
    candidate_id: str
    revision_id: str
    status: CandidateStatus
    version: int
    original_payload_json: str
    current_payload_json: str
    payload_hash: str
    import_id: str
    group_id: str | None
    created_at: str
    updated_at: str
    superseded_at: str | None

    @property
    def payload(self) -> CandidatePayload:
        return CandidatePayload.model_validate_json(self.current_payload_json)


class ScanRecord(LedgerModel):
    scan_id: str
    state: ScanState
    requested_at: str
    started_at: str | None
    finished_at: str | None
    settings_version: int
    discovered_count: int
    accepted_count: int
    excluded_count: int
    quarantine_count: int
    error_count: int
    error_detail: str | None


class ReviewGroupRecord(LedgerModel):
    group_id: str
    title: str
    representative_candidate_id: str
    status: GroupStatus
    version: int
    created_from_check_id: str | None
    representative_overridden: bool
    created_at: str
    updated_at: str


class ImportJobRecord(LedgerModel):
    job_id: str
    state: ImportJobState
    requested_at: str
    started_at: str | None
    finished_at: str | None
    current_ordinal: int
    remote_search_available: bool
    duplicate_risk_acknowledged_at: str | None
    duplicate_risk_ack_text_version: str | None
    pieces_endpoint: str
    context_hash: str
    pause_reason: str | None
    error_detail: str | None


class ImportItemRecord(LedgerModel):
    item_id: str
    job_id: str
    ordinal: int
    candidate_id: str
    candidate_version: int
    frozen_payload_json: str
    frozen_write_arguments_json: str
    import_id: str
    state: ImportItemState
    attempt_count: int
    pieces_memory_id: str | None
    completed_at: str | None
    error_detail: str | None


class ImportAttemptRecord(LedgerModel):
    attempt_id: str
    item_id: str
    attempt_number: int
    kind: AttemptKind
    state: AttemptState
    marker: str
    pieces_endpoint: str
    started_at: str
    dispatch_started_at: str | None
    finished_at: str | None
    marker_outcome: str | None
    parent_memory_id: str | None
    response_id: str | None
    error_detail: str | None


class NormalizedCandidate(LedgerModel):
    """Public, immutable source candidate produced by an adapter."""

    source_agent: SourceAgent
    source_key: str
    source_path: str
    project_scope: str
    source_updated_at: str
    title: str
    markdown_body: str
    external_links: list[str]
    source_hash: str
    payload_hash: str
    import_id: str


class CandidateInput(LedgerModel):
    """Normalized candidate plus its private source-revision identity hash."""

    candidate: NormalizedCandidate
    candidate_input_hash: str


class ScanCounts(LedgerModel):
    discovered: int = 0
    accepted: int = 0
    excluded: int = 0
    quarantined: int = 0


class ScanDisposition(LedgerModel):
    disposition: SourceDisposition
    reason: str
    source_path: str
    source_key: str | None = None
    byte_count: int = 0


class AdapterScanResult(LedgerModel):
    candidates: tuple[CandidateInput, ...] = ()
    dispositions: tuple[ScanDisposition, ...] = ()
    counts: ScanCounts = ScanCounts()


class SafetyFinding(LedgerModel):
    """Safe finding metadata; matched values are deliberately absent."""

    reason_code: str
    severity: FindingSeverity
    line_number: int
