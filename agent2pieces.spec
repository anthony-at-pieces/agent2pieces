# ruff: noqa: F821

import sys

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = sorted(
    set(
        [
            "mcp.client.sse",
            "mcp.client.streamable_http",
        ]
        + collect_submodules("uvicorn")
    )
)

a = Analysis(
    ["src/agent2pieces/cli.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        ("src/agent2pieces/static", "agent2pieces/static"),
        ("src/agent2pieces/migrations", "agent2pieces/migrations"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyInstaller",
        "_pyinstaller_hooks_contrib",
        "_pytest",
        "_readline",
        "altgraph",
        "anyio.pytest_plugin",
        "certifi",
        "charset_normalizer",
        "greenlet",
        "httpcore",
        "httpx",
        "iniconfig",
        "mypy",
        "mypy_extensions",
        "packaging",
        "pathspec",
        "pefile",
        "playwright",
        "pluggy",
        "pyee",
        "pygments",
        "pytest",
        "pytest_asyncio",
        "pytest_base_url",
        "pytest_playwright",
        "python_slugify",
        "pywin32_ctypes",
        "readline",
        "requests",
        "ruff",
        "slugify",
        "setuptools",
        "text_unidecode",
        "urllib3",
    ],
    noarchive=False,
    optimize=0,
)
if sys.platform == "win32":
    filtered_binaries = []
    for entry in a.binaries:
        binary_name = entry[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if binary_name.startswith("api-ms-win-") or binary_name == "ucrtbase.dll":
            continue
        filtered_binaries.append(entry)
    a.binaries = filtered_binaries
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agent2pieces",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
