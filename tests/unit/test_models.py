from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent2pieces.models import (
    AttemptKind,
    AttemptState,
    CandidatePayload,
    CandidateStatus,
    FindingSeverity,
    FindingState,
    GroupStatus,
    ImportItemState,
    ImportJobState,
    ResumeAction,
    ScanState,
    SourceAgent,
    SourceDisposition,
    SourceRevisionIdentity,
)


def sha(character: str = "a") -> str:
    return character * 64


def test_state_enums_expose_only_the_persisted_values() -> None:
    assert {value.value for value in SourceAgent} == {"codex", "claude", "hermes"}
    assert {value.value for value in CandidateStatus} == {
        "pending",
        "approved",
        "excluded",
        "superseded",
        "imported",
    }
    assert {value.value for value in GroupStatus} == {
        "draft",
        "approved",
        "excluded",
        "imported",
    }
    assert {value.value for value in ScanState} == {
        "queued",
        "running",
        "completed",
        "failed",
    }
    assert {value.value for value in ImportJobState} == {
        "queued",
        "running",
        "paused",
        "completed",
        "failed",
    }
    assert {value.value for value in ImportItemState} == {
        "queued",
        "preflight",
        "remote_duplicate",
        "imported",
        "failed",
        "ambiguous",
        "skipped",
    }
    assert {value.value for value in AttemptKind} == {
        "marker_preflight",
        "write",
        "marker_recheck",
    }
    assert {value.value for value in AttemptState} == {
        "pending",
        "ambiguous",
        "succeeded",
        "failed",
        "skipped",
    }
    assert {value.value for value in SourceDisposition} == {"excluded", "quarantined"}
    assert {value.value for value in FindingSeverity} == {"block", "warn"}
    assert {value.value for value in FindingState} == {"open", "cleared", "overridden"}
    assert {value.value for value in ResumeAction} == {"recheck", "retry", "skip"}


def test_candidate_payload_validates_user_edit_boundaries() -> None:
    valid = CandidatePayload(
        title="A title",
        markdown_body="body",
        external_links=["https://example.com"],
        project_scope="scope",
    )
    assert valid.title == "A title"

    invalid_values = (
        {"title": "", "markdown_body": "body"},
        {"title": "x" * 241, "markdown_body": "body"},
        {"title": "title", "markdown_body": ""},
        {"title": "title", "markdown_body": "x" * 65_537},
        {"title": "title", "markdown_body": "body", "project_scope": "x" * 513},
        {
            "title": "title",
            "markdown_body": "body",
            "external_links": ["file:///tmp/private"],
        },
        {
            "title": "title",
            "markdown_body": "body",
            "external_links": ["https://user:password@example.com/private"],
        },
    )
    for value in invalid_values:
        with pytest.raises(ValidationError):
            CandidatePayload.model_validate(value)


def test_candidate_payload_enforces_utf8_byte_limit_not_character_count() -> None:
    with pytest.raises(ValidationError):
        CandidatePayload(title="title", markdown_body="\u00e9" * 32_769)


def test_revision_identity_rejects_non_uuid4_and_noncanonical_hashes() -> None:
    valid = SourceRevisionIdentity(
        agent=SourceAgent.CODEX,
        root_id=str(uuid.uuid4()),
        source_key="rollout.md",
        source_hash=sha("a"),
        candidate_input_hash=sha("b"),
    )
    assert valid.agent is SourceAgent.CODEX

    for invalid_root in (str(uuid.uuid1()), "not-a-uuid", str(uuid.uuid4()).upper()):
        with pytest.raises(ValidationError):
            SourceRevisionIdentity(
                agent="codex",
                root_id=invalid_root,
                source_key="rollout.md",
                source_hash=sha("a"),
                candidate_input_hash=sha("b"),
            )

    for invalid_hash in ("a" * 63, "A" * 64, "z" * 64):
        with pytest.raises(ValidationError):
            SourceRevisionIdentity(
                agent="codex",
                root_id=str(uuid.uuid4()),
                source_key="rollout.md",
                source_hash=invalid_hash,
                candidate_input_hash=sha("b"),
            )

