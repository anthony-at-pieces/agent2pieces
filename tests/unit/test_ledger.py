from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from conftest import digest

from agent2pieces.ledger import (
    ImmutableRecordError,
    Ledger,
    LedgerInstanceLock,
    LedgerInUseError,
    OptimisticConflictError,
)
from agent2pieces.models import (
    AttemptKind,
    CandidatePayload,
    CandidateStatus,
    FindingSeverity,
    ImportItemState,
    ImportJobState,
    ResumeAction,
    ScanState,
    SourceAgent,
    SourceDisposition,
    SourceRevisionIdentity,
)


def make_revision(
    ledger: Ledger,
    *,
    source_hash: str | None = None,
    candidate_input_hash: str | None = None,
    predecessor_revision_id: str | None = None,
) -> tuple[str, str]:
    root_id = ledger.add_source_root(
        agent=SourceAgent.CODEX,
        lexical_path="/memory/codex",
        resolved_path="/memory/codex",
        enabled=True,
        is_default=True,
    )
    revision = ledger.get_or_create_source_revision(
        SourceRevisionIdentity(
            agent=SourceAgent.CODEX,
            root_id=root_id,
            source_key="rollout.md",
            source_hash=source_hash or digest("source"),
            candidate_input_hash=candidate_input_hash or digest("candidate-input"),
        ),
        source_path="/memory/codex/rollout.md",
        predecessor_revision_id=predecessor_revision_id,
    )
    return revision.revision_id, root_id


def make_candidate(ledger: Ledger, payload: CandidatePayload) -> str:
    revision_id, _ = make_revision(ledger)
    candidate = ledger.create_candidate(
        revision_id=revision_id,
        payload=payload,
        payload_hash=digest(payload.model_dump_json()),
        import_id="a" * 26,
    )
    return candidate.candidate_id


def test_initialize_is_idempotent_and_enforces_connection_policy(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    first.initialize()
    first.initialize()

    assert first.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert first.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert first.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    migrations = first.connection.execute(
        "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version"
    ).fetchall()
    assert migrations == [(1, 1), (2, 1)]
    first.close()

    reopened = Ledger(path)
    reopened.initialize()
    assert reopened.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
    reopened.close()


def test_initialize_migrates_v1_import_context_without_resuming_unsafe_job(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    initial_sql = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agent2pieces"
        / "migrations"
        / "001_initial.sql"
    ).read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    connection.executescript(initial_sql)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        ("2026-09-01T00:00:00Z",),
    )
    connection.execute(
        "INSERT INTO settings "
        "(singleton_id, version, ledger_instance_id, mcp_base_url, codex_enabled, "
        "claude_enabled, hermes_enabled, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, 1, 1, 1, ?, ?)",
        (
            str(uuid.uuid4()),
            "http://127.0.0.1:39300",
            "2026-09-01T00:00:00Z",
            "2026-09-01T00:00:00Z",
        ),
    )
    job_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO import_jobs "
        "(job_id, state, requested_at, current_ordinal, remote_search_available) "
        "VALUES (?, ?, ?, 0, 1)",
        (job_id, ImportJobState.QUEUED, "2026-09-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    migrated = Ledger(path)
    migrated.initialize()
    try:
        assert migrated.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]
        job = migrated.get_import_job(job_id)
        assert job.state is ImportJobState.FAILED
        assert job.pieces_endpoint == "unknown"
        assert job.context_hash == ""
        assert job.error_detail == "migration_context_unavailable"
        assert {
            str(row[1])
            for row in migrated.connection.execute("PRAGMA table_info(import_items)")
        } >= {"frozen_write_arguments_json"}
        assert {
            str(row[1])
            for row in migrated.connection.execute("PRAGMA table_info(import_attempts)")
        } >= {"pieces_endpoint"}
    finally:
        migrated.close()


