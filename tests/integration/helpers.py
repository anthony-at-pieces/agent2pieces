from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from agent2pieces.ledger import Ledger
from agent2pieces.mcp_client import (
    DispatchPayload,
    MarkerSearchResult,
    McpCapabilities,
    McpWriteError,
    PathMapping,
    WriteResult,
)
from agent2pieces.models import (
    CandidatePayload,
    CandidateRecord,
    CandidateStatus,
    SourceAgent,
    SourceRevisionIdentity,
)
from agent2pieces.normalization import import_id_from_payload_hash, payload_hash


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def add_candidate(
    ledger: Ledger,
    *,
    payload: CandidatePayload,
    agent: SourceAgent = SourceAgent.CODEX,
    source_key: str = "memory.md",
    source_path: str = "/memory/memory.md",
    source_updated_at: str = "2026-08-01T00:00:00Z",
) -> CandidateRecord:
    root_path = str(Path(source_path).parent)
    root_id = ledger.add_source_root(
        agent=agent,
        lexical_path=root_path,
        resolved_path=root_path,
        enabled=True,
        is_default=False,
    )
    identity_seed = f"{agent}:{source_key}:{payload.model_dump_json()}"
    revision = ledger.get_or_create_source_revision(
        SourceRevisionIdentity(
            agent=agent,
            root_id=root_id,
            source_key=source_key,
            source_hash=digest(identity_seed + ":source"),
            candidate_input_hash=digest(identity_seed + ":input"),
        ),
        source_path=source_path,
    )
    content_hash = payload_hash(
        title=payload.title,
        markdown_body=payload.markdown_body,
        external_links=payload.external_links,
    )
    candidate = ledger.create_candidate(
        revision_id=revision.revision_id,
        payload=payload,
        payload_hash=content_hash,
        import_id=import_id_from_payload_hash(content_hash),
    )
    scan_id = ledger.create_scan(settings_version=ledger.get_settings().version)
    ledger.record_source_observation(
        revision_id=revision.revision_id,
        scan_id=scan_id,
        source_updated_at=source_updated_at,
        file_mtime_ns=1,
        raw_byte_count=len(payload.markdown_body.encode("utf-8")),
    )
    return candidate


def approve_candidate(ledger: Ledger, candidate: CandidateRecord) -> CandidateRecord:
    return ledger.update_candidate(
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        payload=candidate.payload,
        payload_hash=candidate.payload_hash,
        import_id=candidate.import_id,
        status=CandidateStatus.APPROVED,
    )


class FakePiecesClient:
    def __init__(
        self,
        *,
        search_available: bool = True,
        endpoint: str = "http://pieces.test/model_context_protocol/2025-03-26/mcp",
        marker_results: Iterable[MarkerSearchResult] = (),
        write_results: Iterable[Literal["success", "pre_failure", "post_failure"]] = (),
    ) -> None:
        self.capabilities = McpCapabilities(
            transport="streamable-http",
            endpoint=endpoint,
            server_version="fake",
            import_ready=True,
            search_available=search_available,
            blocking_error=None,
            checked_at="2026-09-01T00:00:00Z",
        )
        self.marker_results = deque(marker_results)
        self.write_results = deque(write_results)
        self.events: list[str] = []
        self.arguments: list[dict[str, Any]] = []
        self.mappings: list[tuple[PathMapping, ...]] = []
        self.sdk_entry_hook: Callable[[], None] | None = None
        self.block_write: Awaitable[None] | None = None
        self.active_writes = 0
        self.maximum_active_writes = 0

    async def search_marker(self, import_id: str) -> MarkerSearchResult:
        self.events.append(f"search:{import_id}")
        if self.marker_results:
            return self.marker_results.popleft()
        return MarkerSearchResult(outcome="absent", coverage="complete")

    def build_write_arguments(
        self,
        payload: DispatchPayload,
        *,
        mappings: tuple[PathMapping, ...] = (),
    ) -> dict[str, Any]:
        self.mappings.append(mappings)
        self.events.append(f"build:{payload.import_id}")
        arguments: dict[str, Any] = {
            "summary_description": payload.title,
            "summary": payload.markdown_body,
            "import_id": payload.import_id,
        }
        for mapping in mappings:
            try:
                relative = payload.source_path.resolve(strict=False).relative_to(
                    mapping.local_root.resolve(strict=False)
                )
            except ValueError:
                continue
            arguments["project"] = mapping.project
            arguments["files"] = [
                f"{mapping.host_root.rstrip('/')}/{relative.as_posix()}"
            ]
            break
        return arguments

    async def create_memory(
        self,
        arguments: dict[str, Any],
        *,
        before_call: Callable[[], Awaitable[None]],
    ) -> WriteResult:
        outcome = self.write_results.popleft() if self.write_results else "success"
        if outcome == "pre_failure":
            self.events.append("pre_failure")
            raise RuntimeError("proven failure before SDK call initiation")
        await before_call()
        self.events.append("sdk_entry")
        if self.sdk_entry_hook is not None:
            self.sdk_entry_hook()
        if outcome == "post_failure":
            raise McpWriteError("ambiguous write", error_kind="timeout")
        self.active_writes += 1
        self.maximum_active_writes = max(self.maximum_active_writes, self.active_writes)
        try:
            if self.block_write is not None:
                await self.block_write
            self.arguments.append(dict(arguments))
            return WriteResult(memory_id=f"memory-{len(self.arguments)}")
        finally:
            self.active_writes -= 1
