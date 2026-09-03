from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
VALIDATOR = ROOT / "scripts" / "validate_release_workflow.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SETUP_UV_COMMIT = "d0cc045d04ccac9d8b7881df0226f9e82c39688e"


def test_release_workflow_exists_and_static_validator_accepts_contract() -> None:
    assert VALIDATOR.is_file(), "task-009 release workflow validator is missing"
    assert WORKFLOW.is_file(), "task-009 release workflow is missing"

    result = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "release workflow contract: ok"


def test_release_workflow_uses_reviewed_setup_uv_commit() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert text.count(f"astral-sh/setup-uv@{SETUP_UV_COMMIT} # v6") == 3
    assert "astral-sh/setup-uv@d0d8abe699bfb85fec6de9f7adb5ae17292296ff" not in text


def test_release_validator_rejects_non_native_non_gated_workflow(tmp_path: Path) -> None:
    assert VALIDATOR.is_file(), "task-009 release workflow validator is missing"
    invalid = tmp_path / "release.yml"
    invalid.write_text(
        "name: release\non:\n  workflow_dispatch:\njobs:\n  publish:\n"
        "    runs-on: ubuntu-latest\n    steps: []\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--workflow", str(invalid)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    assert "release workflow contract" in result.stderr


def test_release_validator_rejects_mutable_action_reference(tmp_path: Path) -> None:
    invalid = tmp_path / "release.yml"
    text = WORKFLOW.read_text(encoding="utf-8")
    invalid.write_text(
        text.replace(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "actions/checkout@v4",
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--workflow", str(invalid)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    assert "actions pinned to full commit SHAs" in result.stderr
