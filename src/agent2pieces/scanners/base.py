"""Shared read-only scanner primitives."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from agent2pieces.models import (
    AdapterScanResult,
    CandidateInput,
    NormalizedCandidate,
    ScanCounts,
    ScanDisposition,
    SourceAgent,
    SourceDisposition,
)
from agent2pieces.normalization import (
    candidate_input_hash,
    canonicalize_body,
    extract_external_links,
    import_id_from_payload_hash,
    normalize_title,
    payload_hash,
    source_hash,
)

MAX_SOURCE_BYTES = 65_536
_CLOSING_HEADING_MARKERS = re.compile(r"[ \t]+#+[ \t]*$")


class FrontMatterError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedDocument:
    metadata: dict[str, str]
    body: str


def read_binary(
    path: Path,
    *,
    resolved_root: Path | None = None,
    max_bytes: int | None = MAX_SOURCE_BYTES,
) -> bytes:
    """Read one pinned regular file handle with a bounded allocation."""
    with open_binary(path, resolved_root=resolved_root) as (source, _opened_stat):
        if max_bytes is None:
            return source.read()
        return source.read(max_bytes + 1)


@contextmanager
def open_binary(
    path: Path,
    *,
    resolved_root: Path | None = None,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Resolve within the root, then pin and open the canonical regular file."""

    lexical_before = path.lstat()
    is_file_symlink = stat.S_ISLNK(lexical_before.st_mode)
    if _is_unsupported_reparse(path, lexical_before):
        raise OSError("source reparse points are not supported")
    if not is_file_symlink and not stat.S_ISREG(lexical_before.st_mode):
        raise OSError("source is not a regular file")
    if is_file_symlink and resolved_root is None:
        raise OSError("source symlink requires a configured root")

    canonical_root = resolved_root.resolve(strict=True) if resolved_root is not None else None
    canonical_path = path.resolve(strict=True)
    if canonical_root is not None:
        try:
            canonical_path.relative_to(canonical_root)
        except ValueError as error:
            raise OSError("source escaped configured root") from error
    canonical_before = canonical_path.lstat()
    if (
        stat.S_ISLNK(canonical_before.st_mode)
        or _is_unsupported_reparse(canonical_path, canonical_before)
        or not stat.S_ISREG(canonical_before.st_mode)
    ):
        raise OSError("source target is not a regular file")
    if not is_file_symlink and not os.path.samestat(lexical_before, canonical_before):
        raise OSError("source changed during resolution")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(canonical_path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(canonical_before, opened):
            raise OSError("source changed during open")
        lexical_after = path.lstat()
        if not os.path.samestat(lexical_before, lexical_after):
            raise OSError("source changed during confinement check")
        if path.resolve(strict=True) != canonical_path:
            raise OSError("source link changed during confinement check")
        if (
            canonical_root is not None
            and resolved_root is not None
            and resolved_root.resolve(strict=True) != canonical_root
        ):
            raise OSError("configured root changed during confinement check")
        if not os.path.samestat(opened, canonical_path.stat()):
            raise OSError("source changed during confinement check")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source, opened
    finally:
        os.close(descriptor)


def walk_paths(
    root: Path,
    *,
    excluded_directory_names: frozenset[str] = frozenset(),
) -> Iterable[Path]:
    """Yield entries deterministically without descending directory symlinks."""
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        path = Path(entry.path)
        try:
            opened = path.lstat()
        except OSError:
            yield path
            continue
        mode = opened.st_mode
        if _is_junction(path) or _is_unsupported_reparse(path, opened):
            yield path
            continue
        if stat.S_ISDIR(mode):
            if entry.name.casefold() in excluded_directory_names:
                continue
            yield from walk_paths(path, excluded_directory_names=excluded_directory_names)
        else:
            yield path


def path_problem(path: Path, resolved_root: Path) -> str | None:
    try:
        opened = path.lstat()
    except OSError:
        return "unreadable"
    if _is_junction(path) or _is_unsupported_reparse(path, opened):
        return "symlink_escape"
    if stat.S_ISLNK(opened.st_mode):
        try:
            target = path.resolve(strict=True)
            target.relative_to(resolved_root.resolve(strict=True))
            target_stat = target.lstat()
        except (OSError, ValueError):
            return "symlink_escape"
        if (
            stat.S_ISLNK(target_stat.st_mode)
            or _is_unsupported_reparse(target, target_stat)
            or not stat.S_ISREG(target_stat.st_mode)
        ):
            return "symlink_escape"
        return None
    if not stat.S_ISREG(opened.st_mode):
        return "special_file"
    return None


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        checker = getattr(os.path, "isjunction", None)
        return bool(checker(path)) if checker is not None else False
    try:
        return bool(checker())
    except OSError:
        return True


def _is_unsupported_reparse(path: Path, opened: os.stat_result) -> bool:
    if stat.S_ISLNK(opened.st_mode):
        return False
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(opened, "st_file_attributes", 0)
    return bool(marker and attributes & marker) or _is_junction(path)


def parse_frontmatter(text: str, *, required_keys: set[str]) -> ParsedDocument:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0] != "---":
        return ParsedDocument({}, normalized)
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise FrontMatterError("unterminated front matter") from error
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line:
            continue
        stripped = line.strip()
        if line[0].isspace():
            if any(
                stripped == key or stripped.startswith(key + ":")
                for key in required_keys
            ):
                raise FrontMatterError("invalid required front matter")
            continue
        if ":" not in line:
            if any(
                stripped == key or stripped.startswith(key + " ")
                for key in required_keys
            ):
                raise FrontMatterError("invalid required front matter")
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key not in required_keys:
            continue
        if key in metadata:
            raise FrontMatterError("invalid required front matter")
        metadata[key] = _frontmatter_scalar(raw_value)
    return ParsedDocument(metadata, "\n".join(lines[closing + 1 :]))


