from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agent2pieces.ledger import Ledger
from agent2pieces.mcp_client import MarkerSearchResult
from agent2pieces.models import CandidatePayload, FindingSeverity, ImportItemState
from agent2pieces.routes import ApiDependencies, create_api_router
from agent2pieces.security import LoopbackSecurityConfig, install_loopback_security
from agent2pieces.services import ImportService, ReviewService, ScanService

from .helpers import FakePiecesClient, add_candidate, approve_candidate

CSRF_TOKEN = "test-process-csrf-token"
BASE_URL = "http://127.0.0.1"
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _password_assignment(value: str) -> str:
    return "".join(("pass", "word = ", value))


@dataclass(frozen=True)
class ApiHarness:
    client: TestClient
    ledger: Ledger
    pieces: FakePiecesClient

    def mutation_headers(
        self,
        *,
        host: str = "127.0.0.1",
        origin: str | None = BASE_URL,
    ) -> dict[str, str]:
        headers = {
            "Host": host,
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": CSRF_TOKEN,
        }
        if origin is not None:
            headers["Origin"] = origin
        return headers


def make_harness(
    ledger: Ledger,
    *,
    pieces: FakePiecesClient | None = None,
    command_roots: tuple[tuple[str, Path], ...] = (),
) -> ApiHarness:
    fake_pieces = pieces or FakePiecesClient()
    dependencies = ApiDependencies(
        ledger=ledger,
        scan_service=ScanService(ledger),
        review_service=ReviewService(ledger, fake_pieces),
        import_service=ImportService(ledger, fake_pieces),
        pieces_client=fake_pieces,
        csrf_token=CSRF_TOKEN,
        effective_mcp_base_url="http://pieces.test",
        mcp_base_url_source="cli",
        command_roots=command_roots,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_api_router(dependencies))
    install_loopback_security(
        app,
        LoopbackSecurityConfig(
            csrf_token=CSRF_TOKEN,
            listener_origins=(
                "http://127.0.0.1",
                "http://localhost",
                "http://[::1]",
            ),
            max_json_body_bytes=262_144,
        ),
    )
    return ApiHarness(
        client=TestClient(app, base_url=BASE_URL, raise_server_exceptions=False),
        ledger=ledger,
        pieces=fake_pieces,
    )


@pytest.fixture
def api(ledger: Ledger) -> Iterator[ApiHarness]:
    harness = make_harness(ledger)
    try:
        yield harness
    finally:
        harness.client.close()


def assert_error(response: Any, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["message"], str)
    assert len(payload["error"]["message"]) <= 512
    assert isinstance(payload["error"]["details"], dict)


def _start_request(
    api: ApiHarness,
    candidate: Any,
    *,
    acknowledge_remote_duplicate_risk: bool = False,
) -> dict[str, Any]:
    candidate_ids = [candidate.candidate_id]
    versions = {candidate.candidate_id: candidate.version}
    hashes = {candidate.candidate_id: candidate.payload_hash}
    preview = api.client.post(
        "/api/import-jobs",
        headers=api.mutation_headers(),
        json={
            "action": "preview",
            "candidate_ids": candidate_ids,
            "candidate_versions": versions,
            "payload_hashes": hashes,
        },
    )
    assert preview.status_code == 202
    context = preview.json()
    return {
        "action": "start",
        "candidate_ids": candidate_ids,
        "candidate_versions": versions,
        "payload_hashes": hashes,
        "confirmation": {
            "confirmed": True,
            "pieces_endpoint": context["pieces_endpoint"],
            "selected_write_count": context["selected_write_count"],
            "context_hash": context["context_hash"],
        },
        "acknowledge_remote_duplicate_risk": acknowledge_remote_duplicate_risk,
    }


