"""Deterministic in-process MCP test double for the Pieces client boundary."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    transport: str


def write_tool(
    *,
    optional: tuple[str, ...] = ("connected_client", "externalLinks", "project", "files"),
    extra_required: tuple[str, ...] = (),
) -> Tool:
    properties: dict[str, Any] = {
        "summary_description": {"type": "string"},
        "summary": {"type": "string"},
    }
    properties.update({field: {} for field in optional})
    properties.update({field: {} for field in extra_required})
    return Tool(
        name="create_pieces_memory",
        description="Create one Pieces memory",
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": ["summary_description", "summary", *extra_required],
        },
    )


def search_tool(*, cursor: bool = True) -> Tool:
    properties: dict[str, Any] = {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
    }
    if cursor:
        properties["cursor"] = {"type": "string"}
    return Tool(
        name="annotations_full_text_search",
        description="Search Pieces annotations",
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": ["query"],
        },
    )


def tool_result(
    structured: Any,
    *,
    is_error: bool = False,
    as_text: bool = False,
) -> CallToolResult:
    if as_text:
        import json

        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(structured))],
            isError=is_error,
        )
    return CallToolResult(content=[], structuredContent=structured, isError=is_error)


def annotation_page(
    annotations: list[dict[str, Any]],
    *,
    has_more: bool | None = False,
    next_cursor: str | None = None,
    truncated: bool | None = False,
    as_text: bool = False,
) -> CallToolResult:
    payload: dict[str, Any] = {"annotations": annotations}
    if has_more is not None:
        payload["has_more"] = has_more
    if next_cursor is not None:
        payload["next_cursor"] = next_cursor
    if truncated is not None:
        payload["truncated"] = truncated
    return tool_result(payload, as_text=as_text)


class FakeMcpServer:
    """Small official-model-compatible fake with queued tool outcomes."""

    def __init__(self, tools: list[Tool]) -> None:
        self.tools = tools
        self.events: list[str] = []
        self.endpoints: list[tuple[str, str]] = []
        self.tool_calls: list[ToolCall] = []
        self.initialize_failures: dict[str, BaseException] = {}
        self.hanging_initializers: set[str] = set()
        self.list_tools_failure: BaseException | None = None
        self.hang_list_tools = False
        self.hanging_tools: set[str] = set()
        self._outcomes: dict[str, deque[CallToolResult | BaseException]] = defaultdict(deque)
        self.sessions: list[FakeSession] = []

    async def connector(self, *, transport: str, endpoint: str) -> FakeSession:
        self.events.append(f"connect:{transport}")
        self.endpoints.append((transport, endpoint))
        session = FakeSession(self, transport)
        self.sessions.append(session)
        return session

    def queue(self, tool_name: str, *outcomes: CallToolResult | BaseException) -> None:
        self._outcomes[tool_name].extend(outcomes)

    def next_outcome(self, tool_name: str) -> CallToolResult:
        if not self._outcomes[tool_name]:
            return tool_result({"id": "memory-default"})
        outcome = self._outcomes[tool_name].popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSession:
    def __init__(self, server: FakeMcpServer, transport: str) -> None:
        self.server = server
        self.transport = transport
        self.closed = False

    async def initialize(self) -> SimpleNamespace:
        self.server.events.append(f"initialize:{self.transport}")
        if self.transport in self.server.hanging_initializers:
            await asyncio.Event().wait()
        failure = self.server.initialize_failures.get(self.transport)
        if failure is not None:
            raise failure
        return SimpleNamespace(serverInfo=SimpleNamespace(name="fake-pieces", version="9.8.7"))

    async def list_tools(self) -> ListToolsResult:
        self.server.events.append(f"tools/list:{self.transport}")
        if self.server.hang_list_tools:
            await asyncio.Event().wait()
        if self.server.list_tools_failure is not None:
            raise self.server.list_tools_failure
        return ListToolsResult(tools=self.server.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> CallToolResult:
        self.server.events.append(f"tools/call:{name}:{self.transport}")
        self.server.tool_calls.append(ToolCall(name, arguments or {}, self.transport))
        if name in self.server.hanging_tools:
            await asyncio.Event().wait()
        return self.server.next_outcome(name)

    async def close(self) -> None:
        self.server.events.append(f"close:{self.transport}")
        self.closed = True
