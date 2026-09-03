"""Pieces MCP capability discovery, duplicate search, and single-write adapter."""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from agent2pieces.config import validate_pieces_url
from agent2pieces.dedupe import (
    DedupeCandidate,
    DuplicateClassification,
    classify_scores,
    cosine_similarity,
    five_token_shingle_jaccard,
    prepare_comparison_text,
    title_token_jaccard,
)
from agent2pieces.models import IMPORT_ID_PATTERN, SourceAgent
from agent2pieces.normalization import render_dispatch_summary, visible_import_marker

_WRITE_TOOL = "create_pieces_memory"
_SEARCH_TOOL = "annotations_full_text_search"
_REQUIRED_WRITE_FIELDS = frozenset({"summary_description", "summary"})
_OPTIONAL_WRITE_FIELDS = frozenset({"connected_client", "externalLinks", "project", "files"})
_MAX_ANNOTATIONS = 50
_DEFAULT_INITIALIZE_TIMEOUT_SECONDS = 5.0
_DEFAULT_LIST_TIMEOUT_SECONDS = 5.0
_DEFAULT_SEARCH_TIMEOUT_SECONDS = 10.0
_DEFAULT_WRITE_TIMEOUT_SECONDS = 30.0


class McpConnectionError(RuntimeError):
    """The client could not establish or use an initialized MCP session."""


class McpCapabilityError(RuntimeError):
    """The live Pieces tool surface cannot accept the requested operation."""