def test_router_exposes_exactly_the_nine_internal_json_operations(ledger: Ledger) -> None:
    pieces = FakePiecesClient()
    router = create_api_router(
        ApiDependencies(
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
    )

    operations = {
        (method, route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert operations == {
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


def test_scan_start_status_counts_and_dispositions(api: ApiHarness, tmp_path: Path) -> None:
    empty_root = tmp_path / "empty-codex"
    empty_root.mkdir()
    settings_version = api.ledger.get_settings().version

    started = api.client.post(
        "/api/scans",
        headers=api.mutation_headers(),
        json={
            "settings_version": settings_version,
            "roots": [{"agent": "codex", "path": str(empty_root)}],
        },
    )

    assert started.status_code == 202
    assert UUID4_RE.fullmatch(started.json()["scan_id"])
    assert started.json()["state"] in {"queued", "running", "completed"}

    status = api.client.get(f"/api/scans/{started.json()['scan_id']}")
    assert status.status_code == 200
    payload = status.json()
    assert payload["scan_id"] == started.json()["scan_id"]
    assert payload["state"] in {"queued", "running", "completed", "failed"}
    assert set(payload["counts"]) == {
        "discovered",
        "accepted",
        "excluded",
        "quarantined",
        "errors",
    }
    assert isinstance(payload["by_agent"], dict)
    assert isinstance(payload["disposition_by_reason"], dict)
    assert "error" in payload


def test_scan_rejects_stale_settings_and_invalid_roots(api: ApiHarness) -> None:
    stale = api.client.post(
        "/api/scans",
        headers=api.mutation_headers(),
        json={"settings_version": api.ledger.get_settings().version + 1},
    )
    assert_error(stale, 409, "settings_version_conflict")

    relative = api.client.post(
        "/api/scans",
        headers=api.mutation_headers(),
        json={
            "settings_version": api.ledger.get_settings().version,
            "roots": [{"agent": "codex", "path": "relative/path"}],
        },
    )
    assert_error(relative, 422, "validation_error")


def test_candidates_support_project_security_filters_facets_and_pagination(
    api: ApiHarness,
) -> None:
    clean = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Clean memory",
            markdown_body="Keep the import deterministic.",
            project_scope="Alpha",
        ),
        source_key="clean.md",
        source_path="/codex/clean.md",
    )
    warning = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Contact memory",
            markdown_body="Contact owner@example.test before release.",
            project_scope="Beta",
        ),
        source_key="warning.md",
        source_path="/codex/warning.md",
    )
    api.ledger.add_safety_finding(
        candidate_id=warning.candidate_id,
        candidate_version=warning.version,
        reason_code="pii_email",
        severity=FindingSeverity.WARN,
        line_number=1,
    )

    response = api.client.get(
        "/api/candidates",
        params={"project": "Beta", "security_state": "warning", "page_size": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["candidate_id"] for item in payload["items"]] == [warning.candidate_id]
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert payload["total"] == 1
    assert payload["items"][0]["safety"]["approval_checked"] is False
    assert payload["items"][0]["safety"]["findings"][0] == {
        "finding_id": api.ledger.connection.execute(
            "SELECT finding_id FROM safety_findings WHERE candidate_id = ?",
            (warning.candidate_id,),
        ).fetchone()["finding_id"],
        "reason_code": "pii_email",
        "severity": "warn",
        "line": 1,
        "state": "open",
    }
    assert {facet["value"] for facet in payload["facets"]["projects"]} == {
        "Alpha",
        "Beta",
    }
    assert payload["facets"]["security_states"]["warning"] == 1

    assert api.client.get("/api/candidates", params={"q": "clean.md"}).json()["total"] == 1
    assert api.client.get("/api/candidates", params={"source_agent": "codex"}).json()[
        "total"
    ] == 2
    assert clean.candidate_id != warning.candidate_id

    assert_error(
        api.client.get("/api/candidates", params={"page_size": 201}),
        422,
        "validation_error",
    )
    assert_error(
        api.client.get(
            "/api/candidates",
            params={"include_changed_source": "true", "page_size": 11},
        ),
        422,
        "validation_error",
    )


def test_changed_source_returns_prior_snapshot_and_secret_redacted_line_diff(
    api: ApiHarness,
) -> None:
    candidate = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Rotated credential",
            markdown_body=_password_assignment("historical-secret-value"),
            project_scope="Alpha",
        ),
        source_key="credential.md",
        source_path="/codex/credential.md",
    )
    finding_id = api.ledger.add_safety_finding(
        candidate_id=candidate.candidate_id,
        candidate_version=candidate.version,
        reason_code="secret_assignment",
        severity=FindingSeverity.BLOCK,
        line_number=1,
    )
    review = ReviewService(api.ledger)
    approved = review.approve_candidate(
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        override=_finding_override(finding_id),
    )
    edited = review.edit_candidate(
        candidate_id=approved.candidate_id,
        expected_version=approved.version,
        payload=approved.payload.model_copy(
            update={"markdown_body": _password_assignment("current-secret-value")}
        ),
    )

    response = api.client.get(
        "/api/candidates",
        params={
            "q": "Rotated credential",
            "include_changed_source": "true",
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["candidate_id"] == edited.candidate_id
    assert item["prior_snapshot"]["state"] == "approved"
    assert item["prior_snapshot"]["candidate_version"] == approved.version
    assert item["changed_source"]["available"] is True
    texts = [
        line["text"]
        for hunk in item["changed_source"]["hunks"]
        for line in hunk["lines"]
    ]
    assert texts
    assert set(texts) == {"[redacted:secret_assignment]"}
    assert "historical-secret-value" not in response.text
    assert "current-secret-value" not in response.text


def _finding_override(finding_id: str) -> Any:
    from agent2pieces.services import FindingOverride

    return FindingOverride(
        finding_ids=(finding_id,),
        acknowledged=True,
        reason="The reviewer verified and accepts this exact source finding.",
    )


def test_candidate_save_override_approve_exclude_and_optimistic_conflict(
    api: ApiHarness,
) -> None:
    candidate = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Review candidate",
            markdown_body="Contact reviewer@example.test.",
        ),
        source_key="review.md",
        source_path="/codex/review.md",
    )
    finding_id = api.ledger.add_safety_finding(
        candidate_id=candidate.candidate_id,
        candidate_version=candidate.version,
        reason_code="pii_email",
        severity=FindingSeverity.WARN,
        line_number=1,
    )

    overridden = api.client.patch(
        f"/api/candidates/{candidate.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": candidate.version,
            "action": "override_findings",
            "finding_ids": [finding_id],
            "finding_acknowledged": True,
            "override_reason": "The reviewer verified this contact address for the import.",
        },
    )
    assert overridden.status_code == 200
    assert overridden.json()["safety"]["findings"][0]["state"] == "overridden"

    approved = api.client.patch(
        f"/api/candidates/{candidate.candidate_id}",
        headers=api.mutation_headers(),
        json={"version": candidate.version, "action": "approve", "target": "candidate"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    stale = api.client.patch(
        f"/api/candidates/{candidate.candidate_id}",
        headers=api.mutation_headers(),
        json={"version": candidate.version, "action": "exclude", "target": "candidate"},
    )
    assert_error(stale, 409, "candidate_version_conflict")

    saved = api.client.patch(
        f"/api/candidates/{candidate.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": approved.json()["version"],
            "action": "save",
            "title": "Edited review candidate",
            "markdown_body": "The contact was removed.",
            "external_links": ["https://example.test/review"],
            "project_scope": "Alpha",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["status"] == "pending"
    assert saved.json()["payload_hash"] != approved.json()["payload_hash"]

    excluded = api.client.patch(
        f"/api/candidates/{candidate.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": saved.json()["version"],
            "action": "exclude",
            "target": "candidate",
        },
    )
    assert excluded.status_code == 200
    assert excluded.json()["status"] == "excluded"


def test_duplicate_check_group_default_override_approve_and_exclude(api: ApiHarness) -> None:
    shared_body = "Persist the dispatch boundary before entering the remote SDK call."
    first = add_candidate(
        api.ledger,
        payload=CandidatePayload(title="Dispatch boundary", markdown_body=shared_body),
        source_key="first.md",
        source_path="/codex/first.md",
    )
    second = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Dispatch boundary",
            markdown_body=shared_body,
            external_links=["https://example.test/design"],
            project_scope="Alpha",
        ),
        source_key="second.md",
        source_path="/codex/second.md",
    )

    checked = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        json={
            "candidate_ids": [first.candidate_id, second.candidate_id],
            "candidate_versions": {first.candidate_id: 1, second.candidate_id: 1},
        },
    )
    assert checked.status_code == 200
    check_payload = checked.json()
    assert check_payload["coverage"] == "local-only"
    assert {
        result["coverage"] for result in check_payload["results"]
    } == {"local-only"}
    assert {result["candidate_id"] for result in check_payload["results"]} == {
        first.candidate_id,
        second.candidate_id,
    }
    suggestion = check_payload["suggested_groups"][0]
    assert suggestion["default_representative_candidate_id"] == second.candidate_id

    created = api.client.patch(
        f"/api/candidates/{first.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": first.version,
            "action": "group_create",
            "duplicate_check_id": check_payload["check_id"],
            "evidence_ids": suggestion["evidence_ids"],
        },
    )
    assert created.status_code == 200
    group = created.json()["group"]
    assert group["representative_candidate_id"] == second.candidate_id
    assert group["status"] == "draft"

    selected = api.client.patch(
        f"/api/candidates/{first.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": first.version,
            "action": "set_representative",
            "target": "group",
            "group_id": group["group_id"],
            "group_version": group["version"],
        },
    )
    assert selected.status_code == 200
    assert selected.json()["group"]["representative_candidate_id"] == first.candidate_id

    approved = api.client.patch(
        f"/api/candidates/{first.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": first.version,
            "action": "approve",
            "target": "group",
            "group_id": group["group_id"],
            "group_version": selected.json()["group"]["version"],
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["group"]["status"] == "approved"

    excluded = api.client.patch(
        f"/api/candidates/{first.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": approved.json()["version"],
            "action": "exclude",
            "target": "group",
            "group_id": group["group_id"],
            "group_version": approved.json()["group"]["version"],
        },
    )
    assert excluded.status_code == 200
    assert excluded.json()["group"]["status"] == "excluded"


