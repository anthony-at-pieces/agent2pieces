from __future__ import annotations

import json

import pytest

from agent2pieces.ledger import InvalidStateError, Ledger, OptimisticConflictError
from agent2pieces.mcp_client import McpCapabilities, RemoteDuplicateResult
from agent2pieces.models import (
    CandidatePayload,
    CandidateStatus,
    FindingSeverity,
    FindingState,
    GroupStatus,
    SourceAgent,
)
from agent2pieces.services import FindingOverride, ReviewService

from .helpers import add_candidate


def _api_key_assignment(value: str) -> str:
    return "".join(("api_", "key = ", value))


class RecordingDuplicateClient:
    def __init__(self) -> None:
        self.capabilities = McpCapabilities(
            transport="streamable-http",
            endpoint="http://pieces.test/model_context_protocol/2025-03-26/mcp",
            server_version="fake",
            import_ready=True,
            search_available=True,
            blocking_error=None,
            checked_at="2026-09-02T00:00:00Z",
        )
        self.searched_candidates: list[object] = []

    async def search_duplicates(self, candidate: object) -> RemoteDuplicateResult:
        self.searched_candidates.append(candidate)
        return RemoteDuplicateResult(coverage="complete", matches=())


def finding_ids(ledger: Ledger, candidate_id: str, *, state: str = "open") -> list[str]:
    rows = ledger.connection.execute(
        "SELECT finding_id FROM safety_findings WHERE candidate_id = ? AND state = ? "
        "ORDER BY finding_id",
        (candidate_id, state),
    ).fetchall()
    return [str(row["finding_id"]) for row in rows]


def test_edit_recomputes_content_identity_safety_and_payload_snapshots(ledger: Ledger) -> None:
    original = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Original",
            markdown_body="Contact owner@example.test before changing this.",
            project_scope="alpha",
        ),
    )
    old_finding_id = ledger.add_safety_finding(
        candidate_id=original.candidate_id,
        candidate_version=original.version,
        reason_code="pii_email",
        severity=FindingSeverity.WARN,
        line_number=1,
    )
    review = ReviewService(ledger)
    approved = review.approve_candidate(
        candidate_id=original.candidate_id,
        expected_version=original.version,
        override=FindingOverride(
            finding_ids=(old_finding_id,),
            acknowledged=True,
            reason="The source owner approved this contact address for import.",
        ),
    )

    edited = review.edit_candidate(
        candidate_id=approved.candidate_id,
        expected_version=approved.version,
        payload=CandidatePayload(
            title="Edited",
            markdown_body=_api_key_assignment("super-secret-value-12345"),
            external_links=["https://example.test/edited"],
            project_scope="beta",
        ),
    )

    assert edited.status is CandidateStatus.PENDING
    assert edited.payload_hash != approved.payload_hash
    assert edited.import_id != approved.import_id
    assert json.loads(edited.original_payload_json) == original.payload.model_dump(mode="json")
    snapshots = ledger.connection.execute(
        "SELECT candidate_version, payload_json FROM candidate_payload_snapshots "
        "WHERE candidate_id = ? ORDER BY candidate_version",
        (edited.candidate_id,),
    ).fetchall()
    assert [row["candidate_version"] for row in snapshots] == [1, 2]
    assert ledger.get_safety_finding(old_finding_id).state is FindingState.CLEARED
    new_findings = ledger.connection.execute(
        "SELECT reason_code, severity, state, override_reason FROM safety_findings "
        "WHERE candidate_id = ? AND candidate_version = ?",
        (edited.candidate_id, edited.version),
    ).fetchall()
    assert [(row["reason_code"], row["severity"], row["state"]) for row in new_findings] == [
        ("secret_assignment", "block", "open")
    ]
    assert new_findings[0]["override_reason"] is None


def test_project_only_edit_changes_version_but_not_payload_identity(ledger: Ledger) -> None:
    candidate = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Project move",
            markdown_body="The approved content is unchanged.",
            project_scope="before",
        ),
    )

    edited = ReviewService(ledger).edit_candidate(
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        payload=candidate.payload.model_copy(update={"project_scope": "after"}),
    )

    assert edited.version == candidate.version + 1
    assert edited.payload_hash == candidate.payload_hash
    assert edited.import_id == candidate.import_id
    assert edited.status is CandidateStatus.PENDING


