"""Deterministic secret and PII screening without value retention."""

from __future__ import annotations

import re
from collections.abc import Iterable

from agent2pieces.models import FindingSeverity, SafetyFinding

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("secret_private_key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    (
        "secret_aws_access_key",
        re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"),
    ),
    (
        "secret_github_token",
        re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"),
    ),
    (
        "secret_slack_token",
        re.compile(r"(?<![A-Za-z0-9-])xox[bpars]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"),
    ),
    ("secret_openai_key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    (
        "secret_assignment",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:api_key|apikey|access_token|client_secret|password)\s*[:=]\s*\S{8,}",
            flags=re.IGNORECASE | re.ASCII,
        ),
    ),
    (
        "secret_jwt",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
        ),
    ),
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![A-Za-z0-9-])"
)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[ .()-]*){10,15}(?!\d)")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(value: str) -> bool:
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def scan_safety(*, title: str, markdown_body: str) -> tuple[SafetyFinding, ...]:
    findings: list[SafetyFinding] = []
    _scan_line(title, 1, findings)
    for line_number, line in enumerate(markdown_body.splitlines(), start=1):
        _scan_line(line, line_number, findings)
    return tuple(findings)


def _scan_line(line: str, line_number: int, findings: list[SafetyFinding]) -> None:
    for reason, pattern in _SECRET_PATTERNS:
        if pattern.search(line):
            findings.append(
                SafetyFinding(
                    reason_code=reason, severity=FindingSeverity.BLOCK, line_number=line_number
                )
            )
    warning_matches = (
        ("pii_email", bool(_EMAIL.search(line))),
        ("pii_phone", bool(_PHONE.search(line))),
        ("pii_us_ssn", bool(_SSN.search(line))),
        ("pii_payment_card", any(_luhn(match.group(0)) for match in _CARD.finditer(line))),
    )
    for reason, matched in warning_matches:
        if matched:
            findings.append(
                SafetyFinding(
                    reason_code=reason, severity=FindingSeverity.WARN, line_number=line_number
                )
            )


def approval_default_checked(findings: Iterable[SafetyFinding]) -> bool:
    del findings
    return False


def override_default_checked() -> bool:
    return False


def edit_invalidates_overrides(*, previous_payload_hash: str, current_payload_hash: str) -> bool:
    return previous_payload_hash != current_payload_hash