@pytest.mark.parametrize(
    ("action", "invalid_path_candidate", "error_code"),
    [
        pytest.param("approve", "stale_member", "candidate_version_conflict"),
        pytest.param("exclude", "stale_member", "candidate_version_conflict"),
        pytest.param("approve", "nonmember", "invalid_candidate_state"),
        pytest.param("exclude", "nonmember", "invalid_candidate_state"),
    ],
)
def test_group_decisions_bind_the_path_candidate_and_version(
    api: ApiHarness,
    action: str,
    invalid_path_candidate: str,
    error_code: str,
) -> None:
    shared_body = "Persist the dispatch boundary before entering the remote SDK call."
    first = add_candidate(
        api.ledger,
        payload=CandidatePayload(title="Dispatch boundary", markdown_body=shared_body),
        source_key="first.md",
        source_path="/codex/first.md",
    )
    second = add_candidate(
        api.ledger,
        payload=CandidatePayload(title="Dispatch boundary", markdown_body=shared_body),
        source_key="second.md",
        source_path="/codex/second.md",
    )
    unrelated = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Separate decision",
            markdown_body="Keep this candidate outside the duplicate group.",
        ),
        source_key="unrelated.md",
        source_path="/codex/unrelated.md",
    )
    checked = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        json={
            "candidate_ids": [first.candidate_id, second.candidate_id],
            "candidate_versions": {
                first.candidate_id: first.version,
                second.candidate_id: second.version,
            },
        },
    )
    assert checked.status_code == 200
    suggestion = checked.json()["suggested_groups"][0]
    created = api.client.patch(
        f"/api/candidates/{first.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": first.version,
            "action": "group_create",
            "duplicate_check_id": checked.json()["check_id"],
            "evidence_ids": suggestion["evidence_ids"],
        },
    )
    assert created.status_code == 200
    group = created.json()["group"]

    path_candidate = first if invalid_path_candidate == "stale_member" else unrelated
    request_version = (
        path_candidate.version + 1
        if invalid_path_candidate == "stale_member"
        else path_candidate.version
    )
    response = api.client.patch(
        f"/api/candidates/{path_candidate.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": request_version,
            "action": action,
            "target": "group",
            "group_id": group["group_id"],
            "group_version": group["version"],
        },
    )

    assert_error(response, 409, error_code)
    persisted_group = api.ledger.connection.execute(
        "SELECT status, version FROM review_groups WHERE group_id = ?",
        (group["group_id"],),
    ).fetchone()
    assert (persisted_group["status"], persisted_group["version"]) == (
        "draft",
        group["version"],
    )
    member_statuses = api.ledger.connection.execute(
        "SELECT DISTINCT status FROM candidates WHERE group_id = ?",
        (group["group_id"],),
    ).fetchall()
    assert [row["status"] for row in member_statuses] == ["pending"]