@pytest.mark.parametrize(
    ("reason_code", "severity"),
    [
        ("secret_private_key", FindingSeverity.BLOCK),
        ("pii_email", FindingSeverity.WARN),
    ],
)
def test_open_secret_or_pii_requires_explicit_audited_override(
    ledger: Ledger, reason_code: str, severity: FindingSeverity
) -> None:
    candidate = add_candidate(
        ledger,
        payload=CandidatePayload(title="Review finding", markdown_body="Sensitive source text."),
    )
    finding_id = ledger.add_safety_finding(
        candidate_id=candidate.candidate_id,
        candidate_version=candidate.version,
        reason_code=reason_code,
        severity=severity,
        line_number=1,
    )
    review = ReviewService(ledger)

    with pytest.raises(InvalidStateError, match="finding"):
        review.approve_candidate(
            candidate_id=candidate.candidate_id,
            expected_version=candidate.version,
        )
    with pytest.raises(InvalidStateError, match="acknowledg"):
        review.approve_candidate(
            candidate_id=candidate.candidate_id,
            expected_version=candidate.version,
            override=FindingOverride(
                finding_ids=(finding_id,),
                acknowledged=False,
                reason="The reviewer has evaluated this finding and accepts it.",
            ),
        )

    approved = review.approve_candidate(
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        override=FindingOverride(
            finding_ids=(finding_id,),
            acknowledged=True,
            reason="The reviewer verified the source and explicitly accepts this finding.",
        ),
    )

    finding = ledger.get_safety_finding(finding_id)
    assert approved.status is CandidateStatus.APPROVED
    assert finding.state is FindingState.OVERRIDDEN
    assert finding.override_at is not None
    assert finding.override_reason == (
        "The reviewer verified the source and explicitly accepts this finding."
    )


@pytest.mark.asyncio
async def test_duplicate_check_persists_evidence_and_builds_deterministic_group(
    ledger: Ledger,
) -> None:
    shared_body = "Persist one short transaction for each durable state transition."
    first = add_candidate(
        ledger,
        payload=CandidatePayload(title="SQLite boundary", markdown_body=shared_body),
        agent=SourceAgent.HERMES,
        source_key="MEMORY.md#section=1",
        source_path="/hermes/MEMORY.md",
        source_updated_at="2026-08-01T00:00:00Z",
    )
    richer = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="SQLite boundary",
            markdown_body=shared_body,
            external_links=["https://sqlite.org/lang_transaction.html"],
            project_scope="agent2pieces",
        ),
        agent=SourceAgent.CODEX,
        source_key="rollout.md",
        source_path="/codex/rollout.md",
        source_updated_at="2026-08-02T00:00:00Z",
    )
    review = ReviewService(ledger)

    report = await review.check_duplicates(
        candidate_ids=(first.candidate_id, richer.candidate_id),
        candidate_versions={first.candidate_id: 1, richer.candidate_id: 1},
    )

    assert report.coverage == "local-only"
    assert len(report.suggested_groups) == 1
    suggestion = report.suggested_groups[0]
    assert set(suggestion.member_candidate_ids) == {first.candidate_id, richer.candidate_id}
    assert suggestion.default_representative_candidate_id == richer.candidate_id
    assert suggestion.evidence_ids
    evidence = ledger.connection.execute(
        "SELECT target_kind, classification, rule_id FROM duplicate_evidence "
        "WHERE evidence_id = ?",
        (suggestion.evidence_ids[0],),
    ).fetchone()
    assert (evidence["target_kind"], evidence["classification"], evidence["rule_id"]) == (
        "candidate",
        "likely",
        "body_cosine",
    )

    group = review.create_group(
        check_id=report.check_id,
        title="Same SQLite decision",
        member_candidate_ids=suggestion.member_candidate_ids,
        evidence_ids=suggestion.evidence_ids,
    )
    assert group.representative_candidate_id == richer.candidate_id
    assert group.status is GroupStatus.DRAFT


@pytest.mark.asyncio
async def test_remote_duplicate_check_skips_candidates_with_any_open_safety_finding(
    ledger: Ledger,
) -> None:
    unsafe = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Private contact",
            markdown_body="Contact owner@example.test before making this change.",
        ),
        source_key="unsafe.md",
        source_path="/codex/unsafe.md",
    )
    clean = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Safe decision",
            markdown_body="Keep this public implementation note.",
        ),
        source_key="clean.md",
        source_path="/codex/clean.md",
    )
    ledger.add_safety_finding(
        candidate_id=unsafe.candidate_id,
        candidate_version=unsafe.version,
        reason_code="pii_email",
        severity=FindingSeverity.WARN,
        line_number=1,
    )
    client = RecordingDuplicateClient()

    report = await ReviewService(ledger, client).check_duplicates(
        candidate_ids=(unsafe.candidate_id, clean.candidate_id),
        candidate_versions={unsafe.candidate_id: unsafe.version, clean.candidate_id: clean.version},
    )

    assert report.coverage == "mixed"
    assert report.coverage_by_candidate == {
        unsafe.candidate_id: "local-only",
        clean.candidate_id: "local+pieces",
    }
    assert report.warnings == (
        f"pieces_search_skipped_open_safety:{unsafe.candidate_id}",
    )
    assert [candidate.candidate_id for candidate in client.searched_candidates] == [
        clean.candidate_id
    ]
    assert "owner@example.test" not in repr(client.searched_candidates)


