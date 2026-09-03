from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from agent2pieces.models import AdapterScanResult, SourceAgent, SourceDisposition
from agent2pieces.normalization import canonicalize_body, source_hash
from agent2pieces.safety import approval_default_checked, scan_safety
from agent2pieces.scanners import scan_claude_root, scan_codex_root, scan_hermes_root
from agent2pieces.scanners.base import MAX_SOURCE_BYTES

Scanner = Callable[[Path], AdapterScanResult]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "sources"


def _secret_test_body() -> str:
    return "\n".join(
        (
            "# Credential rejection notes",
            "",
            "".join(("-----BEGIN ", "PRIVATE KEY-----")),
            "".join(("AK", "IA", "ABCDEFGHIJKLMNOP")),
            "".join(("gh", "p_", "abcdefghijklmnopqrst")),
            "".join(("xo", "xb-", "abcdefghijklmnopqrst")),
            "".join(("s", "k-", "abcdefghijklmnopqrst")),
            "".join(("client_", "secret=", "very-secret-value")),
            ".".join(
                (
                    "eyJhbGciOiJIUzI1NiJ9",
                    "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                    "signature123",
                )
            ),
        )
    )


def _metadata(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns, stat.st_mode


@pytest.mark.parametrize(
    ("agent", "fixture_name", "scanner", "accepted", "excluded", "quarantined"),
    [
        (SourceAgent.CODEX, "codex", scan_codex_root, 1, 1, 0),
        (SourceAgent.CLAUDE, "claude", scan_claude_root, 1, 2, 0),
        (SourceAgent.HERMES, "hermes", scan_hermes_root, 2, 0, 0),
    ],
)
def test_curated_fixture_inventory_is_read_only_and_has_expected_dispositions(
    tmp_path: Path,
    agent: SourceAgent,
    fixture_name: str,
    scanner: Scanner,
    accepted: int,
    excluded: int,
    quarantined: int,
) -> None:
    root = tmp_path / fixture_name
    shutil.copytree(FIXTURES / fixture_name, root)
    paths = tuple(path for path in root.rglob("*") if path.is_file())
    before = {path.relative_to(root): _metadata(path) for path in paths}

    result = scanner(root)

    assert result.counts.accepted == accepted
    assert result.counts.excluded == excluded
    assert result.counts.quarantined == quarantined
    assert result.counts.discovered == accepted + excluded + quarantined
    assert {item.candidate.source_agent for item in result.candidates} == {agent}
    assert {item.reason for item in result.dispositions} == {
        SourceAgent.CODEX: {"excluded_raw"},
        SourceAgent.CLAUDE: {"excluded_index", "excluded_user"},
        SourceAgent.HERMES: set(),
    }[agent]
    assert {path.relative_to(root): _metadata(path) for path in paths} == before


@pytest.mark.parametrize(
    ("scanner", "filename", "content", "reason"),
    [
        (scan_codex_root, "malformed.md", "---\nproject:\n---\nBody", "malformed"),
        (scan_claude_root, "malformed.md", "---\ntype:\n---\nBody", "malformed"),
        (scan_codex_root, "empty.md", "  \n", "empty"),
        (scan_claude_root, "empty.md", "  \n", "empty"),
        (scan_hermes_root, "MEMORY.md", "  \n", "empty"),
        (scan_codex_root, "large.md", "x" * (MAX_SOURCE_BYTES + 1), "oversize"),
        (scan_claude_root, "large.md", "x" * (MAX_SOURCE_BYTES + 1), "oversize"),
        (scan_hermes_root, "MEMORY.md", "x" * (MAX_SOURCE_BYTES + 1), "oversize"),
    ],
    ids=(
        "codex-malformed",
        "claude-malformed",
        "codex-empty",
        "claude-empty",
        "hermes-empty",
        "codex-oversize",
        "claude-oversize",
        "hermes-oversize",
    ),
)
def test_dynamic_invalid_sources_are_quarantined_without_silent_truncation(
    tmp_path: Path,
    scanner: Scanner,
    filename: str,
    content: str,
    reason: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / filename
    source.write_text(content, encoding="utf-8")
    before = _metadata(source)

    result = scanner(root)

    assert result.counts.accepted == 0
    assert result.counts.quarantined == 1
    assert result.dispositions[0].disposition is SourceDisposition.QUARANTINED
    assert result.dispositions[0].reason == reason
    assert _metadata(source) == before


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
@pytest.mark.parametrize(
    ("scanner", "link_name"),
    [
        (scan_codex_root, "escape.md"),
        (scan_claude_root, "escape.md"),
        (scan_hermes_root, "MEMORY.md"),
    ],
)
def test_symlink_escape_is_quarantined_without_opening_target(
    tmp_path: Path,
    scanner: Scanner,
    link_name: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside.md"
    target.write_text("outside secret body", encoding="utf-8")
    link = root / link_name
    link.symlink_to(target)
    before = _metadata(target)

    result = scanner(root)

    assert result.counts == result.counts.model_copy(
        update={"discovered": 1, "accepted": 0, "excluded": 0, "quarantined": 1}
    )
    assert result.dispositions[0].reason == "symlink_escape"
    assert _metadata(target) == before


def test_secret_and_pii_fixtures_cover_every_reason_without_value_retention() -> None:
    secret_body = _secret_test_body()
    pii_body = (FIXTURES / "hostile" / "pii.md").read_text(encoding="utf-8")

    secret_findings = scan_safety(title="Safe", markdown_body=secret_body)
    pii_findings = scan_safety(title="Safe", markdown_body=pii_body)

    assert {item.reason_code for item in secret_findings} == {
        "secret_private_key",
        "secret_aws_access_key",
        "secret_github_token",
        "secret_slack_token",
        "secret_openai_key",
        "secret_assignment",
        "secret_jwt",
    }
    assert {item.reason_code for item in pii_findings} == {
        "pii_email",
        "pii_phone",
        "pii_us_ssn",
        "pii_payment_card",
    }
    assert approval_default_checked(secret_findings) is False
    assert approval_default_checked(pii_findings) is False
    serialized = repr(secret_findings) + repr(pii_findings)
    for sensitive in (
        "very-secret-value",
        "person@example.test",
        "123-45-6789",
        "4111 1111 1111 1111",
    ):
        assert sensitive not in serialized


def test_hash_identity_accepts_nfkc_crlf_and_harmless_markdown_whitespace() -> None:
    canonical = "# Caf\u00e9\n\nOne line\n\n\nSecond line"
    equivalent = "\r\n# Cafe\u0301  \r\n\r\nOne line\t\r\n\r\n\r\n\r\nSecond line\t\r\n"

    assert source_hash(canonical) == source_hash(equivalent)
    assert canonicalize_body(canonical) == canonicalize_body(equivalent)
