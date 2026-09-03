from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from agent2pieces.ledger import Ledger

from .test_api_contract import BASE_URL, CSRF_TOKEN, ApiHarness, assert_error, make_harness


@pytest.fixture
def api(ledger: Ledger) -> Iterator[ApiHarness]:
    harness = make_harness(ledger)
    try:
        yield harness
    finally:
        harness.client.close()


def mutation_headers(
    *,
    host: str,
    origin: str | None,
    referer: str | None = None,
    csrf_token: str = CSRF_TOKEN,
    fetch_site: str = "same-origin",
    content_type: str = "application/json",
) -> dict[str, str]:
    headers = {
        "Host": host,
        "Content-Type": content_type,
        "Sec-Fetch-Site": fetch_site,
        "X-CSRF-Token": csrf_token,
    }
    if origin is not None:
        headers["Origin"] = origin
    if referer is not None:
        headers["Referer"] = referer
    return headers


@pytest.mark.parametrize(
    ("request_origin", "host", "origin"),
    [
        ("http://127.0.0.1", "127.0.0.1", "http://127.0.0.1"),
        ("http://127.0.0.1:7345", "127.0.0.1:7345", "http://127.0.0.1:7345"),
        ("http://localhost", "localhost", "http://localhost"),
        ("http://localhost:7345", "localhost:7345", "http://localhost:7345"),
        ("http://[::1]", "[::1]", "http://[::1]"),
        ("http://[::1]:7345", "[::1]:7345", "http://[::1]:7345"),
    ],
)
@pytest.mark.asyncio
async def test_loopback_host_and_exact_same_origin_are_accepted(
    api: ApiHarness,
    request_origin: str,
    host: str,
    origin: str,
) -> None:
    transport = httpx.ASGITransport(
        app=api.client.app,
        raise_app_exceptions=False,
        client=("::1" if "[::1]" in request_origin else "127.0.0.1", 49152),
    )
    async with httpx.AsyncClient(transport=transport, base_url=request_origin) as client:
        response = await client.post(
            "/api/duplicates/check-pieces",
            headers=mutation_headers(host=host, origin=origin),
            json={"candidate_ids": [], "candidate_versions": {}},
        )

    assert response.status_code not in {400, 403}
    assert response.status_code in {200, 409, 422}


@pytest.mark.parametrize(
    "host",
    [
        "evil.test",
        "localhost.evil.test",
        "127.0.0.1.evil.test",
        "127.0.0.1@evil.test",
        "[::1].evil.test",
        "0.0.0.0",
        "192.168.1.50",
    ],
)
def test_dns_rebinding_and_non_loopback_hosts_are_rejected(
    api: ApiHarness,
    host: str,
) -> None:
    response = api.client.get("/api/settings", headers={"Host": host})

    assert_error(response, 400, "invalid_host")


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Forwarded", "host=evil.test;proto=https"),
        ("X-Forwarded-Host", "evil.test"),
        ("X-Forwarded-Proto", "https"),
        ("X-Original-Host", "evil.test"),
    ],
)
def test_forwarding_headers_are_rejected(
    api: ApiHarness,
    header: str,
    value: str,
) -> None:
    response = api.client.get(
        "/api/settings",
        headers={"Host": "127.0.0.1", header: value},
    )

    assert_error(response, 400, "forwarded_request_rejected")


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        (
            mutation_headers(
                host="127.0.0.1",
                origin=BASE_URL,
                csrf_token="wrong-token",
            ),
            "csrf_rejected",
        ),
        (
            {
                "Host": "127.0.0.1",
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "same-origin",
                "Origin": BASE_URL,
            },
            "csrf_rejected",
        ),
        (
            mutation_headers(host="127.0.0.1", origin="http://evil.test"),
            "origin_rejected",
        ),
        (
            mutation_headers(host="127.0.0.1", origin="http://127.0.0.1.evil.test"),
            "origin_rejected",
        ),
        (
            mutation_headers(
                host="127.0.0.1",
                origin=BASE_URL,
                fetch_site="cross-site",
            ),
            "fetch_site_rejected",
        ),
        (
            mutation_headers(host="127.0.0.1", origin=None),
            "origin_rejected",
        ),
    ],
)
def test_mutations_require_process_csrf_and_same_origin(
    api: ApiHarness,
    headers: dict[str, str],
    code: str,
) -> None:
    response = api.client.post(
        "/api/duplicates/check-pieces",
        headers=headers,
        json={"candidate_ids": [], "candidate_versions": {}},
    )

    assert_error(response, 403, code)


