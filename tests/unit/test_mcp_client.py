"""Contract tests for the isolated Pieces MCP adapter."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fixtures.mcp_server import (
    FakeMcpServer,
    annotation_page,
    search_tool,
    tool_result,
    write_tool,
)

from agent2pieces.dedupe import DedupeCandidate
from agent2pieces.models import SourceAgent

IMPORT_ID = "abcdefghijklmnopqrstuvwxyz"
MARKER = f"Agent2Pieces Import ID: {IMPORT_ID}"


@pytest.fixture
def mcp_api() -> ModuleType:
    return importlib.import_module("agent2pieces.mcp_client")


async def connected_client(
    mcp_api: ModuleType,
    server: FakeMcpServer,
    **kwargs: Any,
) -> Any:
    client = mcp_api.PiecesMcpClient(
        "http://pieces.example.test:39300/",
        connector=server.connector,
        **kwargs,
    )
    await client.connect()
    return client


def annotation(
    annotation_id: str,
    text: str,
    *,
    parent_id: str | None = "memory-1",
    **record_fields: Any,
) -> dict[str, Any]:
    annotation_value: dict[str, Any] = {"id": annotation_id, "text": text}
    if parent_id is not None:
        annotation_value["summary"] = {"id": parent_id}
    return {"annotation": annotation_value, **record_fields}


@pytest.mark.asyncio
async def test_connect_prefers_streamable_http_and_maintains_one_session(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    client = await connected_client(mcp_api, server)

    same_capabilities = await client.connect()

    assert server.endpoints == [
        (
            "streamable-http",
            "http://pieces.example.test:39300/model_context_protocol/2025-03-26/mcp",
        )
    ]
    assert len(server.sessions) == 1
    assert same_capabilities.transport == "streamable-http"
    assert same_capabilities.server_version == "9.8.7"
    assert same_capabilities.import_ready is True
    assert same_capabilities.search_available is True
    assert server.events.count("tools/list:streamable-http") == 1

    await client.close()
    assert server.sessions[0].closed is True


@pytest.mark.asyncio
async def test_connect_falls_back_to_sse_only_when_initialization_fails(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool()])
    server.initialize_failures["streamable-http"] = ConnectionError("unsupported endpoint")

    client = await connected_client(mcp_api, server)

    assert server.endpoints == [
        (
            "streamable-http",
            "http://pieces.example.test:39300/model_context_protocol/2025-03-26/mcp",
        ),
        ("sse", "http://pieces.example.test:39300/model_context_protocol/2024-11-05/sse"),
    ]
    assert client.capabilities.transport == "sse"
    assert server.events[-1] == "tools/list:sse"


@pytest.mark.asyncio
async def test_initialize_and_tool_discovery_have_application_deadlines(
    mcp_api: ModuleType,
) -> None:
    initialize_server = FakeMcpServer([write_tool()])
    initialize_server.hanging_initializers.add("streamable-http")
    client = mcp_api.PiecesMcpClient(
        "http://pieces.example.test:39300",
        connector=initialize_server.connector,
        initialize_timeout_seconds=0.01,
    )

    capabilities = await client.connect()

    assert capabilities.transport == "sse"
    assert initialize_server.sessions[0].closed is True

    list_server = FakeMcpServer([write_tool()])
    list_server.hang_list_tools = True
    client = mcp_api.PiecesMcpClient(
        "http://pieces.example.test:39300",
        connector=list_server.connector,
        list_timeout_seconds=0.01,
    )

    with pytest.raises(mcp_api.McpConnectionError, match="tool discovery"):
        await client.connect()

    assert list_server.sessions[0].closed is True


@pytest.mark.asyncio
async def test_does_not_fall_back_after_session_initialization(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool()])
    server.list_tools_failure = ConnectionError("connection lost after initialize")
    client = mcp_api.PiecesMcpClient(
        "http://pieces.example.test:39300", connector=server.connector
    )

    with pytest.raises(mcp_api.McpConnectionError):
        await client.connect()

    assert [transport for transport, _endpoint in server.endpoints] == ["streamable-http"]


@pytest.mark.asyncio
async def test_missing_write_tool_and_optional_search_are_reported(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([search_tool()])

    client = await connected_client(mcp_api, server)

    assert client.capabilities.import_ready is False
    assert client.capabilities.search_available is True
    assert client.capabilities.blocking_error == "missing_write_tool"
    with pytest.raises(mcp_api.McpCapabilityError, match="create_pieces_memory"):
        client.build_write_arguments(
            mcp_api.DispatchPayload(
                title="Title",
                markdown_body="Body",
                external_links=(),
                source_agent=SourceAgent.CODEX,
                source_path=Path("/local/memory.md"),
                import_id=IMPORT_ID,
            )
        )


@pytest.mark.asyncio
async def test_unknown_live_required_field_blocks_imports(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool(extra_required=("tenant_secret",))])

    client = await connected_client(mcp_api, server)

    assert client.capabilities.import_ready is False
    assert client.capabilities.blocking_error == "unknown_required_fields:tenant_secret"


@pytest.mark.asyncio
async def test_missing_search_tool_exposes_limited_duplicate_coverage(
    mcp_api: ModuleType,
) -> None:
    client = await connected_client(mcp_api, FakeMcpServer([write_tool()]))

    marker_result = await client.search_marker(IMPORT_ID)

    assert client.capabilities.search_available is False
    assert marker_result.outcome == "unavailable"
    assert marker_result.coverage == "local-only"


@pytest.mark.asyncio
async def test_write_mapping_is_allowlisted_and_boundary_precedes_sdk_entry(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(optional=("connected_client", "externalLinks"))])
    server.queue("create_pieces_memory", tool_result({"id": "memory-42"}))
    client = await connected_client(mcp_api, server)
    payload = mcp_api.DispatchPayload(
        title="Connection ownership",
        markdown_body="Maintain one initialized MCP connection.",
        external_links=("https://example.test/design",),
        source_agent=SourceAgent.CLAUDE,
        source_path=Path("/workspace/project/memory.md"),
        import_id=IMPORT_ID,
    )
    arguments = client.build_write_arguments(payload)

    async def persist_boundary() -> None:
        server.events.append("persist:dispatch_started_at")

    result = await client.create_memory(arguments, before_call=persist_boundary)

    assert arguments == {
        "summary_description": "Connection ownership",
        "summary": (
            "Maintain one initialized MCP connection.\n\n"
            "---\n"
            "Imported by Agent2Pieces\n"
            "Source agent: Claude Code\n"
            f"{MARKER}"
        ),
        "connected_client": "Agent2Pieces",
        "externalLinks": ["https://example.test/design"],
    }
    assert result.memory_id == "memory-42"
    assert server.events.index("persist:dispatch_started_at") < server.events.index(
        "tools/call:create_pieces_memory:streamable-http"
    )
    assert len(server.tool_calls) == 1


@pytest.mark.asyncio
async def test_unknown_and_absent_optional_fields_are_never_sent(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool(optional=("server_extension",))])
    client = await connected_client(mcp_api, server)
    payload = mcp_api.DispatchPayload(
        title="Allowlist",
        markdown_body="Only send supported known fields.",
        external_links=("https://example.test",),
        source_agent=SourceAgent.HERMES,
        source_path=Path("/workspace/memory.md"),
        import_id=IMPORT_ID,
    )

    arguments = client.build_write_arguments(payload)

    assert set(arguments) == {"summary_description", "summary"}
    assert "server_extension" not in arguments


@pytest.mark.asyncio
async def test_invalid_external_link_is_rejected_before_call(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool()])
    client = await connected_client(mcp_api, server)
    payload = mcp_api.DispatchPayload(
        title="Unsafe link",
        markdown_body="Body",
        external_links=("https://user:secret@example.test/private",),
        source_agent=SourceAgent.CODEX,
        source_path=Path("/workspace/memory.md"),
        import_id=IMPORT_ID,
    )

    with pytest.raises(ValueError, match="HTTP"):
        client.build_write_arguments(payload)

    assert server.tool_calls == []


@pytest.mark.asyncio
async def test_longest_host_mapping_adds_only_supported_project_and_file(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(optional=("project", "files"))])
    client = await connected_client(mcp_api, server)
    payload = mcp_api.DispatchPayload(
        title="Mapped source",
        markdown_body="Body",
        external_links=(),
        source_agent=SourceAgent.CODEX,
        source_path=Path("/workspace/project/memory/topic.md"),
        import_id=IMPORT_ID,
    )
    mappings = (
        mcp_api.PathMapping(Path("/workspace"), "D:/code", "workspace"),
        mcp_api.PathMapping(Path("/workspace/project"), "/srv/project", "agent2pieces"),
    )

    arguments = client.build_write_arguments(payload, mappings=mappings)

    assert arguments["project"] == "agent2pieces"
    assert arguments["files"] == ["/srv/project/memory/topic.md"]
    assert "Project: agent2pieces" in arguments["summary"]
    assert "/workspace" not in arguments["summary"]


@pytest.mark.asyncio
async def test_marker_search_requires_exact_standalone_line_and_groups_parent(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        annotation_page(
            [
                annotation("ann-substring", f"prefix {MARKER}", parent_id="ignored-1"),
                annotation("ann-case", MARKER.lower(), parent_id="ignored-2"),
                annotation("ann-comment", f"<!-- {MARKER} -->", parent_id="ignored-3"),
                annotation("ann-2", f"Before\r\n{MARKER}\r\nAfter", parent_id="memory-z"),
                annotation("ann-1", MARKER, parent_id="memory-z"),
            ]
        ),
    )
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == "one_parent"
    assert result.parent_memory_ids == ("memory-z",)
    assert result.annotation_ids == ("ann-1", "ann-2")
    assert server.tool_calls[0].arguments == {"query": MARKER, "limit": 50}


@pytest.mark.asyncio
async def test_marker_search_accepts_live_empty_results_envelope(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        tool_result(
            {
                "results": [],
                "total": 0,
                "limit": 50,
                "query": MARKER,
                "format": "detailed",
            },
            as_text=True,
        ),
    )
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert (result.outcome, result.coverage) == ("absent", "complete")


@pytest.mark.asyncio
async def test_marker_search_extracts_parent_from_live_results_envelope(
    mcp_api: ModuleType,
) -> None:
    parent_memory_id = "123e4567-e89b-42d3-a456-426614174000"
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        tool_result(
            {
                "results": [
                    {
                        "annotation": {
                            "id": "123e4567-e89b-42d3-a456-426614174001",
                            "text": MARKER,
                            "summaries": {"indices": {parent_memory_id: 0}},
                        }
                    }
                ],
                "total": 1,
                "limit": 50,
                "query": MARKER,
                "format": "detailed",
            },
            as_text=True,
        ),
    )
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert (result.outcome, result.parent_memory_ids) == (
        "one_parent",
        (parent_memory_id,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("records", "expected_outcome", "expected_parents"),
    [
        ([annotation("a", MARKER, parent_id=None)], "parent_unknown", ()),
        (
            [
                annotation("a", MARKER, parent_id="memory-b"),
                annotation("b", MARKER, parent_id="memory-a"),
            ],
            "multiple_parents",
            ("memory-a", "memory-b"),
        ),
    ],
)
async def test_marker_parentless_and_multiple_parent_outcomes(
    mcp_api: ModuleType,
    records: list[dict[str, Any]],
    expected_outcome: str,
    expected_parents: tuple[str, ...],
) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue("annotations_full_text_search", annotation_page(records))
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == expected_outcome
    assert result.parent_memory_ids == expected_parents


@pytest.mark.asyncio
async def test_parent_identity_uses_fixed_precedence(mcp_api: ModuleType) -> None:
    record = annotation("ann", MARKER, parent_id="first")
    record["annotation"]["summary"]["reference"] = {"id": "second"}
    record["annotation"]["summary_id"] = "third"
    record["summary"] = {"id": "fourth"}
    record["summary_id"] = "fifth"
    record["memory"] = {"id": "sixth"}
    record["memory_id"] = "seventh"
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue("annotations_full_text_search", annotation_page([record]))
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert result.parent_memory_ids == ("first",)


@pytest.mark.asyncio
async def test_marker_search_pages_deduplicates_and_caps_at_fifty(mcp_api: ModuleType) -> None:
    first_page = [annotation(f"ann-{index:02d}", "no marker") for index in range(30)]
    second_page = [
        annotation(f"ann-{index:02d}", "no marker") for index in range(29, 51)
    ]
    server = FakeMcpServer([write_tool(), search_tool(cursor=True)])
    server.queue(
        "annotations_full_text_search",
        annotation_page(first_page, has_more=True, next_cursor="page-2"),
        annotation_page(second_page, has_more=True, next_cursor="page-3"),
    )
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == "truncated"
    assert result.unique_annotations == 50
    assert len(server.tool_calls) == 2
    assert server.tool_calls[1].arguments["cursor"] == "page-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        annotation_page([], has_more=True, next_cursor=None),
        annotation_page([], has_more=None, truncated=None),
        annotation_page([], has_more=False, truncated=True),
    ],
)
async def test_unpaged_or_unproven_search_is_truncated(
    mcp_api: ModuleType,
    page: Any,
) -> None:
    server = FakeMcpServer([write_tool(), search_tool(cursor=False)])
    server.queue("annotations_full_text_search", page)
    client = await connected_client(mcp_api, server)

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == "truncated"


@pytest.mark.asyncio
async def test_read_search_retries_twice_with_backoff_hook(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        TimeoutError("first"),
        ConnectionError("second"),
        annotation_page([]),
    )
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    client = await connected_client(
        mcp_api,
        server,
        sleep=record_delay,
        search_retry_delays=(0.25, 0.5),
    )

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == "absent"
    assert delays == [0.25, 0.5]
    assert len(server.tool_calls) == 3


@pytest.mark.asyncio
async def test_search_error_after_retries_is_classified(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        TimeoutError("one"),
        TimeoutError("two"),
        TimeoutError("three"),
    )

    async def no_wait(_delay: float) -> None:
        return None

    client = await connected_client(
        mcp_api,
        server,
        sleep=no_wait,
        search_retry_delays=(0.0, 0.0),
    )

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == "search_error"
    assert result.error_kind == "timeout"
    assert len(server.tool_calls) == 3


@pytest.mark.asyncio
async def test_never_returning_search_is_deadlined_and_retried_twice(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.hanging_tools.add("annotations_full_text_search")

    async def no_wait(_delay: float) -> None:
        return None

    client = await connected_client(
        mcp_api,
        server,
        sleep=no_wait,
        search_retry_delays=(0.0, 0.0),
        search_timeout_seconds=0.01,
    )

    result = await client.search_marker(IMPORT_ID)

    assert result.outcome == "search_error"
    assert result.error_kind == "timeout"
    assert len(server.tool_calls) == 3


@pytest.mark.asyncio
async def test_remote_search_rejects_server_rank_without_local_similarity(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        annotation_page(
            [
                annotation(
                    "ann-weak",
                    "An unrelated grocery list about oranges and rice.",
                    similarity=0.999,
                )
            ]
        ),
    )
    client = await connected_client(mcp_api, server)
    candidate = DedupeCandidate(
        candidate_id="candidate-1",
        source_agent=SourceAgent.CODEX,
        source_key="memory.md",
        title="Durable MCP dispatch boundaries",
        markdown_body="Persist an ambiguous attempt before initiating every remote write call.",
        payload_hash="a" * 64,
    )

    result = await client.search_duplicates(candidate)

    assert result.coverage == "complete"
    assert result.matches == ()


@pytest.mark.asyncio
async def test_remote_search_uses_title_only_and_local_rescoring(mcp_api: ModuleType) -> None:
    body = "alpha beta gamma delta epsilon zeta eta theta iota kappa " + "x" * 2_100
    server = FakeMcpServer([write_tool(), search_tool()])
    server.queue(
        "annotations_full_text_search",
        annotation_page([annotation("ann-strong", body, parent_id="memory-strong")], as_text=True),
    )
    client = await connected_client(mcp_api, server)
    candidate = DedupeCandidate(
        candidate_id="candidate-1",
        source_agent=SourceAgent.CLAUDE,
        source_key="topic.md",
        title="Stable transport",
        markdown_body=body,
        payload_hash="b" * 64,
    )

    result = await client.search_duplicates(candidate)

    assert result.coverage == "complete"
    assert len(result.matches) == 1
    assert result.matches[0].annotation_id == "ann-strong"
    assert result.matches[0].classification == "likely"
    assert server.tool_calls[0].arguments == {
        "query": "Stable transport",
        "limit": 50,
    }
    assert body[:2000] not in str(server.tool_calls[0].arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("slow"), ConnectionError("lost")])
async def test_write_failures_are_ambiguous_and_never_retried(
    mcp_api: ModuleType,
    failure: BaseException,
) -> None:
    server = FakeMcpServer([write_tool()])
    server.queue("create_pieces_memory", failure, tool_result({"id": "must-not-run"}))
    client = await connected_client(mcp_api, server)
    arguments = client.build_write_arguments(
        mcp_api.DispatchPayload(
            title="Write once",
            markdown_body="Do not retry an ambiguous write.",
            external_links=(),
            source_agent=SourceAgent.CODEX,
            source_path=Path("/workspace/memory.md"),
            import_id=IMPORT_ID,
        )
    )

    async def persisted() -> None:
        return None

    with pytest.raises(mcp_api.McpWriteError) as captured:
        await client.create_memory(arguments, before_call=persisted)

    assert captured.value.ambiguous is True
    assert captured.value.retryable is False
    assert captured.value.error_kind in {"timeout", "connection_lost"}
    assert len(server.tool_calls) == 1
    assert [transport for transport, _endpoint in server.endpoints] == ["streamable-http"]


@pytest.mark.asyncio
async def test_never_returning_write_is_deadlined_once_and_remains_ambiguous(
    mcp_api: ModuleType,
) -> None:
    server = FakeMcpServer([write_tool()])
    server.hanging_tools.add("create_pieces_memory")
    client = await connected_client(
        mcp_api,
        server,
        write_timeout_seconds=0.01,
    )
    arguments = client.build_write_arguments(
        mcp_api.DispatchPayload(
            title="Timed write",
            markdown_body="Keep the durable ambiguity boundary.",
            external_links=(),
            source_agent=SourceAgent.CODEX,
            source_path=Path("/workspace/memory.md"),
            import_id=IMPORT_ID,
        )
    )
    boundary_calls = 0

    async def persisted() -> None:
        nonlocal boundary_calls
        boundary_calls += 1

    with pytest.raises(mcp_api.McpWriteError) as captured:
        await client.create_memory(arguments, before_call=persisted)

    assert captured.value.error_kind == "timeout"
    assert captured.value.ambiguous is True
    assert captured.value.retryable is False
    assert boundary_calls == 1
    assert len(server.tool_calls) == 1


@pytest.mark.parametrize("field", ["initialize", "list", "search", "write"])
def test_mcp_deadlines_must_be_positive_and_finite(
    mcp_api: ModuleType,
    field: str,
) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        mcp_api.PiecesMcpClient(
            "http://pieces.example.test:39300",
            **{f"{field}_timeout_seconds": float("inf")},
        )


@pytest.mark.asyncio
async def test_write_boundary_failure_prevents_call_initiation(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool()])
    client = await connected_client(mcp_api, server)
    arguments = client.build_write_arguments(
        mcp_api.DispatchPayload(
            title="Boundary failure",
            markdown_body="Body",
            external_links=(),
            source_agent=SourceAgent.HERMES,
            source_path=Path("/workspace/memory.md"),
            import_id=IMPORT_ID,
        )
    )

    async def fail_to_persist() -> None:
        raise RuntimeError("ledger unavailable")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await client.create_memory(arguments, before_call=fail_to_persist)

    assert server.tool_calls == []


@pytest.mark.asyncio
async def test_malformed_write_result_is_ambiguous(mcp_api: ModuleType) -> None:
    server = FakeMcpServer([write_tool()])
    server.queue("create_pieces_memory", tool_result({"unexpected": "shape"}))
    client = await connected_client(mcp_api, server)
    arguments = client.build_write_arguments(
        mcp_api.DispatchPayload(
            title="Malformed result",
            markdown_body="Body",
            external_links=(),
            source_agent=SourceAgent.CODEX,
            source_path=Path("/workspace/memory.md"),
            import_id=IMPORT_ID,
        )
    )

    async def persisted() -> None:
        return None

    with pytest.raises(mcp_api.McpWriteError) as captured:
        await client.create_memory(arguments, before_call=persisted)

    assert captured.value.error_kind == "malformed_response"
    assert captured.value.ambiguous is True
    assert len(server.tool_calls) == 1


def test_before_call_type_is_async_callback_contract() -> None:
    callback: Callable[[], Awaitable[None]]

    async def callback() -> None:
        return None

    assert callable(callback)
