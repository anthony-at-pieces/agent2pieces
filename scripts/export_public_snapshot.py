"""Export an allowlisted, history-free Agent2Pieces source snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
NORMALIZED_MTIME = 315532800
NORMALIZED_FILE_MODE = 0o644
NORMALIZED_DIRECTORY_MODE = 0o755

PUBLIC_PATHS = (
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
)

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
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
        "documentation",
        "playwright-report",
        "reviews",
        "test-results",
    }
)
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_RELATIVE_PATHS = frozenset(
    {
        "scripts/check_live_inventory.py",
        "scripts/verify_acceptance_evidence.py",
        "specs/agent2pieces-v1/tasks.md",
        "tests/acceptance/test_acceptance_evidence.py",
        "tests/acceptance/test_live_inventory.py",
    }
)

_PERSONAL_PATH_PATTERNS = (
    re.compile(rb"/mnt/[a-z]/Users/[-A-Za-z0-9._][^/\s]*/", re.IGNORECASE),
    re.compile(rb"/(?:home|Users)/[-A-Za-z0-9._][^/\s]*/"),
    re.compile(rb"[A-Za-z]:\\Users\\[-A-Za-z0-9._][^\\\s]*\\", re.IGNORECASE),
)
_EMAIL_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9._%+-])"
    rb"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
    rb"(?![A-Za-z0-9._%+-])",
    re.IGNORECASE,
)
_SAFE_EMAIL_DOMAINS = frozenset({b"example.com", b"example.net", b"example.org"})
_URL_PATTERN = re.compile(rb"https?://[^\s\"'<>`]+", re.IGNORECASE)
_SAFE_SINGLE_LABEL_HOSTS = frozenset({"localhost"})
_CREDENTIAL_PATTERNS = (
    re.compile(rb"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"),
    re.compile(rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"),
    re.compile(rb"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"),
    re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(
        rb"(?<![A-Za-z0-9_])"
        rb"(?:api_key|apikey|access_token|client_secret|password)"
        rb"[ \t]*[:=][ \t]*[^\s\"']{8,}",
        re.IGNORECASE,
    ),
    re.compile(rb"eyJ[A-Za-z0-9_-]{17,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{8,}"),
)


class ExportError(RuntimeError):
    """The requested public snapshot cannot be produced safely."""


def _is_excluded_file(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    relative_parts = set(Path(relative).parts)
    return (
        bool(relative_parts & EXCLUDED_DIRECTORY_NAMES)
        or path.suffix.lower() in EXCLUDED_FILE_SUFFIXES
        or relative in EXCLUDED_RELATIVE_PATHS
    )


def _run_git(*arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *arguments],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExportError("cannot inspect Git source identity") from error
    if result.returncode != 0:
        raise ExportError("source root is not an inspectable Git working tree")
    return result.stdout


def _decode_git_path(encoded: bytes) -> Path:
    try:
        value = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportError("public Git path is not valid UTF-8") from error
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ExportError("Git returned an unsafe public path")
    return path


def _git_tree_files(commit: str, pathspecs: tuple[str, ...]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for record in _run_git("ls-tree", "-r", "-z", commit, *pathspecs).split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, object_type, _object_id = metadata.split(b" ", 2)
        except ValueError as error:
            raise ExportError("Git returned malformed public tree metadata") from error
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ExportError("public Git tree contains a non-regular entry")
        paths.append(_decode_git_path(encoded_path))
    return tuple(paths)


def _validate_git_root() -> None:
    encoded = _run_git("rev-parse", "--show-toplevel").strip()
    try:
        git_root = Path(encoded.decode("utf-8")).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as error:
        raise ExportError("Git source root cannot be resolved") from error
    if git_root != ROOT.resolve(strict=True):
        raise ExportError("exporter must run from the Git working-tree root")


def _head_commit() -> str:
    encoded = _run_git("rev-parse", "--verify", "HEAD").strip()
    try:
        commit = encoded.decode("ascii")
    except UnicodeDecodeError as error:
        raise ExportError("Git HEAD identity is not ASCII") from error
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit) is None:
        raise ExportError("Git HEAD identity is not a full object ID")
    return commit


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction is not None and is_junction())


def _validate_tracked_file(relative: Path) -> Path:
    current = ROOT
    for part in relative.parts:
        current = current / part
        if _is_link_or_junction(current):
            raise ExportError("tracked public source contains a symlink or junction")
    if not current.is_file():
        raise ExportError("tracked public source is missing or is not a regular file")
    try:
        current.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ExportError("tracked public source escapes the Git root") from error
    return current


def _source_files() -> tuple[str, tuple[Path, ...]]:
    _validate_git_root()
    commit = _head_commit()
    pathspecs = ("--", *PUBLIC_PATHS)
    dirty = _run_git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--ignore-submodules=none",
        *pathspecs,
    )
    if dirty:
        raise ExportError(
            "public roots differ from HEAD; commit or ignore local files before export"
        )
    if _head_commit() != commit:
        raise ExportError("Git HEAD changed while preparing the public snapshot")

    tracked = _git_tree_files(commit, pathspecs)
    retained_paths = tuple(path for path in tracked if not _is_excluded_file(ROOT / path))
    retained_names = {path.as_posix() for path in retained_paths}
    for relative_name in PUBLIC_PATHS:
        source = ROOT / relative_name
        if not source.exists() or _is_link_or_junction(source):
            raise ExportError(f"required public path is missing or unsafe: {relative_name}")
        if source.is_file():
            present = relative_name in retained_names
        else:
            present = any(path.parts[0] == relative_name for path in retained_paths)
        if not present:
            raise ExportError(f"required public path is not Git tracked: {relative_name}")

    files = tuple(_validate_tracked_file(path) for path in retained_paths)
    ordered = tuple(sorted(files, key=lambda item: item.relative_to(ROOT).as_posix()))
    return commit, ordered


def unsafe_content_reason(content: bytes) -> str | None:
    if any(pattern.search(content) for pattern in _PERSONAL_PATH_PATTERNS):
        return "personal_path"
    for match in _EMAIL_PATTERN.finditer(content):
        domain = match.group(0).rsplit(b"@", 1)[1].lower()
        if not (domain.endswith(b".test") or domain in _SAFE_EMAIL_DOMAINS):
            return "email_address"
    for match in _URL_PATTERN.finditer(content):
        try:
            host = urlsplit(match.group(0).decode("ascii")).hostname
        except (UnicodeDecodeError, ValueError):
            continue
        if (
            host is not None
            and "." not in host
            and ":" not in host
            and host.casefold() not in _SAFE_SINGLE_LABEL_HOSTS
        ):
            return "single_label_http_host"
    if any(pattern.search(content) for pattern in _CREDENTIAL_PATTERNS):
        return "credential_signature"
    return None


def _snapshot_files(commit: str, files: Iterable[Path]) -> Mapping[Path, bytes]:
    snapshots: dict[Path, bytes] = {}
    for path in files:
        relative = path.relative_to(ROOT)
        snapshots[relative] = _run_git("show", f"{commit}:{relative.as_posix()}")
    return MappingProxyType(snapshots)


def _validate_contents(snapshots: Mapping[Path, bytes]) -> None:
    for relative, content in snapshots.items():
        reason = unsafe_content_reason(content)
        if reason is not None:
            raise ExportError(
                "allowlisted source contains a private distribution indicator: "
                f"{relative} ({reason})"
            )


def _set_and_verify_mode(path: Path, mode: int, kind: str) -> None:
    path.chmod(mode)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise ExportError(f"output filesystem cannot preserve normalized {kind} modes")


def _normalize_file(path: Path) -> None:
    _set_and_verify_mode(path, NORMALIZED_FILE_MODE, "file")
    os.utime(path, (NORMALIZED_MTIME, NORMALIZED_MTIME))


def _normalize_directories(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    directories.append(root)
    for path in directories:
        _set_and_verify_mode(path, NORMALIZED_DIRECTORY_MODE, "directory")
        os.utime(path, (NORMALIZED_MTIME, NORMALIZED_MTIME))


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    entries = [root, *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())]
    for path in entries:
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            digest.update(b"directory\0")
            content_digest = b""
        elif path.is_file():
            digest.update(b"file\0")
            content_digest = hashlib.sha256(path.read_bytes()).digest()
        else:
            raise ExportError("normalized snapshot contains an unsupported file type")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(content_digest)
    return digest.hexdigest()


def export_snapshot(output: Path) -> tuple[int, str]:
    output = output.expanduser().resolve(strict=False)
    root = ROOT.resolve(strict=True)
    if output == root or root in output.parents:
        raise ExportError("output must be outside the source repository")
    if output.exists():
        raise ExportError("output already exists")
    if not output.parent.is_dir():
        raise ExportError("output parent must be an existing directory")

    commit, files = _source_files()
    snapshots = _snapshot_files(commit, files)
    _validate_contents(snapshots)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        for relative, content in snapshots.items():
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            _normalize_file(target)
        _normalize_directories(temporary)
        tree_digest = _tree_digest(temporary)
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return len(snapshots), tree_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory to create outside this repository")
    arguments = parser.parse_args()
    try:
        file_count, tree_hash = export_snapshot(arguments.output)
    except (ExportError, OSError) as error:
        print(f"public snapshot refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "file_count": file_count,
                "snapshot": str(arguments.output.expanduser().resolve(strict=False)),
                "tree_sha256": tree_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
