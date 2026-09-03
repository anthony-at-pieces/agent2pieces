from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from scripts.release_artifacts import (  # noqa: E402
    ReleaseArtifactError,
    allowed_module_roots,
    load_manifest,
    validate_archive_entries,
    validate_binary_entries,
    validate_module_names,
)

MANIFEST = ROOT / "third_party" / "release_components.json"


def test_pyinstaller_has_a_separate_build_dependency_group() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = project["dependency-groups"]

    assert groups["build"] == ["pyinstaller>=6.12,<7"]
    assert {"include-group": "build"} in groups["dev"]
    assert not any(
        isinstance(requirement, str) and requirement.startswith("pyinstaller")
        for requirement in groups["dev"]
    )


def test_reviewed_component_manifest_is_deterministic_and_complete() -> None:
    manifest = load_manifest(MANIFEST)
    roots = allowed_module_roots(manifest, "linux")

    assert manifest["schema_version"] == 1
    assert {"agent2pieces", "fastapi", "mcp", "pydantic", "uvicorn"} <= roots
    assert "pywin32" not in {
        component["name"]
        for component in manifest["python_distributions"]
        if "linux" in component.get("platforms", ["linux"])
    }
    assert json.loads(MANIFEST.read_text(encoding="utf-8")) == manifest


def test_component_manifest_rejects_duplicate_module_ownership(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["python_distributions"][1]["modules"] = ["annotated_doc"]
    invalid = tmp_path / "components.json"
    invalid.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReleaseArtifactError, match="duplicate Python module roots"):
        load_manifest(invalid)


def test_component_manifest_rejects_unknown_binary_pattern_platform(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["python_distributions"][0]["binary_patterns"] = {"plan9": ["*.so"]}
    invalid = tmp_path / "components.json"
    invalid.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReleaseArtifactError, match="unsupported platforms"):
        load_manifest(invalid)


@pytest.mark.parametrize(
    "module",
    [
        "_pytest.fixtures",
        "_readline",
        "anyio.pytest_plugin",
        "certifi.core",
        "pytest_asyncio.plugin",
        "requests.sessions",
    ],
)
def test_module_validation_rejects_development_leaks(module: str) -> None:
    manifest = load_manifest(MANIFEST)

    with pytest.raises(ReleaseArtifactError, match="forbidden modules"):
        validate_module_names([module], manifest, "linux")


def test_module_validation_rejects_unreviewed_dependency() -> None:
    manifest = load_manifest(MANIFEST)

    with pytest.raises(ReleaseArtifactError, match="unmapped module roots"):
        validate_module_names(["surprise_package.client"], manifest, "linux")


def test_module_validation_accepts_stdlib_and_reviewed_runtime() -> None:
    manifest = load_manifest(MANIFEST)

    validate_module_names(
        [
            "_sysconfigdata__linux_x86_64-linux-gnu",
            "asyncio",
            "agent2pieces.cli",
            "mcp.client.sse",
            "pydantic.main",
        ],
        manifest,
        "linux",
    )


def test_binary_validation_rejects_readline() -> None:
    manifest = load_manifest(MANIFEST)

    with pytest.raises(ReleaseArtifactError, match="forbidden binaries"):
        validate_binary_entries(["libreadline.so.8"], manifest, "linux")


def test_archive_validation_rejects_unreviewed_and_forbidden_data() -> None:
    manifest = load_manifest(MANIFEST)

    with pytest.raises(ReleaseArtifactError, match="forbidden archive entries"):
        validate_archive_entries(["pytest-8.4.2.dist-info/METADATA"], manifest, "linux")
    with pytest.raises(ReleaseArtifactError, match="unmapped archive entries"):
        validate_archive_entries(["surprise-1.0.dist-info/METADATA"], manifest, "linux")


def test_archive_validation_accepts_reviewed_data_and_pyinstaller_runtime() -> None:
    manifest = load_manifest(MANIFEST)

    validate_archive_entries(
        [
            "PYZ.pyz",
            "agent2pieces/static/app.js",
            "jsonschema_specifications/schemas/draft7/metaschema.json",
            "pydantic-2.13.5.dist-info/METADATA",
            "pyi_rth_inspect",
        ],
        manifest,
        "linux",
    )


