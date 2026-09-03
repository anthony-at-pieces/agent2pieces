from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent2pieces.config import (
    DEFAULT_MCP_BASE_URL,
    SourceRoot,
    coalesce_source_roots,
    default_source_roots,
    resolve_source_root,
    validate_pieces_url,
)
from agent2pieces.models import SourceAgent


def test_default_roots_honor_environment_and_expand_claude_projects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    hermes_home = tmp_path / "hermes-home"
    codex_root = codex_home / "memories" / "rollout_summaries"
    claude_one = claude_home / "projects" / "one" / "memory"
    claude_two = claude_home / "projects" / "two" / "memory"
    claude_custom = tmp_path / "claude-custom-memory"
    hermes_root = hermes_home / "memories"
    for path in (codex_root, claude_one, claude_two, claude_custom, hermes_root):
        path.mkdir(parents=True)
    (claude_home / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(claude_custom)}),
        encoding="utf-8",
    )

    roots = default_source_roots(
        home=home,
        environ={
            "CODEX_HOME": str(codex_home),
            "CLAUDE_CONFIG_DIR": str(claude_home),
            "HERMES_HOME": str(hermes_home),
        },
    )

    assert [(root.agent, root.resolved_path) for root in roots] == [
        (SourceAgent.CODEX, codex_root.resolve()),
        (SourceAgent.CLAUDE, claude_one.resolve()),
        (SourceAgent.CLAUDE, claude_two.resolve()),
        (SourceAgent.CLAUDE, claude_custom.resolve()),
        (SourceAgent.HERMES, hermes_root.resolve()),
    ]
    assert all(root.enabled and root.is_default for root in roots)


def test_default_roots_use_home_when_environment_is_absent(tmp_path: Path) -> None:
    roots = default_source_roots(home=tmp_path, environ={})

    assert roots == (
        SourceRoot(
            agent=SourceAgent.CODEX,
            lexical_path=tmp_path / ".codex" / "memories" / "rollout_summaries",
            resolved_path=tmp_path / ".codex" / "memories" / "rollout_summaries",
            enabled=True,
            is_default=True,
        ),
        SourceRoot(
            agent=SourceAgent.HERMES,
            lexical_path=tmp_path / ".hermes" / "memories",
            resolved_path=tmp_path / ".hermes" / "memories",
            enabled=True,
            is_default=True,
        ),
    )


def test_public_default_and_strict_pieces_url_validation() -> None:
    assert DEFAULT_MCP_BASE_URL == "http://127.0.0.1:39300"
    assert validate_pieces_url("https://pieces.example.test/root/") == (
        "https://pieces.example.test/root"
    )

    for value in (
        "ftp://pieces.example.test",
        "http://user:password@pieces.example.test",
        "http://pieces.example.test?secret=value",
        "http://pieces.example.test#fragment",
        " http://pieces.example.test",
        "http://pieces.example.test/path with spaces",
        "http://pieces.example.test\\@other.example.test",
        "http://pieces.example.test:70000",
    ):
        with pytest.raises(ValueError):
            validate_pieces_url(value)


def test_configured_roots_are_absolute_resolved_and_coalesced(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    configured = resolve_source_root(SourceAgent.CODEX, alias)
    duplicate = resolve_source_root(SourceAgent.CODEX, actual)
    other_agent = resolve_source_root(SourceAgent.HERMES, actual)

    assert configured.lexical_path == alias.absolute()
    assert configured.resolved_path == actual.resolve()
    assert coalesce_source_roots((configured, duplicate, other_agent)) == (
        configured,
        other_agent,
    )


@pytest.mark.parametrize("relative", [Path("relative"), Path("../escape")])
def test_configured_roots_reject_relative_paths(relative: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_source_root(SourceAgent.CLAUDE, relative)


def test_configured_roots_reject_missing_files_and_unreadable_directories(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    regular_file = tmp_path / "file"
    regular_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="exist"):
        resolve_source_root(SourceAgent.CODEX, missing)
    with pytest.raises(ValueError, match="directory"):
        resolve_source_root(SourceAgent.CODEX, regular_file)
