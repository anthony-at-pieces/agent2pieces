from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from integration.helpers import FakePiecesClient, add_candidate, approve_candidate
from playwright.sync_api import Page, sync_playwright

from agent2pieces.api import create_app
from agent2pieces.ledger import Ledger
from agent2pieces.models import CandidatePayload, FindingSeverity, SourceAgent
from agent2pieces.routes import ApiDependencies
from agent2pieces.services import ImportService, ReviewService, ScanService


@dataclass(frozen=True)
class BrowserHarness:
    base_url: str
    ledger: Ledger
    pieces: FakePiecesClient
    candidate_ids: dict[str, str]
    source_root: Path


def _listen_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener, int(listener.getsockname()[1])


@pytest.fixture
def browser_page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            page.close()
            context.close()
            browser.close()


@pytest.fixture
def browser_harness(tmp_path: Path) -> Iterator[BrowserHarness]:
    ledger = Ledger(tmp_path / "browser.sqlite3")
    ledger.initialize()
    source_root = tmp_path / "codex"
    source_root.mkdir()
    (source_root / "scan-memory.md").write_text(
        "---\nproject: Browser Scan\n---\n# Browser scan memory\n\n"
        "This candidate came from a real read-only scan.\n",
        encoding="utf-8",
    )
    ledger.add_source_root(
        agent=SourceAgent.CODEX,
        lexical_path=str(source_root),
        resolved_path=str(source_root.resolve()),
        enabled=True,
        is_default=False,
    )

    shared = "Persist the dispatch boundary before entering the remote SDK call."
    sparse = add_candidate(
        ledger,
        payload=CandidatePayload(title="Dispatch boundary", markdown_body=shared),
        source_key="sparse.md",
        source_path=str(tmp_path / "sparse.md"),
    )
    complete = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Dispatch boundary",
            markdown_body=shared,
            external_links=["https://example.test/design"],
            project_scope="Alpha",
        ),
        source_key="complete.md",
        source_path=str(tmp_path / "complete.md"),
    )
    changed = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Changed source",
            markdown_body="The previous approved body.",
            project_scope="Alpha",
        ),
        source_key="changed.md",
        source_path=str(tmp_path / "changed.md"),
    )
    changed = approve_candidate(ledger, changed)
    changed = ReviewService(ledger).edit_candidate(
        candidate_id=changed.candidate_id,
        expected_version=changed.version,
        payload=changed.payload.model_copy(
            update={"markdown_body": "The current body has a safe visible change."}
        ),
    )
    hostile = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Hostile markup",
            markdown_body=(
                "<script>window.agent2piecesCompromised = true</script>\n"
                "<img src=x onerror=window.agent2piecesCompromised=true>"
            ),
            project_scope="Hostile",
        ),
        source_key="hostile.md",
        source_path=str(tmp_path / "hostile.md"),
    )
    warning = add_candidate(
        ledger,
        payload=CandidatePayload(
            title="Release contact",
            markdown_body="Contact person@example.test before release.",
            project_scope="Beta",
        ),
        source_key="warning.md",
        source_path=str(tmp_path / "warning.md"),
    )
    ledger.add_safety_finding(
        candidate_id=warning.candidate_id,
        candidate_version=warning.version,
        reason_code="pii_email",
        severity=FindingSeverity.WARN,
        line_number=1,
    )
    approved = approve_candidate(
        ledger,
        add_candidate(
            ledger,
            payload=CandidatePayload(
                title="Approved import",
                markdown_body="Write this approved candidate once.",
                project_scope="Alpha",
            ),
            source_key="approved.md",
            source_path=str(tmp_path / "approved.md"),
        ),
    )

    pieces = FakePiecesClient()
    listener, port = _listen_socket()
    base_url = f"http://127.0.0.1:{port}"
    csrf_token = "browser-process-csrf-token"
    dependencies = ApiDependencies(
        ledger=ledger,
        scan_service=ScanService(ledger),
        review_service=ReviewService(ledger, pieces),
        import_service=ImportService(ledger, pieces),
        pieces_client=pieces,
        csrf_token=csrf_token,
        effective_mcp_base_url="http://pieces.test",
        mcp_base_url_source="cli",
        command_roots=(),
    )
    app = create_app(dependencies=dependencies, listener_origins=(base_url,))
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="agent2pieces-browser-test",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        ledger.close()
        pytest.fail("loopback browser test server did not start")

    try:
        yield BrowserHarness(
            base_url=base_url,
            ledger=ledger,
            pieces=pieces,
            candidate_ids={
                "sparse": sparse.candidate_id,
                "complete": complete.candidate_id,
                "changed": changed.candidate_id,
                "hostile": hostile.candidate_id,
                "warning": warning.candidate_id,
                "approved": approved.candidate_id,
            },
            source_root=source_root,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        ledger.close()
