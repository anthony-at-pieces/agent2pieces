from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources import files

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agent2pieces.ledger import Ledger
from agent2pieces.routes import ApiDependencies
from agent2pieces.services import ImportService, ReviewService, ScanService

from .helpers import FakePiecesClient

CSRF_TOKEN = "static-ui-test-csrf-token"
BASE_URL = "http://127.0.0.1"
_FINGERPRINTED_SCRIPT = re.compile(r"^/assets/app\.[0-9a-f]{8,64}\.js$")
_FINGERPRINTED_STYLE = re.compile(r"^/assets/app\.[0-9a-f]{8,64}\.css$")
_REMOTE_REFERENCE = re.compile(
    r"(?:https?:)?//|@import\s+url|\b(?:cdn|unpkg|jsdelivr)\.",
    re.IGNORECASE,
)
_UNSAFE_JAVASCRIPT = re.compile(
    r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval)\b|"
    r"\bFunction\s*\(",
)


@dataclass(frozen=True)
class UiHarness:
    client: TestClient
    pieces: FakePiecesClient


@pytest.fixture
def ui(ledger: Ledger) -> Iterator[UiHarness]:
    api_module = importlib.import_module("agent2pieces.api")
    pieces = FakePiecesClient()
    dependencies = ApiDependencies(
        ledger=ledger,
        scan_service=ScanService(ledger),
        review_service=ReviewService(ledger, pieces),
        import_service=ImportService(ledger, pieces),
        pieces_client=pieces,
        csrf_token=CSRF_TOKEN,
        effective_mcp_base_url="http://pieces.test",
        mcp_base_url_source="cli",
        command_roots=(),
    )
    app = api_module.create_app(
        dependencies=dependencies,
        listener_origins=(
            "http://127.0.0.1",
            "http://localhost",
            "http://[::1]",
        ),
    )
    client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
    try:
        yield UiHarness(client=client, pieces=pieces)
    finally:
        client.close()


def _asset_references(document: str) -> tuple[str, str]:
    script = re.search(r'<script\s+defer\s+src="([^"]+)"\s*></script>', document)
    style = re.search(r'<link\s+rel="stylesheet"\s+href="([^"]+)"\s*/?>', document)
    assert script is not None
    assert style is not None
    return script.group(1), style.group(1)


def _read_packaged_assets() -> tuple[str, str, str]:
    static = files("agent2pieces").joinpath("static")
    index = static.joinpath("index.html")
    script = static.joinpath("app.js")
    style = static.joinpath("app.css")
    assert index.is_file()
    assert script.is_file()
    assert style.is_file()
    return (
        index.read_text(encoding="utf-8"),
        script.read_text(encoding="utf-8"),
        style.read_text(encoding="utf-8"),
    )


def test_create_app_exposes_only_approved_http_operations(ui: UiHarness) -> None:
    operations = {
        (method, route.path)
        for route in ui.client.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    internal_operations = {
        operation for operation in operations if operation[1].startswith("/api/")
    }

    assert internal_operations == {
        ("POST", "/api/scans"),
        ("GET", "/api/scans/{scan_id}"),
        ("GET", "/api/candidates"),
        ("PATCH", "/api/candidates/{candidate_id}"),
        ("POST", "/api/duplicates/check-pieces"),
        ("POST", "/api/import-jobs"),
        ("GET", "/api/import-jobs/{job_id}"),
        ("GET", "/api/settings"),
        ("PUT", "/api/settings"),
    }
    assert ("GET", "/") in operations
    assert ("GET", "/health") in operations
    assert not any(path in {"/docs", "/redoc", "/openapi.json"} for _, path in operations)


def test_health_reports_bounded_local_readiness_without_mcp_calls(ui: UiHarness) -> None:
    before = tuple(ui.pieces.events)

    response = ui.client.get("/health")

    assert response.status_code == 200
    assert len(response.content) <= 2_048
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "ledger": "ok",
        "assets": "ok",
        "mcp": {
            "status": "ready",
            "transport": "streamable-http",
            "create_pieces_memory": True,
            "annotations_full_text_search": True,
        },
    }
    assert tuple(ui.pieces.events) == before


def test_index_uses_fingerprinted_packaged_assets_and_safe_headers(ui: UiHarness) -> None:
    index = ui.client.get("/")

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    script_path, style_path = _asset_references(index.text)
    assert _FINGERPRINTED_SCRIPT.fullmatch(script_path)
    assert _FINGERPRINTED_STYLE.fullmatch(style_path)

    script = ui.client.get(script_path)
    style = ui.client.get(style_path)
    assert script.status_code == 200
    assert style.status_code == 200
    assert script.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert style.headers["content-type"].startswith("text/css")

    for response in (index, script, style):
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in response.headers


