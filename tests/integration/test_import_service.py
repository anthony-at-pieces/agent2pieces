from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent2pieces.ledger import InvalidStateError, Ledger, OptimisticConflictError
from agent2pieces.mcp_client import MarkerSearchResult, PathMapping
from agent2pieces.models import (
    AttemptKind,
    AttemptState,
    CandidatePayload,
    CandidateStatus,
    GroupStatus,
    ImportItemState,
    ImportJobState,
)
from agent2pieces.services import (
    ApplyConfirmation,
    ImportInProgressError,
    ImportService,
)

from .helpers import FakePiecesClient, add_candidate, approve_candidate


def confirmation(
    client: FakePiecesClient, count: int, *, context_hash: str = "0" * 64
) -> ApplyConfirmation:
    return ApplyConfirmation(
        confirmed=True,
        pieces_endpoint=client.capabilities.endpoint,
        selected_write_count=count,
        context_hash=context_hash,
    )


def create_job(
    service: ImportService,
    client: FakePiecesClient,
    candidates: list[object],
    *,
    acknowledge_remote_duplicate_risk: bool = False,
) -> object:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    candidate_versions = {
        candidate.candidate_id: candidate.version for candidate in candidates
    }
    payload_hashes = {
        candidate.candidate_id: candidate.payload_hash for candidate in candidates
    }
    preview = service.preview_job(
        candidate_ids=candidate_ids,
        candidate_versions=candidate_versions,
        displayed_payload_hashes=payload_hashes,
    )
    return service.create_job(
        candidate_ids=candidate_ids,
        candidate_versions=candidate_versions,
        displayed_payload_hashes=payload_hashes,
        confirmation=confirmation(
            client, len(candidates), context_hash=preview.context_hash
        ),
        acknowledge_remote_duplicate_risk=acknowledge_remote_duplicate_risk,
    )


def test_job_creation_validates_confirmation_versions_hashes_findings_and_groups(
    ledger: Ledger,
) -> None:
    first = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="First", markdown_body="Import the first memory."),
            source_key="first.md",
            source_path="/codex/first.md",
        ),
    )
    second = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Second", markdown_body="Import the second memory."),
            source_key="second.md",
            source_path="/codex/second.md",
        ),
    )
    client = FakePiecesClient()
    service = ImportService(ledger, client)

    with pytest.raises(InvalidStateError, match="confirmation"):
        service.create_job(
            candidate_ids=(first.candidate_id,),
            candidate_versions={first.candidate_id: first.version},
            displayed_payload_hashes={first.candidate_id: first.payload_hash},
            confirmation=ApplyConfirmation(
                confirmed=True,
                pieces_endpoint="http://wrong.test/mcp",
                selected_write_count=1,
                context_hash="0" * 64,
            ),
            acknowledge_remote_duplicate_risk=False,
        )
    with pytest.raises(OptimisticConflictError):
        service.create_job(
            candidate_ids=(first.candidate_id,),
            candidate_versions={first.candidate_id: first.version},
            displayed_payload_hashes={first.candidate_id: "0" * 64},
            confirmation=confirmation(client, 1),
            acknowledge_remote_duplicate_risk=False,
        )

    finding_id = ledger.add_safety_finding(
        candidate_id=first.candidate_id,
        candidate_version=first.version,
        reason_code="pii_email",
        severity="warn",
        line_number=1,
    )
    with pytest.raises(InvalidStateError, match="finding"):
        create_job(service, client, [first])
    ledger.connection.execute(
        "UPDATE safety_findings SET state = 'overridden', override_reason = 'Reviewed source', "
        "override_at = '2026-09-01T00:00:00Z' WHERE finding_id = ?",
        (finding_id,),
    )

    group = ledger.create_review_group(
        title="Two memories",
        candidate_ids=(first.candidate_id, second.candidate_id),
        representative_candidate_id=first.candidate_id,
        created_from_check_id=None,
        evidence_ids=(),
    )
    ledger.connection.execute(
        "UPDATE review_groups SET status = ? WHERE group_id = ?",
        (GroupStatus.APPROVED, group.group_id),
    )
    with pytest.raises(InvalidStateError, match="representative"):
        create_job(service, client, [second])

    job = create_job(service, client, [first])
    items = ledger.list_import_items(job.job_id)
    assert [item.candidate_id for item in items] == [first.candidate_id]
    assert json.loads(items[0].frozen_payload_json) == first.payload.model_dump(mode="json")
    assert items[0].candidate_version == first.version
    assert items[0].import_id == first.import_id


