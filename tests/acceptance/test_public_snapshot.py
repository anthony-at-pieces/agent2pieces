from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
EXPORTER = ROOT / "scripts" / "export_public_snapshot.py"
ACCEPTANCE_DOCUMENTATION = "documentation" + "/acceptance"
LIVE_INVENTORY_SCRIPT = "check_" + "live_inventory.py"
EVIDENCE_SCRIPT = "verify_" + "acceptance_evidence.py"

EXPORTER_SPEC = importlib.util.spec_from_file_location("agent2pieces_public_export", EXPORTER)
assert EXPORTER_SPEC is not None and EXPORTER_SPEC.loader is not None
EXPORTER_MODULE = importlib.util.module_from_spec(EXPORTER_SPEC)
EXPORTER_SPEC.loader.exec_module(EXPORTER_MODULE)

EXPECTED_TOP_LEVEL = {
    ".github",
    ".gitignore",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "agent2pieces.spec",
    "pyproject.toml",
    "scripts",
    "specs",
    "src",
    "tests",
    "third_party",
    "uv.lock",
}

EXCLUDED_PARTS = {
    ".agentic",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "audit_trails",
    "build",
    "dist",
    "playwright-report",
    "reviews",
    "test-results",
}


def _run_export(repository: Path, output: Path) -> subprocess.CompletedProcess[str]:
    exporter = repository / "scripts" / "export_public_snapshot.py"
    return subprocess.run(
        [sys.executable, str(exporter), str(output)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _export(repository: Path, output: Path) -> dict[str, object]:
    result = _run_export(repository, output)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def tracked_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    files: dict[str, str] = {
        ".github/workflows/test.yml": "name: test\n",
        ".gitignore": "*.local\n",
        "CONTRIBUTING.md": "# Contributing\n",
        "LICENSE": "Synthetic test license.\n",
        "README.md": "# Synthetic public tree\n",
        "SECURITY.md": "# Security\n",
        "agent2pieces.spec": "# synthetic spec\n",
        "pyproject.toml": "[project]\nname = 'synthetic-public-tree'\n",
        "scripts/release_artifacts.py": "# synthetic release helper\n",
        "scripts/reviews/private.md": "excluded review\n",
        "specs/agent2pieces-v1/requirements.md": "# Requirements\n",
        "src/.agentic/private.md": "excluded agent state\n",
        "src/agent2pieces/__init__.py": "__version__ = '0.0.0'\n",
        "tests/unit/test_placeholder.py": "def test_placeholder():\n    assert True\n",
        "third_party/release_components.json": "{\"components\": []}\n",
        "uv.lock": "version = 1\n",
    }
    for relative, content in files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    exporter = repository / "scripts" / "export_public_snapshot.py"
    shutil.copyfile(EXPORTER, exporter)
    subprocess.run(["git", "init", "-q", str(repository)], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repository), "add", "--all"],
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Synthetic Test",
            "-c",
            "user.email=person@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        timeout=30,
    )
    return repository


def _tree_identity(root: Path) -> str:
    digest = hashlib.sha256()
    entries = [root, *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())]
    for path in entries:
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            digest.update(b"directory\0")
            content_digest = b""
        else:
            digest.update(b"file\0")
            content_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(content_digest)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _files(root)
    }


def test_python_source_distribution_is_package_scoped() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["tool"]["hatch"]["build"]["targets"]["sdist"] == {
        "only-include": ["src/agent2pieces"]
    }