def test_packaged_assets_are_local_and_use_only_safe_rendering_sinks() -> None:
    index, script, style = _read_packaged_assets()
    combined = "\n".join((index, script, style))

    assert _REMOTE_REFERENCE.search(combined) is None
    assert _UNSAFE_JAVASCRIPT.search(script) is None
    assert "textContent" in script
    assert ".value" in script
    assert "document.createElement" in script
    assert "credentials: \"same-origin\"" in script
    assert "X-CSRF-Token" in script
    assert "Content-Type" in script
    assert "application/json" in script
    assert "new URL(path, window.location.origin)" in script


def test_review_shell_contains_all_manual_review_controls() -> None:
    index, _, style = _read_packaged_assets()
    required_ids = {
        "connection-status",
        "create-memory-capability",
        "search-capability",
        "pieces-effective-endpoint",
        "scan-button",
        "rescan-button",
        "scan-counts",
        "agent-filter",
        "project-filter",
        "duplicate-filter",
        "security-filter",
        "candidate-list",
        "candidate-title",
        "candidate-body",
        "preview-title",
        "preview-body",
        "changed-source",
        "prior-body",
        "current-body",
        "safety-findings",
        "finding-acknowledgement",
        "finding-reason",
        "override-findings",
        "duplicate-evidence",
        "duplicate-group",
        "group-representative",
        "apply-button",
        "apply-dialog",
        "apply-endpoint",
        "apply-count",
        "apply-confirmation",
        "remote-risk-acknowledgement",
        "apply-confirm",
        "apply-cancel",
        "import-progress",
        "import-items",
        "resume-recheck",
        "resume-retry",
        "resume-skip",
        "duplicate-write-risk-acknowledgement",
    }

    actual_ids = set(re.findall(r'\bid="([^"]+)"', index))
    assert required_ids <= actual_ids
    assert "diff-columns" in index
    assert re.search(r"\.diff-columns\s*\{[^}]*grid-template-columns", style, re.DOTALL)


def test_browser_client_covers_scan_review_duplicate_and_import_routes() -> None:
    index, script, _ = _read_packaged_assets()
    required_routes = {
        "/api/scans",
        "/api/candidates",
        "/api/duplicates/check-pieces",
        "/api/import-jobs",
        "/api/settings",
    }
    required_query_fields = {
        "source_agent",
        "project",
        "duplicate_verdict",
        "security_state",
        "include_changed_source",
    }
    required_actions = {
        "save",
        "approve",
        "exclude",
        "group_create",
        "group_join",
        "group_leave",
        "set_representative",
        "override_findings",
    }

    assert all(route in script for route in required_routes)
    assert all(field in script for field in required_query_fields)
    assert all(action in script for action in required_actions)
    assert "prior_snapshot" in script
    assert "changed_source" in script
    assert "hunks" in script
    assert "findings" in script
    assert "suggested_groups" in script
    assert "default_representative_candidate_id" in script
    assert "effective_endpoint" in script
    assert "annotations_full_text_search" in script
    assert "attempt_telemetry" in script
    assert "duplicate-warnings" in index
    assert "renderDuplicateWarnings" in script
    assert "state.duplicateCheck = null" in script
    assert "duration_ms" in script
    assert "retry_state" in script
    assert "recovery_state" in script


def test_apply_confirmation_and_resume_flow_are_explicit_and_stale_safe() -> None:
    index, script, _ = _read_packaged_assets()

    assert "function invalidateConfirmation" in script
    assert script.count("invalidateConfirmation(") >= 4
    assert "selected_write_count" in script
    assert "pieces_endpoint" in script
    assert "context_hash" in script
    assert "apply-context-hash" in index
    assert 'action: "preview"' in script
    assert "project:" in script
    assert "files:" in script
    assert "payload_hashes" in script
    assert "candidate_versions" in script
    assert "acknowledge_remote_duplicate_risk" in script
    assert "Local ledger loss or replacement can hide an earlier remote write." in script
    assert 'resolution: "recheck"' in script
    assert 'resolution: "retry"' in script
    assert 'resolution: "skip"' in script
    assert "acknowledge_duplicate_write_risk" in script
    assert "remote_duplicate" in script
    assert "ambiguous" in script
    assert "pause_reason" in script
    assert "setInterval" not in script
    assert re.search(r"addEventListener\(\s*[\"']click[\"']", script)


def test_page_load_does_not_embed_or_submit_an_import_request() -> None:
    index, script, _ = _read_packaged_assets()

    assert "/api/import-jobs" not in index
    assert CSRF_TOKEN not in index
    assert "submitImport" in script
    assert re.search(
        r"getElementById\([\"']apply-confirm[\"']\)"
        r"[\s\S]{0,300}addEventListener\([\"']click[\"'],\s*submitImport",
        script,
    )
