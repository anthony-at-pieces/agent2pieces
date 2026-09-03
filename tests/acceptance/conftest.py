from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--artifact-dir",
        action="store",
        default=None,
        help="directory containing the native Agent2Pieces build and release archive",
    )


@pytest.fixture(scope="session")
def artifact_dir(request: pytest.FixtureRequest) -> Path:
    raw = request.config.getoption("--artifact-dir")
    if raw is None:
        pytest.skip("native artifact directory was not supplied")
    path = Path(str(raw)).resolve()
    if not path.is_dir():
        pytest.fail(f"artifact directory does not exist: {path}")
    return path
