from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import BinaryIO

import pytest


def _executable(artifact_dir: Path) -> Path:
    name = "agent2pieces.exe" if os.name == "nt" else "agent2pieces"
    matches = [path for path in artifact_dir.rglob(name) if path.is_file()]
    direct = artifact_dir / name
    if direct in matches:
        return direct
    if len(matches) != 1:
        pytest.fail(f"expected one {name} executable under {artifact_dir}, found {len(matches)}")
    return matches[0]


def _clean_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    data = tmp_path / "data"
    for path in (home, data):
        path.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "HERMES_HOME": str(home / ".hermes"),
            "AGENT2PIECES_DATA_DIR": str(data),
            "PYTHONHOME": str(tmp_path / "no-python-runtime"),
        }
    )
    environment.pop("PYTHONPATH", None)
    return environment


def _run(
    executable: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _json_line(output: str) -> dict[str, object]:
    lines = output.splitlines()
    assert len(lines) == 1
    assert len(lines[0].encode("utf-8")) <= 2_048
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value


def _readline_with_timeout(stream: BinaryIO, timeout: float) -> str:
    import queue
    import threading

    result: queue.Queue[bytes] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: result.put(stream.readline()), daemon=True)
    reader.start()
    try:
        return result.get(timeout=timeout).decode("utf-8")
    except queue.Empty:
        return ""


def _http_get(url: str, *, timeout: float = 10) -> tuple[int, str, str]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return (
                    response.status,
                    response.headers.get_content_type(),
                    response.read().decode("utf-8"),
                )
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            time.sleep(0.05)
    raise AssertionError(f"server did not answer {url}: {type(last_error).__name__}")


