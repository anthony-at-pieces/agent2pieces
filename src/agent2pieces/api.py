"""FastAPI application assembly and bundled review interface delivery."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from typing import Final

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agent2pieces import __version__
from agent2pieces.routes import ApiDependencies, create_api_router
from agent2pieces.security import LoopbackSecurityConfig, install_loopback_security

_STATIC_PACKAGE: Final = "agent2pieces"


def _read_asset(name: str) -> bytes:
    return files(_STATIC_PACKAGE).joinpath("static").joinpath(name).read_bytes()


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def create_app(
    *,
    dependencies: ApiDependencies,
    listener_origins: tuple[str, ...],
) -> FastAPI:
    """Build one loopback application with the approved API and local assets."""

    index_template = _read_asset("index.html").decode("utf-8")
    script = _read_asset("app.js")
    style = _read_asset("app.css")
    script_path = f"/assets/app.{_fingerprint(script)}.js"
    style_path = f"/assets/app.{_fingerprint(style)}.css"
    index = (
        index_template.replace("__APP_SCRIPT__", script_path)
        .replace("__APP_STYLE__", style_path)
        .encode("utf-8")
    )

    app = FastAPI(
        title="Agent2Pieces",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api_router = create_api_router(dependencies)
    app.router.routes.extend(api_router.routes)

    @app.get("/", response_class=HTMLResponse)
    async def review_interface() -> Response:
        return Response(content=index, media_type="text/html; charset=utf-8")

    @app.get(script_path)
    async def bundled_script() -> Response:
        return Response(content=script, media_type="text/javascript; charset=utf-8")

    @app.get(style_path)
    async def bundled_style() -> Response:
        return Response(content=style, media_type="text/css; charset=utf-8")

    @app.get("/health")
    async def health() -> JSONResponse:
        dependencies.ledger.connection.execute("SELECT 1").fetchone()
        capabilities = dependencies.pieces_client.capabilities
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "ledger": "ok",
                "assets": "ok",
                "mcp": {
                    "status": "ready" if capabilities.import_ready else "blocked",
                    "transport": capabilities.transport,
                    "create_pieces_memory": capabilities.import_ready,
                    "annotations_full_text_search": capabilities.search_available,
                },
            }
        )

    install_loopback_security(
        app,
        LoopbackSecurityConfig(
            csrf_token=dependencies.csrf_token,
            listener_origins=listener_origins,
        ),
    )
    return app


__all__ = ["create_app"]
