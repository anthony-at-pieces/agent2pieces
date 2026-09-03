"""Command-line entry point for Agent2Pieces."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import webbrowser
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import uvicorn
from platformdirs import user_data_path

from agent2pieces import __version__
from agent2pieces.api import create_app
from agent2pieces.config import (
    SourceRoot,
    coalesce_source_roots,
    default_source_roots,
    resolve_source_root,
    validate_pieces_url,
)
from agent2pieces.ledger import Ledger, LedgerInstanceLock
from agent2pieces.mcp_client import McpCapabilities, PiecesMcpClient
from agent2pieces.models import ScanState, SourceAgent
from agent2pieces.routes import ApiDependencies
from agent2pieces.services import (
    ImportService,
    ReviewService,
    ScanRoot,
    ScanService,
    coalesce_scan_roots,
)

_TRANSIENT_ROOT_PREFIX = "agent2pieces-transient:"
_MCP_CONNECT_TIMEOUT_SECONDS = 3.0


class CliUsageError(ValueError):
    """An argument or saved setting is invalid."""


class CliOperationalError(RuntimeError):
    """A valid command could not complete."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="agent2pieces")
    parser.add_argument("--health-check", action="store_true")
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve")
    serve.add_argument("--pieces-url")
    serve.add_argument("--no-open", action="store_true")
    serve.add_argument("--port", type=_port)
    serve.add_argument("--source-root", action="append", default=[])

    scan = commands.add_parser("scan")
    scan.add_argument("--source-root", action="append", default=[])
    return parser


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