def test_no_search_requires_visible_per_job_duplicate_risk_acknowledgement(
    ledger: Ledger,
) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="No search", markdown_body="A local-only import."),
        ),
    )
    client = FakePiecesClient(search_available=False)
    service = ImportService(ledger, client)

    with pytest.raises(InvalidStateError, match="remote duplicate risk"):
        create_job(service, client, [candidate])

    job = create_job(
        service,
        client,
        [candidate],
        acknowledge_remote_duplicate_risk=True,
    )
    assert job.duplicate_risk_acknowledged_at is not None
    assert job.duplicate_risk_ack_text_version == "v1"


@pytest.mark.parametrize(
    "active_state",
    [ImportJobState.QUEUED, ImportJobState.RUNNING, ImportJobState.PAUSED],
)
def test_job_creation_rejects_every_active_job_state(
    ledger: Ledger,
    active_state: ImportJobState,
) -> None:
    first = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="First", markdown_body="First active job."),
            source_key="first-active.md",
            source_path="/codex/first-active.md",
        ),
    )
    second = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Second", markdown_body="Second active job."),
            source_key="second-active.md",
            source_path="/codex/second-active.md",
        ),
    )
    client = FakePiecesClient()
    service = ImportService(ledger, client)
    active = create_job(service, client, [first])
    if active_state is ImportJobState.RUNNING:
        ledger.start_import_job(active.job_id)
    elif active_state is ImportJobState.PAUSED:
        ledger.pause_import_job(job_id=active.job_id, reason="review_required")

    with pytest.raises(InvalidStateError, match="already active"):
        create_job(service, client, [second])


@pytest.mark.asyncio
async def test_local_ledger_blocks_same_import_id_without_remote_search(
    ledger: Ledger,
) -> None:
    payload = CandidatePayload(
        title="Same content",
        markdown_body="This approved payload must be written only once.",
    )
    first = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=payload,
            source_key="same-one.md",
            source_path="/codex/same-one.md",
        ),
    )
    second = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=payload,
            source_key="same-two.md",
            source_path="/codex/same-two.md",
        ),
    )
    client = FakePiecesClient(search_available=False)
    service = ImportService(ledger, client)
    job = create_job(
        service,
        client,
        [first, second],
        acknowledge_remote_duplicate_risk=True,
    )

    completed = await service.run_job(job.job_id)

    items = ledger.list_import_items(job.job_id)
    assert completed.state is ImportJobState.COMPLETED
    assert [item.state for item in items] == [
        ImportItemState.IMPORTED,
        ImportItemState.REMOTE_DUPLICATE,
    ]
    assert items[1].pieces_memory_id == items[0].pieces_memory_id == "memory-1"
    assert len(client.arguments) == 1


@pytest.mark.asyncio
async def test_local_ledger_blocks_prior_remote_duplicate_without_remote_search(
    ledger: Ledger,
) -> None:
    payload = CandidatePayload(
        title="Already known",
        markdown_body="A prior completed item already resolved this import marker.",
    )
    first = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=payload,
            source_key="known-one.md",
            source_path="/codex/known-one.md",
        ),
    )
    searchable = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(
                outcome="one_parent",
                coverage="complete",
                parent_memory_ids=("existing-memory",),
            ),
        )
    )
    first_service = ImportService(ledger, searchable)
    first_job = create_job(first_service, searchable, [first])
    await first_service.run_job(first_job.job_id)

    second = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=payload,
            source_key="known-two.md",
            source_path="/codex/known-two.md",
        ),
    )
    offline = FakePiecesClient(search_available=False)
    second_service = ImportService(ledger, offline)
    second_job = create_job(
        second_service,
        offline,
        [second],
        acknowledge_remote_duplicate_risk=True,
    )

    completed = await second_service.run_job(second_job.job_id)

    item = ledger.list_import_items(second_job.job_id)[0]
    assert completed.state is ImportJobState.COMPLETED
    assert item.state is ImportItemState.REMOTE_DUPLICATE
    assert item.pieces_memory_id == "existing-memory"
    assert offline.arguments == []