def test_ledger_instance_lock_excludes_another_process(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from agent2pieces.ledger import LedgerInstanceLock",
                    "lock = LedgerInstanceLock(Path(sys.argv[1]))",
                    "lock.acquire()",
                    "print('locked', flush=True)",
                    "sys.stdin.readline()",
                    "lock.close()",
                )
            ),
            str(ledger_path),
        ],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        competing = LedgerInstanceLock(ledger_path)
        with pytest.raises(LedgerInUseError):
            competing.acquire()
    finally:
        if child.stdin is not None:
            child.stdin.write("release\n")
            child.stdin.flush()
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr

    acquired_after_exit = LedgerInstanceLock(ledger_path)
    acquired_after_exit.acquire()
    acquired_after_exit.close()


def test_foreign_keys_are_enforced(ledger: Ledger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        ledger.connection.execute(
            "INSERT INTO source_observations "
            "(observation_id, revision_id, scan_id, source_updated_at, file_mtime_ns, "
            "raw_byte_count, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "2026-01-01T00:00:00Z",
                1,
                1,
                "2026-01-01T00:00:00Z",
            ),
        )


def test_source_revision_identity_includes_candidate_input_hash_and_observations(
    ledger: Ledger,
) -> None:
    revision_id, root_id = make_revision(ledger)
    same = ledger.get_or_create_source_revision(
        SourceRevisionIdentity(
            agent=SourceAgent.CODEX,
            root_id=root_id,
            source_key="rollout.md",
            source_hash=digest("source"),
            candidate_input_hash=digest("candidate-input"),
        ),
        source_path="/memory/codex/rollout.md",
    )
    changed_input = ledger.get_or_create_source_revision(
        SourceRevisionIdentity(
            agent=SourceAgent.CODEX,
            root_id=root_id,
            source_key="rollout.md",
            source_hash=digest("source"),
            candidate_input_hash=digest("title changed"),
        ),
        source_path="/memory/codex/rollout.md",
        predecessor_revision_id=revision_id,
    )
    assert same.revision_id == revision_id
    assert changed_input.revision_id != revision_id
    assert changed_input.predecessor_revision_id == revision_id

    settings = ledger.get_settings()
    scan_id = ledger.create_scan(settings_version=settings.version)
    ledger.record_source_observation(
        revision_id=revision_id,
        scan_id=scan_id,
        source_updated_at="2026-01-02T03:04:05Z",
        file_mtime_ns=123,
        raw_byte_count=456,
    )
    observation = ledger.list_source_observations(revision_id)[0]
    assert observation.file_mtime_ns == 123
    assert observation.raw_byte_count == 456


def test_source_dispositions_safety_findings_and_host_mappings_are_durable(
    ledger: Ledger, payload: CandidatePayload
) -> None:
    settings = ledger.get_settings()
    scan_id = ledger.create_scan(settings_version=settings.version)
    _, root_id = make_revision(ledger)
    disposition_id = ledger.record_source_disposition(
        scan_id=scan_id,
        agent=SourceAgent.CODEX,
        root_id=root_id,
        source_path="/memory/codex/raw.jsonl",
        source_key=None,
        disposition=SourceDisposition.EXCLUDED,
        byte_count=99,
        reason="excluded_raw",
        detail=None,
    )
    disposition = ledger.get_source_disposition(disposition_id)
    assert disposition.reason == "excluded_raw"
    assert disposition.disposition is SourceDisposition.EXCLUDED

    candidate_id = make_candidate(ledger, payload)
    finding_id = ledger.add_safety_finding(
        candidate_id=candidate_id,
        candidate_version=1,
        reason_code="secret_private_key",
        severity=FindingSeverity.BLOCK,
        line_number=4,
    )
    finding = ledger.get_safety_finding(finding_id)
    assert finding.reason_code == "secret_private_key"
    assert finding.line_number == 4
    assert not hasattr(finding, "matched_value")

    mapping_id = ledger.add_host_path_mapping(
        local_root="/work/project", host_root="D:/work/project", project="Agent2Pieces"
    )
    mapping = ledger.get_host_path_mapping(mapping_id)
    assert mapping.local_root == "/work/project"
    assert mapping.host_root == "D:/work/project"
    assert mapping.project == "Agent2Pieces"


