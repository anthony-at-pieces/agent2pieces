# Agent2Pieces

Agent2Pieces discovers curated Codex, Claude Code, and Hermes memories for local
review, duplicate checking, and approved import through the Pieces MCP server.

It never modifies agent source files and never writes a memory until you confirm
the selected candidates, destination endpoint, and write count.

Agent2Pieces is an independent project by Anthony Maio. It is unofficial and is
not maintained or endorsed by Mesh Intelligent Technologies, Inc. Pieces is
Copyright (c) 2026 Mesh Intelligent Technologies, Inc.

## Development setup

Agent2Pieces requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Clone the
repository, then create a platform-local environment:

```text
git clone https://github.com/anthony-at-pieces/agent2pieces.git
cd agent2pieces
uv sync --all-groups
uv run agent2pieces --health-check
```

Do not share one `.venv` between Windows and WSL. Create the environment from the
same operating system that will run Agent2Pieces.

## Commands

After installation, use the console entry point:

```text
agent2pieces serve
agent2pieces serve --pieces-url http://127.0.0.1:39300 --no-open
agent2pieces scan
agent2pieces --health-check
```

The default Pieces MCP base URL is the local Pieces service. If Pieces runs on a
different host, pass its HTTP(S) base URL with `--pieces-url` or update it in the
local settings UI. You can copy the current endpoint from the PiecesOS MCP Servers
menu. The client tries Streamable HTTP first and falls back to legacy SSE.

From a source checkout, run the same commands through the locked environment:

```text
uv run agent2pieces serve --no-open
uv run agent2pieces scan
uv run agent2pieces --health-check
```

Build the native executable and deterministic release archive in a separate
runtime-plus-build environment. On macOS or Linux:

```text
export UV_PROJECT_ENVIRONMENT=.venv-release
uv sync --frozen --no-default-groups --group build
uv run --frozen --no-sync python scripts/build_native.py --clean
```

On Windows PowerShell:

```text
$env:UV_PROJECT_ENVIRONMENT = ".venv-release"
uv sync --frozen --no-default-groups --group build
uv run --frozen --no-sync python scripts/build_native.py --clean
```

Use a platform-local environment. Do not share `.venv-release` between Windows
and WSL.

The build inspects the PyInstaller executable, rejects forbidden or unmapped
components, and packages `THIRD_PARTY_NOTICES.txt`,
`THIRD_PARTY_COMPONENTS.json`, and the matching license files. The component
inventory is conservative: it records the reviewed dependency set for the target
platform even when PyInstaller omits an unused dependency's import modules from a
specific executable.

Both `serve` and `scan` accept repeatable `--source-root AGENT=/absolute/path`
overrides. Overrides are process-local. The web service binds only to
`127.0.0.1`.

Agent source files are opened read-only and are never modified.

## Public snapshot

The private engineering history and local acceptance evidence are not part of the
public source distribution. After the repository has passed its sanitation checks,
create a new history-free snapshot in a directory outside this checkout:

```text
python scripts/export_public_snapshot.py /absolute/path/to/new-public-snapshot
```

The exporter uses an explicit allowlist, rejects private distribution indicators,
and refuses to overwrite an existing directory.

## License

Agent2Pieces is available under the [MIT License](LICENSE). This v0.1 release is
intended for testing. Please report defects without including memory contents,
credentials, or local paths.
