# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for building the OpenAgent CLI standalone executable.

Usage:
    pip install pyinstaller
    pyinstaller cli.spec --clean --noconfirm

Output: dist/openagent-cli (single-file binary).

Onefile mode for the same reason as the server spec: a lighter download
with no ``_internal/`` folder. The CLI bundle is ~13 MB compressed and
starts in well under a second on every subsequent launch.
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

# Build-environment guard — see openagent.spec for rationale.
import iroh  # noqa: F401 — P2P transport, Rust FFI dylib must be bundled

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
    # iroh: see openagent.spec for the full explanation. The CLI uses
    # iroh via openagent.network.iroh_node + .client.session for the
    # ``loopback`` / ``connect`` flows.
    "iroh",
    "iroh.iroh_ffi",
    *collect_submodules("iroh"),
    # The CLI imports openagent.network.* (client + iroh_node + transport)
    # for the loopback flow. We exclude the heavy server modules below
    # but still need the network subpackage.
    *collect_submodules("openagent.network"),
    "cbor2",
    "srptools",
    "cryptography",
    *collect_submodules("cryptography"),
]

# ── Data files ──
# certifi CA bundle for HTTPS requests (aiohttp needs this when bundled)

datas = []
datas += collect_data_files("certifi")

# ── Dynamic libs ──
# iroh's Rust FFI library (libiroh_ffi.{so,dylib,dll}) — see openagent.spec.
binaries = collect_dynamic_libs("iroh")

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

# onefile mode — see the note in openagent.spec. One artifact per platform.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="openagent-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