def test_candidate_reload_retains_duplicate_evidence_for_both_local_matches(
    api: ApiHarness,
) -> None:
    shared_body = "Persist the dispatch boundary before entering the remote SDK call."
    first = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Dispatch boundary summary",
            markdown_body=shared_body,
        ),
        source_key="first.md",
        source_path="/codex/first.md",
    )
    second = add_candidate(
        api.ledger,
        payload=CandidatePayload(
            title="Dispatch boundary decision",
            markdown_body=shared_body,
        ),
        source_key="second.md",
        source_path="/codex/second.md",
    )

    checked = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        json={
            "candidate_ids": [first.candidate_id, second.candidate_id],
            "candidate_versions": {
                first.candidate_id: first.version,
                second.candidate_id: second.version,
            },
        },
    )

    assert checked.status_code == 200
    checked_results = {
        result["candidate_id"]: result["evidence"] for result in checked.json()["results"]
    }
    assert set(checked_results) == {first.candidate_id, second.candidate_id}
    assert all(checked_results.values())

    reloaded_evidence: dict[str, list[dict[str, Any]]] = {}
    for candidate, source_key in ((first, "first.md"), (second, "second.md")):
        response = api.client.get(
            "/api/candidates",
            params={"q": source_key, "page_size": 10},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        item = response.json()["items"][0]
        assert item["candidate_id"] == candidate.candidate_id
        reloaded_evidence[candidate.candidate_id] = item["duplicate"]["evidence"]

    assert {
        candidate_id
        for candidate_id, evidence in reloaded_evidence.items()
        if evidence
    } == {first.candidate_id, second.candidate_id}
    assert {
        candidate_id: [
            (evidence["target_key"], evidence["target_title"])
            for evidence in evidence_items
        ]
        for candidate_id, evidence_items in reloaded_evidence.items()
    } == {
        first.candidate_id: [(second.candidate_id, second.payload.title)],
        second.candidate_id: [(first.candidate_id, first.payload.title)],
    }

    edited = api.client.patch(
        f"/api/candidates/{second.candidate_id}",
        headers=api.mutation_headers(),
        json={
            "version": second.version,
            "action": "save",
            "title": "Changed dispatch boundary decision",
            "markdown_body": shared_body,
            "external_links": [],
            "project_scope": "",
        },
    )
    assert edited.status_code == 200

    unchanged = api.client.get(
        "/api/candidates",
        params={"q": "first.md", "page_size": 10},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["total"] == 1
    duplicate = unchanged.json()["items"][0]["duplicate"]
    assert duplicate["evidence"] == []
    assert duplicate["verdict"] == "distinct"


def test_duplicate_check_rejects_stale_versions_and_caps_batch(api: ApiHarness) -> None:
    candidate = add_candidate(
        api.ledger,
        payload=CandidatePayload(title="One", markdown_body="One candidate."),
    )
    stale = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        json={
            "candidate_ids": [candidate.candidate_id],
            "candidate_versions": {candidate.candidate_id: candidate.version + 1},
        },
    )
    assert_error(stale, 409, "candidate_version_conflict")

    too_many = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        json={"candidate_ids": [candidate.candidate_id] * 501, "candidate_versions": {}},
    )
    assert_error(too_many, 422, "validation_error")


