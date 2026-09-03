from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent2pieces.ledger import Ledger
from agent2pieces.models import CandidatePayload


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture
def payload() -> CandidatePayload:
    return CandidatePayload(
        title="Keep the database boundary explicit",
        markdown_body="# Decision\n\nUse one short SQLite transaction per state change.",
        external_links=["https://example.com/design"],
        project_scope="agent2pieces",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    instance = Ledger(tmp_path / "agent2pieces.sqlite3")
    instance.initialize()
    try:
        yield instance
    finally:
        instance.close()

