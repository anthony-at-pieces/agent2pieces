"""Claude Code auto-memory scanner."""

from __future__ import annotations

from pathlib import Path

import agent2pieces.scanners.base as scanner_base
from agent2pieces.models import AdapterScanResult, CandidateInput, ScanDisposition, SourceAgent
from agent2pieces.scanners.base import (
    MAX_SOURCE_BYTES,
    FrontMatterError,
    disposition,
    first_heading,
    make_candidate,
    parse_frontmatter,
    path_problem,
    result,
    source_timestamp,
    walk_paths,
)

_METADATA_KEYS = {"title", "project", "type", "updated_at"}


def scan_claude_root(root: Path) -> AdapterScanResult:
    candidates: list[CandidateInput] = []
    dispositions: list[ScanDisposition] = []
    resolved_root = root.resolve()
    for path in walk_paths(root):
        if not (path.name.endswith(".md") or path.is_symlink()):
            continue
        key = path.relative_to(root).as_posix()
        problem = path_problem(path, resolved_root)
        if problem is not None:
            dispositions.append(disposition(path, problem, source_key=key))
            continue
        if not path.name.endswith(".md"):
            continue
        if path.name == "MEMORY.md":
            dispositions.append(disposition(path, "excluded_index", source_key=key, excluded=True))
            continue
        try:
            before = path.lstat()
            if before.st_size > MAX_SOURCE_BYTES:
                dispositions.append(
                    disposition(
                        path,
                        "oversize",
                        source_key=key,
                        byte_count=before.st_size,
                    )
                )
                continue
            raw = scanner_base.read_binary(path, resolved_root=resolved_root)
        except OSError:
            dispositions.append(disposition(path, "unreadable", source_key=key))
            continue
        if len(raw) > MAX_SOURCE_BYTES:
            dispositions.append(disposition(path, "oversize", source_key=key, byte_count=len(raw)))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            dispositions.append(
                disposition(path, "invalid_utf8", source_key=key, byte_count=len(raw))
            )
            continue
        try:
            parsed = parse_frontmatter(text, required_keys=_METADATA_KEYS)
        except FrontMatterError:
            dispositions.append(disposition(path, "malformed", source_key=key, byte_count=len(raw)))
            continue
        if parsed.metadata.get("type", "").casefold() == "user":
            dispositions.append(
                disposition(
                    path, "excluded_user", source_key=key, byte_count=len(raw), excluded=True
                )
            )
            continue
        if not parsed.body or not parsed.body.strip():
            dispositions.append(disposition(path, "empty", source_key=key, byte_count=len(raw)))
            continue
        relative_parent = path.relative_to(root).parent.as_posix()
        fallback_project = root.parent.name if root.name == "memory" else relative_parent
        title = (
            parsed.metadata.get("title") or first_heading(parsed.body, h1_only=True) or path.stem
        )
        candidates.append(
            make_candidate(
                agent=SourceAgent.CLAUDE,
                source_key=key,
                source_path=path,
                project_scope=parsed.metadata.get("project", fallback_project),
                source_updated_at=source_timestamp(parsed.metadata, mtime_ns=before.st_mtime_ns),
                title=title,
                body=parsed.body,
            )
        )
    return result(candidates, dispositions)
