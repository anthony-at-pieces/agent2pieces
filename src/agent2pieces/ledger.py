"""SQLite ledger with explicit transactional state transitions."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, SupportsIndex, TypeVar, overload

from pydantic import BaseModel

from agent2pieces.config import DEFAULT_MCP_BASE_URL
from agent2pieces.models import (
    AttemptKind,
    AttemptState,
    CandidatePayload,
    CandidateRecord,
    CandidateStatus,
    FindingSeverity,
    FindingState,
    GroupStatus,
    HostPathMappingRecord,
    ImportAttemptRecord,
    ImportItemRecord,
    ImportItemState,
    ImportJobRecord,
    ImportJobState,
    ResumeAction,
    ReviewGroupRecord,
    SafetyFindingRecord,
    ScanRecord,
    ScanState,
    SettingsRecord,
    SourceAgent,
    SourceDisposition,
    SourceDispositionRecord,
    SourceObservationRecord,
    SourceRevisionIdentity,
    SourceRevisionRecord,
)

MAX_ERROR_DETAIL = 1_024
M = TypeVar("M", bound=BaseModel)


class _LedgerRow(tuple[Any, ...]):
    """Tuple-compatible SQLite row with named column access."""

    _columns: dict[str, int]

    def __new__(
        cls, cursor: sqlite3.Cursor, values: tuple[Any, ...]
    ) -> _LedgerRow:
        instance = super().__new__(cls, values)
        instance._columns = {column[0]: index for index, column in enumerate(cursor.description)}
        return instance

    @overload
    def __getitem__(self, key: SupportsIndex, /) -> Any: ...

    @overload
    def __getitem__(self, key: slice, /) -> tuple[Any, ...]: ...

    @overload
    def __getitem__(self, key: str, /) -> Any: ...

    def __getitem__(self, key: SupportsIndex | slice | str, /) -> Any:
        if isinstance(key, str):
            key = self._columns[key]
        return super().__getitem__(key)

    def as_dict(self) -> dict[str, Any]:
        return {name: self[index] for name, index in self._columns.items()}


class LedgerError(RuntimeError):
    """Base error for ledger state failures."""


class OptimisticConflictError(LedgerError):
    """The caller attempted to update a stale version."""


class ImmutableRecordError(LedgerError):
    """The caller attempted to mutate an imported record."""


class InvalidStateError(LedgerError):
    """The requested state transition is not valid."""


class LedgerInUseError(LedgerError):
    """Another Agent2Pieces process owns the application data directory."""


class LedgerInstanceLock:
    """Nonblocking, process-lifetime ownership lock for one ledger directory."""

    def __init__(self, ledger_path: Path) -> None:
        self.path = ledger_path.parent / ".agent2pieces.lock"
        self._file: Any | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            if lock_file.seek(0, 2) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                locking = vars(msvcrt)["locking"]
                locking(lock_file.fileno(), vars(msvcrt)["LK_NBLCK"], 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.name != "nt":
                self.path.chmod(0o600)
        except (OSError, PermissionError) as error:
            lock_file.close()
            raise LedgerInUseError("application data directory is already in use") from error
        self._file = lock_file

    def close(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                locking = vars(msvcrt)["locking"]
                locking(lock_file.fileno(), vars(msvcrt)["LK_UNLCK"], 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def __enter__(self) -> LedgerInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid4() -> str:
    return str(uuid.uuid4())


def _bounded(value: str | None) -> str | None:
    return value[:MAX_ERROR_DETAIL] if value is not None else None


class Ledger:
    """Durable local state for scans, review decisions, and imports."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerError("ledger is not initialized")
        return self._connection

    def initialize(self) -> None:
        opened_connection = self._connection is None
        if opened_connection:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = _LedgerRow
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection
        try:
            self._apply_migrations()
            self._recover_stale_state()
        except BaseException:
            if opened_connection:
                self.close()
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self, *, exclusive: bool = False) -> Iterator[sqlite3.Connection]:
        mode = "EXCLUSIVE" if exclusive else "IMMEDIATE"
        connection = self.connection
        connection.execute(f"BEGIN {mode}")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _apply_migrations(self) -> None:
        migration_root = Path(__file__).with_name("migrations")
        migrations = (
            (1, migration_root / "001_initial.sql"),
            (2, migration_root / "002_import_context.sql"),
            (3, migration_root / "003_duplicate_evidence_candidates.sql"),
        )
        with self.transaction(exclusive=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for version, migration in migrations:
                present = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if present is not None:
                    continue
                sql = migration.read_text(encoding="utf-8")
                for statement in sql.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                now = _now()
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, now),
                )
                if version == 1:
                    connection.execute(
                        "INSERT INTO settings "
                        "(singleton_id, version, ledger_instance_id, mcp_base_url, "
                        "codex_enabled, claude_enabled, hermes_enabled, created_at, updated_at) "
                        "VALUES (1, 1, ?, ?, 1, 1, 1, ?, ?)",
                        (_uuid4(), DEFAULT_MCP_BASE_URL, now, now),
                    )

    def _recover_stale_state(self) -> None:
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE scan_runs SET state = ?, finished_at = ?, error_count = error_count + 1, "
                "error_detail = ? WHERE state = ?",
                (ScanState.FAILED, now, "startup_recovery", ScanState.RUNNING),
            )
            ambiguous_items = connection.execute(
                "SELECT DISTINCT item_id FROM import_attempts "
                "WHERE dispatch_started_at IS NOT NULL AND state IN (?, ?)",
                (AttemptState.PENDING, AttemptState.AMBIGUOUS),
            ).fetchall()
            for row in ambiguous_items:
                connection.execute(
                    "UPDATE import_attempts SET state = ? WHERE item_id = ? "
                    "AND dispatch_started_at IS NOT NULL AND state IN (?, ?)",
                    (
                        AttemptState.AMBIGUOUS,
                        row["item_id"],
                        AttemptState.PENDING,
                        AttemptState.AMBIGUOUS,
                    ),
                )
                connection.execute(
                    "UPDATE import_items SET state = ? WHERE item_id = ?",
                    (ImportItemState.AMBIGUOUS, row["item_id"]),
                )
                connection.execute(
                    "UPDATE import_jobs SET state = ?, pause_reason = ? "
                    "WHERE job_id = (SELECT job_id FROM import_items WHERE item_id = ?)",
                    (ImportJobState.PAUSED, "ambiguous_write_recovery", row["item_id"]),
                )
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = ? WHERE state = ?",
                (ImportJobState.PAUSED, "startup_recovery", ImportJobState.RUNNING),
            )

    @staticmethod
    def _model(model: type[M], row: _LedgerRow | None) -> M:
        if row is None:
            raise KeyError("ledger record not found")
        return model.model_validate(row.as_dict())

    def get_settings(self) -> SettingsRecord:
        row = self.connection.execute(
            "SELECT version, ledger_instance_id, mcp_base_url, codex_enabled, claude_enabled, "
            "hermes_enabled, created_at, updated_at FROM settings WHERE singleton_id = 1"
        ).fetchone()
        return self._model(SettingsRecord, row)

    def update_settings(
        self,
        *,
        expected_version: int,
        mcp_base_url: str,
        codex_enabled: bool,
        claude_enabled: bool,
        hermes_enabled: bool,
    ) -> SettingsRecord:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE settings SET version = version + 1, mcp_base_url = ?, "
                "codex_enabled = ?, claude_enabled = ?, hermes_enabled = ?, updated_at = ? "
                "WHERE singleton_id = 1 AND version = ?",
                (
                    mcp_base_url,
                    codex_enabled,
                    claude_enabled,
                    hermes_enabled,
                    _now(),
                    expected_version,
                ),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("settings version is stale")
        return self.get_settings()

    def add_source_root(
        self,
        *,
        agent: SourceAgent,
        lexical_path: str,
        resolved_path: str,
        enabled: bool,
        is_default: bool,
    ) -> str:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT root_id FROM source_roots WHERE agent = ? AND resolved_path = ?",
                (agent, resolved_path),
            ).fetchone()
            if existing is not None:
                return str(existing["root_id"])
            root_id = _uuid4()
            connection.execute(
                "INSERT INTO source_roots "
                "(root_id, agent, lexical_path, resolved_path, enabled, is_default, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (root_id, agent, lexical_path, resolved_path, enabled, is_default, _now()),
            )
            return root_id

    def add_host_path_mapping(self, *, local_root: str, host_root: str, project: str) -> str:
        mapping_id = _uuid4()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO host_path_mappings "
                "(mapping_id, local_root, host_root, project, created_at) VALUES (?, ?, ?, ?, ?)",
                (mapping_id, local_root, host_root, project, _now()),
            )
        return mapping_id

    def get_host_path_mapping(self, mapping_id: str) -> HostPathMappingRecord:
        row = self.connection.execute(
            "SELECT * FROM host_path_mappings WHERE mapping_id = ?", (mapping_id,)
        ).fetchone()
        return self._model(HostPathMappingRecord, row)

    def create_scan(self, *, settings_version: int) -> str:
        scan_id = _uuid4()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO scan_runs "
                "(scan_id, state, requested_at, settings_version) VALUES (?, ?, ?, ?)",
                (scan_id, ScanState.QUEUED, _now(), settings_version),
            )
        return scan_id

    def start_scan(self, scan_id: str) -> ScanRecord:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE scan_runs SET state = ?, started_at = ? WHERE scan_id = ? AND state = ?",
                (ScanState.RUNNING, _now(), scan_id, ScanState.QUEUED),
            )
            if changed.rowcount != 1:
                raise InvalidStateError("scan cannot be started")
        return self.get_scan(scan_id)

    def get_scan(self, scan_id: str) -> ScanRecord:
        row = self.connection.execute(
            "SELECT * FROM scan_runs WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        return self._model(ScanRecord, row)

    def get_or_create_source_revision(
        self,
        identity: SourceRevisionIdentity,
        *,
        source_path: str,
        predecessor_revision_id: str | None = None,
    ) -> SourceRevisionRecord:
        values = (
            identity.agent,
            identity.root_id,
            identity.source_key,
            identity.source_hash,
            identity.candidate_input_hash,
        )
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_revisions WHERE agent = ? AND root_id = ? "
                "AND source_key = ? AND source_hash = ? AND candidate_input_hash = ?",
                values,
            ).fetchone()
            if row is None:
                revision_id = _uuid4()
                connection.execute(
                    "INSERT INTO source_revisions "
                    "(revision_id, predecessor_revision_id, agent, root_id, source_key, "
                    "source_path, source_hash, candidate_input_hash, first_observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        predecessor_revision_id,
                        identity.agent,
                        identity.root_id,
                        identity.source_key,
                        source_path,
                        identity.source_hash,
                        identity.candidate_input_hash,
                        _now(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM source_revisions WHERE revision_id = ?", (revision_id,)
                ).fetchone()
        return self._model(SourceRevisionRecord, row)

    def record_source_observation(
        self,
        *,
        revision_id: str,
        scan_id: str,
        source_updated_at: str,
        file_mtime_ns: int,
        raw_byte_count: int,
    ) -> str:
        observation_id = _uuid4()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO source_observations "
                "(observation_id, revision_id, scan_id, source_updated_at, file_mtime_ns, "
                "raw_byte_count, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    revision_id,
                    scan_id,
                    source_updated_at,
                    file_mtime_ns,
                    raw_byte_count,
                    _now(),
                ),
            )
        return observation_id

    def list_source_observations(self, revision_id: str) -> list[SourceObservationRecord]:
        rows = self.connection.execute(
            "SELECT * FROM source_observations WHERE revision_id = ? ORDER BY observed_at",
            (revision_id,),
        ).fetchall()
        return [self._model(SourceObservationRecord, row) for row in rows]

    def record_source_disposition(
        self,
        *,
        scan_id: str,
        agent: SourceAgent,
        root_id: str,
        source_path: str,
        source_key: str | None,
        disposition: SourceDisposition,
        byte_count: int,
        reason: str,
        detail: str | None,
    ) -> str:
        disposition_id = _uuid4()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO source_dispositions "
                "(disposition_id, scan_id, agent, root_id, source_path, source_key, disposition, "
                "byte_count, reason, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    disposition_id,
                    scan_id,
                    agent,
                    root_id,
                    source_path,
                    source_key,
                    disposition,
                    byte_count,
                    reason,
                    _bounded(detail),
                    _now(),
                ),
            )
        return disposition_id

    def get_source_disposition(self, disposition_id: str) -> SourceDispositionRecord:
        row = self.connection.execute(
            "SELECT * FROM source_dispositions WHERE disposition_id = ?", (disposition_id,)
        ).fetchone()
        return self._model(SourceDispositionRecord, row)

    def create_candidate(
        self,
        *,
        revision_id: str,
        payload: CandidatePayload,
        payload_hash: str,
        import_id: str,
    ) -> CandidateRecord:
        candidate_id = _uuid4()
        now = _now()
        payload_json = payload.model_dump_json()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO candidates "
                "(candidate_id, revision_id, status, version, original_payload_json, "
                "current_payload_json, payload_hash, import_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    revision_id,
                    CandidateStatus.PENDING,
                    payload_json,
                    payload_json,
                    payload_hash,
                    import_id,
                    now,
                    now,
                ),
            )
        return self.get_candidate(candidate_id)

    def get_candidate(self, candidate_id: str) -> CandidateRecord:
        row = self.connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        return self._model(CandidateRecord, row)

    def update_candidate(
        self,
        *,
        candidate_id: str,
        expected_version: int,
        payload: CandidatePayload,
        payload_hash: str,
        import_id: str,
        status: CandidateStatus,
    ) -> CandidateRecord:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                raise OptimisticConflictError("candidate version is stale")
            if current["status"] == CandidateStatus.IMPORTED:
                raise ImmutableRecordError("imported candidates are immutable")
            connection.execute(
                "INSERT INTO candidate_payload_snapshots "
                "(snapshot_id, candidate_id, candidate_version, source_revision_id, state, "
                "payload_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _uuid4(),
                    candidate_id,
                    expected_version,
                    current["revision_id"],
                    current["status"],
                    current["current_payload_json"],
                    _now(),
                ),
            )
            connection.execute(
                "UPDATE candidates SET status = ?, version = version + 1, "
                "current_payload_json = ?, payload_hash = ?, import_id = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (
                    status,
                    payload.model_dump_json(),
                    payload_hash,
                    import_id,
                    _now(),
                    candidate_id,
                ),
            )
        return self.get_candidate(candidate_id)

    def mark_candidate_imported(
        self, *, candidate_id: str, expected_version: int
    ) -> CandidateRecord:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT status, version FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                raise OptimisticConflictError("candidate version is stale")
            if current["status"] == CandidateStatus.IMPORTED:
                raise ImmutableRecordError("candidate is already imported")
            connection.execute(
                "UPDATE candidates SET status = ?, version = version + 1, updated_at = ? "
                "WHERE candidate_id = ?",
                (CandidateStatus.IMPORTED, _now(), candidate_id),
            )
        return self.get_candidate(candidate_id)

    def add_safety_finding(
        self,
        *,
        candidate_id: str,
        candidate_version: int,
        reason_code: str,
        severity: FindingSeverity,
        line_number: int | None,
    ) -> str:
        finding_id = _uuid4()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO safety_findings "
                "(finding_id, candidate_id, candidate_version, reason_code, severity, "
                "line_number, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finding_id,
                    candidate_id,
                    candidate_version,
                    reason_code,
                    severity,
                    line_number,
                    FindingState.OPEN,
                    _now(),
                ),
            )
        return finding_id

    def get_safety_finding(self, finding_id: str) -> SafetyFindingRecord:
        row = self.connection.execute(
            "SELECT * FROM safety_findings WHERE finding_id = ?", (finding_id,)
        ).fetchone()
        return self._model(SafetyFindingRecord, row)

    def create_duplicate_check(self, *, candidate_id: str, candidate_version: int) -> str:
        check_id = _uuid4()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO duplicate_checks "
                "(check_id, candidate_id, candidate_version, coverage, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (check_id, candidate_id, candidate_version, "local", _now()),
            )
        return check_id

    def add_duplicate_evidence(
        self,
        *,
        check_id: str,
        candidate_versions: Mapping[str, int],
        target_kind: str,
        target_key: str,
        candidate_target_keys: Mapping[str, str] | None = None,
        candidate_target_titles: Mapping[str, str | None] | None = None,
        classification: str,
        rule_id: str,
        cosine: float | None = None,
        body_shingle_jaccard: float | None = None,
        title_jaccard: float | None = None,
        target_title: str | None = None,
        target_excerpt: str | None = None,
        payload_hash: str | None = None,
        remote_rank: int | None = None,
    ) -> str:
        evidence_id = _uuid4()
        owner_target_keys = dict.fromkeys(candidate_versions, target_key)
        if candidate_target_keys is not None:
            if set(candidate_target_keys) != set(candidate_versions):
                raise InvalidStateError("candidate target keys must match evidence owners")
            owner_target_keys.update(candidate_target_keys)
        owner_target_titles: dict[str, str | None] = dict.fromkeys(
            candidate_versions, target_title
        )
        if candidate_target_titles is not None:
            if set(candidate_target_titles) != set(candidate_versions):
                raise InvalidStateError("candidate target titles must match evidence owners")
            owner_target_titles.update(candidate_target_titles)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO duplicate_evidence "
                "(evidence_id, check_id, target_kind, target_key, target_title, target_excerpt, "
                "payload_hash, cosine, body_shingle_jaccard, title_jaccard, classification, "
                "rule_id, remote_rank) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    check_id,
                    target_kind,
                    target_key,
                    target_title,
                    _bounded(target_excerpt),
                    payload_hash,
                    cosine,
                    body_shingle_jaccard,
                    title_jaccard,
                    classification,
                    rule_id,
                    remote_rank,
                ),
            )
            connection.executemany(
                "INSERT INTO duplicate_evidence_candidates "
                "(evidence_id, candidate_id, candidate_version, target_key, target_title) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        evidence_id,
                        candidate_id,
                        candidate_version,
                        owner_target_keys[candidate_id],
                        owner_target_titles[candidate_id],
                    )
                    for candidate_id, candidate_version in sorted(
                        candidate_versions.items()
                    )
                ],
            )
        return evidence_id

    def create_review_group(
        self,
        *,
        title: str,
        candidate_ids: Sequence[str],
        representative_candidate_id: str,
        created_from_check_id: str | None,
        evidence_ids: Sequence[str],
    ) -> ReviewGroupRecord:
        if representative_candidate_id not in candidate_ids:
            raise InvalidStateError("representative must be a group member")
        group_id = _uuid4()
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO review_groups "
                "(group_id, title, representative_candidate_id, status, version, "
                "created_from_check_id, representative_overridden, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, 0, ?, ?)",
                (
                    group_id,
                    title,
                    representative_candidate_id,
                    GroupStatus.DRAFT,
                    created_from_check_id,
                    now,
                    now,
                ),
            )
            for candidate_id in candidate_ids:
                changed = connection.execute(
                    "UPDATE candidates SET group_id = ?, updated_at = ? "
                    "WHERE candidate_id = ? AND group_id IS NULL",
                    (group_id, now, candidate_id),
                )
                if changed.rowcount != 1:
                    raise InvalidStateError("candidate is missing or already grouped")
            for evidence_id in evidence_ids:
                connection.execute(
                    "INSERT INTO review_group_evidence(group_id, evidence_id) VALUES (?, ?)",
                    (group_id, evidence_id),
                )
        return self._get_review_group(group_id)

    def _get_review_group(self, group_id: str) -> ReviewGroupRecord:
        row = self.connection.execute(
            "SELECT * FROM review_groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        return self._model(ReviewGroupRecord, row)

    def list_review_group_evidence(self, group_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT evidence_id FROM review_group_evidence WHERE group_id = ? "
            "ORDER BY evidence_id",
            (group_id,),
        ).fetchall()
        return [str(row["evidence_id"]) for row in rows]

    def update_group_representative(
        self,
        *,
        group_id: str,
        expected_version: int,
        representative_candidate_id: str,
        overridden: bool,
    ) -> ReviewGroupRecord:
        with self.transaction() as connection:
            member = connection.execute(
                "SELECT 1 FROM candidates WHERE candidate_id = ? AND group_id = ?",
                (representative_candidate_id, group_id),
            ).fetchone()
            if member is None:
                raise InvalidStateError("representative must be a group member")
            changed = connection.execute(
                "UPDATE review_groups SET representative_candidate_id = ?, "
                "representative_overridden = ?, version = version + 1, updated_at = ? "
                "WHERE group_id = ? AND version = ?",
                (representative_candidate_id, overridden, _now(), group_id, expected_version),
            )
            if changed.rowcount != 1:
                raise OptimisticConflictError("group version is stale")
        return self._get_review_group(group_id)

    def create_import_job(
        self,
        *,
        candidates: Sequence[CandidateRecord],
        pieces_endpoint: str,
        context_hash: str,
        frozen_write_arguments: Mapping[str, str],
        remote_search_available: bool,
        duplicate_risk_acknowledged: bool,
    ) -> ImportJobRecord:
        if not remote_search_available and not duplicate_risk_acknowledged:
            raise InvalidStateError("remote duplicate risk acknowledgement is required")
        if not candidates:
            raise InvalidStateError("an import job requires candidates")
        job_id = _uuid4()
        now = _now()
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM import_jobs WHERE state IN (?, ?, ?) LIMIT 1",
                (
                    ImportJobState.QUEUED,
                    ImportJobState.RUNNING,
                    ImportJobState.PAUSED,
                ),
            ).fetchone()
            if active is not None:
                raise InvalidStateError("an import job is already active")
            connection.execute(
                "INSERT INTO import_jobs "
                "(job_id, state, requested_at, current_ordinal, remote_search_available, "
                "duplicate_risk_acknowledged_at, duplicate_risk_ack_text_version, "
                "pieces_endpoint, context_hash) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    ImportJobState.QUEUED,
                    now,
                    remote_search_available,
                    now if duplicate_risk_acknowledged else None,
                    "v1" if duplicate_risk_acknowledged else None,
                    pieces_endpoint,
                    context_hash,
                ),
            )
            for ordinal, candidate in enumerate(candidates):
                if candidate.status is not CandidateStatus.APPROVED:
                    raise InvalidStateError("only approved candidates can be imported")
                connection.execute(
                    "INSERT INTO import_items "
                    "(item_id, job_id, ordinal, candidate_id, candidate_version, "
                    "frozen_payload_json, frozen_write_arguments_json, import_id, state, "
                    "attempt_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        _uuid4(),
                        job_id,
                        ordinal,
                        candidate.candidate_id,
                        candidate.version,
                        candidate.current_payload_json,
                        frozen_write_arguments[candidate.candidate_id],
                        candidate.import_id,
                        ImportItemState.QUEUED,
                    ),
                )
        return self.get_import_job(job_id)

    def get_import_job(self, job_id: str) -> ImportJobRecord:
        row = self.connection.execute(
            "SELECT * FROM import_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._model(ImportJobRecord, row)

    def start_import_job(self, job_id: str) -> ImportJobRecord:
        with self.transaction() as connection:
            running = connection.execute(
                "SELECT 1 FROM import_jobs WHERE state = ? AND job_id <> ?",
                (ImportJobState.RUNNING, job_id),
            ).fetchone()
            if running is not None:
                raise InvalidStateError("an import job is already running")
            changed = connection.execute(
                "UPDATE import_jobs SET state = ?, started_at = COALESCE(started_at, ?) "
                "WHERE job_id = ? AND state = ?",
                (ImportJobState.RUNNING, _now(), job_id, ImportJobState.QUEUED),
            )
            if changed.rowcount != 1:
                raise InvalidStateError("import job cannot be started")
        return self.get_import_job(job_id)

    def pause_import_job(self, *, job_id: str, reason: str) -> ImportJobRecord:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = ? WHERE job_id = ? "
                "AND state IN (?, ?)",
                (
                    ImportJobState.PAUSED,
                    _bounded(reason),
                    job_id,
                    ImportJobState.QUEUED,
                    ImportJobState.RUNNING,
                ),
            )
            if changed.rowcount != 1:
                raise InvalidStateError("import job cannot be paused")
        return self.get_import_job(job_id)

    def list_import_items(self, job_id: str) -> list[ImportItemRecord]:
        rows = self.connection.execute(
            "SELECT * FROM import_items WHERE job_id = ? ORDER BY ordinal", (job_id,)
        ).fetchall()
        return [self._model(ImportItemRecord, row) for row in rows]

    def append_import_attempt(
        self,
        *,
        item_id: str,
        kind: AttemptKind,
        marker: str,
        dispatch_started: bool = False,
    ) -> ImportAttemptRecord:
        with self.transaction() as connection:
            attempt_id = self._append_import_attempt(
                connection,
                item_id=item_id,
                kind=kind,
                marker=marker,
                dispatch_started=dispatch_started,
            )
        return self._get_import_attempt(attempt_id)

    def _append_import_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: str,
        kind: AttemptKind,
        marker: str,
        dispatch_started: bool = False,
        state: AttemptState | None = None,
    ) -> str:
        item = connection.execute(
            "SELECT i.attempt_count, j.pieces_endpoint FROM import_items i "
            "JOIN import_jobs j ON j.job_id = i.job_id WHERE i.item_id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise KeyError("import item not found")
        attempt_number = int(item["attempt_count"]) + 1
        attempt_id = _uuid4()
        now = _now()
        attempt_state = state or (
            AttemptState.AMBIGUOUS if dispatch_started else AttemptState.PENDING
        )
        connection.execute(
            "INSERT INTO import_attempts "
            "(attempt_id, item_id, attempt_number, kind, state, marker, pieces_endpoint, "
            "started_at, dispatch_started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                item_id,
                attempt_number,
                kind,
                attempt_state,
                marker,
                str(item["pieces_endpoint"]),
                now,
                now if dispatch_started else None,
            ),
        )
        item_state = (
            ImportItemState.AMBIGUOUS
            if dispatch_started
            else ImportItemState.PREFLIGHT
            if kind in {AttemptKind.MARKER_PREFLIGHT, AttemptKind.MARKER_RECHECK}
            else ImportItemState.QUEUED
        )
        connection.execute(
            "UPDATE import_items SET attempt_count = ?, state = ? WHERE item_id = ?",
            (attempt_number, item_state, item_id),
        )
        return attempt_id

    def _get_import_attempt(self, attempt_id: str) -> ImportAttemptRecord:
        row = self.connection.execute(
            "SELECT * FROM import_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return self._model(ImportAttemptRecord, row)

    def list_import_attempts(self, item_id: str) -> list[ImportAttemptRecord]:
        rows = self.connection.execute(
            "SELECT * FROM import_attempts WHERE item_id = ? ORDER BY attempt_number",
            (item_id,),
        ).fetchall()
        return [self._model(ImportAttemptRecord, row) for row in rows]

    def resume_import_job(
        self, *, job_id: str, item_id: str, action: ResumeAction
    ) -> ImportJobRecord:
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT state FROM import_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            item = connection.execute(
                "SELECT import_id FROM import_items WHERE item_id = ? AND job_id = ?",
                (item_id, job_id),
            ).fetchone()
            if job is None or job["state"] != ImportJobState.PAUSED or item is None:
                raise InvalidStateError("only a paused job item can be resumed")
            marker = f"Agent2Pieces Import ID: {item['import_id']}"
            if action is ResumeAction.RECHECK:
                self._append_import_attempt(
                    connection,
                    item_id=item_id,
                    kind=AttemptKind.MARKER_RECHECK,
                    marker=marker,
                )
            elif action is ResumeAction.RETRY:
                self._append_import_attempt(
                    connection,
                    item_id=item_id,
                    kind=AttemptKind.MARKER_PREFLIGHT,
                    marker=marker,
                )
            else:
                self._append_import_attempt(
                    connection,
                    item_id=item_id,
                    kind=AttemptKind.MARKER_RECHECK,
                    marker=marker,
                    state=AttemptState.SKIPPED,
                )
                connection.execute(
                    "UPDATE import_items SET state = ?, completed_at = ? WHERE item_id = ?",
                    (ImportItemState.SKIPPED, _now(), item_id),
                )
            connection.execute(
                "UPDATE import_jobs SET state = ?, pause_reason = NULL WHERE job_id = ?",
                (ImportJobState.QUEUED, job_id),
            )
        return self.get_import_job(job_id)
