from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent2pieces.ledger import Ledger
from agent2pieces.models import (
    AdapterScanResult,
    CandidateStatus,
    ScanCounts,
    ScanState,
    SourceAgent,
)
from agent2pieces.services import ScanInProgressError, ScanRoot, ScanService


def configured_root(ledger: Ledger, path: Path, agent: SourceAgent) -> ScanRoot:
    root_id = ledger.add_source_root(
        agent=agent,
        lexical_path=str(path),
        resolved_path=str(path.resolve()),
        enabled=True,
        is_default=False,
    )
    return ScanRoot(root_id=root_id, agent=agent, path=path)


def candidate_rows(ledger: Ledger) -> list[object]:
    return ledger.connection.execute(
        "SELECT * FROM candidates ORDER BY created_at, candidate_id"
    ).fetchall()


@pytest.mark.asyncio
async def test_scan_persists_counts_dispositions_candidates_and_observations(
    ledger: Ledger, tmp_path: Path
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    (root / "decision.md").write_text(
        "---\nproject: agent2pieces\n---\n# Keep scans durable\n\nPersist every result.\n",
        encoding="utf-8",
    )
    (root / "raw.jsonl").write_text('{"raw":true}\n', encoding="utf-8")

    result = await ScanService(ledger).run_scan(
        (configured_root(ledger, root, SourceAgent.CODEX),)
    )

    assert result.state is ScanState.COMPLETED
    assert (
        result.discovered_count,
        result.accepted_count,
        result.excluded_count,
        result.quarantine_count,
        result.error_count,
    ) == (2, 1, 1, 0, 0)
    candidate = candidate_rows(ledger)[0]
    assert candidate["status"] == CandidateStatus.PENDING
    assert json.loads(candidate["original_payload_json"])["title"] == "Keep scans durable"
    assert ledger.connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1
    assert ledger.connection.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1
    disposition = ledger.connection.execute("SELECT * FROM source_dispositions").fetchone()
    assert disposition["reason"] == "excluded_raw"


@pytest.mark.asyncio
async def test_scan_rejects_a_configured_root_retargeted_after_persistence(
    ledger: Ledger,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    outside = tmp_path / "outside"
    original.mkdir()
    outside.mkdir()
    alias = tmp_path / "configured-root"
    try:
        alias.symlink_to(original, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    scan_root = configured_root(ledger, alias, SourceAgent.CODEX)
    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)
    calls: list[Path] = []

    def scanner(path: Path) -> AdapterScanResult:
        calls.append(path)
        return AdapterScanResult(counts=ScanCounts())

    result = await ScanService(
        ledger,
        scanners={SourceAgent.CODEX: scanner},
    ).run_scan((scan_root,))

    assert result.state is ScanState.FAILED
    assert result.error_detail == "InvalidStateError"
    assert calls == []


@pytest.mark.parametrize("changed_field", ["title", "project", "link"])
@pytest.mark.asyncio
async def test_source_content_changes_advance_unimported_candidate_with_snapshot(
    ledger: Ledger, tmp_path: Path, changed_field: str
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "decision.md"

    def source_text(*, title: str, project: str, link: str) -> str:
        return (
            f"---\nproject: {project}\nupdated_at: 2026-08-01T00:00:00Z\n---\n"
            f"# {title}\n\nUse [{link}]({link}) for evidence.\n"
        )

    first_values = {"title": "Original title", "project": "alpha", "link": "https://one.test"}
    source.write_text(source_text(**first_values), encoding="utf-8")
    scan_root = configured_root(ledger, root, SourceAgent.CODEX)
    service = ScanService(ledger)
    await service.run_scan((scan_root,))
    before = candidate_rows(ledger)[0]
    before_payload = json.loads(before["current_payload_json"])

    changed_values = dict(first_values)
    changed_values[changed_field] = {
        "title": "Changed title",
        "project": "beta",
        "link": "https://two.test",
    }[changed_field]
    source.write_text(source_text(**changed_values), encoding="utf-8")
    await service.run_scan((scan_root,))

    rows = candidate_rows(ledger)
    assert len(rows) == 1
    after = rows[0]
    assert after["candidate_id"] == before["candidate_id"]
    assert after["revision_id"] != before["revision_id"]
    assert after["version"] == before["version"] + 1
    assert after["status"] == CandidateStatus.PENDING
    assert json.loads(after["original_payload_json"]) == before_payload
    assert json.loads(after["current_payload_json"]) != before_payload
    predecessor = ledger.connection.execute(
        "SELECT predecessor_revision_id FROM source_revisions WHERE revision_id = ?",
        (after["revision_id"],),
    ).fetchone()[0]
    assert predecessor == before["revision_id"]
    snapshot = ledger.connection.execute(
        "SELECT payload_json FROM candidate_payload_snapshots WHERE candidate_id = ?",
        (after["candidate_id"],),
    ).fetchone()
    assert json.loads(snapshot["payload_json"]) == before_payload


@pytest.mark.asyncio
async def test_timestamp_and_mtime_changes_reuse_revision_as_new_observation(
    ledger: Ledger, tmp_path: Path
) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    source = root / "topic.md"
    source.write_text(
        "---\ntitle: Stable topic\nupdated_at: 2026-08-01T00:00:00Z\n---\nSame body.\n",
        encoding="utf-8",
    )
    scan_root = configured_root(ledger, root, SourceAgent.CLAUDE)
    service = ScanService(ledger)
    await service.run_scan((scan_root,))
    before = candidate_rows(ledger)[0]

    source.write_text(
        "---\ntitle: Stable topic\nupdated_at: 2026-08-02T00:00:00Z\n---\nSame body.\n",
        encoding="utf-8",
    )
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    await service.run_scan((scan_root,))

    after = candidate_rows(ledger)[0]
    assert after["candidate_id"] == before["candidate_id"]
    assert after["revision_id"] == before["revision_id"]
    assert after["version"] == before["version"]
    observations = ledger.list_source_observations(str(after["revision_id"]))
    assert len(observations) == 2
    assert observations[0].source_updated_at != observations[1].source_updated_at


@pytest.mark.asyncio
async def test_imported_candidate_remains_immutable_and_gets_pending_successor(
    ledger: Ledger, tmp_path: Path
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "decision.md"
    source.write_text("# First version\n\nKeep this.\n", encoding="utf-8")
    scan_root = configured_root(ledger, root, SourceAgent.CODEX)
    service = ScanService(ledger)
    await service.run_scan((scan_root,))
    first = ledger.get_candidate(str(candidate_rows(ledger)[0]["candidate_id"]))
    approved = ledger.update_candidate(
        candidate_id=first.candidate_id,
        expected_version=first.version,
        payload=first.payload,
        payload_hash=first.payload_hash,
        import_id=first.import_id,
        status=CandidateStatus.APPROVED,
    )
    imported = ledger.mark_candidate_imported(
        candidate_id=approved.candidate_id, expected_version=approved.version
    )

    source.write_text("# Second version\n\nKeep the successor.\n", encoding="utf-8")
    await service.run_scan((scan_root,))

    rows = candidate_rows(ledger)
    assert len(rows) == 2
    assert ledger.get_candidate(imported.candidate_id).status is CandidateStatus.IMPORTED
    successor = next(row for row in rows if row["candidate_id"] != imported.candidate_id)
    assert successor["status"] == CandidateStatus.PENDING
    predecessor = ledger.connection.execute(
        "SELECT predecessor_revision_id FROM source_revisions WHERE revision_id = ?",
        (successor["revision_id"],),
    ).fetchone()[0]
    assert predecessor == imported.revision_id


@pytest.mark.asyncio
async def test_only_one_scan_runs_per_service_process(ledger: Ledger, tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_scanner(path: Path) -> AdapterScanResult:
        del path
        entered.set()
        await release.wait()
        return AdapterScanResult(counts=ScanCounts())

    root = tmp_path / "codex"
    root.mkdir()
    scan_root = configured_root(ledger, root, SourceAgent.CODEX)
    service = ScanService(ledger, scanners={SourceAgent.CODEX: blocking_scanner})
    first = asyncio.create_task(service.run_scan((scan_root,)))
    await entered.wait()

    with pytest.raises(ScanInProgressError):
        await service.run_scan((scan_root,))

    release.set()
    await first