def test_import_start_binds_endpoint_count_versions_and_displayed_payload_hashes(
    api: ApiHarness,
) -> None:
    candidate = approve_candidate(
        api.ledger,
        add_candidate(
            api.ledger,
            payload=CandidatePayload(title="Approved", markdown_body="Approved body."),
        ),
    )
    request = _start_request(api, candidate)

    wrong_count = dict(request)
    wrong_count["confirmation"] = {**request["confirmation"], "selected_write_count": 2}
    before = api.ledger.connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0]
    mismatch = api.client.post(
        "/api/import-jobs", headers=api.mutation_headers(), json=wrong_count
    )
    assert_error(mismatch, 422, "apply_confirmation_mismatch")
    assert api.ledger.connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0] == before

    wrong_context = dict(request)
    wrong_context["confirmation"] = {
        **request["confirmation"],
        "context_hash": "0" * 64,
    }
    mismatch = api.client.post(
        "/api/import-jobs", headers=api.mutation_headers(), json=wrong_context
    )
    assert_error(mismatch, 422, "apply_confirmation_mismatch")

    stale_hash = dict(request)
    stale_hash["payload_hashes"] = {candidate.candidate_id: "0" * 64}
    conflict = api.client.post(
        "/api/import-jobs", headers=api.mutation_headers(), json=stale_hash
    )
    assert_error(conflict, 409, "candidate_version_conflict")

    started = api.client.post(
        "/api/import-jobs", headers=api.mutation_headers(), json=request
    )
    assert started.status_code == 202
    assert UUID4_RE.fullmatch(started.json()["job_id"])
    assert started.json()["state"] in {"queued", "running", "paused", "completed"}

    status = api.client.get(f"/api/import-jobs/{started.json()['job_id']}")
    assert status.status_code == 200
    job = status.json()
    assert job["job_id"] == started.json()["job_id"]
    assert job["items"][0]["candidate_id"] == candidate.candidate_id
    assert job["items"][0]["candidate_version"] == candidate.version
    assert job["items"][0]["import_id"] == candidate.import_id
    assert set(job["items"][0]) == {
        "ordinal",
        "candidate_id",
        "candidate_version",
        "import_id",
        "source",
        "state",
        "attempts",
        "attempt_telemetry",
        "write_context",
        "pieces_memory_id",
        "error",
    }
    assert job["items"][0]["source"] == {
        "agent": "codex",
        "source_key": "memory.md",
    }
    assert {
        attempt["tool"] for attempt in job["items"][0]["attempt_telemetry"]
    } == {"annotations_full_text_search", "create_pieces_memory"}
    assert all(
        attempt["endpoint"] == api.pieces.capabilities.endpoint
        for attempt in job["items"][0]["attempt_telemetry"]
    )
    assert job["pieces_endpoint"] == api.pieces.capabilities.endpoint
    assert job["context_hash"] == request["confirmation"]["context_hash"]


