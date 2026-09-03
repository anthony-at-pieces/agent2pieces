from __future__ import annotations

from pathlib import Path

import pytest
from integration.helpers import FakePiecesClient, add_candidate, approve_candidate

from agent2pieces.ledger import InvalidStateError, Ledger
from agent2pieces.mcp_client import MarkerSearchResult
from agent2pieces.models import (
    CandidatePayload,
    CandidateRecord,
    CandidateStatus,
    ImportItemState,
    ImportJobRecord,
    SourceAgent,
)
from agent2pieces.services import (
    ApplyConfirmation,
    ImportService,
    ReviewService,
    ScanRoot,
    ScanService,
)


def _confirmation(
    client: FakePiecesClient, count: int = 1, *, context_hash: str = "0" * 64
) -> ApplyConfirmation:
    return ApplyConfirmation(
        confirmed=True,
        pieces_endpoint=client.capabilities.endpoint,
        selected_write_count=count,
        context_hash=context_hash,
    )


def _create_job(
    service: ImportService,
    client: FakePiecesClient,
    candidates: list[CandidateRecord],
) -> ImportJobRecord:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    versions = {candidate.candidate_id: candidate.version for candidate in candidates}
    hashes = {candidate.candidate_id: candidate.payload_hash for candidate in candidates}
    preview = service.preview_job(
        candidate_ids=candidate_ids,
        candidate_versions=versions,
        displayed_payload_hashes=hashes,
    )
    return service.create_job(
        candidate_ids=candidate_ids,
        candidate_versions=versions,
        displayed_payload_hashes=hashes,
        confirmation=_confirmation(
            client, len(candidates), context_hash=preview.context_hash
        ),
        acknowledge_remote_duplicate_risk=False,
    )


def _candidate_ids(ledger: Ledger) -> list[str]:
    return [
        str(row["candidate_id"])
        for row in ledger.connection.execute(
            "SELECT candidate_id FROM candidates ORDER BY candidate_id"
        ).fetchall()
    ]


