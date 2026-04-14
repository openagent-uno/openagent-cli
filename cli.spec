# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for building the OpenAgent CLI standalone executable.

Usage:
    pip install pyinstaller
    pyinstaller cli.spec --clean --noconfirm

Output: dist/openagent-cli/ (onedir bundle)
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ── Hidden imports ──
# The CLI is much lighter than the server: just click, rich, aiohttp, and
# the openagent_cli package itself.

hiddenimports = [
    *collect_submodules("openagent_cli"),
    *collect_submodules("rich"),
    *collect_submodules("aiohttp"),
    *collect_submodules("anyio"),
    "click",
]

# ── Data files ──
# certifi CA bundle for HTTPS requests (aiohttp needs this when bundled)

datas = []
datas += collect_data_files("certifi")

# ── Analysis ──

a = Analysis(
    ["cli/openagent_cli/main.py"],
    pathex=["cli"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy packages not needed at runtime
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "tkinter",
        "test",
        "unittest",
        # Exclude the full openagent server — CLI is a thin client
        "openagent",
        "litellm",
        "mcp",
        "claude_agent_sdk",
        "agno",
        "openai",
        "anthropic",
        "google",
        "sqlalchemy",
        "aiosqlite",
        "croniter",
        "yaml",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="openagent-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="openagent-cli",
)