def test_import_preview_displays_and_binds_exact_mapping_context(
    api: ApiHarness,
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "project"
    source = local_root / "memory" / "topic.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Topic\n", encoding="utf-8")
    candidate = approve_candidate(
        api.ledger,
        add_candidate(
            api.ledger,
            payload=CandidatePayload(
                title="Mapped preview",
                markdown_body="This body must not be repeated in preview output.",
            ),
            source_path=str(source),
        ),
    )
    api.ledger.add_host_path_mapping(
        local_root=str(local_root.resolve()),
        host_root="D:/confirmed/project",
        project="confirmed-project",
    )

    request = _start_request(api, candidate)
    preview = api.client.post(
        "/api/import-jobs",
        headers=api.mutation_headers(),
        json={
            "action": "preview",
            "candidate_ids": request["candidate_ids"],
            "candidate_versions": request["candidate_versions"],
            "payload_hashes": request["payload_hashes"],
        },
    )
    preview_payload = preview.json()
    assert re.fullmatch(r"[0-9a-f]{64}", preview_payload["context_hash"])
    assert preview_payload["items"] == [
        {
            "candidate_id": candidate.candidate_id,
            "title": candidate.payload.title,
            "payload_hash": candidate.payload_hash,
            "project": "confirmed-project",
            "files": ["D:/confirmed/project/memory/topic.md"],
        }
    ]
    assert candidate.payload.markdown_body not in preview.text

    api.ledger.connection.execute("DELETE FROM host_path_mappings")
    api.ledger.add_host_path_mapping(
        local_root=str(local_root.resolve()),
        host_root="Z:/changed/project",
        project="changed-project",
    )
    stale = api.client.post(
        "/api/import-jobs", headers=api.mutation_headers(), json=request
    )
    assert_error(stale, 422, "apply_confirmation_mismatch")


