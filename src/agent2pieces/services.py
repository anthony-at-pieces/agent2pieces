"""Durable scan, review, duplicate, and import orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from agent2pieces.dedupe import (
    DedupeCandidate,
    DuplicateClassification,
    LocalEvidence,
    SuggestedGroup,
    candidate_pairs,
    compare_candidates,
    suggested_groups,
)
from agent2pieces.ledger import (
    ImmutableRecordError,
    InvalidStateError,
    Ledger,
    OptimisticConflictError,
)
from agent2pieces.mcp_client import (
    DispatchPayload,
    MarkerSearchResult,
    McpCapabilities,
    McpWriteError,
    PathMapping,
    RemoteDuplicateResult,
    WriteResult,
)
from agent2pieces.models import (
    AdapterScanResult,
    AttemptKind,
    AttemptState,
    CandidateInput,
    CandidatePayload,
    CandidateRecord,
    CandidateStatus,
    FindingState,
    GroupStatus,
    ImportItemRecord,
    ImportItemState,
    ImportJobRecord,
    ImportJobState,
    ReviewGroupRecord,
    ScanRecord,
    ScanState,
    SourceAgent,
    SourceRevisionIdentity,
)
from agent2pieces.normalization import (
    import_id_from_payload_hash,
    payload_hash,
    visible_import_marker,
)
from agent2pieces.safety import scan_safety
from agent2pieces.scanners import scan_claude_root, scan_codex_root, scan_hermes_root


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid4() -> str:
    return str(uuid.uuid4())


_LOCK_REGISTRY_GUARD = threading.Lock()
_SCAN_LOCKS: dict[str, asyncio.Lock] = {}
_IMPORT_LOCKS: dict[str, asyncio.Lock] = {}
_TRANSIENT_ROOT_PREFIX = "agent2pieces-transient:"

_ACTIVE_JOB_STATES = (
    ImportJobState.QUEUED,
    ImportJobState.RUNNING,
    ImportJobState.PAUSED,
)
_TERMINAL_ITEM_STATES = (
    ImportItemState.IMPORTED,
    ImportItemState.REMOTE_DUPLICATE,
    ImportItemState.SKIPPED,
)


def _process_lock(registry: dict[str, asyncio.Lock], ledger: Ledger) -> asyncio.Lock:
    key = str(ledger.path.resolve(strict=False))
    with _LOCK_REGISTRY_GUARD:
        return registry.setdefault(key, asyncio.Lock())


def _candidate_has_active_import(ledger: Ledger, candidate_id: str) -> bool:
    row = ledger.connection.execute(
        "SELECT 1 FROM import_items i JOIN import_jobs j ON j.job_id = i.job_id "
        "WHERE i.candidate_id = ? AND j.state IN (?, ?, ?) "
        "AND i.state NOT IN (?, ?, ?) LIMIT 1",
        (candidate_id, *_ACTIVE_JOB_STATES, *_TERMINAL_ITEM_STATES),
    ).fetchone()
    return row is not None


class ScanInProgressError(RuntimeError):
    """A scan is already executing in this process."""


class ImportInProgressError(RuntimeError):
    """An import is already executing in this process."""


@dataclass(frozen=True, slots=True)
class ScanRoot:
    root_id: str
    agent: SourceAgent
    path: Path


def coalesce_scan_roots(roots: Sequence[ScanRoot]) -> tuple[ScanRoot, ...]:
    """Resolve roots, remove exact repeats, and scan nested roots first."""

    unique: list[ScanRoot] = []
    seen: set[tuple[SourceAgent, Path]] = set()
    for root in roots:
        resolved = root.path.resolve(strict=False)
        key = (root.agent, resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ScanRoot(root_id=root.root_id, agent=root.agent, path=resolved))
    return tuple(
        root
        for _, root in sorted(
            enumerate(unique),
            key=lambda item: (-len(item[1].path.parts), item[0]),
        )
    )


def _source_unit_key(
    agent: SourceAgent, source_path: str, source_key: str | None
) -> tuple[SourceAgent, Path, str]:
    section = ""
    section_marker = "#section="
    if agent is SourceAgent.HERMES and source_key is not None and section_marker in source_key:
        section = source_key.rpartition(section_marker)[2]
    return agent, Path(source_path).resolve(strict=False), section


@dataclass(frozen=True, slots=True)
class FindingOverride:
    finding_ids: tuple[str, ...]
    acknowledged: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DuplicateCheckReport:
    check_id: str
    coverage: str
    coverage_by_candidate: Mapping[str, str]
    suggested_groups: tuple[SuggestedGroup, ...]
    evidence_by_candidate: Mapping[str, tuple[str, ...]]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportPreviewItem:
    candidate_id: str
    title: str
    payload_hash: str
    project: str | None
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportPreview:
    pieces_endpoint: str
    selected_write_count: int
    context_hash: str
    items: tuple[ImportPreviewItem, ...]


@dataclass(frozen=True, slots=True)
class _PreparedImportContext:
    preview: ImportPreview
    candidates: tuple[CandidateRecord, ...]
    frozen_write_arguments: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ApplyConfirmation:
    confirmed: bool
    pieces_endpoint: str
    selected_write_count: int
    context_hash: str


Scanner = Callable[[Path], AdapterScanResult | Awaitable[AdapterScanResult]]


class _PiecesClient(Protocol):
    @property
    def capabilities(self) -> McpCapabilities: ...

    async def search_marker(self, import_id: str) -> MarkerSearchResult: ...

    async def search_duplicates(self, candidate: DedupeCandidate) -> RemoteDuplicateResult: ...

    def build_write_arguments(
        self,
        payload: DispatchPayload,
        *,
        mappings: tuple[PathMapping, ...] = (),
    ) -> dict[str, object]: ...

    async def create_memory(
        self,
        arguments: dict[str, object],
        *,
        before_call: Callable[[], Awaitable[None]],
    ) -> WriteResult: ...


class ScanService:
    """Run configured adapters and persist each source result atomically."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        scanners: Mapping[SourceAgent, Scanner] | None = None,
    ) -> None:
        self._ledger = ledger
        self._lock = _process_lock(_SCAN_LOCKS, ledger)
        self._queued_scan_id: str | None = None
        self._scanners: Mapping[SourceAgent, Scanner] = scanners or {
            SourceAgent.CODEX: scan_codex_root,
            SourceAgent.CLAUDE: scan_claude_root,
            SourceAgent.HERMES: scan_hermes_root,
        }

    def queue_scan(self) -> ScanRecord:
        """Persist a queued scan after checking both memory and durable state."""

        if self._lock.locked() or self._queued_scan_id is not None:
            raise ScanInProgressError("a scan is already queued or running")
        scan_id = self._ledger.create_scan(
            settings_version=self._ledger.get_settings().version
        )
        self._queued_scan_id = scan_id
        return self._ledger.get_scan(scan_id)

    async def run_scan(
        self, roots: Sequence[ScanRoot], *, scan_id: str | None = None
    ) -> ScanRecord:
        if self._lock.locked():
            raise ScanInProgressError("a scan is already running")
        async with self._lock:
            if scan_id is None:
                scan_id = self._ledger.create_scan(
                    settings_version=self._ledger.get_settings().version
                )
            self._queued_scan_id = None
            self._ledger.start_scan(scan_id)
            discovered = accepted = excluded = quarantined = errors = 0
            seen_units: set[tuple[SourceAgent, Path, str]] = set()
            try:
                for root in coalesce_scan_roots(roots):
                    self._validate_root_identity(root)
                    scanner = self._scanners[root.agent]
                    pending_result = await asyncio.to_thread(scanner, root.path)
                    if inspect.isawaitable(pending_result):
                        result = await pending_result
                    else:
                        result = pending_result
                    for item in result.dispositions:
                        unit_key = _source_unit_key(
                            root.agent, item.source_path, item.source_key
                        )
                        if unit_key in seen_units:
                            continue
                        seen_units.add(unit_key)
                        discovered += 1
                        if item.disposition.value == "excluded":
                            excluded += 1
                        else:
                            quarantined += 1
                        self._ledger.record_source_disposition(
                            scan_id=scan_id,
                            agent=root.agent,
                            root_id=root.root_id,
                            source_path=item.source_path,
                            source_key=item.source_key,
                            disposition=item.disposition,
                            byte_count=item.byte_count,
                            reason=item.reason,
                            detail=None,
                        )
                    for candidate in result.candidates:
                        unit_key = _source_unit_key(
                            root.agent,
                            candidate.candidate.source_path,
                            candidate.candidate.source_key,
                        )
                        if unit_key in seen_units:
                            continue
                        seen_units.add(unit_key)
                        discovered += 1
                        accepted += 1
                        self._persist_candidate(scan_id, root, candidate)
            except asyncio.CancelledError:
                self._fail_scan(
                    scan_id=scan_id,
                    discovered=discovered,
                    accepted=accepted,
                    excluded=excluded,
                    quarantined=quarantined,
                    errors=errors + 1,
                    detail="CancelledError",
                )
                raise
            except Exception as error:
                self._fail_scan(
                    scan_id=scan_id,
                    discovered=discovered,
                    accepted=accepted,
                    excluded=excluded,
                    quarantined=quarantined,
                    errors=errors + 1,
                    detail=type(error).__name__,
                )
                return self._ledger.get_scan(scan_id)
            with self._ledger.transaction() as connection:
                connection.execute(
                    "UPDATE scan_runs SET state = ?, finished_at = ?, discovered_count = ?, "
                    "accepted_count = ?, excluded_count = ?, quarantine_count = ?, "
                    "error_count = ? WHERE scan_id = ?",
                    (
                        ScanState.COMPLETED,
                        _now(),
                        discovered,
                        accepted,
                        excluded,
                        quarantined,
                        errors,
                        scan_id,
                    ),
                )
            return self._ledger.get_scan(scan_id)

    def _validate_root_identity(self, root: ScanRoot) -> None:
        row = self._ledger.connection.execute(
            "SELECT agent, lexical_path, resolved_path FROM source_roots WHERE root_id = ?",
            (root.root_id,),
        ).fetchone()
        if row is None or str(row["agent"]) != root.agent.value:
            raise InvalidStateError("configured source root identity is invalid")
        expected = Path(str(row["resolved_path"])).absolute()
        try:
            if expected.resolve(strict=True) != expected or not expected.is_dir():
                raise InvalidStateError("configured source root was retargeted")
            supplied = root.path.resolve(strict=True)
            lexical_value = str(row["lexical_path"])
            if lexical_value.startswith(_TRANSIENT_ROOT_PREFIX):
                lexical_value = lexical_value.removeprefix(_TRANSIENT_ROOT_PREFIX)
            lexical = Path(lexical_value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise InvalidStateError("configured source root is unavailable") from error
        if supplied != expected or lexical != expected:
            raise InvalidStateError("configured source root was retargeted")

    def _fail_scan(
        self,
        *,
        scan_id: str,
        discovered: int,
        accepted: int,
        excluded: int,
        quarantined: int,
        errors: int,
        detail: str,
    ) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE scan_runs SET state = ?, finished_at = ?, discovered_count = ?, "
                "accepted_count = ?, excluded_count = ?, quarantine_count = ?, "
                "error_count = ?, error_detail = ? WHERE scan_id = ?",
                (
                    ScanState.FAILED,
                    _now(),
                    discovered,
                    accepted,
                    excluded,
                    quarantined,
                    errors,
                    detail[:1024],
                    scan_id,
                ),
            )

    def _persist_candidate(
        self, scan_id: str, root: ScanRoot, candidate_input: CandidateInput
    ) -> None:
        candidate = candidate_input.candidate
        prior = self._ledger.connection.execute(
            "SELECT c.*, r.revision_id AS prior_revision_id FROM candidates c "
            "JOIN source_revisions r ON r.revision_id = c.revision_id "
            "WHERE r.agent = ? AND r.root_id = ? AND r.source_key = ? "
            "ORDER BY r.first_observed_at DESC, c.created_at DESC LIMIT 1",
            (candidate.source_agent, root.root_id, candidate.source_key),
        ).fetchone()
        identity = SourceRevisionIdentity(
            agent=candidate.source_agent,
            root_id=root.root_id,
            source_key=candidate.source_key,
            source_hash=candidate.source_hash,
            candidate_input_hash=candidate_input.candidate_input_hash,
        )
        revision = self._ledger.get_or_create_source_revision(
            identity,
            source_path=candidate.source_path,
            predecessor_revision_id=(
                str(prior["revision_id"]) if prior is not None else None
            ),
        )
        payload = CandidatePayload(
            title=candidate.title,
            markdown_body=candidate.markdown_body,
            external_links=candidate.external_links,
            project_scope=candidate.project_scope,
        )
        existing = self._ledger.connection.execute(
            "SELECT candidate_id FROM candidates WHERE revision_id = ?",
            (revision.revision_id,),
        ).fetchone()
        candidate_record: CandidateRecord
        prior_is_frozen = prior is not None and _candidate_has_active_import(
            self._ledger, str(prior["candidate_id"])
        )
        if existing is not None:
            candidate_record = self._ledger.get_candidate(str(existing["candidate_id"]))
        elif (
            prior is None
            or prior["status"] == CandidateStatus.IMPORTED
            or prior_is_frozen
        ):
            candidate_record = self._ledger.create_candidate(
                revision_id=revision.revision_id,
                payload=payload,
                payload_hash=candidate.payload_hash,
                import_id=candidate.import_id,
            )
            self._record_safety(candidate_record)
        else:
            candidate_id = str(prior["candidate_id"])
            now = _now()
            with self._ledger.transaction() as connection:
                connection.execute(
                    "INSERT INTO candidate_payload_snapshots "
                    "(snapshot_id, candidate_id, candidate_version, source_revision_id, state, "
                    "payload_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _uuid4(),
                        candidate_id,
                        int(prior["version"]),
                        str(prior["revision_id"]),
                        str(prior["status"]),
                        str(prior["current_payload_json"]),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE safety_findings SET state = ? WHERE candidate_id = ? "
                    "AND candidate_version = ? AND state IN (?, ?)",
                    (
                        FindingState.CLEARED,
                        candidate_id,
                        int(prior["version"]),
                        FindingState.OPEN,
                        FindingState.OVERRIDDEN,
                    ),
                )
                connection.execute(
                    "UPDATE candidates SET revision_id = ?, status = ?, version = version + 1, "
                    "current_payload_json = ?, payload_hash = ?, import_id = ?, updated_at = ? "
                    "WHERE candidate_id = ?",
                    (
                        revision.revision_id,
                        CandidateStatus.PENDING,
                        payload.model_dump_json(),
                        candidate.payload_hash,
                        candidate.import_id,
                        now,
                        candidate_id,
                    ),
                )
            candidate_record = self._ledger.get_candidate(candidate_id)
            self._record_safety(candidate_record)

        path = Path(candidate.source_path)
        try:
            stat = path.stat()
            mtime_ns = stat.st_mtime_ns
            byte_count = stat.st_size
        except OSError:
            mtime_ns = 0
            byte_count = len(candidate.markdown_body.encode("utf-8"))
        self._ledger.record_source_observation(
            revision_id=revision.revision_id,
            scan_id=scan_id,
            source_updated_at=candidate.source_updated_at,
            file_mtime_ns=mtime_ns,
            raw_byte_count=byte_count,
        )

    def _record_safety(self, candidate: CandidateRecord) -> None:
        for finding in scan_safety(
            title=candidate.payload.title,
            markdown_body=candidate.payload.markdown_body,
        ):
            self._ledger.add_safety_finding(
                candidate_id=candidate.candidate_id,
                candidate_version=candidate.version,
                reason_code=finding.reason_code,
                severity=finding.severity,
                line_number=finding.line_number,
            )