@pytest.mark.asyncio
async def test_group_creation_rejects_stale_or_unbacked_membership(ledger: Ledger) -> None:
    body = "Use a durable ledger before entering the remote SDK call."
    first = add_candidate(
        ledger,
        payload=CandidatePayload(title="Dispatch boundary", markdown_body=body),
        source_key="one.md",
        source_path="/codex/one.md",
    )
    second = add_candidate(
        ledger,
        payload=CandidatePayload(title="Dispatch boundary", markdown_body=body),
        source_key="two.md",
        source_path="/codex/two.md",
    )
    unrelated = add_candidate(
        ledger,
        payload=CandidatePayload(title="Different", markdown_body="Keep this memory separate."),
        source_key="three.md",
        source_path="/codex/three.md",
    )
    review = ReviewService(ledger)
    report = await review.check_duplicates(
        candidate_ids=(first.candidate_id, second.candidate_id),
        candidate_versions={first.candidate_id: 1, second.candidate_id: 1},
    )
    suggestion = report.suggested_groups[0]

    with pytest.raises(InvalidStateError, match="evidence|component"):
        review.create_group(
            check_id=report.check_id,
            title="Invalid group",
            member_candidate_ids=(*suggestion.member_candidate_ids, unrelated.candidate_id),
            evidence_ids=suggestion.evidence_ids,
        )

    ReviewService(ledger).edit_candidate(
        candidate_id=second.candidate_id,
        expected_version=second.version,
        payload=second.payload.model_copy(update={"title": "Changed after check"}),
    )
    with pytest.raises(OptimisticConflictError):
        review.create_group(
            check_id=report.check_id,
            title="Stale group",
            member_candidate_ids=suggestion.member_candidate_ids,
            evidence_ids=suggestion.evidence_ids,
        )


@pytest.mark.asyncio
async def test_group_representative_and_decision_transitions_are_optimistic(
    ledger: Ledger,
) -> None:
    body = "Require a marker search before each remote memory write."
    first = add_candidate(
        ledger,
        payload=CandidatePayload(title="Marker preflight", markdown_body=body),
        source_key="first.md",
        source_path="/codex/first.md",
    )
    second = add_candidate(
        ledger,
        payload=CandidatePayload(title="Marker preflight", markdown_body=body),
        source_key="second.md",
        source_path="/codex/second.md",
    )
    review = ReviewService(ledger)
    report = await review.check_duplicates(
        candidate_ids=(first.candidate_id, second.candidate_id),
        candidate_versions={first.candidate_id: 1, second.candidate_id: 1},
    )
    suggestion = report.suggested_groups[0]
    group = review.create_group(
        check_id=report.check_id,
        title="Marker preflight",
        member_candidate_ids=suggestion.member_candidate_ids,
        evidence_ids=suggestion.evidence_ids,
    )
    alternate = next(
        candidate_id
        for candidate_id in suggestion.member_candidate_ids
        if candidate_id != group.representative_candidate_id
    )

    changed = review.update_group(
        group_id=group.group_id,
        expected_version=group.version,
        action="set_representative",
        representative_candidate_id=alternate,
    )
    assert changed.representative_candidate_id == alternate
    assert changed.representative_overridden is True
    with pytest.raises(OptimisticConflictError):
        review.update_group(
            group_id=group.group_id,
            expected_version=group.version,
            action="approve",
        )

    approved = review.update_group(
        group_id=group.group_id,
        expected_version=changed.version,
        action="approve",
    )
    assert approved.status is GroupStatus.APPROVED
    members = ledger.connection.execute(
        "SELECT candidate_id, status FROM candidates WHERE group_id = ? ORDER BY candidate_id",
        (group.group_id,),
    ).fetchall()
    states = {str(row["candidate_id"]): str(row["status"]) for row in members}
    assert states[alternate] == CandidateStatus.APPROVED
    assert set(states.values()) == {CandidateStatus.APPROVED, CandidateStatus.EXCLUDED}


@pytest.mark.asyncio
async def test_excluding_group_excludes_every_member(ledger: Ledger) -> None:
    body = "Do not merge source memories silently."
    first = add_candidate(
        ledger,
        payload=CandidatePayload(title="No silent merge", markdown_body=body),
        source_key="first.md",
        source_path="/codex/first.md",
    )
    second = add_candidate(
        ledger,
        payload=CandidatePayload(title="No silent merge", markdown_body=body),
        source_key="second.md",
        source_path="/codex/second.md",
    )
    review = ReviewService(ledger)
    report = await review.check_duplicates(
        candidate_ids=(first.candidate_id, second.candidate_id),
        candidate_versions={first.candidate_id: 1, second.candidate_id: 1},
    )
    suggestion = report.suggested_groups[0]
    group = review.create_group(
        check_id=report.check_id,
        title="No silent merge",
        member_candidate_ids=suggestion.member_candidate_ids,
        evidence_ids=suggestion.evidence_ids,
    )

    excluded = review.update_group(
        group_id=group.group_id,
        expected_version=group.version,
        action="exclude",
    )

    assert excluded.status is GroupStatus.EXCLUDED
    states = ledger.connection.execute(
        "SELECT DISTINCT status FROM candidates WHERE group_id = ?", (group.group_id,)
    ).fetchall()
    assert [row["status"] for row in states] == [CandidateStatus.EXCLUDED]