def test_failed_import_exposes_safe_attempt_duration_retry_and_recovery_telemetry(
    ledger: Ledger,
) -> None:
    pieces = FakePiecesClient(
        marker_results=(
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
        ),
        write_results=("post_failure",),
    )
    api = make_harness(ledger, pieces=pieces)
    private_body = "Body value must not appear in attempt telemetry."
    try:
        candidate = approve_candidate(
            ledger,
            add_candidate(
                ledger,
                payload=CandidatePayload(title="Timed failure", markdown_body=private_body),
                source_key="timed-failure.md",
                source_path="/codex/timed-failure.md",
            ),
        )
        started = api.client.post(
            "/api/import-jobs",
            headers=api.mutation_headers(),
            json=_start_request(api, candidate),
        )
        status = api.client.get(f"/api/import-jobs/{started.json()['job_id']}")

        assert status.status_code == 200
        item = status.json()["items"][0]
        assert item["state"] == "ambiguous"
        write = next(
            attempt
            for attempt in item["attempt_telemetry"]
            if attempt["tool"] == "create_pieces_memory"
        )
        assert write["endpoint"] == pieces.capabilities.endpoint
        assert isinstance(write["duration_ms"], int)
        assert write["duration_ms"] >= 0
        assert write["retry_state"] == "subsequent"
        assert write["recovery_state"] == "required"
        assert write["error"] == "timeout"
        assert private_body not in json.dumps(item["attempt_telemetry"])
    finally:
        api.client.close()


def test_import_requires_per_job_risk_ack_and_resume_mutates_same_job(
    ledger: Ledger,
) -> None:
    pieces = FakePiecesClient(search_available=False)
    api = make_harness(ledger, pieces=pieces)
    try:
        candidate = approve_candidate(
            ledger,
            add_candidate(
                ledger,
                payload=CandidatePayload(title="Local only", markdown_body="Approved body."),
            ),
        )
        request = _start_request(api, candidate)
        unacknowledged = api.client.post(
            "/api/import-jobs", headers=api.mutation_headers(), json=request
        )
        assert_error(unacknowledged, 422, "remote_duplicate_risk_unacknowledged")

        request["acknowledge_remote_duplicate_risk"] = True
        started = api.client.post(
            "/api/import-jobs", headers=api.mutation_headers(), json=request
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]

        job = ledger.get_import_job(job_id)
        if job.state.value == "completed":
            candidate = approve_candidate(
                ledger,
                add_candidate(
                    ledger,
                    payload=CandidatePayload(title="Paused", markdown_body="Paused body."),
                    source_key="paused.md",
                    source_path="/codex/paused.md",
                ),
            )
            service = ImportService(ledger, pieces)
            preview = service.preview_job(
                candidate_ids=(candidate.candidate_id,),
                candidate_versions={candidate.candidate_id: candidate.version},
                displayed_payload_hashes={candidate.candidate_id: candidate.payload_hash},
            )
            job = service.create_job(
                candidate_ids=(candidate.candidate_id,),
                candidate_versions={candidate.candidate_id: candidate.version},
                displayed_payload_hashes={candidate.candidate_id: candidate.payload_hash},
                confirmation=_confirmation(
                    pieces.capabilities.endpoint, preview.context_hash
                ),
                acknowledge_remote_duplicate_risk=True,
            )
            job_id = job.job_id
        ledger.pause_import_job(job_id=job_id, reason="manual_review")
        item = ledger.list_import_items(job_id)[0]
        ledger.connection.execute(
            "UPDATE import_items SET state = ? WHERE item_id = ?",
            (ImportItemState.FAILED, item.item_id),
        )
        job_count_before_resume = ledger.connection.execute(
            "SELECT COUNT(*) FROM import_jobs"
        ).fetchone()[0]

        resumed = api.client.post(
            "/api/import-jobs",
            headers=api.mutation_headers(),
            json={
                "action": "resume",
                "resume_job_id": job_id,
                "resolution": "skip",
                "acknowledge_duplicate_write_risk": False,
            },
        )
        assert resumed.status_code == 202
        assert resumed.json()["job_id"] == job_id
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0]
            == job_count_before_resume
        )
    finally:
        api.client.close()


