"""Deterministic candidate normalization and content identities."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlsplit

from agent2pieces.models import SourceAgent

_TRAILING_ASCII_WHITESPACE = re.compile(r"[ \t]+$")
_TITLE_WHITESPACE = re.compile(r"\s+")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"https?://[^\s<>()\[\]]+")
_TRAILING_URL_PUNCTUATION = ".,;:!?\"'"


def _canonical_lines(value: str) -> str:
    protected: dict[str, str] = {}
    protected_value = value
    placeholder_codepoint = 0xE000
    for character in dict.fromkeys(value):
        if character.isspace() and character not in {" ", "\t", "\n", "\r"}:
            while chr(placeholder_codepoint) in value:
                placeholder_codepoint += 1
            placeholder = chr(placeholder_codepoint)
            placeholder_codepoint += 1
            protected[placeholder] = character
            protected_value = protected_value.replace(character, placeholder)
    normalized = unicodedata.normalize("NFKC", protected_value)
    for placeholder, character in protected.items():
        normalized = normalized.replace(placeholder, character)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_TRAILING_ASCII_WHITESPACE.sub("", line) for line in normalized.split("\n")]
    compact: list[str] = []
    blank_count = 0
    for line in lines:
        if line == "":
            blank_count += 1
            if blank_count <= 2:
                compact.append(line)
        else:
            blank_count = 0
            compact.append(line)
    while compact and compact[0] == "":
        compact.pop(0)
    while compact and compact[-1] == "":
        compact.pop()
    return "\n".join(compact)


def canonicalize_source_text(value: str) -> str:
    return _canonical_lines(value)


def canonicalize_body(value: str) -> str:
    return _canonical_lines(value)


def normalize_title(value: str) -> str:
    return _TITLE_WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())


def _valid_link(value: str) -> bool:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
    )


def extract_external_links(value: str) -> tuple[str, ...]:
    positioned: list[tuple[int, str]] = []
    for pattern in (_MARKDOWN_LINK, _BARE_URL):
        for match in pattern.finditer(value):
            raw = match.group(1) if match.lastindex else match.group(0)
            positioned.append((match.start(), raw.rstrip(_TRAILING_URL_PUNCTUATION)))
    seen: set[str] = set()
    links: list[str] = []
    for _, link in sorted(positioned, key=lambda item: item[0]):
        if link not in seen and _valid_link(link):
            seen.add(link)
            links.append(link)
    return tuple(links)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def source_hash(value: str) -> str:
    return hashlib.sha256(canonicalize_source_text(value).encode("utf-8")).hexdigest()


def payload_hash(*, title: str, markdown_body: str, external_links: Iterable[str]) -> str:
    return _digest(
        {
            "external_links": list(external_links),
            "markdown_body": canonicalize_body(markdown_body),
            "title": normalize_title(title),
        }
    )


def candidate_input_hash(
    *,
    title: str,
    markdown_body: str,
    external_links: Iterable[str],
    project_scope: str,
) -> str:
    links = sorted(set(external_links), key=lambda value: value.encode("utf-8"))
    return _digest(
        {
            "external_links": links,
            "markdown_body": canonicalize_body(markdown_body),
            "project_scope": unicodedata.normalize("NFKC", project_scope).strip(),
            "title": normalize_title(title),
        }
    )


def import_id_from_payload_hash(value: str) -> str:
    return base64.b32encode(bytes.fromhex(value)).decode("ascii").lower()[:26]


def visible_import_marker(import_id: str) -> str:
    return f"Agent2Pieces Import ID: {import_id}"


def render_dispatch_summary(
    *,
    markdown_body: str,
    source_agent: SourceAgent,
    import_id: str,
    mapped_project: str | None = None,
) -> str:
    names = {
        SourceAgent.CODEX: "Codex",
        SourceAgent.CLAUDE: "Claude Code",
        SourceAgent.HERMES: "Hermes",
    }
    footer = ["---", "Imported by Agent2Pieces", f"Source agent: {names[source_agent]}"]
    if mapped_project is not None:
        footer.append(f"Project: {mapped_project}")
    footer.append(visible_import_marker(import_id))
    return f"{canonicalize_body(markdown_body)}\n\n" + "\n".join(footer)