def _data_directory() -> Path:
    configured = os.environ.get("AGENT2PIECES_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(user_data_path("agent2pieces", appauthor=False)).resolve(strict=False)


def _ledger_path() -> Path:
    directory = _data_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    return directory / "agent2pieces.sqlite3"


def _initialize_ledger(ledger: Ledger) -> None:
    """Initialize the ledger and keep persisted memory state private on POSIX."""

    ledger.initialize()
    if os.name == "nt":
        return
    for suffix in ("", "-wal", "-shm"):
        path = ledger.path.with_name(ledger.path.name + suffix)
        if path.exists():
            path.chmod(0o600)


def _validate_pieces_url(value: str) -> str:
    try:
        return validate_pieces_url(value)
    except ValueError as error:
        raise CliUsageError(str(error)) from error


def _parse_source_roots(values: Sequence[str]) -> tuple[SourceRoot, ...]:
    parsed: list[SourceRoot] = []
    for value in values:
        agent_value, separator, path_value = value.partition("=")
        if separator != "=" or agent_value not in {agent.value for agent in SourceAgent}:
            raise CliUsageError("source root must use codex, claude, or hermes")
        if not path_value:
            raise CliUsageError("source root path is required")
        path = Path(path_value)
        try:
            parsed.append(resolve_source_root(SourceAgent(agent_value), path))
        except (OSError, ValueError) as error:
            raise CliUsageError("source root is invalid") from error
    return coalesce_source_roots(parsed)


def _bootstrap_default_roots(ledger: Ledger) -> None:
    settings = ledger.get_settings()
    enabled = {
        SourceAgent.CODEX: settings.codex_enabled,
        SourceAgent.CLAUDE: settings.claude_enabled,
        SourceAgent.HERMES: settings.hermes_enabled,
    }
    for proposed in default_source_roots():
        if not enabled[proposed.agent]:
            continue
        try:
            root = resolve_source_root(
                proposed.agent,
                proposed.lexical_path,
                enabled=True,
                is_default=True,
            )
        except (OSError, ValueError):
            continue
        ledger.add_source_root(
            agent=root.agent,
            lexical_path=str(root.lexical_path),
            resolved_path=str(root.resolved_path),
            enabled=True,
            is_default=True,
        )


def _scan_roots(ledger: Ledger, command_roots: Sequence[SourceRoot]) -> tuple[ScanRoot, ...]:
    roots: list[ScanRoot] = []
    stored = ledger.connection.execute(
        "SELECT root_id, agent, lexical_path, resolved_path FROM source_roots "
        "WHERE enabled = 1 ORDER BY created_at, root_id"
    ).fetchall()
    for row in stored:
        agent = SourceAgent(str(row["agent"]))
        try:
            validated = resolve_source_root(agent, Path(str(row["resolved_path"])))
        except (OSError, ValueError) as error:
            raise CliUsageError("saved source root is invalid") from error
        roots.append(
            ScanRoot(
                root_id=str(row["root_id"]),
                agent=agent,
                path=validated.resolved_path,
            )
        )
    known = {(root.agent, root.path.resolve(strict=False)) for root in roots}
    for root in command_roots:
        key = (root.agent, root.resolved_path)
        if key in known:
            continue
        root_id = ledger.add_source_root(
            agent=root.agent,
            lexical_path=_TRANSIENT_ROOT_PREFIX + str(root.lexical_path),
            resolved_path=str(root.resolved_path),
            enabled=False,
            is_default=False,
        )
        roots.append(ScanRoot(root_id=root_id, agent=root.agent, path=root.resolved_path))
        known.add(key)
    return coalesce_scan_roots(roots)


def _scan_result(ledger: Ledger, scan_id: str) -> dict[str, object]:
    record = ledger.get_scan(scan_id)
    empty = {"discovered": 0, "accepted": 0, "excluded": 0, "quarantined": 0}
    by_agent: dict[str, dict[str, int]] = {}
    dispositions: list[dict[str, str]] = []
    rows = ledger.connection.execute(
        "SELECT agent, source_path, source_key, disposition, reason FROM source_dispositions "
        "WHERE scan_id = ? ORDER BY created_at, disposition_id",
        (scan_id,),
    ).fetchall()
    for row in rows:
        agent = str(row["agent"])
        counts = by_agent.setdefault(agent, dict(empty))
        counts["discovered"] += 1
        field = "quarantined" if str(row["disposition"]) == "quarantined" else "excluded"
        counts[field] += 1
        source = row["source_key"] or Path(str(row["source_path"])).name
        if len(dispositions) < 100:
            dispositions.append(
                {
                    "agent": agent,
                    "source": str(source)[:512],
                    "disposition": str(row["disposition"]),
                    "reason": str(row["reason"]),
                }
            )
    accepted = ledger.connection.execute(
        "SELECT r.agent, COUNT(*) AS count FROM source_observations o "
        "JOIN source_revisions r ON r.revision_id = o.revision_id "
        "WHERE o.scan_id = ? GROUP BY r.agent",
        (scan_id,),
    ).fetchall()
    for row in accepted:
        agent = str(row["agent"])
        counts = by_agent.setdefault(agent, dict(empty))
        count = int(row["count"])
        counts["accepted"] += count
        counts["discovered"] += count
    total = {
        "discovered": record.discovered_count,
        "accepted": record.accepted_count,
        "excluded": record.excluded_count,
        "quarantined": record.quarantine_count,
    }
    return {
        "status": "ok" if record.state is ScanState.COMPLETED else "error",
        "scan_id": record.scan_id,
        "counts": {"total": total, **by_agent},
        "dispositions": dispositions,
        "errors": record.error_count,
    }


async def _run_scan(ledger: Ledger, command_roots: Sequence[SourceRoot]) -> int:
    roots = _scan_roots(ledger, command_roots)
    record = await ScanService(ledger).run_scan(roots)
    _emit_json(_scan_result(ledger, record.scan_id))
    return 0 if record.state is ScanState.COMPLETED else 1


def _health_check() -> int:
    checks = {"database": False, "migrations": False, "static_assets": False}
    error_code: str | None = None
    temporary: Path | None = None
    ledger: Ledger | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix="agent2pieces-health-"))
        ledger = Ledger(temporary / "health.sqlite3")
        try:
            ledger.initialize()
        except FileNotFoundError:
            error_code = "migrations_missing"
        except Exception:
            error_code = "database_failed"
        else:
            checks["database"] = True
            row = ledger.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version IN (1, 2)"
            ).fetchone()
            if row is None or int(row[0]) != 2:
                error_code = "migration_failed"
            else:
                checks["migrations"] = True
                static = files("agent2pieces").joinpath("static")
                checks["static_assets"] = all(
                    static.joinpath(name).is_file()
                    for name in ("index.html", "app.js", "app.css")
                )
                if not checks["static_assets"]:
                    error_code = "static_assets_missing"
    except Exception:
        if error_code is None:
            error_code = "database_failed"
    finally:
        if ledger is not None:
            ledger.close()
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
            except OSError:
                if error_code is None:
                    error_code = "cleanup_failed"
    ok = error_code is None and all(checks.values())
    _emit_json(
        {
            "status": "ok" if ok else "error",
            "version": __version__,
            "checks": checks,
            "error_code": error_code,
        }
    )
    return 0 if ok else 1