@pytest.mark.asyncio
async def test_rescan_tracks_changed_source_then_observes_unchanged_revision(
    ledger: Ledger,
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "memory.md"
    source.write_text(
        "---\nproject: agent2pieces\nupdated_at: 2026-08-30T00:00:00Z\n---\n"
        "# Changed memory\n\nThe first approved body.\n",
        encoding="utf-8",
    )
    root_id = ledger.add_source_root(
        agent=SourceAgent.CODEX,
        lexical_path=str(root),
        resolved_path=str(root.resolve()),
        enabled=True,
        is_default=False,
    )
    scan_root = ScanRoot(root_id=root_id, agent=SourceAgent.CODEX, path=root)
    scanner = ScanService(ledger)

    first_scan = await scanner.run_scan((scan_root,))
    candidate = ledger.get_candidate(_candidate_ids(ledger)[0])
    approved = ReviewService(ledger).approve_candidate(
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
    )
    source.write_text(
        "---\nproject: agent2pieces\nupdated_at: 2026-08-31T00:00:00Z\n---\n"
        "# Changed memory\n\nThe approved body changed after review.\n",
        encoding="utf-8",
    )

    second_scan = await scanner.run_scan((scan_root,))
    changed = ledger.get_candidate(candidate.candidate_id)
    snapshot_count = ledger.connection.execute(
        "SELECT COUNT(*) FROM candidate_payload_snapshots WHERE candidate_id = ?",
        (candidate.candidate_id,),
    ).fetchone()[0]
    observations_before = ledger.connection.execute(
        "SELECT COUNT(*) FROM source_observations"
    ).fetchone()[0]
    third_scan = await scanner.run_scan((scan_root,))
    observed_again = ledger.get_candidate(candidate.candidate_id)
    observations_after = ledger.connection.execute(
        "SELECT COUNT(*) FROM source_observations"
    ).fetchone()[0]

    assert first_scan.accepted_count == second_scan.accepted_count == third_scan.accepted_count == 1
    assert approved.status is CandidateStatus.APPROVED
    assert changed.status is CandidateStatus.PENDING
    assert changed.version == approved.version + 1
    assert snapshot_count >= 2
    assert observed_again.version == changed.version
    assert _candidate_ids(ledger) == [candidate.candidate_id]
    assert observations_after == observations_before + 1


@pytest.mark.asyncio
async def test_successful_apply_then_second_scan_has_no_duplicate_write(
    ledger: Ledger,
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    (root / "memory.md").write_text(
        "# Idempotent import\n\nImport this curated memory once.\n",
        encoding="utf-8",
    )
    root_id = ledger.add_source_root(
        agent=SourceAgent.CODEX,
        lexical_path=str(root),
        resolved_path=str(root.resolve()),
        enabled=True,
        is_default=False,
    )
    scan_root = ScanRoot(root_id=root_id, agent=SourceAgent.CODEX, path=root)
    scans = ScanService(ledger)
    await scans.run_scan((scan_root,))
    candidate = ledger.get_candidate(_candidate_ids(ledger)[0])
    candidate = ReviewService(ledger).approve_candidate(
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
    )
    pieces = FakePiecesClient()
    imports = ImportService(ledger, pieces)
    job = _create_job(imports, pieces, [candidate])

    await imports.run_job(job.job_id)
    await scans.run_scan((scan_root,))
    after_rescan = ledger.get_candidate(candidate.candidate_id)

    assert after_rescan.status is CandidateStatus.IMPORTED
    assert len(pieces.arguments) == 1
    with pytest.raises(InvalidStateError, match="approved"):
        imports.create_job(
            candidate_ids=(after_rescan.candidate_id,),
            candidate_versions={after_rescan.candidate_id: after_rescan.version},
            displayed_payload_hashes={after_rescan.candidate_id: after_rescan.payload_hash},
            confirmation=_confirmation(pieces),
            acknowledge_remote_duplicate_risk=False,
        )
    assert len(pieces.arguments) == 1


@pytest.mark.asyncio
async def test_exact_remote_marker_prevents_write_after_local_database_is_removed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deleted-ledger.sqlite3"
    first_ledger = Ledger(database)
    first_ledger.initialize()
    original = approve_candidate(
        first_ledger,
        add_candidate(
            first_ledger,
            payload=CandidatePayload(
                title="Recovered marker",
                markdown_body="The remote marker survives loss of the local ledger.",
            ),
        ),
    )
    expected_import_id = original.import_id
    first_ledger.close()
    database.unlink()

    replacement = Ledger(database)
    replacement.initialize()
    try:
        candidate = approve_candidate(
            replacement,
            add_candidate(
                replacement,
                payload=CandidatePayload(
                    title="Recovered marker",
                    markdown_body="The remote marker survives loss of the local ledger.",
                ),
            ),
        )
        pieces = FakePiecesClient(
            marker_results=(
                MarkerSearchResult(
                    outcome="one_parent",
                    coverage="complete",
                    parent_memory_ids=("existing-memory",),
                ),
            )
        )
        service = ImportService(replacement, pieces)
        job = _create_job(service, pieces, [candidate])

        completed = await service.run_job(job.job_id)
        item = replacement.list_import_items(job.job_id)[0]

        assert candidate.import_id == expected_import_id
        assert completed.state.value == "completed"
        assert item.state is ImportItemState.REMOTE_DUPLICATE
        assert pieces.arguments == []
    finally:
        replacement.close()


@pytest.mark.asyncio
async def test_partial_batch_stops_after_ambiguous_write_and_preserves_later_item(
    ledger: Ledger,
) -> None:
    first = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="First", markdown_body="The first batch item."),
            source_key="first.md",
            source_path="/codex/first.md",
        ),
    )
    second = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(title="Second", markdown_body="The second batch item."),
            source_key="second.md",
            source_path="/codex/second.md",
        ),
    )
    pieces = FakePiecesClient(write_results=("post_failure", "success"))
    service = ImportService(ledger, pieces)
    job = _create_job(service, pieces, [first, second])

    paused = await service.run_job(job.job_id)
    items = ledger.list_import_items(job.job_id)

    assert paused.state.value == "paused"
    assert [item.state for item in items] == [
        ImportItemState.AMBIGUOUS,
        ImportItemState.QUEUED,
    ]
    assert pieces.arguments == []