class ReviewService:
    """Apply optimistic review decisions and evidence-backed grouping."""

    def __init__(self, ledger: Ledger, client: _PiecesClient | None = None) -> None:
        self._ledger = ledger
        self._client = client
        self._reports: dict[str, tuple[tuple[str, int], ...]] = {}
        self._suggestions: dict[str, tuple[SuggestedGroup, ...]] = {}

    def edit_candidate(
        self,
        *,
        candidate_id: str,
        expected_version: int,
        payload: CandidatePayload,
    ) -> CandidateRecord:
        content_hash = payload_hash(
            title=payload.title,
            markdown_body=payload.markdown_body,
            external_links=payload.external_links,
        )
        findings = scan_safety(title=payload.title, markdown_body=payload.markdown_body)
        now = _now()
        with self._ledger.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                raise OptimisticConflictError("candidate version is stale")
            if current["status"] == CandidateStatus.IMPORTED:
                raise ImmutableRecordError("imported candidates are immutable")
            if _candidate_has_active_import(self._ledger, candidate_id):
                raise InvalidStateError("candidate is frozen by an active import job")
            connection.execute(
                "INSERT INTO candidate_payload_snapshots "
                "(snapshot_id, candidate_id, candidate_version, source_revision_id, state, "
                "payload_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _uuid4(),
                    candidate_id,
                    expected_version,
                    str(current["revision_id"]),
                    str(current["status"]),
                    str(current["current_payload_json"]),
                    now,
                ),
            )
            connection.execute(
                "UPDATE candidates SET status = ?, version = version + 1, "
                "current_payload_json = ?, payload_hash = ?, import_id = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (
                    CandidateStatus.PENDING,
                    payload.model_dump_json(),
                    content_hash,
                    import_id_from_payload_hash(content_hash),
                    now,
                    candidate_id,
                ),
            )
            connection.execute(
                "UPDATE safety_findings SET state = ? WHERE candidate_id = ? "
                "AND state IN (?, ?)",
                (
                    FindingState.CLEARED,
                    candidate_id,
                    FindingState.OPEN,
                    FindingState.OVERRIDDEN,
                ),
            )
            connection.executemany(
                "INSERT INTO safety_findings "
                "(finding_id, candidate_id, candidate_version, reason_code, severity, "
                "line_number, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        _uuid4(),
                        candidate_id,
                        expected_version + 1,
                        finding.reason_code,
                        finding.severity,
                        finding.line_number,
                        FindingState.OPEN,
                        now,
                    )
                    for finding in findings
                ],
            )
        return self._ledger.get_candidate(candidate_id)

    def approve_candidate(
        self,
        *,
        candidate_id: str,
        expected_version: int,
        override: FindingOverride | None = None,
    ) -> CandidateRecord:
        now = _now()
        with self._ledger.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                raise OptimisticConflictError("candidate version is stale")
            if current["status"] == CandidateStatus.IMPORTED:
                raise ImmutableRecordError("imported candidates are immutable")
            if _candidate_has_active_import(self._ledger, candidate_id):
                raise InvalidStateError("candidate is frozen by an active import job")
            if current["group_id"] is not None:
                raise InvalidStateError("grouped candidates require a group approval decision")
            open_rows = connection.execute(
                "SELECT * FROM safety_findings WHERE candidate_id = ? "
                "AND candidate_version = ? AND state = ? ORDER BY finding_id",
                (candidate_id, expected_version, FindingState.OPEN),
            ).fetchall()
            if open_rows:
                if override is None:
                    raise InvalidStateError(
                        "open safety finding requires an explicit override"
                    )
                expected_ids = {str(row["finding_id"]) for row in open_rows}
                if set(override.finding_ids) != expected_ids:
                    raise InvalidStateError("every open finding must be overridden")
                if not override.acknowledged:
                    raise InvalidStateError("finding override acknowledgement is required")
                if not 10 <= len(override.reason) <= 500:
                    raise InvalidStateError(
                        "finding override reason must be 10 to 500 characters"
                    )
            connection.execute(
                "INSERT INTO candidate_payload_snapshots "
                "(snapshot_id, candidate_id, candidate_version, source_revision_id, state, "
                "payload_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _uuid4(),
                    candidate_id,
                    expected_version,
                    str(current["revision_id"]),
                    str(current["status"]),
                    str(current["current_payload_json"]),
                    now,
                ),
            )
            connection.execute(
                "UPDATE candidates SET status = ?, version = version + 1, updated_at = ? "
                "WHERE candidate_id = ?",
                (CandidateStatus.APPROVED, now, candidate_id),
            )
            if open_rows and override is not None:
                connection.executemany(
                    "UPDATE safety_findings SET state = ?, override_reason = ?, override_at = ? "
                    "WHERE finding_id = ? AND state = ?",
                    [
                        (
                            FindingState.OVERRIDDEN,
                            override.reason,
                            now,
                            finding_id,
                            FindingState.OPEN,
                        )
                        for finding_id in override.finding_ids
                    ],
                )
        return self._ledger.get_candidate(candidate_id)

    def exclude_candidate(
        self, *, candidate_id: str, expected_version: int
    ) -> CandidateRecord:
        if _candidate_has_active_import(self._ledger, candidate_id):
            raise InvalidStateError("candidate is frozen by an active import job")
        current = self._ledger.get_candidate(candidate_id)
        return self._ledger.update_candidate(
            candidate_id=candidate_id,
            expected_version=expected_version,
            payload=current.payload,
            payload_hash=current.payload_hash,
            import_id=current.import_id,
            status=CandidateStatus.EXCLUDED,
        )

    async def check_duplicates(
        self,
        *,
        candidate_ids: Sequence[str],
        candidate_versions: Mapping[str, int],
    ) -> DuplicateCheckReport:
        records = [self._ledger.get_candidate(candidate_id) for candidate_id in candidate_ids]
        for record in records:
            if candidate_versions.get(record.candidate_id) != record.version:
                raise OptimisticConflictError("candidate version is stale")
        if not records:
            raise InvalidStateError("duplicate check requires candidates")
        check_id = self._ledger.create_duplicate_check(
            candidate_id=records[0].candidate_id,
            candidate_version=records[0].version,
        )
        candidates = tuple(self._as_dedupe_candidate(record) for record in records)
        checked_versions = {
            record.candidate_id: record.version for record in records
        }
        local_evidence: list[LocalEvidence] = []
        evidence_by_candidate: dict[str, list[str]] = {
            candidate.candidate_id: [] for candidate in candidates
        }
        for left, right in candidate_pairs(candidates):
            score = compare_candidates(left, right)
            if score.classification is DuplicateClassification.DISTINCT:
                continue
            evidence_id = self._ledger.add_duplicate_evidence(
                check_id=check_id,
                candidate_versions={
                    left.candidate_id: checked_versions[left.candidate_id],
                    right.candidate_id: checked_versions[right.candidate_id],
                },
                target_kind="candidate",
                target_key=right.candidate_id,
                candidate_target_keys={
                    left.candidate_id: right.candidate_id,
                    right.candidate_id: left.candidate_id,
                },
                candidate_target_titles={
                    left.candidate_id: right.title,
                    right.candidate_id: left.title,
                },
                classification=score.classification.value,
                rule_id=score.rule_id,
                cosine=score.cosine,
                body_shingle_jaccard=score.body_shingle_jaccard,
                title_jaccard=score.title_jaccard,
                target_title=right.title,
                payload_hash=right.payload_hash,
            )
            local_evidence.append(
                LocalEvidence(
                    evidence_id=evidence_id,
                    left_candidate_id=left.candidate_id,
                    right_candidate_id=right.candidate_id,
                    classification=score.classification,
                    rule_id=score.rule_id,
                    cosine=score.cosine,
                    body_shingle_jaccard=score.body_shingle_jaccard,
                    title_jaccard=score.title_jaccard,
                )
            )
            evidence_by_candidate[left.candidate_id].append(evidence_id)
            evidence_by_candidate[right.candidate_id].append(evidence_id)
        groups = suggested_groups(candidates, local_evidence)
        coverage_by_candidate = {
            candidate.candidate_id: "local-only" for candidate in candidates
        }
        warnings: list[str] = []
        if self._client is not None and self._client.capabilities.search_available:
            for candidate in candidates:
                if self._open_findings(candidate.candidate_id, None):
                    warnings.append(
                        f"pieces_search_skipped_open_safety:{candidate.candidate_id}"
                    )
                    continue
                search_duplicates = getattr(self._client, "search_duplicates", None)
                if search_duplicates is None:
                    warnings.append(f"pieces_search_error:{candidate.candidate_id}")
                    continue
                remote = await search_duplicates(candidate)
                if remote.coverage in {"error", "truncated"}:
                    warnings.append(
                        f"pieces_search_{remote.coverage}:{candidate.candidate_id}"
                    )
                if remote.coverage == "complete":
                    coverage_by_candidate[candidate.candidate_id] = "local+pieces"
                elif remote.coverage == "truncated":
                    coverage_by_candidate[candidate.candidate_id] = "local+pieces-partial"
                for rank, match in enumerate(remote.matches, start=1):
                    evidence_id = self._ledger.add_duplicate_evidence(
                        check_id=check_id,
                        candidate_versions={
                            candidate.candidate_id: checked_versions[
                                candidate.candidate_id
                            ]
                        },
                        target_kind="annotation",
                        target_key=match.annotation_id,
                        classification=match.classification,
                        rule_id=match.rule_id,
                        cosine=match.cosine,
                        body_shingle_jaccard=match.body_shingle_jaccard,
                        title_jaccard=match.title_jaccard,
                        remote_rank=rank,
                    )
                    evidence_by_candidate[candidate.candidate_id].append(evidence_id)
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE duplicate_checks SET finished_at = ?, verdict = ? WHERE check_id = ?",
                (_now(), "matches" if groups else "distinct", check_id),
            )
        versions = tuple(sorted((record.candidate_id, record.version) for record in records))
        self._reports[check_id] = versions
        self._suggestions[check_id] = groups
        unique_coverage = set(coverage_by_candidate.values())
        if not unique_coverage:
            coverage = "local-only"
        elif len(unique_coverage) == 1:
            coverage = unique_coverage.pop()
        else:
            coverage = "mixed"
        return DuplicateCheckReport(
            check_id=check_id,
            coverage=coverage,
            coverage_by_candidate=coverage_by_candidate,
            suggested_groups=groups,
            evidence_by_candidate={
                candidate_id: tuple(evidence_ids)
                for candidate_id, evidence_ids in evidence_by_candidate.items()
            },
            warnings=tuple(warnings),
        )

    def create_group_from_evidence(
        self,
        *,
        check_id: str,
        candidate_id: str,
        evidence_ids: Sequence[str],
    ) -> ReviewGroupRecord:
        """Create the verified suggested component containing a candidate."""

        requested_evidence = tuple(sorted(evidence_ids))
        suggestion = next(
            (
                item
                for item in self._suggestions.get(check_id, ())
                if candidate_id in item.member_candidate_ids
                and item.evidence_ids == requested_evidence
            ),
            None,
        )
        if suggestion is None:
            raise InvalidStateError("duplicate evidence component is unavailable")
        representative = self._ledger.get_candidate(
            suggestion.default_representative_candidate_id
        )
        return self.create_group(
            check_id=check_id,
            title=representative.payload.title,
            member_candidate_ids=suggestion.member_candidate_ids,
            evidence_ids=suggestion.evidence_ids,
        )

    def create_group(
        self,
        *,
        check_id: str,
        title: str,
        member_candidate_ids: Sequence[str],
        evidence_ids: Sequence[str],
    ) -> ReviewGroupRecord:
        if check_id not in self._reports:
            raise InvalidStateError("duplicate evidence component is unavailable")
        for candidate_id, version in self._reports[check_id]:
            if self._ledger.get_candidate(candidate_id).version != version:
                raise OptimisticConflictError("candidate changed after duplicate check")
        requested_members = tuple(sorted(member_candidate_ids))
        requested_evidence = tuple(sorted(evidence_ids))
        suggestion = next(
            (
                item
                for item in self._suggestions[check_id]
                if item.member_candidate_ids == requested_members
                and item.evidence_ids == requested_evidence
            ),
            None,
        )
        if suggestion is None:
            raise InvalidStateError("group membership is not backed by the evidence component")
        return self._ledger.create_review_group(
            title=title,
            candidate_ids=requested_members,
            representative_candidate_id=suggestion.default_representative_candidate_id,
            created_from_check_id=check_id,
            evidence_ids=requested_evidence,
        )

    def join_group(
        self,
        *,
        group_id: str,
        expected_group_version: int,
        candidate_id: str,
        expected_candidate_version: int,
        check_id: str,
        evidence_ids: Sequence[str],
    ) -> ReviewGroupRecord:
        """Join a candidate only through a complete, current evidence component."""

        group = self._ledger._get_review_group(group_id)
        candidate = self._ledger.get_candidate(candidate_id)
        if group.version != expected_group_version:
            raise OptimisticConflictError("group version is stale")
        if candidate.version != expected_candidate_version:
            raise OptimisticConflictError("candidate version is stale")
        if group.status is GroupStatus.IMPORTED or candidate.status is CandidateStatus.IMPORTED:
            raise ImmutableRecordError("imported records are immutable")
        if candidate.group_id is not None:
            raise InvalidStateError("candidate is already grouped")
        if _candidate_has_active_import(self._ledger, candidate_id):
            raise InvalidStateError("candidate is frozen by an active import job")
        members = {
            str(row["candidate_id"])
            for row in self._ledger.connection.execute(
                "SELECT candidate_id FROM candidates WHERE group_id = ?", (group_id,)
            ).fetchall()
        }
        if any(_candidate_has_active_import(self._ledger, member_id) for member_id in members):
            raise InvalidStateError("group is frozen by an active import job")
        requested_evidence = tuple(sorted(evidence_ids))
        suggestion = next(
            (
                item
                for item in self._suggestions.get(check_id, ())
                if set(item.member_candidate_ids) == members | {candidate_id}
                and item.evidence_ids == requested_evidence
            ),
            None,
        )
        if suggestion is None:
            raise InvalidStateError("group join is not backed by the evidence component")
        report_versions = dict(self._reports.get(check_id, ()))
        if any(
            report_versions.get(member_id)
            != self._ledger.get_candidate(member_id).version
            for member_id in members | {candidate_id}
        ):
            raise OptimisticConflictError("candidate changed after duplicate check")
        representative_id = (
            group.representative_candidate_id
            if group.representative_overridden
            else suggestion.default_representative_candidate_id
        )
        candidate_status = (
            CandidateStatus.APPROVED
            if group.status is GroupStatus.APPROVED
            and representative_id == candidate_id
            else (
                CandidateStatus.EXCLUDED
                if group.status is GroupStatus.APPROVED
                else candidate.status
            )
        )
        now = _now()
        with self._ledger.transaction() as connection:
            changed = connection.execute(
                "UPDATE candidates SET group_id = ?, status = ?, version = version + 1, "
                "updated_at = ? WHERE candidate_id = ? AND version = ? AND group_id IS NULL",
                (
                    group_id,
                    candidate_status,
                    now,
                    candidate_id,
                    expected_candidate_version,
                ),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("candidate version is stale")
            changed = connection.execute(
                "UPDATE review_groups SET representative_candidate_id = ?, "
                "version = version + 1, updated_at = ? WHERE group_id = ? AND version = ?",
                (representative_id, now, group_id, expected_group_version),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("group version is stale")
            if (
                group.status is GroupStatus.APPROVED
                and representative_id != group.representative_candidate_id
            ):
                connection.execute(
                    "UPDATE candidates SET status = CASE WHEN candidate_id = ? THEN ? "
                    "ELSE ? END, version = version + 1, updated_at = ? "
                    "WHERE candidate_id IN (?, ?)",
                    (
                        representative_id,
                        CandidateStatus.APPROVED,
                        CandidateStatus.PENDING,
                        now,
                        representative_id,
                        group.representative_candidate_id,
                    ),
                )
            connection.executemany(
                "INSERT OR IGNORE INTO review_group_evidence(group_id, evidence_id) "
                "VALUES (?, ?)",
                [(group_id, evidence_id) for evidence_id in requested_evidence],
            )
        return self._ledger._get_review_group(group_id)

    def leave_group(
        self,
        *,
        group_id: str,
        expected_group_version: int,
        candidate_id: str,
        expected_candidate_version: int,
    ) -> ReviewGroupRecord:
        """Remove a mutable member while preserving group decision history."""

        group = self._ledger._get_review_group(group_id)
        candidate = self._ledger.get_candidate(candidate_id)
        if group.version != expected_group_version:
            raise OptimisticConflictError("group version is stale")
        if candidate.version != expected_candidate_version:
            raise OptimisticConflictError("candidate version is stale")
        if group.status is GroupStatus.IMPORTED or candidate.status is CandidateStatus.IMPORTED:
            raise ImmutableRecordError("imported records are immutable")
        if candidate.group_id != group_id:
            raise InvalidStateError("candidate is not a group member")
        if (
            group.representative_candidate_id == candidate_id
            and group.status is not GroupStatus.EXCLUDED
        ):
            raise InvalidStateError("select another representative before leaving")
        if _candidate_has_active_import(self._ledger, candidate_id):
            raise InvalidStateError("candidate is frozen by an active import job")
        members = self._ledger.connection.execute(
            "SELECT candidate_id FROM candidates WHERE group_id = ?", (group_id,)
        ).fetchall()
        if any(
            _candidate_has_active_import(self._ledger, str(row["candidate_id"]))
            for row in members
        ):
            raise InvalidStateError("group is frozen by an active import job")
        status = (
            CandidateStatus.PENDING
            if group.status is GroupStatus.APPROVED
            else candidate.status
        )
        now = _now()
        with self._ledger.transaction() as connection:
            changed = connection.execute(
                "UPDATE candidates SET group_id = NULL, status = ?, version = version + 1, "
                "updated_at = ? WHERE candidate_id = ? AND version = ? AND group_id = ?",
                (status, now, candidate_id, expected_candidate_version, group_id),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("candidate version is stale")
            changed = connection.execute(
                "UPDATE review_groups SET version = version + 1, updated_at = ? "
                "WHERE group_id = ? AND version = ?",
                (now, group_id, expected_group_version),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("group version is stale")
        return self._ledger._get_review_group(group_id)

    def update_group(
        self,
        *,
        group_id: str,
        expected_version: int,
        action: Literal["set_representative", "approve", "exclude"],
        representative_candidate_id: str | None = None,
        acting_candidate_id: str | None = None,
        expected_candidate_version: int | None = None,
    ) -> ReviewGroupRecord:
        group = self._ledger._get_review_group(group_id)
        if group.version != expected_version:
            raise OptimisticConflictError("group version is stale")
        if group.status is GroupStatus.IMPORTED:
            raise ImmutableRecordError("imported groups are immutable")
        active_member = self._ledger.connection.execute(
            "SELECT 1 FROM candidates c JOIN import_items i "
            "ON i.candidate_id = c.candidate_id "
            "JOIN import_jobs j ON j.job_id = i.job_id "
            "WHERE c.group_id = ? AND j.state IN (?, ?, ?) "
            "AND i.state NOT IN (?, ?, ?) LIMIT 1",
            (group_id, *_ACTIVE_JOB_STATES, *_TERMINAL_ITEM_STATES),
        ).fetchone()
        if active_member is not None:
            raise InvalidStateError("group is frozen by an active import job")
        if action == "set_representative":
            if representative_candidate_id is None:
                raise InvalidStateError("representative candidate is required")
            if self._open_findings(representative_candidate_id, None):
                raise InvalidStateError("representative has an open finding")
            changed_group = self._ledger.update_group_representative(
                group_id=group_id,
                expected_version=expected_version,
                representative_candidate_id=representative_candidate_id,
                overridden=True,
            )
            if group.status is GroupStatus.APPROVED:
                with self._ledger.transaction() as connection:
                    connection.execute(
                        "UPDATE candidates SET status = CASE WHEN candidate_id = ? THEN ? "
                        "WHEN candidate_id = ? THEN ? ELSE status END, version = version + 1, "
                        "updated_at = ? WHERE group_id = ? AND candidate_id IN (?, ?)",
                        (
                            representative_candidate_id,
                            CandidateStatus.APPROVED,
                            group.representative_candidate_id,
                            CandidateStatus.PENDING,
                            _now(),
                            group_id,
                            representative_candidate_id,
                            group.representative_candidate_id,
                        ),
                    )
            return changed_group
        if acting_candidate_id is None or expected_candidate_version is None:
            raise InvalidStateError("acting candidate and version are required")
        with self._ledger.transaction() as connection:
            acting_candidate = connection.execute(
                "SELECT version, group_id FROM candidates WHERE candidate_id = ?",
                (acting_candidate_id,),
            ).fetchone()
            if (
                acting_candidate is None
                or int(acting_candidate["version"]) != expected_candidate_version
            ):
                raise OptimisticConflictError("candidate version is stale")
            if acting_candidate["group_id"] != group_id:
                raise InvalidStateError("candidate is not a group member")
            changed = connection.execute(
                "UPDATE review_groups SET status = ?, version = version + 1, updated_at = ? "
                "WHERE group_id = ? AND version = ?",
                (
                    GroupStatus.APPROVED if action == "approve" else GroupStatus.EXCLUDED,
                    _now(),
                    group_id,
                    expected_version,
                ),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("group version is stale")
            if action == "approve":
                if self._open_findings(group.representative_candidate_id, None):
                    raise InvalidStateError("representative has an open finding")
                members = connection.execute(
                    "SELECT candidate_id, version, revision_id, status, current_payload_json "
                    "FROM candidates WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
                for member in members:
                    connection.execute(
                        "INSERT INTO candidate_payload_snapshots "
                        "(snapshot_id, candidate_id, candidate_version, source_revision_id, "
                        "state, payload_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            _uuid4(),
                            str(member["candidate_id"]),
                            int(member["version"]),
                            str(member["revision_id"]),
                            str(member["status"]),
                            str(member["current_payload_json"]),
                            _now(),
                        ),
                    )
                connection.execute(
                    "UPDATE candidates SET status = CASE WHEN candidate_id = ? THEN ? ELSE ? END, "
                    "version = version + 1, updated_at = ? WHERE group_id = ?",
                    (
                        group.representative_candidate_id,
                        CandidateStatus.APPROVED,
                        CandidateStatus.EXCLUDED,
                        _now(),
                        group_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE candidates SET status = ?, version = version + 1, updated_at = ? "
                    "WHERE group_id = ?",
                    (CandidateStatus.EXCLUDED, _now(), group_id),
                )
        return self._ledger._get_review_group(group_id)

    def _record_safety(self, candidate: CandidateRecord) -> None:
        for finding in scan_safety(
            title=candidate.payload.title,
            markdown_body=candidate.payload.markdown_body,
        ):
            self._ledger.add_safety_finding(
                candidate_id=candidate.candidate_id,
                candidate_version=candidate.version,
                reason_code=finding.reason_code,
                severity=finding.severity,
                line_number=finding.line_number,
            )

    def _open_findings(self, candidate_id: str, version: int | None) -> list[Any]:
        if version is None:
            version = self._ledger.get_candidate(candidate_id).version
        return self._ledger.connection.execute(
            "SELECT * FROM safety_findings WHERE candidate_id = ? "
            "AND candidate_version = ? AND state = ? ORDER BY finding_id",
            (candidate_id, version, FindingState.OPEN),
        ).fetchall()

    def _as_dedupe_candidate(self, record: CandidateRecord) -> DedupeCandidate:
        row = self._ledger.connection.execute(
            "SELECT r.agent, r.source_key, o.source_updated_at FROM source_revisions r "
            "LEFT JOIN source_observations o ON o.revision_id = r.revision_id "
            "WHERE r.revision_id = ? ORDER BY o.observed_at DESC LIMIT 1",
            (record.revision_id,),
        ).fetchone()
        if row is None:
            raise KeyError("source revision not found")
        return DedupeCandidate(
            candidate_id=record.candidate_id,
            source_agent=SourceAgent(str(row["agent"])),
            source_key=str(row["source_key"]),
            title=record.payload.title,
            markdown_body=record.payload.markdown_body,
            payload_hash=record.payload_hash,
            import_id=record.import_id,
            project_scope=record.payload.project_scope,
            external_links=tuple(record.payload.external_links),
            source_updated_at=(
                str(row["source_updated_at"]) if row["source_updated_at"] is not None else None
            ),
        )


class ImportService:
    """Freeze and execute sequential import jobs with durable ambiguity."""

    def __init__(self, ledger: Ledger, client: _PiecesClient) -> None:
        self._ledger = ledger
        self._client = client
        self._lock = _process_lock(_IMPORT_LOCKS, ledger)

    def create_job(
        self,
        *,
        candidate_ids: Sequence[str],
        candidate_versions: Mapping[str, int],
        displayed_payload_hashes: Mapping[str, str],
        confirmation: ApplyConfirmation,
        acknowledge_remote_duplicate_risk: bool,
    ) -> ImportJobRecord:
        prepared = self._prepare_import_context(
            candidate_ids=candidate_ids,
            candidate_versions=candidate_versions,
            displayed_payload_hashes=displayed_payload_hashes,
        )
        preview = prepared.preview
        if (
            not confirmation.confirmed
            or confirmation.pieces_endpoint != preview.pieces_endpoint
            or confirmation.selected_write_count != preview.selected_write_count
            or confirmation.context_hash != preview.context_hash
        ):
            raise InvalidStateError("apply confirmation does not match import context")
        capabilities = self._client.capabilities
        return self._ledger.create_import_job(
            candidates=prepared.candidates,
            pieces_endpoint=preview.pieces_endpoint,
            context_hash=preview.context_hash,
            frozen_write_arguments=prepared.frozen_write_arguments,
            remote_search_available=capabilities.search_available,
            duplicate_risk_acknowledged=acknowledge_remote_duplicate_risk,
        )

    def preview_job(
        self,
        *,
        candidate_ids: Sequence[str],
        candidate_versions: Mapping[str, int],
        displayed_payload_hashes: Mapping[str, str],
    ) -> ImportPreview:
        return self._prepare_import_context(
            candidate_ids=candidate_ids,
            candidate_versions=candidate_versions,
            displayed_payload_hashes=displayed_payload_hashes,
        ).preview

    def _prepare_import_context(
        self,
        *,
        candidate_ids: Sequence[str],
        candidate_versions: Mapping[str, int],
        displayed_payload_hashes: Mapping[str, str],
    ) -> _PreparedImportContext:
        capabilities = self._client.capabilities
        if not capabilities.import_ready:
            raise InvalidStateError("Pieces write tool is not import ready")
        records: list[CandidateRecord] = []
        frozen_write_arguments: dict[str, str] = {}
        preview_items: list[ImportPreviewItem] = []
        context_items: list[dict[str, object]] = []
        mappings = self._path_mappings()
        for candidate_id in candidate_ids:
            record = self._ledger.get_candidate(candidate_id)
            if (
                candidate_versions.get(candidate_id) != record.version
                or displayed_payload_hashes.get(candidate_id) != record.payload_hash
            ):
                raise OptimisticConflictError("candidate version or payload hash is stale")
            if record.status is not CandidateStatus.APPROVED:
                raise InvalidStateError("only approved candidates can be imported")
            finding = self._ledger.connection.execute(
                "SELECT 1 FROM safety_findings WHERE candidate_id = ? "
                "AND candidate_version = ? AND state = ?",
                (candidate_id, record.version, FindingState.OPEN),
            ).fetchone()
            if finding is not None:
                raise InvalidStateError("open finding blocks import")
            if record.group_id is not None:
                group = self._ledger._get_review_group(record.group_id)
                if (
                    group.status is not GroupStatus.APPROVED
                    or group.representative_candidate_id != candidate_id
                ):
                    raise InvalidStateError("only an approved group representative can import")
            source = self._ledger.connection.execute(
                "SELECT r.agent, r.source_path FROM source_revisions r "
                "JOIN candidates c ON c.revision_id = r.revision_id WHERE c.candidate_id = ?",
                (record.candidate_id,),
            ).fetchone()
            if source is None:
                raise InvalidStateError("source revision is missing")
            payload = record.payload
            dispatch = DispatchPayload(
                title=payload.title,
                markdown_body=payload.markdown_body,
                external_links=tuple(payload.external_links),
                source_agent=SourceAgent(str(source["agent"])),
                source_path=Path(str(source["source_path"])),
                import_id=record.import_id,
            )
            arguments = self._client.build_write_arguments(dispatch, mappings=mappings)
            serialized_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            frozen_write_arguments[record.candidate_id] = serialized_arguments
            project_value = arguments.get("project")
            project = project_value if isinstance(project_value, str) else None
            files_value = arguments.get("files")
            files = (
                tuple(str(item) for item in files_value)
                if isinstance(files_value, list)
                else ()
            )
            preview_items.append(
                ImportPreviewItem(
                    candidate_id=record.candidate_id,
                    title=payload.title,
                    payload_hash=record.payload_hash,
                    project=project,
                    files=files,
                )
            )
            context_items.append(
                {
                    "candidate_id": record.candidate_id,
                    "candidate_version": record.version,
                    "payload_hash": record.payload_hash,
                    "write_arguments": arguments,
                }
            )
            records.append(record)
        context_document = {
            "pieces_endpoint": capabilities.endpoint,
            "items": context_items,
        }
        context_hash = hashlib.sha256(
            json.dumps(
                context_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        preview = ImportPreview(
            pieces_endpoint=capabilities.endpoint,
            selected_write_count=len(records),
            context_hash=context_hash,
            items=tuple(preview_items),
        )
        return _PreparedImportContext(
            preview=preview,
            candidates=tuple(records),
            frozen_write_arguments=frozen_write_arguments,
        )

    async def run_job(self, job_id: str) -> ImportJobRecord:
        if self._lock.locked():
            raise ImportInProgressError("an import is already running")
        async with self._lock:
            return await self._run_job_locked(job_id)

    def validate_resume(
        self,
        *,
        job_id: str,
        resolution: Literal["recheck", "retry", "skip"],
        acknowledge_duplicate_write_risk: bool = False,
    ) -> ImportJobRecord:
        """Validate a resume request without performing remote work."""

        if self._lock.locked():
            raise ImportInProgressError("an import is already running")
        job = self._ledger.get_import_job(job_id)
        if job.state is not ImportJobState.PAUSED:
            raise InvalidStateError("only a paused job can be resumed")
        self._require_job_endpoint(job)
        self._paused_item(job_id)
        if resolution == "recheck" and not self._client.capabilities.search_available:
            raise InvalidStateError("marker recheck requires Pieces search")
        if resolution == "retry" and not acknowledge_duplicate_write_risk:
            raise InvalidStateError("duplicate write risk acknowledgement is required")
        return job

    async def _run_job_locked(self, job_id: str) -> ImportJobRecord:
        job = self._ledger.get_import_job(job_id)
        self._require_job_endpoint(job)
        if job.state is ImportJobState.QUEUED:
            self._ledger.start_import_job(job_id)
        elif job.state is not ImportJobState.RUNNING:
            raise InvalidStateError("import job is not queued")
        items = self._ledger.list_import_items(job_id)
        for item in items:
            if item.state in {
                ImportItemState.IMPORTED,
                ImportItemState.REMOTE_DUPLICATE,
                ImportItemState.SKIPPED,
            }:
                continue
            completed = await self._process_item(job_id, item)
            if not completed:
                return self._ledger.get_import_job(job_id)
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_jobs SET state = ?, finished_at = ?, pause_reason = NULL, "
                "current_ordinal = (SELECT COUNT(*) FROM import_items WHERE job_id = ?) "
                "WHERE job_id = ?",
                (ImportJobState.COMPLETED, _now(), job_id, job_id),
            )
        return self._ledger.get_import_job(job_id)

    async def _process_item(self, job_id: str, item: ImportItemRecord) -> bool:
        prior = self._ledger.connection.execute(
            "SELECT pieces_memory_id FROM import_items WHERE import_id = ? AND item_id <> ? "
            "AND state IN (?, ?) AND pieces_memory_id IS NOT NULL "
            "ORDER BY completed_at, item_id LIMIT 1",
            (
                item.import_id,
                item.item_id,
                ImportItemState.IMPORTED,
                ImportItemState.REMOTE_DUPLICATE,
            ),
        ).fetchone()
        if prior is not None:
            self._finish_item_remote_duplicate(item, str(prior["pieces_memory_id"]))
            self._advance(job_id, item.ordinal + 1)
            return True
        if self._client.capabilities.search_available:
            preflight = self._ledger.append_import_attempt(
                item_id=item.item_id,
                kind=AttemptKind.MARKER_PREFLIGHT,
                marker=visible_import_marker(item.import_id),
            )
            try:
                result = await self._client.search_marker(item.import_id)
            except asyncio.CancelledError:
                self._finish_attempt(
                    preflight.attempt_id,
                    state=AttemptState.FAILED,
                    marker_outcome="search_error",
                    error_detail="marker_preflight_cancelled",
                )
                self._pause_item(job_id, item.item_id, "marker_preflight_cancelled")
                raise
            except Exception:
                self._finish_attempt(
                    preflight.attempt_id,
                    state=AttemptState.FAILED,
                    marker_outcome="search_error",
                    error_detail="marker_preflight_failed",
                )
                self._pause_item(job_id, item.item_id, "marker_preflight_failed")
                return False
            self._finish_attempt(
                preflight.attempt_id,
                state=(
                    AttemptState.SUCCEEDED
                    if result.outcome in {"absent", "one_parent"}
                    else AttemptState.FAILED
                ),
                marker_outcome=result.outcome,
                parent_memory_id=(
                    result.parent_memory_ids[0]
                    if len(result.parent_memory_ids) == 1
                    else None
                ),
            )
            if result.outcome == "one_parent":
                self._finish_item_remote_duplicate(item, result.parent_memory_ids[0])
                self._advance(job_id, item.ordinal + 1)
                return True
            if result.outcome != "absent":
                self._pause_item(job_id, item.item_id, "marker_preflight_unresolved")
                return False

        try:
            arguments_value = json.loads(item.frozen_write_arguments_json)
            if not isinstance(arguments_value, dict):
                raise ValueError("frozen write arguments must be an object")
            arguments = {str(key): value for key, value in arguments_value.items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            self._fail_item(job_id, item.item_id, "write_arguments_failed")
            return False

        write_attempt_id: str | None = None

        async def before_call() -> None:
            nonlocal write_attempt_id
            attempt = self._ledger.append_import_attempt(
                item_id=item.item_id,
                kind=AttemptKind.WRITE,
                marker=visible_import_marker(item.import_id),
                dispatch_started=True,
            )
            write_attempt_id = attempt.attempt_id

        try:
            write_result = await self._client.create_memory(
                arguments,
                before_call=before_call,
            )
        except asyncio.CancelledError:
            if write_attempt_id is None:
                self._fail_item(job_id, item.item_id, "write_cancelled_before_dispatch")
            else:
                self._set_attempt_error(write_attempt_id, "cancelled")
                self._pause_item(job_id, item.item_id, "ambiguous_write")
            raise
        except McpWriteError as error:
            if write_attempt_id is None:
                self._fail_item(job_id, item.item_id, "write_failed_before_dispatch")
                return False
            if self._client.capabilities.search_available:
                try:
                    recovered = await self._client.search_marker(item.import_id)
                except asyncio.CancelledError:
                    self._set_attempt_error(write_attempt_id, error.error_kind)
                    self._pause_item(job_id, item.item_id, "ambiguous_write")
                    raise
                except Exception:
                    self._set_attempt_error(write_attempt_id, error.error_kind)
                    self._pause_item(job_id, item.item_id, "ambiguous_write")
                    return False
                if recovered.outcome == "one_parent":
                    self._finish_attempt(
                        write_attempt_id,
                        state=AttemptState.SUCCEEDED,
                        marker_outcome="one_parent",
                        parent_memory_id=recovered.parent_memory_ids[0],
                    )
                    self._import_item(item, recovered.parent_memory_ids[0])
                    self._advance(job_id, item.ordinal + 1)
                    return True
            self._set_attempt_error(write_attempt_id, error.error_kind)
            self._pause_item(job_id, item.item_id, "ambiguous_write")
            return False
        except Exception as error:
            if write_attempt_id is None:
                self._fail_item(job_id, item.item_id, "write_failed_before_dispatch")
            else:
                self._set_attempt_error(write_attempt_id, type(error).__name__)
                self._pause_item(job_id, item.item_id, "ambiguous_write")
            return False

        if write_attempt_id is None:
            self._fail_item(job_id, item.item_id, "write_boundary_missing")
            return False
        self._finish_attempt(
            write_attempt_id,
            state=AttemptState.SUCCEEDED,
            response_id=write_result.memory_id,
        )
        self._import_item(item, write_result.memory_id)
        self._advance(job_id, item.ordinal + 1)
        return True

    def _path_mappings(self) -> tuple[PathMapping, ...]:
        rows = self._ledger.connection.execute(
            "SELECT local_root, host_root, project FROM host_path_mappings "
            "ORDER BY created_at, mapping_id"
        ).fetchall()
        mappings: list[PathMapping] = []
        for row in rows:
            local_root = Path(str(row["local_root"]))
            host_root = str(row["host_root"])
            project = str(row["project"])
            if (
                not local_root.is_absolute()
                or not host_root
                or host_root != host_root.strip()
                or not project
                or project != project.strip()
                or any(
                    character.isspace() and character not in {" "}
                    or ord(character) < 32
                    or ord(character) == 127
                    for character in host_root + project
                )
            ):
                raise InvalidStateError("saved host path mapping is invalid")
            mappings.append(
                PathMapping(
                    local_root=local_root.resolve(strict=False),
                    host_root=host_root,
                    project=project,
                )
            )
        return tuple(mappings)

    async def resume_job(
        self,
        *,
        job_id: str,
        resolution: Literal["recheck", "retry", "skip"],
        acknowledge_duplicate_write_risk: bool = False,
    ) -> ImportJobRecord:
        if self._lock.locked():
            raise ImportInProgressError("an import is already running")
        async with self._lock:
            job = self._ledger.get_import_job(job_id)
            if job.state is not ImportJobState.PAUSED:
                raise InvalidStateError("only a paused job can be resumed")
            self._require_job_endpoint(job)
            item = self._paused_item(job_id)
            if resolution == "recheck":
                if not self._client.capabilities.search_available:
                    raise InvalidStateError("marker recheck requires Pieces search")
                attempt = self._ledger.append_import_attempt(
                    item_id=item.item_id,
                    kind=AttemptKind.MARKER_RECHECK,
                    marker=visible_import_marker(item.import_id),
                )
                try:
                    result = await self._client.search_marker(item.import_id)
                except asyncio.CancelledError:
                    self._finish_attempt(
                        attempt.attempt_id,
                        state=AttemptState.FAILED,
                        marker_outcome="search_error",
                        error_detail="marker_recheck_cancelled",
                    )
                    if item.state is ImportItemState.AMBIGUOUS:
                        self._keep_item_ambiguous(
                            job_id, item.item_id, "marker_recheck_cancelled"
                        )
                    else:
                        self._keep_item_failed(
                            job_id, item.item_id, "marker_recheck_cancelled"
                        )
                    raise
                except Exception:
                    self._finish_attempt(
                        attempt.attempt_id,
                        state=AttemptState.FAILED,
                        marker_outcome="search_error",
                        error_detail="marker_recheck_failed",
                    )
                    if item.state is ImportItemState.AMBIGUOUS:
                        self._keep_item_ambiguous(
                            job_id, item.item_id, "marker_recheck_failed"
                        )
                    else:
                        self._keep_item_failed(
                            job_id, item.item_id, "marker_recheck_failed"
                        )
                    return self._ledger.get_import_job(job_id)
                self._finish_attempt(
                    attempt.attempt_id,
                    state=(
                        AttemptState.SUCCEEDED
                        if result.outcome == "one_parent"
                        else AttemptState.FAILED
                    ),
                    marker_outcome=result.outcome,
                    parent_memory_id=(
                        result.parent_memory_ids[0]
                        if len(result.parent_memory_ids) == 1
                        else None
                    ),
                )
                if result.outcome != "one_parent":
                    if item.state is ImportItemState.AMBIGUOUS:
                        self._keep_item_ambiguous(
                            job_id, item.item_id, "marker_recheck_unresolved"
                        )
                    else:
                        self._keep_item_failed(
                            job_id, item.item_id, "marker_recheck_unresolved"
                        )
                    return self._ledger.get_import_job(job_id)
                if item.state is ImportItemState.AMBIGUOUS:
                    self._import_item(item, result.parent_memory_ids[0])
                else:
                    self._finish_item_remote_duplicate(
                        item, result.parent_memory_ids[0]
                    )
                    self._advance(job_id, item.ordinal + 1)
                self._queue_job(job_id)
                return await self._run_job_locked(job_id)
            if resolution == "retry":
                if not acknowledge_duplicate_write_risk:
                    raise InvalidStateError("duplicate write risk acknowledgement is required")
                with self._ledger.transaction() as connection:
                    connection.execute(
                        "UPDATE import_items SET state = ?, error_detail = NULL WHERE item_id = ?",
                        (ImportItemState.QUEUED, item.item_id),
                    )
                self._queue_job(job_id)
                return await self._run_job_locked(job_id)
            attempt = self._ledger.append_import_attempt(
                item_id=item.item_id,
                kind=AttemptKind.MARKER_RECHECK,
                marker=visible_import_marker(item.import_id),
            )
            self._finish_attempt(attempt.attempt_id, state=AttemptState.SKIPPED)
            with self._ledger.transaction() as connection:
                connection.execute(
                    "UPDATE import_items SET state = ?, completed_at = ? WHERE item_id = ?",
                    (ImportItemState.SKIPPED, _now(), item.item_id),
                )
            self._advance(job_id, item.ordinal + 1)
            self._queue_job(job_id)
            return await self._run_job_locked(job_id)

    def _require_job_endpoint(self, job: ImportJobRecord) -> None:
        if job.pieces_endpoint != self._client.capabilities.endpoint:
            raise InvalidStateError("import job endpoint differs from the active Pieces endpoint")

    def _paused_item(self, job_id: str) -> ImportItemRecord:
        candidates = [
            item
            for item in self._ledger.list_import_items(job_id)
            if item.state in {ImportItemState.AMBIGUOUS, ImportItemState.FAILED}
        ]
        if not candidates:
            raise InvalidStateError("paused job has no resolvable item")
        return candidates[0]

    def _finish_attempt(
        self,
        attempt_id: str,
        *,
        state: AttemptState,
        marker_outcome: str | None = None,
        parent_memory_id: str | None = None,
        response_id: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_attempts SET state = ?, finished_at = ?, marker_outcome = ?, "
                "parent_memory_id = ?, response_id = ?, error_detail = ? "
                "WHERE attempt_id = ?",
                (
                    state,
                    _now(),
                    marker_outcome,
                    parent_memory_id,
                    response_id,
                    error_detail,
                    attempt_id,
                ),
            )

    def _set_attempt_error(self, attempt_id: str, error_detail: str) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_attempts SET finished_at = COALESCE(finished_at, ?), "
                "error_detail = ? WHERE attempt_id = ?",
                (_now(), error_detail[:1024], attempt_id),
            )

    def _finish_item_remote_duplicate(self, item: ImportItemRecord, memory_id: str) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_items SET state = ?, pieces_memory_id = ?, completed_at = ? "
                "WHERE item_id = ?",
                (ImportItemState.REMOTE_DUPLICATE, memory_id, _now(), item.item_id),
            )

    def _import_item(self, item: ImportItemRecord, memory_id: str) -> None:
        with self._ledger.transaction() as connection:
            candidate = connection.execute(
                "SELECT status, version, group_id FROM candidates WHERE candidate_id = ?",
                (item.candidate_id,),
            ).fetchone()
            if candidate is None or int(candidate["version"]) != item.candidate_version:
                raise OptimisticConflictError("frozen candidate version is stale")
            connection.execute(
                "INSERT OR IGNORE INTO candidate_payload_snapshots "
                "(snapshot_id, candidate_id, candidate_version, source_revision_id, state, "
                "payload_json, recorded_at) "
                "SELECT ?, candidate_id, version, revision_id, status, current_payload_json, ? "
                "FROM candidates WHERE candidate_id = ?",
                (_uuid4(), _now(), item.candidate_id),
            )
            connection.execute(
                "UPDATE candidates SET status = ?, version = version + 1, updated_at = ? "
                "WHERE candidate_id = ?",
                (CandidateStatus.IMPORTED, _now(), item.candidate_id),
            )
            connection.execute(
                "UPDATE import_items SET state = ?, pieces_memory_id = ?, completed_at = ?, "
                "error_detail = NULL WHERE item_id = ?",
                (ImportItemState.IMPORTED, memory_id, _now(), item.item_id),
            )
            if candidate["group_id"] is not None:
                connection.execute(
                    "UPDATE review_groups SET status = ?, version = version + 1, updated_at = ? "
                    "WHERE group_id = ? AND representative_candidate_id = ? AND status = ?",
                    (
                        GroupStatus.IMPORTED,
                        _now(),
                        str(candidate["group_id"]),
                        item.candidate_id,
                        GroupStatus.APPROVED,
                    ),
                )

    def _pause_item(self, job_id: str, item_id: str, reason: str) -> None:
        with self._ledger.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM import_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is not None:
                next_state = (
                    ImportItemState.AMBIGUOUS
                    if row["state"] == ImportItemState.AMBIGUOUS
                    else ImportItemState.FAILED
                )
                connection.execute(
                    "UPDATE import_items SET state = ?, error_detail = ? WHERE item_id = ?",
                    (next_state, reason, item_id),
                )
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = ? WHERE job_id = ?",
                (ImportJobState.PAUSED, reason, job_id),
            )

    def _keep_item_ambiguous(self, job_id: str, item_id: str, reason: str) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_items SET state = ?, error_detail = ? WHERE item_id = ?",
                (ImportItemState.AMBIGUOUS, reason, item_id),
            )
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = ? WHERE job_id = ?",
                (ImportJobState.PAUSED, reason, job_id),
            )

    def _keep_item_failed(self, job_id: str, item_id: str, reason: str) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_items SET state = ?, error_detail = ? WHERE item_id = ?",
                (ImportItemState.FAILED, reason, item_id),
            )
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = ? WHERE job_id = ?",
                (ImportJobState.PAUSED, reason, job_id),
            )

    def _fail_item(self, job_id: str, item_id: str, reason: str) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_items SET state = ?, error_detail = ? WHERE item_id = ?",
                (ImportItemState.FAILED, reason, item_id),
            )
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = ? WHERE job_id = ?",
                (ImportJobState.PAUSED, reason, job_id),
            )

    def _advance(self, job_id: str, ordinal: int) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_jobs SET current_ordinal = ? WHERE job_id = ?",
                (ordinal, job_id),
            )

    def _queue_job(self, job_id: str) -> None:
        with self._ledger.transaction() as connection:
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = NULL WHERE job_id = ?",
                (ImportJobState.QUEUED, job_id),
            )


__all__ = [
    "ApplyConfirmation",
    "DuplicateCheckReport",
    "FindingOverride",
    "ImportInProgressError",
    "ImportService",
    "ReviewService",
    "ScanInProgressError",
    "ScanRoot",
    "ScanService",
    "coalesce_scan_roots",
]