@pytest.mark.asyncio
async def test_every_searchable_item_has_preflight_and_writes_are_sequential(
    ledger: Ledger,
) -> None:
    first = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="First", markdown_body="First sequential write."),
            source_key="first.md",
            source_path="/codex/first.md",
        ),
    )
    second = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Second", markdown_body="Second sequential write."),
            source_key="second.md",
            source_path="/codex/second.md",
        ),
    )
    client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        )
    )
    service = ImportService(ledger, client)
    job = create_job(service, client, [first, second])

    completed = await service.run_job(job.job_id)

    assert completed.state is ImportJobState.COMPLETED
    items = ledger.list_import_items(job.job_id)
    assert [item.state for item in items] == [ImportItemState.IMPORTED, ImportItemState.IMPORTED]
    assert [ledger.get_candidate(item.candidate_id).status for item in items] == [
        CandidateStatus.IMPORTED,
        CandidateStatus.IMPORTED,
    ]
    assert client.maximum_active_writes == 1
    assert client.events == [
        f"build:{first.import_id}",
        f"build:{second.import_id}",
        f"build:{first.import_id}",
        f"build:{second.import_id}",
        f"search:{first.import_id}",
        "sdk_entry",
        f"search:{second.import_id}",
        "sdk_entry",
    ]
    assert [attempt.kind for attempt in ledger.list_import_attempts(items[0].item_id)] == [
        AttemptKind.MARKER_PREFLIGHT,
        AttemptKind.WRITE,
    ]


@pytest.mark.asyncio
async def test_real_import_loads_and_passes_validated_saved_host_mappings(
    ledger: Ledger,
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "project"
    source_path = local_root / "memory" / "topic.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# Topic\nBody\n", encoding="utf-8")
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Mapped", markdown_body="Mapped body."),
            source_key="memory/topic.md",
            source_path=str(source_path),
        ),
    )
    ledger.add_host_path_mapping(
        local_root=str(local_root.resolve()),
        host_root="D:/shared/project",
        project="agent2pieces",
    )
    client = FakePiecesClient()
    service = ImportService(ledger, client)
    job = create_job(service, client, [candidate])
    ledger.connection.execute("DELETE FROM host_path_mappings")
    ledger.add_host_path_mapping(
        local_root=str(local_root.resolve()),
        host_root="Z:/changed/project",
        project="changed-after-confirmation",
    )

    completed = await service.run_job(job.job_id)

    assert completed.state is ImportJobState.COMPLETED
    assert client.mappings == [
        (
            PathMapping(
                local_root=local_root.resolve(),
                host_root="D:/shared/project",
                project="agent2pieces",
            ),
        ),
        (
            PathMapping(
                local_root=local_root.resolve(),
                host_root="D:/shared/project",
                project="agent2pieces",
            ),
        ),
    ]
    assert client.arguments[0]["project"] == "agent2pieces"
    assert client.arguments[0]["files"] == ["D:/shared/project/memory/topic.md"]


@pytest.mark.asyncio
async def test_invalid_saved_host_mapping_stops_before_write(
    ledger: Ledger,
) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Mapped", markdown_body="Mapped body."),
        ),
    )
    ledger.add_host_path_mapping(
        local_root="relative/path",
        host_root="D:/shared/project",
        project="agent2pieces",
    )
    client = FakePiecesClient()
    service = ImportService(ledger, client)
    with pytest.raises(InvalidStateError, match="saved host path mapping"):
        create_job(service, client, [candidate])
    assert client.arguments == []


@pytest.mark.asyncio
async def test_remote_marker_match_skips_write_and_leaves_candidate_approved(
    ledger: Ledger,
) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(
                title="Already remote", markdown_body="This marker already exists in Pieces."
            ),
        ),
    )
    client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(
                outcome="one_parent",
                coverage="complete",
                parent_memory_ids=("existing-memory",),
            ),
        )
    )
    service = ImportService(ledger, client)
    job = create_job(service, client, [candidate])

    completed = await service.run_job(job.job_id)

    item = ledger.list_import_items(job.job_id)[0]
    assert completed.state is ImportJobState.COMPLETED
    assert item.state is ImportItemState.REMOTE_DUPLICATE
    assert ledger.get_candidate(candidate.candidate_id).status is CandidateStatus.APPROVED
    assert client.arguments == []
    attempts = ledger.list_import_attempts(item.item_id)
    assert len(attempts) == 1
    assert attempts[0].marker_outcome == "one_parent"


