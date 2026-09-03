# Contributing

Agent2Pieces targets Python 3.12. Create a platform-local development environment:

```text
uv sync --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
```

Keep changes narrow and add tests for behavior changes. Tests must use temporary
source roots and a fake MCP server. Do not commit real agent memories, local
acceptance output, credentials, database files, build artifacts, or machine-specific
paths. Never run a live Pieces write as part of an automated test.

Before preparing a public snapshot, run the sanitation exporter from the repository
root and inspect its output:

```text
python scripts/export_public_snapshot.py /absolute/path/to/new-public-snapshot
```

Open an issue before starting a large change. By submitting a contribution, you
agree that it may be distributed under the repository's MIT License.
