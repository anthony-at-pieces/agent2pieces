"""Validate the release workflow without executing or importing YAML."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
VERSION_SOURCE = ROOT / "src" / "agent2pieces" / "__init__.py"


def _job_block(text: str, job: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job)}:\s*$\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)",
        text,
    )
    return "" if match is None else match.group(1)


def _contains_line(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.MULTILINE) is not None


def validate(text: str) -> list[str]:
    failures: list[str] = []

    action_refs = re.findall(r"(?m)^\s*- uses:\s*([^\s#]+)", text)
    pinned_actions = bool(action_refs) and all(
        re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference) is not None
        for reference in action_refs
    )

    checks = {
        "push tag trigger v*": _contains_line(text, r'^\s+tags:\s*\[\s*["\']v\*["\']\s*\]\s*$'),
        "workflow_dispatch dry-build trigger": _contains_line(text, r"^\s+workflow_dispatch:\s*$"),
        "read-only default permissions": _contains_line(text, r"^permissions:\s*$")
        and _contains_line(text, r"^  contents:\s*read\s*$"),
        "normal CI keeps live inventory disabled": "AGENT2PIECES_RUN_LIVE_INVENTORY" not in text,
        "actions pinned to full commit SHAs": pinned_actions,
    }
    failures.extend(name for name, passed in checks.items() if not passed)

    platform_contract = {
        "build-linux": ("ubuntu-latest", "agent2pieces-linux-x86_64", "linux-x86_64.tar.gz"),
        "build-windows": (
            "windows-latest",
            "agent2pieces-windows-x86_64",
            "windows-x86_64.zip",
        ),
        "build-macos": ("macos-14", "agent2pieces-macos-arm64", "macos-arm64.tar.gz"),
    }
    for job, (runner, artifact, archive) in platform_contract.items():
        block = _job_block(text, job)
        required = {
            "job exists": bool(block),
            f"native runner {runner}": f"runs-on: {runner}" in block,
            "Python 3.12": "python-version: \"3.12\"" in block,
            "frozen dependency install": "uv sync --frozen" in block,
            "native build": "scripts/build_native.py --clean" in block,
            "isolated native build environment": "UV_PROJECT_ENVIRONMENT" in block,
            "runtime and build dependencies only": (
                "uv sync --frozen --no-default-groups --group build" in block
            ),
            "build does not resync development dependencies": (
                "uv run --frozen --no-sync python scripts/build_native.py --clean" in block
            ),
            "native smoke": "tests/acceptance/test_packaged_binary.py --artifact-dir dist" in block,
            f"artifact name {artifact}": f"name: {artifact}" in block,
            f"archive {archive}": archive in block,
            "checksum upload": ".sha256" in block,
        }
        failures.extend(f"{job}: {name}" for name, passed in required.items() if not passed)

    linux = _job_block(text, "build-linux")
    linux_checks = {
        "Linux source tests": "uv run pytest tests/acceptance tests/integration tests/unit"
        in linux,
        "Linux browser tests": "uv run pytest tests/browser" in linux,
        "Linux browser install": "playwright install" in linux and "chromium" in linux,
        "Linux lint": "uv run ruff check ." in linux,
        "Linux type check": "uv run mypy src/agent2pieces" in linux,
    }
    failures.extend(name for name, passed in linux_checks.items() if not passed)

    release = _job_block(text, "release")
    release_checks = {
        "release job exists": bool(release),
        "release needs all native builds": all(
            job in release for job in ("build-linux", "build-windows", "build-macos")
        ),
        "release is tag push gated": "github.event_name == 'push'" in release
        and "startsWith(github.ref, 'refs/tags/v')" in release,
        "release has write permission": re.search(r"(?m)^      contents:\s*write\s*$", release)
        is not None,
        "release downloads build artifacts": "actions/download-artifact@" in release,
        "release validates the tag against the package version": "--release-tag" in release
        and '"$GITHUB_REF_NAME"' in release,
        "release publishes GitHub Release assets": "gh release create" in release,
    }
    failures.extend(name for name, passed in release_checks.items() if not passed)
    return failures


def validate_release_tag(tag: str) -> str | None:
    try:
        source = VERSION_SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        return f"cannot read package version: {error}"
    match = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$', source)
    if match is None:
        return "cannot determine package version"
    expected = f"v{match.group(1)}"
    if tag != expected:
        return f"release tag {tag!r} does not match package version {expected!r}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    parser.add_argument("--release-tag")
    arguments = parser.parse_args()
    try:
        text = arguments.workflow.read_text(encoding="utf-8")
    except OSError as error:
        print(f"release workflow contract: cannot read workflow: {error}", file=sys.stderr)
        return 1
    failures = validate(text)
    if arguments.release_tag is not None:
        tag_failure = validate_release_tag(arguments.release_tag)
        if tag_failure is not None:
            failures.append(tag_failure)
    if failures:
        print(
            "release workflow contract: failed: " + "; ".join(failures),
            file=sys.stderr,
        )
        return 1
    print("release workflow contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