@pytest.mark.asyncio
async def test_dispatch_boundary_is_committed_before_sdk_entry(ledger: Ledger) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(
                title="Dispatch boundary", markdown_body="Persist ambiguity before the call."
            ),
        ),
    )
    client = FakePiecesClient()
    service = ImportService(ledger, client)
    job = create_job(service, client, [candidate])
    item = ledger.list_import_items(job.job_id)[0]

    def assert_durable_boundary() -> None:
        row = ledger.connection.execute(
            "SELECT state, dispatch_started_at FROM import_attempts "
            "WHERE item_id = ? ORDER BY attempt_number DESC LIMIT 1",
            (item.item_id,),
        ).fetchone()
        assert row["state"] == AttemptState.AMBIGUOUS
        assert row["dispatch_started_at"] is not None
        assert ledger.list_import_items(job.job_id)[0].state is ImportItemState.AMBIGUOUS

    client.sdk_entry_hook = assert_durable_boundary
    await service.run_job(job.job_id)

    write_attempt = ledger.list_import_attempts(item.item_id)[-1]
    assert write_attempt.state is AttemptState.SUCCEEDED
    assert write_attempt.dispatch_started_at is not None


@pytest.mark.parametrize(
    ("write_outcome", "expected_item_state", "expected_attempts"),
    [
        ("pre_failure", ImportItemState.FAILED, 1),
        ("post_failure", ImportItemState.AMBIGUOUS, 2),
    ],
)
@pytest.mark.asyncio
async def test_pre_and_post_initiation_failures_have_distinct_durable_states(
    ledger: Ledger,
    write_outcome: str,
    expected_item_state: ImportItemState,
    expected_attempts: int,
) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Failure", markdown_body="Stop after a write failure."),
        ),
    )
    client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        ),
        write_results=(write_outcome,),
    )
    service = ImportService(ledger, client)
    job = create_job(service, client, [candidate])

    paused = await service.run_job(job.job_id)

    item = ledger.list_import_items(job.job_id)[0]
    assert paused.state is ImportJobState.PAUSED
    assert item.state is expected_item_state
    assert ledger.get_candidate(candidate.candidate_id).status is CandidateStatus.APPROVED
    attempts = ledger.list_import_attempts(item.item_id)
    assert len(attempts) == expected_attempts
    if write_outcome == "pre_failure":
        assert all(attempt.kind is not AttemptKind.WRITE for attempt in attempts)
    else:
        assert attempts[-1].state is AttemptState.AMBIGUOUS
        assert attempts[-1].dispatch_started_at is not None


@pytest.mark.asyncio
async def test_ambiguous_current_job_write_is_recovered_only_by_one_parent(
    ledger: Ledger,
) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Recover", markdown_body="Recover by exact marker."),
        ),
    )
    client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(
                outcome="one_parent",
                coverage="complete",
                parent_memory_ids=("created-memory",),
            ),
        ),
        write_results=("post_failure",),
    )
    service = ImportService(ledger, client)
    job = create_job(service, client, [candidate])

    completed = await service.run_job(job.job_id)

    item = ledger.list_import_items(job.job_id)[0]
    assert completed.state is ImportJobState.COMPLETED
    assert item.state is ImportItemState.IMPORTED
    assert item.pieces_memory_id == "created-memory"
    assert ledger.get_candidate(candidate.candidate_id).status is CandidateStatus.IMPORTED


@pytest.mark.asyncio
async def test_recheck_retry_and_skip_mutate_same_paused_job_and_append_attempts(
    ledger: Ledger,
) -> None:
    retry_candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Retry", markdown_body="Retry only after approval."),
            source_key="retry.md",
            source_path="/codex/retry.md",
        ),
    )
    client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        ),
        write_results=("post_failure", "success"),
    )
    service = ImportService(ledger, client)
    job = create_job(service, client, [retry_candidate])
    paused = await service.run_job(job.job_id)
    item_before = ledger.list_import_items(job.job_id)[0]
    frozen_before = item_before.frozen_payload_json
    attempt_count_before = len(ledger.list_import_attempts(item_before.item_id))
    assert paused.state is ImportJobState.PAUSED

    rechecked = await service.resume_job(job_id=job.job_id, resolution="recheck")
    assert rechecked.job_id == job.job_id
    assert rechecked.state is ImportJobState.PAUSED
    with pytest.raises(InvalidStateError, match="acknowledg"):
        await service.resume_job(job_id=job.job_id, resolution="retry")

    completed = await service.resume_job(
        job_id=job.job_id,
        resolution="retry",
        acknowledge_duplicate_write_risk=True,
    )
    item_after = ledger.list_import_items(job.job_id)[0]
    assert completed.job_id == job.job_id
    assert completed.state is ImportJobState.COMPLETED
    assert item_after.item_id == item_before.item_id
    assert item_after.frozen_payload_json == frozen_before
    assert len(ledger.list_import_attempts(item_after.item_id)) > attempt_count_before

    skip_candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Skip", markdown_body="Leave skipped content approved."),
            source_key="skip.md",
            source_path="/codex/skip.md",
        ),
    )
    skip_client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        ),
        write_results=("post_failure",),
    )
    skip_service = ImportService(ledger, skip_client)
    skip_job = create_job(skip_service, skip_client, [skip_candidate])
    await skip_service.run_job(skip_job.job_id)
    skipped = await skip_service.resume_job(job_id=skip_job.job_id, resolution="skip")
    skipped_item = ledger.list_import_items(skip_job.job_id)[0]
    assert skipped.job_id == skip_job.job_id
    assert skipped.state is ImportJobState.COMPLETED
    assert skipped_item.state is ImportItemState.SKIPPED
    assert ledger.get_candidate(skip_candidate.candidate_id).status is CandidateStatus.APPROVED


