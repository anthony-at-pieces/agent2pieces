"""Loopback-only HTTP security and bounded JSON error handling."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

LOGGER = logging.getLogger(__name__)

_MUTATION_METHODS = frozenset({"POST", "PATCH", "PUT"})
_FORWARDING_HEADERS = frozenset(
    {
        "forwarded",
        "x-real-ip",
        "x-url-scheme",
    }
)
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


@dataclass(frozen=True, slots=True)
class LoopbackSecurityConfig:
    """Per-process security inputs for the local web listener."""

    csrf_token: str
    listener_origins: tuple[str, ...]
    max_json_body_bytes: int = 262_144


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message[:512],
                "details": {},
            }
        },
    )


def _loopback_host(host_header: str) -> tuple[str, int | None] | None:
    if not host_header or any(character.isspace() for character in host_header):
        return None
    try:
        parsed = urlsplit(f"//{host_header}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or hostname is None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized = hostname.casefold()
    if normalized == "localhost":
        return normalized, port
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return None
    if not address.is_loopback or normalized not in {"127.0.0.1", "::1"}:
        return None
    return normalized, port


def _origin_matches_request(value: str, request_host: tuple[str, int | None]) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        return False
    normalized_port = port if port is not None else 80
    request_port = request_host[1] if request_host[1] is not None else 80
    return parsed.hostname.casefold() == request_host[0] and normalized_port == request_port


def _source_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is not None:
        return origin
    referer = request.headers.get("referer")
    if referer is None:
        return None
    parsed = urlsplit(referer)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _add_safe_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"


def _has_forwarding_header(request: Request) -> bool:
    return any(
        name in _FORWARDING_HEADERS
        or name.startswith("x-forwarded-")
        or name.startswith("x-original-")
        for name in request.headers
    )


async def _read_bounded_body(request: Request, maximum: int) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            return None
        body.extend(chunk)
    value = bytes(body)
    request._body = value  # Starlette replays this cached body to the downstream app.
    return value


def install_loopback_security(app: FastAPI, config: LoopbackSecurityConfig) -> None:
    """Install host, same-origin, CSRF, body-bound, and safe-error policies."""

    allowed_hosts = {
        host
        for origin in config.listener_origins
        if (host := _loopback_host(urlsplit(origin).netloc)) is not None
    }

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, error: RequestValidationError) -> JSONResponse:
        del request, error
        return _error(422, "validation_error", "The request did not match the API contract.")

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, error: HTTPException) -> JSONResponse:
        del request
        if isinstance(error.detail, dict):
            raw_code = error.detail.get("code")
            raw_message = error.detail.get("message")
            if isinstance(raw_code, str) and isinstance(raw_message, str):
                return _error(error.status_code, raw_code, raw_message)
        code = {
            404: "not_found",
            405: "method_not_allowed",
        }.get(error.status_code, "request_error")
        detail = error.detail if isinstance(error.detail, str) else "The request failed."
        return _error(error.status_code, code, detail)

    @app.middleware("http")
    async def loopback_security(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response: Response
        host = _loopback_host(request.headers.get("host", ""))
        if host is None or (
            host not in allowed_hosts and (host[0], None) not in allowed_hosts
        ):
            response = _error(400, "invalid_host", "The Host header is not loopback.")
            _add_safe_headers(response)
            return response
        if _has_forwarding_header(request):
            response = _error(
                400,
                "forwarded_request_rejected",
                "Forwarding headers are not accepted by the local service.",
            )
            _add_safe_headers(response)
            return response
        if request.method in _MUTATION_METHODS:
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), config.csrf_token):
                response = _error(403, "csrf_rejected", "The CSRF token was rejected.")
                _add_safe_headers(response)
                return response
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site not in {"same-origin", "none"}:
                response = _error(
                    403,
                    "fetch_site_rejected",
                    "The request did not originate from this application.",
                )
                _add_safe_headers(response)
                return response
            source_origin = _source_origin(request)
            if source_origin is not None and not _origin_matches_request(source_origin, host):
                response = _error(403, "origin_rejected", "The request origin was rejected.")
                _add_safe_headers(response)
                return response
            if source_origin is None and fetch_site != "none":
                response = _error(403, "origin_rejected", "The request origin was rejected.")
                _add_safe_headers(response)
                return response
            content_type = request.headers.get("content-type", "").partition(";")[0].strip()
            if content_type.casefold() != "application/json":
                response = _error(
                    415,
                    "unsupported_media_type",
                    "State-changing requests require application/json.",
                )
                _add_safe_headers(response)
                return response
            content_length = request.headers.get("content-length")
            try:
                declared_length = int(content_length) if content_length is not None else 0
            except ValueError:
                declared_length = config.max_json_body_bytes + 1
            if declared_length < 0:
                response = _error(400, "invalid_content_length", "Content-Length is invalid.")
                _add_safe_headers(response)
                return response
            if declared_length > config.max_json_body_bytes:
                response = _error(413, "request_too_large", "The request body is too large.")
                _add_safe_headers(response)
                return response
            body = await _read_bounded_body(request, config.max_json_body_bytes)
            if body is None:
                response = _error(413, "request_too_large", "The request body is too large.")
                _add_safe_headers(response)
                return response
        try:
            response = await call_next(request)
        except Exception as error:
            LOGGER.error("Unhandled request failure (%s)", type(error).__name__)
            response = _error(500, "internal_error", "An internal request failure occurred.")
        if response.status_code == 405:
            response = _error(405, "method_not_allowed", "The method is not allowed.")
        _add_safe_headers(response)
        return response


__all__ = ["LoopbackSecurityConfig", "install_loopback_security"]
