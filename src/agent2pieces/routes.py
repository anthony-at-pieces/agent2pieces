"""Exact internal JSON API for the local review application."""

from __future__ import annotations

import difflib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent2pieces.config import validate_pieces_url
from agent2pieces.ledger import (
    ImmutableRecordError,
    InvalidStateError,
    Ledger,
    OptimisticConflictError,
)
from agent2pieces.models import (
    CandidatePayload,
    CandidateRecord,
    CandidateStatus,
    FindingSeverity,
    FindingState,
    ImportJobRecord,
    SourceAgent,
)
from agent2pieces.safety import scan_safety
from agent2pieces.services import (
    ApplyConfirmation,
    ImportInProgressError,
    ImportService,
    ReviewService,
    ScanInProgressError,
    ScanRoot,
    ScanService,
    coalesce_scan_roots,
)

MAX_DUPLICATE_CANDIDATES = 500
_TRANSIENT_ROOT_PREFIX = "agent2pieces-transient:"


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    ledger: Ledger
    scan_service: ScanService
    review_service: ReviewService
    import_service: ImportService
    pieces_client: Any
    csrf_token: str
    effective_mcp_base_url: str
    mcp_base_url_source: Literal["cli", "saved"]
    command_roots: tuple[tuple[str, Path], ...]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanRootRequest(ApiModel):
    agent: SourceAgent
    path: Path

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("source root must be absolute")
        return value


class StartScanRequest(ApiModel):
    settings_version: Annotated[int, Field(ge=1)]
    roots: list[ScanRootRequest] = Field(default_factory=list, max_length=100)