def _confirmation(endpoint: str, context_hash: str) -> Any:
    from agent2pieces.services import ApplyConfirmation

    return ApplyConfirmation(
        confirmed=True,
        pieces_endpoint=endpoint,
        selected_write_count=1,
        context_hash=context_hash,
    )


def test_settings_round_trip_roots_mappings_capabilities_and_version(
    api: ApiHarness,
    tmp_path: Path,
) -> None:
    initial = api.client.get("/api/settings")
    assert initial.status_code == 200
    current = initial.json()
    assert current["csrf_token"] == CSRF_TOKEN
    assert current["version"] == api.ledger.get_settings().version
    assert current["effective_mcp_base_url"] == "http://pieces.test"
    assert current["mcp_base_url_source"] == "cli"
    assert current["mcp_base_url_restart_required"] is True
    assert current["capabilities"]["effective_endpoint"] == api.pieces.capabilities.endpoint
    assert current["capabilities"]["create_pieces_memory"] is True

    source_root = tmp_path / "codex"
    local_root = tmp_path / "project"
    source_root.mkdir()
    local_root.mkdir()
    updated = api.client.put(
        "/api/settings",
        headers=api.mutation_headers(),
        json={
            "version": current["version"],
            "source_roots": [
                {"agent": "codex", "path": str(source_root), "enabled": True}
            ],
            "host_path_mappings": [
                {
                    "local_root": str(local_root),
                    "host_root": "C:/host/project",
                    "project": "project-id",
                }
            ],
            "mcp_base_url": "http://saved.example.test:39300",
        },
    )
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["version"] == current["version"] + 1
    assert payload["mcp_base_url"] == "http://saved.example.test:39300"
    assert payload["effective_mcp_base_url"] == "http://pieces.test"
    assert payload["mcp_base_url_source"] == "cli"
    assert payload["mcp_base_url_restart_required"] is True
    assert payload["source_roots"][0]["path"] == str(source_root.resolve())
    assert payload["source_roots"][0]["enabled"] is True
    assert UUID4_RE.fullmatch(payload["source_roots"][0]["root_id"])
    assert payload["host_path_mappings"][0]["host_root"] == "C:/host/project"
    assert UUID4_RE.fullmatch(payload["host_path_mappings"][0]["mapping_id"])

    stale = api.client.put(
        "/api/settings",
        headers=api.mutation_headers(),
        json={
            "version": current["version"],
            "source_roots": [],
            "host_path_mappings": [],
            "mcp_base_url": "http://pieces.test",
        },
    )
    assert_error(stale, 409, "settings_version_conflict")


@pytest.mark.parametrize(
    "mcp_base_url",
    [
        "http://pieces.test?secret=value",
        "http://pieces.test#fragment",
        " http://pieces.test",
        "http://pieces.test/path with spaces",
        "http://pieces.test\\@other.test",
        "http://pieces.test:70000",
    ],
)
def test_settings_rejects_noncanonical_pieces_urls(
    api: ApiHarness,
    mcp_base_url: str,
) -> None:
    response = api.client.put(
        "/api/settings",
        headers=api.mutation_headers(),
        json={
            "version": api.ledger.get_settings().version,
            "source_roots": [],
            "host_path_mappings": [],
            "mcp_base_url": mcp_base_url,
        },
    )

    assert_error(response, 422, "validation_error")


@pytest.mark.parametrize(
    "path",
    [
        "/api/scans/not-a-uuid",
        "/api/scans/00000000-0000-4000-8000-000000000000",
        "/api/import-jobs/not-a-uuid",
        "/api/import-jobs/00000000-0000-4000-8000-000000000000",
    ],
)
def test_malformed_and_missing_identifiers_are_404(api: ApiHarness, path: str) -> None:
    assert_error(api.client.get(path), 404, "not_found")


def test_method_and_schema_failures_are_bounded(api: ApiHarness) -> None:
    wrong_method = api.client.delete("/api/settings")
    assert_error(wrong_method, 405, "method_not_allowed")

    malformed = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        json={"candidate_ids": "not-an-array", "candidate_versions": {}},
    )
    assert_error(malformed, 422, "validation_error")
