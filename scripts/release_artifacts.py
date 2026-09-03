"""Validate native bundles and generate deterministic third-party notices."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "third_party" / "release_components.json"


class ReleaseArtifactError(RuntimeError):
    """The native artifact or its license inventory is not releasable."""


@dataclass(frozen=True)
class ArtifactInventory:
    module_names: tuple[str, ...]
    binary_entries: tuple[str, ...]
    archive_entries: tuple[str, ...]
    native_components: tuple[str, ...]


def _object(value: object, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseArtifactError(f"{context} must be an object")
    return value


def _objects(value: object, *, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ReleaseArtifactError(f"{context} must be a list of objects")
    return value


def _strings(value: object, *, context: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ReleaseArtifactError(f"{context} must be a non-empty list of strings")
    return value


def _required_strings(component: dict[str, Any], *, context: str) -> None:
    for field in ("name", "license", "source"):
        if not isinstance(component.get(field), str) or not component[field]:
            raise ReleaseArtifactError(f"{context}.{field} must be a non-empty string")


def _validate_binary_pattern_map(component: dict[str, Any], *, context: str) -> None:
    value = component.get("binary_patterns")
    if value is None:
        return
    patterns_by_platform = _object(value, context=f"{context}.binary_patterns")
    unsupported = sorted(set(patterns_by_platform) - {"linux", "darwin", "win32"})
    if unsupported:
        raise ReleaseArtifactError(
            f"{context}.binary_patterns has unsupported platforms: "
            + ", ".join(unsupported)
        )
    for target_platform, patterns in patterns_by_platform.items():
        values = _strings(
            patterns,
            context=f"{context}.binary_patterns.{target_platform}",
        )
        if len(set(values)) != len(values):
            raise ReleaseArtifactError(
                f"{context}.binary_patterns.{target_platform} contains duplicate values"
            )


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        manifest = _object(json.loads(path.read_text(encoding="utf-8")), context="manifest")
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseArtifactError(f"cannot read component manifest: {error}") from error
    if manifest.get("schema_version") != 1:
        raise ReleaseArtifactError("unsupported component manifest schema")
    python_components = _objects(
        manifest.get("python_distributions"), context="python_distributions"
    )
    build_components = _objects(
        manifest.get("build_components"), context="build_components"
    )
    native_libraries = _object(
        manifest.get("native_libraries"), context="native_libraries"
    )
    names: set[str] = set()
    module_roots: set[str] = set()
    for index, component in enumerate(python_components):
        context = f"python_distributions[{index}]"
        _required_strings(component, context=context)
        name = str(component["name"])
        if name in names:
            raise ReleaseArtifactError(f"duplicate Python distribution: {name}")
        names.add(name)
        modules = _strings(component.get("modules"), context=f"{context}.modules")
        duplicates = module_roots.intersection(modules)
        if duplicates:
            raise ReleaseArtifactError(
                "duplicate Python module roots: " + ", ".join(sorted(duplicates))
            )
        module_roots.update(modules)
        _validate_binary_pattern_map(component, context=context)
    for index, component in enumerate(build_components):
        context = f"build_components[{index}]"
        _required_strings(component, context=context)
        if "distribution" not in component:
            _strings(
                component.get("license_candidates"),
                context=f"{context}.license_candidates",
            )
    for target_platform in ("linux", "darwin", "win32"):
        components = _objects(
            native_libraries.get(target_platform),
            context=f"native_libraries.{target_platform}",
        )
        native_names: set[str] = set()
        for index, component in enumerate(components):
            context = f"native_libraries.{target_platform}[{index}]"
            native_name = component.get("component")
            if not isinstance(native_name, str) or not native_name:
                raise ReleaseArtifactError(
                    f"{context}.component must be a non-empty string"
                )
            if native_name in native_names:
                raise ReleaseArtifactError(f"duplicate native component: {native_name}")
            native_names.add(native_name)
            for field in ("license", "source"):
                if not isinstance(component.get(field), str) or not component[field]:
                    raise ReleaseArtifactError(
                        f"{context}.{field} must be a non-empty string"
                    )
            _strings(component.get("patterns"), context=f"{context}.patterns")
            if "archive_patterns" in component:
                archive_patterns = _strings(
                    component["archive_patterns"],
                    context=f"{context}.archive_patterns",
                )
                if len(set(archive_patterns)) != len(archive_patterns):
                    raise ReleaseArtifactError(
                        f"{context}.archive_patterns contains duplicate values"
                    )
            if "covered_by" not in component:
                _strings(
                    component.get("license_candidates"),
                    context=f"{context}.license_candidates",
                )
    for field in ("forbidden_module_prefixes", "forbidden_binary_patterns"):
        values = _strings(manifest.get(field), context=field)
        if len(set(values)) != len(values):
            raise ReleaseArtifactError(f"{field} contains duplicate values")
    return manifest


def platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    raise ReleaseArtifactError(f"unsupported release platform: {sys.platform}")


def _applies(component: dict[str, Any], target_platform: str) -> bool:
    platforms = component.get("platforms")
    return platforms is None or target_platform in platforms


def allowed_module_roots(manifest: dict[str, Any], target_platform: str) -> set[str]:
    roots = {"agent2pieces"}
    for component in _objects(
        manifest["python_distributions"], context="python_distributions"
    ):
        if not _applies(component, target_platform):
            continue
        modules = component.get("modules")
        if not isinstance(modules, list) or not all(isinstance(item, str) for item in modules):
            raise ReleaseArtifactError("python distribution modules must be strings")
        roots.update(modules)
    return roots


def validate_module_names(
    module_names: list[str] | tuple[str, ...],
    manifest: dict[str, Any],
    target_platform: str,
) -> None:
    forbidden = tuple(str(value) for value in manifest["forbidden_module_prefixes"])
    violations = sorted(
        name
        for name in module_names
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    )
    if violations:
        raise ReleaseArtifactError("forbidden modules: " + ", ".join(violations[:20]))

    allowed = allowed_module_roots(manifest, target_platform) | set(sys.stdlib_module_names)
    roots = {name.split(".", 1)[0] for name in module_names}
    unknown = sorted(
        root
        for root in roots - allowed
        if not root.startswith("_sysconfigdata_")
    )
    if unknown:
        raise ReleaseArtifactError("unmapped module roots: " + ", ".join(unknown))


def _is_binary_entry(name: str) -> bool:
    basename = Path(name).name.lower()
    return (
        ".so" in basename
        or basename == "python"
        or basename.endswith(".dll")
        or basename.endswith(".dylib")
        or basename.endswith(".pyd")
    )


def _binary_patterns_for_platform(
    component: dict[str, Any],
    target_platform: str,
) -> tuple[str, ...]:
    value = component.get("binary_patterns")
    if not isinstance(value, dict):
        return ()
    patterns = value.get(target_platform)
    if not isinstance(patterns, list):
        return ()
    return tuple(str(pattern) for pattern in patterns)


def _matches_binary_pattern(name: str, pattern: str) -> bool:
    normalized_name = name.replace("\\", "/").lower()
    normalized_pattern = pattern.replace("\\", "/").lower()
    subject = (
        normalized_name
        if "/" in normalized_pattern
        else normalized_name.rsplit("/", 1)[-1]
    )
    return fnmatch.fnmatchcase(subject, normalized_pattern)


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def validate_archive_entries(
    archive_entries: list[str] | tuple[str, ...],
    manifest: dict[str, Any],
    target_platform: str,
) -> None:
    allowed_roots = (
        allowed_module_roots(manifest, target_platform) | set(sys.stdlib_module_names)
    )
    distributions = {
        _canonical_distribution_name(str(component.get("distribution", component["name"])))
        for component in _objects(
            manifest["python_distributions"], context="python_distributions"
        )
        if _applies(component, target_platform)
    }
    forbidden = {
        _canonical_distribution_name(str(value).split(".", 1)[0])
        for value in manifest["forbidden_module_prefixes"]
    }
    internal_names = {"PYZ.pyz", "base_library.zip", "cli", "struct"}
    internal_prefixes = ("pyi_", "pyiboot", "pyimod")
    unknown: list[str] = []
    ambiguous: list[str] = []
    violations: list[str] = []
    native = _object(manifest["native_libraries"], context="native_libraries")
    native_components = _objects(
        native.get(target_platform),
        context=f"native_libraries.{target_platform}",
    )
    for name in archive_entries:
        if _is_binary_entry(name):
            continue
        root = name.split("/", 1)[0]
        if root in internal_names or root.startswith(internal_prefixes):
            continue
        if root.endswith(".dist-info"):
            match = re.fullmatch(r"(.+?)-\d[^/]*\.dist-info", root)
            distribution = "" if match is None else _canonical_distribution_name(match[1])
            if distribution in forbidden:
                violations.append(name)
            elif distribution not in distributions:
                unknown.append(name)
            continue
        canonical_root = _canonical_distribution_name(root)
        if canonical_root in forbidden:
            violations.append(name)
            continue
        if (
            root in allowed_roots
            or root.startswith("_sysconfigdata_")
            or (root.startswith("python") and "/lib-dynload/" in name)
        ):
            continue
        owners = sorted(
            {
                str(component["component"])
                for component in native_components
                if any(
                    _matches_binary_pattern(name, str(pattern))
                    for pattern in component.get("archive_patterns", [])
                )
            }
        )
        if not owners:
            unknown.append(name)
        elif len(owners) > 1:
            ambiguous.append(f"{name} ({', '.join(owners)})")
    if violations:
        raise ReleaseArtifactError(
            "forbidden archive entries: " + ", ".join(sorted(violations)[:20])
        )
    if ambiguous:
        raise ReleaseArtifactError(
            "ambiguous bundled archive ownership: "
            + ", ".join(sorted(ambiguous)[:20])
        )
    if unknown:
        raise ReleaseArtifactError(
            "unmapped archive entries: " + ", ".join(sorted(unknown)[:20])
        )


def validate_binary_entries(
    binary_entries: list[str] | tuple[str, ...],
    manifest: dict[str, Any],
    target_platform: str,
) -> tuple[str, ...]:
    forbidden = tuple(str(value) for value in manifest["forbidden_binary_patterns"])
    violations = sorted(
        name
        for name in binary_entries
        if any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in forbidden)
    )
    if violations:
        raise ReleaseArtifactError("forbidden binaries: " + ", ".join(violations))

    native = _object(manifest["native_libraries"], context="native_libraries")
    native_components = _objects(
        native.get(target_platform),
        context=f"native_libraries.{target_platform}",
    )
    matched_components: set[str] = set()
    unknown: list[str] = []
    ambiguous: list[str] = []
    for name in binary_entries:
        if not _is_binary_entry(name):
            continue
        owners: list[tuple[str, str]] = []
        for component in _objects(
            manifest["python_distributions"], context="python_distributions"
        ):
            if not _applies(component, target_platform):
                continue
            if any(
                _matches_binary_pattern(name, pattern)
                for pattern in _binary_patterns_for_platform(component, target_platform)
            ):
                owners.append(("python", str(component["name"])))
        for component in native_components:
            if any(
                _matches_binary_pattern(name, str(pattern))
                for pattern in component["patterns"]
            ):
                owners.append(("native", str(component["component"])))
        unique_owners = sorted(set(owners))
        if not unique_owners:
            unknown.append(name)
            continue
        if len(unique_owners) != 1:
            owner_names = ", ".join(f"{kind}:{owner}" for kind, owner in unique_owners)
            ambiguous.append(f"{name} ({owner_names})")
            continue
        owner_kind, owner_name = unique_owners[0]
        if owner_kind == "native":
            matched_components.add(owner_name)
    if ambiguous:
        raise ReleaseArtifactError(
            "ambiguous bundled binary ownership: " + ", ".join(sorted(ambiguous))
        )
    if unknown:
        raise ReleaseArtifactError("unmapped bundled binaries: " + ", ".join(sorted(unknown)))
    return tuple(sorted(matched_components))


def inspect_artifact(
    executable: Path,
    manifest: dict[str, Any],
    target_platform: str,
) -> ArtifactInventory:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as error:
        raise ReleaseArtifactError("PyInstaller is required to inspect the artifact") from error
    try:
        archive = CArchiveReader(str(executable))
        embedded = archive.open_embedded_archive("PYZ.pyz")
    except Exception as error:
        raise ReleaseArtifactError(f"cannot inspect native artifact: {error}") from error
    module_names = tuple(sorted(str(name) for name in embedded.toc))
    archive_entries = tuple(sorted(str(name) for name in archive.toc))
    binary_entries = tuple(name for name in archive_entries if _is_binary_entry(name))
    validate_module_names(module_names, manifest, target_platform)
    validate_archive_entries(archive_entries, manifest, target_platform)
    native_components = validate_binary_entries(binary_entries, manifest, target_platform)
    return ArtifactInventory(
        module_names,
        binary_entries,
        archive_entries,
        native_components,
    )


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _copy_asset(source: Path, destination: Path) -> dict[str, str]:
    data = source.read_bytes()
    destination.write_bytes(data)
    return {
        "path": f"third_party_licenses/{destination.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _distribution_license_paths(distribution: importlib.metadata.Distribution) -> list[Path]:
    headers = distribution.metadata.get_all("License-File") or []
    files = list(distribution.files or ())
    matches: list[Path] = []
    for header in headers:
        normalized = str(header).replace("\\", "/")
        candidates = [
            item
            for item in files
            if str(item).replace("\\", "/").endswith(f"/{normalized}")
            or str(item).replace("\\", "/").endswith(f"/licenses/{normalized}")
        ]
        for item in candidates:
            path = Path(distribution.locate_file(item))
            if path.is_file() and path not in matches:
                matches.append(path)
    if not matches:
        for item in files:
            text = str(item).replace("\\", "/").lower()
            if ".dist-info/" not in text:
                continue
            if any(token in Path(text).name for token in ("license", "copying", "notice")):
                path = Path(distribution.locate_file(item))
                if path.is_file() and path not in matches:
                    matches.append(path)
    return sorted(matches, key=lambda path: path.as_posix().lower())


def _distribution_component(
    component: dict[str, Any],
    licenses_dir: Path,
) -> dict[str, Any]:
    name = str(component.get("distribution", component["name"]))
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as error:
        raise ReleaseArtifactError(f"required release distribution is missing: {name}") from error
    paths = _distribution_license_paths(distribution)
    if not paths:
        raise ReleaseArtifactError(f"no license files found for distribution: {name}")
    assets = []
    for index, path in enumerate(paths, 1):
        filename = f"{_safe_name(name)}-{distribution.version}-{index}-{_safe_name(path.name)}.txt"
        assets.append(_copy_asset(path, licenses_dir / filename))
    return {
        "name": str(component["name"]),
        "version": distribution.version,
        "license": str(component["license"]),
        "source": str(component["source"]),
        "license_files": assets,
    }


def _candidate_path(value: str) -> Path:
    return Path(value.format(base_prefix=sys.base_prefix, prefix=sys.prefix))


def _path_component(
    component: dict[str, Any],
    licenses_dir: Path,
    *,
    suffix: str,
) -> dict[str, Any]:
    name = str(component.get("name", component.get("component", "")))
    if not name:
        raise ReleaseArtifactError("component name is missing")
    candidates = component.get("license_candidates")
    if not isinstance(candidates, list) or not all(isinstance(item, str) for item in candidates):
        raise ReleaseArtifactError(f"license candidates missing for {name}")
    source = next((path for path in map(_candidate_path, candidates) if path.is_file()), None)
    if source is None:
        raise ReleaseArtifactError(f"no license source found for {name}")
    filename = f"{_safe_name(name)}-{suffix}-{_safe_name(source.name)}.txt"
    asset = _copy_asset(source, licenses_dir / filename)
    return {
        "name": name,
        "version": suffix,
        "license": str(component["license"]),
        "source": str(component["source"]),
        "license_files": [asset],
    }


def _native_component(
    component: dict[str, Any],
    licenses_dir: Path,
    target_platform: str,
    covered_components: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    covered_by = component.get("covered_by")
    if covered_by is not None:
        covered = covered_components.get(str(covered_by))
        if covered is None:
            raise ReleaseArtifactError(
                f"native component {component['component']} references missing {covered_by}"
            )
        return {
            "name": str(component["component"]),
            "version": target_platform,
            "license": str(component["license"]),
            "source": str(component["source"]),
            "covered_by": str(covered_by),
            "license_files": list(covered["license_files"]),
        }
    result = _path_component(component, licenses_dir, suffix=target_platform)
    expression = str(component["license"])
    common: list[Path] = []
    if "Apache-2.0" in expression:
        common.append(Path("/usr/share/common-licenses/Apache-2.0"))
    if "GPL-3" in expression:
        common.append(Path("/usr/share/common-licenses/GPL-3"))
    if "GPL-2" in expression:
        common.append(Path("/usr/share/common-licenses/GPL-2"))
    if "LGPL-2.1" in expression:
        common.append(Path("/usr/share/common-licenses/LGPL-2.1"))
    for index, path in enumerate((item for item in common if item.is_file()), 2):
        name = _safe_name(str(component["component"]))
        filename = f"{name}-{target_platform}-{index}-{_safe_name(path.name)}.txt"
        result["license_files"].append(_copy_asset(path, licenses_dir / filename))
    result["name"] = str(component["component"])
    return result


def create_release_materials(
    executable: Path,
    output_dir: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[Path, Path, Path]:
    manifest = load_manifest(manifest_path)
    target_platform = platform_key()
    inventory = inspect_artifact(executable, manifest, target_platform)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    licenses_dir = output_dir / "third_party_licenses"
    licenses_dir.mkdir(parents=True)

    components: list[dict[str, Any]] = []
    for component in _objects(manifest["python_distributions"], context="python_distributions"):
        if _applies(component, target_platform):
            components.append(_distribution_component(component, licenses_dir))
    for component in _objects(manifest["build_components"], context="build_components"):
        if "distribution" in component:
            components.append(_distribution_component(component, licenses_dir))
        else:
            components.append(
                _path_component(component, licenses_dir, suffix=sys.version.split()[0])
            )

    native_manifest = _object(manifest["native_libraries"], context="native_libraries")
    native_components = _objects(
        native_manifest[target_platform], context=f"native_libraries.{target_platform}"
    )
    by_name = {str(component["component"]): component for component in native_components}
    covered_components = {str(component["name"]): component for component in components}
    for name in inventory.native_components:
        if name == "CPython":
            continue
        component = by_name.get(name)
        if component is None:
            raise ReleaseArtifactError(f"native component is not declared: {name}")
        result = _native_component(
            component,
            licenses_dir,
            target_platform,
            covered_components,
        )
        if result is not None:
            components.append(result)

    components.sort(key=lambda component: str(component["name"]).lower())
    inventory_payload = {
        "schema_version": 1,
        "platform": target_platform,
        "python_version": sys.version.split()[0],
        "components": components,
        "artifact": {
            "archive_entries": list(inventory.archive_entries),
            "binary_entries": list(inventory.binary_entries),
            "module_roots": sorted({name.split(".", 1)[0] for name in inventory.module_names}),
        },
    }
    components_path = output_dir / "THIRD_PARTY_COMPONENTS.json"
    components_path.write_text(
        json.dumps(inventory_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    lines = [
        "Agent2Pieces third-party notices",
        "",
        "Full license texts are provided in third_party_licenses/.",
        "The component manifest records the SHA-256 of every license file.",
        "",
    ]
    for component in components:
        lines.extend(
            [
                f"Component: {component['name']} {component['version']}",
                f"License: {component['license']}",
                f"Source: {component['source']}",
            ]
        )
        lines.extend(f"License file: {item['path']}" for item in component["license_files"])
        lines.append("")
    notices_path = output_dir / "THIRD_PARTY_NOTICES.txt"
    notices_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return notices_path, components_path, licenses_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args()
    try:
        create_release_materials(
            arguments.executable,
            arguments.output_dir,
            manifest_path=arguments.manifest,
        )
    except ReleaseArtifactError as error:
        print(f"release artifact validation failed: {error}", file=sys.stderr)
        return 1
    print("release artifact validation: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
