"""Deterministic local duplicate scoring and representative selection."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from agent2pieces.models import SourceAgent

_MARKER_LINE = re.compile(
    r"(?im)^[ \t]*Agent2Pieces Import ID:[ \t]*[a-z2-7]{26}[ \t]*$"
)
_GENERATED_FOOTER = re.compile(
    r"(?ims)^---[ \t]*\nImported by Agent2Pieces[ \t]*(?:\n|\Z).*\Z"
)
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_HTML_CONTAINER = re.compile(
    r"(?is)<(?P<tag>details|script|style|template)\b[^>]*>.*?</(?P=tag)\s*>"
)
_HTML_TAG = re.compile(r"(?s)<[^>]*>")
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^\n)]*\)")


class DuplicateClassification(StrEnum):
    """Ordered duplicate verdicts emitted by deterministic rules."""

    EXACT = "exact"
    LIKELY = "likely"
    POSSIBLE = "possible"
    DISTINCT = "distinct"


@dataclass(frozen=True, slots=True)
class DedupeCandidate:
    """The immutable candidate fields needed by local duplicate checks."""

    candidate_id: str
    source_agent: SourceAgent
    source_key: str
    title: str
    markdown_body: str
    payload_hash: str
    import_id: str | None = None
    project_scope: str = ""
    external_links: tuple[str, ...] = ()
    source_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """Rounded score evidence and the strongest matching rule."""

    classification: DuplicateClassification
    rule_id: str
    cosine: float
    body_shingle_jaccard: float
    title_jaccard: float


@dataclass(frozen=True, slots=True)
class LocalEvidence:
    """A stored local evidence edge used to suggest review groups."""

    evidence_id: str
    left_candidate_id: str
    right_candidate_id: str
    classification: DuplicateClassification
    rule_id: str = ""
    cosine: float = 0.0
    body_shingle_jaccard: float = 0.0
    title_jaccard: float = 0.0


@dataclass(frozen=True, slots=True)
class SuggestedGroup:
    """An evidence-connected group proposed for explicit user review."""

    member_candidate_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    default_representative_candidate_id: str


def tokenize(text: str) -> tuple[str, ...]:
    """Normalize text and return runs of Unicode letters and numbers."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if unicodedata.category(character)[0] in {"L", "N"}:
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def prepare_comparison_text(markdown_body: str) -> tuple[str, ...]:
    """Remove generated/non-semantic Markdown details and tokenize the body."""

    text = markdown_body.replace("\r\n", "\n").replace("\r", "\n")
    text = _GENERATED_FOOTER.sub("", text)
    text = _MARKER_LINE.sub("", text)
    text = _HTML_COMMENT.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = _HTML_CONTAINER.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    return tokenize(text)