def test_binary_validation_rejects_unmapped_native_library() -> None:
    manifest = load_manifest(MANIFEST)

    with pytest.raises(ReleaseArtifactError, match="unmapped bundled binaries"):
        validate_binary_entries(["libsurprise.so.1"], manifest, "linux")


@pytest.mark.parametrize(
    ("target_platform", "binary"),
    [
        ("linux", "pydantic_core/libsurprise.so.9"),
        ("darwin", "pydantic_core/libsurprise.dylib"),
        ("win32", "pydantic_core/libsurprise.dll"),
        ("win32", "pydantic_core/libsurprise.pyd"),
    ],
)
def test_binary_validation_rejects_nested_unowned_binary(
    target_platform: str,
    binary: str,
) -> None:
    manifest = load_manifest(MANIFEST)

    with pytest.raises(ReleaseArtifactError, match="unmapped bundled binaries"):
        validate_binary_entries([binary], manifest, target_platform)


def test_binary_validation_rejects_ambiguous_component_ownership() -> None:
    manifest = load_manifest(MANIFEST)
    fastapi = next(
        component
        for component in manifest["python_distributions"]
        if component["name"] == "fastapi"
    )
    fastapi["binary_patterns"] = {
        "linux": ["pydantic_core/_pydantic_core.*.so"]
    }

    with pytest.raises(ReleaseArtifactError, match="ambiguous bundled binary ownership"):
        validate_binary_entries(
            ["pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so"],
            manifest,
            "linux",
        )


def test_binary_validation_maps_reviewed_runtime_and_extensions() -> None:
    manifest = load_manifest(MANIFEST)

    components = validate_binary_entries(
        [
            "libpython3.12.so.1.0",
            "libssl.so.3",
            "pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so",
            "python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so",
        ],
        manifest,
        "linux",
    )

    assert components == ("CPython", "OpenSSL")


@pytest.mark.parametrize(
    ("target_platform", "binary"),
    [
        ("linux", "_cffi_backend.cpython-312-x86_64-linux-gnu.so"),
        ("darwin", "cryptography/hazmat/bindings/_rust.abi3.so"),
        ("win32", "pydantic_core/_pydantic_core.cp312-win_amd64.pyd"),
        ("win32", "win32job.pyd"),
        ("win32", "win32/win32job.pyd"),
        ("win32", "pywin32_system32/pywintypes312.dll"),
    ],
)
def test_binary_validation_maps_reviewed_distribution_extensions(
    target_platform: str,
    binary: str,
) -> None:
    manifest = load_manifest(MANIFEST)

    assert validate_binary_entries([binary], manifest, target_platform) == ()


@pytest.mark.parametrize(
    ("target_platform", "binary"),
    [
        ("linux", "python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so"),
        ("darwin", "python3.12/lib-dynload/_ssl.cpython-312-darwin.so"),
        ("win32", "python312.dll"),
        ("win32", "_ssl.pyd"),
    ],
)
def test_binary_validation_maps_cpython_runtime(
    target_platform: str,
    binary: str,
) -> None:
    manifest = load_manifest(MANIFEST)

    assert validate_binary_entries([binary], manifest, target_platform) == ("CPython",)


def test_spec_excludes_known_development_and_copyleft_payloads() -> None:
    spec = (ROOT / "agent2pieces.spec").read_text(encoding="utf-8")

    assert 'collect_submodules("anyio")' not in spec
    for module in (
        "_pytest",
        "_readline",
        "anyio.pytest_plugin",
        "readline",
        "requests",
        "setuptools",
    ):
        assert f'"{module}"' in spec


def test_manifest_has_native_rules_for_every_release_platform() -> None:
    manifest = load_manifest(MANIFEST)

    assert set(manifest["native_libraries"]) == {"linux", "darwin", "win32"}
    assert sys.version_info >= (3, 12)