class McpWriteError(RuntimeError):
    """A call-initiated write did not return a proven success."""

    def __init__(self, message: str, *, error_kind: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.ambiguous = True
        self.retryable = False


class _Session(Protocol):
    async def initialize(self) -> Any: ...

    async def list_tools(self) -> Any: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any: ...

    async def close(self) -> None: ...


class _Connector(Protocol):
    async def __call__(self, *, transport: str, endpoint: str) -> _Session: ...


class _Sleep(Protocol):
    async def __call__(self, delay: float) -> None: ...


class _BeforeCall(Protocol):
    async def __call__(self) -> None: ...


@dataclass(frozen=True, slots=True)
class McpCapabilities:
    transport: Literal["streamable-http", "sse"]
    endpoint: str
    server_version: str
    import_ready: bool
    search_available: bool
    blocking_error: str | None
    checked_at: str


@dataclass(frozen=True, slots=True)
class DispatchPayload:
    title: str
    markdown_body: str
    external_links: tuple[str, ...]
    source_agent: SourceAgent
    source_path: Path
    import_id: str


@dataclass(frozen=True, slots=True)
class PathMapping:
    local_root: Path
    host_root: str
    project: str


@dataclass(frozen=True, slots=True)
class WriteResult:
    memory_id: str


@dataclass(frozen=True, slots=True)
class MarkerSearchResult:
    outcome: str
    coverage: str
    parent_memory_ids: tuple[str, ...] = ()
    annotation_ids: tuple[str, ...] = ()
    unique_annotations: int = 0
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteDuplicateMatch:
    annotation_id: str
    parent_memory_id: str | None
    classification: str
    rule_id: str
    cosine: float
    body_shingle_jaccard: float
    title_jaccard: float


@dataclass(frozen=True, slots=True)
class RemoteDuplicateResult:
    coverage: str
    matches: tuple[RemoteDuplicateMatch, ...]
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class _AnnotationPage:
    records: tuple[dict[str, Any], ...]
    complete: bool
    has_more: bool
    next_cursor: str | None
    truncated: bool


class _SdkSession:
    """Own the SDK transport and session contexts behind the local protocol."""

    def __init__(self, session: ClientSession, stack: AsyncExitStack) -> None:
        self._session = session
        self._stack = stack

    async def initialize(self) -> Any:
        return await self._session.initialize()

    async def list_tools(self) -> Any:
        return await self._session.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._session.call_tool(name, arguments, **kwargs)

    async def close(self) -> None:
        await self._stack.aclose()


async def _official_connector(*, transport: str, endpoint: str) -> _Session:
    """Open one session through the public MCP SDK transport clients."""

    stack = AsyncExitStack()
    try:
        if transport == "streamable-http":
            read_stream, write_stream = await stack.enter_async_context(
                streamable_http_client(endpoint)
            )
        elif transport == "sse":
            read_stream, write_stream = await stack.enter_async_context(sse_client(endpoint))
        else:
            raise ValueError(f"unsupported MCP transport: {transport}")
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
    except BaseException:
        await stack.aclose()
        raise
    return _SdkSession(session, stack)


def _base_url(value: str) -> str:
    return validate_pieces_url(value)


def _server_version(initialize_result: Any) -> str:
    server_info = getattr(initialize_result, "serverInfo", None)
    if server_info is None:
        server_info = getattr(initialize_result, "server_info", None)
    version = getattr(server_info, "version", "")
    return str(version) if version is not None else ""


def _tool_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    return cast(dict[str, Any], schema) if isinstance(schema, dict) else {}


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", ""))


def _error_kind(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, (ConnectionError, OSError)):
        return "connection_lost"
    return "tool_error"


def _result_payload(result: Any) -> dict[str, Any]:
    if bool(getattr(result, "isError", False) or getattr(result, "is_error", False)):
        raise ValueError("MCP tool returned an error result")
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", ()):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError("MCP tool response did not contain an object")


def _annotation_id(record: dict[str, Any]) -> str | None:
    annotation = record.get("annotation")
    if not isinstance(annotation, dict):
        return None
    value = annotation.get("id")
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def _annotation_text(record: dict[str, Any]) -> str | None:
    annotation = record.get("annotation")
    if not isinstance(annotation, dict):
        return None
    value = annotation.get("text")
    return value if isinstance(value, str) else None


def _nested_scalar(value: Any, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, (str, int, float)) and not isinstance(current, bool):
        rendered = str(current).strip()
        return rendered or None
    return None


def _parent_id(record: dict[str, Any]) -> str | None:
    paths = (
        ("annotation", "summary", "id"),
        ("annotation", "summary", "reference", "id"),
        ("annotation", "summary_id"),
        ("summary", "id"),
        ("summary_id",),
        ("memory", "id"),
        ("memory_id",),
    )
    for path in paths:
        value = _nested_scalar(record, *path)
        if value is not None:
            return value
    return None


def _has_exact_marker_line(text: str, marker: str) -> bool:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return any(line == marker for line in normalized.split("\n"))


def _valid_link(value: str) -> bool:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


class PiecesMcpClient:
    """Maintain one initialized Pieces session and expose bounded MCP operations."""

    def __init__(
        self,
        base_url: str,
        *,
        connector: _Connector | None = None,
        sleep: _Sleep = asyncio.sleep,
        search_retry_delays: tuple[float, ...] = (0.25, 0.5),
        initialize_timeout_seconds: float = _DEFAULT_INITIALIZE_TIMEOUT_SECONDS,
        list_timeout_seconds: float = _DEFAULT_LIST_TIMEOUT_SECONDS,
        search_timeout_seconds: float = _DEFAULT_SEARCH_TIMEOUT_SECONDS,
        write_timeout_seconds: float = _DEFAULT_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _base_url(base_url)
        self._connector = connector or _official_connector
        self._sleep = sleep
        self._search_retry_delays = search_retry_delays
        self._initialize_timeout_seconds = _validated_timeout(
            initialize_timeout_seconds, "initialize"
        )
        self._list_timeout_seconds = _validated_timeout(list_timeout_seconds, "list")
        self._search_timeout_seconds = _validated_timeout(search_timeout_seconds, "search")
        self._write_timeout_seconds = _validated_timeout(write_timeout_seconds, "write")
        self._session: _Session | None = None
        self._capabilities: McpCapabilities | None = None
        self._write_properties: frozenset[str] = frozenset()
        self._search_cursor_supported = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def capabilities(self) -> McpCapabilities:
        if self._capabilities is None:
            raise McpConnectionError("Pieces MCP is not connected")
        return self._capabilities

    async def __aenter__(self) -> PiecesMcpClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> McpCapabilities:
        async with self._lifecycle_lock:
            return await self._connect_unlocked()

    async def _connect_unlocked(self) -> McpCapabilities:
        if self._session is not None:
            return self.capabilities

        session: _Session | None = None
        initialize_result: Any = None
        selected_transport: Literal["streamable-http", "sse"] = "streamable-http"
        selected_endpoint = self._endpoint("streamable-http")
        try:
            session = await self._connector(
                transport="streamable-http", endpoint=selected_endpoint
            )
            async with asyncio.timeout(self._initialize_timeout_seconds):
                initialize_result = await session.initialize()
        except asyncio.CancelledError:
            if session is not None:
                await self._close_failed_session(session)
            raise
        except Exception:
            if session is not None:
                await self._close_failed_session(session)
                session = None
            selected_transport = "sse"
            selected_endpoint = self._endpoint("sse")
            try:
                session = await self._connector(transport="sse", endpoint=selected_endpoint)
                async with asyncio.timeout(self._initialize_timeout_seconds):
                    initialize_result = await session.initialize()
            except asyncio.CancelledError:
                if session is not None:
                    await self._close_failed_session(session)
                raise
            except Exception as sse_error:
                if session is not None:
                    await self._close_failed_session(session)
                raise McpConnectionError("Pieces MCP initialization failed") from sse_error

        if session is None:
            raise McpConnectionError("Pieces MCP initialization returned no session")
        try:
            async with asyncio.timeout(self._list_timeout_seconds):
                listed = await session.list_tools()
            tools = tuple(getattr(listed, "tools", ()))
            self._discover_tools(tools)
        except asyncio.CancelledError:
            await self._close_failed_session(session)
            raise
        except Exception as error:
            await self._close_failed_session(session)
            raise McpConnectionError("Pieces MCP tool discovery failed") from error

        write_tool = next((tool for tool in tools if _tool_name(tool) == _WRITE_TOOL), None)
        search_tool = next((tool for tool in tools if _tool_name(tool) == _SEARCH_TOOL), None)
        blocking_error: str | None = None
        if write_tool is None:
            blocking_error = "missing_write_tool"
        else:
            write_schema = _tool_schema(write_tool)
            required = write_schema.get("required", ())
            required_fields = {
                str(field) for field in required if isinstance(field, str)
            } if isinstance(required, list) else set()
            unknown_required = sorted(required_fields - _REQUIRED_WRITE_FIELDS)
            if unknown_required:
                blocking_error = "unknown_required_fields:" + ",".join(unknown_required)
            elif not _REQUIRED_WRITE_FIELDS.issubset(self._write_properties):
                missing = sorted(_REQUIRED_WRITE_FIELDS - self._write_properties)
                blocking_error = "missing_write_fields:" + ",".join(missing)

        self._session = session
        self._capabilities = McpCapabilities(
            transport=selected_transport,
            endpoint=selected_endpoint,
            server_version=_server_version(initialize_result),
            import_ready=blocking_error is None,
            search_available=search_tool is not None,
            blocking_error=blocking_error,
            checked_at=datetime.now(UTC).isoformat(),
        )
        return self._capabilities

    async def close(self) -> None:
        async with self._lifecycle_lock:
            session = self._session
            if session is not None:
                await session.close()
            self._session = None
            self._capabilities = None
            self._write_properties = frozenset()
            self._search_cursor_supported = False

    @staticmethod
    async def _close_failed_session(session: _Session) -> None:
        """Best-effort cleanup that does not hide a connection-stage failure."""

        try:
            await session.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _endpoint(self, transport: str) -> str:
        suffix = (
            "/model_context_protocol/2025-03-26/mcp"
            if transport == "streamable-http"
            else "/model_context_protocol/2024-11-05/sse"
        )
        return self._base_url + suffix

    def _discover_tools(self, tools: tuple[Any, ...]) -> None:
        write_tool = next((tool for tool in tools if _tool_name(tool) == _WRITE_TOOL), None)
        if write_tool is None:
            self._write_properties = frozenset()
        else:
            properties = _tool_schema(write_tool).get("properties", {})
            self._write_properties = (
                frozenset(properties) if isinstance(properties, dict) else frozenset()
            )
        search_tool = next((tool for tool in tools if _tool_name(tool) == _SEARCH_TOOL), None)
        search_properties = _tool_schema(search_tool).get("properties", {}) if search_tool else {}
        self._search_cursor_supported = (
            isinstance(search_properties, dict) and "cursor" in search_properties
        )

    def build_write_arguments(
        self,
        payload: DispatchPayload,
        *,
        mappings: tuple[PathMapping, ...] = (),
    ) -> dict[str, Any]:
        capabilities = self.capabilities
        if not capabilities.import_ready:
            raise McpCapabilityError(
                "Pieces create_pieces_memory tool is unavailable or incompatible"
            )
        if not payload.title.strip() or not payload.markdown_body.strip():
            raise ValueError("write title and body must not be empty")
        if IMPORT_ID_PATTERN.fullmatch(payload.import_id) is None:
            raise ValueError("import_id must be a 26-character lowercase base32 digest")
        if any(not _valid_link(link) for link in payload.external_links):
            raise ValueError("external links must be credential-free HTTP(S) URLs")

        mapping = self._select_mapping(payload.source_path, mappings)
        mapped_project = mapping.project if mapping is not None else None
        summary = render_dispatch_summary(
            markdown_body=payload.markdown_body,
            source_agent=payload.source_agent,
            import_id=payload.import_id,
            mapped_project=mapped_project,
        )
        arguments: dict[str, Any] = {
            "summary_description": payload.title,
            "summary": summary,
        }
        supported_optional = self._write_properties & _OPTIONAL_WRITE_FIELDS
        if "connected_client" in supported_optional:
            arguments["connected_client"] = "Agent2Pieces"
        if "externalLinks" in supported_optional and payload.external_links:
            arguments["externalLinks"] = list(payload.external_links)
        if mapping is not None:
            if "project" in supported_optional:
                arguments["project"] = mapping.project
            if "files" in supported_optional:
                relative = payload.source_path.resolve(strict=False).relative_to(
                    mapping.local_root.resolve(strict=False)
                )
                host_root = mapping.host_root.rstrip("/")
                arguments["files"] = [f"{host_root}/{relative.as_posix()}"]
        return arguments

    @staticmethod
    def _select_mapping(
        source_path: Path,
        mappings: tuple[PathMapping, ...],
    ) -> PathMapping | None:
        source = source_path.resolve(strict=False)
        matches: list[tuple[int, PathMapping]] = []
        for mapping in mappings:
            root = mapping.local_root.resolve(strict=False)
            try:
                source.relative_to(root)
            except ValueError:
                continue
            if not mapping.host_root.strip() or not mapping.project.strip():
                continue
            matches.append((len(root.parts), mapping))
        return max(matches, key=lambda item: item[0])[1] if matches else None

    async def create_memory(
        self,
        arguments: dict[str, Any],
        *,
        before_call: _BeforeCall,
    ) -> WriteResult:
        session = self._required_session()
        if not self.capabilities.import_ready:
            raise McpCapabilityError("Pieces create_pieces_memory tool is unavailable")
        await before_call()
        try:
            async with asyncio.timeout(self._write_timeout_seconds):
                result = await session.call_tool(_WRITE_TOOL, arguments)
            payload = _result_payload(result)
            memory_id = payload.get("id")
            if not isinstance(memory_id, (str, int)) or not str(memory_id).strip():
                raise ValueError("missing memory id")
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            kind = (
                "malformed_response"
                if isinstance(error, (ValueError, json.JSONDecodeError))
                else _error_kind(error)
            )
            raise McpWriteError(
                "Pieces write outcome is ambiguous and was not retried",
                error_kind=kind,
            ) from error
        return WriteResult(memory_id=str(memory_id))

    async def search_marker(self, import_id: str) -> MarkerSearchResult:
        if IMPORT_ID_PATTERN.fullmatch(import_id) is None:
            raise ValueError("import_id must be a 26-character lowercase base32 digest")
        if not self.capabilities.search_available:
            return MarkerSearchResult(outcome="unavailable", coverage="local-only")
        marker = visible_import_marker(import_id)
        try:
            records, complete = await self._search_records(marker)
        except Exception as error:
            return MarkerSearchResult(
                outcome="search_error",
                coverage="error",
                error_kind=_error_kind(error),
            )
        if not complete:
            return MarkerSearchResult(
                outcome="truncated",
                coverage="truncated",
                unique_annotations=len(records),
            )

        matching: list[tuple[str, str | None]] = []
        for record in records:
            annotation_id = _annotation_id(record)
            text = _annotation_text(record)
            if annotation_id is None or text is None or not _has_exact_marker_line(text, marker):
                continue
            matching.append((annotation_id, _parent_id(record)))
        annotation_ids = tuple(sorted(annotation_id for annotation_id, _ in matching))
        parents = tuple(sorted({parent for _, parent in matching if parent is not None}))
        if any(parent is None for _, parent in matching):
            outcome = "parent_unknown"
        elif len(parents) > 1:
            outcome = "multiple_parents"
        elif len(parents) == 1:
            outcome = "one_parent"
        else:
            outcome = "absent"
        return MarkerSearchResult(
            outcome=outcome,
            coverage="complete",
            parent_memory_ids=parents,
            annotation_ids=annotation_ids,
            unique_annotations=len(records),
        )

    async def search_duplicates(self, candidate: DedupeCandidate) -> RemoteDuplicateResult:
        if not self.capabilities.search_available:
            return RemoteDuplicateResult(coverage="local-only", matches=())
        query = candidate.title
        try:
            records, complete = await self._search_records(query)
        except Exception as error:
            return RemoteDuplicateResult(
                coverage="error", matches=(), error_kind=_error_kind(error)
            )

        candidate_tokens = prepare_comparison_text(candidate.markdown_body)
        matches: list[RemoteDuplicateMatch] = []
        for record in records:
            annotation_id = _annotation_id(record)
            text = _annotation_text(record)
            if annotation_id is None or text is None:
                continue
            remote_tokens = prepare_comparison_text(text)
            scores = classify_scores(
                payload_hash_equal=False,
                import_marker_equal=False,
                cosine=cosine_similarity(candidate_tokens, remote_tokens),
                body_shingle_jaccard=five_token_shingle_jaccard(
                    candidate_tokens, remote_tokens
                ),
                title_jaccard=title_token_jaccard(candidate.title, text),
            )
            if scores.classification is DuplicateClassification.DISTINCT:
                continue
            matches.append(
                RemoteDuplicateMatch(
                    annotation_id=annotation_id,
                    parent_memory_id=_parent_id(record),
                    classification=scores.classification.value,
                    rule_id=scores.rule_id,
                    cosine=scores.cosine,
                    body_shingle_jaccard=scores.body_shingle_jaccard,
                    title_jaccard=scores.title_jaccard,
                )
            )
        return RemoteDuplicateResult(
            coverage="complete" if complete else "truncated",
            matches=tuple(sorted(matches, key=lambda match: match.annotation_id)),
        )

    async def _search_records(self, query: str) -> tuple[tuple[dict[str, Any], ...], bool]:
        session = self._required_session()
        records_by_id: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        complete = False
        while len(records_by_id) < _MAX_ANNOTATIONS:
            arguments: dict[str, Any] = {"query": query, "limit": _MAX_ANNOTATIONS}
            if cursor is not None:
                arguments["cursor"] = cursor
            page = await self._call_search_with_retries(session, arguments)
            page_overflow = False
            for record in page.records:
                annotation_id = _annotation_id(record)
                if annotation_id is None or annotation_id in records_by_id:
                    continue
                if len(records_by_id) >= _MAX_ANNOTATIONS:
                    page_overflow = True
                    break
                records_by_id[annotation_id] = record
            if page.truncated or page_overflow:
                complete = False
                break
            if not page.has_more:
                complete = page.complete
                break
            if not self._search_cursor_supported or page.next_cursor is None:
                complete = False
                break
            if page.next_cursor in seen_cursors:
                complete = False
                break
            if len(records_by_id) >= _MAX_ANNOTATIONS:
                complete = False
                break
            cursor = page.next_cursor
            seen_cursors.add(cursor)
        return tuple(records_by_id.values()), complete

    async def _call_search_with_retries(
        self,
        session: _Session,
        arguments: dict[str, Any],
    ) -> _AnnotationPage:
        attempts = len(self._search_retry_delays) + 1
        for attempt in range(attempts):
            try:
                async with asyncio.timeout(self._search_timeout_seconds):
                    result = await session.call_tool(_SEARCH_TOOL, arguments)
                payload = _result_payload(result)
                annotations = payload.get("annotations")
                if not isinstance(annotations, list) or any(
                    not isinstance(record, dict) for record in annotations
                ):
                    raise ValueError("annotation search response is malformed")
                has_more_value = payload.get("has_more")
                truncated_value = payload.get("truncated")
                next_cursor_value = payload.get("next_cursor")
                complete = isinstance(has_more_value, bool) and isinstance(
                    truncated_value, bool
                )
                return _AnnotationPage(
                    records=tuple(cast(dict[str, Any], record) for record in annotations),
                    complete=complete,
                    has_more=has_more_value is True,
                    next_cursor=(
                        next_cursor_value
                        if isinstance(next_cursor_value, str) and next_cursor_value
                        else None
                    ),
                    truncated=truncated_value is True,
                )
            except (TimeoutError, ConnectionError, OSError) as error:
                if attempt >= len(self._search_retry_delays):
                    raise error
                await self._sleep(self._search_retry_delays[attempt])
        raise AssertionError("unreachable search retry state")

    def _required_session(self) -> _Session:
        if self._session is None:
            raise McpConnectionError("Pieces MCP is not connected")
        return self._session


def _validated_timeout(value: float, operation: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{operation} timeout must be a positive finite number")
    return value


__all__ = [
    "DispatchPayload",
    "MarkerSearchResult",
    "McpCapabilities",
    "McpCapabilityError",
    "McpConnectionError",
    "McpWriteError",
    "PathMapping",
    "PiecesMcpClient",
    "RemoteDuplicateMatch",
    "RemoteDuplicateResult",
    "WriteResult",
]