class CandidateMutationRequest(ApiModel):
    version: Annotated[int, Field(ge=1)]
    action: Literal[
        "save",
        "approve",
        "exclude",
        "group_create",
        "group_join",
        "group_leave",
        "set_representative",
        "override_findings",
    ]
    title: str | None = None
    markdown_body: str | None = None
    external_links: list[str] | None = None
    project_scope: str | None = None
    target: Literal["candidate", "group"] | None = None
    group_id: str | None = None
    group_version: Annotated[int, Field(ge=1)] | None = None
    duplicate_check_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=1000)
    finding_ids: list[str] = Field(default_factory=list, max_length=1000)
    finding_acknowledged: bool = False
    override_reason: Annotated[str, Field(min_length=10, max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> CandidateMutationRequest:
        edit_supplied = any(
            value is not None
            for value in (
                self.title,
                self.markdown_body,
                self.external_links,
                self.project_scope,
            )
        )
        if self.action == "save":
            if not edit_supplied:
                raise ValueError("save requires at least one editable field")
            return self
        if edit_supplied:
            raise ValueError("editable fields are accepted only with save")
        if self.action in {"approve", "exclude"}:
            if self.target is None:
                raise ValueError("approve and exclude require a target")
            if self.target == "group" and (
                self.group_id is None or self.group_version is None
            ):
                raise ValueError("group actions require a group ID and version")
        elif self.action == "group_create":
            if self.duplicate_check_id is None or not self.evidence_ids:
                raise ValueError("group creation requires duplicate evidence")
        elif self.action == "group_join":
            if (
                self.target != "group"
                or self.group_id is None
                or self.group_version is None
                or self.duplicate_check_id is None
                or not self.evidence_ids
            ):
                raise ValueError("group join requires a group and duplicate evidence")
        elif self.action == "group_leave":
            if self.target != "group" or self.group_id is None or self.group_version is None:
                raise ValueError("group leave requires a group ID and version")
        elif self.action == "set_representative":
            if self.target != "group" or self.group_id is None or self.group_version is None:
                raise ValueError("representative selection requires a group ID and version")
        elif self.action == "override_findings":
            if (
                not self.finding_ids
                or not self.finding_acknowledged
                or self.override_reason is None
            ):
                raise ValueError("finding override requires IDs, acknowledgement, and reason")
        return self


class DuplicateCheckRequest(ApiModel):
    candidate_ids: list[str] = Field(max_length=MAX_DUPLICATE_CANDIDATES)
    candidate_versions: dict[str, Annotated[int, Field(ge=1)]]


class ApplyConfirmationRequest(ApiModel):
    confirmed: bool
    pieces_endpoint: Annotated[str, Field(min_length=1, max_length=2048)]
    selected_write_count: Annotated[int, Field(ge=0, le=MAX_DUPLICATE_CANDIDATES)]
    context_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ImportJobRequest(ApiModel):
    action: Literal["preview", "start", "resume"]
    candidate_ids: list[str] = Field(default_factory=list, max_length=MAX_DUPLICATE_CANDIDATES)
    candidate_versions: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    payload_hashes: dict[str, str] = Field(default_factory=dict)
    confirmation: ApplyConfirmationRequest | None = None
    acknowledge_remote_duplicate_risk: bool = False
    resume_job_id: str | None = None
    resolution: Literal["recheck", "retry", "skip"] | None = None
    acknowledge_duplicate_write_risk: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> ImportJobRequest:
        if self.action in {"preview", "start"}:
            if (
                (self.action == "start" and self.confirmation is None)
                or (self.action == "preview" and self.confirmation is not None)
                or self.resume_job_id is not None
                or self.resolution is not None
                or self.acknowledge_duplicate_write_risk
                or not self.candidate_ids
            ):
                raise ValueError("preview/start candidate context is invalid")
            if len(set(self.candidate_ids)) != len(self.candidate_ids):
                raise ValueError("candidate IDs must be unique")
        elif (
            self.resume_job_id is None
            or self.resolution is None
            or self.candidate_ids
            or self.candidate_versions
            or self.payload_hashes
            or self.confirmation is not None
            or self.acknowledge_remote_duplicate_risk
        ):
            raise ValueError("resume requires only a job and resolution")
        return self


class SourceRootSetting(ApiModel):
    agent: SourceAgent
    path: Path
    enabled: bool = True

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("source root must be absolute")
        return value


class HostPathMappingSetting(ApiModel):
    local_root: Path
    host_root: Annotated[str, Field(min_length=1, max_length=2048)]
    project: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("local_root")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("local mapping root must be absolute")
        return value


class SettingsRequest(ApiModel):
    version: Annotated[int, Field(ge=1)]
    source_roots: list[SourceRootSetting] = Field(max_length=500)
    host_path_mappings: list[HostPathMappingSetting] = Field(max_length=500)
    mcp_base_url: Annotated[str, Field(min_length=1, max_length=2048)]

    @field_validator("mcp_base_url")
    @classmethod
    def valid_mcp_url(cls, value: str) -> str:
        return validate_pieces_url(value)

    @model_validator(mode="after")
    def unique_paths(self) -> SettingsRequest:
        roots = [
            (root.agent, str(root.path.expanduser().resolve(strict=False)))
            for root in self.source_roots
        ]
        if len(set(roots)) != len(roots):
            raise ValueError("source roots must be unique")
        mappings = [
            str(mapping.local_root.expanduser().resolve(strict=False))
            for mapping in self.host_path_mappings
        ]
        if len(set(mappings)) != len(mappings):
            raise ValueError("host path mappings must be unique")
        return self


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid4() -> str:
    return str(uuid.uuid4())


def _api_error(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _valid_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _security_state(rows: list[Any]) -> str:
    if any(
        row["state"] == FindingState.OPEN and row["severity"] == FindingSeverity.BLOCK
        for row in rows
    ):
        return "blocked"
    if any(
        row["state"] == FindingState.OPEN and row["severity"] == FindingSeverity.WARN
        for row in rows
    ):
        return "warning"
    if any(row["state"] == FindingState.OVERRIDDEN for row in rows):
        return "overridden"
    return "clean"


def _redact_secret_lines(value: str) -> str:
    output: list[str] = []
    for line in value.splitlines():
        findings = scan_safety(title="line", markdown_body=line or " ")
        blocking = next(
            (finding for finding in findings if finding.severity is FindingSeverity.BLOCK),
            None,
        )
        output.append(f"[redacted:{blocking.reason_code}]" if blocking is not None else line)
    return "\n".join(output)


def _diff_hunks(old_body: str, new_body: str) -> list[dict[str, Any]]:
    old_lines = old_body.splitlines()
    new_lines = new_body.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[dict[str, Any]] = []
    for opcodes in matcher.get_grouped_opcodes(3):
        lines: list[dict[str, str]] = []
        for tag, old_start, old_end, new_start, new_end in opcodes:
            if tag == "equal":
                lines.extend(
                    {"op": "context", "text": line}
                    for line in old_lines[old_start:old_end]
                )
            if tag in {"delete", "replace"}:
                lines.extend(
                    {"op": "delete", "text": line}
                    for line in old_lines[old_start:old_end]
                )
            if tag in {"insert", "replace"}:
                lines.extend(
                    {"op": "insert", "text": line}
                    for line in new_lines[new_start:new_end]
                )
        first = opcodes[0]
        last = opcodes[-1]
        hunks.append(
            {
                "old_start": first[1] + 1,
                "old_count": last[2] - first[1],
                "new_start": first[3] + 1,
                "new_count": last[4] - first[3],
                "lines": lines,
            }
        )
    return hunks


def _duplicate_verdict(evidence: list[dict[str, Any]]) -> str:
    priority = {"exact": 0, "likely": 1, "possible": 2, "distinct": 3}
    return min(
        (str(item["classification"]) for item in evidence),
        key=lambda value: priority.get(value, 4),
        default="distinct",
    )


def _group_json(ledger: Ledger, group_id: str | None) -> dict[str, Any]:
    if group_id is None:
        return {
            "group_id": None,
            "version": None,
            "status": None,
            "representative_candidate_id": None,
        }
    group = ledger._get_review_group(group_id)
    return {
        "group_id": group.group_id,
        "version": group.version,
        "status": group.status,
        "representative_candidate_id": group.representative_candidate_id,
    }


def _finding_rows(ledger: Ledger, candidate_id: str) -> list[Any]:
    return list(
        ledger.connection.execute(
            "SELECT * FROM safety_findings WHERE candidate_id = ? "
            "AND state IN (?, ?) ORDER BY created_at, finding_id",
            (candidate_id, FindingState.OPEN, FindingState.OVERRIDDEN),
        ).fetchall()
    )


def _candidate_json(
    ledger: Ledger,
    candidate: CandidateRecord,
    *,
    include_changed_source: bool = False,
) -> dict[str, Any]:
    source = ledger.connection.execute(
        "SELECT r.agent, r.source_key, r.source_path, r.source_hash, "
        "o.source_updated_at FROM source_revisions r "
        "LEFT JOIN source_observations o ON o.revision_id = r.revision_id "
        "WHERE r.revision_id = ? ORDER BY o.observed_at DESC LIMIT 1",
        (candidate.revision_id,),
    ).fetchone()
    if source is None:
        raise KeyError("source revision not found")
    findings = _finding_rows(ledger, candidate.candidate_id)
    payload = candidate.payload
    display_body = (
        _redact_secret_lines(payload.markdown_body)
        if _security_state(findings) == "blocked"
        else payload.markdown_body
    )
    prior_row = ledger.connection.execute(
        "SELECT s.state, s.candidate_version, s.payload_json, s.recorded_at "
        "FROM candidate_payload_snapshots s "
        "JOIN source_revisions prior ON prior.revision_id = s.source_revision_id "
        "JOIN source_revisions current ON current.revision_id = ? "
        "WHERE prior.agent = current.agent AND prior.root_id = current.root_id "
        "AND prior.source_key = current.source_key AND s.state IN (?, ?) "
        "AND NOT (s.candidate_id = ? AND s.candidate_version >= ?) "
        "ORDER BY s.recorded_at DESC, s.snapshot_id DESC LIMIT 1",
        (
            candidate.revision_id,
            CandidateStatus.APPROVED,
            CandidateStatus.IMPORTED,
            candidate.candidate_id,
            candidate.version,
        ),
    ).fetchone()
    prior: dict[str, Any] | None = None
    changed: dict[str, Any] | None = None
    if prior_row is not None:
        prior_payload = CandidatePayload.model_validate_json(str(prior_row["payload_json"]))
        prior_body = _redact_secret_lines(prior_payload.markdown_body)
        prior = {
            "state": str(prior_row["state"]),
            "candidate_version": int(prior_row["candidate_version"]),
            "recorded_at": str(prior_row["recorded_at"]),
            "payload": {
                "title": prior_payload.title,
                "markdown_body": prior_body,
                "external_links": prior_payload.external_links,
                "project_scope": prior_payload.project_scope,
            },
        }
        if include_changed_source:
            current_body = _redact_secret_lines(payload.markdown_body)
            hunks = _diff_hunks(prior_body, current_body)
            if not hunks and prior_payload.markdown_body != payload.markdown_body:
                redacted_lines = current_body.splitlines()
                hunks = [
                    {
                        "old_start": 1,
                        "old_count": len(redacted_lines),
                        "new_start": 1,
                        "new_count": len(redacted_lines),
                        "lines": [
                            {"op": "context", "text": line}
                            for line in redacted_lines
                        ],
                    }
                ]
            changed = {
                "available": True,
                "hunks": hunks,
            }
    evidence_rows = ledger.connection.execute(
        "SELECT e.*, owner.target_key AS owner_target_key, "
        "owner.target_title AS owner_target_title "
        "FROM duplicate_evidence_candidates owner "
        "JOIN duplicate_evidence e ON e.evidence_id = owner.evidence_id "
        "JOIN duplicate_checks d ON d.check_id = e.check_id "
        "WHERE owner.candidate_id = ? AND owner.candidate_version = ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM duplicate_evidence_candidates peer "
        "JOIN candidates current ON current.candidate_id = peer.candidate_id "
        "WHERE peer.evidence_id = owner.evidence_id "
        "AND current.version != peer.candidate_version"
        ") "
        "ORDER BY d.started_at DESC, e.evidence_id LIMIT 50",
        (candidate.candidate_id, candidate.version),
    ).fetchall()
    evidence = [
        {
            "target_kind": str(row["target_kind"]),
            "target_key": str(row["owner_target_key"]),
            "target_title": row["owner_target_title"],
            "target_excerpt": row["target_excerpt"],
            "classification": str(row["classification"]),
            "rule_id": str(row["rule_id"]),
            "cosine": row["cosine"],
            "body_shingle_jaccard": row["body_shingle_jaccard"],
            "title_jaccard": row["title_jaccard"],
            "remote_rank": row["remote_rank"],
        }
        for row in evidence_rows
    ]
    verdict = _duplicate_verdict(evidence)
    return {
        "candidate_id": candidate.candidate_id,
        "version": candidate.version,
        "status": candidate.status,
        "source": {
            "agent": str(source["agent"]),
            "key": str(source["source_key"]),
            "path": str(source["source_path"]),
            "updated_at": source["source_updated_at"],
            "hash": str(source["source_hash"]),
        },
        "project_scope": payload.project_scope,
        "title": payload.title,
        "markdown_body": display_body,
        "external_links": payload.external_links,
        "payload_hash": candidate.payload_hash,
        "import_id": candidate.import_id,
        "group": _group_json(ledger, candidate.group_id),
        "safety": {
            "approval_checked": not any(row["state"] == FindingState.OPEN for row in findings),
            "findings": [
                {
                    "finding_id": str(row["finding_id"]),
                    "reason_code": str(row["reason_code"]),
                    "severity": str(row["severity"]),
                    "line": row["line_number"],
                    "state": str(row["state"]),
                }
                for row in findings
            ],
        },
        "prior_snapshot": prior,
        "changed_source": changed if include_changed_source else None,
        "duplicate": {"coverage": "local", "verdict": verdict, "evidence": evidence},
    }


def _scan_json(ledger: Ledger, scan_id: str) -> dict[str, Any]:
    try:
        record = ledger.get_scan(scan_id)
    except KeyError:
        _api_error(404, "not_found", "Scan not found.")
    rows = ledger.connection.execute(
        "SELECT agent, disposition, reason, COUNT(*) AS count FROM source_dispositions "
        "WHERE scan_id = ? GROUP BY agent, disposition, reason",
        (scan_id,),
    ).fetchall()
    accepted_rows = ledger.connection.execute(
        "SELECT r.agent, COUNT(*) AS count FROM source_observations o "
        "JOIN source_revisions r ON r.revision_id = o.revision_id "
        "WHERE o.scan_id = ? GROUP BY r.agent",
        (scan_id,),
    ).fetchall()
    by_agent: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        agent = str(row["agent"])
        counts = by_agent.setdefault(
            agent, {"discovered": 0, "accepted": 0, "excluded": 0, "quarantined": 0}
        )
        count = int(row["count"])
        counts["discovered"] += count
        counts["quarantined" if row["disposition"] == "quarantined" else "excluded"] += count
        reasons[str(row["reason"])] = reasons.get(str(row["reason"]), 0) + count
    for row in accepted_rows:
        agent = str(row["agent"])
        counts = by_agent.setdefault(
            agent, {"discovered": 0, "accepted": 0, "excluded": 0, "quarantined": 0}
        )
        count = int(row["count"])
        counts["accepted"] += count
        counts["discovered"] += count
    return {
        "scan_id": record.scan_id,
        "state": record.state,
        "requested_at": record.requested_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "counts": {
            "discovered": record.discovered_count,
            "accepted": record.accepted_count,
            "excluded": record.excluded_count,
            "quarantined": record.quarantine_count,
            "errors": record.error_count,
        },
        "by_agent": by_agent,
        "disposition_by_reason": reasons,
        "error": record.error_detail,
    }


def _attempt_duration_ms(started_at: str, finished_at: str | None) -> int | None:
    if finished_at is None:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((finished - started).total_seconds() * 1_000))


def _attempt_telemetry(ledger: Ledger, item: Any) -> list[dict[str, Any]]:
    telemetry: list[dict[str, Any]] = []
    for attempt in ledger.list_import_attempts(item.item_id):
        is_search = attempt.kind.value in {"marker_preflight", "marker_recheck"}
        if attempt.kind.value == "marker_recheck":
            recovery_state = "recheck"
        elif attempt.state.value == "ambiguous":
            recovery_state = "required"
        elif attempt.parent_memory_id is not None:
            recovery_state = "recovered"
        else:
            recovery_state = "none"
        retry_state = (
            "retries_exhausted"
            if is_search and attempt.marker_outcome == "search_error"
            else "subsequent"
            if attempt.attempt_number > 1
            else "initial"
        )
        telemetry.append(
            {
                "attempt_number": attempt.attempt_number,
                "tool": (
                    "annotations_full_text_search" if is_search else "create_pieces_memory"
                ),
                "endpoint": attempt.pieces_endpoint,
                "state": attempt.state,
                "duration_ms": _attempt_duration_ms(
                    attempt.started_at, attempt.finished_at
                ),
                "retry_state": retry_state,
                "recovery_state": recovery_state,
                "error": attempt.error_detail,
            }
        )
    return telemetry


def _job_json(ledger: Ledger, record: ImportJobRecord) -> dict[str, Any]:
    def item_json(item: Any) -> dict[str, Any]:
        source = ledger.connection.execute(
            "SELECT r.agent, r.source_key FROM source_revisions r "
            "JOIN candidates c ON c.revision_id = r.revision_id WHERE c.candidate_id = ?",
            (item.candidate_id,),
        ).fetchone()
        return {
            "ordinal": item.ordinal,
            "candidate_id": item.candidate_id,
            "candidate_version": item.candidate_version,
            "import_id": item.import_id,
            "source": (
                {
                    "agent": str(source["agent"]),
                    "source_key": str(source["source_key"]),
                }
                if source is not None
                else None
            ),
            "state": item.state,
            "attempts": item.attempt_count,
            "write_context": _write_context(item.frozen_write_arguments_json),
            "attempt_telemetry": _attempt_telemetry(ledger, item),
            "pieces_memory_id": item.pieces_memory_id,
            "error": item.error_detail,
        }

    return {
        "job_id": record.job_id,
        "state": record.state,
        "pieces_endpoint": record.pieces_endpoint,
        "context_hash": record.context_hash,
        "current_ordinal": record.current_ordinal,
        "pause_reason": record.pause_reason,
        "items": [item_json(item) for item in ledger.list_import_items(record.job_id)],
    }


def _write_context(arguments_json: str) -> dict[str, Any]:
    try:
        arguments = json.loads(arguments_json)
    except (TypeError, ValueError):
        return {"project": None, "files": []}
    if not isinstance(arguments, dict):
        return {"project": None, "files": []}
    project = arguments.get("project")
    files = arguments.get("files")
    return {
        "project": project if isinstance(project, str) else None,
        "files": [str(value) for value in files] if isinstance(files, list) else [],
    }


def _settings_json(dependencies: ApiDependencies) -> dict[str, Any]:
    ledger = dependencies.ledger
    settings = ledger.get_settings()
    roots = ledger.connection.execute(
        "SELECT * FROM source_roots ORDER BY created_at, root_id"
    ).fetchall()
    mappings = ledger.connection.execute(
        "SELECT * FROM host_path_mappings ORDER BY created_at, mapping_id"
    ).fetchall()
    capabilities = dependencies.pieces_client.capabilities
    restart_required = dependencies.effective_mcp_base_url != settings.mcp_base_url
    return {
        "version": settings.version,
        "source_roots": [
            {
                "root_id": str(row["root_id"]),
                "agent": str(row["agent"]),
                "path": str(row["resolved_path"]),
                "enabled": bool(row["enabled"]),
                "is_default": bool(row["is_default"]),
            }
            for row in roots
            if not str(row["lexical_path"]).startswith(_TRANSIENT_ROOT_PREFIX)
        ],
        "host_path_mappings": [
            {
                "mapping_id": str(row["mapping_id"]),
                "local_root": str(row["local_root"]),
                "host_root": str(row["host_root"]),
                "project": str(row["project"]),
            }
            for row in mappings
        ],
        "mcp_base_url": settings.mcp_base_url,
        "effective_mcp_base_url": dependencies.effective_mcp_base_url,
        "mcp_base_url_source": dependencies.mcp_base_url_source,
        "mcp_base_url_restart_required": restart_required,
        "capabilities": {
            "status": "ready" if capabilities.import_ready else "blocked",
            "transport": capabilities.transport,
            "effective_endpoint": capabilities.endpoint,
            "create_pieces_memory": capabilities.import_ready,
            "annotations_full_text_search": capabilities.search_available,
            "checked_at": capabilities.checked_at,
            "error": capabilities.blocking_error,
        },
        "csrf_token": dependencies.csrf_token,
    }


def create_api_router(dependencies: ApiDependencies) -> APIRouter:
    """Create the one router containing the exact nine internal operations."""

    router = APIRouter()

    @router.post("/api/scans", status_code=202)
    async def start_scan(
        request: StartScanRequest, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        if request.settings_version != dependencies.ledger.get_settings().version:
            _api_error(409, "settings_version_conflict", "Settings changed before this scan.")
        roots: list[ScanRoot] = []
        stored = dependencies.ledger.connection.execute(
            "SELECT root_id, agent, resolved_path FROM source_roots WHERE enabled = 1"
        ).fetchall()
        roots.extend(
            ScanRoot(
                root_id=str(row["root_id"]),
                agent=SourceAgent(str(row["agent"])),
                path=Path(str(row["resolved_path"])),
            )
            for row in stored
        )
        extra = [(SourceAgent(agent), path) for agent, path in dependencies.command_roots]
        extra.extend((item.agent, item.path) for item in request.roots)
        for agent, path in extra:
            resolved = path.expanduser().resolve(strict=False)
            root_id = dependencies.ledger.add_source_root(
                agent=agent,
                lexical_path=_TRANSIENT_ROOT_PREFIX + str(path),
                resolved_path=str(resolved),
                enabled=False,
                is_default=False,
            )
            roots.append(ScanRoot(root_id=root_id, agent=agent, path=resolved))
        try:
            record = dependencies.scan_service.queue_scan()
        except ScanInProgressError:
            _api_error(409, "scan_in_progress", "A scan is already running.")
        background_tasks.add_task(
            dependencies.scan_service.run_scan,
            coalesce_scan_roots(roots),
            scan_id=record.scan_id,
        )
        return {"scan_id": record.scan_id, "state": record.state}

    @router.get("/api/scans/{scan_id}")
    async def get_scan(scan_id: str) -> dict[str, Any]:
        if not _valid_uuid4(scan_id):
            _api_error(404, "not_found", "Scan not found.")
        return _scan_json(dependencies.ledger, scan_id)

    @router.get("/api/candidates")
    async def get_candidates(
        scan_id: str | None = None,
        status: CandidateStatus | None = None,
        source_agent: SourceAgent | None = None,
        project: str | None = None,
        security_state: Literal["clean", "warning", "blocked", "overridden"] | None = None,
        group_id: str | None = None,
        duplicate_verdict: Literal["distinct", "possible", "likely", "exact"] | None = None,
        q: str | None = None,
        include_changed_source: bool = False,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        if include_changed_source and page_size > 10:
            _api_error(422, "validation_error", "Changed-source pages are limited to 10.")
        rows = dependencies.ledger.connection.execute(
            "SELECT c.candidate_id FROM candidates c ORDER BY c.created_at, c.candidate_id"
        ).fetchall()
        all_items = [
            _candidate_json(
                dependencies.ledger, dependencies.ledger.get_candidate(str(row["candidate_id"]))
            )
            for row in rows
        ]
        facets = {
            "projects": [
                {"value": value, "count": sum(item["project_scope"] == value for item in all_items)}
                for value in sorted(
                    {item["project_scope"] for item in all_items if item["project_scope"]}
                )
            ],
            "security_states": {
                name: sum(
                    _security_state(_finding_rows(dependencies.ledger, item["candidate_id"]))
                    == name
                    for item in all_items
                )
                for name in ("clean", "warning", "blocked", "overridden")
            },
        }
        items = all_items
        if scan_id is not None:
            observed = {
                str(row["candidate_id"])
                for row in dependencies.ledger.connection.execute(
                    "SELECT c.candidate_id FROM candidates c JOIN source_observations o "
                    "ON o.revision_id = c.revision_id WHERE o.scan_id = ?",
                    (scan_id,),
                ).fetchall()
            }
            items = [item for item in items if item["candidate_id"] in observed]
        if status is not None:
            items = [item for item in items if item["status"] == status]
        if source_agent is not None:
            items = [item for item in items if item["source"]["agent"] == source_agent]
        if project is not None:
            normalized_project = unicodedata.normalize("NFKC", project).strip()
            items = [
                item
                for item in items
                if unicodedata.normalize("NFKC", item["project_scope"]).strip()
                == normalized_project
            ]
        if security_state is not None:
            items = [
                item
                for item in items
                if _security_state(_finding_rows(dependencies.ledger, item["candidate_id"]))
                == security_state
            ]
        if group_id is not None:
            items = [item for item in items if item["group"]["group_id"] == group_id]
        if duplicate_verdict is not None:
            items = [
                item
                for item in items
                if item["duplicate"]["verdict"] == duplicate_verdict
            ]
        if q is not None:
            term = q.casefold()
            items = [
                item
                for item in items
                if term in item["title"].casefold()
                or term in item["project_scope"].casefold()
                or term in item["source"]["key"].casefold()
            ]
        total = len(items)
        start = (page - 1) * page_size
        page_items = [
            _candidate_json(
                dependencies.ledger,
                dependencies.ledger.get_candidate(item["candidate_id"]),
                include_changed_source=include_changed_source,
            )
            for item in items[start : start + page_size]
        ]
        return {
            "items": page_items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "facets": facets,
        }

    @router.patch("/api/candidates/{candidate_id}")
    async def mutate_candidate(
        candidate_id: str, request: CandidateMutationRequest
    ) -> dict[str, Any]:
        if not _valid_uuid4(candidate_id):
            _api_error(404, "not_found", "Candidate not found.")
        try:
            current = dependencies.ledger.get_candidate(candidate_id)
        except KeyError:
            _api_error(404, "not_found", "Candidate not found.")
        try:
            if request.action == "save":
                payload = current.payload.model_copy(
                    update={
                        key: value
                        for key, value in {
                            "title": request.title,
                            "markdown_body": request.markdown_body,
                            "external_links": request.external_links,
                            "project_scope": request.project_scope,
                        }.items()
                        if value is not None
                    }
                )
                payload = CandidatePayload.model_validate(payload)
                current = dependencies.review_service.edit_candidate(
                    candidate_id=candidate_id,
                    expected_version=request.version,
                    payload=payload,
                )
            elif request.action == "override_findings":
                _override_findings(dependencies.ledger, candidate_id, request)
                current = dependencies.ledger.get_candidate(candidate_id)
            elif request.action == "group_create":
                assert request.duplicate_check_id is not None
                group = dependencies.review_service.create_group_from_evidence(
                    check_id=request.duplicate_check_id,
                    candidate_id=candidate_id,
                    evidence_ids=request.evidence_ids,
                )
                current = dependencies.ledger.get_candidate(candidate_id)
                result = _candidate_json(dependencies.ledger, current)
                result["group"] = _group_json(dependencies.ledger, group.group_id)
                return result
            elif request.action == "group_join":
                assert request.group_id is not None
                assert request.group_version is not None
                assert request.duplicate_check_id is not None
                group = dependencies.review_service.join_group(
                    group_id=request.group_id,
                    expected_group_version=request.group_version,
                    candidate_id=candidate_id,
                    expected_candidate_version=request.version,
                    check_id=request.duplicate_check_id,
                    evidence_ids=request.evidence_ids,
                )
                current = dependencies.ledger.get_candidate(candidate_id)
                result = _candidate_json(dependencies.ledger, current)
                result["group"] = _group_json(dependencies.ledger, group.group_id)
                return result
            elif request.action == "group_leave":
                assert request.group_id is not None
                assert request.group_version is not None
                dependencies.review_service.leave_group(
                    group_id=request.group_id,
                    expected_group_version=request.group_version,
                    candidate_id=candidate_id,
                    expected_candidate_version=request.version,
                )
                current = dependencies.ledger.get_candidate(candidate_id)
                return _candidate_json(dependencies.ledger, current)
            elif (
                request.action in {"set_representative", "approve", "exclude"}
                and request.target == "group"
            ):
                if request.group_id is None or request.group_version is None:
                    _api_error(422, "validation_error", "Group ID and version are required.")
                group = dependencies.review_service.update_group(
                    group_id=request.group_id,
                    expected_version=request.group_version,
                    action=request.action,
                    representative_candidate_id=(
                        candidate_id if request.action == "set_representative" else None
                    ),
                    acting_candidate_id=(
                        candidate_id if request.action in {"approve", "exclude"} else None
                    ),
                    expected_candidate_version=(
                        request.version
                        if request.action in {"approve", "exclude"}
                        else None
                    ),
                )
                current = dependencies.ledger.get_candidate(candidate_id)
                result = _candidate_json(dependencies.ledger, current)
                result["group"] = _group_json(dependencies.ledger, group.group_id)
                return result
            elif request.action == "approve" and request.target == "candidate":
                current = dependencies.review_service.approve_candidate(
                    candidate_id=candidate_id,
                    expected_version=request.version,
                )
            elif request.action == "exclude" and request.target == "candidate":
                current = dependencies.review_service.exclude_candidate(
                    candidate_id=candidate_id,
                    expected_version=request.version,
                )
            else:
                _api_error(422, "validation_error", "The candidate action is incomplete.")
        except OptimisticConflictError:
            _api_error(409, "candidate_version_conflict", "The candidate or group changed.")
        except ImmutableRecordError:
            _api_error(409, "immutable_candidate", "The candidate is immutable.")
        except InvalidStateError:
            _api_error(409, "invalid_candidate_state", "The candidate action is not allowed.")
        return _candidate_json(dependencies.ledger, current)

    @router.post("/api/duplicates/check-pieces")
    async def check_duplicates(request: DuplicateCheckRequest) -> dict[str, Any]:
        candidate_ids = request.candidate_ids
        versions = request.candidate_versions
        if not candidate_ids:
            records = dependencies.ledger.connection.execute(
                "SELECT candidate_id, version FROM candidates WHERE status IN (?, ?) "
                "ORDER BY created_at LIMIT ?",
                (CandidateStatus.PENDING, CandidateStatus.APPROVED, MAX_DUPLICATE_CANDIDATES),
            ).fetchall()
            candidate_ids = [str(row["candidate_id"]) for row in records]
            versions = {str(row["candidate_id"]): int(row["version"]) for row in records}
        if not candidate_ids:
            _api_error(422, "validation_error", "No candidates are available to check.")
        try:
            report = await dependencies.review_service.check_duplicates(
                candidate_ids=candidate_ids,
                candidate_versions=versions,
            )
        except KeyError:
            _api_error(404, "not_found", "Candidate not found.")
        except OptimisticConflictError:
            _api_error(409, "candidate_version_conflict", "A candidate changed.")
        results = []
        for candidate_id in candidate_ids:
            evidence_ids = report.evidence_by_candidate.get(candidate_id, ())
            placeholders = ",".join("?" for _ in evidence_ids)
            rows = (
                dependencies.ledger.connection.execute(
                    "SELECT e.*, owner.target_key AS owner_target_key "
                    "FROM duplicate_evidence_candidates owner "
                    "JOIN duplicate_evidence e ON e.evidence_id = owner.evidence_id "
                    "WHERE owner.candidate_id = ? AND owner.candidate_version = ? "
                    "AND e.evidence_id IN ("
                    + placeholders
                    + ") ORDER BY e.evidence_id",
                    (candidate_id, versions[candidate_id], *evidence_ids),
                ).fetchall()
                if evidence_ids
                else []
            )
            result_evidence = [
                {
                    "evidence_id": str(row["evidence_id"]),
                    "classification": str(row["classification"]),
                    "rule_id": str(row["rule_id"]),
                    "target_kind": str(row["target_kind"]),
                    "target_key": str(row["owner_target_key"]),
                }
                for row in rows
            ]
            results.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_version": versions[candidate_id],
                    "coverage": report.coverage_by_candidate[candidate_id],
                    "verdict": _duplicate_verdict(result_evidence),
                    "evidence": result_evidence,
                }
            )
        return {
            "check_id": report.check_id,
            "coverage": report.coverage,
            "results": results,
            "suggested_groups": [
                {
                    "member_candidate_ids": list(group.member_candidate_ids),
                    "evidence_ids": list(group.evidence_ids),
                    "default_representative_candidate_id": (
                        group.default_representative_candidate_id
                    ),
                }
                for group in report.suggested_groups
            ],
            "warnings": list(report.warnings),
        }

    @router.post("/api/import-jobs", status_code=202)
    async def mutate_import_job(
        request: ImportJobRequest, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        try:
            if request.action == "preview":
                preview = dependencies.import_service.preview_job(
                    candidate_ids=request.candidate_ids,
                    candidate_versions=request.candidate_versions,
                    displayed_payload_hashes=request.payload_hashes,
                )
                return {
                    "pieces_endpoint": preview.pieces_endpoint,
                    "selected_write_count": preview.selected_write_count,
                    "context_hash": preview.context_hash,
                    "items": [
                        {
                            "candidate_id": item.candidate_id,
                            "title": item.title,
                            "payload_hash": item.payload_hash,
                            "project": item.project,
                            "files": list(item.files),
                        }
                        for item in preview.items
                    ],
                }
            if request.action == "start":
                if request.confirmation is None:
                    _api_error(422, "validation_error", "Apply confirmation is required.")
                confirmation = ApplyConfirmation(
                    confirmed=request.confirmation.confirmed,
                    pieces_endpoint=request.confirmation.pieces_endpoint,
                    selected_write_count=request.confirmation.selected_write_count,
                    context_hash=request.confirmation.context_hash,
                )
                try:
                    record = dependencies.import_service.create_job(
                        candidate_ids=request.candidate_ids,
                        candidate_versions=request.candidate_versions,
                        displayed_payload_hashes=request.payload_hashes,
                        confirmation=confirmation,
                        acknowledge_remote_duplicate_risk=request.acknowledge_remote_duplicate_risk,
                    )
                except InvalidStateError as error:
                    message = str(error)
                    if "confirmation" in message:
                        _api_error(
                            422, "apply_confirmation_mismatch", "Apply confirmation does not match."
                        )
                    if "remote duplicate risk" in message:
                        _api_error(
                            422,
                            "remote_duplicate_risk_unacknowledged",
                            "Remote duplicate risk must be acknowledged.",
                        )
                    raise
                background_tasks.add_task(
                    dependencies.import_service.run_job, record.job_id
                )
            else:
                if request.resume_job_id is None or request.resolution is None:
                    _api_error(422, "validation_error", "Resume fields are required.")
                if not _valid_uuid4(request.resume_job_id):
                    _api_error(404, "not_found", "Import job not found.")
                record = dependencies.import_service.validate_resume(
                    job_id=request.resume_job_id,
                    resolution=request.resolution,
                    acknowledge_duplicate_write_risk=request.acknowledge_duplicate_write_risk,
                )
                background_tasks.add_task(
                    dependencies.import_service.resume_job,
                    job_id=request.resume_job_id,
                    resolution=request.resolution,
                    acknowledge_duplicate_write_risk=request.acknowledge_duplicate_write_risk,
                )
        except KeyError:
            _api_error(404, "not_found", "Candidate or import job not found.")
        except OptimisticConflictError:
            _api_error(409, "candidate_version_conflict", "A candidate changed before import.")
        except (ImportInProgressError, InvalidStateError):
            _api_error(409, "invalid_import_state", "The import action is not allowed.")
        return {"job_id": record.job_id, "state": record.state}

    @router.get("/api/import-jobs/{job_id}")
    async def get_import_job(job_id: str) -> dict[str, Any]:
        if not _valid_uuid4(job_id):
            _api_error(404, "not_found", "Import job not found.")
        try:
            record = dependencies.ledger.get_import_job(job_id)
        except KeyError:
            _api_error(404, "not_found", "Import job not found.")
        return _job_json(dependencies.ledger, record)

    @router.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return _settings_json(dependencies)

    @router.put("/api/settings")
    async def put_settings(request: SettingsRequest) -> dict[str, Any]:
        ledger = dependencies.ledger
        with ledger.transaction() as connection:
            current = connection.execute(
                "SELECT version FROM settings WHERE singleton_id = 1"
            ).fetchone()
            if current is None or int(current["version"]) != request.version:
                _api_error(409, "settings_version_conflict", "Settings changed.")
            connection.execute(
                "UPDATE settings SET version = version + 1, mcp_base_url = ?, updated_at = ? "
                "WHERE singleton_id = 1",
                (request.mcp_base_url, _now()),
            )
            connection.execute(
                "UPDATE source_roots SET enabled = 0"
            )
            for root in request.source_roots:
                resolved = root.path.expanduser().resolve(strict=False)
                existing = connection.execute(
                    "SELECT root_id FROM source_roots WHERE agent = ? AND resolved_path = ?",
                    (root.agent, str(resolved)),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO source_roots "
                        "(root_id, agent, lexical_path, resolved_path, enabled, "
                        "is_default, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                        (
                            _uuid4(),
                            root.agent,
                            str(root.path),
                            str(resolved),
                            root.enabled,
                            _now(),
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE source_roots SET lexical_path = ?, enabled = ? "
                        "WHERE root_id = ?",
                        (str(root.path), root.enabled, str(existing["root_id"])),
                    )
            connection.execute(
                "DELETE FROM source_roots WHERE enabled = 0 AND is_default = 0 "
                "AND root_id NOT IN (SELECT root_id FROM source_revisions)"
            )
            connection.execute("DELETE FROM host_path_mappings")
            for mapping in request.host_path_mappings:
                connection.execute(
                    "INSERT INTO host_path_mappings "
                    "(mapping_id, local_root, host_root, project, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        _uuid4(),
                        str(mapping.local_root.expanduser().resolve(strict=False)),
                        mapping.host_root.strip(),
                        mapping.project.strip(),
                        _now(),
                    ),
                )
        return _settings_json(dependencies)

    return router


def _override_findings(
    ledger: Ledger, candidate_id: str, request: CandidateMutationRequest
) -> None:
    if not request.finding_acknowledged or request.override_reason is None:
        _api_error(422, "validation_error", "A finding acknowledgement and reason are required.")
    with ledger.transaction() as connection:
        candidate = connection.execute(
            "SELECT version FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            _api_error(404, "not_found", "Candidate not found.")
        if int(candidate["version"]) != request.version:
            _api_error(409, "candidate_version_conflict", "The candidate changed.")
        rows = connection.execute(
            "SELECT finding_id FROM safety_findings WHERE candidate_id = ? "
            "AND candidate_version = ? AND state = ?",
            (candidate_id, request.version, FindingState.OPEN),
        ).fetchall()
        expected = {str(row["finding_id"]) for row in rows}
        if expected != set(request.finding_ids):
            _api_error(409, "invalid_candidate_state", "Every open finding must be selected.")
        connection.executemany(
            "UPDATE safety_findings SET state = ?, override_reason = ?, override_at = ? "
            "WHERE finding_id = ? AND state = ?",
            [
                (
                    FindingState.OVERRIDDEN,
                    request.override_reason,
                    _now(),
                    finding_id,
                    FindingState.OPEN,
                )
                for finding_id in request.finding_ids
            ],
        )


__all__ = ["ApiDependencies", "create_api_router"]
