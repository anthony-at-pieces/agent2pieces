from __future__ import annotations

import math

import pytest

from agent2pieces.dedupe import (
    DedupeCandidate,
    DuplicateClassification,
    candidate_pairs,
    classify_scores,
    compare_candidates,
    cosine_similarity,
    five_token_shingle_jaccard,
    prepare_comparison_text,
    title_token_jaccard,
    tokenize,
)
from agent2pieces.models import SourceAgent


def candidate(
    candidate_id: str,
    *,
    title: str = "Database boundary",
    body: str = "Keep each database transaction short and explicit.",
    payload_hash: str | None = None,
) -> DedupeCandidate:
    return DedupeCandidate(
        candidate_id=candidate_id,
        source_agent=SourceAgent.CODEX,
        source_key=f"memory/{candidate_id}.md",
        title=title,
        markdown_body=body,
        payload_hash=payload_hash or candidate_id.removeprefix("candidate-").ljust(64, "0"),
    )


def test_preparation_removes_generated_provenance_marker_html_and_link_destinations() -> None:
    body = """Keep the [database boundary](https://example.test/private-token) explicit.

<!-- generated details must not affect similarity -->
<details>
<summary>Generated</summary>
hidden transport metadata
</details>

---
Imported by Agent2Pieces
Source agent: Claude
Project: agent2pieces
Agent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz
"""

    assert prepare_comparison_text(body) == (
        "keep",
        "the",
        "database",
        "boundary",
        "explicit",
    )


def test_preparation_removes_a_standalone_visible_marker_without_footer() -> None:
    assert prepare_comparison_text(
        "Decision text\nAgent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz\n"
    ) == ("decision", "text")


def test_tokenize_applies_nfkc_and_casefold_before_letter_number_runs() -> None:
    assert tokenize("Stra\u00dfe \uff21\uff22\uff23 CAF\u00c9 \u2460 snake_case") == (
        "strasse",
        "abc",
        "caf\u00e9",
        "1",
        "snake",
        "case",
    )


def test_raw_term_frequency_cosine_is_rounded_to_six_decimal_places() -> None:
    score = cosine_similarity(("a", "a", "b"), ("a", "b"))

    assert score == 0.948683
    assert score == pytest.approx(3 / math.sqrt(10), abs=0.0000005)


def test_cosine_for_two_empty_vectors_is_zero() -> None:
    assert cosine_similarity((), ()) == 0.0
    assert cosine_similarity(("word",), ()) == 0.0


def test_five_token_shingles_use_sets_and_round_to_six_places() -> None:
    left = tokenize("a b c d e f g")
    right = tokenize("a b c d e x g")

    assert five_token_shingle_jaccard(left, right) == 0.2


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ((), ()),
        (("a", "b", "c", "d"), ("a", "b", "c", "d")),
        (("a", "b", "c", "d", "e"), ("a", "b", "c", "d")),
    ],
)
def test_five_token_shingles_are_zero_if_either_body_is_short(
    left: tuple[str, ...], right: tuple[str, ...]
) -> None:
    assert five_token_shingle_jaccard(left, right) == 0.0


def test_title_jaccard_uses_normalized_token_sets() -> None:
    assert title_token_jaccard("SQLite Transaction Boundary", "sqlite boundary rules") == 0.5
    assert title_token_jaccard("", "nonempty") == 0.0


@pytest.mark.parametrize(
    ("hash_equal", "cosine", "shingle", "title", "classification", "rule_id"),
    [
        (True, 0.0, 0.0, 0.0, DuplicateClassification.EXACT, "payload_hash"),
        (False, 0.920000, 0.0, 0.0, DuplicateClassification.LIKELY, "body_cosine"),
        (False, 0.919999, 0.800000, 0.0, DuplicateClassification.LIKELY, "body_shingle"),
        (False, 0.780000, 0.0, 0.500000, DuplicateClassification.POSSIBLE, "possible"),
        (False, 0.919999, 0.0, 0.500000, DuplicateClassification.POSSIBLE, "possible"),
        (False, 0.779999, 1.0, 0.0, DuplicateClassification.LIKELY, "body_shingle"),
        (False, 0.779999, 0.799999, 1.0, DuplicateClassification.DISTINCT, "distinct"),
        (False, 0.920000, 0.0, 1.0, DuplicateClassification.LIKELY, "body_cosine"),
        (False, 0.919999, 0.0, 0.499999, DuplicateClassification.DISTINCT, "distinct"),
    ],
)
def test_classification_thresholds_and_strongest_rule_precedence(
    hash_equal: bool,
    cosine: float,
    shingle: float,
    title: float,
    classification: DuplicateClassification,
    rule_id: str,
) -> None:
    result = classify_scores(
        payload_hash_equal=hash_equal,
        import_marker_equal=False,
        cosine=cosine,
        body_shingle_jaccard=shingle,
        title_jaccard=title,
    )

    assert result.classification is classification
    assert result.rule_id == rule_id
    assert result.cosine == round(cosine, 6)
    assert result.body_shingle_jaccard == round(shingle, 6)
    assert result.title_jaccard == round(title, 6)


def test_exact_import_marker_has_same_precedence_as_equal_payload_hash() -> None:
    result = classify_scores(
        payload_hash_equal=False,
        import_marker_equal=True,
        cosine=0.0,
        body_shingle_jaccard=0.0,
        title_jaccard=0.0,
    )

    assert result.classification is DuplicateClassification.EXACT
    assert result.rule_id == "import_marker"


def test_compare_candidates_scores_prepared_content_and_ignores_provenance() -> None:
    left = candidate(
        "candidate-a",
        body="Use one short transaction per state change.",
        payload_hash="a" * 64,
    )
    right = candidate(
        "candidate-b",
        body=(
            "Use one short transaction per state change.\n\n"
            "---\nImported by Agent2Pieces\nSource agent: Hermes\n"
            "Agent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz"
        ),
        payload_hash="b" * 64,
    )

    result = compare_candidates(left, right)

    assert result.cosine == 1.0
    assert result.body_shingle_jaccard == 1.0
    assert result.classification is DuplicateClassification.LIKELY


def test_candidate_pairs_are_stable_and_do_not_compare_a_candidate_to_itself() -> None:
    candidates = [
        candidate("candidate-c"),
        candidate("candidate-a"),
        candidate("candidate-b"),
    ]

    pairs = candidate_pairs(candidates)

    assert [(left.candidate_id, right.candidate_id) for left, right in pairs] == [
        ("candidate-a", "candidate-b"),
        ("candidate-a", "candidate-c"),
        ("candidate-b", "candidate-c"),
    ]