def test_same_origin_referer_is_valid_fallback_but_cross_origin_referer_is_not(
    api: ApiHarness,
) -> None:
    same_origin = api.client.post(
        "/api/duplicates/check-pieces",
        headers=mutation_headers(
            host="127.0.0.1",
            origin=None,
            referer=f"{BASE_URL}/review?tab=pending",
        ),
        json={"candidate_ids": [], "candidate_versions": {}},
    )
    assert same_origin.status_code not in {400, 403}

    cross_origin = api.client.post(
        "/api/duplicates/check-pieces",
        headers=mutation_headers(
            host="127.0.0.1",
            origin=None,
            referer="http://evil.test/review",
        ),
        json={"candidate_ids": [], "candidate_versions": {}},
    )
    assert_error(cross_origin, 403, "origin_rejected")


@pytest.mark.parametrize("path", ["/api/scans", "/api/duplicates/check-pieces", "/api/import-jobs"])
def test_post_mutations_require_json_content_type(api: ApiHarness, path: str) -> None:
    response = api.client.post(
        path,
        headers=mutation_headers(
            host="127.0.0.1",
            origin=BASE_URL,
            content_type="text/plain",
        ),
        content="{}",
    )

    assert_error(response, 415, "unsupported_media_type")


def test_patch_and_put_mutations_require_json_content_type(api: ApiHarness) -> None:
    patch = api.client.patch(
        "/api/candidates/00000000-0000-4000-8000-000000000000",
        headers=mutation_headers(
            host="127.0.0.1",
            origin=BASE_URL,
            content_type="application/x-www-form-urlencoded",
        ),
        content="version=1",
    )
    assert_error(patch, 415, "unsupported_media_type")

    put = api.client.put(
        "/api/settings",
        headers=mutation_headers(
            host="127.0.0.1",
            origin=BASE_URL,
            content_type="text/plain",
        ),
        content="{}",
    )
    assert_error(put, 415, "unsupported_media_type")


def test_json_request_body_is_bounded_before_validation(api: ApiHarness) -> None:
    oversized = "x" * 300_000
    response = api.client.post(
        "/api/duplicates/check-pieces",
        headers=api.mutation_headers(),
        content='{"candidate_ids":[],"candidate_versions":{},"padding":"'
        + oversized
        + '"}',
    )

    assert_error(response, 413, "request_too_large")
    assert oversized[:100] not in response.text


def test_security_and_cache_headers_are_present_without_cors(api: ApiHarness) -> None:
    response = api.client.get("/api/settings", headers={"Host": "127.0.0.1"})

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers

    preflight = api.client.options(
        "/api/settings",
        headers={
            "Host": "127.0.0.1",
            "Origin": "http://evil.test",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-methods" not in preflight.headers


def test_settings_rejects_url_credentials_without_echoing_them(
    api: ApiHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    credential = "do-not-log-this-password"
    response = api.client.put(
        "/api/settings",
        headers=api.mutation_headers(),
        json={
            "version": api.ledger.get_settings().version,
            "source_roots": [],
            "host_path_mappings": [],
            "mcp_base_url": f"http://admin:{credential}@pieces.test",
        },
    )

    assert_error(response, 422, "validation_error")
    assert credential not in response.text
    assert credential not in caplog.text
    assert "admin:" not in response.text
    assert "admin:" not in caplog.text


def test_validation_and_not_found_errors_do_not_log_body_secret_or_csrf(
    api: ApiHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    body_secret = "".join(("api_", "key=", "body-value-that-must-never-appear"))
    secret_token = "csrf-value-that-must-never-appear"
    response = api.client.patch(
        "/api/candidates/00000000-0000-4000-8000-000000000000",
        headers=mutation_headers(
            host="127.0.0.1",
            origin=BASE_URL,
            csrf_token=secret_token,
        ),
        json={
            "version": 1,
            "action": "save",
            "title": "Sensitive",
            "markdown_body": body_secret,
        },
    )

    assert_error(response, 403, "csrf_rejected")
    combined = response.text + caplog.text
    assert body_secret not in combined
    assert secret_token not in combined
    assert CSRF_TOKEN not in combined


def test_error_middleware_bounds_unexpected_failures_without_internal_details(
    api: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    internal_secret = "internal-secret-memory-body"

    def fail() -> Any:
        raise RuntimeError(internal_secret)

    monkeypatch.setattr(api.ledger, "get_settings", fail)
    response = api.client.get("/api/settings", headers={"Host": "127.0.0.1"})

    assert_error(response, 500, "internal_error")
    assert internal_secret not in response.text
    assert internal_secret not in caplog.text
