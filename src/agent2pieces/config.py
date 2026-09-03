"""Source-root discovery and validation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from agent2pieces.models import SourceAgent

DEFAULT_MCP_BASE_URL = "http://127.0.0.1:39300"
_MAX_CLAUDE_SETTINGS_BYTES = 1_048_576


@dataclass(frozen=True)
class SourceRoot:
    agent: SourceAgent
    lexical_path: Path
    resolved_path: Path
    enabled: bool = True
    is_default: bool = False


def resolve_source_root(
    agent: SourceAgent,
    path: Path,
    *,
    enabled: bool = True,
    is_default: bool = False,
) -> SourceRoot:
    if not path.is_absolute():
        raise ValueError("source root must be absolute")
    lexical = path.absolute()
    if not lexical.exists():
        raise ValueError("source root does not exist")
    if not lexical.is_dir():
        raise ValueError("source root must be a directory")
    if not os.access(lexical, os.R_OK | os.X_OK):
        raise ValueError("source root is not readable")
    return SourceRoot(agent, lexical, lexical.resolve(), enabled, is_default)


def coalesce_source_roots(roots: Iterable[SourceRoot]) -> tuple[SourceRoot, ...]:
    seen: set[tuple[SourceAgent, Path]] = set()
    result: list[SourceRoot] = []
    for root in roots:
        key = (root.agent, root.resolved_path)
        if key not in seen:
            seen.add(key)
            result.append(root)
    return tuple(result)


def validate_pieces_url(value: str) -> str:
    """Validate and normalize a credential-free Pieces HTTP base URL."""

    if (
        not value
        or value != value.strip()
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("Pieces URL must be a credential-free HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid Pieces URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError("Pieces URL must be a credential-free HTTP(S) URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _configured_claude_memory_roots(
    claude_home: Path,
    *,
    home: Path,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    settings_path = claude_home / "settings.json"
    try:
        if settings_path.stat().st_size > _MAX_CLAUDE_SETTINGS_BYTES:
            return ()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(settings, dict):
        return ()
    configured = settings.get("autoMemoryDirectory")
    if not isinstance(configured, str) or not configured.strip():
        return ()
    path_value = configured.strip()
    if path_value == "~":
        path = home
    elif path_value.startswith(("~/", "~\\")):
        path = home / path_value[2:]
    else:
        path = Path(path_value)
        if not path.is_absolute():
            return ()
    if path.is_dir():
        roots.append(path)
    return tuple(roots)


def default_source_roots(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[SourceRoot, ...]:
    environment = os.environ if environ is None else environ
    home_path = Path.home() if home is None else home
    codex_home = Path(environment.get("CODEX_HOME", home_path / ".codex"))
    claude_home = Path(environment.get("CLAUDE_CONFIG_DIR", home_path / ".claude"))
    hermes_home = Path(environment.get("HERMES_HOME", home_path / ".hermes"))

    candidates: list[tuple[SourceAgent, Path]] = [
        (SourceAgent.CODEX, codex_home / "memories" / "rollout_summaries"),
    ]
    projects = claude_home / "projects"
    if projects.is_dir():
        candidates.extend(
            (SourceAgent.CLAUDE, path)
            for path in sorted(projects.glob("*/memory"))
            if path.is_dir()
        )
    candidates.extend(
        (SourceAgent.CLAUDE, path)
        for path in _configured_claude_memory_roots(claude_home, home=home_path)
    )
    candidates.append((SourceAgent.HERMES, hermes_home / "memories"))
    roots: list[SourceRoot] = []
    for agent, path in candidates:
        if agent is SourceAgent.CLAUDE and not path.is_dir():
            continue
        lexical = path.absolute()
        roots.append(
            SourceRoot(
                agent=agent,
                lexical_path=lexical,
                resolved_path=lexical.resolve(),
                enabled=True,
                is_default=True,
            )
        )
    return coalesce_source_roots(roots)