@pytest.mark.asyncio
async def test_startup_recovery_keeps_frozen_job_for_exact_marker_recheck(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.sqlite3"
    before = Ledger(database)
    before.initialize()
    candidate = approve_candidate(
        before,
        add_candidate(
            before,
            payload=CandidatePayload(title="Restart", markdown_body="Recover after restart."),
        ),
    )
    first_client = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        ),
        write_results=("post_failure",),
    )
    first_service = ImportService(before, first_client)
    job = create_job(first_service, first_client, [candidate])
    await first_service.run_job(job.job_id)
    item_before = before.list_import_items(job.job_id)[0]
    before.close()

    after = Ledger(database)
    after.initialize()
    try:
        recovered = after.get_import_job(job.job_id)
        assert recovered.state is ImportJobState.PAUSED
        client = FakePiecesClient(
            marker_results=(
                MarkerSearchResult(
                    outcome="one_parent",
                    coverage="complete",
                    parent_memory_ids=("recovered-memory",),
                ),
            )
        )
        completed = await ImportService(after, client).resume_job(
            job_id=job.job_id, resolution="recheck"
        )
        item_after = after.list_import_items(job.job_id)[0]
        assert completed.state is ImportJobState.COMPLETED
        assert item_after.item_id == item_before.item_id
        assert item_after.state is ImportItemState.IMPORTED
        assert item_after.pieces_memory_id == "recovered-memory"
    finally:
        after.close()


@pytest.mark.asyncio
async def test_paused_job_rejects_resume_on_a_different_pieces_endpoint(
    ledger: Ledger,
) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(
                title="Endpoint bound",
                markdown_body="Recover only against the confirmed endpoint.",
            ),
        ),
    )
    original = FakePiecesClient(
        endpoint="http://first.example.test/mcp",
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        ),
        write_results=("post_failure",),
    )
    job = create_job(ImportService(ledger, original), original, [candidate])
    await ImportService(ledger, original).run_job(job.job_id)

    changed = FakePiecesClient(endpoint="http://second.example.test/mcp")
    changed_service = ImportService(ledger, changed)
    with pytest.raises(InvalidStateError, match="endpoint differs"):
        changed_service.validate_resume(job_id=job.job_id, resolution="recheck")
    with pytest.raises(InvalidStateError, match="endpoint differs"):
        await changed_service.resume_job(job_id=job.job_id, resolution="recheck")

    attempts = ledger.list_import_attempts(
        ledger.list_import_items(job.job_id)[0].item_id
    )
    assert {attempt.pieces_endpoint for attempt in attempts} == {
        "http://first.example.test/mcp"
    }
    assert changed.events == []


@pytest.mark.asyncio
async def test_only_one_import_execution_runs_per_process(ledger: Ledger) -> None:
    candidate = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Lock", markdown_body="Only one importer runs."),
        ),
    )
    client = FakePiecesClient()
    release = asyncio.Event()
    entered = asyncio.Event()
    client.block_write = release.wait()
    client.sdk_entry_hook = entered.set
    service = ImportService(ledger, client)
    job = create_job(service, client, [candidate])
    running = asyncio.create_task(service.run_job(job.job_id))
    await entered.wait()

    with pytest.raises(ImportInProgressError):
        await service.run_job(job.job_id)

    release.set()
    await running