def test_public_snapshot_is_allowlisted_sanitized_and_deterministic(
    tmp_path: Path,
    tracked_repository: Path,
) -> None:
    first = tmp_path / "public-one"
    second = tmp_path / "public-two"

    first_run = _run_export(tracked_repository, first)
    if first_run.returncode != 0:
        assert "cannot preserve normalized" in first_run.stderr
        assert not first.exists()
        return
    first_result = json.loads(first_run.stdout)
    second_result = _export(tracked_repository, second)

    assert first_result["file_count"] == len(_files(first))
    assert second_result["file_count"] == len(_files(second))
    assert {path.name for path in first.iterdir()} == EXPECTED_TOP_LEVEL
    assert _digests(first) == _digests(second)
    assert first_result["tree_sha256"] == _tree_identity(first)
    assert second_result["tree_sha256"] == _tree_identity(second)

    relative_paths = [path.relative_to(first) for path in _files(first)]
    assert all(not (set(path.parts) & EXCLUDED_PARTS) for path in relative_paths)
    assert not (first / "documentation").exists()
    assert not (first / ".git").exists()
    assert not (first / "scripts" / LIVE_INVENTORY_SCRIPT).exists()
    assert not (first / "scripts" / EVIDENCE_SCRIPT).exists()
    assert not (first / "specs" / "agent2pieces-v1" / "tasks.md").exists()
    assert not (first / "tests" / "acceptance" / "test_acceptance_evidence.py").exists()
    assert not (first / "tests" / "acceptance" / "test_live_inventory.py").exists()
    assert all(not path.is_symlink() for path in first.rglob("*"))

    retained_tests = b"\n".join(
        path.read_bytes() for path in _files(first / "tests") if path.suffix == ".py"
    ).lower()
    assert ACCEPTANCE_DOCUMENTATION.encode() not in retained_tests
    assert LIVE_INVENTORY_SCRIPT.removesuffix(".py").encode() not in retained_tests
    assert EVIDENCE_SCRIPT.removesuffix(".py").encode() not in retained_tests

    combined = b"\n".join(path.read_bytes() for path in _files(first))
    assert EXPORTER_MODULE.unsafe_content_reason(combined) is None

    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in _files(first))
    directories = [first, *(path for path in first.rglob("*") if path.is_dir())]
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o755
        for path in directories
    )


