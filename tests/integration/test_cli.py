from __future__ import annotations

import json
import os
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent2pieces import __version__, cli


def _isolate_user_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    data_dir = tmp_path / "data"
    codex = home / ".codex"
    claude = home / ".claude"
    hermes = home / ".hermes"
    for path in (home, data_dir, codex, claude, hermes):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("AGENT2PIECES_DATA_DIR", str(data_dir))
    return data_dir


def _one_json_line(captured: pytest.CaptureFixture[str]) -> dict[str, object]:
    output = captured.readouterr().out
    lines = output.splitlines()
    assert len(lines) == 1
    assert len(lines[0].encode("utf-8")) <= 2_048
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value


def _iter_values(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_values(child)


def _find_count_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    for value in _iter_values(payload):
        if isinstance(value, dict) and key in value:
            candidate = value[key]
            if isinstance(candidate, dict):
                return candidate
    raise AssertionError(f"missing count mapping for {key}")


@pytest.mark.parametrize(
    "arguments",
    [
        ["watch"],
        ["serve", "--host", "0.0.0.0"],
        ["serve", "--host", "localhost"],
        ["serve", "--pieces-url", "ftp://pieces.test"],
        ["serve", "--pieces-url", "http://user:password@pieces.test"],
        ["serve", "--port", "0"],
        ["serve", "--port", "65536"],
        ["scan", "--pieces-url", "http://pieces.test"],
        ["scan", "--source-root", "cursor=/absolute/path"],
        ["scan", "--source-root", "Codex=/absolute/path"],
        ["scan", "--source-root", "codex="],
        ["scan", "--source-root", "codex=relative/path"],
        ["--health-check", "--no-open"],
    ],
)
def test_cli_rejects_unlisted_commands_options_and_invalid_values(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolate_user_state(monkeypatch, tmp_path)

    assert cli.main(arguments) == 2


def test_cli_rejects_missing_file_and_unreadable_source_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolate_user_state(monkeypatch, tmp_path)
    missing = tmp_path / "missing"
    regular_file = tmp_path / "memory.md"
    regular_file.write_text("# Not a root\n", encoding="utf-8")

    assert cli.main(["scan", "--source-root", f"codex={missing}"]) == 2
    assert cli.main(["scan", "--source-root", f"codex={regular_file}"]) == 2

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    real_access = os.access

    def deny_target(path: os.PathLike[str] | str, mode: int) -> bool:
        if Path(path).resolve(strict=False) == unreadable.resolve():
            return False
        return real_access(path, mode)

    monkeypatch.setattr("agent2pieces.config.os.access", deny_target)
    assert cli.main(["scan", "--source-root", f"codex={unreadable}"]) == 2


def test_scan_coalesces_repeatable_roots_reports_counts_and_does_not_save_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _isolate_user_state(monkeypatch, tmp_path)
    root = tmp_path / "codex"
    root.mkdir()
    (root / "decision.md").write_text(
        "---\nproject: agent2pieces\n---\n# Keep CLI scans explicit\n\nOne memory.\n",
        encoding="utf-8",
    )
    (root / "raw.jsonl").write_text('{"raw":true}\n', encoding="utf-8")
    source_arg = f"codex={root}"

    assert cli.main(["scan", "--source-root", source_arg, "--source-root", source_arg]) == 0
    payload = _one_json_line(capsys)

    assert payload.get("status") == "ok"
    total = _find_count_mapping(payload, "total")
    codex = _find_count_mapping(payload, "codex")
    assert total == {"discovered": 2, "accepted": 1, "excluded": 1, "quarantined": 0}
    assert codex == total
    safe_text = json.dumps(payload, sort_keys=True)
    assert "excluded_raw" in safe_text
    assert "raw.jsonl" in safe_text
    assert '{"raw":true}' not in safe_text

    databases = list(data_dir.rglob("*.sqlite3"))
    assert len(databases) == 1
    with sqlite3.connect(databases[0]) as connection:
        version, base_url = connection.execute(
            "SELECT version, mcp_base_url FROM settings WHERE singleton_id = 1"
        ).fetchone()
        saved_exactly = connection.execute(
            "SELECT COUNT(*) FROM source_roots WHERE lexical_path = ?", (str(root),)
        ).fetchone()[0]
    assert version == 1
    assert base_url == "http://127.0.0.1:39300"
    assert saved_exactly == 0


def test_health_check_is_disposable_bounded_and_performs_no_external_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _isolate_user_state(monkeypatch, tmp_path)

    def prohibited(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("health check attempted prohibited I/O")

    monkeypatch.setattr("socket.socket", prohibited)
    monkeypatch.setattr("webbrowser.open", prohibited)
    monkeypatch.setattr("agent2pieces.mcp_client.PiecesMcpClient.connect", prohibited)
    monkeypatch.setattr("agent2pieces.scanners.scan_codex_root", prohibited)
    monkeypatch.setattr("agent2pieces.scanners.scan_claude_root", prohibited)
    monkeypatch.setattr("agent2pieces.scanners.scan_hermes_root", prohibited)

    assert cli.main(["--health-check"]) == 0
    payload = _one_json_line(capsys)

    assert payload == {
        "status": "ok",
        "version": __version__,
        "checks": {"database": True, "migrations": True, "static_assets": True},
        "error_code": None,
    }
    assert list(data_dir.iterdir()) == []
    serialized = json.dumps(payload)
    assert str(tmp_path) not in serialized


def test_health_check_reports_sanitized_database_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_user_state(monkeypatch, tmp_path)

    def fail_initialize(_self: object) -> None:
        raise OSError(f"sensitive path: {tmp_path / 'do-not-print'}")

    monkeypatch.setattr("agent2pieces.ledger.Ledger.initialize", fail_initialize)

    assert cli.main(["--health-check"]) == 1
    payload = _one_json_line(capsys)
    assert payload["status"] == "error"
    assert payload["error_code"] == "database_failed"
    assert payload["checks"] == {
        "database": False,
        "migrations": False,
        "static_assets": False,
    }
    assert str(tmp_path) not in json.dumps(payload)


def test_omitted_port_is_a_prebound_race_free_loopback_socket() -> None:
    listener = cli.bind_listener(None)
    try:
        host, port = listener.getsockname()[:2]
        assert host == "127.0.0.1"
        assert 1 <= port <= 65_535
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competitor,
            pytest.raises(OSError),
        ):
            competitor.bind(("127.0.0.1", port))
    finally:
        listener.close()


def test_occupied_explicit_port_is_operational_failure_before_pieces_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolate_user_state(monkeypatch, tmp_path)

    async def reject_connection(_self: object) -> None:
        raise AssertionError("occupied-port failure attempted a Pieces connection")

    monkeypatch.setattr(
        "agent2pieces.mcp_client.PiecesMcpClient.connect", reject_connection
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        assert cli.main(["serve", "--no-open", "--port", str(port)]) == 1


def test_scan_operational_failure_returns_one_and_sanitizes_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_user_state(monkeypatch, tmp_path)
    root = tmp_path / "codex"
    root.mkdir()

    def fail_initialize(_self: object) -> None:
        raise OSError(f"private detail: {tmp_path / 'must-not-escape'}")

    monkeypatch.setattr("agent2pieces.ledger.Ledger.initialize", fail_initialize)

    assert cli.main(["scan", "--source-root", f"codex={root}"]) == 1
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err


def test_browser_open_policy_is_exactly_once_or_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def record(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr("webbrowser.open", record)

    cli.maybe_open_browser("http://127.0.0.1:43123", no_open=False)
    assert opened == ["http://127.0.0.1:43123"]
    cli.maybe_open_browser("http://127.0.0.1:43124", no_open=True)
    assert opened == ["http://127.0.0.1:43123"]


def test_pieces_url_override_is_validated_without_mutating_saved_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = _isolate_user_state(monkeypatch, tmp_path)
    root = tmp_path / "empty-codex"
    root.mkdir()
    assert cli.main(["scan", "--source-root", f"codex={root}"]) == 0
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        assert (
            cli.main(
                [
                    "serve",
                    "--no-open",
                    "--port",
                    str(port),
                    "--pieces-url",
                    "https://pieces.example.test",
                ]
            )
            == 1
        )
    finally:
        listener.close()

    databases = list(data_dir.rglob("*.sqlite3"))
    assert len(databases) == 1
    with sqlite3.connect(databases[0]) as connection:
        version, base_url = connection.execute(
            "SELECT version, mcp_base_url FROM settings WHERE singleton_id = 1"
        ).fetchone()
    assert version == 1
    assert base_url == "http://127.0.0.1:39300"


def test_serve_uses_saved_endpoint_when_cli_override_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = _isolate_user_state(monkeypatch, tmp_path)
    assert cli.main(["scan"]) == 0
    database = next(data_dir.rglob("*.sqlite3"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE settings SET mcp_base_url = ? WHERE singleton_id = 1",
            ("http://saved.example.test:4100",),
        )

    captured: dict[str, object] = {}

    async def capture_serve(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_serve_async", capture_serve)

    assert cli.main(["serve", "--no-open"]) == 0
    assert captured["pieces_url"] == "http://saved.example.test:4100"
    assert captured["pieces_url_source"] == "saved"