def test_packaged_health_check_runs_without_a_python_runtime(
    artifact_dir: Path,
    tmp_path: Path,
) -> None:
    executable = _executable(artifact_dir)
    clean_cwd = tmp_path / "cwd"
    clean_cwd.mkdir()

    result = _run(
        executable,
        ["--health-check"],
        cwd=clean_cwd,
        environment=_clean_environment(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    payload = _json_line(result.stdout)
    assert payload["status"] == "ok"
    assert payload["checks"] == {
        "database": True,
        "migrations": True,
        "static_assets": True,
    }
    assert payload["error_code"] is None


def test_packaged_scan_and_local_static_server_smoke(
    artifact_dir: Path,
    tmp_path: Path,
) -> None:
    executable = _executable(artifact_dir)
    clean_cwd = tmp_path / "cwd"
    clean_cwd.mkdir()
    environment = _clean_environment(tmp_path)
    source = tmp_path / "codex"
    source.mkdir()
    (source / "fixture.md").write_text(
        "---\nproject: packaged-smoke\n---\n# Native fixture\n\nRead from the fixture root.\n",
        encoding="utf-8",
    )

    scan = _run(
        executable,
        ["scan", "--source-root", f"codex={source}"],
        cwd=clean_cwd,
        environment=environment,
    )
    assert scan.returncode == 0, scan.stderr
    scan_payload = _json_line(scan.stdout)
    assert scan_payload["status"] == "ok"
    serialized_scan = json.dumps(scan_payload)
    assert '"accepted": 1' in serialized_scan
    assert '"discovered": 1' in serialized_scan

    process = subprocess.Popen(
        [
            str(executable),
            "serve",
            "--no-open",
            "--pieces-url",
            "http://127.0.0.1:9",
            "--source-root",
            f"codex={source}",
            "--source-root",
            f"codex={source}",
        ],
        cwd=clean_cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        line = _readline_with_timeout(process.stdout, 20)
        assert line, process.stderr.read().decode("utf-8", errors="replace")
        startup = _json_line(line)
        assert set(startup) == {"url", "port", "pid", "version", "ledger", "endpoint"}
        assert startup["url"] == f"http://127.0.0.1:{startup['port']}"
        assert startup["endpoint"] == "http://127.0.0.1:9"
        assert Path(str(startup["ledger"])).is_absolute()

        status, media_type, health_body = _http_get(f"{startup['url']}/health")
        assert status == 200
        assert media_type == "application/json"
        assert json.loads(health_body)["status"] == "ok"

        status, media_type, index = _http_get(str(startup["url"]))
        assert status == 200
        assert media_type == "text/html"
        assert "https://" not in index
        asset_paths = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index)
        assert len(asset_paths) == 2
        for asset_path in asset_paths:
            asset_status, _, asset_body = _http_get(f"{startup['url']}{asset_path}")
            assert asset_status == 200
            assert asset_body
    finally:
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode in {0, 130, -signal.SIGINT}


def _expected_platform_archive(artifact_dir: Path) -> Path:
    machine = platform.machine().lower()
    if os.name == "nt":
        pattern = "agent2pieces-v*-windows-x86_64.zip"
    elif platform.system() == "Darwin":
        assert machine in {"arm64", "aarch64"}
        pattern = "agent2pieces-v*-macos-arm64.tar.gz"
    else:
        assert machine in {"x86_64", "amd64"}
        pattern = "agent2pieces-v*-linux-x86_64.tar.gz"
    matches = sorted(artifact_dir.glob(pattern))
    if len(matches) != 1:
        pytest.fail(f"expected one {pattern} archive, found {len(matches)}")
    return matches[0]


def _expected_root(archive: Path) -> str:
    suffix = ".zip" if archive.suffix == ".zip" else ".tar.gz"
    return archive.name.removesuffix(suffix)


def test_native_archive_manifest_metadata_and_checksum(artifact_dir: Path) -> None:
    archive = _expected_platform_archive(artifact_dir)
    checksum = archive.with_name(f"{archive.name}.sha256")
    assert checksum.is_file()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="ascii") == f"{digest}  {archive.name}\n"
    root = _expected_root(archive)
    executable_name = "agent2pieces.exe" if archive.suffix == ".zip" else "agent2pieces"
    required = [
        f"{root}/LICENSE",
        f"{root}/README.md",
        f"{root}/THIRD_PARTY_NOTICES.txt",
        f"{root}/THIRD_PARTY_COMPONENTS.json",
    ]
    executable_member = f"{root}/{executable_name}"
    payloads: dict[str, bytes]

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            members = [info for info in bundle.infolist() if not info.is_dir()]
            names = [info.filename for info in members]
            payloads = {info.filename: bundle.read(info) for info in members}
            assert names[:4] == required
            assert names[-1] == executable_member
            assert any(name.startswith(f"{root}/third_party_licenses/") for name in names)
            for info in members:
                expected_mode = 0o755 if info.filename.endswith(executable_name) else 0o644
                assert stat.S_IMODE(info.external_attr >> 16) == expected_mode
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            members = [member for member in bundle.getmembers() if member.isfile()]
            names = [member.name for member in members]
            payloads = {}
            for member in members:
                extracted = bundle.extractfile(member)
                assert extracted is not None
                payloads[member.name] = extracted.read()
            assert names[:4] == required
            assert names[-1] == executable_member
            assert any(name.startswith(f"{root}/third_party_licenses/") for name in names)
            for member in members:
                expected_mode = 0o755 if member.name.endswith(executable_name) else 0o644
                assert member.mode == expected_mode
                assert member.uid == member.gid == 0
                assert member.uname == member.gname == ""
            assert archive.read_bytes()[4:8] == b"\x00\x00\x00\x00"

    notices = payloads[f"{root}/THIRD_PARTY_NOTICES.txt"].decode("utf-8")
    inventory = json.loads(payloads[f"{root}/THIRD_PARTY_COMPONENTS.json"])
    assert payloads[executable_member] == _executable(artifact_dir).read_bytes()
    assert inventory["schema_version"] == 1
    assert not any("readline" in name.lower() for name in inventory["artifact"]["binary_entries"])
    assert "_pytest" not in inventory["artifact"]["module_roots"]
    for component in inventory["components"]:
        assert f"Component: {component['name']} {component['version']}" in notices
        for license_file in component["license_files"]:
            content = payloads[f"{root}/{license_file['path']}"]
            assert hashlib.sha256(content).hexdigest() == license_file["sha256"]