def bind_listener(port: int | None) -> socket.socket:
    """Bind and listen on loopback, preserving an OS-selected port without a race."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0 if port is None else port))
        listener.listen(socket.SOMAXCONN)
    except BaseException:
        listener.close()
        raise
    return listener


def maybe_open_browser(url: str, *, no_open: bool) -> None:
    if no_open:
        return
    try:
        webbrowser.open(url)
    except Exception:
        print("agent2pieces: browser_open_failed", file=sys.stderr)


def _blocked_capabilities(base_url: str) -> McpCapabilities:
    return McpCapabilities(
        transport="streamable-http",
        endpoint=base_url.rstrip("/") + "/model_context_protocol/2025-03-26/mcp",
        server_version="",
        import_ready=False,
        search_available=False,
        blocking_error="connection_failed",
        checked_at=datetime.now(UTC).isoformat(),
    )


async def _serve_async(
    *,
    ledger: Ledger,
    listener: socket.socket,
    pieces_url: str,
    pieces_url_source: Literal["cli", "saved"],
    command_roots: Sequence[SourceRoot],
    no_open: bool,
) -> int:
    port = int(listener.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    client = PiecesMcpClient(pieces_url)
    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=_MCP_CONNECT_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            client._capabilities = _blocked_capabilities(pieces_url)
        except Exception:
            client._capabilities = _blocked_capabilities(pieces_url)
        dependencies = ApiDependencies(
            ledger=ledger,
            scan_service=ScanService(ledger),
            review_service=ReviewService(ledger, cast(Any, client)),
            import_service=ImportService(ledger, cast(Any, client)),
            pieces_client=client,
            csrf_token=secrets.token_urlsafe(32),
            effective_mcp_base_url=pieces_url,
            mcp_base_url_source=pieces_url_source,
            command_roots=tuple(
                (root.agent.value, root.resolved_path) for root in command_roots
            ),
        )
        app = create_app(
            dependencies=dependencies,
            listener_origins=(
                url,
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
            ),
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)
        if serving.done() and not server.started:
            await serving
            return 1
        _emit_json(
            {
                "url": url,
                "port": port,
                "pid": os.getpid(),
                "version": __version__,
                "ledger": str(ledger.path.resolve(strict=False)),
                "endpoint": pieces_url,
            }
        )
        await asyncio.to_thread(maybe_open_browser, url, no_open=no_open)
        await serving
        return 0
    finally:
        await client.close()


def _serve(
    *,
    pieces_url: str | None,
    port: int | None,
    command_roots: Sequence[SourceRoot],
    no_open: bool,
) -> int:
    try:
        listener = bind_listener(port)
    except OSError as error:
        raise CliOperationalError("listener_bind_failed") from error
    ledger_path = _ledger_path()
    instance_lock = LedgerInstanceLock(ledger_path)
    ledger = Ledger(ledger_path)
    try:
        instance_lock.acquire()
        _initialize_ledger(ledger)
        _bootstrap_default_roots(ledger)
        pieces_url_source: Literal["cli", "saved"]
        if pieces_url is None:
            effective_pieces_url = _validate_pieces_url(ledger.get_settings().mcp_base_url)
            pieces_url_source = "saved"
        else:
            effective_pieces_url = _validate_pieces_url(pieces_url)
            pieces_url_source = "cli"
        return asyncio.run(
            _serve_async(
                ledger=ledger,
                listener=listener,
                pieces_url=effective_pieces_url,
                pieces_url_source=pieces_url_source,
                command_roots=command_roots,
                no_open=no_open,
            )
        )
    except KeyboardInterrupt:
        return 130
    finally:
        listener.close()
        ledger.close()
        instance_lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        namespace = _parser().parse_args(arguments)
        if namespace.health_check:
            if namespace.command is not None:
                raise CliUsageError("health check cannot be combined with a command")
            return _health_check()
        if namespace.command is None:
            raise CliUsageError("a command is required")
        command_roots = _parse_source_roots(namespace.source_root)
        if namespace.command == "scan":
            ledger_path = _ledger_path()
            instance_lock = LedgerInstanceLock(ledger_path)
            ledger = Ledger(ledger_path)
            try:
                instance_lock.acquire()
                _initialize_ledger(ledger)
                _bootstrap_default_roots(ledger)
                return asyncio.run(_run_scan(ledger, command_roots))
            finally:
                ledger.close()
                instance_lock.close()
        pieces_url = (
            _validate_pieces_url(namespace.pieces_url)
            if namespace.pieces_url is not None
            else None
        )
        return _serve(
            pieces_url=pieces_url,
            port=namespace.port,
            command_roots=command_roots,
            no_open=namespace.no_open,
        )
    except CliUsageError:
        return 2
    except (CliOperationalError, OSError, RuntimeError, ValueError):
        print("agent2pieces: operation_failed", file=sys.stderr)
        return 1


def entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()


__all__ = ["bind_listener", "entrypoint", "main", "maybe_open_browser"]