def _frontmatter_scalar(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise FrontMatterError("invalid required front matter")
    if value[0] not in {'"', "'"}:
        value = re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip()
        if not value or value[0] in "[{|>&*!":
            raise FrontMatterError("invalid required front matter")
        return value
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise FrontMatterError("invalid required front matter")
    inner = value[1:-1]
    if quote == "'":
        decoded = inner.replace("''", "'")
        if not decoded:
            raise FrontMatterError("invalid required front matter")
        return decoded
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise FrontMatterError("invalid required front matter") from error
    if not isinstance(decoded, str) or not decoded:
        raise FrontMatterError("invalid required front matter")
    return decoded


def source_timestamp(metadata: dict[str, str], *, mtime_ns: int) -> str:
    raw = metadata.get("updated_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def first_heading(text: str, *, h1_only: bool) -> str | None:
    for line in text.splitlines():
        stripped = line.lstrip()
        marker_count = len(stripped) - len(stripped.lstrip("#"))
        if marker_count == 0 or (h1_only and marker_count != 1) or marker_count > 6:
            continue
        remainder = stripped[marker_count:]
        if not remainder.startswith(" "):
            continue
        title = remainder.strip()
        if title and set(title) == {"#"}:
            continue
        title = _CLOSING_HEADING_MARKERS.sub("", title).rstrip()
        if title:
            return title
    return None


def make_candidate(
    *,
    agent: SourceAgent,
    source_key: str,
    source_path: Path,
    project_scope: str,
    source_updated_at: str,
    title: str,
    body: str,
) -> CandidateInput:
    normalized_body = canonicalize_body(body)
    normalized_title = normalize_title(title)[:240]
    normalized_project_scope = project_scope.strip()[:512]
    links = extract_external_links(normalized_body)
    content_hash = payload_hash(
        title=normalized_title,
        markdown_body=normalized_body,
        external_links=links,
    )
    candidate = NormalizedCandidate(
        source_agent=agent,
        source_key=source_key,
        source_path=str(source_path.absolute()),
        project_scope=normalized_project_scope,
        source_updated_at=source_updated_at,
        title=normalized_title,
        markdown_body=normalized_body,
        external_links=list(links),
        source_hash=source_hash(body),
        payload_hash=content_hash,
        import_id=import_id_from_payload_hash(content_hash),
    )
    return CandidateInput(
        candidate=candidate,
        candidate_input_hash=candidate_input_hash(
            title=normalized_title,
            markdown_body=normalized_body,
            external_links=links,
            project_scope=normalized_project_scope,
        ),
    )


def disposition(
    path: Path,
    reason: str,
    *,
    source_key: str | None = None,
    byte_count: int = 0,
    excluded: bool = False,
) -> ScanDisposition:
    return ScanDisposition(
        disposition=(SourceDisposition.EXCLUDED if excluded else SourceDisposition.QUARANTINED),
        reason=reason,
        source_path=str(path.absolute()),
        source_key=source_key,
        byte_count=byte_count,
    )


def result(
    candidates: list[CandidateInput], dispositions: list[ScanDisposition]
) -> AdapterScanResult:
    excluded = sum(item.disposition is SourceDisposition.EXCLUDED for item in dispositions)
    quarantined = len(dispositions) - excluded
    return AdapterScanResult(
        candidates=tuple(candidates),
        dispositions=tuple(dispositions),
        counts=ScanCounts(
            discovered=len(candidates) + len(dispositions),
            accepted=len(candidates),
            excluded=excluded,
            quarantined=quarantined,
        ),
    )