def cosine_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Return raw term-frequency cosine rounded to six decimal places."""

    if not left or not right:
        return 0.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    dot_product = sum(count * right_counts.get(token, 0) for token, count in left_counts.items())
    left_norm = math.sqrt(sum(count * count for count in left_counts.values()))
    right_norm = math.sqrt(sum(count * count for count in right_counts.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return round(dot_product / (left_norm * right_norm), 6)


def five_token_shingle_jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    """Return Jaccard similarity for sets of contiguous five-token shingles."""

    if len(left) < 5 or len(right) < 5:
        return 0.0
    left_shingles = {tuple(left[index : index + 5]) for index in range(len(left) - 4)}
    right_shingles = {tuple(right[index : index + 5]) for index in range(len(right) - 4)}
    union = left_shingles | right_shingles
    if not union:
        return 0.0
    return round(len(left_shingles & right_shingles) / len(union), 6)


def title_token_jaccard(left_title: str, right_title: str) -> float:
    """Return normalized title-token set Jaccard similarity."""

    left_tokens = set(tokenize(left_title))
    right_tokens = set(tokenize(right_title))
    if not left_tokens or not right_tokens:
        return 0.0
    return round(len(left_tokens & right_tokens) / len(left_tokens | right_tokens), 6)


def classify_scores(
    *,
    payload_hash_equal: bool,
    import_marker_equal: bool,
    cosine: float,
    body_shingle_jaccard: float,
    title_jaccard: float,
) -> SimilarityResult:
    """Classify scores using the documented strongest-rule precedence."""

    rounded_cosine = round(cosine, 6)
    rounded_shingle = round(body_shingle_jaccard, 6)
    rounded_title = round(title_jaccard, 6)
    if payload_hash_equal:
        classification = DuplicateClassification.EXACT
        rule_id = "payload_hash"
    elif import_marker_equal:
        classification = DuplicateClassification.EXACT
        rule_id = "import_marker"
    elif rounded_cosine >= 0.92:
        classification = DuplicateClassification.LIKELY
        rule_id = "body_cosine"
    elif rounded_shingle >= 0.80:
        classification = DuplicateClassification.LIKELY
        rule_id = "body_shingle"
    elif 0.78 <= rounded_cosine < 0.92 and rounded_title >= 0.50:
        classification = DuplicateClassification.POSSIBLE
        rule_id = "possible"
    else:
        classification = DuplicateClassification.DISTINCT
        rule_id = "distinct"
    return SimilarityResult(
        classification=classification,
        rule_id=rule_id,
        cosine=rounded_cosine,
        body_shingle_jaccard=rounded_shingle,
        title_jaccard=rounded_title,
    )


def compare_candidates(left: DedupeCandidate, right: DedupeCandidate) -> SimilarityResult:
    """Score a candidate pair after removing generated provenance."""

    left_body = prepare_comparison_text(left.markdown_body)
    right_body = prepare_comparison_text(right.markdown_body)
    return classify_scores(
        payload_hash_equal=left.payload_hash == right.payload_hash,
        import_marker_equal=(
            left.import_id is not None
            and right.import_id is not None
            and left.import_id == right.import_id
        ),
        cosine=cosine_similarity(left_body, right_body),
        body_shingle_jaccard=five_token_shingle_jaccard(left_body, right_body),
        title_jaccard=title_token_jaccard(left.title, right.title),
    )


def candidate_pairs(
    candidates: Iterable[DedupeCandidate],
) -> tuple[tuple[DedupeCandidate, DedupeCandidate], ...]:
    """Build a stable all-pairs candidate pool without self comparisons."""

    ordered = sorted(candidates, key=lambda candidate: candidate.candidate_id)
    return tuple(
        (ordered[left_index], ordered[right_index])
        for left_index in range(len(ordered))
        for right_index in range(left_index + 1, len(ordered))
    )


def _parsed_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_order(value: datetime | None) -> int:
    if value is None:
        return -1
    return (
        value.toordinal() * 86_400_000_000
        + value.hour * 3_600_000_000
        + value.minute * 60_000_000
        + value.second * 1_000_000
        + value.microsecond
    )


def _representative_key(candidate: DedupeCandidate) -> tuple[int, int, int, int, str, str]:
    parsed_timestamp = _parsed_timestamp(candidate.source_updated_at)
    metadata_completeness = sum(
        (
            bool(candidate.title.strip()),
            bool(candidate.project_scope.strip()),
            bool(candidate.external_links),
            parsed_timestamp is not None,
        )
    )
    source_preference = {
        SourceAgent.CODEX: 0,
        SourceAgent.CLAUDE: 1,
        SourceAgent.HERMES: 2,
    }
    normalized_body_length = len(
        unicodedata.normalize("NFKC", candidate.markdown_body).encode("utf-8")
    )
    return (
        -metadata_completeness,
        -normalized_body_length,
        -_timestamp_order(parsed_timestamp),
        source_preference[candidate.source_agent],
        unicodedata.normalize("NFKC", candidate.source_key),
        candidate.candidate_id,
    )


def rank_representatives(candidates: Iterable[DedupeCandidate]) -> tuple[DedupeCandidate, ...]:
    """Rank candidates by the exact deterministic representative score."""

    return tuple(sorted(candidates, key=_representative_key))


def default_representative(candidates: Iterable[DedupeCandidate]) -> DedupeCandidate:
    """Return the highest-information candidate from a non-empty group."""

    ranked = rank_representatives(candidates)
    if not ranked:
        raise ValueError("duplicate group must contain at least one candidate")
    return ranked[0]


def select_representative(
    candidates: Iterable[DedupeCandidate], *, override_candidate_id: str | None = None
) -> DedupeCandidate:
    """Select a validated override, or the deterministic default."""

    members = tuple(candidates)
    if override_candidate_id is None:
        return default_representative(members)
    for member in members:
        if member.candidate_id == override_candidate_id:
            return member
    raise ValueError("representative override must be a group member")


def suggested_groups(
    candidates: Iterable[DedupeCandidate], evidence: Iterable[LocalEvidence]
) -> tuple[SuggestedGroup, ...]:
    """Build stable connected components from non-distinct local evidence."""

    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    graph: dict[str, set[str]] = defaultdict(set)
    accepted_edges: list[LocalEvidence] = []
    for edge in evidence:
        if edge.classification is DuplicateClassification.DISTINCT:
            continue
        if (
            edge.left_candidate_id not in candidate_by_id
            or edge.right_candidate_id not in candidate_by_id
            or edge.left_candidate_id == edge.right_candidate_id
        ):
            continue
        graph[edge.left_candidate_id].add(edge.right_candidate_id)
        graph[edge.right_candidate_id].add(edge.left_candidate_id)
        accepted_edges.append(edge)

    components: list[tuple[str, ...]] = []
    visited: set[str] = set()
    for start in sorted(graph):
        if start in visited:
            continue
        queue = deque([start])
        visited.add(start)
        members: list[str] = []
        while queue:
            current = queue.popleft()
            members.append(current)
            for neighbor in sorted(graph[current]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(members) >= 2:
            components.append(tuple(sorted(members)))

    groups: list[SuggestedGroup] = []
    for member_ids in sorted(components):
        member_set = set(member_ids)
        evidence_ids = tuple(
            sorted(
                edge.evidence_id
                for edge in accepted_edges
                if edge.left_candidate_id in member_set and edge.right_candidate_id in member_set
            )
        )
        representative = default_representative(
            candidate_by_id[candidate_id] for candidate_id in member_ids
        )
        groups.append(
            SuggestedGroup(
                member_candidate_ids=member_ids,
                evidence_ids=evidence_ids,
                default_representative_candidate_id=representative.candidate_id,
            )
        )
    return tuple(groups)
