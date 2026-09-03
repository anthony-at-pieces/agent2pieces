"""Build one native Agent2Pieces executable and deterministic release archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from release_artifacts import create_release_materials

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def _version() -> str:
    namespace: dict[str, str] = {}
    source = (ROOT / "src" / "agent2pieces" / "__init__.py").read_text(encoding="utf-8")
    exec(compile(source, "agent2pieces/__init__.py", "exec"), namespace)
    return namespace["__version__"]


def _platform_name() -> tuple[str, str]:
    machine = platform.machine().lower()
    system = platform.system()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64", "agent2pieces.exe"
    if system == "Linux" and machine in {"amd64", "x86_64"}:
        return "linux-x86_64", "agent2pieces"
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "macos-arm64", "agent2pieces"
    raise RuntimeError(f"unsupported native build platform: {system} {machine}")


def _clean() -> None:
    for path in (BUILD, DIST):
        if path.exists():
            shutil.rmtree(path)


def _tar_info(name: str, *, mode: int, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _release_members(
    executable: Path,
    materials_dir: Path,
) -> tuple[tuple[str, Path, int], ...]:
    fixed = (
        ("LICENSE", ROOT / "LICENSE", 0o644),
        ("README.md", ROOT / "README.md", 0o644),
        ("THIRD_PARTY_NOTICES.txt", materials_dir / "THIRD_PARTY_NOTICES.txt", 0o644),
        ("THIRD_PARTY_COMPONENTS.json", materials_dir / "THIRD_PARTY_COMPONENTS.json", 0o644),
    )
    licenses = tuple(
        (f"third_party_licenses/{path.name}", path, 0o644)
        for path in sorted((materials_dir / "third_party_licenses").iterdir())
        if path.is_file()
    )
    return fixed + licenses + (
        (executable.name, executable, 0o755),
    )


def _make_tar(
    archive: Path,
    root_name: str,
    members: tuple[tuple[str, Path, int], ...],
) -> None:
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as bundle,
    ):
        bundle.addfile(_tar_info(root_name, mode=0o755, directory=True))
        for name, source, mode in members:
            content = source.read_bytes()
            info = _tar_info(f"{root_name}/{name}", mode=mode, size=len(content))
            bundle.addfile(info, io.BytesIO(content))


def _make_zip(
    archive: Path,
    root_name: str,
    members: tuple[tuple[str, Path, int], ...],
) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        directory = zipfile.ZipInfo(f"{root_name}/", date_time=(1980, 1, 1, 0, 0, 0))
        directory.create_system = 3
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        bundle.writestr(directory, b"")
        for name, source, mode in members:
            info = zipfile.ZipInfo(
                f"{root_name}/{name}", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(
                info,
                source.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def _write_checksum(archive: Path) -> Path:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(f"{archive.name}.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
    return checksum


def build(*, clean: bool) -> tuple[Path, Path, Path]:
    if clean:
        _clean()
    DIST.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        command.append("--clean")
    command.append(str(ROOT / "agent2pieces.spec"))
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["SOURCE_DATE_EPOCH"] = "0"
    subprocess.run(command, cwd=ROOT, env=environment, check=True)

    platform_name, executable_name = _platform_name()
    executable = DIST / executable_name
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not create {executable_name}")
    if os.name != "nt":
        executable.chmod(0o755)
    materials_dir = BUILD / "release-materials"
    create_release_materials(executable, materials_dir)
    members = _release_members(executable, materials_dir)
    root_name = f"agent2pieces-v{_version()}-{platform_name}"
    if os.name == "nt":
        archive = DIST / f"{root_name}.zip"
        _make_zip(archive, root_name, members)
    else:
        archive = DIST / f"{root_name}.tar.gz"
        _make_tar(archive, root_name, members)
    checksum = _write_checksum(archive)
    return executable, archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    arguments = parser.parse_args()
    executable, archive, checksum = build(clean=arguments.clean)
    print(executable)
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