def test_settings_updates_use_optimistic_versioning(ledger: Ledger) -> None:
    original = ledger.get_settings()
    updated = ledger.update_settings(
        expected_version=original.version,
        mcp_base_url="http://pieces.example.test:39300",
        codex_enabled=True,
        claude_enabled=False,
        hermes_enabled=True,
    )
    assert updated.version == original.version + 1
    assert updated.mcp_base_url == "http://pieces.example.test:39300"

    with pytest.raises(OptimisticConflictError):
        ledger.update_settings(
            expected_version=original.version,
            mcp_base_url="http://localhost:39300",
            codex_enabled=True,
            claude_enabled=True,
            hermes_enabled=True,
        )
    assert ledger.get_settings().mcp_base_url == "http://pieces.example.test:39300"


def test_candidate_updates_conflict_and_imported_candidates_are_immutable(
    ledger: Ledger, payload: CandidatePayload
) -> None:
    candidate_id = make_candidate(ledger, payload)
    edited_payload = payload.model_copy(update={"title": "Edited title"})
    edited = ledger.update_candidate(
        candidate_id=candidate_id,
        expected_version=1,
        payload=edited_payload,
        payload_hash=digest(edited_payload.model_dump_json()),
        import_id="b" * 26,
        status=CandidateStatus.APPROVED,
    )
    assert edited.version == 2
    assert edited.status is CandidateStatus.APPROVED

    with pytest.raises(OptimisticConflictError):
        ledger.update_candidate(
            candidate_id=candidate_id,
            expected_version=1,
            payload=payload,
            payload_hash=digest(payload.model_dump_json()),
            import_id="a" * 26,
            status=CandidateStatus.PENDING,
        )

    ledger.mark_candidate_imported(candidate_id=candidate_id, expected_version=2)
    with pytest.raises(ImmutableRecordError):
        ledger.update_candidate(
            candidate_id=candidate_id,
            expected_version=3,
            payload=payload,
            payload_hash=digest(payload.model_dump_json()),
            import_id="a" * 26,
            status=CandidateStatus.PENDING,
        )


def test_group_updates_are_optimistic_and_retain_evidence(
    ledger: Ledger, payload: CandidatePayload
) -> None:
    first_id = make_candidate(ledger, payload)
    revision_id, _ = make_revision(
        ledger,
        source_hash=digest("other source"),
        candidate_input_hash=digest("other candidate"),
    )
    second = ledger.create_candidate(
        revision_id=revision_id,
        payload=payload.model_copy(update={"title": "Second"}),
        payload_hash=digest("second"),
        import_id="c" * 26,
    )
    check_id = ledger.create_duplicate_check(candidate_id=first_id, candidate_version=1)
    evidence_id = ledger.add_duplicate_evidence(
        check_id=check_id,
        target_kind="candidate",
        target_key=second.candidate_id,
        classification="likely",
        rule_id="body_cosine",
        cosine=0.95,
        body_shingle_jaccard=0.4,
        title_jaccard=0.3,
    )
    group = ledger.create_review_group(
        title="Same decision",
        candidate_ids=[first_id, second.candidate_id],
        representative_candidate_id=first_id,
        created_from_check_id=check_id,
        evidence_ids=[evidence_id],
    )
    assert ledger.list_review_group_evidence(group.group_id) == [evidence_id]

    changed = ledger.update_group_representative(
        group_id=group.group_id,
        expected_version=1,
        representative_candidate_id=second.candidate_id,
        overridden=True,
    )
    assert changed.version == 2
    assert changed.representative_candidate_id == second.candidate_id
    with pytest.raises(OptimisticConflictError):
        ledger.update_group_representative(
            group_id=group.group_id,
            expected_version=1,
            representative_candidate_id=first_id,
            overridden=True,
        )


