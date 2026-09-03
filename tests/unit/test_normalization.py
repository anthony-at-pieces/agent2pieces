from __future__ import annotations

import re

from agent2pieces.models import SourceAgent
from agent2pieces.normalization import (
    candidate_input_hash,
    canonicalize_body,
    canonicalize_source_text,
    extract_external_links,
    import_id_from_payload_hash,
    normalize_title,
    payload_hash,
    render_dispatch_summary,
    source_hash,
    visible_import_marker,
)


def test_source_hash_ignores_only_defined_encoding_and_markdown_whitespace_noise() -> None:
    baseline = "# Café\n\nOne line\n\n\nSecond line"
    equivalent = "\r\n# Cafe\u0301  \r\n\r\nOne line\t\r\n\r\n\r\n\r\nSecond line\t\r\n\r\n"

    assert canonicalize_source_text(equivalent) == baseline
    assert source_hash(equivalent) == source_hash(baseline)


def test_source_hash_preserves_meaningful_whitespace_and_markdown() -> None:
    baseline = "# Heading\nOne two\n\nEnd"
    variants = (
        "## Heading\nOne two\n\nEnd",
        "# Heading\nOne  two\n\nEnd",
        "# Heading\nOne\ttwo\n\nEnd",
        "# Heading\nOne\u00a0two\n\nEnd",
        "# Heading\nOne two\nEnd",
        "# Heading\nOne two\n\n\nEnd",
        "# Heading\n[One](https://example.test) two\n\nEnd",
        "# Heading\nOne changed\n\nEnd",
    )

    assert all(source_hash(value) != source_hash(baseline) for value in variants)


def test_candidate_input_hash_tracks_candidate_driving_fields_only() -> None:
    baseline = candidate_input_hash(
        title="  Cafe\u0301\tresult  ",
        markdown_body="\nBody  \r\n\r\n\r\n\r\nEnd\n",
        external_links=["https://b.test", "https://a.test", "https://a.test"],
        project_scope="  project\tname  ",
    )
    observation_only = candidate_input_hash(
        title="Café result",
        markdown_body="Body\n\n\nEnd",
        external_links=["https://a.test", "https://b.test"],
        project_scope="project\tname",
    )

    assert baseline == observation_only
    assert baseline != candidate_input_hash(
        title="Changed",
        markdown_body="Body\n\n\nEnd",
        external_links=["https://a.test", "https://b.test"],
        project_scope="project\tname",
    )
    assert baseline != candidate_input_hash(
        title="Café result",
        markdown_body="Body\n\n\nEnd",
        external_links=["https://a.test", "https://b.test"],
        project_scope="different-project",
    )
    assert baseline != candidate_input_hash(
        title="Café result",
        markdown_body="Body\n\n\nEnd",
        external_links=["https://a.test", "https://c.test"],
        project_scope="project\tname",
    )


def test_payload_hash_and_import_id_are_content_only() -> None:
    first = payload_hash(
        title="Same memory",
        markdown_body="Detailed Markdown.",
        external_links=["https://example.test"],
    )
    second = payload_hash(
        title="Same memory",
        markdown_body="Detailed Markdown.",
        external_links=["https://example.test"],
    )
    import_id = import_id_from_payload_hash(first)

    assert first == second
    assert len(first) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert re.fullmatch(r"[a-z2-7]{26}", import_id)
    assert import_id == import_id_from_payload_hash(second)
    assert first != payload_hash(
        title="Same memory",
        markdown_body="Detailed Markdown changed.",
        external_links=["https://example.test"],
    )


def test_project_and_provenance_do_not_participate_in_payload_hash() -> None:
    digest = payload_hash(title="Title", markdown_body="Body", external_links=[])

    assert import_id_from_payload_hash(digest) == import_id_from_payload_hash(digest)
    assert candidate_input_hash(
        title="Title",
        markdown_body="Body",
        external_links=[],
        project_scope="one",
    ) != candidate_input_hash(
        title="Title",
        markdown_body="Body",
        external_links=[],
        project_scope="two",
    )


def test_visible_marker_and_dispatch_footer_are_exact_and_not_hidden() -> None:
    import_id = "abcdefghijklmnopqrstuvwxyz"

    assert visible_import_marker(import_id) == (
        "Agent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz"
    )
    assert render_dispatch_summary(
        markdown_body="Body",
        source_agent=SourceAgent.CLAUDE,
        import_id=import_id,
    ) == (
        "Body\n\n---\nImported by Agent2Pieces\nSource agent: Claude Code\n"
        "Agent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz"
    )
    assert render_dispatch_summary(
        markdown_body="Body",
        source_agent=SourceAgent.CODEX,
        import_id=import_id,
        mapped_project="visible-project",
    ).endswith(
        "Source agent: Codex\nProject: visible-project\n"
        "Agent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz"
    )


def test_title_body_and_external_link_normalization() -> None:
    text = (
        "See [one](https://example.test/a), https://example.test/b. "
        "Duplicate https://example.test/a and reject ftp://example.test/file, "
        "https://user:pass@example.test/private, https://example.test/\x01control, "
        "and https:///missing-host."
    )

    assert normalize_title("  A\t multi\nline  title ") == "A multi line title"
    assert canonicalize_body("\r\nBody  \r\n\r\n\r\n\r\nEnd\t\r\n") == "Body\n\n\nEnd"
    assert extract_external_links(text) == (
        "https://example.test/a",
        "https://example.test/b",
    )