def test_public_snapshot_refuses_existing_or_nested_destinations(
    tmp_path: Path,
    tracked_repository: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    result = _run_export(tracked_repository, existing)
    assert result.returncode == 2
    assert "already exists" in result.stderr

    nested = tracked_repository / "public-export-must-not-be-created"
    result = _run_export(tracked_repository, nested)
    assert result.returncode == 2
    assert "outside the source repository" in result.stderr
    assert not nested.exists()


@pytest.mark.parametrize("relative", ("src/.env", "tests/private-note.txt"))
def test_public_snapshot_refuses_nonignored_untracked_files(
    tmp_path: Path,
    tracked_repository: Path,
    relative: str,
) -> None:
    untracked = tracked_repository / relative
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("private local content\n", encoding="utf-8")
    output = tmp_path / "refused-untracked"

    result = _run_export(tracked_repository, output)

    assert result.returncode == 2
    assert "differ from HEAD" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("staged", (False, True), ids=("unstaged", "staged"))
def test_public_snapshot_refuses_tracked_changes(
    tmp_path: Path,
    tracked_repository: Path,
    staged: bool,
) -> None:
    source = tracked_repository / "src" / "agent2pieces" / "__init__.py"
    source.write_text("__version__ = 'changed'\n", encoding="utf-8")
    if staged:
        subprocess.run(
            ["git", "-C", str(tracked_repository), "add", str(source)],
            check=True,
            timeout=30,
        )
    output = tmp_path / "refused-change"

    result = _run_export(tracked_repository, output)

    assert result.returncode == 2
    assert "differ from HEAD" in result.stderr
    assert not output.exists()


def test_public_snapshot_ignores_gitignored_untracked_files(
    tracked_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ignored = tracked_repository / "src" / "generated.local"
    ignored.write_text("ignored local content\n", encoding="utf-8")
    monkeypatch.setattr(EXPORTER_MODULE, "ROOT", tracked_repository)

    _, sources = EXPORTER_MODULE._source_files()
    relative_sources = {path.relative_to(tracked_repository).as_posix() for path in sources}

    assert "src/generated.local" not in relative_sources


def test_public_snapshot_writes_the_validated_snapshot_after_source_replacement(
    tmp_path: Path,
    tracked_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = Path("src/agent2pieces/__init__.py")
    source = tracked_repository / relative
    original = subprocess.run(
        ["git", "-C", str(tracked_repository), "show", f"HEAD:{relative.as_posix()}"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    replacement = b"api_" + b"key=replacement-must-not-be-exported"
    output = tmp_path / "public"
    validate_contents = EXPORTER_MODULE._validate_contents

    def replace_source_after_validation(snapshots: object) -> None:
        validate_contents(snapshots)
        source.write_bytes(replacement)

    monkeypatch.setattr(EXPORTER_MODULE, "ROOT", tracked_repository)
    monkeypatch.setattr(EXPORTER_MODULE, "_validate_contents", replace_source_after_validation)
    if os.name == "nt":
        # This test isolates immutable blob reuse. NTFS does not expose the
        # normalized POSIX modes that the real exporter intentionally requires.
        monkeypatch.setattr(EXPORTER_MODULE, "_normalize_file", lambda _path: None)
        monkeypatch.setattr(EXPORTER_MODULE, "_normalize_directories", lambda _root: None)

    EXPORTER_MODULE.export_snapshot(output)

    assert (output / relative).read_bytes() == original
    assert replacement not in (output / relative).read_bytes()


def test_public_snapshot_refuses_a_link_like_tracked_path_component(
    tmp_path: Path,
    tracked_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "refused-link-like-component"
    link_like = tracked_repository / "src" / "agent2pieces"
    is_link_or_junction = EXPORTER_MODULE._is_link_or_junction

    monkeypatch.setattr(EXPORTER_MODULE, "ROOT", tracked_repository)
    monkeypatch.setattr(
        EXPORTER_MODULE,
        "_is_link_or_junction",
        lambda path: path == link_like or is_link_or_junction(path),
    )

    with pytest.raises(
        EXPORTER_MODULE.ExportError,
        match="tracked public source contains a symlink or junction",
    ):
        EXPORTER_MODULE.export_snapshot(output)

    assert not output.exists()


@pytest.mark.skipif(os.name == "nt", reason="native symlink fixture requires POSIX")
def test_public_snapshot_refuses_a_committed_git_symlink(
    tmp_path: Path,
    tracked_repository: Path,
) -> None:
    link = tracked_repository / "src" / "tracked-link"
    link.symlink_to("agent2pieces")
    subprocess.run(
        ["git", "-C", str(tracked_repository), "add", str(link)],
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tracked_repository),
            "-c",
            "user.name=Synthetic Test",
            "-c",
            "user.email=person@example.test",
            "commit",
            "-qm",
            "add link fixture",
        ],
        check=True,
        timeout=30,
    )
    output = tmp_path / "refused-git-symlink"

    result = _run_export(tracked_repository, output)

    assert result.returncode == 2
    assert "non-regular entry" in result.stderr
    assert not output.exists()


def test_public_snapshot_refuses_nonpreserving_output_filesystem(
    tmp_path: Path,
    tracked_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "refused-mode"
    real_chmod = os.chmod

    def set_wrong_mode(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del mode
        real_chmod(path, 0o600, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(EXPORTER_MODULE, "ROOT", tracked_repository)
    monkeypatch.setattr(os, "chmod", set_wrong_mode)

    with pytest.raises(EXPORTER_MODULE.ExportError, match="cannot preserve normalized file modes"):
        EXPORTER_MODULE.export_snapshot(output)

    assert not output.exists()
    assert not list(tmp_path.glob(".refused-mode-*"))


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"http://" + b"buildbox:39300/path", "single_label_http_host"),
        (b"/ho" + b"me/" + b"sample-user/project/", "personal_path"),
        (b"/Us" + b"ers/" + b"sample-user/project/", "personal_path"),
        (b"/mnt/" + b"c/Users/" + b"sample-user/project/", "personal_path"),
        (b"C:\\" + b"Users\\" + b"sample-user\\project", "personal_path"),
        (b"developer" + b"@" + b"company.invalid", "email_address"),
        (b"client_" + b"secret=" + b"placeholder-value", "credential_signature"),
        (b"AK" + b"IA" + b"ABCDEFGHIJKLMNOP", "credential_signature"),
    ],
    ids=(
        "single-label-http-host",
        "linux-home",
        "macos-home",
        "wsl-home",
        "windows-home",
        "email",
        "credential-assignment",
        "provider-token",
    ),
)
def test_public_snapshot_generic_content_guard(content: bytes, reason: str) -> None:
    assert EXPORTER_MODULE.unsafe_content_reason(content) == reason


def test_public_snapshot_generic_content_guard_allows_public_examples() -> None:
    content = b"\n".join(
        (
            b"http://localhost:39300",
            b"https://pieces.app/",
            b"person@example.test",
            b"/absolute/path/to/snapshot",
        )
    )

    assert EXPORTER_MODULE.unsafe_content_reason(content) is None
