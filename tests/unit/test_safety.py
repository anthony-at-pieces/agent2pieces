from __future__ import annotations

import json

import pytest

from agent2pieces.models import FindingSeverity
from agent2pieces.safety import (
    approval_default_checked,
    edit_invalidates_overrides,
    override_default_checked,
    scan_safety,
)


def _secret_value(*parts: str) -> str:
    return "".join(parts)


@pytest.mark.parametrize(
    ("reason_code", "value"),
    [
        ("secret_private_key", _secret_value("-----BEGIN RSA ", "PRIVATE KEY-----")),
        ("secret_aws_access_key", _secret_value("AK", "IA", "ABCDEFGHIJKLMNOP")),
        ("secret_github_token", _secret_value("gh", "p_", "abcdefghijklmnopqrst")),
        ("secret_slack_token", _secret_value("xo", "xb-", "abcdefghijklmnopqrst")),
        ("secret_openai_key", _secret_value("s", "k-", "abcdefghijklmnopqrst")),
        ("secret_assignment", _secret_value("client_", "secret=", "very-secret-value")),
        (
            "secret_jwt",
            _secret_value(
                "eyJhbGciOiJIUzI1NiJ9",
                ".",
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                ".",
                "signature123",
            ),
        ),
    ],
)
def test_every_secret_signature_blocks_without_retaining_the_value(
    reason_code: str,
    value: str,
) -> None:
    findings = scan_safety(title="Safe title", markdown_body=f"before\n{value}\nafter")
    finding = next(item for item in findings if item.reason_code == reason_code)

    assert finding.severity is FindingSeverity.BLOCK
    assert finding.line_number == 2
    serialized = json.dumps(finding.model_dump(mode="json"), sort_keys=True)
    assert value not in serialized
    assert set(finding.model_dump()) == {"reason_code", "severity", "line_number"}


def test_secret_boundaries_reject_short_or_embedded_lookalikes() -> None:
    findings = scan_safety(
        title="Safe",
        markdown_body=(
            f"prefix{_secret_value('AK', 'IA', 'ABCDEFGHIJKLMNOP')}suffix\n"
            f"{_secret_value('api_', 'key', '=short')}\n"
            "sk-too-short\n"
            "not.a.jwt"
        ),
    )

    assert findings == ()


@pytest.mark.parametrize(
    ("reason_code", "value"),
    [
        ("pii_email", "person@example.test"),
        ("pii_phone", "+1 (212) 555-0198"),
        ("pii_us_ssn", "123-45-6789"),
        ("pii_payment_card", "4111 1111 1111 1111"),
    ],
)
def test_every_pii_signature_warns(reason_code: str, value: str) -> None:
    findings = scan_safety(title="Safe", markdown_body=f"line one\n{value}")
    finding = next(item for item in findings if item.reason_code == reason_code)

    assert finding.severity is FindingSeverity.WARN
    assert finding.line_number == 2
    assert value not in json.dumps(finding.model_dump(mode="json"))


def test_invalid_luhn_number_is_not_a_payment_card_warning() -> None:
    findings = scan_safety(title="Safe", markdown_body="4111 1111 1111 1112")

    assert "pii_payment_card" not in {finding.reason_code for finding in findings}


def test_title_is_scanned_as_line_one_before_body_lines() -> None:
    findings = scan_safety(
        title="Contact person@example.test",
        markdown_body="Body only",
    )

    assert [(item.reason_code, item.line_number) for item in findings] == [("pii_email", 1)]


def test_approval_and_override_controls_default_unchecked_and_edits_invalidate() -> None:
    assert approval_default_checked(()) is False
    assert approval_default_checked(
        scan_safety(title="Safe", markdown_body="person@example.test")
    ) is False
    assert override_default_checked() is False
    assert edit_invalidates_overrides(
        previous_payload_hash="a" * 64,
        current_payload_hash="b" * 64,
    ) is True
    assert edit_invalidates_overrides(
        previous_payload_hash="a" * 64,
        current_payload_hash="a" * 64,
    ) is False
