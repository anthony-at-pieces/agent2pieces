from __future__ import annotations

from collections.abc import Iterable

import pytest

from agent2pieces.dedupe import (
    DedupeCandidate,
    DuplicateClassification,
    LocalEvidence,
    default_representative,
    rank_representatives,
    select_representative,
    suggested_groups,
)
from agent2pieces.models import SourceAgent


def candidate(
    candidate_id: str,
    *,
    source_agent: SourceAgent = SourceAgent.CODEX,
    source_key: str | None = None,
    title: str = "A",
    body: str = "body",
    project_scope: str = "",
    external_links: Iterable[str] = (),
    source_updated_at: str | None = None,
) -> DedupeCandidate:
    return DedupeCandidate(
        candidate_id=candidate_id,
        source_agent=source_agent,
        source_key=source_key or f"memory/{candidate_id}.md",
        title=title,
        markdown_body=body,
        payload_hash=(candidate_id.replace("-", "") + "0" * 64)[:64],
        project_scope=project_scope,
        external_links=tuple(external_links),
        source_updated_at=source_updated_at,
    )


def evidence(
    evidence_id: str,
    left: str,
    right: str,
    classification: DuplicateClassification,
) -> LocalEvidence:
    return LocalEvidence(
        evidence_id=evidence_id,
        left_candidate_id=left,
        right_candidate_id=right,
        classification=classification,
    )


def test_suggested_groups_are_evidence_connected_components() -> None:
    candidates = [candidate("d"), candidate("a"), candidate("c"), candidate("b")]
    edges = [
        evidence("edge-3", "c", "d", DuplicateClassification.DISTINCT),
        evidence("edge-2", "b", "c", DuplicateClassification.POSSIBLE),
        evidence("edge-1", "a", "b", DuplicateClassification.LIKELY),
    ]

    groups = suggested_groups(reversed(candidates), reversed(edges))

    assert len(groups) == 1
    assert groups[0].member_candidate_ids == ("a", "b", "c")
    assert groups[0].evidence_ids == ("edge-1", "edge-2")
    assert groups[0].default_representative_candidate_id == "a"


def test_disconnected_evidence_builds_stably_ordered_groups() -> None:
    candidates = [candidate("z"), candidate("y"), candidate("b"), candidate("a")]
    edges = [
        evidence("edge-zy", "z", "y", DuplicateClassification.EXACT),
        evidence("edge-ab", "a", "b", DuplicateClassification.POSSIBLE),
    ]

    groups = suggested_groups(candidates, edges)

    assert [group.member_candidate_ids for group in groups] == [("a", "b"), ("y", "z")]


def test_remote_or_missing_candidate_evidence_cannot_create_local_groups() -> None:
    candidates = [candidate("a"), candidate("b")]
    edges = [
        evidence("remote", "a", "pieces:annotation-1", DuplicateClassification.EXACT),
        evidence("distinct", "a", "b", DuplicateClassification.DISTINCT),
    ]

    assert suggested_groups(candidates, edges) == ()


def test_metadata_completeness_is_the_first_representative_key() -> None:
    sparse = candidate(
        "a",
        title="Title",
        body="x" * 10_000,
        source_updated_at="2026-08-31T00:00:00Z",
    )
    complete = candidate(
        "z",
        title="Title",
        body="x",
        project_scope="agent2pieces",
        external_links=("https://example.test",),
        source_updated_at="2020-01-01T00:00:00Z",
    )

    assert default_representative([sparse, complete]).candidate_id == "z"


def test_body_utf8_byte_length_is_the_second_representative_key() -> None:
    short = candidate("a", body="ascii")
    longer_utf8 = candidate("z", body="\u00e9\u00e9\u00e9")

    assert default_representative([short, longer_utf8]).candidate_id == "z"


def test_timestamp_is_newest_first_and_missing_or_invalid_is_oldest() -> None:
    newest = candidate("z", source_updated_at="2026-09-01T04:00:00+00:00")
    older = candidate("a", source_updated_at="2025-09-01T00:00:00Z")
    missing = candidate("b")
    invalid = candidate("c", source_updated_at="not-a-timestamp")

    ranked = rank_representatives([missing, older, invalid, newest])

    assert [item.candidate_id for item in ranked] == ["z", "a", "b", "c"]


def test_source_preference_is_codex_then_claude_then_hermes() -> None:
    candidates = [
        candidate("h", source_agent=SourceAgent.HERMES, source_key="same"),
        candidate("c", source_agent=SourceAgent.CLAUDE, source_key="same"),
        candidate("x", source_agent=SourceAgent.CODEX, source_key="same"),
    ]

    assert [item.source_agent for item in rank_representatives(candidates)] == [
        SourceAgent.CODEX,
        SourceAgent.CLAUDE,
        SourceAgent.HERMES,
    ]


def test_source_key_uses_nfkc_code_point_order_without_case_folding() -> None:
    normalized_a = candidate("z", source_key="\uff21-key")
    uppercase_b = candidate("a", source_key="B-key")
    lowercase_a = candidate("b", source_key="a-key")

    assert [item.candidate_id for item in rank_representatives(
        [lowercase_a, uppercase_b, normalized_a]
    )] == ["z", "a", "b"]


def test_candidate_id_is_the_final_ascending_tie_breaker() -> None:
    first = candidate("00000000-0000-4000-8000-000000000001", source_key="same")
    second = candidate("00000000-0000-4000-8000-000000000002", source_key="same")

    assert default_representative([second, first]).candidate_id == first.candidate_id


def test_ranking_is_stable_across_discovery_order() -> None:
    candidates = [
        candidate("c", source_agent=SourceAgent.HERMES),
        candidate("a", source_agent=SourceAgent.CODEX),
        candidate("b", source_agent=SourceAgent.CLAUDE),
    ]

    forward = rank_representatives(candidates)
    backward = rank_representatives(reversed(candidates))

    assert [item.candidate_id for item in forward] == [item.candidate_id for item in backward]


def test_explicit_override_selects_a_member_without_changing_default_score() -> None:
    scored_default = candidate("a", source_agent=SourceAgent.CODEX, source_key="same")
    override = candidate("b", source_agent=SourceAgent.HERMES, source_key="same")
    candidates = [override, scored_default]

    selected = select_representative(candidates, override_candidate_id="b")

    assert selected.candidate_id == "b"
    assert default_representative(candidates).candidate_id == "a"


def test_explicit_override_must_be_a_group_member() -> None:
    with pytest.raises(ValueError, match="group member"):
        select_representative([candidate("a")], override_candidate_id="not-a-member")
