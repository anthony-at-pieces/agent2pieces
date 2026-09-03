"""Hermes sectioned-memory scanner."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

import agent2pieces.scanners.base as scanner_base
from agent2pieces.models import AdapterScanResult, CandidateInput, ScanDisposition, SourceAgent
from agent2pieces.scanners.base import (
    MAX_SOURCE_BYTES,
    disposition,
    first_heading,
    make_candidate,
    path_problem,
    result,
    source_timestamp,
    walk_paths,
)

_DELIMITER = "\u00a7".encode()
_READ_CHUNK_BYTES = 8_192


def _stream_entries(source: BinaryIO) -> Iterable[tuple[bytes | None, int]]:
    """Yield nonempty entries while retaining at most one bounded entry buffer."""

    buffer = bytearray()
    byte_count = 0
    has_content = False
    oversized = False
    carry = b""

    def append(data: bytes) -> None:
        nonlocal byte_count, has_content, oversized
        byte_count += len(data)
        has_content = has_content or bool(data.strip())
        if oversized:
            return
        remaining = MAX_SOURCE_BYTES + 1 - len(buffer)
        buffer.extend(data[:remaining])
        if byte_count > MAX_SOURCE_BYTES:
            oversized = True
            buffer.clear()

    def finish() -> tuple[bytes | None, int] | None:
        nonlocal byte_count, has_content, oversized
        entry = (None if oversized else bytes(buffer), byte_count) if has_content else None
        buffer.clear()
        byte_count = 0
        has_content = False
        oversized = False
        return entry

    while chunk := source.read(_READ_CHUNK_BYTES):
        data = carry + chunk
        carry = b""
        cursor = 0
        while (delimiter_at := data.find(_DELIMITER, cursor)) >= 0:
            append(data[cursor:delimiter_at])
            entry = finish()
            if entry is not None:
                yield entry
            cursor = delimiter_at + len(_DELIMITER)
        remainder = data[cursor:]
        if remainder.endswith(_DELIMITER[:1]):
            carry = remainder[-1:]
            remainder = remainder[:-1]
        append(remainder)
    append(carry)
    entry = finish()
    if entry is not None:
        yield entry


def scan_hermes_root(root: Path) -> AdapterScanResult:
    candidates: list[CandidateInput] = []
    dispositions: list[ScanDisposition] = []
    resolved_root = root.resolve()
    for path in walk_paths(root):
        if path.name != "MEMORY.md" and not path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        problem = path_problem(path, resolved_root)
        if problem is not None:
            dispositions.append(disposition(path, problem, source_key=relative))
            continue
        if path.name != "MEMORY.md":
            continue
        parent = path.relative_to(root).parent.as_posix()
        project = "hermes" if parent == "." else parent
        entry_found = False
        try:
            with scanner_base.open_binary(path, resolved_root=resolved_root) as (
                source,
                before,
            ):
                for index, (raw_entry, byte_count) in enumerate(
                    _stream_entries(source), start=1
                ):
                    entry_found = True
                    key = f"{relative}#section={index}"
                    if raw_entry is None:
                        dispositions.append(
                            disposition(
                                path, "oversize", source_key=key, byte_count=byte_count
                            )
                        )
                        continue
                    try:
                        entry = raw_entry.decode("utf-8")
                    except UnicodeDecodeError:
                        dispositions.append(
                            disposition(
                                path,
                                "invalid_utf8",
                                source_key=key,
                                byte_count=byte_count,
                            )
                        )
                        continue
                    heading = first_heading(entry, h1_only=False)
                    first_line = next(
                        (line.strip() for line in entry.splitlines() if line.strip()), ""
                    )
                    title = heading or first_line[:120] or f"Hermes memory {index}"
                    candidates.append(
                        make_candidate(
                            agent=SourceAgent.HERMES,
                            source_key=key,
                            source_path=path,
                            project_scope=project,
                            source_updated_at=source_timestamp(
                                {}, mtime_ns=before.st_mtime_ns
                            ),
                            title=title,
                            body=entry,
                        )
                    )
        except OSError:
            dispositions.append(disposition(path, "unreadable", source_key=relative))
            continue
        if not entry_found:
            dispositions.append(
                disposition(path, "empty", source_key=relative)
            )
    return result(candidates, dispositions)