def test_import_job_freezes_payload_and_resumes_in_place(
    ledger: Ledger, payload: CandidatePayload
) -> None:
    candidate_id = make_candidate(ledger, payload)
    ledger.update_candidate(
        candidate_id=candidate_id,
        expected_version=1,
        payload=payload,
        payload_hash=digest(payload.model_dump_json()),
        import_id="a" * 26,
        status=CandidateStatus.APPROVED,
    )
    approved = ledger.get_candidate(candidate_id)
    job = ledger.create_import_job(
        candidates=[approved],
        pieces_endpoint="http://pieces.test/mcp",
        context_hash="b" * 64,
        frozen_write_arguments={approved.candidate_id: '{"summary":"frozen"}'},
        remote_search_available=True,
        duplicate_risk_acknowledged=False,
    )
    items = ledger.list_import_items(job.job_id)
    assert len(items) == 1
    assert job.pieces_endpoint == "http://pieces.test/mcp"
    assert job.context_hash == "b" * 64
    item = items[0]
    frozen_before = json.loads(item.frozen_payload_json)

    attempt = ledger.append_import_attempt(
        item_id=item.item_id,
        kind=AttemptKind.MARKER_PREFLIGHT,
        marker="Agent2Pieces Import ID: " + item.import_id,
    )
    ledger.pause_import_job(job_id=job.job_id, reason="search_error")
    resumed = ledger.resume_import_job(
        job_id=job.job_id,
        item_id=item.item_id,
        action=ResumeAction.RECHECK,
    )
    assert resumed.job_id == job.job_id
    assert ledger.list_import_items(job.job_id)[0].item_id == item.item_id
    assert json.loads(ledger.list_import_items(job.job_id)[0].frozen_payload_json) == frozen_before
    attempts = ledger.list_import_attempts(item.item_id)
    assert attempts[0].attempt_id == attempt.attempt_id
    assert attempts[0].pieces_endpoint == "http://pieces.test/mcp"
    assert [entry.attempt_number for entry in attempts] == [1, 2]
    assert attempts[1].kind is AttemptKind.MARKER_RECHECK


def test_startup_recovers_running_scans_and_call_started_writes_as_ambiguous(
    tmp_path: Path, payload: CandidatePayload
) -> None:
    path = tmp_path / "recovery.sqlite3"
    before = Ledger(path)
    before.initialize()
    scan_id = before.create_scan(settings_version=before.get_settings().version)
    before.start_scan(scan_id)

    candidate_id = make_candidate(before, payload)
    before.update_candidate(
        candidate_id=candidate_id,
        expected_version=1,
        payload=payload,
        payload_hash=digest(payload.model_dump_json()),
        import_id="a" * 26,
        status=CandidateStatus.APPROVED,
    )
    job = before.create_import_job(
        candidates=[before.get_candidate(candidate_id)],
        pieces_endpoint="http://pieces.test/mcp",
        context_hash="c" * 64,
        frozen_write_arguments={candidate_id: '{"summary":"frozen"}'},
        remote_search_available=True,
        duplicate_risk_acknowledged=False,
    )
    before.start_import_job(job.job_id)
    item = before.list_import_items(job.job_id)[0]
    attempt = before.append_import_attempt(
        item_id=item.item_id,
        kind=AttemptKind.WRITE,
        marker="Agent2Pieces Import ID: " + item.import_id,
        dispatch_started=True,
    )
    assert attempt.dispatch_started_at is not None
    before.close()

    after = Ledger(path)
    after.initialize()
    assert after.get_scan(scan_id).state is ScanState.FAILED
    assert after.get_import_job(job.job_id).state is ImportJobState.PAUSED
    recovered_item = after.list_import_items(job.job_id)[0]
    assert recovered_item.state is ImportItemState.AMBIGUOUS
    assert after.list_import_attempts(recovered_item.item_id)[0].state.value == "ambiguous"
    after.close()
